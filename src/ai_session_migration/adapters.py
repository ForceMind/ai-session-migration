from __future__ import annotations

from collections import defaultdict, deque
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tomllib
import copy
from urllib.parse import urlparse, unquote
from typing import Iterator

from .models import Attachment, Catalog, Message, SessionPage, SessionRecord, identity
from .safeio import SnapshotStore, header, open_lines, read_json, walk_files, within, stable_bytes, owned_file, directory_issue, remote_path


def timestamp(value) -> float:
    try:
        if isinstance(value, (float, int)):
            return value / 1000 if value > 100000000000 else float(value)
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def display_json(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def content_messages(content, role="assistant", label="") -> list[Message]:
    result = []
    if isinstance(content, str):
        return [Message(role, content, label=label)] if content else []
    if not isinstance(content, list):
        return result
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type", "unknown")
        if not isinstance(kind, str):
            kind = "unknown"
        if kind in {"text", "input_text", "output_text"}:
            result.append(Message(role, str(block.get("text", "")), label=label))
        elif kind in {"thinking", "reasoning", "reasoning_text", "redacted_thinking"}:
            text = block.get("thinking", block.get("text", block.get("data", "")))
            result.append(Message("thinking", str(text) if text else "该记录为不可直接展示的思考块；原始内容保留在来源中。", "thinking", "思考记录 · 原样保留"))
        elif kind in {"tool_use", "tool-call", "function_call"}:
            result.append(Message("tool", display_json(block.get("input", block.get("arguments", block))), "tool", str(block.get("name", "工具调用"))))
        elif kind in {"tool_result", "function_call_output"}:
            result.append(Message("tool", display_json(block.get("content", block.get("output", block))), "tool", "历史工具结果"))
        elif kind in {"image", "input_image", "localImage", "file", "document"}:
            result.append(Message(role, "附件保存在原始记录中，可在附件页查看可用内容。", "attachment", "附件记录"))
        else:
            result.append(Message("event", display_json(block), "event", "原始内容块 · " + str(kind)))
    return result


def claude_messages(event: dict) -> list[Message]:
    kind = event.get("type", "")
    if kind in {"user", "assistant"}:
        msg = event.get("message", {})
        if isinstance(msg, dict):
            return content_messages(msg.get("content", []), msg.get("role", kind))
    if kind == "summary":
        return [Message("event", str(event.get("summary", "")), "event", "来源保存的上下文摘要")]
    return []


def codex_messages(item: dict) -> list[Message]:
    if not isinstance(item, dict):
        return [Message("event", display_json(item), "event", "未知原始记录")]
    kind = item.get("type", "")
    if not isinstance(kind, str):
        return [Message("event", display_json(item), "event", "未知原始记录")]
    if kind == "userMessage":
        return content_messages(item.get("content", []), "user")
    if kind == "agentMessage":
        return [Message("assistant", str(item.get("text", "")))]
    if kind == "reasoning":
        blocks = (item.get("summary") or []) + (item.get("content") or [])
        text = "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in blocks)
        return [Message("thinking", text or "该来源没有保存可展示的思考文本；原始记录仍保留。", "thinking", "思考记录 · 原样保留")]
    if kind == "commandExecution":
        text = str(item.get("command", ""))
        if item.get("aggregatedOutput"):
            text += "\n\n" + str(item["aggregatedOutput"])
        return [Message("tool", text, "tool", "历史命令记录")]
    if kind == "mcpToolCall":
        text = display_json({k: item[k] for k in ("arguments", "result", "error") if item.get(k) is not None})
        return [Message("tool", text, "tool", str(item.get("tool", "MCP 工具记录")))]
    if kind in {"fileChange", "webSearch", "imageView", "collabAgentToolCall", "subAgentActivity"}:
        return [Message("tool", display_json(item), "tool", {"fileChange": "历史文件修改记录", "webSearch": "历史搜索记录"}.get(kind, "历史工具记录"))]
    if kind == "message":
        return content_messages(item.get("content", []), item.get("role", "assistant"))
    if kind in {"function_call", "function_call_output"}:
        return [Message("tool", display_json(item), "tool", "历史工具记录")]
    return []


def dsh_messages(event: dict) -> list[Message]:
    kind, data = event.get("type", ""), event.get("data", {})
    if not isinstance(data, dict):
        return []
    if kind in {"user/message", "assistant/message", "tool/result", "system/message"}:
        msg = data.get("message", data)
        if isinstance(msg, dict):
            role = msg.get("role", "user" if kind == "user/message" else "tool" if kind == "tool/result" else "assistant")
            if kind == "tool/result":
                return [Message("tool", display_json(msg.get("content", [])), "tool", "历史工具结果")]
            return content_messages(msg.get("content", []), role)
    if kind == "tool/call":
        # The same native tool call can also appear inside assistant/message.
        return []
    if "compaction" in kind:
        return [Message("event", display_json(data), "event", "来源保存的上下文整理记录")]
    return []


class LocalLibrary:
    def __init__(self, home: Path, cache: Path, custom_sources: list[dict] | None = None, check_project_paths=True):
        self.home = home.resolve()
        self.custom_sources = custom_sources or []
        self.check_project_paths = check_project_paths
        protected = [self.home / ".claude", self.home / ".codex", self.home / ".dsh", self.home / "Library/Application Support/Claude", self.home / "Library/Application Support/Codex"] + [Path(s["path"]) for s in self.custom_sources if s.get("path")]
        protected.extend(p for p in (self.home / "Library/Application Support").glob("Claude-*") if p.is_dir())
        protected.extend(Path(os.environ[v]).expanduser() for v in ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "DSH_HOME"] if os.environ.get(v))
        if any(within(cache, root) for root in protected):
            raise ValueError("本工具缓存不能保存在来源资料中")
        self.snapshots = SnapshotStore(cache / "sqlite")
        self.catalog = Catalog()
        self._codex_exports: dict[str, list[dict]] = {}

    def discover(self):
        sources = []
        candidates = [("claude", self.home / ".claude"), ("codex", self.home / ".codex"), ("deepseek", self.home / ".dsh")]
        for provider, variable in [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("deepseek", "DSH_HOME")]:
            if self.home == Path.home().resolve() and os.environ.get(variable):
                candidates.append((provider, Path(os.environ[variable]).expanduser()))
        for provider, path in candidates:
            if path.is_dir():
                sources.append({"provider": provider, "path": str(path.resolve())})
        sources.extend(self.custom_sources)
        unique = {}
        for source in sources:
            if source.get("provider") not in {"claude", "codex", "deepseek"}:
                continue
            p = Path(source.get("path", "")).expanduser().resolve()
            if p == self.home or p == Path(p.anchor):
                continue
            unique[(source["provider"], str(p))] = {"provider": source["provider"], "path": str(p)}
        return list(unique.values())

    def scan(self, progress=lambda *_: None) -> Catalog:
        result = Catalog()
        for source in self.discover():
            provider, root = source["provider"], Path(source["path"])
            progress("正在读取 " + {"claude": "Claude", "codex": "Codex", "deepseek": "DeepSeek Harness"}[provider], 0)
            try:
                records = getattr(self, "_scan_" + provider)(root)
                source.update(status="可读取", count=len([r for r in records if not r.child]), total=len(records))
                result.records.extend(records)
            except Exception as error:
                source.update(status="需要关注", count=0, error=type(error).__name__ + "：" + str(error))
                result.warnings.append(source["error"])
            result.sources.append(source)
        result.records = list({r.uid: r for r in result.records}.values())
        result.records.sort(key=lambda r: r.updated, reverse=True)
        self.catalog = result
        return result

    def _scan_claude(self, root: Path):
        code = root if (root / "projects").is_dir() else self.home / ".claude"
        if not (code / "projects").is_dir():
            return []
        index = defaultdict(list)
        profiles = list((self.home / "Library/Application Support").glob("Claude*"))
        if (root / "claude-code-sessions").is_dir():
            profiles.append(root)
        for profile in set(profiles):
            if not profile.is_dir() or not (profile.name == "Claude" or profile.name.startswith("Claude-")):
                continue
            for p in (profile / "claude-code-sessions").rglob("local_*.json"):
                d = read_json(p, {})
                if isinstance(d, dict) and d.get("cliSessionId"):
                    index[str(d["cliSessionId"])].append((p, d))
        logs = defaultdict(list)
        for p in (code / "projects").rglob("*.jsonl"):
            if not p.is_symlink():
                logs[p.stem].append(p)
        records = []
        for sid, files in logs.items():
            links = sorted(index.get(sid, []), key=lambda x: timestamp(x[1].get("lastActivityAt")), reverse=True)
            data = links[0][1] if links else {}
            cwd, title = str(data.get("cwd", "")), str(data.get("title", ""))
            child = any("subagents" in p.parts for p in files)
            if not title or not cwd:
                it = open_lines(files[0])
                try:
                    for i, event in enumerate(it):
                        if i > 30:
                            break
                        if not isinstance(event, dict):
                            continue
                        cwd = cwd or str(event.get("cwd", ""))
                        if event.get("type") == "custom-title":
                            title = title or str(event.get("customTitle", ""))
                        if cwd and title:
                            break
                except Exception:
                    pass
                finally:
                    it.close()
            record = SessionRecord(identity("claude", str(code), sid), "claude", sid, title or ("子任务 · " if child else "未命名会话 · ") + sid[:12], str(code), cwd, timestamp(data.get("lastActivityAt")) or max(p.stat().st_mtime for p in files), bool(data.get("isArchived")), child=child)
            record.files = [str(p) for p in files]
            record.metadata = {"indexes": [str(p) for p, _ in links], "desktop_ids": sorted({str(d.get("sessionId", "")) for _, d in links}), "primary_file": str(files[0]), "copies": len(files)}
            if len(files) > 1:
                record.issue = "存在多份原始日志，备份会全部保留"
            record.issue = directory_issue(cwd, self.check_project_paths) or record.issue
            records.append(record)
        return records

    def _scan_codex(self, root: Path):
        db_root = root
        try:
            config = tomllib.loads(stable_bytes(root / "config.toml", 2 * 1024 * 1024).decode())
            override = config.get("sqlite_db_path")
            if isinstance(override, str) and Path(override).expanduser().is_dir():
                db_root = Path(override).expanduser().resolve()
                if within(self.snapshots.directory, db_root):
                    raise ValueError("缓存不能位于历史数据库目录中")
        except (OSError, tomllib.TOMLDecodeError):
            pass
        databases = sorted(db_root.glob("state_*.sqlite"), key=lambda p: int(re.search(r"_(\d+)\.sqlite", p.name)[1]))
        if not databases:
            return self._scan_codex_logs(root)
        records = []
        c = self.snapshots.connect(databases[-1])
        try:
            columns = {x[1] for x in c.execute("PRAGMA table_info(threads)")}
            for row in c.execute("SELECT * FROM threads"):
                d = dict(row)
                source = str(d.get("source", ""))
                sid = str(d["id"])
                cwd = str(d.get("cwd", ""))
                record = SessionRecord(identity("codex", str(root), sid), "codex", sid, str(d.get("name") or d.get("title") or "未命名会话 · " + sid[:12]), str(root), cwd, timestamp(d.get("updated_at_ms") or d.get("updated_at")), bool(d.get("archived")), bool(d.get("is_pinned")), child="subagent" in source)
                original = Path(str(d.get("rollout_path", "")))
                candidates = [original, Path(str(original) + ".zst")]
                if not any(owned_file(p, root) for p in candidates):
                    candidates = list((root / "sessions").glob("**/*" + sid + ".jsonl*")) + list((root / "archived_sessions").glob("*" + sid + ".jsonl*"))
                record.files = [str(p) for p in candidates if owned_file(p, root)]
                history_dbs = sorted(db_root.glob("thread_history_*.sqlite"))
                record.metadata = {"state_database": str(databases[-1]), "history_database": str(history_dbs[-1]) if history_dbs else "", "history_mode": d.get("history_mode", "legacy"), "model_provider": d.get("model_provider", ""), "parent_id": d.get("attached_thread_parent_id"), "internal_review": '"guardian"' in source, "native_thread_metadata": d}
                record.issue = directory_issue(cwd, self.check_project_paths)
                records.append(record)
        finally:
            c.close()
        return records

    def _scan_codex_logs(self, root):
        result = []
        for folder in [root / "sessions", root / "archived_sessions"]:
            for p in folder.glob("**/*.jsonl*"):
                if not p.is_file() or p.is_symlink():
                    continue
                try:
                    h = header(p).get("payload", {})
                    sid = str(h.get("id", p.stem))
                    record = SessionRecord(identity("codex", str(root), sid), "codex", sid, "历史会话 · " + sid[:12], str(root), str(h.get("cwd", "")), p.stat().st_mtime, folder.name == "archived_sessions", files=[str(p)])
                    result.append(record)
                except Exception:
                    continue
        return result

    def _scan_deepseek(self, root: Path):
        registry = read_json(root / "storages/workspace.json", {})
        global_state = registry.get("global", {})
        workspaces = registry.get("tables", {}).get("workspaces", {})
        owners = {}
        for wid in global_state.get("workspaceIds", []):
            workspace = workspaces.get(wid, {})
            for sid in workspace.get("sessionIds", []):
                owners[sid] = {"workspace_id": wid, "project_title": workspace.get("title", ""), "workspace_path": workspace.get("path", "")}
        archived, pinned = set(global_state.get("archivedSessionIds", [])), set(global_state.get("pinnedSessionIds", []))
        records = []
        for directory in (root / "sessions").glob("*/*"):
            if not directory.is_dir() or directory.is_symlink():
                continue
            generations = []
            for p in directory.iterdir():
                match = re.fullmatch(r"session(?:\.v(\d+))?\.jsonl(?:\.zstd)?", p.name)
                if match and p.is_file():
                    generations.append((int(match[1] or 0), p))
            if not generations:
                continue
            generations.sort(key=lambda x: x[0], reverse=True)
            active_version, active = generations[0]
            try:
                h = header(active)
            except Exception:
                h = {}
            sid = str(h.get("id", directory.name))
            projection = read_json(root / "storages/session_projcache/sessions" / (sid + ".json"), {})
            cache = projection.get("record", {})
            cache_title = cache.get("rows", {}).get("title", {}).get("val", "")
            lifecycle = cache.get("identity", {})
            title_matches = lifecycle.get("formatVersion") == active_version and lifecycle.get("createdAt") == h.get("createdAt") and lifecycle.get("isSeeded", False) == h.get("isSeeded", False)
            title = cache_title if title_matches and isinstance(cache_title, str) else "未命名会话 · " + sid[:12]
            owner = owners.get(sid, {})
            cwd = str(owner.get("workspace_path") or h.get("cwd", ""))
            record = SessionRecord(identity("deepseek", str(root), sid), "deepseek", sid, title, str(root), cwd, active.stat().st_mtime, sid in archived, sid in pinned, bool(h.get("delegationDepth", 0)), files=[str(p) for _, p in generations])
            record.metadata = {"primary_file": str(active), "format_version": active_version, "registered": sid in owners, "title_from_cache": title_matches, "is_seeded": h.get("isSeeded", False), **owner}
            if active_version != 4 or not h.get("id"):
                record.issue = "格式待确认，原始文件可保全"
            else:
                record.issue = directory_issue(cwd, self.check_project_paths)
            records.append(record)
        return records

    def raw_events(self, record: SessionRecord) -> Iterator[dict]:
        if record.provider == "codex":
            root = Path(record.root)
            databases = sorted(root.glob("thread_history_*.sqlite"))
            configured = record.metadata.get("history_database")
            if configured and Path(configured).is_file():
                databases = [Path(configured)]
            if databases:
                fixed = getattr(self, "_fixed_history", {}).get(str(databases[-1].resolve()))
                if fixed:
                    c = sqlite3.connect(Path(fixed).as_uri() + "?mode=ro", uri=True)
                    c.execute("PRAGMA query_only=ON")
                    c.row_factory = sqlite3.Row
                else:
                    c = self.snapshots.connect(databases[-1])
                try:
                    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "thread_items" in tables:
                        found = False
                        for row in c.execute("SELECT * FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal, item_id", (record.session_id,)):
                            found = True
                            yield {"_source_table": "thread_items", "_source_row": dict(row), "item": json.loads(row["item_json"])}
                        if found:
                            return
                finally:
                    c.close()
            for file in record.files:
                for event in open_lines(Path(file)):
                    if isinstance(event, dict) and event.get("type") == "response_item":
                        yield {"item": event.get("payload", {}), "_original_event": event}
                return
        else:
            file = Path(record.metadata.get("primary_file") or record.files[0])
            for event in open_lines(file):
                if isinstance(event, dict):
                    yield event

    def for_backup(self, selected):
        reader = copy.copy(self)
        reader._fixed_history = {}
        for record in selected:
            if record.provider != "codex" or record.metadata.get("preview_only"):
                continue
            configured = record.metadata.get("history_database")
            candidates = [Path(configured)] if configured and Path(configured).is_file() else sorted(Path(record.root).glob("thread_history_*.sqlite"))
            if candidates:
                source = candidates[-1].resolve()
                if str(source) not in reader._fixed_history:
                    reader._fixed_history[str(source)] = self.snapshots.sqlite(source)
        return reader

    def page(self, record: SessionRecord, limit=100, cancel=None) -> SessionPage:
        result = SessionPage()
        tail = deque(maxlen=limit)
        if record.provider == "deepseek" and record.metadata.get("format_version") != 4:
            result.warnings.append("此代际尚未启用正文解析；原始代际文件可以备份保全。")
            return result
        for event in self.raw_events(record):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("已取消旧会话的读取")
            if "_viewer_warning" in event:
                result.warnings.append(event["_viewer_warning"])
                continue
            messages = codex_messages(event.get("item", {})) if record.provider == "codex" else claude_messages(event) if record.provider == "claude" else dsh_messages(event)
            for msg in messages:
                result.total += 1
                # Truncation is presentation-only; exports use the unmodified event.
                if len(msg.text) > 120000:
                    msg.text = msg.text[:120000] + "\n\n[预览已截断；备份保留完整原始记录]"
                tail.append(msg)
            self._event_attachments(record, event, result.attachments)
        result.messages = list(tail)
        self._file_attachments(record, result.attachments)
        unique = {}
        for a in result.attachments:
            unique[(a.path, a.name, hashlib.sha256(a.data.encode()).hexdigest() if a.data else "")] = a
        result.attachments = list(unique.values())
        result.warnings = list(dict.fromkeys(result.warnings))
        return result

    def _event_attachments(self, record, event, output, include_embedded=True):
        roots = [Path(record.root) / "projects", Path(record.root) / "uploads", Path(record.root) / "attachments"]
        if record.provider == "codex":
            roots += [Path(record.root) / "generated_images"]
        if record.cwd and not remote_path(record.cwd):
            project = Path(record.cwd)
            if project.is_absolute() and len(project.parts) >= 5 and project != self.home:
                roots.append(project)
        def markdown_images(text):
            for match in re.finditer(r"!\[([^\]]*)\]\(([^\n]+?)\)", text):
                value = match[2].strip().strip("<>")
                if value.startswith("file:"):
                    url = urlparse(value)
                    if url.netloc not in {"", "localhost"}:
                        continue
                    value = unquote(url.path)
                f = Path(value)
                if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"} and any(owned_file(f, root) for root in roots):
                    output.append(Attachment(match[1] or f.name, str(f)))
        def visit(value, depth=0):
            if depth > 18:
                return
            if isinstance(value, dict):
                t = value.get("type", "")
                if t in ("text", "input_text", "output_text") and isinstance(value.get("text"), str):
                    markdown_images(value["text"])
                if isinstance(t, str) and t in {"image", "input_image", "localImage", "document"}:
                    attachment = value.get("attachment", {})
                    if record.provider == "deepseek" and isinstance(attachment, dict):
                        aid = str(attachment.get("attachmentId", ""))
                        if re.fullmatch(r"[a-f0-9]{64}", aid):
                            f = Path(record.root) / "attachments/v1/objects" / aid[:2] / aid
                            output.append(Attachment(str(attachment.get("name") or "图片附件"), str(f) if f.is_file() else "", note="" if f.is_file() else "来源附件缺失", media_type=str(attachment.get("mediaType", ""))))
                    src = value.get("source", {})
                    if include_embedded and isinstance(src, dict) and src.get("type") == "base64" and isinstance(src.get("data"), str) and len(src["data"]) < 28 * 1024 * 1024:
                        output.append(Attachment("会话内嵌图片", kind="embedded", data=src["data"], media_type=str(src.get("media_type", "image/png"))))
                    for key in ["path", "file_path", "filePath"]:
                        p = value.get(key) or (src.get(key) if isinstance(src, dict) else None)
                        if isinstance(p, str):
                            f = Path(p)
                            if any(owned_file(f, root) for root in roots):
                                output.append(Attachment(f.name, str(f)))
                for child in value.values():
                    if isinstance(child, (list, dict)):
                        visit(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    visit(child, depth + 1)
        if record.provider == "claude":
            value = event.get("message", {}).get("content", []) if isinstance(event.get("message"), dict) else event.get("attachment", {})
        elif record.provider == "codex":
            item = event.get("item", {})
            value = [item.get("content", []), item.get("result", {})] if isinstance(item, dict) else []
            if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                markdown_images(item["text"])
        else:
            data = event.get("data", {})
            message = data.get("message", data) if isinstance(data, dict) else {}
            value = message.get("content", []) if isinstance(message, dict) else []
        visit(value)

    def _file_attachments(self, record, output):
        for file in record.files:
            p = Path(file)
            owned = p.parent / record.session_id if record.provider == "claude" else p.parent / "attachments"
            if owned.is_dir():
                for f in walk_files(owned):
                    if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".md", ".txt", ".json"}:
                        output.append(Attachment(f.name, str(f)))

    def original_files(self, record: SessionRecord):
        """Only session-owned paths; never collect arbitrary paths from history text."""
        result = []
        root = Path(record.root)
        for value in record.files:
            f = Path(value)
            if owned_file(f, root):
                result.append((f, "original-session"))
                owned = f.parent / record.session_id if record.provider == "claude" else f.parent / "attachments"
                if owned.is_dir():
                    result.extend((x, "attachment-or-tool-result") for x in walk_files(owned))
        uploads = root / "uploads" / record.session_id
        if uploads.is_dir():
            result.extend((x, "attachment") for x in walk_files(uploads))
        return result

    def shared_attachments(self, selected: list[SessionRecord]):
        roots = {Path(r.root) for r in selected if r.provider == "deepseek"}
        return [(p, "shared-attachment") for root in roots for p in walk_files(root / "attachments/v1/objects")]

    def selected_registry(self, selected: list[SessionRecord]):
        output = []
        for root in {r.root for r in selected if r.provider == "deepseek"}:
            ids = {r.session_id for r in selected if r.root == root and r.provider == "deepseek"}
            raw = read_json(Path(root) / "storages/workspace.json", {})
            global_state = raw.get("global", {})
            projects = {}
            for wid, project in raw.get("tables", {}).get("workspaces", {}).items():
                members = [x for x in project.get("sessionIds", []) if x in ids]
                if members:
                    projects[wid] = {**project, "sessionIds": members}
            output.append({"sourceRoot": root, "unit": raw.get("unit"), "global": {"workspaceIds": [x for x in global_state.get("workspaceIds", []) if x in projects], "archivedSessionIds": [x for x in global_state.get("archivedSessionIds", []) if x in ids], "pinnedSessionIds": [x for x in global_state.get("pinnedSessionIds", []) if x in ids]}, "tables": {"workspaces": projects}, "derivedSelection": True})
        return output

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import zipfile

from .adapters import LocalLibrary, claude_messages, codex_messages, dsh_messages, timestamp
from .backup import SCHEMA
from .portable import PORTABLE_SCHEMA
from .models import Attachment, Catalog, SessionPage, SessionRecord, identity
from .safeio import CHUNK, ReadFailure, file_signature, header, open_lines, independent_copy, hash_file


class PackageLibrary(LocalLibrary):
    """Preview data packages in a private cache; never import into source clients."""
    def __init__(self, archive: Path, cache: Path):
        self._extract_lock = threading.RLock()
        self.archive = archive.resolve()
        signature = str(self.archive) + str(self.archive.stat().st_size) + str(self.archive.stat().st_mtime_ns)
        self.cache = cache / "packages" / hashlib.sha256(signature.encode()).hexdigest()
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        super().__init__(Path.home(), cache / "package-db")
        with zipfile.ZipFile(self.archive) as z:
            if z.getinfo("manifest.json").file_size > 512 * CHUNK:
                raise ValueError("数据包清单过大")
            self.manifest = json.loads(z.read("manifest.json"))
            for root in self.manifest.get("roots", []):
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(root.get("label", ""))):
                    raise ValueError("数据包的来源标识不安全")
            self.names = set(z.namelist())
            if len(self.names) != len(z.namelist()):
                raise ValueError("数据包存在重复对象")
            if any(PurePosixPath(x).is_absolute() or ".." in PurePosixPath(x).parts or "\\" in x or ":" in x for x in self.names):
                raise ValueError("数据包路径不安全")
            if self.manifest.get("schema") in {SCHEMA, PORTABLE_SCHEMA}:
                if z.getinfo("catalog.json").file_size > 64 * CHUNK:
                    raise ValueError("会话目录过大")
                raw = z.read("catalog.json")
                if hashlib.sha256(raw).hexdigest() != self.manifest["catalogSha256"]:
                    raise ValueError("会话目录校验失败")
                self.package_catalog = json.loads(raw)
                self.entries = {e["object"]: e for e in self.manifest["entries"]}
            elif self.manifest.get("schema") == "conversation-migration-1":
                self.package_catalog = None
                self.entries = {e["object"]: {**e, "size": e["capturedSize"]} for e in self.manifest["entries"] if "object" in e}
            else:
                raise ValueError("尚未支持的数据包格式，原文件未改变")
        self.legacy_adapters = {}
        self._legacy_histories = {}
        self._legacy_history_files = {}
        self._legacy_logs = {}
        self._origins = {}

    def _extract(self, object_name: str, filename="payload") -> Path:
        with self._extract_lock:
            return self._extract_locked(object_name, filename)

    def _extract_locked(self, object_name: str, filename="payload") -> Path:
        if object_name not in self.entries or not re.fullmatch(r"objects/\d{8}", object_name):
            raise ValueError("数据对象引用无效")
        entry = self.entries[object_name]
        suffix = Path(filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ".bin"
        target = self.cache / (object_name.split("/")[-1] + "-payload" + suffix)
        if target.exists() and target.stat().st_size == entry["size"] and hash_file(target) == entry["sha256"]:
            return target
        if shutil.disk_usage(self.cache).free < entry["size"] + 128 * CHUNK:
            raise ReadFailure("预览该数据对象需要更多缓存空间，请在设置中更改缓存位置")
        part = target.with_suffix(target.suffix + ".part")
        h, size = hashlib.sha256(), 0
        try:
            with zipfile.ZipFile(self.archive) as z, z.open(object_name) as source, part.open("wb") as dst:
                for block in iter(lambda: source.read(CHUNK), b""):
                    h.update(block)
                    size += len(block)
                    if size > entry["size"]:
                        raise ValueError("对象长度超出清单")
                    dst.write(block)
            if size != entry["size"] or h.hexdigest() != entry["sha256"]:
                raise ValueError("数据对象完整性校验失败")
            os.replace(part, target)
            return target
        finally:
            part.unlink(missing_ok=True)

    def scan(self, progress=lambda *_: None):
        if self.package_catalog is not None:
            records = [SessionRecord.from_dict(d) for d in self.package_catalog["records"]]
            if len({r.uid for r in records}) != len(records):
                raise ValueError("数据包存在重复的会话标识，未进行合并或导入")
            for record in records:
                record.metadata = {**record.metadata, "package": str(self.archive), "preview_only": True}
            self.catalog = Catalog(records, [{"provider": p, "path": str(self.archive), "status": "数据包预览", "count": sum(r.provider == p and not r.child for r in records)} for p in sorted({r.provider for r in records})])
            return self.catalog
        records, sources, warnings = [], [], []
        self.by_root_path = {(e["root"], e["path"]): e for e in self.manifest["entries"] if "object" in e}
        if len(self.by_root_path) != len(self.entries):
            raise ValueError("旧数据包存在重复的来源文件路径")
        for root in self.manifest.get("roots", []):
            kind, label = root["kind"], root["label"]
            if kind == "codex":
                state = self.legacy_databases(label, "state")
                if not state:
                    continue
                progress("读取旧迁移包的会话目录", 0)
                cached_root = self.cache / label
                cached_root.mkdir(exist_ok=True)
                state_file = self._extract(state[-1]["object"], state[-1]["path"])
                canonical = cached_root / Path(state[-1]["path"]).name
                if not canonical.exists():
                    independent_copy(state_file, canonical)
                elif canonical.is_symlink() or canonical.is_junction() or hash_file(canonical) != state[-1]["sha256"]:
                    raise ValueError("旧包数据库缓存已变化，请更改缓存位置后重新打开")
                adapter = LocalLibrary(Path.home(), self.cache / "db-cache", check_project_paths=False)
                subset = adapter._scan_codex(cached_root, db_root_override=cached_root)
                history = self.legacy_databases(label, "thread_history")
                if history:
                    self._legacy_histories[label] = history[-1]
                self._legacy_logs[label] = [e for (r, p), e in self.by_root_path.items() if r == label
                                            and self.legacy_log_path(p, {"sessions", "archived_sessions"})]
                for record in subset:
                    record.uid = identity("codex", str(self.archive) + label, record.session_id)
                    record.metadata.update(package=str(self.archive), package_root_label=label, preview_only=True)
                    self.legacy_adapters[record.uid] = adapter
                records.extend(subset)
                sources.append({"provider": "codex", "path": str(self.archive), "status": "旧迁移包预览", "count": len([r for r in subset if not r.child])})
            elif kind == "claude-code":
                subset = self.legacy_claude_records(label)
                records.extend(subset)
                sources.append({"provider": "claude", "path": str(self.archive), "status": "旧迁移包预览", "count": sum(not r.child for r in subset)})
        warnings.append("旧数据包按索引预览；按需解压所选历史到本工具缓存，不写入任何原客户端。")
        self.catalog = Catalog(sorted(records, key=lambda r: r.updated, reverse=True), sources, warnings)
        return self.catalog

    def legacy_databases(self, label, prefix):
        pattern = re.compile(re.escape(prefix) + r"_(\d+)\.sqlite")
        found = [(int(match[1]), entry) for (root, path), entry in self.by_root_path.items()
                 if root == label and (match := pattern.fullmatch(path))]
        return [entry for _, entry in sorted(found, key=lambda pair: pair[0])]

    @staticmethod
    def legacy_log_path(value, folders):
        path = PurePosixPath(value)
        return (len(path.parts) >= 2 and path.parts[0] in folders and path.as_posix() == value
                and not any(part == ".." or ":" in part or "\\" in part for part in path.parts)
                and path.name.endswith((".jsonl", ".jsonl.zst", ".jsonl.zstd")))

    def legacy_claude_records(self, label):
        with zipfile.ZipFile(self.archive) as z:
            if "session-catalog.json" in self.names:
                if z.getinfo("session-catalog.json").file_size > 64 * CHUNK:
                    raise ValueError("旧包会话索引过大")
                index = json.loads(z.read("session-catalog.json")).get("claudeDesktop", [])
            else:
                index = []
        by_id = {}
        for item in index:
            sid = item.get("cliSessionId")
            updated = timestamp(item.get("lastActivityAt"))
            if sid and (sid not in by_id or timestamp(by_id[sid].get("lastActivityAt")) < updated):
                by_id[sid] = item
        logs = defaultdict(list)
        for (root, path), entry in self.by_root_path.items():
            if root != label or not self.legacy_log_path(path, {"projects"}):
                continue
            parts = PurePosixPath(path).parts
            child = len(parts) == 5 and parts[3] == "subagents"
            if len(parts) != 3 and not child:
                continue
            sid = re.sub(r"\.jsonl(?:\.zst[d]?)?$", "", parts[-1])
            logs[sid, parts[2] if child else ""].append(entry)
        location = self.cache / label
        for (sid, parent), entries in logs.items():
            data = {} if parent else by_id.get(sid, {})
            yield SessionRecord(
                identity("claude", str(self.archive) + label, sid + (":" + parent if parent else "")),
                "claude", sid, data.get("title") or ("子代理 · " if parent else "未命名会话 · ") + sid[:12],
                str(location), data.get("cwd", ""), timestamp(data.get("lastActivityAt")),
                bool(data.get("isArchived")), child=bool(parent), files=[str(location / e["path"]) for e in entries],
                metadata={"package": str(self.archive), "preview_only": True, "legacy_objects": [e["object"] for e in entries]},
            )

    def legacy_rollout(self, record):
        label = record.metadata["package_root_label"]
        declared = str(record.metadata.get("native_thread_metadata", {}).get("rollout_path") or "").replace("\\", "/")
        matches, preferred = [], []
        for entry in self._legacy_logs.get(label, []):
            relative = entry["path"]
            exact = any(declared == path or declared.endswith("/" + path)
                        for path in (relative, relative.removesuffix(".zst").removesuffix(".zstd")))
            basename = PurePosixPath(relative).name
            possible = any(basename.endswith(suffix) for suffix in (record.session_id + ".jsonl", record.session_id + ".jsonl.zst",
                                                                    record.session_id + ".jsonl.zstd"))
            if not (exact or possible):
                continue
            local = self._extract(entry["object"], relative)
            start = header(local)
            if start.get("type") == "session_meta" and start.get("payload", {}).get("id") == record.session_id:
                (preferred if exact else matches).append((local, relative))
        chosen = preferred or matches
        if len(chosen) > 1:
            raise ValueError("旧包中该 Codex 会话存在多份不同来源的 rollout，请先核对原包")
        return chosen[0] if chosen else None

    def legacy_codex_record(self, record):
        label = record.metadata["package_root_label"]
        history = self._legacy_histories.get(label)
        path = None
        if history:
            with self._extract_lock:
                ready, signature = self._legacy_history_files.get(label, (None, None))
                if ready is not None and ready.is_file() and not ready.is_symlink() and file_signature(ready) == signature:
                    path = ready
                else:
                    path = self._extract(history["object"], history["path"])
                    self._legacy_history_files[label] = (path, file_signature(path))
        metadata = dict(record.metadata, preview_only=False, history_database=str(path) if path else "")
        return replace(record, metadata=metadata, files=[])

    def native_database_rows(self, record):
        if self.package_catalog is None and record.uid in self.legacy_adapters:
            yield from self.legacy_adapters[record.uid].native_database_rows(self.legacy_codex_record(record))

    def _event_attachments(self, record, event, output, include_embedded=True):
        # Package exports can copy declared attachment objects, never paths on the current computer.
        return None

    def raw_events(self, record):
        if self.package_catalog is not None:
            name = self.package_catalog["histories"][record.uid]
            for event in open_lines(self._extract(name, "history.jsonl")):
                yield event
        elif record.provider == "claude":
            names = record.metadata.get("legacy_objects", [])
            if names:
                yield from open_lines(self._extract(names[0], self.entries[names[0]]["path"]))
        elif record.provider == "codex":
            local = self.legacy_codex_record(record)
            found = False
            for event in self.legacy_adapters[record.uid].raw_events(local):
                found = True
                yield event
            if not found and (rollout := self.legacy_rollout(record)):
                for event in open_lines(rollout[0]):
                    if event.get("type") == "response_item":
                        yield {"item": event.get("payload", {}), "_original_event": event}

    def page(self, record, limit=100, cancel=None):
        result = SessionPage()
        messages = deque(maxlen=limit)
        for event in self.raw_events(record):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("已取消旧会话的读取")
            if "_viewer_warning" in event:
                result.warnings.append(event["_viewer_warning"])
                continue
            values = codex_messages(event.get("item", {})) if record.provider == "codex" else claude_messages(event) if record.provider == "claude" else dsh_messages(event)
            for msg in values:
                result.total += 1
                if len(msg.text) > 120000:
                    msg.text = msg.text[:120000] + "\n[预览截断；原始对象未改变]"
                messages.append(msg)
        result.messages = list(messages)
        # Extract only attachments owned by this selected session, not project trees.
        if self.package_catalog is not None:
            for name, entry in self.entries.items():
                if (entry.get("sessionUid") == record.uid or record.uid in entry.get("sessionUids", [])) and "attachment" in entry.get("role", ""):
                    original = entry.get("originalPath", "附件")
                    result.attachments.append(Attachment(entry.get("displayName") or Path(original).name, str(self._extract(name, Path(original).name)), media_type=entry.get("mediaType", "")))
        result.warnings.append("当前为数据包只读预览，尚未执行原生导入或续聊。")
        return result

    def original_files(self, record):
        if self.package_catalog is not None:
            for name, entry in self.entries.items():
                if (entry.get("sessionUid") == record.uid or record.uid in entry.get("sessionUids", [])) and entry.get("role") != "lossless-history-export":
                    if entry.get("role") in {"project-work-file", "project-local-change"} and not getattr(self, "_include_packaged_projects", False):
                        continue
                    cached = self._extract(name, Path(entry.get("originalPath") or entry.get("logicalName") or "payload").name)
                    self._origins[str(cached)] = {"originalPath": entry.get("originalPath") or entry.get("logicalName") or "derived-record", **{k: entry[k] for k in ["logicalName", "displayName", "mediaType", "projectId", "projectPath", "readingNumber", "sourceSha256"] if k in entry}}
                    yield cached, entry.get("role", "original-session")
        elif record.provider == "claude":
            for name in record.metadata.get("legacy_objects", []):
                relative = self.entries[name]["path"]
                cached = self._extract(name, relative)
                self._origins[str(cached)] = {"originalPath": str(Path(record.root) / relative)}
                yield cached, "original-session"
        elif record.provider == "codex" and (rollout := self.legacy_rollout(record)):
            cached, relative = rollout
            self._origins[str(cached)] = {"originalPath": str(Path(record.root) / relative)}
            yield cached, "original-session"

    def origin_metadata(self, path):
        return self._origins.get(str(path), {"originalPath": str(path)})

    def selected_registry(self, selected):
        result = []
        selected_ids = {}
        for r in selected:
            if r.provider == "deepseek":
                selected_ids.setdefault(r.root, set()).add(r.session_id)
        for raw in self.package_catalog.get("registries", []) if self.package_catalog else []:
            ids = selected_ids.get(raw.get("sourceRoot"), set())
            if not ids:
                continue
            projects = {wid: {**p, "sessionIds": [s for s in p.get("sessionIds", []) if s in ids]} for wid, p in raw.get("tables", {}).get("workspaces", {}).items() if any(s in ids for s in p.get("sessionIds", []))}
            g = raw.get("global", {})
            result.append({**raw, "global": {"workspaceIds": [w for w in g.get("workspaceIds", []) if w in projects], "archivedSessionIds": [s for s in g.get("archivedSessionIds", []) if s in ids], "pinnedSessionIds": [s for s in g.get("pinnedSessionIds", []) if s in ids]}, "tables": {"workspaces": projects}})
        return result

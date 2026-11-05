"""Offline native import. Stage complete targets; publish only after backup/readback.

Package SQL is never executed. Existing session IDs are never updated. Full previous
target directories remain available for conditional rollback, including after a crash.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
import ctypes
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import zstandard

from .adapters import LocalLibrary
from .backup import check_cancel as _check_cancel, verify_package
from .models import SessionRecord
from .packages import PackageLibrary
from .safeio import (CHUNK, MAX_LINE, SnapshotStore, atomic_json, hash_file,
                     independent_copy, open_lines, remote_path, stable_bytes, within)

SCHEMA = "asm-native-import-1"
PROVIDERS = {"claude", "codex", "deepseek"}
HISTORY_TABLES = {"thread_turns", "thread_items", "thread_history_projection_state", "thread_realtime_items"}
STATE_TABLES = {"threads", "projects", "project_roots", "thread_attachments", "thread_spawn_edges"}


def check_cancel(cancel):
    if cancel is not None:
        _check_cancel(cancel)


def checked_directory(value: Path) -> Path:
    value = Path(value).expanduser()
    if remote_path(value) or not value.is_absolute() or ".." in value.parts:
        raise ValueError("请选择本机绝对目录，不使用网络路径或上级跳转")
    for p in [value, *value.parents]:
        if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            raise ValueError("导入目录不能经过符号链接或目录联接")
    value = value.resolve()
    if value == Path.home().resolve() or len(value.parts) < 3:
        raise ValueError("请选择专用客户端资料目录，不能选择用户目录或磁盘根目录")
    if value.exists() and not value.is_dir():
        raise ValueError("目标位置不是目录")
    return value


def inventory(root: Path, progress=lambda *_: None, cancel=None) -> dict:
    """Hash every regular file; refuse incomplete backups, links and special files."""
    if not root.exists():
        return {"exists": False, "files": {}}
    checked_directory(root)
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            check_cancel(cancel)
            p = Path(base) / name
            st = p.lstat()
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise ValueError("目标含链接或特殊文件，无法作完整安全备份：" + str(p))
            relative = p.relative_to(root).as_posix()
            if stat.S_ISREG(st.st_mode):
                digest = hash_file(p)
                after = p.stat()
                if (st.st_size, st.st_mtime_ns, st.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError("目标资料仍在变化，请退出客户端后重新预览")
                result[relative] = {"sha256": digest, "bytes": st.st_size, "mode": stat.S_IMODE(st.st_mode)}
            else:
                result[relative + "/"] = {"directory": True, "mode": stat.S_IMODE(st.st_mode)}
        progress("检查目标资料 " + root.name, 0)
    return {"exists": True, "files": result}


def native_processes(targets: dict[str, str]) -> list[str]:
    """Refuse known live native clients for normal client homes; never terminate them."""
    defaults = {Path.home() / ".claude", Path.home() / ".codex", Path.home() / ".dsh"}
    defaults.update(Path(os.environ[v]).expanduser() for v in ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "DSH_HOME"] if os.environ.get(v))
    if not any(Path(p).resolve() in {d.resolve() for d in defaults} for p in targets.values()):
        return []
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            names = [r[0].lower() for r in csv.reader(io.StringIO(out)) if r]
            return sorted({n for n in names if n in {"claude.exe", "codex.exe", "dsh.exe", "deepseek.exe"}})
        out = subprocess.run(["ps", "-ax", "-o", "comm="], capture_output=True, text=True, check=True, timeout=15).stdout
        return sorted({Path(line.strip()).name for line in out.splitlines() if Path(line.strip()).name.lower() in {"claude", "codex", "dsh", "deepseek"}})
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("无法检查原客户端是否已退出，未开始写入：" + type(error).__name__) from error


def relative_original(value: str, root: str) -> PurePosixPath:
    cls = PureWindowsPath if re.match(r"^[A-Za-z]:", root) or "\\" in root else PurePosixPath
    try:
        relative = cls(value).relative_to(cls(root))
    except ValueError as error:
        raise ValueError("包内文件不属于声明的来源目录") from error
    if not relative.parts or any(x in {"..", ".", ""} or ":" in x or "\\" in x for x in relative.parts):
        raise ValueError("包内文件相对路径不安全")
    return PurePosixPath(*relative.parts)


def safe_segment(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) or value.upper() in {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}:
        raise ValueError("此会话 ID 不适合跨平台原生导入，原数据仍可预览")
    return value


def dsh_project_key(cwd: str) -> str:
    # Same UTF-16 code-unit contract as the first-party persistence backend.
    result, separator = "", False
    raw = cwd.encode("utf-16-le", errors="surrogatepass")
    for i in range(0, len(raw), 2):
        code = int.from_bytes(raw[i:i + 2], "little")
        ch = chr(code)
        if ch in "/\\:":
            if not separator:
                result += "-"
            separator = True
        else:
            result += ch if re.fullmatch(r"[A-Za-z0-9._-]", ch) else "~" + format(code, "04X")
            separator = False
    return "--" + (result.lstrip("-") or "root")[:251] + "--"


def entries_for(package: PackageLibrary, record: SessionRecord):
    return [e for e in package.entries.values() if e.get("sessionUid") == record.uid or record.uid in e.get("sessionUids", [])]


def native_rows(package, record):
    entries = [e for e in entries_for(package, record) if e.get("role") == "native-database-rows"]
    if not entries:
        raise ValueError("包内缺少 Codex 原生数据库记录，请使用新版客户端重新导出该会话")
    rows = []
    for entry in entries:
        for event in open_lines(package._extract(entry["object"], "native.jsonl")):
            if "row" in event:
                rows.append(event)
    if not any(e.get("table") == "threads" and e.get("row", {}).get("id") == record.session_id for e in rows):
        raise ValueError("Codex 原生会话索引不完整")
    return rows


def source_logs(package, record):
    result = []
    for e in entries_for(package, record):
        if e.get("role") == "original-session":
            rel = relative_original(e.get("originalPath", ""), record.root)
            result.append((e, rel))
    return result


def codex_databases(root: Path, expected_root=None):
    # A custom sqlite_home must be selected as a target together with its logs;
    # do not silently mutate another directory outside this transaction.
    import tomllib
    expected_root = Path(expected_root or root).resolve()
    if os.environ.get("CODEX_SQLITE_HOME") and Path(os.environ["CODEX_SQLITE_HOME"]).expanduser().resolve() != expected_root:
        raise ValueError("CODEX_SQLITE_HOME 指向独立目录，此版不跨目录替换；需在目标客户端使用同目录存储")
    config = root / "config.toml"
    if config.is_file():
        data = tomllib.loads(stable_bytes(config).decode())
        if data.get("sqlite_db_path") and Path(data["sqlite_db_path"]).expanduser().resolve() != expected_root:
            raise ValueError("目标使用独立 SQLite 目录，此版不跨目录替换；请在目标客户端改用同目录存储后再预览")
    state = sorted(root.glob("state_*.sqlite"), key=lambda p: int(re.search(r"_(\d+)\.sqlite$", p.name)[1]))
    history = sorted(root.glob("thread_history_*.sqlite"))
    if not state:
        raise ValueError("Codex 目标尚未初始化，请先在目标电脑打开一次 Codex 后退出；数据库须位于所选资料目录")
    return state[-1], history[-1] if history else None


@dataclass
class ImportPlan:
    archive: str
    archive_sha256: str
    targets: dict
    before: dict
    items: list[dict]
    mappings: dict
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self):
        return [i for i in self.items if i["action"] == "import"]

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


def build_plan(package: PackageLibrary, selected: list[SessionRecord], targets: dict, mappings: dict, cache: Path, progress=lambda *_: None, cancel=None) -> ImportPlan:
    if not selected:
        raise ValueError("请先选择要导入的包内会话")
    if package.package_catalog is None:
        raise ValueError("此旧数据包只支持预览；请回到源电脑重新识别本地资料，并用新版导出后导入")
    verify_package(package.archive, progress, cancel)
    package = PackageLibrary(package.archive, cache)
    records = {r.uid: r for r in package.scan().records}
    providers = {r.provider for r in selected}
    if not providers <= PROVIDERS or not providers <= targets.keys():
        raise ValueError("请为每种来源选择目标资料目录")
    clean = {p: str(checked_directory(Path(targets[p]))) for p in sorted(providers)}
    if "claude" in providers and targets.get("claude_desktop"):
        clean["claude_desktop"] = str(checked_directory(Path(targets["claude_desktop"])))
    for p, v in clean.items():
        root = Path(v)
        if within(package.archive, root) or within(cache, root):
            raise ValueError("迁移包和本工具缓存必须在目标资料之外")
        for other, path in clean.items():
            if p != other and (within(root, Path(path)) or within(Path(path), root)):
                raise ValueError("不同来源的目标资料目录不能重叠")
    live = native_processes(clean)
    if live:
        raise ValueError("请先正常退出原客户端，再预览导入：" + "、".join(live))
    before, existing, issues = {}, {}, {}
    reader = LocalLibrary(cache / "isolated-home", cache / "import-preview")
    for provider, value in clean.items():
        root = Path(value)
        before[provider] = inventory(root, progress, cancel)
        try:
            if provider == "claude_desktop":
                existing[provider] = {str(json.loads(stable_bytes(p)).get("cliSessionId")) for p in root.glob("local_*.json")}
                continue
            existing[provider] = {r.session_id for r in getattr(reader, "_scan_" + provider)(root)}
            if provider == "codex":
                codex_databases(root)
                existing[provider].update(r.session_id for r in reader._scan_codex_logs(root))
            if provider == "deepseek" and (root / "storages/workspace.json").exists():
                registry = json.loads(stable_bytes(root / "storages/workspace.json"))
                if registry.get("unit") != {"name": "workspace", "version": 2}:
                    raise ValueError("DeepSeek 项目注册表版本不兼容")
                existing[provider].update(sid for p in registry.get("tables", {}).get("workspaces", {}).values() for sid in p.get("sessionIds", []))
        except Exception as error:
            existing[provider] = set()
            issues[provider] = str(error)
    items, seen = [], {}
    for selected_record in selected:
        check_cancel(cancel)
        record = records.get(selected_record.uid)
        if record is None:
            raise ValueError("数据包目录已变化，请重新打开迁移包")
        item = {"record": record.to_dict(), "action": "import", "reason": "新增会话；保留现有资料", "cwd": str(mappings.get(record.cwd, ""))}
        try:
            safe_segment(record.session_id)
            if record.provider in issues:
                raise ValueError(issues[record.provider])
            if record.provider == "claude" and "claude_desktop" in issues:
                raise ValueError("Desktop 索引目录不兼容：" + issues["claude_desktop"])
            if record.session_id in existing[record.provider]:
                item.update(action="skip", reason="目标已有相同 ID，保留目标记录并跳过")
            elif record.provider == "claude" and record.session_id in existing.get("claude_desktop", set()):
                item.update(action="skip", reason="目标 Desktop 索引已有同一 Code 会话，未覆盖")
            elif not item["cwd"] or not checked_directory(Path(item["cwd"])).is_dir():
                raise ValueError("请将原项目映射到目标机已存在的项目目录")
            elif record.provider in {"claude", "deepseek"} and not source_logs(package, record):
                raise ValueError("缺少可原生恢复的原始日志")
            elif record.provider == "deepseek" and record.metadata.get("format_version") != 4:
                raise ValueError("当前仅支持 DeepSeek v4 原生导入，其他代际仍可保全/预览")
            elif record.provider == "codex":
                native = native_rows(package, record)
                state, history = codex_databases(Path(clean["codex"]))
                if any(r.get("table") in HISTORY_TABLES for r in native) and history is None:
                    raise ValueError("Codex 目标缺少分页历史数据库，请先用相容版本初始化")
                for row in native:
                    table = row.get("table")
                    if table not in STATE_TABLES | HISTORY_TABLES:
                        continue
                    c = reader.snapshots.connect(history if table in HISTORY_TABLES else state)
                    try:
                        _check_row_shape(c, table, row["row"])
                        if table == "thread_attachments" and c.execute('SELECT 1 FROM thread_attachments WHERE id=?', (row["row"].get("id"),)).fetchone():
                            raise ValueError("目标存在同 ID 附件记录，未覆盖")
                    finally:
                        c.close()
            if item["action"] == "import" and record.provider == "claude" and "claude_desktop" in clean:
                indexes = [e for e in entries_for(package, record) if e.get("role") == "claude-desktop-index"]
                if not indexes:
                    raise ValueError("包内缺少 Desktop 原始索引；请重新导出，或清空可选的 Desktop 索引目录")
                name = PurePosixPath(indexes[0]["originalPath"].replace("\\", "/")).name
                if (Path(clean["claude_desktop"]) / name).exists():
                    item.update(action="skip", reason="目标已有同名 Desktop 索引文件，未覆盖")
            key = (record.provider, record.session_id)
            if key in seen:
                item.update(action="blocked", reason="包内多来源副本使用相同 ID，请只选择一份再预览")
                seen[key].update(action="blocked", reason=item["reason"])
            seen[key] = item
        except (ValueError, OSError) as error:
            item.update(action="blocked", reason=str(error))
        items.append(item)
    # A child must keep its parent relationship, rather than silently becoming a root.
    dependencies = {}
    for item in items:
        if item["action"] != "import":
            continue
        record = SessionRecord.from_dict(item["record"])
        parent = None
        if record.provider == "claude" and record.child:
            logs = source_logs(package, record)
            rel = logs[0][1] if logs else None
            if rel and len(rel.parts) >= 5 and rel.parts[3] == "subagents":
                parent = rel.parts[2]
        elif record.provider == "codex":
            parent = record.metadata.get("parent_id")
        elif record.provider == "deepseek":
            parent = next(package.raw_events(record), {}).get("parentSession")
        if parent:
            dependencies[(record.provider, record.session_id)] = str(parent)
    changed = True
    while changed:
        changed = False
        available = {(i["record"]["provider"], i["record"]["session_id"]) for i in items if i["action"] == "import"}
        available.update((p, sid) for p, values in existing.items() for sid in values)
        for item in items:
            key = (item["record"]["provider"], item["record"]["session_id"])
            if item["action"] == "import" and key in dependencies and (key[0], dependencies[key]) not in available:
                item.update(action="blocked", reason="父会话未选择或不可导入；请同时选择父会话，保留父子关系")
                changed = True
    # Include only providers that can actually receive at least one session.
    active = {i["record"]["provider"] for i in items if i["action"] == "import"}
    if "claude" in active and "claude_desktop" in clean:
        active.add("claude_desktop")
    clean = {p: v for p, v in clean.items() if p in active}
    before = {p: v for p, v in before.items() if p in active}
    return ImportPlan(str(package.archive), hash_file(package.archive), clean, before, items, mappings,
                      ["导入会修改目标副本中的目录/索引字段；原包、正文与 thinking 保留。", "只恢复历史资料，不恢复登录、权限、Goals、队列或执行任务。", "Claude 支持本地 Claude Code 会话；云端聊天不属于此导入接口。", "Desktop 索引仅在包内有原始索引且选择了目标索引目录时恢复；其他情况恢复 Code 本地日志。", "项目工作文件不写入；历史正文中的旧路径保留。DeepSeek 会在原客户端重建显示缓存。"])


def _write_new(path: Path, data: bytes):
    checked_directory(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())


def _copy_new(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if hash_file(source) == hash_file(destination):
            return
        raise ValueError("目标已存在不同的文件，未覆盖：" + str(destination))
    independent_copy(source, destination)


def _attachment_refs(value, mapping):
    """Remap only declared file/image fields; no text/thinking/tool arguments."""
    if isinstance(value, list):
        return [_attachment_refs(v, mapping) for v in value]
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if str(value.get("type", "")).lower() in {"image", "input_image", "image_url", "localimage", "file", "input_file", "imagefile"}:
        def locations(v):
            if isinstance(v, list):
                return [locations(x) for x in v]
            if isinstance(v, dict):
                return {k: mapping.get(x, x) if k in {"path", "file_path", "filePath", "localPath", "imagePath", "url"} and isinstance(x, str) else locations(x) for k, x in v.items()}
            return v
        return locations(value)
    for key in ["content", "message", "data", "item", "payload", "attachments", "images"]:
        if key in value:
            result[key] = _attachment_refs(value[key], mapping)
    return result


def _rewrite_log(source: Path, destination: Path, provider: str, cwd: str, cancel, sid, mapping=None):
    """Only header/top-level location metadata changes; never historical content."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise ValueError("目标日志已存在，未覆盖")
    raw = source.open("rb")
    reader = zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) if source.name.endswith((".zstd", ".zst")) else raw
    offsets, original_offset, target_offset = {0: 0}, 0, 0
    try:
        stream = io.BufferedReader(reader)
        with destination.open("xb") as f:
            first = True
            body = None
            compressed = destination.name.endswith((".zstd", ".zst"))
            compressor = zstandard.ZstdCompressor(write_checksum=True)
            try:
                while line := stream.readline(MAX_LINE + 1):
                    check_cancel(cancel)
                    if len(line) > MAX_LINE or not line.endswith(b"\n"):
                        raise ValueError("原始日志存在超长行或不完整尾行，未导入")
                    original_offset += len(line)
                    event = json.loads(line)
                    changed = False
                    if provider == "deepseek" and first:
                        if event.get("type") != "session" or event.get("version") != 4 or event.get("id") != sid:
                            raise ValueError("DeepSeek 原生日志头不兼容")
                        event["cwd"] = cwd; changed = True
                    elif provider == "claude" and "cwd" in event:
                        event["cwd"] = cwd; changed = True
                    elif provider == "codex" and event.get("type") == "session_meta":
                        if event.get("payload", {}).get("id") != sid:
                            raise ValueError("Codex 日志与会话 ID 不一致")
                        event["payload"]["cwd"] = cwd; changed = True
                    elif provider == "codex" and event.get("type") == "turn_context":
                        payload = event.get("payload", {})
                        for key, value in {"cwd": cwd, "approval_policy": "on-request", "sandbox_policy": {"type": "read-only"}}.items():
                            if key in payload:
                                payload[key] = value; changed = True
                    if mapping:
                        remapped = _attachment_refs(event, mapping)
                        changed = changed or remapped != event
                        event = remapped
                    if changed:
                        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                    target_offset += len(line)
                    if provider == "codex":
                        offsets[original_offset] = target_offset
                    if compressed:
                        if first:
                            f.write(compressor.compress(line))
                        else:
                            if body is None:
                                body = compressor.stream_writer(f, closefd=False)
                            body.write(line)
                    else:
                        f.write(line)
                    first = False
                if first:
                    raise ValueError("原始日志为空，未导入")
            finally:
                if body is not None:
                    body.close()
            f.flush(); os.fsync(f.fileno())
    finally:
        reader.close()
        raw.close()
    return offsets


def _decode_blob(value):
    if isinstance(value, dict) and set(value) == {"_sqlite_blob_base64"}:
        import base64
        return base64.b64decode(value["_sqlite_blob_base64"], validate=True)
    return value


def _check_row_shape(c: sqlite3.Connection, table: str, row: dict):
    if not re.fullmatch(r"[a-z_]+", table):
        raise ValueError("不安全的数据库表名")
    info = list(c.execute('PRAGMA table_info("' + table + '")'))
    if not info:
        raise ValueError("目标 Codex 缺少原生表 " + table + "；请使用相容版本")
    columns = {r[1] for r in info}
    if set(row) - columns:
        raise ValueError("目标 Codex 表 " + table + " 缺少源记录字段；未丢弃字段或覆盖目标")
    if any(r[3] and r[4] is None and r[1] not in row for r in info):
        raise ValueError("目标 Codex 原生表存在未支持的必填字段：" + table)
    if any(not re.fullmatch(r"[a-z_]+", name) for name in row):
        raise ValueError("不安全的数据库字段")


def _insert(c: sqlite3.Connection, table: str, row: dict):
    _check_row_shape(c, table, row)
    names = list(row)
    sql = 'INSERT INTO "' + table + '" (' + ','.join('"' + n + '"' for n in names) + ') VALUES (' + ','.join('?' for _ in names) + ')'
    c.execute(sql, [_decode_blob(row[n]) for n in names])


def _codex_stage(package, items, root: Path, cancel):
    state, history = codex_databases(root, expected_root=Path(items[0]["target_root"]))
    connections = {"state": sqlite3.connect(state), "history": sqlite3.connect(history) if history else None}
    ids = {i["record"]["session_id"] for i in items}
    rows = []
    roots_by_project, project_map = {}, {}
    try:
        available_ids = ids | {r[0] for r in connections["state"].execute("SELECT id FROM threads")}
        original_state, original_history = codex_databases(Path(items[0]["target_root"]))
        for key, c in connections.items():
            if c:
                c.execute("PRAGMA foreign_keys=ON")
                original = original_history if key == "history" else original_state
                snapshot = package.snapshots.sqlite(original)
                c.execute("ATTACH DATABASE ? AS preserved", (str(snapshot),))
        for item in items:
            record = SessionRecord.from_dict(item["record"])
            values = native_rows(package, record)
            pid = record.metadata.get("native_thread_metadata", {}).get("project_id")
            if pid:
                project_map[pid] = str(uuid.uuid5(uuid.NAMESPACE_URL, "asm-project:" + item["cwd"]))
                roots_by_project[pid] = item["cwd"]
            rows.extend((item, v) for v in values)
        seen = set()
        order = {"projects": 0, "project_roots": 1, "threads": 2, "thread_turns": 3, "thread_items": 4, "thread_history_projection_state": 5, "thread_realtime_items": 6, "thread_attachments": 7, "thread_spawn_edges": 8}
        for item, entry in sorted(rows, key=lambda pair: order.get(pair[1].get("table"), 99)):
            check_cancel(cancel)
            table, row = entry.get("table"), dict(entry.get("row", {}))
            if table not in STATE_TABLES | HISTORY_TABLES:
                continue  # no executable tools, Goals, migration SQL, queues or identities
            if table == "threads" and row.get("id") != item["record"]["session_id"]:
                raise ValueError("包内 Codex 索引超出所选会话")
            if "thread_id" in row and row["thread_id"] != item["record"]["session_id"]:
                raise ValueError("包内分页历史超出所选会话")
            if table == "thread_spawn_edges" and (row.get("child_thread_id") not in ids or row.get("parent_thread_id") not in available_ids):
                continue
            if table == "threads":
                row["cwd"] = item["cwd"]
                row["rollout_path"] = item.get("rollout_path", "")
                for name, value in {"sandbox_policy": '{"type":"read-only"}', "approval_mode": "on-request", "memory_mode": "disabled", "daybreak_enabled": False, "creator_user_id": None, "creator_account_id": None, "thread_section_id": None}.items():
                    if name in row:
                        row[name] = value
                if row.get("attached_thread_parent_id") not in available_ids:
                    row["attached_thread_parent_id"] = None if "attached_thread_parent_id" in row else row.get("attached_thread_parent_id")
                    if "attached_thread_parent_id" not in entry["row"]:
                        row.pop("attached_thread_parent_id", None)
            if table in {"thread_items", "thread_realtime_items"} and "item_json" in row and item.get("attachment_map"):
                decoded = json.loads(row["item_json"])
                remapped = _attachment_refs(decoded, item["attachment_map"])
                if remapped != decoded:
                    row["item_json"] = json.dumps(remapped, ensure_ascii=False, separators=(",", ":"))
            if table == "thread_turns" and row.get("status") in {"inProgress", "in_progress", "running"}:
                row["status"] = "interrupted"
            for key in ["rollout_byte_offset", "rollout_end_byte_offset", "next_rollout_byte_offset"]:
                if row.get(key) is not None and item.get("offset_map"):
                    if row[key] not in item["offset_map"]:
                        raise ValueError("Codex 分页偏移不对应完整日志边界，需重新导出稳定资料")
                    row[key] = item["offset_map"][row[key]]
            if table == "projects":
                old_id = row["id"]
                if old_id not in project_map:
                    continue
                row["id"] = project_map[old_id]
                # Do not carry a project execution/configuration blob across machines.
                if "metadata" in row:
                    row["metadata"] = "{}"
            if "project_id" in row:
                old_id = row["project_id"]
                row["project_id"] = project_map.get(old_id)
                if table == "project_roots":
                    if old_id not in roots_by_project:
                        continue
                    row["path"] = roots_by_project[old_id]
                    row["position"] = 0
            fingerprint = json.dumps([table, row], sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            c = connections["history" if table in HISTORY_TABLES else "state"]
            if c is None:
                raise ValueError("目标缺少分页历史数据库")
            if table == "projects" and c.execute("SELECT 1 FROM projects WHERE id=?", (row["id"],)).fetchone():
                continue
            if table == "project_roots" and c.execute("SELECT 1 FROM project_roots WHERE project_id=? AND position=?", (row["project_id"], row["position"])).fetchone():
                continue
            _insert(c, table, row)
        for key, c in connections.items():
            if c:
                tables = HISTORY_TABLES if key == "history" else STATE_TABLES
                actual = {r[0] for r in c.execute("SELECT name FROM preserved.sqlite_master WHERE type='table'")}
                for table in tables & actual:
                    if c.execute('SELECT * FROM preserved."' + table + '" EXCEPT SELECT * FROM main."' + table + '" LIMIT 1').fetchone():
                        raise ValueError("暂存修改触及既有 Codex 记录，未发布")
                c.commit()
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Codex 暂存数据库完整性校验失败")
                if c.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Codex 暂存数据库存在未恢复的关联记录")
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                c.execute("DETACH DATABASE preserved")
    finally:
        for c in connections.values():
            if c:
                c.close()


def _stage_sessions(package, items, stages, cancel):
    codex = []
    for item in items:
        record = SessionRecord.from_dict(item["record"])
        root = stages[record.provider]
        sid = safe_segment(record.session_id)
        logs = source_logs(package, record)
        attachment_map = {}
        for asset in entries_for(package, record):
            if "attachment" not in asset.get("role", ""):
                continue
            original_path = asset.get("originalPath", "")
            try:
                rel = relative_original(original_path, record.root)
            except ValueError:
                rel = None
            if record.provider == "deepseek" and rel and rel.parts[:3] == ("attachments", "v1", "objects"):
                destination_asset = root.joinpath(*rel.parts)
            else:
                suffix = PurePosixPath(original_path.replace("\\", "/")).suffix.lower()
                suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else ".bin"
                destination_asset = root / "attachments" / ("asm-" + sid) / (asset["object"].split("/")[-1] + suffix)
                # Tool result paths owned by a Claude session remain at their canonical relative positions.
                if record.provider == "claude" and rel and rel.parts[0] == "projects" and len(rel.parts) >= 4:
                    project = re.sub(r"[^A-Za-z0-9]", "-", item["cwd"])
                    destination_asset = root / "projects" / project
                    destination_asset = destination_asset.joinpath(*rel.parts[2:])
            _copy_new(package._extract(asset["object"], destination_asset.name), destination_asset)
            attachment_map[original_path] = str(Path(item["target_root"]) / destination_asset.relative_to(root))
        item["attachment_map"] = attachment_map
        if record.provider == "claude":
            project = re.sub(r"[^A-Za-z0-9]", "-", item["cwd"])
            preferred = record.metadata.get("primary_file")
            chosen = next(((e, rel) for e, rel in logs if e.get("originalPath") == preferred), logs[0])
            e, original = chosen
            if original.parts[0] != "projects" or len(original.parts) < 3 or original.name != sid + ".jsonl":
                raise ValueError("Claude 原始项目日志位置不兼容")
            destination = (root / "projects" / project).joinpath(*original.parts[2:])
            _rewrite_log(package._extract(e["object"], original.name), destination, "claude", item["cwd"], cancel, sid, attachment_map)
            item["source_session_dir"] = str(original.parent / sid)
            item["session_dir"] = str(destination.parent / sid)
            if "claude_desktop" in stages:
                indexes = [e for e in entries_for(package, record) if e.get("role") == "claude-desktop-index"]
                if not indexes:
                    raise ValueError("包内缺少 Claude Desktop 索引，请用 0.2.0 或更新客户端重新导出，或不选择 Desktop 索引目录")
                # Import one selected profile into the explicitly chosen target account container.
                entry = indexes[0]
                original_name = PurePosixPath(entry["originalPath"].replace("\\", "/")).name
                if not re.fullmatch(r"local_[A-Za-z0-9_-]+\.json", original_name):
                    raise ValueError("Desktop 索引文件名不兼容")
                source_index = json.loads(stable_bytes(package._extract(entry["object"], "index.json")))
                if source_index.get("cliSessionId") != sid:
                    raise ValueError("Desktop 索引与 CLI 会话 ID 不一致")
                allowed = {"sessionId", "cliSessionId", "title", "titleSource", "createdAt", "lastActivityAt", "lastFocusedAt", "isArchived", "model", "effort", "completedTurns", "lastAssistantUuid"}
                data = {k: v for k, v in source_index.items() if k in allowed}
                data.update(cwd=item["cwd"], originCwd=item["cwd"], permissionMode="default", isArchived=record.archived)
                _write_new(stages["claude_desktop"] / original_name, (json.dumps(data, ensure_ascii=False) + "\n").encode())
        elif record.provider == "deepseek":
            destination = root / "sessions" / dsh_project_key(item["cwd"]) / sid
            # Only v4 is transformed; preserved historical generations stay in the package.
            v4 = [(e, rel) for e, rel in logs if re.fullmatch(r"session\.v4\.jsonl(?:\.zstd)?", rel.name)]
            if not v4:
                raise ValueError("DeepSeek 缺少 v4 日志")
            if len(v4) > 1:
                raise ValueError("DeepSeek 同一代际存在多个物理副本，请只保留明确的来源后导入")
            e, original = v4[0]
            _rewrite_log(package._extract(e["object"], original.name), destination / original.name, "deepseek", item["cwd"], cancel, sid, attachment_map)
            item["source_session_dir"] = str(original.parent)
            item["session_dir"] = str(destination)
        else:
            rollout = None
            for e, rel in logs:
                if rel.name.endswith((".jsonl", ".jsonl.zst")):
                    moment = record.updated or time.time()
                    name = "rollout-" + time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(moment)) + "-" + sid + (".jsonl.zst" if rel.name.endswith(".zst") else ".jsonl")
                    rollout = root / ("archived_sessions" if record.archived else "sessions") / name
                    item["offset_map"] = _rewrite_log(package._extract(e["object"], rel.name), rollout, "codex", item["cwd"], cancel, sid, attachment_map)
                    break
            # Use the final native path, never a staging directory pointer.
            final_root = Path(item["target_root"])
            item["rollout_path"] = str(final_root / rollout.relative_to(root)) if rollout else ""
            if record.metadata.get("history_mode") != "paginated" and rollout is None:
                raise ValueError("Codex 旧式历史缺少原始 rollout，不能伪造可续聊记录")
            codex.append(item)
    if codex:
        _codex_stage(package, codex, stages["codex"], cancel)
    if "deepseek" in stages:
        _dsh_registry(package, [i for i in items if i["record"]["provider"] == "deepseek"], stages["deepseek"])


def _dsh_registry(package, items, root):
    path = root / "storages/workspace.json"
    registry = json.loads(stable_bytes(path)) if path.exists() else {"unit": {"name": "workspace", "version": 2}, "global": {"initialized": True, "workspaceIds": [], "archivedSessionIds": [], "pinnedSessionIds": []}, "tables": {"workspaces": {}}}
    if registry.get("unit") != {"name": "workspace", "version": 2}:
        raise ValueError("DeepSeek 目标注册表版本不支持")
    g, projects = registry["global"], registry["tables"]["workspaces"]
    for item in items:
        record = SessionRecord.from_dict(item["record"])
        sid, cwd = record.session_id, item["cwd"]
        wid = next((w for w, p in projects.items() if p.get("path") == cwd), None)
        if wid is None:
            wid = "asm-" + uuid.uuid5(uuid.NAMESPACE_URL, cwd).hex
            if wid in projects:
                raise ValueError("DeepSeek 项目标识冲突，未覆盖")
            projects[wid] = {"path": cwd, "title": record.project, "sessionIds": [], "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        members = projects[wid].setdefault("sessionIds", [])
        if sid in members:
            raise ValueError("DeepSeek 项目已包含该会话，未覆盖")
        members.append(sid)
        if wid not in g.setdefault("workspaceIds", []):
            g["workspaceIds"].append(wid)
        if not g.get("defaultWorkspaceId"):
            g["defaultWorkspaceId"] = wid
        for name, enabled in [("archivedSessionIds", record.archived), ("pinnedSessionIds", record.pinned)]:
            if enabled and sid not in g.setdefault(name, []):
                g[name].append(sid)
    atomic_json(path, registry)


def validate_staged(package, items, stages, cache):
    reader = LocalLibrary(Path.home(), cache / "import-readback")
    checks = []
    for provider, root in stages.items():
        if provider == "claude_desktop":
            continue
        values = reader._scan_codex(root, db_root_override=root) if provider == "codex" else getattr(reader, "_scan_" + provider)(root)
        found = {r.session_id: r for r in values}
        for item in [i for i in items if i["record"]["provider"] == provider]:
            source = SessionRecord.from_dict(item["record"])
            if source.session_id not in found:
                raise ValueError("暂存原生索引中找不到导入会话")
            restored = found[source.session_id]
            original_page = package.page(source, 100)
            restored_page = reader.page(restored, 100)
            if original_page.total != restored_page.total or restored_page.warnings:
                raise ValueError("暂存原生历史回读与包内历史不一致，未发布")
            if restored.cwd != item["cwd"]:
                raise ValueError("暂存项目路径映射核验失败")
            if [(m.kind, m.text) for m in original_page.messages if m.kind == "thinking"] != [(m.kind, m.text) for m in restored_page.messages if m.kind == "thinking"]:
                raise ValueError("暂存 thinking 回读不一致，未发布")
            checks.append({"provider": provider, "messages": restored_page.total, "historyReadback": True})
    return checks


def rename_new(source: Path, target: Path):
    """No-replace directory rename; never replace a concurrently-created target."""
    if sys.platform == "win32":
        os.rename(source, target)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        call = libc.renamex_np
        call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = call(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    else:
        call = libc.renameat2
        call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = call(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def target_locks(targets, cache):
    import hashlib
    acquired = []
    directory = cache / "import-locks"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        for value in sorted(targets.values()):
            path = directory / (hashlib.sha256(str(value).encode()).hexdigest() + ".lock")
            file = path.open("a+b")
            try:
                if sys.platform == "win32":
                    import msvcrt
                    file.seek(0); file.write(b"0"); file.flush(); file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                file.close()
                raise ValueError("已有导入/恢复任务正在操作此目标，请等待完成") from error
            acquired.append(file)
        yield
    finally:
        for file in reversed(acquired):
            file.close()  # OS releases advisory locks even on a process crash.


def validate_backup_location(plan, backup_directory, cache):
    backup_directory = checked_directory(backup_directory)
    for value in plan.targets.values():
        if within(backup_directory, Path(value)) or within(cache, Path(value)):
            raise ValueError("目标备份和本工具缓存必须放在目标资料目录之外")
    return backup_directory


def execute_import(plan: ImportPlan, backup_directory: Path, cache: Path, *, confirmed_offline=False, progress=lambda *_: None, cancel=None, fault=None):
    if not confirmed_offline:
        raise ValueError("请先确认所有目标客户端与后台任务已正常退出")
    if not plan.ready:
        raise ValueError("没有可导入项；同 ID 冲突不会覆盖")
    backup_directory = validate_backup_location(plan, backup_directory, cache)
    if native_processes(plan.targets):
        raise ValueError("检测到原客户端仍在运行，未开始导入")
    cancel = cancel or threading.Event()
    with target_locks(plan.targets, cache):
        if hash_file(Path(plan.archive)) != plan.archive_sha256:
            raise ValueError("迁移包在预览后已变化，请重新预览")
        for provider, value in plan.targets.items():
            if inventory(Path(value), progress, cancel) != plan.before[provider]:
                raise ValueError("目标资料在预览后已变化，请重新预览；尚未写入")
        token = uuid.uuid4().hex
        output = backup_directory / ("导入恢复-" + time.strftime("%Y%m%d-%H%M%S") + "-" + token)
        output.mkdir(parents=True, mode=0o700)
        report_path = output / "导入结果.json"
        report = {"schema": SCHEMA, "token": token, "status": "backing-up", "output": str(output), "journal": str(report_path), "startedAt": time.time(), "roots": [], "imported": len(plan.ready), "skipped": len(plan.items) - len(plan.ready), "modelVerified": False, "realWindowsVerified": False, "thinkingTransform": False, "backupVerified": False, "warnings": list(plan.warnings)}
        atomic_json(output / "冲突预览.json", plan.to_dict())
        atomic_json(report_path, report)
        try:
            estimated = sum(f.get("bytes", 0) for tree in plan.before.values() for f in tree["files"].values())
            if shutil.disk_usage(output).free < estimated + 128 * CHUNK:
                raise ValueError("目标备份位置空间不足，未写入客户端资料")
            archive = output / "目标资料完整备份.zip"
            with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
                for provider, tree in plan.before.items():
                    root = Path(plan.targets[provider])
                    for name, info in tree["files"].items():
                        check_cancel(cancel)
                        progress("备份目标资料 " + provider, 0)
                        z.write(root / name.rstrip("/"), provider + "/" + name)
                z.writestr("asm-target-inventory.json", json.dumps(plan.before, ensure_ascii=False))
            archive.chmod(0o600)
            with archive.open("r+b") as f:
                os.fsync(f.fileno())
            with zipfile.ZipFile(archive) as z:
                import hashlib
                for provider, tree in plan.before.items():
                    for name, info in tree["files"].items():
                        check_cancel(cancel)
                        if info.get("directory"):
                            continue
                        with z.open(provider + "/" + name) as f:
                            if hashlib.file_digest(f, "sha256").hexdigest() != info["sha256"]:
                                raise ValueError("目标备份回读失败，未写入客户端资料")
            report.update(backupVerified=True, backup=str(archive), backupSha256=hash_file(archive), status="staging")
            atomic_json(report_path, report)
            stages = {}
            for provider, value in plan.targets.items():
                check_cancel(cancel)
                root = Path(value)
                stage = root.with_name(".asm-stage-" + token + "-" + root.name)
                old = root.with_name(".asm-before-" + token + "-" + root.name)
                root.parent.mkdir(parents=True, exist_ok=True)
                stage.mkdir(mode=0o700)
                stages[provider] = stage
                entry = {"provider": provider, "target": str(root), "stage": str(stage), "beforePath": str(old), "before": plan.before[provider], "phase": "staging"}
                report["roots"].append(entry); atomic_json(report_path, report)
                if shutil.disk_usage(stage).free < sum(f.get("bytes", 0) for f in plan.before[provider]["files"].values()) + 128 * CHUNK:
                    raise ValueError("目标磁盘缺少独立暂存空间，未写入客户端资料")
                for name, info in plan.before[provider]["files"].items():
                    check_cancel(cancel)
                    p = stage / name.rstrip("/")
                    if info.get("directory"):
                        p.mkdir(parents=True, exist_ok=True)
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        independent_copy(root / name, p)
                    p.chmod(info["mode"])
                if inventory(stage, progress, cancel)["files"] != plan.before[provider]["files"]:
                    raise ValueError("目标暂存副本校验失败，未发布")
            items = json.loads(json.dumps(plan.ready))
            for item in items:
                item["target_root"] = plan.targets[item["record"]["provider"]]
            progress("在独立副本恢复原生会话", 0)
            package = PackageLibrary(Path(plan.archive), cache)
            _stage_sessions(package, items, stages, cancel)
            report["nativeReadback"] = validate_staged(package, items, stages, cache)
            for entry in report["roots"]:
                entry["after"] = inventory(Path(entry["stage"]), progress, cancel)
            if fault:
                fault("staged")
            for provider, value in plan.targets.items():
                if inventory(Path(value), progress, cancel) != plan.before[provider]:
                    raise ValueError("目标客户端资料发生变化，未发布任何副本，请退出后重新预览")
            if native_processes(plan.targets):
                raise ValueError("目标客户端已启动，未发布副本")
            check_cancel(cancel)
            report["status"] = "publishing"; atomic_json(report_path, report)
            # Publication is a short non-cancellable boundary; durable journal precedes each rename.
            for entry in report["roots"]:
                root, stage, old = map(Path, [entry["target"], entry["stage"], entry["beforePath"]])
                entry["phase"] = "moving-original"; atomic_json(report_path, report)
                if entry["before"]["exists"]:
                    rename_new(root, old)
                entry["phase"] = "moving-stage"; atomic_json(report_path, report)
                if fault:
                    fault("before-publish-" + entry["provider"])
                rename_new(stage, root)
                entry["phase"] = "published"; atomic_json(report_path, report)
                if inventory(root) != entry["after"]:
                    raise ValueError("发布后的目标核验失败")
                if entry["before"]["exists"] and inventory(old) != entry["before"]:
                    raise ValueError("原目标在发布期间发生变化，保留两份资料并停止")
            report.update(status="complete", finishedAt=time.time(), targetReadbackVerified=True)
            atomic_json(report_path, report)
            return report
        except BaseException as error:
            report.update(status="failed", error=type(error).__name__ + "：" + str(error))
            atomic_json(report_path, report)
            try:
                report = _rollback(report_path, progress)
                report["error"] = type(error).__name__ + "：" + str(error)
                atomic_json(report_path, report)
            except Exception as recovery_error:
                report.update(status="needs-attention", recoveryError=str(recovery_error))
                atomic_json(report_path, report)
            error.report = report
            raise


def _rollback(report_path: Path, progress=lambda *_: None):
    report = json.loads(stable_bytes(report_path, 256 * CHUNK))
    if report.get("schema") != SCHEMA or not re.fullmatch(r"[a-f0-9]{32}", report.get("token", "")):
        raise ValueError("导入恢复记录不受支持")
    token = report["token"]
    if report["status"] == "rolled-back":
        return report
    roots = report["roots"]
    if native_processes({e["provider"]: e["target"] for e in roots}):
        raise ValueError("请退出目标客户端后再撤销/恢复")
    # Validate every target before changing any directory.
    for e in roots:
        root = checked_directory(Path(e["target"]))
        if Path(e["stage"]) != root.with_name(".asm-stage-" + token + "-" + root.name) or Path(e["beforePath"]) != root.with_name(".asm-before-" + token + "-" + root.name):
            raise ValueError("恢复记录路径不匹配，未更改资料")
        old = Path(e["beforePath"])
        if old.exists() and inventory(old) != e["before"]:
            raise ValueError("原目标副本已变化，保留所有副本并停止自动恢复")
        if root.exists():
            current = inventory(root)
            if current != e.get("after") and current != e["before"]:
                raise ValueError("导入后目标资料已变化，不覆盖后续新对话；请保留备份并人工核对")
        elif e["before"]["exists"] and not old.exists():
            raise ValueError("原目标副本缺失，未作不完整恢复")
    report["status"] = "rolling-back"; atomic_json(report_path, report)
    for e in reversed(roots):
        root, old = Path(e["target"]), Path(e["beforePath"])
        if root.exists() and inventory(root) == e.get("after"):
            parked = root.with_name(".asm-undone-" + token + "-" + root.name)
            rename_new(root, parked)
            e["undonePath"] = str(parked)
        if old.exists() and not root.exists():
            rename_new(old, root)
        if inventory(root) != e["before"]:
            raise ValueError("恢复后资料与原目标不一致，所有副本已保留")
        e["phase"] = "restored"; atomic_json(report_path, report)
        progress("恢复原目标 " + e["provider"], 0)
    report.update(status="rolled-back", rollbackVerified=True, finishedAt=time.time())
    atomic_json(report_path, report)
    return report


def rollback_import(report_path: Path, cache: Path, *, confirmed_offline=False, progress=lambda *_: None):
    if not confirmed_offline:
        raise ValueError("请先确认目标客户端已正常退出")
    report = json.loads(stable_bytes(report_path, 256 * CHUNK))
    targets = {e["provider"]: e["target"] for e in report.get("roots", [])}
    with target_locks(targets, cache):
        return _rollback(report_path, progress)

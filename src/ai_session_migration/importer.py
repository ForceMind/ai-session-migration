"""Offline native import. Stage complete targets; publish only after backup/readback.

Package SQL is never executed. Existing session IDs are never updated. Full previous
target directories remain available for conditional rollback, including after a crash.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
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
                     header, independent_copy, open_lines, remote_path, stable_bytes, within)

SCHEMA = "asm-native-import-1"
from .migration_policy import forbid_claude_directory, forbid_client_storage, require_native_targets
from .attachments import ATTACHMENT_KINDS, ATTACHMENT_PATH_FIELDS, local_file_uri_path
from .portable import PORTABLE_SCHEMA, check_portable
HISTORY_TABLES = {"thread_turns", "thread_items", "thread_history_projection_state", "thread_realtime_items"}
STATE_TABLES = {"threads", "projects", "project_roots", "thread_attachments", "thread_spawn_edges"}
CODEX_CREATOR_FIELDS = ("creator_user_id", "creator_account_id")
CODEX_ROLLOUT_OFFSETS = ("rollout_byte_offset", "rollout_end_byte_offset", "next_rollout_byte_offset")
CLAUDE_ACCOUNT_FIELDS = ("accountUuid", "ownerAccountUuid", "ownerOrganizationUuid", "vetoedAgainstAccountUuid")
CODEX_SESSION_DIRS = {"sessions", "archived_sessions", "attachments"}
CODEX_INDEX = re.compile(r"(?:state|thread_history)_\d+\.sqlite(?:-wal|-shm|-journal)?", re.IGNORECASE)
DSH_V4_LOG = re.compile(r"session\.v4\.jsonl(?:\.zstd)?")
WINDOWS_CLIENT_HOMES = {"codex": ".codex", "claude": ".claude", "deepseek": ".dsh"}


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


def claude_project_origin(cwd, source, mappings):
    """Translate the original project separately from a session's working directory."""
    cwd = Path(cwd)
    original, working = source.get("originCwd"), source.get("cwd")
    if not isinstance(original, str) or not isinstance(working, str) or not original or original == working:
        return cwd
    candidate = mappings.get(original)
    if candidate is None:
        foreign = PureWindowsPath if PureWindowsPath(working).is_absolute() else PurePosixPath
        origin, old_cwd = foreign(original), foreign(working)
        if not origin.is_absolute() or ".." in origin.parts or ".." in old_cwd.parts or not old_cwd.is_relative_to(origin):
            return cwd
        suffix = old_cwd.relative_to(origin).parts
        if not suffix or len(cwd.parts) <= len(suffix):
            return cwd
        if tuple(p.casefold() for p in cwd.parts[-len(suffix):]) != tuple(p.casefold() for p in suffix):
            return cwd
        candidate = cwd.parents[len(suffix) - 1]
    try:
        directory = checked_directory(Path(candidate))
    except (OSError, ValueError):
        return cwd
    return directory if directory.is_dir() else cwd


def inventory(root: Path, progress=lambda *_: None, cancel=None, *, codex_sessions=False) -> dict:
    """Hash the planned files, refusing links, special files, and incomplete scans."""
    if not root.exists():
        return {"exists": False, "files": {}}
    checked_directory(root)
    result, regular = {}, []

    def fail_scan(error):
        raise error

    for base, dirs, files in os.walk(root, followlinks=False, onerror=fail_scan):
        if codex_sessions and Path(base) == root:
            dirs[:] = [name for name in dirs if name.casefold() in CODEX_SESSION_DIRS]
            files = [name for name in files if CODEX_INDEX.fullmatch(name)]
        for name in sorted(dirs + files):
            check_cancel(cancel)
            p = Path(base) / name
            st = p.lstat()
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise ValueError("目标含链接或特殊文件，无法作完整安全备份：" + str(p))
            relative = p.relative_to(root).as_posix()
            if stat.S_ISREG(st.st_mode):
                regular.append((relative, p, st))
            else:
                result[relative + "/"] = {"directory": True, "mode": stat.S_IMODE(st.st_mode)}

    def check_file(relative, path, before):
        check_cancel(cancel)
        digest = hash_file(path)
        after = path.lstat()
        before_signature = before.st_size, before.st_mtime_ns, before.st_ino, before.st_mode
        after_signature = after.st_size, after.st_mtime_ns, after.st_ino, after.st_mode
        if before_signature != after_signature:
            raise ValueError("目标资料仍在变化，请退出客户端后重新预览")
        return relative, {"sha256": digest, "bytes": before.st_size, "mode": stat.S_IMODE(before.st_mode)}

    label = ".codex 会话" if codex_sessions else re.sub(r"^\.asm-stage-[a-f0-9]{32}-", "暂存 ", root.name)
    step = f"检查 {label}（{len(regular)} 个文件）"
    if regular:
        progress(step, 0)
        percent = 0
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="target-check") as pool:
            futures = [pool.submit(check_file, *item) for item in regular]
            try:
                for count, future in enumerate(as_completed(futures), 1):
                    name, info = future.result()
                    result[name] = info
                    updated = 100 * count // len(futures)
                    if updated != percent:
                        percent = updated
                        progress(step, percent)
            finally:
                for future in futures:
                    future.cancel()
    else:
        progress(step, 100)
    return {"exists": True, "files": dict(sorted(result.items()))}


def native_processes(targets: dict[str, str]) -> list[str]:
    """Refuse known live native clients for normal client homes; never terminate them."""
    home = Path.home()
    clients = {
        "claude": (".claude", "CLAUDE_CONFIG_DIR", {"claude.exe", "claude-desktop.exe"}),
        "codex": (".codex", "CODEX_HOME", {"codex.exe", "chatgpt.exe", "codex-code-mode-host.exe"}),
        "deepseek": (".dsh", "DSH_HOME", {"deepseek harness.exe", "dsh.exe", "deepseek.exe"}),
    }
    expected = set()
    for provider, (folder, variable, processes) in clients.items():
        defaults = {home / folder, Path(os.environ.get(variable) or home / folder).expanduser().resolve()}
        if provider in targets and Path(targets[provider]).resolve() in defaults:
            expected.update(processes)
    if "claude_desktop" in targets:
        desktop_root = home / "AppData" / "Local" / "Claude-Data"
        if within(Path(targets["claude_desktop"]), desktop_root):
            expected.update(clients["claude"][2])
    if not expected:
        return []
    try:
        if sys.platform == "win32":
            result = subprocess.run(["tasklist.exe", "/FO", "CSV", "/NH"], capture_output=True, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            # Only the ASCII executable names and numeric PIDs matter in the localized CSV.
            rows = list(csv.reader(io.StringIO(result.stdout.decode("mbcs", errors="replace"))))
            names = {row[0].casefold() for row in rows if len(row) >= 2 and row[1].isdigit()}
            if not names:
                raise ValueError("Windows 未返回可核验的进程表")
            return sorted(names & expected)
        out = subprocess.run(["ps", "-ax", "-o", "comm="], capture_output=True, text=True, check=True, timeout=15).stdout
        active = {Path(line.strip()).name.lower() for line in out.splitlines()}
        return sorted(active & {n.removesuffix(".exe") for n in expected})
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


def legacy_windows_paths():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] != 1
    except OSError:
        return True


def stage_directory(root: Path, transaction: str) -> Path:
    return root.with_name(".asm-stage-" + transaction + "-" + root.name)


def dsh_log_destination(root: Path, cwd: str, sid: str, filename: str) -> Path:
    return root / "sessions" / dsh_project_key(cwd) / sid / filename


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


def codex_rollout(package, record, rows, cancel):
    required = {
        entry["row"][key] for entry in rows if entry.get("table") in HISTORY_TABLES
        for key in CODEX_ROLLOUT_OFFSETS if entry["row"].get(key) is not None
    } - {0}
    candidates = [
        (entry, rel) for entry, rel in source_logs(package, record)
        if rel.name.endswith((".jsonl", ".jsonl.zst", ".jsonl.zstd"))
    ]
    if not candidates:
        if required:
            raise ValueError("Codex 分页索引引用了原始 rollout，但包内没有该日志")
        if record.metadata.get("history_mode") != "paginated":
            raise ValueError("Codex 旧式历史缺少原始 rollout，不能伪造可续聊记录")
        return None
    native_path = next(
        entry["row"].get("rollout_path") for entry in rows if entry.get("table") == "threads"
        and entry["row"].get("id") == record.session_id
    )
    candidates.sort(key=lambda pair: pair[0]["originalPath"] != native_path)
    correct_header = False
    for entry, rel in candidates:
        check_cancel(cancel)
        path = package._extract(entry["object"], rel.name)
        with path.open("rb") as raw:
            reader = zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) if path.suffix in {".zst", ".zstd"} else raw
            with io.BufferedReader(reader) as stream:
                first = stream.readline(MAX_LINE + 1)
                if len(first) > MAX_LINE or not first.endswith(b"\n"):
                    continue
                meta = json.loads(first)
                if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != record.session_id:
                    continue
                correct_header = True
                if not required:
                    return entry["object"]
                offset = len(first)
                pending = required - {offset}
                last_offset = max(required)
                while pending and offset < last_offset:
                    check_cancel(cancel)
                    line = stream.readline(MAX_LINE + 1)
                    if len(line) > MAX_LINE or not line.endswith(b"\n"):
                        break
                    offset += len(line)
                    pending.discard(offset)
                if not pending:
                    return entry["object"]
    if correct_header:
        raise ValueError("Codex 分页偏移不对应完整日志边界，需重新导出稳定资料")
    raise ValueError("Codex 日志首条记录与会话 ID 不一致")


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
    codex_sessions: bool = False

    @property
    def ready(self):
        return [i for i in self.items if i["action"] == "import"]

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


def build_plan(package: PackageLibrary, selected: list[SessionRecord], targets: dict, mappings: dict, cache: Path, progress=lambda *_: None, cancel=None, *, codex_sessions=False) -> ImportPlan:
    if not selected:
        raise ValueError("请先选择要导入的包内会话")
    if package.package_catalog is None:
        raise ValueError("此旧数据包只支持预览。请在会话库选中所需会话，使用「新建备份」生成经过账号检查的迁移包")
    verify_package(package.archive, progress, cancel)
    package = PackageLibrary(package.archive, cache)
    records = {r.uid: r for r in package.scan().records}
    providers = {records[r.uid].provider for r in selected if r.uid in records}
    require_native_targets(providers)
    portable = package.manifest.get("schema") == PORTABLE_SCHEMA
    if portable:
        check_portable(package.archive, check=lambda: check_cancel(cancel))
    if not providers <= targets.keys():
        raise ValueError("请为每种来源选择目标资料目录")
    clean = {p: str(checked_directory(Path(targets[p]))) for p in sorted(providers)}
    for p, v in clean.items():
        root = Path(v)
        forbid_claude_directory(root)
        if within(package.archive, root) or within(cache, root):
            raise ValueError("迁移包和本工具缓存必须在目标资料之外")
        for other, path in clean.items():
            if p != other and (within(root, Path(path)) or within(Path(path), root)):
                raise ValueError("不同来源的目标资料目录不能重叠")
    before, existing, issues = {}, {}, {}
    reader = LocalLibrary(cache / "isolated-home", cache / "import-preview")
    for provider, value in clean.items():
        root = Path(value)
        before[provider] = inventory(root, progress, cancel, codex_sessions=codex_sessions and provider == "codex")
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
    codex_row_shapes = set()
    check_dsh_length = "deepseek" in providers and legacy_windows_paths()
    step = f"预检会话及源日志（{len(selected)} 条）"
    progress(step, 0)
    for count, selected_record in enumerate(selected, 1):
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
            elif record.provider == "claude" and not source_logs(package, record):
                raise ValueError("缺少可原生恢复的原始日志")
            elif record.provider == "deepseek":
                if record.metadata.get("format_version") != 4:
                    raise ValueError("当前仅支持 DeepSeek v4 原生导入，其他代际仍可保全/预览")
                logs = [rel.name for _, rel in source_logs(package, record) if DSH_V4_LOG.fullmatch(rel.name)]
                if not logs:
                    raise ValueError("DeepSeek 缺少 v4 日志")
                if len(logs) > 1:
                    raise ValueError("DeepSeek 同一代际存在多个物理副本，请只保留明确的来源后导入")
                if check_dsh_length:
                    stage = stage_directory(Path(clean["deepseek"]), "0" * 32)
                    log = dsh_log_destination(stage, item["cwd"], record.session_id, logs[0])
                    too_long = any(len(str(path).encode("utf-16-le", errors="surrogatepass")) // 2 > limit
                                   for path, limit in ((log, 259), (log.parent, 248)))
                    if too_long:
                        raise ValueError("Windows 当前的长路径设置无法暂存此 DeepSeek 会话，请改选较短的项目目录或 DSH 目标资料目录后重新预览")
            elif record.provider == "codex":
                if not portable:
                    raise ValueError("旧包需在会话库选择会话，再用新建导出生成经账号检查的迁移包")
                native = native_rows(package, record)
                item["codex_rollout"] = codex_rollout(package, record, native, cancel)
                state, history = codex_databases(Path(clean["codex"]))
                if any(r.get("table") in HISTORY_TABLES for r in native) and history is None:
                    raise ValueError("Codex 目标缺少分页历史数据库，请先用相容版本初始化")
                for row in native:
                    table = row.get("table")
                    if table not in STATE_TABLES | HISTORY_TABLES:
                        continue
                    shape = (table, frozenset(row["row"]))
                    if table != "thread_attachments" and shape in codex_row_shapes:
                        continue
                    c = reader.snapshots.connect(history if table in HISTORY_TABLES else state)
                    try:
                        _check_row_shape(c, table, row["row"])
                        codex_row_shapes.add(shape)
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
        progress(step, 100 * count // len(selected))
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
            if not parent:
                raw_source = record.metadata.get("native_thread_metadata", {}).get("source", "")
                if isinstance(raw_source, str) and raw_source.startswith("{"):
                    source = json.loads(raw_source).get("subagent")
                    if isinstance(source, dict):
                        parent = source.get("thread_spawn", {}).get("parent_thread_id")
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
                      ["Codex 和 DSH 恢复到目标机的本地资料库。", "Claude 历史通过会话库或接手页面只读查看。", "请完成项目映射，并在写入前正常退出参与恢复的客户端。"],
                      codex_sessions)


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
    if str(value.get("type", "")).lower() in ATTACHMENT_KINDS:
        def location(raw):
            uri = local_file_uri_path(raw)
            if uri is not None:
                return Path(mapping[uri]).as_uri() if uri in mapping else raw
            return mapping.get(raw, raw)

        def locations(v):
            if isinstance(v, list):
                return [locations(x) for x in v]
            if isinstance(v, dict):
                return {k: location(x) if k in ATTACHMENT_PATH_FIELDS and isinstance(x, str) else locations(x) for k, x in v.items()}
            return v
        return locations(value)
    for key in ["content", "message", "data", "item", "payload", "attachments", "images"]:
        if key in value:
            result[key] = _attachment_refs(value[key], mapping)
    return result


def strip_claude_account_metadata(event: dict) -> int:
    """Remove Mac account ownership from the transcript envelope and account attachments."""
    removed = 0
    for name in CLAUDE_ACCOUNT_FIELDS:
        if name in event:
            del event[name]
            removed += 1
    attachment = event.get("attachment")
    if isinstance(attachment, dict):
        if "organizationUuid" in attachment:
            del attachment["organizationUuid"]
            removed += 1
        context = attachment.get("context")
        if isinstance(context, dict) and "userEmail" in context:
            del context["userEmail"]
            removed += 1
    return removed


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
                    if provider == "codex" and first:
                        if event.get("type") != "session_meta" or event.get("payload", {}).get("id") != sid:
                            raise ValueError("Codex 日志首条记录与会话 ID 不一致")
                    if provider == "deepseek" and first:
                        if event.get("type") != "session" or event.get("version") != 4 or event.get("id") != sid:
                            raise ValueError("DeepSeek 原生日志头不兼容")
                        event["cwd"] = cwd; changed = True
                    elif provider == "claude":
                        changed = bool(strip_claude_account_metadata(event))
                        if "cwd" in event and event["cwd"] != cwd:
                            event["cwd"] = cwd
                            changed = True
                    elif provider == "codex" and event.get("type") == "session_meta":
                        # A fork has its own first header and can carry earlier headers
                        # from the inherited history. Scrub identities in every header.
                        payload = event["payload"]
                        payload["cwd"] = cwd
                        for key in CODEX_CREATOR_FIELDS:
                            payload.pop(key, None)
                        if payload.get("runtime_workspace_roots"):
                            payload["runtime_workspace_roots"] = [cwd]
                        changed = True
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


def _codex_stage(package, items, root: Path, cancel, progress):
    state, history = codex_databases(root, expected_root=Path(items[0]["target_root"]))
    connections = {"state": sqlite3.connect(state), "history": sqlite3.connect(history) if history else None}
    ids = {i["record"]["session_id"] for i in items}
    rows = []
    roots_by_project, project_map = {}, {}
    try:
        thread_columns = {row[1] for row in connections["state"].execute("PRAGMA table_info(threads)")}
        available_ids = ids | {r[0] for r in connections["state"].execute("SELECT id FROM threads")}
        original_state, original_history = codex_databases(Path(items[0]["target_root"]))
        for key, c in connections.items():
            if c:
                c.execute("PRAGMA foreign_keys=ON")
                original = original_history if key == "history" else original_state
                snapshot = package.snapshots.sqlite(original)
                c.execute("ATTACH DATABASE ? AS preserved", (str(snapshot),))
        step = f"读取 Codex 原生记录（{len(items)} 条会话）"
        progress(step, 0)
        for count, item in enumerate(items, 1):
            record = SessionRecord.from_dict(item["record"])
            values = native_rows(package, record)
            pid = record.metadata.get("native_thread_metadata", {}).get("project_id")
            if pid:
                project_map[pid] = str(uuid.uuid5(uuid.NAMESPACE_URL, "asm-project:" + item["cwd"]))
                roots_by_project[pid] = item["cwd"]
            rows.extend((item, v) for v in values)
            progress(step, 100 * count // len(items))
        seen = set()
        order = {"projects": 0, "project_roots": 1, "threads": 2, "thread_turns": 3, "thread_items": 4, "thread_history_projection_state": 5, "thread_realtime_items": 6, "thread_attachments": 7, "thread_spawn_edges": 8}
        step = f"写入 Codex 原生记录（{len(rows)} 行待核对）"
        percent = -1
        for count, (item, entry) in enumerate(sorted(rows, key=lambda pair: order.get(pair[1].get("table"), 99))):
            check_cancel(cancel)
            updated = 100 * count // len(rows)
            if updated != percent:
                percent = updated
                progress(step, percent)
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
                for name in thread_columns.intersection(CODEX_CREATOR_FIELDS):
                    row[name] = None
                for name, value in {"sandbox_policy": '{"type":"read-only"}', "approval_mode": "on-request", "memory_mode": "disabled", "daybreak_enabled": False, "thread_section_id": None}.items():
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
            for key in CODEX_ROLLOUT_OFFSETS:
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
        progress(step, 100)
        step = "核验 Codex 数据库与既有记录"
        progress(step, 0)
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
        progress(step, 100)
    finally:
        for c in connections.values():
            if c:
                c.close()


def _stage_sessions(package, items, stages, cancel, progress=lambda *_: None, *, mappings):
    require_native_targets(stages)
    require_native_targets(item["record"]["provider"] for item in items)
    codex = []
    step = f"在独立副本恢复原生会话（{len(items)} 条）"
    progress(step, 0)
    for count, item in enumerate(items, 1):
        record = SessionRecord.from_dict(item["record"])
        root = stages[record.provider]
        sid = safe_segment(record.session_id)
        logs = source_logs(package, record)
        claude_cwd = item.get("claude_project_cwd", item["cwd"])
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
                    project = re.sub(r"[^A-Za-z0-9]", "-", claude_cwd)
                    destination_asset = root / "projects" / project
                    destination_asset = destination_asset.joinpath(*rel.parts[2:])
            _copy_new(package._extract(asset["object"], destination_asset.name), destination_asset)
            attachment_map[original_path] = str(Path(item["target_root"]) / destination_asset.relative_to(root))
        item["attachment_map"] = attachment_map
        if record.provider == "claude":
            project = re.sub(r"[^A-Za-z0-9]", "-", claude_cwd)
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
                origin = claude_project_origin(item["cwd"], source_index, mappings)
                data.update(cwd=item["cwd"], originCwd=str(origin), permissionMode="default", isArchived=record.archived)
                _write_new(stages["claude_desktop"] / original_name, (json.dumps(data, ensure_ascii=False) + "\n").encode())
        elif record.provider == "deepseek":
            # Only v4 is transformed; preserved historical generations stay in the package.
            v4 = [(e, rel) for e, rel in logs if DSH_V4_LOG.fullmatch(rel.name)]
            if not v4:
                raise ValueError("DeepSeek 缺少 v4 日志")
            if len(v4) > 1:
                raise ValueError("DeepSeek 同一代际存在多个物理副本，请只保留明确的来源后导入")
            e, original = v4[0]
            log = dsh_log_destination(root, item["cwd"], sid, original.name)
            _rewrite_log(package._extract(e["object"], original.name), log, "deepseek", item["cwd"], cancel, sid, attachment_map)
            item["source_session_dir"] = str(original.parent)
            item["session_dir"] = str(log.parent)
        else:
            rollout = None
            for e, rel in logs:
                if e["object"] == item["codex_rollout"]:
                    moment = record.updated or time.time()
                    name = "rollout-" + time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(moment)) + "-" + sid + (".jsonl.zst" if rel.name.endswith((".zst", ".zstd")) else ".jsonl")
                    rollout = root / ("archived_sessions" if record.archived else "sessions") / name
                    item["offset_map"] = _rewrite_log(package._extract(e["object"], rel.name), rollout, "codex", item["cwd"], cancel, sid, attachment_map)
                    break
            # Use the final native path, never a staging directory pointer.
            final_root = Path(item["target_root"])
            item["rollout_path"] = str(final_root / rollout.relative_to(root)) if rollout else ""
            if record.metadata.get("history_mode") != "paginated" and rollout is None:
                raise ValueError("Codex 旧式历史缺少原始 rollout，不能伪造可续聊记录")
            codex.append(item)
        progress(step, 100 * count // len(items))
    if codex:
        _codex_stage(package, codex, stages["codex"], cancel, progress)
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


def validate_staged(package, items, stages, cache, progress=lambda *_: None, cancel=None):
    reader = LocalLibrary(cache / "isolated-home", cache / "import-readback")
    checks = []
    step = f"回读会话历史（{len(items)} 条）"
    progress(step, 0)
    for provider, root in stages.items():
        if provider == "claude_desktop":
            continue
        values = reader._scan_codex(root, db_root_override=root) if provider == "codex" else getattr(reader, "_scan_" + provider)(root)
        found = {r.session_id: r for r in values}
        for item in [i for i in items if i["record"]["provider"] == provider]:
            check_cancel(cancel)
            source = SessionRecord.from_dict(item["record"])
            if source.session_id not in found:
                raise ValueError("暂存原生索引中找不到导入会话")
            restored = found[source.session_id]
            if provider == "codex":
                index = restored.metadata.get("native_thread_metadata", {})
                if any(index.get(key) is not None for key in CODEX_CREATOR_FIELDS):
                    raise ValueError("Codex 暂存索引仍含源账号归属")
                for file in restored.files:
                    payload = header(Path(file)).get("payload", {})
                    if any(key in payload for key in CODEX_CREATOR_FIELDS):
                        raise ValueError("Codex 暂存日志头仍含源账号归属")
            original_page = package.page(source, 100)
            restored_page = reader.page(restored, 100)
            if original_page.total != restored_page.total or restored_page.warnings:
                raise ValueError("暂存原生历史回读与包内历史不一致，未发布")
            if provider == "claude" and not restored.cwd:
                # The catalog preview only looks at the first few records. A
                # transcript can start with many snapshots without a cwd.
                with closing(open_lines(Path(restored.files[0]))) as events:
                    for event in events:
                        check_cancel(cancel)
                        if event.get("cwd"):
                            restored.cwd = str(event["cwd"])
                            break
            if restored.cwd != item["cwd"]:
                raise ValueError("暂存项目路径映射核验失败：" + source.session_id)
            if [(m.kind, m.text) for m in original_page.messages if m.kind == "thinking"] != [(m.kind, m.text) for m in restored_page.messages if m.kind == "thinking"]:
                raise ValueError("暂存 thinking 回读不一致，未发布")
            checks.append({"provider": provider, "messages": restored_page.total, "historyReadback": True})
            progress(step, 100 * len(checks) // len(items))
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


def windows_artifact_archive(path: Path):
    if sys.platform != "win32" or path.parent != Path.home():
        return None
    if not re.fullmatch(r"\.asm-(?:before|stage|undone)-[a-f0-9]{32}-\.(?:codex|claude|dsh)", path.name):
        return None
    return Path.home() / "AppData" / "Local" / "AI-Session-Migration" / "restore-residue" / path.name


def retained_snapshot(entry):
    original = Path(entry["beforePath"])
    if original.exists():
        return original
    archived = windows_artifact_archive(original)
    if archived is not None and archived.exists():
        return checked_directory(archived)
    return original


def archive_terminal_artifacts(report):
    """Consolidate Windows home-directory artifacts after a transaction is terminal."""
    result = {"moved": {}, "errors": {}}
    token = report.get("token", "")
    safe = {
        "complete": report.get("backupVerified") and report.get("targetReadbackVerified"),
        "rolled-back": report.get("rollbackVerified"),
        "aborted-before-publish": report.get("targetUnmodifiedByImport"),
    }
    if not safe.get(report["status"]) or not re.fullmatch(r"[a-f0-9]{32}", token):
        return result
    for entry in report["roots"]:
        name = WINDOWS_CLIENT_HOMES.get(entry["provider"])
        if name is None or Path(entry["target"]) != Path.home() / name:
            continue
        for label, key in (("before", "beforePath"), ("stage", "stage"), ("undone", "undonePath")):
            source = Path.home() / f".asm-{label}-{token}-{name}"
            if entry.get(key) != str(source):
                continue
            target = windows_artifact_archive(source)
            if target is None or not source.exists():
                continue
            try:
                if checked_directory(source) != source or checked_directory(target) != target:
                    raise ValueError("归档路径解析后发生变化")
                target.parent.mkdir(parents=True, exist_ok=True)
                before = source.lstat()
                if before.st_dev != target.parent.stat().st_dev:
                    raise ValueError("归档目录与原目录必须在同一卷")
                rename_new(source, target)
                after = target.lstat()
                if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("归档目录文件标识回读失败")
                result["moved"][str(source)] = str(target)
            except (OSError, ValueError) as error:
                result["errors"][str(source)] = str(error)
    return result


def record_archived_artifacts(report_path, report):
    result = archive_terminal_artifacts(report)
    if result["moved"] or result["errors"]:
        report["artifactArchive"] = result
        try:
            atomic_json(report_path, report)
        except OSError as error:
            report.setdefault("warnings", []).append("归档结果写回失败：" + str(error))


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
    forbid_client_storage(backup_directory)
    return backup_directory


def preview_import_space(plan, package, backup_directory):
    """Group concurrent staging and backup allocations by their actual volumes."""
    if not plan.ready:
        return []
    volumes = {}

    def add(location, staging=0, backup=0, incoming=0):
        directory = next((p for p in (location, *location.parents) if p.is_dir()), None)
        if directory is None:
            raise ValueError("备份位置或目标所在磁盘不可用，请重新连接后预览")
        device = directory.stat().st_dev
        available = shutil.disk_usage(directory).free
        group = volumes.setdefault(device, {"locations": [], "stagingBytes": 0, "backupBytes": 0,
                                            "incomingBytes": 0, "availableBytes": available})
        group["locations"].append(str(location))
        group["stagingBytes"] += staging
        group["backupBytes"] += backup
        group["incomingBytes"] += incoming
        group["availableBytes"] = min(group["availableBytes"], available)

    selected = {}
    for item in plan.ready:
        record = item["record"]
        selected.setdefault(record["provider"], set()).add(record["uid"])
    payload = {provider: 0 for provider in selected}
    for entry in package.entries.values():
        role = entry.get("role", "")
        if role not in {"original-session", "native-database-rows"} and "attachment" not in role:
            continue
        owners = {entry.get("sessionUid"), *entry.get("sessionUids", ())}
        for provider, uids in selected.items():
            if owners & uids:
                payload[provider] += entry["size"]

    existing = {provider: sum(item.get("bytes", 0) for item in plan.before[provider]["files"].values())
                for provider in plan.targets}
    for provider, root in plan.targets.items():
        add(Path(root).parent, staging=existing[provider], incoming=payload.get(provider, 0))
    add(Path(backup_directory), backup=sum(existing.values()))
    for volume in volumes.values():
        volume["locations"] = list(dict.fromkeys(volume["locations"]))
        volume["minimumBytes"] = volume["stagingBytes"] + volume["backupBytes"] + 128 * CHUNK
        volume["estimatedBytes"] = volume["minimumBytes"] + volume["incomingBytes"]
        volume["enough"] = volume["availableBytes"] >= volume["minimumBytes"]
    return list(volumes.values())


def session_only_changes(provider, before, after):
    prefixes = {
        "claude": ("projects/", "attachments/"),
        "claude_desktop": (),
        "codex": ("sessions/", "archived_sessions/", "attachments/"),
        "deepseek": ("sessions/", "attachments/"),
    }[provider]
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    for name in changed:
        allowed = name.startswith(prefixes)
        if provider == "codex":
            allowed |= bool(CODEX_INDEX.fullmatch(name))
        if provider == "deepseek":
            allowed |= name in {"storages/", "storages/workspace.json"}
        if provider == "claude_desktop":
            allowed |= bool(re.fullmatch(r"local_[A-Za-z0-9_-]+\.json", name))
        if not allowed:
            raise ValueError(f"暂存变更超出会话资料范围：{provider}/{name}")


def by_top_level(tree):
    units = {}
    for name, info in tree["files"].items():
        units.setdefault(name.split("/", 1)[0], {})[name] = info
    return units


def codex_changes(entry):
    before = by_top_level(entry["before"])
    after = by_top_level(entry.get("after", entry["before"]))
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    return before, after, changed


def publish_codex_sessions(entry, report, report_path, fault, progress=lambda *_: None):
    root, stage, old = map(Path, (entry["target"], entry["stage"], entry["beforePath"]))
    before, after, changed = codex_changes(entry)
    old.mkdir(mode=0o700)
    for name in changed:
        entry.update(phase="moving-original", activeUnit=name)
        atomic_json(report_path, report)
        if name in before:
            rename_new(root / name, old / name)
        entry["phase"] = "moving-stage"
        atomic_json(report_path, report)
        if fault:
            fault("before-publish-codex")
        if name in after:
            rename_new(stage / name, root / name)
    entry.pop("activeUnit", None)
    entry["phase"] = "published"
    atomic_json(report_path, report)
    if inventory(root, progress, codex_sessions=True) != entry["after"]:
        raise ValueError("Codex 发布后的会话资料校验失败")
    retained = {name: before[name] for name in changed if name in before}
    if by_top_level(inventory(old, progress, codex_sessions=True)) != retained:
        raise ValueError("Codex 原会话资料在发布时发生变化")


def inspect_codex_rollback(entry):
    root, old = Path(entry["target"]), retained_snapshot(entry)
    if not root.is_dir():
        raise ValueError("Codex 目标目录已改变，无法自动回滚会话")
    before, after, changed = codex_changes(entry)
    current = by_top_level(inventory(root, codex_sessions=True))
    retained = by_top_level(inventory(old, codex_sessions=True))
    if not set(retained) <= set(changed):
        raise ValueError("Codex 回滚副本包含本批未替换的文件")
    for name in before.keys() | current.keys() | after.keys():
        b, a = before.get(name), after.get(name)
        live, saved = current.get(name), retained.get(name)
        allowed = ((b, None), (None, b), (a, b)) if name in changed else ((b, None),)
        if (live, saved) not in allowed:
            raise ValueError("Codex 会话资料已变化，保留所有副本并停止自动回滚")
    return before, current, retained, changed


def rollback_codex_sessions(entry, token):
    root, old = Path(entry["target"]), retained_snapshot(entry)
    before, current, retained, changed = inspect_codex_rollback(entry)
    parked = root.with_name(".asm-undone-" + token + "-" + root.name)
    for name in reversed(changed):
        if name in current and current[name] != before.get(name):
            parked.mkdir(exist_ok=True, mode=0o700)
            checked_directory(parked)
            rename_new(root / name, parked / name)
            entry["undonePath"] = str(parked)
        if name in retained:
            rename_new(old / name, root / name)
    if inventory(root, codex_sessions=True) != entry["before"]:
        raise ValueError("Codex 回滚后的会话资料与原目标不一致")


def execute_import(plan: ImportPlan, backup_directory: Path, cache: Path, *, confirmed_offline=False, progress=lambda *_: None, cancel=None, fault=None):
    require_native_targets(plan.targets)
    require_native_targets(item["record"]["provider"] for item in plan.ready)
    for value in plan.targets.values():
        forbid_claude_directory(value)
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
        source = PackageLibrary(Path(plan.archive), cache)
        actual = {r.uid: r for r in source.scan().records}
        for item in plan.ready:
            record = item["record"]
            parsed = actual.get(record["uid"])
            if parsed is None or parsed.provider != record["provider"] or parsed.session_id != record["session_id"]:
                raise ValueError("已保存计划和迁移包内会话不一致，请重新预览")
        if any(item["record"]["provider"] == "codex" for item in plan.ready):
            check_portable(source.archive, check=lambda: check_cancel(cancel))
        for provider, value in plan.targets.items():
            if inventory(Path(value), progress, cancel, codex_sessions=plan.codex_sessions and provider == "codex") != plan.before[provider]:
                raise ValueError("目标资料在预览后已变化，请重新预览；尚未写入")
        volumes = preview_import_space(plan, source, backup_directory)
        if any(not volume["enough"] for volume in volumes):
            raise ValueError("目标暂存或完整备份空间不足，请更改位置后重新预览")
        token = uuid.uuid4().hex
        output = backup_directory / ("导入恢复-" + time.strftime("%Y%m%d-%H%M%S") + "-" + token)
        output.mkdir(parents=True, mode=0o700)
        report_path = output / "导入结果.json"
        report = {"schema": SCHEMA, "token": token, "status": "staging", "output": str(output), "journal": str(report_path), "startedAt": time.time(), "roots": [], "imported": len(plan.ready), "skipped": len(plan.items) - len(plan.ready), "modelVerified": False, "realWindowsVerified": False, "thinkingTransform": False, "backupVerified": False, "warnings": list(plan.warnings), "spaceBeforeStaging": volumes}
        atomic_json(output / "冲突预览.json", plan.to_dict())
        atomic_json(report_path, report)
        publication_started = False
        try:
            stages = {}
            for provider, value in plan.targets.items():
                check_cancel(cancel)
                root = Path(value)
                stage = stage_directory(root, token)
                old = root.with_name(".asm-before-" + token + "-" + root.name)
                root.parent.mkdir(parents=True, exist_ok=True)
                stage.mkdir(mode=0o700)
                stages[provider] = stage
                entry = {"provider": provider, "target": str(root), "stage": str(stage), "beforePath": str(old), "before": plan.before[provider], "phase": "staging"}
                if provider == "codex" and plan.codex_sessions:
                    entry["scope"] = "codex-sessions"
                report["roots"].append(entry); atomic_json(report_path, report)
                if shutil.disk_usage(stage).free < sum(f.get("bytes", 0) for f in plan.before[provider]["files"].values()) + 128 * CHUNK:
                    raise ValueError("目标磁盘缺少独立暂存空间，未写入客户端资料")
                files = plan.before[provider]["files"]
                step = f"复制 {provider} 目标资料（{len(files)} 项）"
                progress(step, 0 if files else 100)
                for count, (name, info) in enumerate(files.items(), 1):
                    check_cancel(cancel)
                    p = stage / name.rstrip("/")
                    if info.get("directory"):
                        p.mkdir(parents=True, exist_ok=True)
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        independent_copy(root / name, p)
                    p.chmod(info["mode"])
                    progress(step, 100 * count // len(files))
                if inventory(stage, progress, cancel)["files"] != plan.before[provider]["files"]:
                    raise ValueError("目标暂存副本校验失败，未发布")
            items = json.loads(json.dumps(plan.ready))
            for item in items:
                item["target_root"] = plan.targets[item["record"]["provider"]]
            package = PackageLibrary(Path(plan.archive), cache)
            _stage_sessions(package, items, stages, cancel, progress, mappings=plan.mappings)
            report["nativeReadback"] = validate_staged(package, items, stages, cache, progress, cancel)
            for entry in report["roots"]:
                entry["after"] = inventory(Path(entry["stage"]), progress, cancel)
                session_only_changes(entry["provider"], entry["before"]["files"], entry["after"]["files"])
            if fault:
                fault("staged")
            if native_processes(plan.targets):
                raise ValueError("目标客户端已启动，未发布副本")

            # A bad source batch only costs staging. Verify the original backup
            # before the first write to the native target.
            report["status"] = "backing-up"
            atomic_json(report_path, report)
            estimated = sum(f.get("bytes", 0) for tree in plan.before.values() for f in tree["files"].values())
            if shutil.disk_usage(output).free < estimated + 128 * CHUNK:
                raise ValueError("目标备份位置空间不足，未写入客户端资料")
            archive = output / ("目标会话资料备份.zip" if plan.codex_sessions and "codex" in plan.targets else "目标资料完整备份.zip")
            with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
                for provider, tree in plan.before.items():
                    root = Path(plan.targets[provider])
                    total = len(tree["files"])
                    step = f"备份 {provider} 目标资料（{total} 项）"
                    progress(step, 0 if total else 100)
                    for count, name in enumerate(tree["files"], 1):
                        check_cancel(cancel)
                        z.write(root / name.rstrip("/"), provider + "/" + name)
                        progress(step, 100 * count // total)
                z.writestr("asm-target-inventory.json", json.dumps(plan.before, ensure_ascii=False))
            archive.chmod(0o600)
            with archive.open("r+b") as f:
                os.fsync(f.fileno())
            with zipfile.ZipFile(archive) as z:
                import hashlib
                for provider, tree in plan.before.items():
                    files = [(name, info) for name, info in tree["files"].items() if not info.get("directory")]
                    step = f"回读 {provider} 目标备份（{len(files)} 个文件）"
                    progress(step, 0 if files else 100)
                    for count, (name, info) in enumerate(files, 1):
                        check_cancel(cancel)
                        with z.open(provider + "/" + name) as f:
                            if hashlib.file_digest(f, "sha256").hexdigest() != info["sha256"]:
                                raise ValueError("目标备份回读失败，未写入客户端资料")
                        progress(step, 100 * count // len(files))
            report.update(backupVerified=True, backup=str(archive), backupSha256=hash_file(archive))
            for provider, value in plan.targets.items():
                if inventory(Path(value), progress, cancel, codex_sessions=plan.codex_sessions and provider == "codex") != plan.before[provider]:
                    raise ValueError("目标客户端资料发生变化，未发布任何副本，请退出后重新预览")
            if native_processes(plan.targets):
                raise ValueError("目标客户端已启动，未发布副本")
            check_cancel(cancel)
            report["status"] = "publishing"; atomic_json(report_path, report)
            publication_started = True
            # Publication is a short non-cancellable boundary; durable journal precedes each rename.
            for entry in report["roots"]:
                if entry.get("scope") == "codex-sessions":
                    publish_codex_sessions(entry, report, report_path, fault, progress)
                    continue
                root, stage, old = map(Path, [entry["target"], entry["stage"], entry["beforePath"]])
                entry["phase"] = "moving-original"; atomic_json(report_path, report)
                if entry["before"]["exists"]:
                    rename_new(root, old)
                entry["phase"] = "moving-stage"; atomic_json(report_path, report)
                if fault:
                    fault("before-publish-" + entry["provider"])
                rename_new(stage, root)
                entry["phase"] = "published"; atomic_json(report_path, report)
                if inventory(root, progress) != entry["after"]:
                    raise ValueError("发布后的目标核验失败")
                if entry["before"]["exists"] and inventory(old, progress) != entry["before"]:
                    raise ValueError("原目标在发布期间发生变化，保留两份资料并停止")
            report.update(status="complete", finishedAt=time.time(), targetReadbackVerified=True)
            atomic_json(report_path, report)
            record_archived_artifacts(report_path, report)
            return report
        except BaseException as error:
            reason = type(error).__name__ + "：" + str(error)
            if not publication_started:
                report.update(status="aborted-before-publish", error=reason,
                              targetUnmodifiedByImport=True, finishedAt=time.time())
                atomic_json(report_path, report)
                record_archived_artifacts(report_path, report)
            else:
                report.update(status="failed", error=reason)
                atomic_json(report_path, report)
                try:
                    report = _rollback(report_path, progress)
                    report["error"] = reason
                    atomic_json(report_path, report)
                except Exception as recovery_error:
                    report.update(status="needs-attention", recoveryError=str(recovery_error))
                    atomic_json(report_path, report)
            error.report = report
            raise


def _rollback(report_path: Path, progress=lambda *_: None):
    forbid_client_storage(report_path.parent)
    report = json.loads(stable_bytes(report_path, 256 * CHUNK))
    if report.get("schema") != SCHEMA or not re.fullmatch(r"[a-f0-9]{32}", report.get("token", "")):
        raise ValueError("导入恢复记录不受支持")
    token = report["token"]
    if report["status"] in {"rolled-back", "aborted-before-publish"}:
        return report
    roots = report["roots"]
    require_native_targets(e["provider"] for e in roots)
    for entry in roots:
        forbid_claude_directory(entry["target"])
    if native_processes({e["provider"]: e["target"] for e in roots}):
        raise ValueError("请退出目标客户端后再撤销/恢复")
    # Validate every target before changing any directory.
    for e in roots:
        root = checked_directory(Path(e["target"]))
        if Path(e["stage"]) != stage_directory(root, token) or Path(e["beforePath"]) != root.with_name(".asm-before-" + token + "-" + root.name):
            raise ValueError("恢复记录路径不匹配，未更改资料")
        if e.get("scope") == "codex-sessions":
            if e["provider"] != "codex":
                raise ValueError("恢复记录的 Codex 会话范围不匹配")
            inspect_codex_rollback(e)
            continue
        old = retained_snapshot(e)
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
        if e.get("scope") == "codex-sessions":
            rollback_codex_sessions(e, token)
            e["phase"] = "restored"; atomic_json(report_path, report)
            progress("恢复原 Codex 会话资料", 100)
            continue
        root, old = Path(e["target"]), retained_snapshot(e)
        if root.exists() and inventory(root) == e.get("after"):
            parked = root.with_name(".asm-undone-" + token + "-" + root.name)
            rename_new(root, parked)
            e["undonePath"] = str(parked)
        if old.exists() and not root.exists():
            rename_new(old, root)
        if inventory(root) != e["before"]:
            raise ValueError("恢复后资料与原目标不一致，所有副本已保留")
        e["phase"] = "restored"; atomic_json(report_path, report)
        progress("恢复原目标 " + e["provider"], 100)
    report.update(status="rolled-back", rollbackVerified=True, finishedAt=time.time())
    atomic_json(report_path, report)
    record_archived_artifacts(report_path, report)
    return report


def rollback_import(report_path: Path, cache: Path, *, confirmed_offline=False, progress=lambda *_: None):
    if not confirmed_offline:
        raise ValueError("请先确认目标客户端已正常退出")
    report = json.loads(stable_bytes(report_path, 256 * CHUNK))
    targets = {e["provider"]: e["target"] for e in report.get("roots", [])}
    require_native_targets(targets)
    with target_locks(targets, cache):
        return _rollback(report_path, progress)

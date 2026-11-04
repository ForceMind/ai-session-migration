from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import uuid

import zstandard

CHUNK = 1024 * 1024
MAX_LINE = 32 * CHUNK
_snapshot_lock = threading.RLock()


class ReadFailure(RuntimeError):
    pass


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def remote_path(value) -> bool:
    value = str(value)
    return value.startswith(("\\\\", "//")) or "://" in value


def owned_file(path: Path, root: Path) -> bool:
    """Validate a historical file reference before doing any filesystem lookup."""
    if remote_path(path) or not path.is_absolute():
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if ".." in relative.parts:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            return False
    return path.is_file()


def directory_issue(cwd, check=True):
    if not cwd or not check:
        return ""
    if remote_path(cwd):
        return "远程项目目录未自动访问"
    path = Path(cwd)
    if not path.is_absolute():
        return "项目目录信息需确认"
    try:
        return "" if path.is_dir() else "原项目目录缺失"
    except OSError:
        return "项目目录无法访问"


def file_signature(path: Path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def stable_bytes(path: Path, limit: int = 16 * CHUNK) -> bytes:
    for _ in range(4):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            if path.is_symlink() or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ReadFailure("文件类型或大小不适合直接预览")
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(limit + 1)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino) and len(data) == before.st_size:
                return data
        finally:
            os.close(fd)
    raise ReadFailure("资料仍在变化，请稍后重试")


def read_json(path: Path, default=None):
    try:
        return json.loads(stable_bytes(path))
    except (OSError, ValueError, ReadFailure):
        if default is not None:
            return default
        raise


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def independent_copy(source: Path, target: Path):
    """APFS copy-on-write clone when possible; never hard-link source data."""
    if source.is_symlink():
        raise ReadFailure("不跟随资料中的符号链接")
    if sys.platform == "darwin":
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        clone = libc.clonefile
        clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clone.restype = ctypes.c_int
        if clone(os.fsencode(source), os.fsencode(target), 0) == 0:
            return
    if shutil.disk_usage(target.parent).free < source.stat().st_size + 128 * CHUNK:
        raise ReadFailure("本工具的缓存空间不足，请在设置中选择其他缓存位置")
    shutil.copyfile(source, target, follow_symlinks=False)


class SnapshotStore:
    """All database opens occur under this tool's private cache, never at source."""
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def sqlite(self, source: Path) -> Path:
        source = source.resolve()
        def state():
            return {suffix: file_signature(Path(str(source) + suffix)) for suffix in ("", "-wal", "-journal") if Path(str(source) + suffix).exists()}
        with _snapshot_lock:
            for _ in range(5):
                before = state()
                if "" not in before:
                    raise ReadFailure("历史数据库不存在")
                key = hashlib.sha256((str(source) + repr(before)).encode()).hexdigest()
                final = self.directory / key
                db = final / "data.sqlite"
                if db.exists():
                    return db
                temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=self.directory))
                try:
                    for suffix in before:
                        independent_copy(Path(str(source) + suffix), temporary / ("data.sqlite" + suffix))
                    if before != state():
                        continue
                    # Recovery and SHM creation are confined to our independent copy.
                    c = sqlite3.connect(temporary / "data.sqlite", timeout=8)
                    try:
                        c.execute("SELECT count(*) FROM sqlite_master").fetchone()
                    finally:
                        c.close()
                    os.rename(temporary, final)
                    self._prune(keep={final})
                    return db
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            raise ReadFailure("数据库持续变化，尚未取得稳定副本；请稍后重试")

    def _prune(self, keep: set[Path]):
        # Only tool-owned hash directories, and only old snapshots not in use.
        dirs = [p for p in self.directory.iterdir() if p.is_dir() and len(p.name) == 64]
        for p in sorted(dirs, key=lambda x: x.stat().st_mtime, reverse=True)[12:]:
            if p not in keep and time.time() - p.stat().st_mtime > 3600:
                try:
                    shutil.rmtree(p)
                except OSError:
                    pass

    def connect(self, source: Path) -> sqlite3.Connection:
        cached = self.sqlite(source)
        c = sqlite3.connect(cached.as_uri() + "?mode=ro", uri=True, timeout=8)
        c.execute("PRAGMA query_only=ON")
        c.row_factory = sqlite3.Row
        return c


def open_lines(path: Path):
    """Read all concatenated Zstandard frames, with a bounded physical-line buffer."""
    raw = path.open("rb")
    if path.name.endswith((".zstd", ".zst")):
        reader = zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True)
    else:
        reader = raw
    try:
        buffered = io.BufferedReader(reader)
        while True:
            line = buffered.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                while line and not line.endswith(b"\n"):
                    line = buffered.readline(MAX_LINE + 1)
                yield {"_viewer_warning": "单条原始记录过大，预览已略过；备份保留原文件"}
                continue
            try:
                yield json.loads(line)
            except (ValueError, UnicodeError):
                yield {"_viewer_warning": "存在无法解析或尚未写完的记录；原文件未改变"}
    except zstandard.ZstdError:
        yield {"_viewer_warning": "压缩日志存在未完成或损坏的帧；只展示已读出的记录，原文件未改变"}
    finally:
        reader.close()
        raw.close()


def header(path: Path):
    iterator = open_lines(path)
    try:
        return next(iterator, {})
    finally:
        iterator.close()


def walk_files(root: Path):
    if not root.is_dir() or root.is_symlink():
        return
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [x for x in dirs if not (Path(base) / x).is_symlink()]
        for name in sorted(files):
            p = Path(base) / name
            if p.is_file() and not p.is_symlink() and p.name not in {"session.lock", "LOCK"}:
                yield p

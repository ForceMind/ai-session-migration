from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import zipfile

from .adapters import LocalLibrary, claude_messages, codex_messages, dsh_messages
from .backup import SCHEMA
from .models import Attachment, Catalog, SessionPage, SessionRecord, identity
from .safeio import CHUNK, ReadFailure, SnapshotStore, open_lines, read_json, independent_copy


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
            if self.manifest.get("schema") == SCHEMA:
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
        self._legacy_original = {}

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
        if target.exists() and target.stat().st_size == entry["size"]:
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
        for root in self.manifest.get("roots", []):
            kind, label = root["kind"], root["label"]
            if kind == "codex":
                state = [e for (r, p), e in self.by_root_path.items() if r == label and re.fullmatch(r"state_\d+\.sqlite", p)]
                if not state:
                    continue
                progress("读取旧迁移包的会话目录", 0)
                cached_root = self.cache / label
                cached_root.mkdir(exist_ok=True)
                state_file = self._extract(state[-1]["object"], state[-1]["path"])
                canonical = cached_root / Path(state[-1]["path"]).name
                if not canonical.exists():
                    independent_copy(state_file, canonical)
                adapter = LocalLibrary(Path.home(), self.cache / "db-cache", check_project_paths=False)
                subset = adapter._scan_codex(cached_root)
                for record in subset:
                    record.uid = identity("codex", str(self.archive) + label, record.session_id)
                    record.metadata.update(package=str(self.archive), package_root_label=label, preview_only=True)
                    self.legacy_adapters[record.uid] = adapter
                    self._legacy_original[record.uid] = record
                records.extend(subset)
                sources.append({"provider": "codex", "path": str(self.archive), "status": "旧迁移包预览", "count": len([r for r in subset if not r.child])})
            elif kind == "claude-code":
                try:
                    with zipfile.ZipFile(self.archive) as z:
                        index = json.loads(z.read("session-catalog.json")).get("claudeDesktop", [])
                except KeyError:
                    index = []
                by_id = {}
                for data in index:
                    sid = data.get("cliSessionId")
                    if not sid:
                        continue
                    matched = [e for (r, p), e in self.by_root_path.items() if r == label and p.endswith("/" + sid + ".jsonl")]
                    if not matched:
                        continue
                    from .adapters import timestamp
                    record = SessionRecord(identity("claude", str(self.archive) + label, sid), "claude", sid, data.get("title") or "未命名会话 · " + sid[:12], str(self.archive), data.get("cwd", ""), timestamp(data.get("lastActivityAt")), bool(data.get("isArchived")), files=[], metadata={"package": str(self.archive), "preview_only": True, "legacy_objects": [e["object"] for e in matched]})
                    if sid not in by_id or record.updated > by_id[sid].updated:
                        by_id[sid] = record
                records.extend(by_id.values())
                sources.append({"provider": "claude", "path": str(self.archive), "status": "旧迁移包预览", "count": len(by_id)})
        warnings.append("旧数据包按索引预览；按需解压所选历史到本工具缓存，不写入任何原客户端。")
        self.catalog = Catalog(sorted(records, key=lambda r: r.updated, reverse=True), sources, warnings)
        return self.catalog

    def raw_events(self, record):
        if self.package_catalog is not None:
            name = self.package_catalog["histories"][record.uid]
            for event in open_lines(self._extract(name, "history.jsonl")):
                yield event
        elif record.provider == "claude":
            names = record.metadata.get("legacy_objects", [])
            if names:
                yield from open_lines(self._extract(names[0], "history.jsonl"))
        elif record.provider == "codex":
            label = record.metadata["package_root_label"]
            found = [e for (r, p), e in self.by_root_path.items() if r == label and re.fullmatch(r"thread_history_\d+\.sqlite", p)]
            if found:
                database = self._extract(found[-1]["object"], found[-1]["path"])
                root = Path(record.root)
                canonical = root / Path(found[-1]["path"]).name
                if not canonical.exists():
                    independent_copy(database, canonical)
                yield from self.legacy_adapters[record.uid].raw_events(record)

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
                    yield self._extract(name, Path(entry.get("originalPath", "payload")).name), entry.get("role", "original-session")
        elif record.provider == "claude":
            for name in record.metadata.get("legacy_objects", []):
                yield self._extract(name, "history.jsonl"), "original-session"

    def selected_registry(self, selected):
        return self.package_catalog.get("registries", []) if self.package_catalog else []

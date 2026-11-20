from __future__ import annotations

import base64
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import hashlib
import hmac
from itertools import chain
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import struct
import threading
import tempfile
import time
import uuid
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from .models import SessionRecord
from .migration_policy import forbid_client_storage
from .portable import PORTABLE_SCHEMA, PortablePrivacyError, build_portable
from .work_projects import configured_projects, discover_projects, project_files
from .safeio import CHUNK, atomic_json, hash_file, stable_bytes, within, independent_copy, file_signature, owned_file, remote_path

SCHEMA = "ai-session-migration-gui-1"
MAGIC = b"ASMB1\x00"


class Cancelled(RuntimeError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("任务已取消；保留已有结果，原始资料未改变")


def save_recovery_key(path: Path) -> dict:
    key = secrets.token_bytes(32)
    data = {"schema": "asm-recovery-key-1", "keyId": hashlib.sha256(key).hexdigest()[:24], "key": base64.b64encode(key).decode()}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    if json.loads(path.read_text()) != data:
        raise ValueError("恢复密钥保存后的回读不一致")
    return {"keyId": data["keyId"], "path": str(path)}


def read_key(path: Path):
    data = json.loads(stable_bytes(path, 16384))
    if data.get("schema") != "asm-recovery-key-1":
        raise ValueError("恢复密钥格式不受支持")
    key = base64.b64decode(data["key"], validate=True)
    if len(key) != 32 or hashlib.sha256(key).hexdigest()[:24] != data.get("keyId"):
        raise ValueError("恢复密钥校验失败")
    return key, data["keyId"]


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=lambda v: {"_sqlite_blob_base64": base64.b64encode(v).decode()} if isinstance(v, bytes) else str(v))


def _stable_file(source: Path, target: Path, cancel, snapshot_dir=None):
    if snapshot_dir is not None:
        snapshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".payload-", dir=snapshot_dir) as temporary:
            snapshot = Path(temporary) / "data"
            for _ in range(4):
                check_cancel(cancel)
                before = file_signature(source)
                independent_copy(source, snapshot)
                if file_signature(source) == before:
                    result = _stable_file(snapshot, target, cancel)
                    result["mtimeNs"] = before[3]
                    result["capture"] = "independent-stable-file-snapshot"
                    return result
                snapshot.unlink(missing_ok=True)
        raise ValueError("文件持续变化，无法取得稳定副本：" + source.name)
    for _ in range(4):
        check_cancel(cancel)
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            if source.is_symlink():
                raise ValueError("不跟随原始资料中的符号链接")
            checksum = hashlib.sha256()
            remaining = before.st_size
            with os.fdopen(fd, "rb", closefd=False) as src, target.open("wb") as dst:
                while remaining:
                    check_cancel(cancel)
                    chunk = src.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    checksum.update(chunk)
                    dst.write(chunk)
            after = os.fstat(fd)
            if not remaining and (before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino):
                return {"sha256": checksum.hexdigest(), "size": before.st_size, "mtimeNs": before.st_mtime_ns}
        finally:
            os.close(fd)
    raise ValueError("文件持续变化，无法取得稳定副本：" + source.name)


def verify_package(archive: Path, progress=lambda *_: None, cancel=None):
    cancel = cancel or threading.Event()
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise ValueError("数据包含重复文件名")
        for name in names:
            p = PurePosixPath(name)
            if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
                raise ValueError("数据包路径不安全")
        if z.getinfo("manifest.json").file_size > 512 * CHUNK:
            raise ValueError("数据包清单过大")
        manifest = json.loads(z.read("manifest.json"))
        schema = manifest.get("schema")
        if schema in {SCHEMA, PORTABLE_SCHEMA}:
            entries = manifest.get("entries", [])
            expected = {"manifest.json", "catalog.json"}
            catalog_checksum = hashlib.sha256(z.read("catalog.json")).hexdigest()
            if catalog_checksum != manifest["catalogSha256"]:
                raise ValueError("会话目录校验失败")
            pairs = [(e["object"], e["sha256"], e["size"]) for e in entries]
        elif schema == "conversation-migration-1":
            entries = [e for e in manifest["entries"] if "object" in e]
            expected = {"manifest.json"}
            for name, checksum in manifest.get("supportFiles", {}).items():
                if name not in {"migrate.py", "VERIFY-WINDOWS.cmd", "README-WINDOWS.txt", "session-catalog.json"}:
                    raise ValueError("旧数据包的附加文件不受支持")
                if hashlib.sha256(z.read(name)).hexdigest() != checksum:
                    raise ValueError("旧数据包附加文件校验失败")
                expected.add(name)
            pairs = [(e["object"], e["sha256"], e["capturedSize"]) for e in entries]
        else:
            raise ValueError("数据包版本尚未支持；没有导入或改写它")
        for name, _, _ in pairs:
            check_cancel(cancel)
            if name in expected or not name.startswith("objects/") or len(PurePosixPath(name).parts) != 2:
                raise ValueError("数据包对象路径无效")
            expected.add(name)
        if set(names) != expected:
            raise ValueError("数据包存在未登记或缺失的对象")

        def verify_object(name, checksum, size):
            check_cancel(cancel)
            digest, count = hashlib.sha256(), 0
            with z.open(name) as stream:
                for block in iter(lambda: stream.read(CHUNK), b""):
                    check_cancel(cancel)
                    count += len(block)
                    digest.update(block)
            if digest.hexdigest() != checksum or count != size:
                raise ValueError("文件完整性校验失败：" + name)

        step = f"校验备份包（{len(pairs)} 个对象）"
        if pairs:
            progress(step, 0)
            percent = 0
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="archive-check") as pool:
                futures = [pool.submit(verify_object, *item) for item in pairs]
                try:
                    for count, future in enumerate(as_completed(futures), 1):
                        future.result()
                        updated = 100 * count // len(futures)
                        if updated != percent:
                            percent = updated
                            progress(step, percent)
                finally:
                    for future in futures:
                        future.cancel()
        else:
            progress(step, 100)
        return {"verified": True, "files": len(pairs), "schema": schema}


def encrypt_archive(archive, destination, key_path, progress, cancel):
    key, key_id = read_key(key_path)
    nonce = secrets.token_bytes(12)
    header = _json({"algorithm": "AES-256-GCM", "nonce": base64.b64encode(nonce).decode(), "keyId": key_id}).encode()
    prefix = MAGIC + struct.pack(">I", len(header)) + header
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix)
    checksum, total, done = hashlib.sha256(), archive.stat().st_size, 0
    with archive.open("rb") as src, destination.open("xb") as dst:
        dst.write(prefix)
        for block in iter(lambda: src.read(CHUNK), b""):
            check_cancel(cancel)
            checksum.update(block)
            dst.write(encryptor.update(block))
            done += len(block)
            progress("加密保全备份", int(done * 100 / max(1, total)))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)
        dst.flush()
        os.fsync(dst.fileno())
    return checksum.hexdigest()


def verify_encrypted(archive: Path, key_path: Path, expected_plain_sha: str, progress=lambda *_: None, cancel=None):
    cancel = cancel or threading.Event()
    key, key_id = read_key(key_path)
    with archive.open("rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError("加密备份格式不正确")
        size_data = f.read(4)
        if len(size_data) != 4:
            raise ValueError("加密备份头损坏")
        length = struct.unpack(">I", size_data)[0]
        if not 1 <= length <= 16384:
            raise ValueError("加密备份头大小不受支持")
        header = f.read(length)
        meta = json.loads(header)
        if meta.get("keyId") != key_id or meta.get("algorithm") != "AES-256-GCM":
            raise ValueError("恢复密钥与这份备份不匹配")
        start = f.tell()
        total = archive.stat().st_size
        if total < start + 16:
            raise ValueError("加密备份不完整")
        f.seek(total - 16)
        tag = f.read(16)
        f.seek(start)
        decoder = Cipher(algorithms.AES(key), modes.GCM(base64.b64decode(meta["nonce"]), tag)).decryptor()
        decoder.authenticate_additional_data(MAGIC + size_data + header)
        left, done, h = total - start - 16, 0, hashlib.sha256()
        while left:
            check_cancel(cancel)
            block = f.read(min(CHUNK, left))
            if not block:
                raise ValueError("加密备份被截断")
            left -= len(block)
            done += len(block)
            h.update(decoder.update(block))
            progress("实际解密回读", int(done * 100 / max(1, total - start - 16)))
        h.update(decoder.finalize())
        if h.hexdigest() != expected_plain_sha:
            raise ValueError("解密回读与原始归档不一致")
    return True


def decrypt_for_preview(archive: Path, key_path: Path, cache: Path, progress=lambda *_: None, auth_path=None):
    """Decrypt into this app's private cache, never into an original client's root."""
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    signature = str(archive.resolve()) + str(archive.stat().st_size) + str(archive.stat().st_mtime_ns) + hash_file(key_path)
    target = cache / (hashlib.sha256(signature.encode()).hexdigest() + ".zip")
    if target.exists():
        return target
    if shutil.disk_usage(cache).free < archive.stat().st_size + 128 * CHUNK:
        raise ValueError("解密预览需要约 " + str(round(archive.stat().st_size / 2**30, 1)) + " GiB 缓存空间，请先在设置中选择空间充足的缓存位置")
    part = cache / (".decrypt-" + uuid.uuid4().hex + ".part")
    try:
        with archive.open("rb") as src:
            magic = src.read(8)
            src.seek(0)
            if magic.startswith(MAGIC):
                key, kid = read_key(key_path)
                prefix = src.read(len(MAGIC))
                length_raw = src.read(4)
                length = struct.unpack(">I", length_raw)[0]
                if not 1 <= length <= 16384:
                    raise ValueError("加密备份头损坏")
                raw_header = src.read(length)
                meta = json.loads(raw_header)
                if kid != meta.get("keyId"):
                    raise ValueError("恢复密钥不匹配")
                start = src.tell()
                end = archive.stat().st_size - 16
                if end < start:
                    raise ValueError("加密备份不完整")
                src.seek(end); tag = src.read(16); src.seek(start)
                decoder = Cipher(algorithms.AES(key), modes.GCM(base64.b64decode(meta["nonce"]), tag)).decryptor()
                decoder.authenticate_additional_data(prefix + length_raw + raw_header)
                unpadder = None
            elif magic == b"Salted__":
                if auth_path is None:
                    raise ValueError("旧加密备份需要配套的 backup-auth.json")
                meta = json.loads(Path(auth_path).read_text())
                password_file = stable_bytes(key_path, 16384)
                mac = hmac.new(hashlib.sha256(b"backup-auth-v1\x00" + password_file).digest(), digestmod="sha256")
                checksum = hashlib.sha256()
                for block in iter(lambda: src.read(CHUNK), b""):
                    mac.update(block); checksum.update(block)
                if not hmac.compare_digest(mac.hexdigest(), meta.get("hmac", "")) or checksum.hexdigest() != meta.get("ciphertextSha256"):
                    raise ValueError("旧备份认证失败，密钥或文件可能不匹配")
                src.seek(8)
                salt_size = 8 if archive.stat().st_size % 16 == 0 else 16
                salt = src.read(salt_size)
                material = hashlib.pbkdf2_hmac("sha256", password_file.splitlines()[0], salt, 200000, 48)
                decoder = Cipher(algorithms.AES(material[:32]), modes.CBC(material[32:])).decryptor()
                unpadder = padding.PKCS7(128).unpadder()
                start, end = src.tell(), archive.stat().st_size
            else:
                raise ValueError("不支持的加密备份格式；原件未改变")
            total = end - start
            left = total
            with part.open("xb") as dst:
                while left:
                    block = src.read(min(CHUNK, left))
                    if not block:
                        raise ValueError("加密备份被截断")
                    left -= len(block)
                    plain = decoder.update(block)
                    dst.write(unpadder.update(plain) if unpadder else plain)
                    progress("解密到私有预览缓存", int((total - left) * 100 / max(1, total)))
                final = decoder.finalize()
                dst.write(unpadder.update(final) + unpadder.finalize() if unpadder else final)
                dst.flush(); os.fsync(dst.fileno())
        os.replace(part, target)
        return target
    finally:
        part.unlink(missing_ok=True)


def planned_projects(library, selected, project_choices, include_projects, check):
    projects = discover_projects(selected, library.home, check)
    return configured_projects(projects, project_choices, library.home, include_projects)


def estimate_backup(library, selected, destination, key_path, include_projects=False, project_choices=None, cancel=None):
    """Estimate from file metadata without extracting packages or copying sources."""
    cancel = cancel or threading.Event()

    def check():
        check_cancel(cancel)

    snapshots = library.snapshots.directory
    cache = snapshots.parent
    destination = Path(destination).resolve()
    registered = {record.uid for record in selected}
    projects = {}
    sizes = {}
    databases = {}
    derived = 0
    extracts = 0

    def collect(path, role, project=""):
        check()
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return 0
        sizes.setdefault((str(path.resolve()), project), (role, size))
        return size

    package_catalog = getattr(library, "package_catalog", None)
    archive = getattr(library, "archive", None)
    if package_catalog is not None:
        for project in package_catalog.get("projects", []):
            if registered.intersection(project.get("members", {})):
                projects[project["id"]] = {"title": project["title"], "mode": project["mode"] if include_projects else "link", "files": 0, "bytes": 0}
        for name, entry in library.entries.items():
            check()
            if entry.get("sessionUid") not in registered and not registered.intersection(entry.get("sessionUids", ())):
                continue
            role, size = entry["role"], entry["size"]
            project = projects.get(entry.get("projectId"))
            if role in {"project-work-file", "project-local-change"}:
                if not include_projects:
                    continue
                if project is not None:
                    project["files"] += 1
                    project["bytes"] += size
            sizes[name] = (role, size)
            extracts += size
    elif archive is not None:
        extracts = collect(archive, "existing-package")
    else:
        for record in selected:
            check()
            for path, role in library.original_files(record):
                size = collect(path, role)
                if role in {"original-session", "claude-reading"}:
                    derived += size
            for name in ("state_database", "history_database"):
                source = Path(record.metadata.get(name) or "")
                root = Path(record.root)
                if source in databases or not owned_file(source, root):
                    continue
                wal = source.with_name(source.name + "-wal")
                try:
                    databases[source] = source.stat().st_size + (wal.stat().st_size if owned_file(wal, root) else 0)
                except FileNotFoundError:
                    continue
        history_sizes = Counter(r.metadata.get("history_database") for r in library.catalog.records if r.provider == "codex")
        selected_histories = Counter(r.metadata.get("history_database") for r in selected if r.provider == "codex")
        for name, count in selected_histories.items():
            if name:
                # Codex can contribute both viewer events and native database rows.
                derived += 2 * databases.get(Path(name), 0) * count // max(count, history_sizes[name])
        for project, mode, carry in planned_projects(library, selected, project_choices, include_projects, check):
            row = {"title": project.root.name, "mode": mode, "files": 0, "bytes": 0}
            projects[project.id] = row
            role = "project-local-change" if mode == "git" else "project-work-file"
            for path, _ in project_files(project, mode, carry, [destination, Path(key_path), cache], check):
                row["bytes"] += collect(path, role, project.id)
                row["files"] += 1

    reserve = 128 * CHUNK
    archive_metadata = 65536 + len(sizes) * 512
    footprint = sum(size for _, size in sizes.values())
    portable = archive_metadata + derived + sum(size for role, size in sizes.values() if role != "claude-desktop-index")
    private = archive_metadata + derived + footprint
    largest = max((size for _, size in sizes.values()), default=0)
    output_temporary = archive_metadata + derived + footprint + largest
    cache_temporary = largest + sum(databases.values()) + extracts

    cache_directory = next((path for path in (cache, *cache.parents) if path.is_dir()), None)
    output_directory = next((path for path in (destination, *destination.parents) if path.is_dir()), None)
    if cache_directory is None or output_directory is None:
        raise ValueError("备份位置或缓存所在磁盘不可用，请重新连接后估算")
    same_volume = cache_directory.stat().st_dev == output_directory.stat().st_dev
    output_free = shutil.disk_usage(output_directory).free
    cache_free = output_free if same_volume else shutil.disk_usage(cache_directory).free
    output_required = portable + private + output_temporary + reserve
    cache_required = cache_temporary + reserve
    enough = output_free >= (output_required + cache_temporary if same_volume else output_required) and (
        same_volume or cache_free >= cache_required)
    return {"portableBytes": portable, "encryptedBytes": private, "outputTemporaryBytes": output_temporary,
            "cacheTemporaryBytes": cache_temporary, "outputRequired": output_required,
            "cacheRequired": cache_required, "outputFree": output_free, "cacheFree": cache_free,
            "sameVolume": same_volume, "cache": str(cache), "enough": enough,
            "projects": list(projects.values())}


def make_backup(library, selected: list[SessionRecord], destination: Path, key_path: Path, progress=lambda *_: None,
                cancel=None, annotations=None, include_projects=False, *, project_choices=None, model=None,
                generation_cancel=None):
    cancel = cancel or threading.Event()
    if not selected:
        raise ValueError("请先选择会话")
    destination = destination.expanduser().resolve()
    key_path = key_path.resolve()
    protected = {source["path"] for source in library.catalog.sources} | {r.root for r in selected if not r.metadata.get("preview_only")}
    for value in protected:
        root = Path(value)
        if within(destination, root) or within(key_path, root):
            raise ValueError("备份和密钥必须保存在源资料目录之外")
    if within(key_path, destination):
        raise ValueError("请把恢复密钥保存到备份位置之外")
    read_key(key_path)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    output = destination / ("会话备份-" + run_id)
    output.mkdir(mode=0o700)
    report = {"schema": SCHEMA, "status": "capturing", "startedAt": time.time(), "sessions": len(selected), "providers": sorted({r.provider for r in selected}), "sourceDataModified": False, "thinkingTransform": False, "snapshotMode": "online-per-file-and-per-database", "needsColdFinalBackup": True, "windowsImported": False, "modelVerified": False, "includeProjectWorkFiles": include_projects, "warnings": [], "output": str(output), "keyPath": str(key_path)}
    report_path = output / "结果.json"
    atomic_json(report_path, report)
    entries, catalog = [], {"schema": SCHEMA, "records": [r.to_dict() for r in selected], "histories": {}, "registries": library.selected_registry(selected), "annotations": annotations or {}, "viewerOnly": True}
    portable = output / ("Windows-会话迁移包-" + run_id + ".zip")
    private_work = tempfile.TemporaryDirectory(prefix=".会话快照-", dir=output)
    part = Path(private_work.name) / "original.zip"
    portable_part = output / ".迁移副本.zip.part"
    stage = output / ".当前文件.tmp"
    sources, seen, asset_info, project_assets = [], set(), {}, {}
    try:
        progress("取得历史数据库副本", 0)
        capture_started = time.time()
        library = library.for_backup(selected, include_projects)
        with zipfile.ZipFile(part, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as z:
            for index, record in enumerate(selected):
                check_cancel(cancel)
                progress("读取选定会话", int(index * 100 / len(selected)))
                object_name = "objects/" + str(len(entries)).zfill(8)
                h, size, count = hashlib.sha256(), 0, 0
                attachments = []
                with z.open(object_name, "w", force_zip64=True) as target:
                    for event in library.raw_events(record):
                        check_cancel(cancel)
                        if "_viewer_warning" in event:
                            report["warnings"].append(record.title + "：" + event["_viewer_warning"])
                            continue
                        raw = (_json(event) + "\n").encode()
                        target.write(raw)
                        h.update(raw)
                        size += len(raw)
                        count += 1
                        library._event_attachments(record, event, attachments, include_embedded=False)
                entries.append({"object": object_name, "sha256": h.hexdigest(), "size": size, "role": "lossless-history-export", "sessionUid": record.uid, "events": count})
                if not count:
                    report["warnings"].append(record.title + "：未读到已保存的历史事件，保留了目录和可用原始文件")
                if "格式" in record.issue:
                    report["warnings"].append(record.title + "：格式兼容未验证，保留原始记录")
                catalog["histories"][record.uid] = object_name
                if record.provider == "codex":
                    with closing(library.native_database_rows(record)) as rows:
                        first = next(rows, None)
                        if first is not None:
                            native_name = "objects/" + str(len(entries)).zfill(8)
                            native_hash, native_size = hashlib.sha256(), 0
                            with z.open(native_name, "w", force_zip64=True) as target:
                                for row in chain((first,), rows):
                                    check_cancel(cancel)
                                    raw = (_json(row) + "\n").encode()
                                    target.write(raw); native_hash.update(raw); native_size += len(raw)
                            entries.append({"object": native_name, "sha256": native_hash.hexdigest(), "size": native_size, "role": "native-database-rows", "logicalName": "codex-native-rows.jsonl", "sessionUid": record.uid})
                sources.extend((p, role, record.uid) for p, role in library.original_files(record))
                for attachment in attachments:
                    if attachment.path and Path(attachment.path).is_file():
                        sources.append((Path(attachment.path), "attachment", record.uid))
                        asset_info[str(Path(attachment.path).resolve())] = {"displayName": attachment.name, "mediaType": attachment.media_type}
                    elif attachment.note:
                        report["warnings"].append(attachment.name + "：" + attachment.note)
            owners = defaultdict(set)
            project_data = []
            if getattr(library, "package_catalog", None):
                for previous in library.package_catalog.get("projects", []):
                    members = {uid: sub for uid, sub in previous["members"].items() if uid in {r.uid for r in selected}}
                    if members:
                        project_data.append(dict(previous, members=members) if include_projects else
                                            dict(previous, members=members, mode="link", git={}, changes={}))
            else:
                progress("统计关联项目工作文件", 0)
                for project, mode, carry in planned_projects(library, selected, project_choices, include_projects, lambda: check_cancel(cancel)):
                    project_data.append(project.manifest(mode, carry))
                    role = "project-local-change" if mode == "git" else "project-work-file"
                    for p, relative in project_files(project, mode, carry,
                                                    [destination, key_path, library.snapshots.directory.parent],
                                                    lambda: check_cancel(cancel)):
                        for uid in project.members:
                            project_assets[str(p.resolve()), uid] = {"projectId": project.id, "projectPath": relative}
                            sources.append((p, role, uid))
            catalog["projects"] = project_data
            by_uid = {item["uid"]: item for item in catalog["records"]}
            for entry in project_data:
                for uid, relative in entry["members"].items():
                    by_uid[uid]["metadata"].update(project_ref=entry["id"], project_relative=relative,
                                                   project_title=entry["title"])
            def item_identity(p, role, uid):
                path = str(p.resolve())
                extra = project_assets.get((path, uid), {}) if role in {"project-work-file", "project-local-change"} else {}
                return (path, extra.get("projectId", "")), extra

            for p, role, uid in sources:
                identity, _ = item_identity(p, role, uid)
                owners[identity].add(uid)
            estimated = sum({str(p.resolve()): p.stat().st_size for p, _, _ in sources if p.exists()}.values())
            if shutil.disk_usage(output).free < estimated * 3 + part.stat().st_size * 2 + 128 * CHUNK:
                raise ValueError("备份位置空间不足，请选择空间充足的位置")
            for index, (p, role, uid) in enumerate(sources):
                check_cancel(cancel)
                identity, project_info = item_identity(p, role, uid)
                if identity in seen:
                    continue
                seen.add(identity)
                progress("创建原始资料归档", int((index + 1) * 100 / max(1, len(sources))))
                try:
                    meta = _stable_file(p, stage, cancel, library.snapshots.directory.parent / "payload")
                except FileNotFoundError:
                    report["warnings"].append(p.name + "：读取前已被来源程序移除")
                    continue
                name = "objects/" + str(len(entries)).zfill(8)
                z.write(stage, name)
                entries.append({"object": name, **meta, "originalPath": str(p), "role": role, "sessionUid": uid, "sessionUids": sorted(owners[identity]), **library.origin_metadata(p), **asset_info.get(identity[0], {}), **project_info})
                if role == "claude-reading" and hasattr(library, "draft"):
                    record = next(r for r in selected if r.uid == uid)
                    entries[-1]["readingNumber"] = record.session_id
                    prompt = library.draft(record, meta["sha256"])
                    if prompt:
                        data = prompt.encode("utf-8")
                        draft_name = "objects/" + str(len(entries)).zfill(8)
                        z.writestr(draft_name, data)
                        entries.append({"object": draft_name, "role": "handoff-draft", "sessionUid": uid,
                                        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                        "sourceSha256": meta["sha256"], "readingNumber": record.session_id})
                stage.unlink(missing_ok=True)
            raw_catalog = _json(catalog).encode()
            z.writestr("catalog.json", raw_catalog)
            manifest = {"schema": SCHEMA, "purpose": "private-preservation", "entries": entries, "catalogSha256": hashlib.sha256(raw_catalog).hexdigest(), "snapshotMode": report["snapshotMode"], "sourceDataModified": False, "thinkingTransform": False, "projectWorkFilesIncluded": include_projects, "projectExclusions": ["Git 元数据", "依赖缓存", "独立凭据和密钥", "备份与本工具缓存位置"], "warnings": list(dict.fromkeys(report["warnings"])), "windowsImported": False, "modelVerified": False}
            report["snapshotWindow"] = {"startedAt": capture_started, "finishedAt": time.time(),
                                        "selectedSessions": len(selected), "projectCount": len(project_data),
                                        "nativeDatabaseObjects": sum(e["role"] == "native-database-rows" for e in entries)}
            manifest["snapshotWindow"] = report["snapshotWindow"]
            z.writestr("manifest.json", _json(manifest))
        if report["warnings"]:
            report["warnings"] = [f"采集时有 {len(manifest['warnings'])} 项需要关注，明细保存在加密保全清单中。"]
        report.update(files=len(entries), originalFiles=len(seen))
        atomic_json(report_path, report)
        report["archiveCheck"] = verify_package(part, progress, cancel)
        check_cancel(cancel)
        encrypted_part = output / ".加密备份处理中.asmb.part"
        plain_sha = encrypt_archive(part, encrypted_part, key_path, progress, cancel)
        verify_encrypted(encrypted_part, key_path, plain_sha, progress, cancel)
        encrypted = output / ("Mac-会话保全备份-" + run_id + ".asmb")
        os.rename(encrypted_part, encrypted)
        report.update(encrypted=str(encrypted), encryptedReadbackVerified=True, plainSha256=plain_sha)
        report["privacyCheck"] = build_portable(part, portable_part, lambda: check_cancel(cancel), progress, model=model,
                                                generation_cancel=generation_cancel)
        report["portableCheck"] = verify_package(portable_part, progress, cancel)
        os.rename(portable_part, portable)
        with portable.open("r+b") as f:
            os.fsync(f.fileno())
        report.update(status="complete-with-warnings" if report["warnings"] else "complete", finishedAt=time.time(), files=len(entries), originalFiles=len(seen), encryptedReadbackVerified=True, portable=str(portable), encrypted=str(encrypted), plainSha256=plain_sha, bytes=portable.stat().st_size, warnings=list(dict.fromkeys(report["warnings"])))
        with zipfile.ZipFile(portable) as bundle:
            atomic_json(output / "文件清单.json", json.loads(bundle.read("manifest.json")))
            readings = json.loads(bundle.read("catalog.json")).get("readings", {})
            report["handoff"] = dict(Counter(e["status"] for e in readings.values()))
        atomic_json(report_path, report)
        progress("备份与回读完成", 100)
        return report
    except BaseException as error:
        if isinstance(error, PortablePrivacyError):
            report["privacyCheck"] = {"verified": False, "findings": error.findings}
        report.update(status="cancelled" if isinstance(error, Cancelled) else "failed", error=type(error).__name__ + "：" + str(error), finishedAt=time.time())
        atomic_json(report_path, report)
        error.report = report
        raise
    finally:
        stage.unlink(missing_ok=True)
        portable_part.unlink(missing_ok=True)
        (output / ".加密备份处理中.asmb.part").unlink(missing_ok=True)
        private_work.cleanup()


def rebuild_portable(report, key_path, exclusions, progress=lambda *_: None, cancel=None):
    """Recheck the original encrypted snapshot and publish only a new migration ZIP."""
    if not report.get("encryptedReadbackVerified") or not report.get("encrypted") or not report.get("plainSha256"):
        raise ValueError("重新导出需要这次任务已校验的原样加密保全")
    preserved = Path(report.get("preservationDirectory") or report["output"]).resolve()
    encrypted = Path(report["encrypted"])
    if encrypted.is_symlink() or not encrypted.is_file() or encrypted.resolve().parent != preserved:
        raise ValueError("原样加密保全的位置已变化，请先从备份库打开该文件")
    forbid_client_storage(preserved)
    previous = report.get("privacyExclusions") or {}
    excluded = {kind: sorted(set(exclusions.get(kind, ())) | set(previous.get(kind, ())))
                for kind in ("sessions", "objects", "projects")}
    if not any(excluded.values()):
        raise ValueError("请先选择要排除的会话或项目资料")
    cancel = cancel or threading.Event()
    def check():
        check_cancel(cancel)

    def decrypt_progress(stage, percent):
        check()
        progress(stage, percent)

    read_key(Path(key_path))
    serial = uuid.uuid4().hex[:8]
    output = preserved.parent / ("迁移重建-" + serial)
    output.mkdir(mode=0o700)
    updated = {key: value for key, value in report.items() if key not in {"portable", "portableCheck", "privacyCheck", "handoff", "error", "bytes"}}
    updated.update(output=str(output), preservationDirectory=str(preserved), keyPath=str(key_path),
                   startedAt=time.time(), privacyExclusions=excluded, status="rebuilding")
    archive = output / ("Windows-会话迁移包-" + serial + ".zip")
    part = archive.with_suffix(".zip.part")
    try:
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=output) as temp:
            captured = decrypt_for_preview(encrypted, Path(key_path), Path(temp), decrypt_progress)
            check()
            if hash_file(captured) != report["plainSha256"]:
                raise ValueError("解密后的原样保全与该任务的校验记录不一致")
            updated["privacyCheck"] = build_portable(captured, part, check, progress, excluded=excluded)
            updated["portableCheck"] = verify_package(part, progress, cancel)
        with zipfile.ZipFile(part) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            catalog = json.loads(bundle.read("catalog.json"))
        check()
        with part.open("r+b") as stream:
            os.fsync(stream.fileno())
        atomic_json(output / "文件清单.json", manifest)
        os.rename(part, archive)
        updated.update(status="complete-with-warnings" if updated.get("warnings") else "complete",
                       portable=str(archive), bytes=archive.stat().st_size, sessions=len(catalog["records"]),
                       files=len(manifest["entries"]), finishedAt=time.time(),
                       handoff=dict(Counter(e["status"] for e in catalog.get("readings", {}).values())))
        atomic_json(output / "结果.json", updated)
        return updated
    except Exception as error:
        if isinstance(error, PortablePrivacyError):
            updated["privacyCheck"] = {"verified": False, "findings": error.findings}
        updated.update(status="cancelled" if isinstance(error, Cancelled) else "failed", error=str(error), finishedAt=time.time())
        atomic_json(output / "结果.json", updated)
        error.report = updated
        raise
    finally:
        part.unlink(missing_ok=True)


def resume_backup(report: dict, key_path: Path, progress=lambda *_: None, cancel=None, protected_roots=()):
    """Resume at the verified-archive boundary; incomplete raw capture uses a new run."""
    cancel = cancel or threading.Event()
    output = Path(report["output"]).resolve()
    archive = Path(report.get("pendingArchive", "")).resolve()
    if report.get("schema") != SCHEMA or not report.get("archiveReady") or not archive.is_file() or archive.parent != output or archive.name != ".归档处理中.zip.part" or not output.name.startswith("会话备份-"):
        raise ValueError("这份任务没有可安全续接的完整归档，请新建备份；旧结果保留")
    if any(within(output, Path(root)) or within(key_path, Path(root)) for root in protected_roots):
        raise ValueError("恢复任务的输出与密钥不能位于原始资料目录中")
    if within(key_path, output):
        raise ValueError("恢复密钥需与备份分开保存")
    read_key(key_path)
    updated = dict(report)
    updated["attempts"] = list(report.get("attempts", [])) + [{"status": report.get("status"), "error": report.get("error"), "at": time.time()}]
    try:
        updated["archiveCheck"] = verify_package(archive, progress, cancel)
        suffix = uuid.uuid4().hex[:8]
        encrypted_part = output / (".加密恢复-" + suffix + ".part")
        plain_sha = encrypt_archive(archive, encrypted_part, key_path, progress, cancel)
        verify_encrypted(encrypted_part, key_path, plain_sha, progress, cancel)
        encrypted = output / ("Mac-会话保全备份-恢复-" + suffix + ".asmb")
        portable = output / ("Windows-会话迁移包-恢复-" + suffix + ".zip")
        os.rename(encrypted_part, encrypted)
        portable_part = output / (".迁移副本-" + suffix + ".part")
        try:
            updated["privacyCheck"] = build_portable(archive, portable_part, lambda: check_cancel(cancel), progress)
            updated["portableCheck"] = verify_package(portable_part, progress, cancel)
            os.rename(portable_part, portable)
        finally:
            portable_part.unlink(missing_ok=True)
        archive.unlink()
        updated.update(status="complete-with-warnings" if updated.get("warnings") else "complete", finishedAt=time.time(), portable=str(portable), encrypted=str(encrypted), encryptedReadbackVerified=True, plainSha256=plain_sha, bytes=portable.stat().st_size, keyPath=str(key_path), error=None)
        with zipfile.ZipFile(portable) as package:
            atomic_json(output / "文件清单.json", json.loads(package.read("manifest.json")))
        atomic_json(output / "结果.json", updated)
        return updated
    except Exception as error:
        updated.update(status="cancelled" if isinstance(error, Cancelled) else "failed", error=type(error).__name__ + "：" + str(error), finishedAt=time.time())
        atomic_json(output / "结果.json", updated)
        error.report = updated
        raise

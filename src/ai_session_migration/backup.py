from __future__ import annotations

import base64
from collections import defaultdict
import hashlib
import hmac
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
from .safeio import CHUNK, atomic_json, hash_file, stable_bytes, within, independent_copy, file_signature, remote_path

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
        if schema == SCHEMA:
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
        for index, (name, checksum, size) in enumerate(pairs):
            check_cancel(cancel)
            if name in expected or not name.startswith("objects/") or len(PurePosixPath(name).parts) != 2:
                raise ValueError("数据包对象路径无效")
            expected.add(name)
            h, count = hashlib.sha256(), 0
            with z.open(name) as f:
                for block in iter(lambda: f.read(CHUNK), b""):
                    check_cancel(cancel)
                    count += len(block)
                    h.update(block)
            if h.hexdigest() != checksum or count != size:
                raise ValueError("文件完整性校验失败：" + name)
            if index % 20 == 0:
                progress("逐文件校验", int((index + 1) * 100 / max(1, len(pairs))))
        if set(names) != expected:
            raise ValueError("数据包存在未登记或缺失的对象")
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


def make_backup(library, selected: list[SessionRecord], destination: Path, key_path: Path, progress=lambda *_: None, cancel=None, annotations=None, include_projects=False):
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
    part = output / ".归档处理中.zip.part"
    stage = output / ".当前文件.tmp"
    sources, seen, asset_info = [], set(), {}
    try:
        progress("取得历史数据库副本", 0)
        library = library.for_backup(selected)
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
                sources.extend((p, role, record.uid) for p, role in library.original_files(record))
                for attachment in attachments:
                    if attachment.path and Path(attachment.path).is_file():
                        sources.append((Path(attachment.path), "attachment", record.uid))
                        asset_info[str(Path(attachment.path).resolve())] = {"displayName": attachment.name, "mediaType": attachment.media_type}
                    elif attachment.note:
                        report["warnings"].append(attachment.name + "：" + attachment.note)
            owners = defaultdict(set)
            if include_projects:
                skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", ".nuxt", ".turbo", ".pytest_cache"}
                for record in selected:
                    if record.metadata.get("preview_only"):
                        report["warnings"].append("数据包预览中的项目原路径不会自动读取，请在本机来源中另行关联")
                        continue
                    project = Path(record.cwd) if record.cwd else None
                    if project is None or remote_path(project) or not project.is_absolute() or not project.is_dir():
                        report["warnings"].append(record.title + "：项目目录缺失，未纳入工作文件")
                        continue
                    project = project.resolve()
                    if project == library.home or len(project.parts) < 5:
                        report["warnings"].append(record.title + "：工作目录过于宽泛，未自动收集项目文件")
                        continue
                    progress("统计关联项目工作文件", 0)
                    for base, dirs, files in os.walk(project, followlinks=False):
                        check_cancel(cancel)
                        dirs[:] = [d for d in dirs if d not in skip_dirs and not (Path(base) / d).is_symlink() and not within(Path(base) / d, destination) and not within(Path(base) / d, library.snapshots.directory.parent)]
                        for filename in files:
                            p = Path(base) / filename
                            if p.is_symlink() or p.resolve() == key_path or filename.startswith((".env", ".credentials")) or p.suffix.lower() in {".key", ".asmkey", ".pem", ".p12", ".pfx", ".keystore"} or filename in {"auth.json", "credentials.json"}:
                                continue
                            sources.append((p, "project-work-file", record.uid))
            for p, _, uid in sources:
                owners[str(p.resolve())].add(uid)
            estimated = sum({str(p.resolve()): p.stat().st_size for p, _, _ in sources if p.exists()}.values())
            if shutil.disk_usage(output).free < estimated * 2 + 128 * CHUNK:
                raise ValueError("备份位置空间不足，请选择空间充足的位置")
            for index, (p, role, uid) in enumerate(sources):
                check_cancel(cancel)
                identity = str(p.resolve())
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
                entries.append({"object": name, **meta, "originalPath": str(p), "role": role, "sessionUid": uid, "sessionUids": sorted(owners[identity]), **asset_info.get(identity, {})})
                stage.unlink(missing_ok=True)
            raw_catalog = _json(catalog).encode()
            z.writestr("catalog.json", raw_catalog)
            manifest = {"schema": SCHEMA, "entries": entries, "catalogSha256": hashlib.sha256(raw_catalog).hexdigest(), "snapshotMode": report["snapshotMode"], "sourceDataModified": False, "thinkingTransform": False, "projectWorkFilesIncluded": include_projects, "projectExclusions": ["Git 元数据", "依赖缓存", "独立凭据和密钥", "备份与本工具缓存位置"], "warnings": list(dict.fromkeys(report["warnings"])), "windowsImported": False, "modelVerified": False}
            z.writestr("manifest.json", _json(manifest))
        report.update(archiveReady=True, pendingArchive=str(part), files=len(entries), originalFiles=len(seen))
        atomic_json(report_path, report)
        report["archiveCheck"] = verify_package(part, progress, cancel)
        check_cancel(cancel)
        encrypted_part = output / ".加密备份处理中.asmb.part"
        plain_sha = encrypt_archive(part, encrypted_part, key_path, progress, cancel)
        verify_encrypted(encrypted_part, key_path, plain_sha, progress, cancel)
        encrypted = output / ("Mac-会话保全备份-" + run_id + ".asmb")
        os.rename(encrypted_part, encrypted)
        os.rename(part, portable)
        with portable.open("r+b") as f:
            os.fsync(f.fileno())
        report.update(status="complete-with-warnings" if report["warnings"] else "complete", finishedAt=time.time(), files=len(entries), originalFiles=len(seen), encryptedReadbackVerified=True, portable=str(portable), encrypted=str(encrypted), plainSha256=plain_sha, bytes=portable.stat().st_size, warnings=list(dict.fromkeys(report["warnings"])))
        atomic_json(output / "文件清单.json", manifest)
        atomic_json(report_path, report)
        progress("备份与回读完成", 100)
        return report
    except BaseException as error:
        report.update(status="cancelled" if isinstance(error, Cancelled) else "failed", error=type(error).__name__ + "：" + str(error), finishedAt=time.time())
        atomic_json(report_path, report)
        error.report = report
        raise
    finally:
        stage.unlink(missing_ok=True)


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
        os.rename(archive, portable)
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

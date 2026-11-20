"""Replace a selected backup directory while retaining its complete previous version."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import uuid
import zipfile

from .backup import SCHEMA, check_cancel, verify_encrypted, verify_package
from .importer import checked_directory, rename_new, target_locks
from .migration_policy import forbid_client_storage
from .portable import check_portable
from .safeio import atomic_json, file_signature, stable_bytes


def paired_backup(report):
    if (report.get("schema") != SCHEMA or report.get("status") not in {"complete", "complete-with-warnings"}
            or not report.get("encryptedReadbackVerified") or not report.get("plainSha256")
            or not all(report.get(k) for k in ("output", "portable", "encrypted"))):
        return False
    root = Path(report["output"])
    return (root.is_absolute() and root.name.startswith("会话备份-")
            and all(Path(report[k]).parent == root for k in ("portable", "encrypted")))


def _fingerprint(directory, report):
    if checked_directory(directory) != directory or not directory.is_dir():
        raise ValueError("备份目录已移动或经过链接，请重新选择备份")
    result = {}
    for kind in ("portable", "encrypted"):
        path = directory / Path(report[kind]).name
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise ValueError("备份文件已移动、缺失或经过链接，请重新选择备份")
        result[kind] = list(file_signature(path))
    return result


def _verify_references(archive):
    with zipfile.ZipFile(archive) as bundle:
        catalog = json.loads(bundle.read("catalog.json"))
        manifest = json.loads(bundle.read("manifest.json"))
    entries = {entry["object"]: entry for entry in manifest["entries"]}
    records = {record["uid"] for record in catalog["records"]}
    histories = catalog["histories"]
    if records != histories.keys() or any(entries.get(name, {}).get("role") != "lossless-history-export"
                                          or uid != entries[name].get("sessionUid") for uid, name in histories.items()):
        raise ValueError("新版备份的会话正文引用不完整")
    for uid, reading in catalog.get("readings", {}).items():
        if uid not in records or (reading.get("parentUid") and reading["parentUid"] not in records):
            raise ValueError("新版备份的 Claude 阅读引用无法核对")
        if reading.get("status") == "unavailable":
            continue
        original = entries.get(reading.get("object"), {})
        if (original.get("role") != "claude-reading" or original.get("sessionUid") != uid
                or original.get("sha256") != reading.get("sourceSha256")):
            raise ValueError("新版备份的 Claude 阅读引用无法核对")
        if draft_id := reading.get("draft"):
            draft = entries.get(draft_id, {})
            if (draft.get("role") != "handoff-draft" or draft.get("sessionUid") != uid
                    or draft.get("sourceSha256") != original["sha256"]):
                raise ValueError("新版备份的 Claude 接手稿与阅读原文不匹配")


def _rebased(report, directory):
    return {**report, "output": str(directory), **{
        kind: str(directory / Path(report[kind]).name) for kind in ("portable", "encrypted")
    }}


def switch_backup_version(new, old, key_path, cache, progress=lambda *_: None, cancel=None):
    if not paired_backup(new) or not paired_backup(old):
        raise ValueError("请选择两份带迁移 ZIP 和加密保全的已完成备份")
    candidate = checked_directory(Path(new["output"]))
    current = checked_directory(Path(old["output"]))
    if candidate == current or candidate.parent != current.parent:
        raise ValueError("请在同一个备份总目录另存新版，再选择旧版进行切换")
    forbid_client_storage(candidate)
    forbid_client_storage(current)
    operation = hashlib.sha256((str(candidate) + "\0" + str(current)).encode("utf-8")).hexdigest()[:20]
    journal = current.parent / f".asm-backup-switch-{operation}.json"
    cancel = cancel or threading.Event()

    with target_locks({"candidate": str(candidate), "current": str(current)}, Path(cache)):
        if journal.is_file():
            saved = json.loads(stable_bytes(journal))
            retained = current.parent / saved.get("retained", "")
            if (saved.get("operation") != operation or saved.get("candidate") != candidate.name
                    or saved.get("current") != current.name or retained.parent != current.parent
                    or not retained.name.startswith("会话备份-旧版-")):
                raise ValueError("备份切换记录不匹配，请核对备份位置")
            checked_directory(retained)
            if retained.is_dir() and current.is_dir() and not candidate.exists():
                if (_fingerprint(current, new) != saved['newFingerprint']
                        or _fingerprint(retained, old) != saved['oldFingerprint']):
                    raise ValueError("切换后文件已变化，请保留两份备份并核对")
                return _finish(new, old, current, retained, journal, saved)
            if retained.is_dir() and candidate.is_dir() and not current.exists():
                if _fingerprint(retained, old) != saved['oldFingerprint']:
                    raise ValueError("保留的旧备份已变化，请核对恢复记录")
                rename_new(retained, current)
            elif retained.exists() and saved.get("phase") != "published":
                raise ValueError("已有未完成的备份切换，请保留两个目录并核对恢复记录")

        initial = _fingerprint(candidate, new)
        original = _fingerprint(current, old)
        progress("正在回读新版迁移包", 0)
        verify_package(Path(new["portable"]), progress, cancel)
        check_portable(Path(new["portable"]), check=lambda: check_cancel(cancel))
        _verify_references(Path(new["portable"]))
        verify_encrypted(Path(new["encrypted"]), Path(key_path), new["plainSha256"], progress, cancel)
        if initial != _fingerprint(candidate, new) or original != _fingerprint(current, old):
            raise ValueError("备份在校验期间发生变化，请重新选择")
        check_cancel(cancel)

        retained = current.parent / ("会话备份-旧版-" + uuid.uuid4().hex[:12])
        saved = dict(operation=operation, candidate=candidate.name, current=current.name, retained=retained.name,
                     phase="verified", newFingerprint=initial, oldFingerprint=original, keyPath=str(key_path))
        atomic_json(journal, saved)
        progress("保留旧版本并切换目录", 95)
        rename_new(current, retained)
        try:
            rename_new(candidate, current)
        except OSError:
            if not current.exists():
                rename_new(retained, current)
            raise
        return _finish(new, old, current, retained, journal, saved)


def _finish(new, old, current, retained, journal, saved):
    active, prior = _rebased(new, current), _rebased(old, retained)
    active.update(retainedVersion=str(retained), versionSwitchRecord=str(journal), keyPath=saved["keyPath"])
    prior.update(versionSwitchRecord=str(journal))
    atomic_json(current / "结果.json", active)
    atomic_json(journal, dict(saved, phase="published"))
    return active, prior

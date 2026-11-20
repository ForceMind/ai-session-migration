"""Read the archive and Windows session stores to build a restore plan."""

from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import unicodedata
import zipfile


def index_entries(package):
    indexed = defaultdict(list)
    for entry in package.entries.values():
        uids = set(entry.get("sessionUids", []))
        if entry.get("sessionUid"):
            uids.add(entry["sessionUid"])
        for uid in uids:
            indexed[uid].append(entry)
    return indexed


def source_project(cwd):
    parts = PurePosixPath(cwd).parts
    if tuple(part.casefold() for part in parts[:3]) == ("/", "volumes", "work"):
        tail = parts[3:]
    elif parts[:2] == ("/", "Users") and parts[3:4] == ("工作",):
        tail = parts[4:]
    else:
        return "", ()
    return (tail[0].casefold(), tail[1:]) if tail else ("", ())


def existing_project(root, parts):
    from ai_session_migration.importer import checked_directory

    if not parts or any(PureWindowsPath(part).is_reserved() or
                        re.search(r'[<>:"/\\|?*\x00-\x1f]|[. ]$', part) for part in parts):
        return None
    path = root.joinpath(*parts)
    try:
        if path.is_dir():
            target = checked_directory(path)
            if target.is_relative_to(root):
                return target
    except (ValueError, OSError):
        pass
    return None


def folded_project_name(name):
    return " ".join(unicodedata.normalize("NFC", name.rstrip("\uf028")).casefold().split())


def client_targets():
    sources = {
        "codex": ("CODEX_HOME", ".codex"),
        "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
        "deepseek": ("DSH_HOME", ".dsh"),
    }
    return {
        provider: Path(os.environ.get(variable) or Path.home() / folder).expanduser().resolve()
        for provider, (variable, folder) in sources.items()
    }


def live_session_ids(cache):
    from ai_session_migration.adapters import LocalLibrary
    from ai_session_migration.importer import codex_databases
    from ai_session_migration.safeio import stable_bytes

    local_home = Path.home()
    targets = client_targets()
    reader = LocalLibrary(local_home, cache / "mapping-preview", check_project_paths=False)
    codex = targets["codex"]
    if codex.is_dir() and any(codex.glob("state_*.sqlite")):
        codex_databases(codex)
    claude = targets["claude"]
    deepseek = targets["deepseek"]
    ids = {
        "codex": {r.session_id for r in reader._scan_codex(codex) + reader._scan_codex_logs(codex)},
        "claude": {p.stem for p in (claude / "projects").rglob("*.jsonl") if p.is_file() and not p.is_symlink()},
        "deepseek": {r.session_id for r in reader._scan_deepseek(deepseek)},
    }
    desktop = local_home / "AppData" / "Local" / "Claude-Data"
    for p in desktop.glob("*/claude-code-sessions/*/*/local_*.json"):
        sid = json.loads(stable_bytes(p)).get("cliSessionId")
        if isinstance(sid, str) and sid:
            ids["claude"].add(sid)

    registry_path = deepseek / "storages" / "workspace.json"
    default_workspace = None
    if registry_path.is_file():
        registry = json.loads(stable_bytes(registry_path))
        if registry.get("unit") != {"name": "workspace", "version": 2}:
            raise ValueError("DeepSeek workspace registry version is incompatible")
        workspaces = registry["tables"]["workspaces"]
        for workspace in workspaces.values():
            ids["deepseek"].update(workspace.get("sessionIds", []))
        default_id = registry["global"].get("defaultWorkspaceId")
        default_workspace = workspaces.get(default_id, {}).get("path")
    return ids, targets, default_workspace


def pending_project(cwd, roots, name_index, deepseek_default):
    label, parts = source_project(cwd)
    candidates = set()
    reason = "Mac 路径不在已确认的两个项目目录下"
    if label in roots and parts:
        reason = "找不到与 Mac 相同的相对路径"
        for parent in name_index[folded_project_name(parts[0])]:
            alternative = existing_project(parent.parent, (parent.name, *parts[1:]))
            if alternative:
                candidates.add(str(alternative))
        parent_parts = parts
        if ".claude" in parts and "worktrees" in parts:
            reason = "原 Claude 工作树目录缺失"
            parent_parts = parts[:parts.index(".claude")]
        else:
            parent_parts = parts[:-1]
        if parent_parts:
            parent = existing_project(roots[label], parent_parts)
            if parent:
                candidates.add(str(parent))
    elif "/.codex/worktrees/" in cwd:
        reason = "Mac Codex 工作树，需要确认对应的 Windows 项目"
        candidates.update(str(path) for path in name_index[folded_project_name(PurePosixPath(cwd).name)])
    elif cwd.endswith("/deepseek-harness/default-workspace") and deepseek_default:
        reason = "Mac DSH 默认工作区，候选为本机现用默认工作区"
        if Path(deepseek_default).is_dir():
            candidates.add(deepseek_default)
    elif not cwd:
        reason = "备份没有记录工作目录"
    return reason, sorted(candidates)


def target_path_conflicts(records, mappings):
    from ai_session_migration.importer import dsh_project_key

    groups = defaultdict(dict)
    for record in records:
        target = mappings.get(record.cwd)
        if not target:
            continue
        if record.provider == "claude":
            key = re.sub(r"[^A-Za-z0-9]", "-", target).casefold()
        elif record.provider == "deepseek":
            key = dsh_project_key(target).casefold()
        else:
            key = record.metadata.get("native_thread_metadata", {}).get("project_id")
        if key:
            groups[record.provider, key][Path(target)] = target
    return [
        {"provider": provider, "key": key, "windowsPaths": sorted(paths.values())}
        for (provider, key), paths in sorted(groups.items()) if len(paths) > 1
    ]


def preview(package, records, project_roots, cache, manual_mappings=None):
    from ai_session_migration.importer import checked_directory
    from ..migration_policy import NATIVE_PROVIDERS

    records = [record for record in records if record.provider in NATIVE_PROVIDERS]

    roots = {
        label: Path(value).expanduser().resolve()
        for label, value in zip(("prive", "work"), project_roots, strict=True)
    }
    for root in roots.values():
        if not root.is_dir():
            raise ValueError(f"找不到 Windows 项目根目录 {root}")
    ids, targets, deepseek_default = live_session_ids(cache)
    by_cwd = defaultdict(Counter)
    main_by_cwd = Counter()
    mappings = {}
    for record in records:
        by_cwd[record.cwd][record.provider] += 1
        main_by_cwd[record.cwd] += not record.child
    name_index = defaultdict(set)
    for root in roots.values():
        for path in root.iterdir():
            if path.is_dir():
                name_index[folded_project_name(path.name)].add(path)
    for cwd in by_cwd:
        label, parts = source_project(cwd)
        if label in roots:
            target = existing_project(roots[label], parts)
            if target:
                mappings[cwd] = str(target)
                name_index[folded_project_name(target.name)].add(target)
    for cwd, value in (manual_mappings or {}).items():
        target = checked_directory(Path(value))
        if cwd not in by_cwd or not target.is_dir() or not any(target.is_relative_to(root) for root in roots.values()):
            raise ValueError(f"自定义映射必须对应备份中的记录和已确认根目录下的现有文件夹：{target}")
        mappings[cwd] = str(target)

    pending = []
    for cwd, counts in by_cwd.items():
        if cwd not in mappings:
            reason, candidates = pending_project(cwd, roots, name_index, deepseek_default)
            pending.append({
                "macCwd": cwd, "reason": reason, "candidates": candidates,
                "recordsByProvider": counts, "mainRecords": main_by_cwd[cwd],
            })
    pending.sort(key=lambda p: (-sum(p["recordsByProvider"].values()), p["macCwd"]))

    entries = index_entries(package)
    counts = defaultdict(Counter)
    conflicts = []
    first_batch, existing_samples, needs_sample = {}, {}, set()
    for record in records:
        p = record.provider
        stats = counts[p]
        stats["records"] += 1
        stats["mainRecords"] += not record.child
        mapped = record.cwd in mappings
        stats["directMappedRecords"] += mapped
        stats["directMappedMainRecords"] += mapped and not record.child
        stats["pendingRecords"] += not mapped
        duplicate = record.session_id in ids[p]
        stats["sameIdConflicts"] += duplicate
        if duplicate:
            conflicts.append({"provider": p, "sessionId": record.session_id, "action": "skip"})
        if not mapped:
            continue
        item_entries = entries[record.uid]
        logs = [entry for entry in item_entries if entry["role"] == "original-session"]
        events = next(e["events"] for e in item_entries if e["role"] == "lossless-history-export")
        if not duplicate and (events or logs):
            needs_sample.add(p)
        # Prefer a recent main session with one clear original log for the first native trial.
        if record.archived or record.child or len(logs) != 1 or not events or record.metadata.get("parent_id"):
            continue
        samples = existing_samples if duplicate else first_batch
        samples.setdefault(p, {
            "sessionId": record.session_id, "macCwd": record.cwd,
            "windowsCwd": mappings[record.cwd], "exportedEvents": events,
            "existing": duplicate,
        })
    for p in needs_sample - first_batch.keys():
        if p in existing_samples:
            first_batch[p] = existing_samples[p]

    return {
        "schema": "asm-windows-project-preview-1",
        "scope": "目录与同 ID 的只读对照，正式写入前再用原生导入引擎复核",
        "archive": str(package.archive),
        "catalogSha256": package.manifest["catalogSha256"],
        "projectRoots": {label: str(root) for label, root in roots.items()},
        "summary": counts,
        "targets": {
            p: {"directory": str(path), "existingSessionIds": len(ids[p])}
            for p, path in targets.items() if p in counts
        },
        "firstBatch": first_batch,
        "needsSample": sorted(needs_sample - first_batch.keys()),
        "sameIdConflicts": conflicts,
        "targetPathConflicts": target_path_conflicts(records, mappings),
        "mappings": dict(sorted(mappings.items())),
        "pendingPaths": pending,
    }


def create_targets(run, provider, source):
    from ai_session_migration.importer import codex_databases
    from ai_session_migration.safeio import SnapshotStore, stable_bytes

    directories = {"codex": ".codex", "claude": ".claude", "deepseek": ".dsh"}
    target = run / directories[provider]
    target.mkdir(parents=True)
    if provider == "codex":
        snapshots = SnapshotStore(run / "snapshots")
        for path in codex_databases(source):
            if path is None:
                continue
            snapshot = snapshots.sqlite(path)
            with closing(sqlite3.connect(snapshot)) as original, closing(sqlite3.connect(target / path.name)) as dest:
                original.backup(dest)
    elif provider == "claude":
        (target / "projects").mkdir()
    elif provider == "deepseek":
        registry = Path("storages/workspace.json")
        if (source / registry).is_file():
            (target / registry.parent).mkdir()
            (target / registry).write_bytes(stable_bytes(source / registry))
    return {provider: str(target)}


def selected_package(package, selected, destination, claude_log_owners, progress=lambda *_: None):
    """Reuse the archived objects and checksums for the selected sessions."""
    selected_ids = {r.uid for r in selected}
    included = {}
    entries = index_entries(package)
    for record in selected:
        for entry in entries[record.uid]:
            owner = claude_log_owners.get(entry.get("originalPath"))
            if owner and entry["role"] in {"original-session", "attachment-or-tool-result"}:
                if owner in selected_ids:
                    included[entry["object"]] = dict(
                        entry, role="original-session", sessionUid=owner, sessionUids=[owner]
                    )
            else:
                included[entry["object"]] = entry

    catalog = package.package_catalog
    catalog_bytes = json.dumps({
        **catalog,
        "records": [r for r in catalog["records"] if r["uid"] in selected_ids],
        "histories": {uid: name for uid, name in catalog["histories"].items() if uid in selected_ids},
        "registries": [],
    }, ensure_ascii=False).encode("utf-8")
    manifest = dict(
        package.manifest, entries=list(included.values()),
        catalogSha256=hashlib.sha256(catalog_bytes).hexdigest(),
    )
    with zipfile.ZipFile(package.archive) as source, zipfile.ZipFile(
        destination, "x", zipfile.ZIP_DEFLATED, compresslevel=1,
    ) as output:
        step = f"准备批次包（{len(included)} 个对象及索引）"
        progress(step, 0)
        for count, name in enumerate(included, 1):
            with source.open(name) as original, output.open(name, "w", force_zip64=True) as copy:
                shutil.copyfileobj(original, copy, length=1024 * 1024)
            progress(step, 100 * count // (len(included) + 2))
        output.writestr("catalog.json", catalog_bytes)
        output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    progress(step, 100)

"""Plan and run the approved mappings after the three native pilot checks pass."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import zipfile


BATCH_BYTES = 768 * 1024 * 1024
BATCH_RECORDS = 256
CODEX_BATCH_BYTES = 2 * BATCH_BYTES
CODEX_BATCH_RECORDS = 2 * BATCH_RECORDS


def parent_id(record, header=None):
    from ai_session_migration.importer import relative_original

    if record.provider == "claude" and record.child:
        rel = relative_original(record.metadata["primary_file"], record.root)
        return rel.parts[2] if len(rel.parts) >= 5 and rel.parts[3] == "subagents" else None
    if record.provider == "deepseek":
        return header.get("parentSession") if header else None
    if record.provider != "codex":
        return None
    if record.metadata.get("parent_id"):
        return record.metadata["parent_id"]
    source = record.metadata.get("native_thread_metadata", {}).get("source", "")
    if isinstance(source, str) and source.startswith("{"):
        subagent = json.loads(source).get("subagent")
        if isinstance(subagent, dict):
            return subagent.get("thread_spawn", {}).get("parent_thread_id")
    return None


def batch_groups(draft, archive, records, existing):
    from . import planning as probe
    from ai_session_migration.importer import checked_directory

    mappings = draft["mappings"]
    roots = tuple(Path(p).resolve() for p in draft["projectRoots"].values())
    for value in set(mappings.values()):
        target = checked_directory(Path(value))
        if not target.is_dir() or not any(target.is_relative_to(root) for root in roots):
            raise ValueError(f"已批准的项目目录无法验证：{target}")

    from ..migration_policy import NATIVE_PROVIDERS
    selected = {(r.provider, r.session_id): r for r in records if r.provider in NATIVE_PROVIDERS and r.cwd in mappings}
    if len(selected) != sum(row["directMappedRecords"] for row in draft["summary"].values()):
        raise ValueError("已映射的会话集合与预览不符")
    entries = probe.index_entries(archive)
    sections, parents, weights, results = {}, {}, {}, {}
    with zipfile.ZipFile(archive.archive) as source:
        for key, record in selected.items():
            if record.session_id in existing[record.provider]:
                results[record.uid] = {"provider": record.provider, "status": "existing"}
            payload = entries[record.uid]
            history = next(e for e in payload if e["role"] == "lossless-history-export")
            has_original = any(e["role"] == "original-session" for e in payload)
            if record.uid not in results and not history["events"] and not has_original:
                results[record.uid] = {"provider": record.provider, "status": "no-saved-history"}
            header = None
            if record.provider == "deepseek" and history["events"]:
                with source.open(history["object"]) as stream:
                    header = json.loads(stream.readline())
            parent = parent_id(record, header)
            parents[key] = (record.provider, parent) if parent else None
            weights[record.uid] = sum(e["size"] for e in payload)

    for key, record in selected.items():
        visited = {key}
        head = key
        while ancestor := parents.get(head):
            if ancestor in visited:
                raise ValueError(f"原会话包父子关系循环：{record.provider}/{record.session_id}")
            if ancestor not in selected or results.get(selected[ancestor].uid, {}).get("status") == "no-saved-history":
                if ancestor[1] not in existing[record.provider] and record.uid not in results:
                    results[record.uid] = {
                        "provider": record.provider, "status": "parent-unavailable", "parentId": ancestor[1],
                    }
                break
            visited.add(ancestor)
            head = ancestor
        if record.uid not in results:
            family = selected[head]
            sections[key] = (mappings[family.cwd].casefold(), head[1], len(visited), record.session_id)

    groups = {}
    for provider in draft["targets"]:
        byte_limit = CODEX_BATCH_BYTES if provider == "codex" else BATCH_BYTES
        record_limit = CODEX_BATCH_RECORDS if provider == "codex" else BATCH_RECORDS
        batches, current, size = [], [], 0
        for key in sorted((k for k in sections if k[0] == provider), key=sections.get):
            record = selected[key]
            weight = weights[record.uid]
            if current and (size + weight > byte_limit or len(current) >= record_limit):
                batches.append(current)
                current, size = [], 0
            current.append(record)
            size += weight
        if current:
            batches.append(current)
        groups[provider] = batches
    return groups, parents, results


def data_failure(error):
    if isinstance(error, (sqlite3.IntegrityError, json.JSONDecodeError, FileNotFoundError)):
        return True
    return isinstance(error, ValueError) and str(error).startswith((
        "原始日志", "目标已存在不同的文件", "目标日志已存在", "暂存原生",
        "暂存项目", "暂存 thinking", "Codex 分页偏移", "Codex 暂存数据库",
        "DeepSeek 原生", "DeepSeek 缺少", "DeepSeek 同一代际",
        "Claude 原始", "Codex 日志与会话",
    ))


def execute_bulk(draft, archive, state, run, owners, progress, save):
    from ai_session_migration.importer import build_plan, checked_directory, execute_import, native_processes
    from ai_session_migration.packages import PackageLibrary
    from . import planning as probe

    roots = {p: checked_directory(Path(target["directory"])) for p, target in draft["targets"].items()}
    for provider, root in roots.items():
        if any(other != provider and root.is_relative_to(path) for other, path in roots.items()):
            raise ValueError("不同客户端的目标资料目录不能重叠")
    records = archive.scan().records
    by_id = {(r.provider, r.session_id): r for r in records}
    existing, current_targets, _ = probe.live_session_ids(run / "cache")
    if any(root != current_targets[provider] for provider, root in roots.items()):
        raise ValueError("Windows 客户端资料目录与预览不一致")
    groups, parents, outcomes = batch_groups(draft, archive, records, existing)
    if any(group and not state["pilot"].get(p, {}).get("nativeVerified") for p, group in groups.items()):
        raise ValueError("有待写入的客户端须先通过正式库样本回读")
    bulk = state.setdefault("bulk", {"batches": [], "outcomes": {}})
    known = bulk["outcomes"]
    for record in by_id.values():
        if record.session_id not in existing[record.provider] and known.get(record.uid, {}).get("status") in {"imported", "existing"}:
            known.pop(record.uid)
    for uid, outcome in outcomes.items():
        if not (outcome["status"] == "existing" and known.get(uid, {}).get("status") == "imported"):
            known[uid] = outcome
    portable_root = run / "batches"
    portable_root.mkdir(exist_ok=True)
    state_lock = threading.Lock()
    totals = {provider: row["directMappedRecords"] for provider, row in draft["summary"].items()}
    bulk["providers"] = {}

    def persist():
        summary = {p: Counter() for p in groups}
        for outcome in known.values():
            summary[outcome["provider"]][outcome["status"]] += 1
        bulk["summary"] = {p: dict(counts) for p, counts in summary.items()}
        save(state)

    def provider_status(provider, **entry):
        with state_lock:
            bulk["providers"][provider] = entry
            persist()

    def show_count(provider):
        counts = bulk["summary"][provider]
        present = counts.get("imported", 0) + counts.get("existing", 0)
        total = totals[provider]
        print(f"{provider} · 已入库或同 ID 保留 {present}/{total} 条（{present / max(1, total):.1%}）", flush=True)

    persist()

    def perform(provider, group):
        targets = {provider: str(roots[provider])}
        # Keep the existing Codex extraction cache for resume. Other providers have
        # separate caches so that independent ZIP subsets never share a scratch file.
        cache = run / "cache"
        if provider != "codex":
            cache /= provider

        def stage_progress(step, percent):
            progress(f"{provider} · {step}", percent)

        live = native_processes(targets)
        if live:
            provider_status(provider, status="waiting-for-exit", activeProcesses=live)
            return False
        batch_id = hashlib.sha256("\n".join(record.uid for record in group).encode()).hexdigest()[:16]
        destination = portable_root / (batch_id + ".zip")
        if not destination.exists():
            scratch = portable_root / (batch_id + "-" + os.urandom(4).hex() + ".part")
            try:
                probe.selected_package(archive, group, scratch, owners, stage_progress)
                os.rename(scratch, destination)
            finally:
                scratch.unlink(missing_ok=True)
        package = PackageLibrary(destination, cache)
        included = {r.uid: r for r in package.scan().records}
        plan = build_plan(package, [included[r.uid] for r in group], targets, draft["mappings"], cache, stage_progress,
                          codex_sessions=provider == "codex")
        for item in plan.ready:
            key = (provider, item["record"]["session_id"])
            if provider == "claude" and (parent := parents[key]):
                parent_record = by_id.get(parent)
                if parent_record and parent_record.cwd in draft["mappings"]:
                    item["claude_project_cwd"] = draft["mappings"][parent_record.cwd]
        report = None
        if plan.ready:
            try:
                report = execute_import(plan, run / "backups", cache, confirmed_offline=True, progress=stage_progress)
            except Exception as error:
                record = getattr(error, "report", None)
                if record and (record.get("rollbackVerified") or record.get("targetUnmodifiedByImport")) and data_failure(error):
                    if len(group) > 1:
                        middle = len(group) // 2
                        return perform(provider, group[:middle]) and perform(provider, group[middle:])
                    uid = group[0].uid
                    with state_lock:
                        known[uid] = {"provider": provider, "status": "data-issue", "reason": str(error),
                                      "journal": record["journal"]}
                        persist()
                    return True
                entry = {"status": "error", "reason": str(error)}
                if record:
                    entry["journal"] = record["journal"]
                provider_status(provider, **entry)
                print(f"{provider} · {error}", flush=True)
                return False
        with state_lock:
            for item in plan.items:
                status = {"import": "imported", "skip": "existing", "blocked": "blocked"}[item["action"]]
                outcome = {"provider": provider, "status": status}
                if status == "blocked":
                    outcome["reason"] = item["reason"]
                known[item["record"]["uid"]] = outcome
            if report:
                bulk["batches"].append({"provider": provider, "imported": report["imported"], "journal": report["journal"]})
            persist()
            show_count(provider)
        return True

    def process(provider):
        provider_groups = groups[provider]
        try:
            for index, group in enumerate(provider_groups, 1):
                provider_status(provider, status="running", batch=index, plannedBatches=len(provider_groups))
                print(f"{provider} · 待恢复批次 {index}/{len(provider_groups)}，{len(group)} 条", flush=True)
                if not perform(provider, group):
                    return False
            provider_status(provider, status="processed", plannedBatches=len(provider_groups))
            if not provider_groups:
                with state_lock:
                    show_count(provider)
            return True
        except Exception as error:
            provider_status(provider, status="error", reason=f"{type(error).__name__}：{error}")
            print(f"{provider} · {error}", flush=True)
            return False

    # One writer per native library. Parent and child batches in a library still run in order.
    with ThreadPoolExecutor(max_workers=len(groups), thread_name_prefix="native-restore") as pool:
        return all(list(pool.map(process, groups)))

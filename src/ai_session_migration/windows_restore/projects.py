"""Register restored Codex chats under their existing Windows project folders."""

from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import os
import sqlite3
import time
import uuid

from .desktop import app_server, installed_desktop


def windows_path(path):
    return Path(str(path).removeprefix("\\\\?\\")).resolve()


def list_projects(request):
    projects, cursor = [], None
    while True:
        page = request("project/list", {"cursor": cursor, "limit": 100})
        projects.extend(page["data"])
        cursor = page["nextCursor"]
        if cursor is None:
            return projects


def register_sidebar(library, groups, report, assignments, run):
    from ai_session_migration.importer import native_processes
    from ai_session_migration.safeio import atomic_json, stable_bytes

    targets = {"codex": str(library)}
    if native_processes(targets):
        raise RuntimeError("请正常退出 Codex 后再登记侧栏项目")
    file = library / ".codex-global-state.json"
    if not file.is_file():
        raise FileNotFoundError("Windows Codex 还没有初始化侧栏")
    original = stable_bytes(file)
    state = json.loads(original)
    projects = state.setdefault("local-projects", {})
    order = state.setdefault("project-order", [])
    membership = state.setdefault("thread-project-assignments", {})
    hosts = state.setdefault("thread-project-membership-host-ids", {})
    aliases = state.setdefault("app-server-project-id-by-legacy-project-id-by-host", {})
    local_aliases = aliases.setdefault("local:" + str(library), {})
    existing = {
        windows_path(data["rootPaths"][0]): key
        for key, data in projects.items() if len(data.get("rootPaths", [])) == 1
    }
    now = int(time.time() * 1000)
    counts = {"projects": 0, "threads": 0}
    for row in report["roots"]:
        root = Path(row["path"])
        native_id = row["projectId"]
        key = existing.get(windows_path(root), native_id)
        if key not in projects:
            projects[key] = {"id": key, "name": root.name, "rootPaths": [str(root)],
                             "createdAt": now, "updatedAt": now}
        if key not in order:
            order.append(key)
        previous = local_aliases.setdefault(key, native_id)
        if previous != native_id:
            raise ValueError(f"Codex 侧栏已经为此项目登记了不同的项目 ID：{root}")
        counts["projects"] += 1
        for sid in groups[root]:
            if assignments[sid] != native_id:
                continue
            assigned = membership.setdefault(sid, {"projectKind": "local", "projectId": key})
            if assigned == {"projectKind": "local", "projectId": key}:
                hosts.setdefault(sid, "local")
                counts["threads"] += 1

    if state != json.loads(original):
        backup = run / "backups" / ("Codex-sidebar-" + uuid.uuid4().hex[:12])
        backup.mkdir(parents=True)
        saved = backup / file.name
        saved.write_bytes(original)
        if saved.read_bytes() != original or stable_bytes(file) != original or native_processes(targets):
            raise RuntimeError("Codex 侧栏在登记前发生变化，已保留原资料")
        atomic_json(file, state)
        if json.loads(stable_bytes(file)) != state:
            raise RuntimeError("Codex 侧栏回读失败")
        counts["backup"] = str(saved)
    return counts


def restore_projects(draft, archive, state, run, *, sidebar=False):
    from ai_session_migration.importer import checked_directory, codex_databases
    from ai_session_migration.safeio import SnapshotStore, atomic_json

    library = checked_directory(Path(draft["targets"]["codex"]["directory"]))
    approved = tuple(Path(root).resolve() for root in draft["projectRoots"].values())
    groups = defaultdict(list)
    outcomes = state["bulk"]["outcomes"]
    pilot = state["pilot"].get("codex", {})
    pilot_id = draft["firstBatch"].get("codex", {}).get("sessionId") if pilot.get("imported") else None
    for record in archive.scan().records:
        if record.provider != "codex":
            continue
        if outcomes.get(record.uid, {}).get("status") != "imported" and record.session_id != pilot_id:
            continue
        root = Path(draft["mappings"][record.cwd])
        groups[root].append(record.session_id)
    if not groups:
        return {"status": "verified", "projectCount": 0, "threads": 0}
    native_db, _ = codex_databases(library)
    for root in groups:
        if not checked_directory(root).is_dir() or not any(root.is_relative_to(parent) for parent in approved):
            raise ValueError(f"Codex 项目目录无法核对：{root}")

    snapshots = SnapshotStore(run / "cache" / "codex-projects")
    initial = snapshots.sqlite(native_db)
    with closing(sqlite3.connect(initial)) as db:
        assignments = {sid: (cwd, project) for sid, cwd, project in db.execute(
            "SELECT id, cwd, project_id FROM threads"
        )}
    for root, ids in groups.items():
        if any(sid not in assignments or windows_path(assignments[sid][0]) != windows_path(root) for sid in ids):
            raise ValueError(f"Codex 项目包含工作目录不同或索引缺失的会话：{root}")

    journal = run / "codex-projects.json"
    env = dict(os.environ, CODEX_HOME=str(library))
    with app_server(installed_desktop().cli, env, run, run / "codex-projects.log", "migration_projects") as request:
        previous = list_projects(request)
        by_root = {
            windows_path(project["roots"][0]["path"]): project
            for project in previous if len(project["roots"]) == 1
        }
        changed = any(
            windows_path(root) not in by_root or any(not assignments[sid][1] for sid in ids)
            for root, ids in groups.items()
        )
        report = {"status": "planning", "roots": [], "preservedOtherAssignments": 0}
        if not changed and journal.is_file():
            previous_report = json.loads(journal.read_bytes())
            if (previous_report.get("catalogSha256") == draft["catalogSha256"]
                    and previous_report.get("databaseBackup")):
                report["databaseBackup"] = previous_report["databaseBackup"]
        if changed:
            checkpoint = run / "backups" / ("Codex-projects-" + uuid.uuid4().hex[:12])
            checkpoint.mkdir(parents=True)
            saved = checkpoint / native_db.name
            with closing(sqlite3.connect(initial)) as source, closing(sqlite3.connect(saved)) as backup:
                source.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("Codex 项目索引备份未通过校验")
            report["databaseBackup"] = str(saved)
            journal = checkpoint / journal.name
        atomic_json(journal, report)
        expected = {}
        for root, ids in sorted(groups.items()):
            existing = by_root.get(windows_path(root))
            unassigned = [sid for sid in ids if not assignments[sid][1]]
            if existing is None:
                key = str(uuid.uuid5(uuid.NAMESPACE_URL, draft["catalogSha256"] + "|" + str(root).casefold()))
                project = request("project/import", {
                    "name": root.name, "roots": [{"path": str(root)}],
                    "threads": unassigned, "idempotencyKey": key,
                })["project"]
            else:
                project = existing
                for sid in unassigned:
                    request("thread/metadata/update", {"threadId": sid, "projectId": project["id"]})
            count = 0
            for sid in ids:
                old = assignments[sid][1]
                expected[sid] = old or project["id"]
                if expected[sid] == project["id"]:
                    count += 1
            report["preservedOtherAssignments"] += len(ids) - count
            report["roots"].append({"path": str(root), "projectId": project["id"], "threads": count, "created": existing is None})
            report["status"] = "registering"
            atomic_json(journal, report)

        actual = {p["id"]: p for p in list_projects(request)}
        if any(row["projectId"] not in actual or windows_path(actual[row["projectId"]]["roots"][0]["path"]) != windows_path(row["path"])
               for row in report["roots"]):
            raise RuntimeError("Codex 项目列表回读失败")

    with closing(sqlite3.connect(snapshots.sqlite(native_db))) as db:
        verified = dict(db.execute("SELECT id, project_id FROM threads"))
    if any(verified.get(sid) != project for sid, project in expected.items()):
        raise RuntimeError("Codex 项目与会话归属回读失败")
    report.update(status="verified", catalogSha256=draft["catalogSha256"], projectCount=len(groups),
                  threads=sum(row["threads"] for row in report["roots"]), journal=str(journal))
    if sidebar:
        report["sidebar"] = register_sidebar(library, groups, report, verified, run)
    atomic_json(journal, report)
    if journal != run / "codex-projects.json":
        atomic_json(run / "codex-projects.json", report)
    print(f"Codex 已核对 {len(groups)} 个项目和 {report['threads']} 条会话归属", flush=True)
    return report

"""Preview, run and verify a native Windows session restore."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import planning as probe


from ..migration_policy import CLAUDE_READ_ONLY, NATIVE_PROVIDERS, require_native_targets

ORDER = ("deepseek", "codex")
CLAUDE_ACCOUNT_POLICY = "mac-account-metadata"
CODEX_ACCOUNT_POLICY = "session-header-and-index"
_progress_state = threading.local()
_progress_lock = threading.Lock()


def progress(step, percent):
    checkpoint = step, percent // 25
    if getattr(_progress_state, "checkpoint", None) != checkpoint:
        with _progress_lock:
            print(f"{step} {percent}%", flush=True)
        _progress_state.checkpoint = checkpoint


def read_preview(path, archive):
    draft = json.loads(path.read_bytes())
    if (draft["catalogSha256"] != archive.manifest["catalogSha256"]
            or Path(draft["archive"]).resolve() != archive.archive):
        raise ValueError("当前会话包和已批准预览不一致")
    return draft


def read_state(draft, run):
    signature = hashlib.sha256(json.dumps(
        {key: draft[key] for key in ("catalogSha256", "firstBatch", "mappings", "targets")},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    path = run / "progress.json"
    if path.is_file():
        data = json.loads(path.read_bytes())
        if data["catalogSha256"] != draft["catalogSha256"] or data["firstBatch"] != draft["firstBatch"]:
            raise ValueError("此恢复进度与预览中的会话选择不符")
        if data.get("planSha256", signature) != signature:
            raise ValueError("已有恢复进度的项目映射发生变化，请为新计划使用独立恢复目录")
        return data
    return {"catalogSha256": draft["catalogSha256"], "firstBatch": draft["firstBatch"],
            "planSha256": signature, "pilot": {}}


def write_state(run, state):
    from ai_session_migration.safeio import atomic_json

    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    atomic_json(run / "progress.json", state)


def claude_logs(records):
    from ai_session_migration.importer import relative_original

    owners = {}
    for record in records:
        if record.provider != "claude":
            continue
        for path in record.files:
            rel = relative_original(path, record.root)
            if rel.parts[0] == "projects" and rel.name == record.session_id + ".jsonl":
                owners[path] = record.uid
    return owners


def pilot_packages(draft, archive, state, run):
    from ai_session_migration.importer import build_plan, execute_import
    from ai_session_migration.packages import PackageLibrary
    from .audit import inspect_transcript

    records = {(r.provider, r.session_id): r for r in archive.scan().records}
    claude_log_owners = claude_logs(records.values())
    for p in ORDER:
        sample = draft["firstBatch"].get(p)
        if not sample:
            continue
        r = records[p, sample["sessionId"]]
        if r.cwd != sample["macCwd"] or sample["windowsCwd"] != draft["mappings"].get(r.cwd):
            raise ValueError(f"{p} 的项目映射与预览不符")
        entry = state["pilot"].get(p, {})
        if entry.get("nativeVerified") or entry.get("stagedReadback") or entry.get("existingSample"):
            continue
        if sample.get("existing"):
            state["pilot"][p] = {"existingSample": True}
            write_state(run, state)
            continue
        folder = run / "pilot" / p
        folder.mkdir(parents=True, exist_ok=True)
        portable = folder / "selected.zip"
        if not portable.exists():
            scratch = folder / (uuid.uuid4().hex[:8] + ".part")
            try:
                probe.selected_package(archive, [r], scratch, claude_log_owners, progress)
                os.rename(scratch, portable)
            finally:
                scratch.unlink(missing_ok=True)
        package = PackageLibrary(portable, run / "cache")
        records_for_import = package.scan().records
        if [x.uid for x in records_for_import] != [r.uid]:
            raise ValueError(f"{p} 的首批恢复包与预览不符")
        print(f"{p} 首批在副本中校验", flush=True)
        target = Path(draft["targets"][p]["directory"])
        # Keep the temporary path short enough for Windows tool-result filenames.
        parent = target.parent.resolve()
        with tempfile.TemporaryDirectory(dir=parent, prefix=".asm-test-") as temporary:
            stage_home = Path(temporary).resolve()
            if stage_home.parent != parent:
                raise ValueError("首批暂存目录超出目标客户端父目录")
            targets = probe.create_targets(stage_home, p, target)
            plan = build_plan(package, records_for_import, targets, draft["mappings"], run / "cache", progress,
                              codex_sessions=p == "codex")
            if len(plan.ready) != 1:
                raise ValueError(f"{p} 样本无法导入副本：{plan.items[0]['reason']}")
            report = execute_import(plan, folder / "rehearsal-backups", run / "cache",
                                    confirmed_offline=True, progress=progress)
            if p == "claude":
                logs = list((Path(targets[p]) / "projects").glob("**/*.jsonl"))
                if len(logs) != 1 or logs[0].stem != r.session_id:
                    raise ValueError("Claude 首批副本出现额外的会话日志")
                result = inspect_transcript(logs[0], logs[0].stat().st_size)
                if result["accountFields"] or result["invalidLines"]:
                    raise ValueError("Claude 首批副本的账号字段验收失败")
        state["pilot"][p] = {
            "stagedReadback": bool(report["targetReadbackVerified"]),
            "scopeChecked": True,
            "source": str(portable), "sourceSha256": plan.archive_sha256,
            "rehearsalJournal": report["journal"],
        }
        if p == "codex":
            state["pilot"][p]["accountPolicy"] = CODEX_ACCOUNT_POLICY
        elif p == "claude":
            state["pilot"][p]["accountPolicy"] = CLAUDE_ACCOUNT_POLICY
        write_state(run, state)


def child_environment(variable, directory):
    env = os.environ.copy()
    env[variable] = str(directory)
    return env


def run_native(args, env, *, stdin="", cwd=None, timeout=60):
    return subprocess.run(
        [str(arg) for arg in args], env=env, cwd=cwd, input=stdin, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def verify_claude(target, session, cwd):
    desktop = Path.home() / "AppData" / "Local" / "Claude-Data"
    binaries = list(desktop.glob("*/claude-code/*/*/claude.exe"))
    exe = shutil.which("claude.exe") or (max(binaries, key=lambda path: tuple(map(int, path.parent.parent.name.split(".")))) if binaries else None)
    if not exe:
        raise FileNotFoundError("找不到本机 Claude Code")
    env = child_environment("CLAUDE_CONFIG_DIR", target)
    env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
    init = {"type": "control_request", "request_id": "migration", "request": {"subtype": "initialize"}}
    flags = ["--setting-sources=", "--print", "--bare", "--input-format", "stream-json",
             "--output-format", "stream-json", "--verbose", "--resume", session]
    result = run_native([exe, *flags], env, stdin=json.dumps(init) + "\n", cwd=cwd)
    success = any(
        value.get("type") == "control_response" and value.get("response", {}).get("subtype") == "success"
        for value in map(json.loads, result.stdout.splitlines()) if isinstance(value, dict)
    )
    if result.returncode or not success:
        raise RuntimeError(f"Claude Code 未能按原会话 ID 加载，退出码 {result.returncode}")
    return {"resumed": True, "sessionId": session}


DSH_READBACK = r"""
import {dirname, join} from 'node:path';
import {pathToFileURL} from 'node:url';
const folder = join(dirname(process.execPath), 'resources', 'app.asar', 'dsh', 'node_modules', '@deepseek-ai');
const load = name => import(pathToFileURL(join(folder, name, 'lib', 'index.js')).href);
const {Context} = await load('cordis');
const persistence = await load('dsh-session-persistence-jsonl');
const ctx = new Context();
const scope = ctx.plugin(persistence, {root: join(process.env.DSH_HOME, 'sessions'), compression: 'zstd'});
await scope.await();
try {
    const service = ctx.sessionPersistence;
    const id = process.env.ASM_VERIFY_SESSION_ID;
    const listed = (await service.list()).some(row => (row.id ?? row.header?.id) === id);
    const handle = await service.open(id, 'read');
    try {
        const {events} = await handle.read();
        console.log(JSON.stringify({listed, resumed: events.length > 0, events: events.length, sessionId: id}));
    } finally {
        await handle.close();
    }
} finally {
    scope.dispose();
}
"""


def verify_dsh(target, session, cwd):
    exe = Path.home() / "AppData" / "Local" / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe"
    env = child_environment("DSH_HOME", target)
    env.update(ELECTRON_RUN_AS_NODE="1", ASM_VERIFY_SESSION_ID=session)
    args = [exe, "--expose-internals", "--input-type=module", "-"]
    result = run_native(args, env, stdin=DSH_READBACK, cwd=cwd)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"DSH 无法通过原生持久化服务加载样本，退出码 {result.returncode}")
    data = json.loads(result.stdout.splitlines()[-1])
    if not data["listed"] or not data["resumed"] or data["sessionId"] != session:
        raise RuntimeError("DSH 列表或历史回读未找到样本")
    return data


def verify_codex(target, session, cwd, run):
    from .desktop import app_server, installed_desktop

    exe = installed_desktop().cli
    env = child_environment("CODEX_HOME", target)
    with app_server(exe, env, cwd, run / "codex-readback.log", "migration_readback") as request:
        cursor = None
        while True:
            listing = request("thread/list", {"useStateDbOnly": True, "limit": 100, "cursor": cursor})
            listed = any(row["id"] == session for row in listing["data"])
            cursor = listing["nextCursor"]
            if listed or not cursor:
                break
        restored = request("thread/read", {"threadId": session, "includeTurns": False})["thread"]
        turns = request("thread/turns/list", {"threadId": session, "limit": 1})
        resumed = request("thread/resume", {"threadId": session, "excludeTurns": True})["thread"]
        if not listed or restored["id"] != session or resumed["id"] != session or not turns["data"]:
            raise RuntimeError("Codex 列表、历史分页或恢复校验失败")
        return {"listed": True, "resumed": True, "historyPageRead": True, "sessionId": session}


def apply_pilot(draft, state, chosen, run):
    from ai_session_migration.importer import build_plan, execute_import, native_processes
    from ai_session_migration.packages import PackageLibrary

    checks = {"claude": verify_claude, "deepseek": verify_dsh,
              "codex": lambda target, sid, cwd: verify_codex(target, sid, cwd, run)}
    for p in chosen:
        entry = state["pilot"][p]
        previous = dict(entry)
        if entry.get("nativeVerified"):
            continue
        existing = entry.get("existingSample")
        if not existing and not (entry.get("stagedReadback") and entry.get("scopeChecked")):
            raise ValueError(f"{p} 样本未完成副本预验")
        policy = {"claude": CLAUDE_ACCOUNT_POLICY, "codex": CODEX_ACCOUNT_POLICY}.get(p)
        if not existing and policy and entry.get("accountPolicy") != policy:
            raise ValueError(f"{p} 样本需先通过账号归属字段检查")
        sample = draft["firstBatch"][p]
        target = draft["targets"][p]["directory"]
        targets = {p: target}
        try:
            live = native_processes(targets)
            if live:
                entry.update(status="waiting-for-exit", activeProcesses=live)
                if entry != previous:
                    print(f"{p} 等待退出：{', '.join(live)}", flush=True)
                continue
            entry.pop("activeProcesses", None)
            if existing:
                entry.update(status="existing-id", imported=0)
            else:
                package = PackageLibrary(Path(entry["source"]), run / "cache")
                plan = build_plan(package, package.scan().records, targets, draft["mappings"], run / "cache", progress,
                                  codex_sessions=p == "codex")
                if plan.archive_sha256 != entry["sourceSha256"]:
                    raise ValueError("首批恢复包在预验后发生变化")
                if len(plan.ready) == 1:
                    print(f"{p} 开始备份和导入正式库", flush=True)
                    report = execute_import(plan, run / "backups", run / "cache",
                                            confirmed_offline=True, progress=progress)
                    entry.update(status="imported", journal=report["journal"], imported=report["imported"])
                    write_state(run, state)
                elif len(plan.items) != 1 or plan.items[0]["action"] != "skip":
                    raise ValueError(plan.items[0]["reason"])
                elif not entry.get("imported"):
                    entry.update(status="existing-id", imported=0)
            entry["nativeReadback"] = checks[p](target, sample["sessionId"], sample["windowsCwd"])
            entry.update(status="verified", nativeVerified=True)
            entry.pop("error", None)
            print(f"{p} 正式库原生回读通过", flush=True)
        except Exception as error:
            entry.update(status="error", error=f"{type(error).__name__}: {error}")
            if getattr(error, "report", None):
                entry["journal"] = error.report["journal"]
                entry["rollbackVerified"] = error.report.get("rollbackVerified", False)
            print(f"{p} {entry['error']}", flush=True)
        finally:
            if entry != previous:
                write_state(run, state)


def wait_for_pilot(draft, state, chosen, wait_seconds, run):
    deadline = time.monotonic() + wait_seconds
    pending = chosen
    while pending:
        apply_pilot(draft, state, pending, run)
        pending = [p for p in pending if state["pilot"][p].get("status") == "waiting-for-exit"]
        left = deadline - time.monotonic()
        if left <= 0 or not pending:
            return
        time.sleep(min(5, left))


def verify_mapped_ids(draft, archive, state, run):
    outcomes = state["bulk"]["outcomes"]
    selected = {record.uid: record for record in archive.scan().records
                if record.provider in NATIVE_PROVIDERS and record.cwd in draft["mappings"]}
    if selected.keys() != outcomes.keys():
        raise ValueError("正式恢复结果未覆盖已确认映射的会话集合")
    live, _, _ = probe.live_session_ids(run / "cache")
    verified = dict.fromkeys(draft["targets"], 0)
    for uid, record in selected.items():
        if outcomes[uid]["status"] in {"imported", "existing"}:
            if record.session_id not in live[record.provider]:
                raise ValueError(f"正式库未找到已记录入库的 {record.provider} 会话 {record.session_id}")
            verified[record.provider] += 1
    state["bulk"]["verifiedNativeIds"] = verified
    print("正式库会话 ID 复核通过  " + "、".join(f"{p} {n} 条" for p, n in verified.items()), flush=True)


def run_migration(draft, archive, state, wait_seconds, run, *, sidebar=False):
    require_native_targets(draft["targets"])
    from ai_session_migration.safeio import atomic_json
    from .audit import audit_has_issues, audit_installed
    from .batches import execute_bulk
    from .projects import restore_projects

    chosen = [provider for provider in ORDER if provider in draft["firstBatch"]]
    try:
        state["phase"] = "pilot"
        state.pop("error", None)
        write_state(run, state)
        wait_for_pilot(draft, state, chosen, wait_seconds, run)
        if not all(state["pilot"][p].get("nativeVerified") for p in chosen):
            return
        state["phase"] = "bulk"
        write_state(run, state)
        finished = execute_bulk(
            draft, archive, state, run, claude_logs(archive.scan().records), progress,
            lambda saved: write_state(run, saved),
        )
        if finished:
            verify_mapped_ids(draft, archive, state, run)
            audit = audit_installed(draft, archive, state, run)
            audit_path = run / "account-and-dsh-audit.json"
            atomic_json(audit_path, audit)
            state["accountAudit"] = str(audit_path)
            if audit_has_issues(audit):
                raise ValueError("会话和账号范围复核有差异，请查看 account-and-dsh-audit.json")
            print(f"会话和账号范围复核通过 {audit_path}", flush=True)
        state["phase"] = "mapped-processed" if finished else "bulk-pending"
        if finished and "codex" in draft["targets"]:
            state["codexProjects"] = restore_projects(draft, archive, state, run, sidebar=sidebar)
    except Exception as error:
        state.update(phase="needs-attention", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_state(run, state)


def can_reopen_codex(draft, run):
    target = Path(draft["targets"]["codex"]["directory"])
    if not target.is_dir():
        return False
    unresolved = {"publishing", "rolling-back", "needs-attention"}
    for file in (run / "backups").glob("导入恢复-*/导入结果.json"):
        report = json.loads(file.read_bytes())
        if report.get("status") in unresolved and any(
            row["provider"] == "codex" and Path(row["target"]) == target for row in report.get("roots", [])
        ):
            return False
    return True


def guide(draft, archive, state, wait_seconds, run, start_automatically=False):
    require_native_targets(draft["targets"])
    from ai_session_migration.importer import checked_directory, native_processes
    from .desktop import installed_desktop, reopen, request_quit

    print("Windows 会话恢复向导", flush=True)
    print("本次写入会话日志、附件和会话索引，启动写入前会备份 Windows 现有资料。", flush=True)
    names = {"claude": "Claude Code", "deepseek": "DeepSeek Harness", "codex": "Codex"}
    current_targets = probe.client_targets()
    if any(checked_directory(Path(target["directory"])) != current_targets[p] for p, target in draft["targets"].items()):
        raise ValueError("Windows 客户端资料目录与预览不一致")
    if draft.get("needsSample"):
        raise ValueError("这些客户端有待恢复记录，请为每种来源映射一条有原始日志的未归档主会话：" +
                         "、".join(names[p] for p in draft["needsSample"]))
    for provider, count in draft["summary"].items():
        print(f"{names[provider]} · 已匹配 {count['directMappedRecords']} 条，待确认路径 {count['pendingRecords']} 条", flush=True)
    print("请保存手头工作。需要恢复的 Claude Code 和 DSH 会话库要先退出对应客户端。", flush=True)
    print("向导会验证首批会话，分批发布到正式库，登记 Codex 项目并重新打开客户端。", flush=True)
    print(str(run / "progress.json"), flush=True)
    if not start_automatically and input("按 Enter 开始自动恢复，或输入 Q 退出 > ").strip().casefold() == "q":
        return 0
    pilot_packages(draft, archive, state, run)
    other_targets = {
        p: draft["targets"][p]["directory"] for p in ("claude", "deepseek")
        if p in draft["targets"] and draft["summary"][p]["directMappedRecords"]
    }
    if live := native_processes(other_targets):
        print("请先正常退出 " + "、".join(live) + "。", flush=True)
        deadline = time.monotonic() + (wait_seconds or 3600)
        while native_processes(other_targets):
            if time.monotonic() >= deadline:
                raise RuntimeError("等待 Claude／DSH 退出超时")
            time.sleep(4)

    desktop = None
    if draft["summary"].get("codex", {}).get("directMappedRecords"):
        desktop = installed_desktop()
        print("正在向 Codex 发送正常退出命令。", flush=True)
        quit_requested = request_quit(desktop)
        codex = {"codex": draft["targets"]["codex"]["directory"]}
        deadline = time.monotonic() + 75
        while live := native_processes(codex):
            if time.monotonic() >= deadline:
                raise RuntimeError("Codex 仍在运行，请处理退出确认或结束独立 CLI：" + "、".join(live))
            time.sleep(2)
        print("确认 Codex 已退出，开始恢复。", flush=True)
        state["desktop"] = {"state": "closed", "quitRequested": quit_requested, "appId": desktop.app_id}
        write_state(run, state)
    try:
        run_migration(draft, archive, state, wait_seconds or 3600, run, sidebar=desktop is not None)
    finally:
        if desktop and can_reopen_codex(draft, run):
            try:
                opened = reopen(desktop)
                state["desktop"].update(state="reopened", pid=opened["pid"])
                print("已重新打开 Codex。", flush=True)
            except Exception as error:
                state["desktop"].update(state="open-failed", error=str(error))
                print(f"重新打开 Codex 时出错：{error}", flush=True)
        elif desktop:
            state["desktop"]["state"] = "reopen-pending-verification"
            print("Codex 会话目录的发布状态需要核对，请查看恢复记录。", flush=True)
        write_state(run, state)
    phase = state.get("phase")
    if phase == "mapped-processed":
        print("已处理完所有直接匹配的批次。请查看进度文件中的导入、跳过和剩余项目。", flush=True)
        print("现在可以打开 Claude 和 DSH。", flush=True)
    else:
        print(f"恢复暂停在 {phase}。请查看进度文件中的待退出客户端或错误。", flush=True)
        return 2
    return 0 if not desktop or state["desktop"]["state"] == "reopened" else 2


def main(argv=None):
    from ai_session_migration.importer import checked_directory, rollback_import, target_locks
    from ai_session_migration.packages import PackageLibrary
    from ai_session_migration.safeio import atomic_json
    from .audit import audit_has_issues, audit_installed

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="guide",
                        choices=("preview", "guide", "audit", "status", "rollback", "claude-sidebar"))
    parser.add_argument("--archive", type=Path, help="Mac 导出的 Windows 会话 ZIP")
    parser.add_argument("--run-dir", type=Path, help="本次预览、恢复记录及目标备份目录")
    parser.add_argument("--project-roots", nargs=2, type=Path, metavar=("PRIVE", "WORK"))
    parser.add_argument("--mapping-file", type=Path, help="可选 JSON，指定 Mac 路径到 Windows 目录的映射")
    parser.add_argument("--preview-file", type=Path, help="读取已有预览，默认使用本次目录中的 preview.json")
    parser.add_argument("--journal", type=Path, help="rollback 使用的一次正式导入回执")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--auto", action="store_true", help="直接启动已确认的恢复向导")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("本向导在 Windows 上运行")
    if args.auto and args.action != "guide":
        parser.error("--auto 只能用于 guide")
    if args.wait_seconds < 0:
        parser.error("--wait-seconds 必须大于等于零")
    run = args.run_dir
    if args.action in {"audit", "status", "rollback", "claude-sidebar"} and not run:
        parser.error("此操作需要 --run-dir")
    if args.action == "status":
        print((run / "progress.json").read_text(encoding="utf-8"))
        return 0
    if args.action == "rollback":
        if not args.journal:
            parser.error("rollback 需要 --journal")
        run = run.resolve()
        journal = args.journal.resolve()
        if not journal.is_relative_to(run / "backups"):
            parser.error("请选择本次备份目录下的导入回执")
        if input("回退前请退出对应客户端。输入 ROLLBACK 继续 > ").strip() != "ROLLBACK":
            return 0
        with target_locks({"migration": str(Path.home())}, guide_lock_cache()):
            report = rollback_import(journal, run / "cache", confirmed_offline=True, progress=progress)
        print(json.dumps({"status": report["status"], "journal": str(journal)}, ensure_ascii=False))
        return 0

    archive_path = args.archive
    existing_preview = args.preview_file or (run / "preview.json" if run else None)
    if not archive_path and existing_preview and existing_preview.is_file():
        archive_path = Path(json.loads(existing_preview.read_bytes())["archive"])
    interactive = args.action == "guide" and not args.auto
    if not archive_path and interactive:
        archive_path = Path(input("Mac 导出的会话 ZIP 路径 > ").strip().strip('"'))
    if archive_path is None or not archive_path.expanduser().is_file():
        parser.error("请提供已存在的 --archive 会话 ZIP")
    archive_path = archive_path.expanduser().resolve()
    if not run:
        default_run = archive_path.parent / ("restore-" + archive_path.stem)
        if interactive:
            value = input(f"恢复及备份目录，Enter 使用 {default_run} > ").strip().strip('"')
            run = Path(value) if value else default_run
        else:
            run = default_run
    run = checked_directory(run.expanduser().absolute())
    if any(run.is_relative_to(target) for target in probe.client_targets().values()):
        parser.error("恢复和备份目录必须在原客户端资料目录之外")
    preview_file = args.preview_file or run / "preview.json"
    if args.preview_file and not preview_file.is_file():
        parser.error("--preview-file 必须是已存在的预览文件")
    with target_locks({"migration": str(run / "progress.json")}, run / "cache"):
        archive = PackageLibrary(archive_path, run / "cache")
        if not archive.package_catalog:
            raise ValueError("请选择包含原生会话目录的 Windows ZIP")
        if preview_file.is_file():
            draft = read_preview(preview_file, archive)
            if args.project_roots and tuple(path.resolve() for path in args.project_roots) != tuple(
                Path(draft["projectRoots"][label]) for label in ("prive", "work")
            ):
                parser.error("项目根目录与已有预览不同，请使用新的 --run-dir")
            if args.mapping_file:
                manual = json.loads(args.mapping_file.read_bytes())
                if any(draft["mappings"].get(key) != str(Path(value).resolve()) for key, value in manual.items()):
                    parser.error("映射与已有预览不同，请使用新的 --run-dir")
        else:
            if args.action == "audit":
                parser.error("找不到本次恢复的预览文件")
            project_roots = args.project_roots
            if not project_roots and interactive:
                project_roots = [Path(input(f"Windows {name} 项目根目录 > ").strip().strip('"')) for name in ("Prive", "Work")]
            if not project_roots:
                parser.error("请提供 --project-roots PRIVE WORK")
            manual = json.loads(args.mapping_file.read_bytes()) if args.mapping_file else {}
            if not isinstance(manual, dict) or any(not isinstance(value, str) for value in manual.values()):
                parser.error("--mapping-file 应为 Mac 路径到 Windows 路径的 JSON 对象")
            draft = probe.preview(archive, archive.scan().records, project_roots, run / "cache", manual)
            atomic_json(preview_file, draft)
            atomic_json(run / "pending-paths.json", draft["pendingPaths"])
        state = read_state(draft, run)
        if args.action == "preview":
            print(json.dumps({"preview": str(preview_file), "summary": draft["summary"],
                              "needsSample": draft.get("needsSample", [])}, ensure_ascii=False, indent=2))
            return 0
        if args.action == "guide":
            with target_locks({"migration": str(Path.home())}, guide_lock_cache()):
                return guide(draft, archive, state, args.wait_seconds, run, args.auto)
        if not (run / "progress.json").is_file():
            parser.error("找不到本次恢复进度")
        if args.action == "claude-sidebar":
            raise ValueError(CLAUDE_READ_ONLY)
        report = audit_installed(draft, archive, state, run)
        atomic_json(run / "account-and-dsh-audit.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if audit_has_issues(report) else 0


def guide_lock_cache():
    local_data = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return local_data / "AI Session Migration" / "windows-restore"


if __name__ == "__main__":
    raise SystemExit(main())

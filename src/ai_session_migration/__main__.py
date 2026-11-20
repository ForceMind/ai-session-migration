from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import io
import zipfile

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QIcon, QPalette, QColor
from PySide6.QtWidgets import QApplication

from . import __version__
from .gui import MainWindow
from .safeio import atomic_json, within


def synthetic_import_acceptance(window):
    """Packaged-program qualification on pre-created, isolated synthetic homes only."""
    from .backup import make_backup, save_recovery_key
    from .file_restore import build_file_plan, execute_file_restore, rollback_file_restore
    from .history import ReadingCatalog, load_prompts
    from .migration_policy import CLAUDE_READ_ONLY, NATIVE_PROVIDERS
    from .packages import PackageLibrary
    from .importer import build_plan, execute_import, rollback_import, inventory
    from .reading_transfer import materialize_readings
    from .safeio import hash_file
    from .transfer_gui import mapped_project
    from .work_projects import discover_projects, ProjectView

    if window.home.resolve() == Path.home().resolve() or {r.session_id for r in window.records} != {"claude-session", "codex-session", "harness-session"}:
        raise ValueError("Synthetic import qualification refuses real or unknown source data")
    target = window.state.root / "synthetic-import-target"
    target_project = target / "示例项目"
    projects = discover_projects(window.records, window.home)
    if (not target_project.is_dir() or len(projects) != 1
            or not (window.home / "示例项目/selected.txt").is_file()):
        raise ValueError("Synthetic source and target must be prepared by the acceptance fixture generator")
    before_source = {str(p): hash_file(p) for p in window.home.rglob("*") if p.is_file()}
    key = window.state.root / "synthetic-recovery.asmkey"
    save_recovery_key(key)

    draft = "先核对消息 #1，再检查设置页的三个分组。"

    def fixture_model(prompt, check):
        check()
        if "把设置页分成账号" not in prompt or "fixture-existing-thinking" in prompt:
            raise ValueError("Unexpected synthetic model input")
        return draft

    project_id = projects[0].id
    exported = make_backup(window.library, window.records, window.state.root / "synthetic-export", key,
                           project_choices={project_id: ("files", False)}, model=fixture_model)
    package = PackageLibrary(Path(exported["portable"]), window.state.cache / "synthetic-receiving")
    selected = package.scan().records
    mapped = [(r.cwd, mapped_project(r, {project_id: str(target_project)})) for r in selected]
    view = ProjectView(package, project_id)
    project_entries = [package.entries[name] for name in view.items]
    providers = {r.provider for r in selected}
    owners = {r.uid for r in selected}
    project_shared = (providers == {"claude", "codex", "deepseek"}
                      and len(package.package_catalog.get("projects", [])) == 1
                      and sorted(e["projectPath"] for e in project_entries) == ["existing.txt", "selected.txt"]
                      and all(set(e["sessionUids"]) == owners for e in project_entries)
                      and all(path == str(target_project) for _, path in mapped))

    reading = materialize_readings(package)
    prompts, fingerprints = load_prompts(extra=reading["extraPromptsPath"])
    catalog = ReadingCatalog(reading["readingRoot"], prompts, fingerprints).scan()
    main = next(r for r in catalog.entries if r.kind == "main")
    messages, digest = catalog.read(main)
    reading_meta = package.package_catalog["readings"][next(r.uid for r in selected if r.provider == "claude")]
    claude_readback = (len(messages) == 2 and "把设置页分成账号" in messages[0].numbered_body()
                       and messages[0].start < messages[1].start and digest == reading_meta["sourceSha256"]
                       and draft in prompts[main.number].prompt and exported["handoff"] == {"generated": 1})

    existing = target_project / "existing.txt"
    with existing.open("xb") as stream:
        stream.write(b"synthetic target version")
    before_project = inventory(target_project)
    project_plan = build_file_plan(view, list(view.items), (), target_project, window.state.root / "synthetic-project-backups")
    project_report = execute_file_restore(view, project_plan)
    project_recovered = (project_plan["new"] == 1 and project_plan["skip"] == 1 and project_plan["blocked"] == 0
                         and project_report["backupVerified"] and project_report["readbackVerified"]
                         and (target_project / "selected.txt").read_bytes() == b"synthetic work file"
                         and existing.read_bytes() == b"synthetic target version")

    targets = {p: str(target / d) for p, d in [("claude", ".claude"), ("codex", ".codex"), ("deepseek", ".dsh")]}
    before = {p: inventory(Path(root)) for p, root in targets.items()}
    try:
        build_plan(package, selected, targets, dict(mapped), window.state.cache)
    except ValueError as error:
        if str(error) != CLAUDE_READ_ONLY:
            raise
    else:
        raise ValueError("Synthetic import preview admitted Claude as a native provider")
    native = [r for r in selected if r.provider in NATIVE_PROVIDERS]
    plan = build_plan(package, native, targets, dict(mapped), window.state.cache)
    if {item["record"]["provider"] for item in plan.ready} != {"codex", "deepseek"}:
        raise ValueError("Synthetic import preview did not admit Codex and DeepSeek Harness")
    report = execute_import(plan, window.state.root / "synthetic-target-backups", window.state.cache, confirmed_offline=True)
    claude_untouched = before["claude"] == inventory(Path(targets["claude"]))
    duplicate = build_plan(package, native, targets, dict(mapped), window.state.cache)
    skipped = {item["record"]["provider"] for item in duplicate.items if item["action"] == "skip"}
    restored = rollback_import(Path(report["journal"]), window.state.cache, confirmed_offline=True)
    project_rollback = rollback_file_restore(project_report)
    source_unchanged = before_source == {str(p): hash_file(p) for p in window.home.rglob("*") if p.is_file()}
    target_restored = before == {p: inventory(Path(root)) for p, root in targets.items()}
    result = {
        "syntheticOnly": True, "providers": sorted(providers), "imported": report["imported"],
        "backupVerified": report["backupVerified"], "nativeReadback": report["nativeReadback"],
        "duplicateIdsSkipped": not duplicate.ready and skipped == {"codex", "deepseek"},
        "rollbackVerified": restored["rollbackVerified"] and target_restored,
        "sourceFilesUnchanged": source_unchanged, "projectArchivedOnce": project_shared,
        "projectConflictAndRecoveryVerified": project_recovered,
        "projectRollbackVerified": project_rollback["rollbackVerified"] and inventory(target_project) == before_project,
        "claudeReadingAndDraftVerified": claude_readback, "claudeNativeStoreUntouched": claude_untouched,
        "privateBackupVerified": exported["encryptedReadbackVerified"], "portablePrivacyVerified": exported["privacyCheck"]["verified"],
    }
    result["mixedPackageVerified"] = all(result[k] for k in [
        "projectArchivedOnce", "projectConflictAndRecoveryVerified", "projectRollbackVerified",
        "claudeReadingAndDraftVerified", "claudeNativeStoreUntouched", "privateBackupVerified", "portablePrivacyVerified",
    ])
    result["ok"] = report["status"] == "complete" and report["imported"] == 2 and all(result[k] for k in [
        "backupVerified", "duplicateIdsSkipped", "rollbackVerified", "sourceFilesUnchanged", "mixedPackageVerified",
    ])
    return result


def synthetic_file_acceptance(window):
    """Qualify packaged recovery codecs and file recovery on synthetic data only."""
    from .recovery_catalog import RecoveryCatalog
    from .file_restore import build_file_plan, plan_rows, execute_file_restore, check_file_restore, rollback_file_restore
    from .safeio import hash_file
    from .importer import inventory
    if window.home.resolve()==Path.home().resolve() or {r.session_id for r in window.records}!={'claude-session','codex-session','harness-session'}:
        raise ValueError('File recovery acceptance refuses real or unknown sources')
    base=window.state.root/'synthetic-file-recovery';base.mkdir(mode=0o700)
    source=base/'source';source.mkdir(mode=0o700)
    archive=source/'project.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        z.writestr('folder/selected.txt',b'preserved thinking and original bytes')
        z.writestr('folder/existing.txt',b'source version cannot overwrite existing')
        z.writestr('not-selected.txt',b'must remain absent')
    before_source=hash_file(archive)
    catalog=RecoveryCatalog(source,window.state.cache/'synthetic-file-recovery')
    try:
        group=catalog.discover()[0];group=catalog.index_group(group['id'])
        rows=catalog.list_items(group['id'],limit=200)
        folder=next(r for r in rows if r['path']=='folder')
        target=base/'target';target.mkdir(mode=0o700);(target/'keep.txt').write_bytes(b'original target')
        backup=base/'backups'
        preliminary=build_file_plan(catalog,[folder['id']],[],target,backup)
        existing=next(r for r in plan_rows(preliminary,limit=100) if r['path'].replace('\\','/').endswith('/existing.txt'))
        p=Path(existing['destination']);p.parent.mkdir(parents=True);p.write_bytes(b'existing target remains')
        before=inventory(target)
        plan=build_file_plan(catalog,[folder['id']],[],target,backup)
        report=execute_file_restore(catalog,plan)
        checked=check_file_restore(report)
        selected=next(r for r in plan_rows(plan) if r['path'].replace('\\','/').endswith('/selected.txt'))
        content_ok=Path(selected['destination']).read_bytes()==b'preserved thinking and original bytes'
        unselected_absent=not any(p.name=='not-selected.txt' for p in target.rglob('*'))
        rolled=rollback_file_restore(checked)
        result={'syntheticOnly':True,'backupVerified':report['backupVerified'],'readbackVerified':checked['readbackVerified'],
                'selectedBytesCorrect':content_ok,'existingPreserved':p.read_bytes()==b'existing target remains',
                'unselectedAbsent':unselected_absent,'rollbackVerified':rolled['rollbackVerified'] and inventory(target)==before,
                'sourceUnchanged':hash_file(archive)==before_source,'nativeClientRestored':False}
        result['ok']=all(result[k] for k in ['backupVerified','readbackVerified','selectedBytesCorrect','existingPreserved','unselectedAbsent','rollbackVerified','sourceUnchanged'])
        return result
    finally:catalog.close()


def main():
    parser = argparse.ArgumentParser(add_help=False)
    # Internal acceptance hooks; normal users launch the app without arguments.
    parser.add_argument("--smoke-home")
    parser.add_argument("--smoke-state")
    parser.add_argument("--smoke-report")
    parser.add_argument("--smoke-screenshot")
    parser.add_argument("--smoke-real", action="store_true")
    parser.add_argument("--smoke-import", action="store_true")
    parser.add_argument("--smoke-recovery", action="store_true")
    parser.add_argument("--smoke-history", action="store_true")
    parser.add_argument("--smoke-history-session")
    args, _ = parser.parse_known_args()
    if any([args.smoke_home, args.smoke_state, args.smoke_report, args.smoke_screenshot, args.smoke_real, args.smoke_import, args.smoke_recovery, args.smoke_history, args.smoke_history_session]) and os.environ.get("ASM_ACCEPTANCE_TEST") != "1":
        raise SystemExit("Internal acceptance hooks are disabled for normal application launches")
    if (args.smoke_import or args.smoke_recovery) and (not args.smoke_report or not args.smoke_state or not args.smoke_home or args.smoke_real or Path(args.smoke_home).resolve() == Path.home().resolve()):
        raise SystemExit("Import acceptance requires isolated synthetic homes and a report")
    if args.smoke_history and (not args.smoke_report or not args.smoke_state or not args.smoke_real or args.smoke_import or args.smoke_recovery):
        raise SystemExit("History acceptance requires explicit read-only real data, isolated state and report")
    if args.smoke_report:
        roots = []
        for home in {Path.home().resolve(), Path(args.smoke_home or Path.home()).resolve()}:
            roots += [home / ".claude", home / ".codex", home / ".dsh", home / "Library/Application Support/Claude", home / "Library/Application Support/Codex"]
            roots += list((home / "Library/Application Support").glob("Claude-*"))
        for value in [args.smoke_report, args.smoke_screenshot, args.smoke_state]:
            if value and any(within(Path(value), root) for root in roots):
                raise SystemExit("Acceptance output cannot be written inside original client data")
        for value in [args.smoke_report, args.smoke_screenshot]:
            if value and Path(value).exists():
                raise SystemExit("Acceptance output exists; refusing to overwrite it")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("AI Session Migration")
    app.setOrganizationName("ForceMind")
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    font = QFont()
    font.setFamily("PingFang SC" if sys.platform == "darwin" else "Segoe UI" if sys.platform == "win32" else "DejaVu Sans")
    font.setPixelSize(13)
    app.setFont(font)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#111316"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e9edf2"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#191c20"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e9edf2"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#20252b"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e9edf2"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#294a37"))
    app.setPalette(palette)
    asset_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "assets"
    if (asset_root / "app-icon.png").is_file():
        app.setWindowIcon(QIcon(str(asset_root / "app-icon.png")))
    window = MainWindow(Path(args.smoke_home) if args.smoke_home else None, Path(args.smoke_state) if args.smoke_state else None)
    screen = app.primaryScreen()
    if screen:
        available = screen.availableGeometry()
        window.resize(min(1440, available.width() - 40), min(940, available.height() - 60))
    window.show()
    if args.smoke_history:
        from .history_smoke import start_history_smoke
        start_history_smoke(window, app, args)
        return app.exec()
    if args.smoke_screenshot and not args.smoke_real:
        window.resize(1440, 940)
    if args.smoke_report:
        start = time.monotonic()
        report_path = Path(args.smoke_report)
        real_data = bool(args.smoke_real) or Path(args.smoke_home or Path.home()).resolve() == Path.home().resolve()
        result = {"nativeQt": True, "platformPlugin": app.platformName(), "packaged": bool(getattr(sys, "frozen", False)), "realLocalData": real_data, "modelRequestSent": False, "windowsImported": False, "thinkingTransform": False}
        state = {"read_started": False, "read_done": False, "read_error": None}
        def finish():
            result["elapsedSeconds"] = round(time.monotonic() - start, 2)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
                f.flush(); os.fsync(f.fileno())
            window.close()
            QTimer.singleShot(1500, app.quit)
        def poll():
            if time.monotonic() - start > 180:
                result.update(ok=False, error="native acceptance timed out")
                finish()
                return
            if window.catalog is None or window.loading:
                QTimer.singleShot(100, poll)
                return
            if not state["read_started"]:
                state["read_started"] = True
                result["sourceCounts"] = {p: sum(r.provider == p and not r.child for r in window.records) for p in ["claude", "codex", "deepseek"]}
                result["totalRecords"] = len(window.records)
                library = window.library
                def inspect(progress):
                    checks = {}
                    for provider in ["claude", "codex", "deepseek"]:
                        candidates = [r for r in window.records if r.provider == provider and not r.child][:12]
                        for record in candidates:
                            page = library.page(record, 10)
                            if page.messages:
                                checks[provider] = {"sessionIdSha256": hashlib.sha256(record.session_id.encode()).hexdigest(), "messages": page.total, "attachments": len(page.attachments), "thinkingInPage": sum(m.kind == "thinking" for m in page.messages), "warnings": len(page.warnings)}
                                break
                        if provider not in checks:
                            checks[provider] = {"messages": 0}
                    if args.smoke_import:
                        checks["syntheticImport"] = synthetic_import_acceptance(window)
                    if args.smoke_recovery:
                        checks['syntheticRecovery']=synthetic_file_acceptance(window)
                    return checks
                def read_done(checks):
                    if "syntheticImport" in checks:
                        result["syntheticImport"] = checks.pop("syntheticImport")
                    if 'syntheticRecovery' in checks:
                        result['syntheticRecovery']=checks.pop('syntheticRecovery')
                        window.navigate('recovery')
                        result['recoveryWidgetCreated']='recovery' in window.pages
                    result["readChecks"] = checks
                    state["read_done"] = True
                def read_failed(error):
                    result["readError"] = error
                    state["read_error"] = error
                    state["read_done"] = True
                window.task(inspect, read_done, "正在验证原生读取", read_failed)
            if not state["read_done"] or window.tasks:
                QTimer.singleShot(100, poll)
                return
            result["ok"] = not state["read_error"] and all(result.get("readChecks", {}).get(p, {}).get("messages", 0) > 0 for p in ["claude", "codex", "deepseek"])
            if args.smoke_import:
                result["ok"] = result["ok"] and result.get("syntheticImport", {}).get("ok", False)
            if args.smoke_recovery:
                result['ok']=result['ok'] and result.get('syntheticRecovery',{}).get('ok',False) and result.get('recoveryWidgetCreated',False)
            if args.smoke_screenshot:
                # Only synthetic fixtures may be captured for sharable screenshots.
                if real_data:
                    result["screenshotSkipped"] = "real data privacy"
                else:
                    target = Path(args.smoke_screenshot)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    window.grab().save(str(target))
                    result["screenshot"] = str(target)
            finish()
        QTimer.singleShot(100, window.scan)
        QTimer.singleShot(200, poll)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

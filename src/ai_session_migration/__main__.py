from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import io

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QIcon, QPalette, QColor
from PySide6.QtWidgets import QApplication

from . import __version__
from .gui import MainWindow
from .safeio import atomic_json, within


def synthetic_import_acceptance(window):
    """Packaged-program qualification on pre-created, isolated synthetic homes only."""
    from .backup import make_backup, save_recovery_key
    from .packages import PackageLibrary
    from .importer import build_plan, execute_import, rollback_import, inventory
    from .safeio import hash_file
    if window.home.resolve() == Path.home().resolve() or {r.session_id for r in window.records} != {"claude-session", "codex-session", "harness-session"}:
        raise ValueError("Synthetic import qualification refuses real or unknown source data")
    target = window.state.root / "synthetic-import-target"
    if not (target / "示例项目").is_dir():
        raise ValueError("Synthetic target must be prepared by the acceptance fixture generator")
    before_source = {str(p): hash_file(p) for p in window.home.rglob("*") if p.is_file()}
    key = window.state.root / "synthetic-recovery.asmkey"
    save_recovery_key(key)
    exported = make_backup(window.library, window.records, window.state.root / "synthetic-export", key)
    package = PackageLibrary(Path(exported["portable"]), window.state.cache)
    selected = package.scan().records
    targets = {p: str(target / d) for p, d in [("claude", ".claude"), ("codex", ".codex"), ("deepseek", ".dsh")]}
    before = {p: inventory(Path(root)) for p, root in targets.items()}
    plan = build_plan(package, selected, targets, {r.cwd: str(target / "示例项目") for r in selected}, window.state.cache)
    if len(plan.ready) != 3:
        raise ValueError("Synthetic import preview did not admit all three providers")
    report = execute_import(plan, window.state.root / "synthetic-target-backups", window.state.cache, confirmed_offline=True)
    duplicate = build_plan(package, selected, targets, {r.cwd: str(target / "示例项目") for r in selected}, window.state.cache)
    restored = rollback_import(Path(report["journal"]), window.state.cache, confirmed_offline=True)
    source_unchanged = before_source == {str(p): hash_file(p) for p in window.home.rglob("*") if p.is_file()}
    target_restored = before == {p: inventory(Path(root)) for p, root in targets.items()}
    result = {"syntheticOnly": True, "providers": sorted(targets), "imported": report["imported"], "backupVerified": report["backupVerified"], "nativeReadback": report["nativeReadback"], "duplicateIdsSkipped": len(duplicate.ready) == 0, "rollbackVerified": restored["rollbackVerified"] and target_restored, "sourceFilesUnchanged": source_unchanged}
    result["ok"] = report["status"] == "complete" and all(result[k] for k in ["backupVerified", "duplicateIdsSkipped", "rollbackVerified", "sourceFilesUnchanged"])
    return result


def main():
    parser = argparse.ArgumentParser(add_help=False)
    # Internal acceptance hooks; normal users launch the app without arguments.
    parser.add_argument("--smoke-home")
    parser.add_argument("--smoke-state")
    parser.add_argument("--smoke-report")
    parser.add_argument("--smoke-screenshot")
    parser.add_argument("--smoke-real", action="store_true")
    parser.add_argument("--smoke-import", action="store_true")
    args, _ = parser.parse_known_args()
    if any([args.smoke_home, args.smoke_state, args.smoke_report, args.smoke_screenshot, args.smoke_real, args.smoke_import]) and os.environ.get("ASM_ACCEPTANCE_TEST") != "1":
        raise SystemExit("Internal acceptance hooks are disabled for normal application launches")
    if args.smoke_import and (not args.smoke_report or not args.smoke_state or not args.smoke_home or args.smoke_real or Path(args.smoke_home).resolve() == Path.home().resolve()):
        raise SystemExit("Import acceptance requires isolated synthetic homes and a report")
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
                    return checks
                def read_done(checks):
                    if "syntheticImport" in checks:
                        result["syntheticImport"] = checks.pop("syntheticImport")
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

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

from .gui import MainWindow
from .safeio import atomic_json, within


def main():
    parser = argparse.ArgumentParser(add_help=False)
    # Internal acceptance hooks; normal users launch the app without arguments.
    parser.add_argument("--smoke-home")
    parser.add_argument("--smoke-state")
    parser.add_argument("--smoke-report")
    parser.add_argument("--smoke-screenshot")
    parser.add_argument("--smoke-real", action="store_true")
    args, _ = parser.parse_known_args()
    if any([args.smoke_home, args.smoke_state, args.smoke_report, args.smoke_screenshot, args.smoke_real]) and os.environ.get("ASM_ACCEPTANCE_TEST") != "1":
        raise SystemExit("Internal acceptance hooks are disabled for normal application launches")
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
    app.setApplicationVersion("0.1.0")
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
                    return checks
                def read_done(checks):
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

"""Explicit, read-only packaged-history qualification. Not a user workflow."""
from pathlib import Path
import hashlib
import json
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .history import load_local_config
from .safeio import within


def start_history_smoke(window, app, args):
    config_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2] / 'local-data'
    config = load_local_config(config_dir)
    root = Path(config['readingRoot'])
    if any(within(Path(value), root) for value in (args.smoke_state, args.smoke_report)):
        raise ValueError('History qualification cannot write into the reading archive')
    window.history_config = config
    window.navigate('history')
    page = window.history_center
    page.config = config
    page.root.setText(config['readingRoot'])
    page.load_sources()
    start, phase = time.monotonic(), [0]
    selected_number = [args.smoke_history_session]
    result = {'packaged': bool(getattr(sys, 'frozen', False)), 'nativeQt': True,
              'platformPlugin': app.platformName(), 'modelRequestSent': False,
              'accountIndexWritten': False, 'browserTested': False}

    def finish(error=None):
        result['ok'] = not error and all(result.get(key) for key in ('countsCorrect', 'draftExact', 'copyCorrect', 'sourceUnchanged', 'subagentsVisible', 'sourcePathVisible'))
        if error:
            result['error'] = str(error)
        result['elapsedSeconds'] = round(time.monotonic() - start, 2)
        output = Path(args.smoke_report)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        window.close()
        QTimer.singleShot(500, lambda: app.exit(0 if result['ok'] else 1))

    def poll():
        if time.monotonic() - start > 120:
            finish('History qualification timed out')
            return
        if window.tasks:
            QTimer.singleShot(100, poll)
            return
        try:
            if phase[0] == 0:
                if page.catalog is None:
                    raise ValueError('History catalog unavailable: ' + page.notice.text())
                catalog = page.catalog
                result['main'] = sum(e.kind == 'main' for e in catalog.entries)
                result['agent'] = sum(e.kind == 'agent' for e in catalog.entries)
                result['prompts'] = len(catalog.prompts)
                result['countsCorrect'] = result['main'] > 0 and result['prompts'] > 0
                selected_number[0] = selected_number[0] or next(e.number for e in catalog.entries if e.kind == 'main' and e.number in catalog.prompts)
                page.search.setText(selected_number[0])
                page.timer.stop()
                page.filter_entries()
                phase[0] = 1
            elif phase[0] == 1:
                if page.current is None or page.current.number != selected_number[0] or not page.digest:
                    raise ValueError('Selected history context did not load')
                expected = page.catalog.prompts[selected_number[0]].prompt
                result['draftExact'] = page.prompt.toPlainText() == expected
                page.copy_prompt.click()
                result['copyCorrect'] = QApplication.clipboard().text() == expected
                with page.current.path.open('rb') as stream:
                    result['sourceUnchanged'] = hashlib.file_digest(stream, 'sha256').hexdigest() == page.digest
                result['messagesSelected'] = len(page.messages)
                result['sourcePathVisible'] = Path(page.location.text()) == page.current.path
                page.search.clear()
                page.timer.stop()
                page.kind.setCurrentIndex(1)
                phase[0] = 2
            else:
                result['subagentsVisible'] = page.entries.count() == result['agent']
                finish()
                return
        except Exception as error:
            finish(error)
            return
        QTimer.singleShot(100, poll)

    QTimer.singleShot(100, poll)

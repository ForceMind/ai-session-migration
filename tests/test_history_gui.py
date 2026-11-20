import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import hashlib
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit, QSplitter

from ai_session_migration.gui import MainWindow
from test_history import source_fixture

APP = QApplication.instance() or QApplication([])


class HistoryGuiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root, self.prepared = source_fixture(self.base)
        self.before = self.fingerprints()
        self.window = MainWindow(self.base / 'empty-home', self.base / 'state')
        self.errors = []
        self.window.error = self.errors.append
        self.window.history_config = {'readingRoot': str(self.root), 'preparedPath': str(self.prepared)}
        self.window.navigate('history')
        self.page = self.window.history_center
        self.page.load_sources()
        self.wait()

    def fingerprints(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [*self.root.iterdir(), self.prepared]}

    def wait(self):
        limit = time.monotonic() + 15
        while self.window.tasks and time.monotonic() < limit:
            APP.processEvents()
            time.sleep(.01)
        APP.processEvents()
        self.assertFalse(self.window.tasks)
        self.assertFalse(self.errors)

    def tearDown(self):
        self.wait()
        self.window.close()
        APP.processEvents()
        self.temp.cleanup()

    def test_real_history_navigation_message_lines_and_prepared_copy(self):
        page = self.page
        self.assertIs(self.window.stack.currentWidget(), page)
        self.assertEqual(page.entries.count(), 1)
        self.assertEqual(page.message_list.count(), 2)
        self.assertIn('6  │', page.body.toPlainText())
        self.assertEqual(page.location.text(), str(self.root / '0001.md'))
        self.assertIn('旧结论', page.prompt.toPlainText())
        page.prompt.appendPlainText('用户补充')
        page.copy_prompt.click()
        self.assertIn('用户补充', APP.clipboard().text())
        page.copy_path.click()
        self.assertEqual(APP.clipboard().text(), str(self.root / '0001.md'))
        self.assertEqual(self.before, self.fingerprints())
        self.assertNotIn('示例', ' '.join(button.text() for button in page.findChildren(__import__('PySide6.QtWidgets', fromlist=['QPushButton']).QPushButton)))

    def test_subagent_and_selected_excerpts_preserve_original_location(self):
        page = self.page
        page.kind.setCurrentIndex(1)
        self.wait()
        self.assertEqual(page.entries.count(), 1)
        self.assertFalse(page.copy_prompt.isEnabled())
        page.message_list.item(0).setCheckState(Qt.CheckState.Checked)
        page.compose.click()
        self.assertIn('尚未核对', page.prompt_status.text())
        page.copy_prompt.click()
        copied = APP.clipboard().text()
        self.assertIn('0002.md', copied)
        self.assertIn('子代理原文', copied)
        self.assertIn('已复制', page.prompt_status.text())
        self.assertEqual(self.before, self.fingerprints())

    def test_search_empty_then_full_text_and_folder_route(self):
        page = self.page
        page.search.setText('不存在')
        page.timer.stop()
        page.filter_entries()
        self.wait()
        self.assertEqual(page.entries.count(), 0)
        self.assertFalse(page.copy_prompt.isEnabled())
        page.full_text.setChecked(True)
        page.search.setText('pineapple')
        page.timer.stop()
        page.filter_entries()
        self.wait()
        self.assertEqual(page.entries.count(), 1)
        with patch('ai_session_migration.history_gui.QDesktopServices.openUrl', return_value=True) as open_url:
            page.open_folder.click()
            self.assertEqual(open_url.call_args.args[0].toLocalFile(), str(self.root).replace('\\', '/'))

    def test_source_change_blocks_clipboard_and_clear_sources_invalidates(self):
        page = self.page
        APP.clipboard().setText('protected clipboard')
        (self.root / '0001.md').write_text('changed', encoding='utf-8')
        page.copy_prompt.click()
        self.assertEqual(APP.clipboard().text(), 'protected clipboard')
        self.assertIn('原文已变化', page.prompt_status.text())
        self.assertFalse(page.copy_prompt.isEnabled())
        page.root.setText(str(self.base / 'missing'))
        self.assertIsNone(page.catalog)
        self.assertEqual(page.entries.count(), 0)
        self.assertFalse(page.compose.isEnabled())

    def test_native_and_package_routes_and_read_only_mode(self):
        with patch.object(self.window, 'scan') as scan:
            self.page.open_native()
            scan.assert_called_once()
        self.page.read_only()
        self.wait()
        self.assertNotIn('preparedPath', self.page.config)
        self.assertEqual(self.page.entries.count(), 1)
        self.assertFalse(self.page.copy_prompt.isEnabled())
        self.assertEqual(self.before, self.fingerprints())

    def test_package_handoff_regenerates_only_selected_session_and_preserves_prior_draft(self):
        from ai_session_migration.backup import make_backup, save_recovery_key
        from ai_session_migration.history_gui import HistoryCenter
        from ai_session_migration.packages import PackageLibrary
        from ai_session_migration.reading_transfer import ReadingSource, materialize_readings

        source = ReadingSource(self.window.history_config, self.base / 'reader-cache')
        key = self.base / 'fixture.asmkey'
        save_recovery_key(key)
        report = make_backup(source, source.catalog.records, self.base / 'portable', key)
        path = Path(report['portable'])
        package = PackageLibrary(path, self.base / 'receiving')
        before = path.read_bytes()
        self.window.library = package
        self.window.apply_catalog(package.scan())
        config = materialize_readings(package)
        page = HistoryCenter(self.window, config, package_mode=True)
        page.load_sources()
        self.wait()
        prior = page.prompt.toPlainText()
        self.assertIn('旧结论', prior)
        self.assertIn('复用接手稿', page.entries.item(0).text())
        self.assertTrue(page.generate_prompt.isEnabled())
        requests = []

        def model(prompt, check):
            check()
            requests.append(prompt)
            return '新稿请核对消息 #1，然后复查项目。'

        options = SimpleNamespace(model=model, exec=lambda: QDialog.DialogCode.Accepted)
        with patch('ai_session_migration.transfer_gui.ExportOptions', return_value=options) as chooser:
            page.generate_prompt.click()
        self.wait()
        selected, = chooser.call_args.args[2]
        self.assertFalse(selected.child)
        self.assertTrue(chooser.call_args.kwargs['model_only'])
        self.assertEqual(len(requests), 1)
        self.assertIn('pineapple', requests[0])
        self.assertNotIn('子代理原文', requests[0])
        self.assertEqual(page.prompt.toPlainText(), prior)
        self.assertTrue(page.compare_prompt.isEnabled())

        def adopt(dialog):
            splitter = dialog.findChild(QSplitter)
            old = splitter.widget(0).findChild(QPlainTextEdit)
            new = splitter.widget(1).findChild(QPlainTextEdit)
            self.assertTrue(old.isReadOnly() and new.isReadOnly())
            self.assertEqual(old.toPlainText(), prior)
            self.assertIn('新稿请核对消息 #1', new.toPlainText())
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog, 'exec', new=adopt):
            page.compare_prompt.click()
        self.assertIn('新稿请核对消息 #1', page.prompt.toPlainText())
        page.copy_prompt.click()
        self.assertIn('新稿请核对消息 #1', APP.clipboard().text())
        page.reset_prompt.click()
        self.assertEqual(page.prompt.toPlainText(), prior)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.before, self.fingerprints())


if __name__ == '__main__':
    unittest.main()

import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile,unittest,time
from unittest.mock import patch
from PySide6.QtWidgets import QApplication,QLabel
from PySide6.QtCore import Qt,QUrl
from ai_session_migration.gui import MainWindow,BackupWizard,ImportWizard,SafeText,label
from ai_session_migration.adapters import LocalLibrary
from ai_session_migration.backup import make_backup,save_recovery_key
from ai_session_migration.packages import PackageLibrary
from fixtures import make_home,make_import_target
from test_core import fingerprints

APP=QApplication.instance() or QApplication([])


class GuiTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name).resolve();make_home(self.base/'home')
        self.window=MainWindow(self.base/'home',self.base/'state')
        self.window.library=LocalLibrary(self.base/'home',self.base/'cache')
        self.catalog=self.window.library.scan()
        # Prevent asynchronous reads in these focused native widget tests.
        self.window.load_page=lambda:None
        self.window.apply_catalog(self.catalog)

    def tearDown(self):
        self.window.close();APP.processEvents();self.temp.cleanup()

    def test_real_widget_filters_marks_and_selection_never_write_sources(self):
        before=fingerprints(self.base/'home')
        self.assertEqual(self.window.session_list.count(),3)
        self.window.filter_provider('deepseek');self.assertEqual(self.window.session_list.count(),1)
        item=self.window.session_list.item(0);self.window.choose_session(item)
        self.window.toggle_mark();self.assertTrue(self.window.marks)
        self.window.add_current();self.assertEqual(len(self.window.selected),1)
        self.assertEqual(before,fingerprints(self.base/'home'))
        self.assertTrue(self.window.state.path.is_file())

    def test_key_and_selection_gate(self):
        wizard=BackupWizard(self.window)
        self.assertFalse(wizard.next.isEnabled())
        wizard.choose_all(True);self.assertTrue(wizard.next.isEnabled())
        wizard.forward();self.assertFalse(wizard.next.isEnabled())
        self.assertFalse(wizard.key_confirm.isEnabled())
        wizard.reject()

    def test_historical_markup_cannot_load_external_or_local_resources(self):
        view=SafeText('![private](file:///etc/passwd)\n![remote](https://example.invalid/image.png)')
        self.assertIsNone(view.loadResource(2,QUrl('https://example.invalid/image.png')))
        self.assertIsNone(view.loadResource(2,QUrl('file:///etc/passwd')))
        title=label('<img src="file:///etc/passwd">')
        self.assertEqual(title.textFormat(),Qt.TextFormat.PlainText)
        view.deleteLater();title.deleteLater();APP.processEvents()

    def wait_tasks(self):
        limit=time.monotonic()+20
        while self.window.tasks and time.monotonic()<limit:
            APP.processEvents();time.sleep(0.01)
        self.assertFalse(self.window.tasks)

    def test_native_import_wizard_preview_confirmation_restore_and_undo(self):
        before=fingerprints(self.base/'home')
        save_recovery_key(self.base/'key.asmkey')
        exported=make_backup(self.window.library,self.catalog.records,self.base/'exports',self.base/'key.asmkey')
        package=PackageLibrary(Path(exported['portable']),self.base/'package-cache')
        self.window.library=package;self.window.apply_catalog(package.scan())
        make_import_target(self.base/'target')
        wizard=ImportWizard(self.window)
        self.assertFalse(wizard.next.isEnabled())
        wizard.choose_all(True);wizard.forward()
        for p,folder in [('claude','.claude'),('codex','.codex'),('deepseek','.dsh')]:
            wizard.target_fields[p].setText(str(self.base/'target'/folder))
        for field in wizard.mapping_fields.values():field.setText(str(self.base/'target/示例项目'))
        wizard.destination.setText(str(self.base/'target-backups'))
        wizard.forward();self.wait_tasks()
        self.assertEqual(wizard.step,2)
        self.assertEqual(len(wizard.plan.ready),3)
        self.assertFalse(wizard.next.isEnabled())
        wizard.offline.setChecked(True);self.assertFalse(wizard.next.isEnabled())
        wizard.private_backup.setChecked(True);self.assertTrue(wizard.next.isEnabled())
        self.assertEqual(before,fingerprints(self.base/'home'))
        wizard.forward();self.wait_tasks()
        report=self.window.state.data['imports'][0]
        self.assertEqual(report['status'],'complete')
        self.assertTrue(report['backupVerified'])
        self.assertEqual(before,fingerprints(self.base/'home'))
        from PySide6.QtWidgets import QMessageBox
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):
            self.window.undo_import(report)
        self.wait_tasks()
        self.assertEqual(self.window.state.data['imports'][0]['status'],'rolled-back')
        self.assertTrue(self.window.state.data['imports'][0]['rollbackVerified'])


if __name__=='__main__':unittest.main()

import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile,unittest
from PySide6.QtWidgets import QApplication,QLabel
from PySide6.QtCore import Qt,QUrl
from ai_session_migration.gui import MainWindow,BackupWizard,SafeText,label
from ai_session_migration.adapters import LocalLibrary
from fixtures import make_home
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


if __name__=='__main__':unittest.main()

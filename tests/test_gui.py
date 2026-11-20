import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile,unittest,time
import threading
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtWidgets import QApplication,QComboBox,QDialog,QLabel,QListWidget,QPlainTextEdit,QPushButton
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
        self.errors=[]
        self.window.error=self.errors.append
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
        wizard.prepared_selection = tuple(r.uid for r in wizard.selected())
        wizard.forward();self.assertFalse(wizard.next.isEnabled())
        self.assertFalse(wizard.key_confirm.isEnabled())
        wizard.reject()

    def test_backup_final_page_previews_space_before_confirming_export(self):
        wizard=BackupWizard(self.window)
        wizard.choose_all(True)
        wizard.prepared_selection=tuple(r.uid for r in wizard.selected())
        wizard.forward()
        destination=self.base/'preview-exports'
        wizard.destination.setText(str(destination))
        wizard.key_path=self.base/'preview-key.asmkey'
        save_recovery_key(wizard.key_path)
        wizard.key_confirm.setEnabled(True);wizard.key_confirm.setChecked(True)
        wizard.forward();self.wait_tasks()
        self.assertEqual(wizard.step,2)
        self.assertTrue(wizard.next.isEnabled())
        for visible in ('迁移包约','加密保全约','临时文件约','缓存同盘'):
            self.assertIn(visible,wizard.space_summary.text())
        self.assertEqual(wizard.project_sizes.count(),1)
        self.assertFalse(destination.exists())
        wizard.reject()

    def test_privacy_review_selects_a_single_project_file_without_using_live_source(self):
        report={"privacyCheck":{"findings":[{"role":"project-work-file","locator":"objects/00000009",
                    "projectPath":"flagged.txt","category":"已识别账号值或疑似凭据","sessions":[]}]}}

        def confirm(dialog):
            item=dialog.findChild(QListWidget).item(0)
            self.assertIn("排除项目文件 flagged.txt",item.text())
            self.assertEqual(item.checkState(),Qt.CheckState.Checked)
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog,"exec",new=confirm), patch.object(self.window,"rebuild_from_preservation") as rebuild:
            self.window.review_privacy_findings(report,report["privacyCheck"]["findings"])
        rebuild.assert_called_once_with(report,{"sessions":set(),"objects":{"objects/00000009"},"projects":set()})

    def test_backup_version_switch_requires_selecting_one_existing_backup(self):
        from ai_session_migration.backup import SCHEMA

        def report(parent, name):
            output=parent/('会话备份-'+name)
            return dict(schema=SCHEMA,status='complete',encryptedReadbackVerified=True,plainSha256='fixture',
                        output=str(output),portable=str(output/'migration.zip'),encrypted=str(output/'private.asmb'))

        root=self.base/'backup-root'
        incoming, first, second=[report(root,name) for name in ('new','first','second')]
        elsewhere=report(self.base/'separate-root','unrelated')
        self.window.state.data['backups']=[incoming,first,second,elsewhere]
        options=self.window.backup_version_options(incoming)
        self.assertEqual(options,[first,second])

        def select(dialog):
            targets=dialog.findChild(QComboBox)
            submit=next(button for button in dialog.findChildren(QPushButton) if button.text()=='校验并切换到新版')
            self.assertFalse(submit.isEnabled())
            targets.setCurrentIndex(2)
            self.assertTrue(submit.isEnabled())
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog,'exec',new=select),patch.object(self.window,'activate_backup_version') as activate:
            self.window.choose_backup_version(incoming,options)
        activate.assert_called_once_with(incoming,second)

    def test_historical_markup_cannot_load_external_or_local_resources(self):
        view=SafeText('![private](file:///etc/passwd)\n![remote](https://example.invalid/image.png)')
        self.assertIsNone(view.loadResource(2,QUrl('https://example.invalid/image.png')))
        self.assertIsNone(view.loadResource(2,QUrl('file:///etc/passwd')))
        title=label('<img src="file:///etc/passwd">')
        self.assertEqual(title.textFormat(),Qt.TextFormat.PlainText)
        view.deleteLater();title.deleteLater();APP.processEvents()

    def test_model_confirmation_lists_only_selected_claude_main_sessions(self):
        from ai_session_migration.transfer_gui import ExportOptions
        options=ExportOptions(self.window,[],self.catalog.records)
        options.mode.setCurrentIndex(options.mode.findData("custom"))
        options.base.setText("http://localhost:4321/v1")
        options.model_name.setText("synthetic-model")
        options.key.setText("synthetic-memory-only-key")
        reviewed=[]

        def reject(dialog):
            reviewed.append(dialog)
            sessions=dialog.findChild(QListWidget)
            self.assertEqual(sessions.count(),1)
            self.assertIn("claude-session",sessions.item(0).text())
            labels=" ".join(label.text() for label in dialog.findChildren(QLabel))
            self.assertIn("http://localhost:4321/v1 · synthetic-model",labels)
            self.assertNotIn("synthetic-memory-only-key",labels)
            return QDialog.DialogCode.Rejected

        with patch.object(QDialog,"exec",new=reject):
            options.confirm()
        self.assertEqual(len(reviewed),1)
        self.assertEqual(options.result(),QDialog.DialogCode.Rejected)

    def test_stop_handoff_generation_during_export_keeps_the_reading_bundle(self):
        from ai_session_migration.reading_transfer import materialize_readings
        source_before=fingerprints(self.base/'home')
        started=threading.Event()

        def model(_text,check):
            started.set()
            until=time.monotonic()+10
            while time.monotonic()<until:
                check();time.sleep(0.01)
            raise AssertionError("stopping generation did not reach the model")

        key=self.base/'generation-key.asmkey'
        save_recovery_key(key)
        self.window.run_backup(self.catalog.records,self.base/'generation-exports',key,model=model)
        until=time.monotonic()+10
        while not started.is_set() and time.monotonic()<until:
            APP.processEvents();time.sleep(0.01)
        self.assertTrue(started.is_set())
        buttons=self.window.pages['progress'].findChildren(QPushButton)
        next(b for b in buttons if b.text()=="停止生成接手稿，继续备份").click()
        self.wait_tasks()
        self.assertFalse(self.window.cancel.is_set())
        report=self.window.state.data['backups'][0]
        self.assertIn(report['status'],{'complete','complete-with-warnings'})
        self.assertEqual(report['handoff'],{'pending':1})
        self.assertTrue(report['encryptedReadbackVerified'])
        package=PackageLibrary(Path(report['portable']),self.base/'generation-readback')
        self.assertTrue(materialize_readings(package)['readingRoot'])
        self.assertEqual(source_before,fingerprints(self.base/'home'))

    def test_git_preview_conflict_disables_publish_and_allows_existing_project(self):
        from ai_session_migration.transfer_gui import ProjectRestore
        from ai_session_migration.work_projects import GitChange
        project={"id":"fixture", "title":"示例", "mode":"git", "git":{"commit":"f"*40,"dependenciesChecked":True}}
        package=SimpleNamespace(package_catalog={"projects":[project]})
        restore=ProjectRestore(self.window,package)
        closed=[]
        reviewed=SimpleNamespace(commit=project["git"]["commit"],destination=self.base/'new-project',
                                 changes=[GitChange("same.txt","untracked",conflict="基线已存在同名文件")],
                                 close=lambda:closed.append(True))

        def reject(preview):
            self.assertIn("基线已存在同名文件",preview.findChild(QPlainTextEdit).toPlainText())
            publish=next(b for b in preview.findChildren(QPushButton) if b.text()=="确认恢复到目标目录")
            self.assertFalse(publish.isEnabled())
            return QDialog.DialogCode.Rejected

        with patch.object(QDialog,"exec",new=reject):
            restore.confirm_git(project,reviewed)
        self.assertEqual(closed,[True])
        self.assertFalse(reviewed.destination.exists())
        restore.git_failed("提交暂时不可用")
        self.assertEqual(restore.mode.currentData(),"link")
        existing=self.base/'home'/'示例项目'
        restore.location.setText(str(existing));restore.preview()
        self.assertEqual(self.window.project_locations[project["id"]],str(existing))

    def wait_tasks(self):
        limit=time.monotonic()+20
        while self.window.tasks and time.monotonic()<limit:
            APP.processEvents();time.sleep(0.01)
        self.assertFalse(self.window.tasks)
        self.assertFalse(self.errors, '\n'.join(self.errors))

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
        for p,folder in [('codex','.codex'),('deepseek','.dsh')]:
            wizard.target_fields[p].setText(str(self.base/'target'/folder))
        for field in wizard.mapping_fields.values():field.setText(str(self.base/'target/示例项目'))
        wizard.destination.setText(str(self.base/'target-backups'))
        wizard.forward();self.wait_tasks()
        self.assertEqual(wizard.step,2)
        self.assertEqual(len(wizard.plan.ready),2)
        self.assertIn('目标暂存',wizard.space_notice.text())
        self.assertIn('完整目标备份',wizard.space_notice.text())
        self.assertIn('可用',wizard.space_notice.text())
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

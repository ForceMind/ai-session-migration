import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import json
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from ai_session_migration.gui import MainWindow
from ai_session_migration.recovery_gui import RecoveryCenter

APP = QApplication.instance() or QApplication([])


class FakeCatalog:
    def __init__(self, root, cache, keys=None):
        self.root, self.cache = root, cache
        self.selected_keys = keys or []
        self.closed = False
        self.group_data = [dict(id='projects', label='示例项目备份', category='projects', path=str(root),
            status='unindexed', count=450, size=4500, encrypted=False, detail='generic fixture'),
            dict(id='missing', label='未连接的示例卷', category='snapshots', path='fixture-volume',
            status='missing', count=0, size=0, encrypted=False, detail='卷缺失'),
            dict(id='keys', label='密钥备份', category='keys', path='fixture-keys',
            status='ready', count=1, size=10, encrypted=False, detail='仅元数据')]

    def discover(self):
        return self.group_data

    def groups(self):
        return self.group_data

    def index_group(self, group_id, progress, cancel):
        progress('索引示例项目', 50)
        if cancel.is_set():
            raise InterruptedError('已取消')
        group = next(g for g in self.group_data if g['id'] == group_id)
        group['status'] = 'ready'
        return group

    def _rows(self, group_id, search=''):
        if group_id == 'keys':
            return [dict(id='key', group_id='keys', path='example.asmkey', original_path='example.asmkey',
                kind='file', size=10, status='ready', sha256='abc', detail='密钥格式：asmkey', secret='DO-NOT-PREVIEW')]
        return [dict(id=f'item-{i}', group_id=group_id, path=f'project/file-{i:04d}.txt',
            original_path=f'fixture/project/file-{i:04d}.txt', kind='directory' if i == 0 else 'file',
            size=10, status='ready', sha256='abc', detail='generic fixture')
            for i in range(450) if not search or search in f'project/file-{i:04d}.txt']

    def count_items(self, group_id, search=''):
        return len(self._rows(group_id, search))

    def list_items(self, group_id, search='', offset=0, limit=200):
        return self._rows(group_id, search)[offset:offset + limit]

    def close(self):
        self.closed = True


class RecoveryGuiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.owner = MainWindow(self.base / 'home', self.base / 'state')
        self.page = RecoveryCenter(self.owner, FakeCatalog)
        self.page.root_field.setText(str(self.base))
        self.page.load_root(); self.wait_tasks()

    def tearDown(self):
        self.wait_tasks()
        self.page.deleteLater(); self.owner.close(); APP.processEvents()
        self.temp.cleanup()

    def wait_tasks(self):
        deadline = time.monotonic() + 10
        while self.owner.tasks and time.monotonic() < deadline:
            APP.processEvents(); time.sleep(.005)
        APP.processEvents()
        self.assertFalse(self.owner.tasks)

    def choose_project(self):
        self.page.group_list.setCurrentRow(0)
        self.page.index_group(); self.wait_tasks()
        self.assertEqual(self.page.items.count(), 200)

    def test_paged_folder_item_group_selection_search_and_metadata(self):
        self.choose_project()
        self.page.items.item(0).setCheckState(Qt.CheckState.Checked)
        self.assertIn('item-0', self.page.selected_items)
        self.page.next.click(); self.wait_tasks()
        self.assertEqual(self.page.offset, 200)
        self.assertEqual(self.page.items.count(), 200)
        self.page.previous.click(); self.wait_tasks()
        self.assertEqual(self.page.items.item(0).checkState(), Qt.CheckState.Checked)
        self.page.search.setText('0449'); self.page.search_items(); self.wait_tasks()
        self.assertEqual(self.page.items.count(), 1)
        self.page.items.setCurrentRow(0)
        self.assertIn('fixture/project/file-0449.txt', self.page.details.toPlainText())
        self.page.group_list.item(0).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self.page.selected_groups, {'projects'})
        self.assertEqual(self.page.selected_items, {'item-0'})
        self.assertFalse(self.page.group_list.item(1).flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def test_key_metadata_never_previews_body_and_category_filter(self):
        self.page.category.setCurrentIndex(self.page.category.findData('keys'))
        self.assertEqual(self.page.group_list.count(), 1)
        self.page.group_list.setCurrentRow(0); self.wait_tasks()
        self.page.items.setCurrentRow(0)
        self.assertIn('example.asmkey', self.page.details.toPlainText())
        self.assertIn('类型：文件', self.page.details.toPlainText())
        self.assertIn('状态：可选择', self.page.details.toPlainText())
        self.assertNotIn('DO-NOT-PREVIEW', self.page.details.toPlainText())
        with patch('ai_session_migration.recovery_gui.QFileDialog.getOpenFileNames', return_value=([str(self.base / 'selected.asmkey')], '')):
            self.page.choose_keys(); self.wait_tasks()
        self.assertEqual(len(self.page.catalog.selected_keys), 1)
        self.assertNotIn('selected.asmkey', self.page.key_info.text())

    def test_preview_ack_gate_invalidation_restore_and_saved_record(self):
        self.choose_project()
        self.page.items.item(1).setCheckState(Qt.CheckState.Checked)
        self.page.target.setText(str(self.base / 'target'))
        self.page.backup.setText(str(self.base / 'target-backups'))
        calls = []
        def build(catalog, item_ids, group_ids, target, backup, **kwargs):
            calls.append(('build', item_ids, group_ids))
            return dict(id='plan', target=str(target), backupRoot=str(backup), new=1, skip=1,
                blocked=1, bytes=10, selected=3, database='fixture.sqlite')
        def rows(plan, offset=0, limit=200):
            return [dict(id=str(i), path=f'file-{i}', destination=f'target/file-{i}', status=status,
                reason='fixture reason', size=10) for i, status in enumerate(['new', 'skip', 'blocked'])]
        def execute(catalog, plan, **kwargs):
            calls.append(('execute',))
            report = dict(schema='asm-file-restore-1', status='complete', journal=str(self.base / 'report.json'),
                output=plan['target'], backup=plan['backupRoot'], restored=1, skipped=1, blocked=1,
                backupVerified=True, readbackVerified=True, nativeClientRestored=False)
            Path(report['journal']).write_text(json.dumps(report))
            return report
        api = types.SimpleNamespace(build_file_plan=build, plan_rows=rows, execute_file_restore=execute,
            check_file_restore=lambda report, **kwargs: dict(report, backupVerified=True, readbackVerified=True))
        with patch.dict('sys.modules', {'ai_session_migration.file_restore': api}):
            self.page.preview(); self.wait_tasks()
            self.assertEqual(self.page.plan_list.count(), 3)
            self.assertFalse(self.page.restore_button.isEnabled())
            self.page.restore()
            self.assertEqual(len(calls), 1)
            self.page.ack.setChecked(True)
            self.assertTrue(self.page.restore_button.isEnabled())
            self.page.target.setText(str(self.base / 'other-target'))
            self.assertIsNone(self.page.plan)
            self.assertFalse(self.page.ack.isChecked())
            self.page.preview(); self.wait_tasks()
            self.page.ack.setChecked(True); self.page.restore(); self.wait_tasks()
            self.page.check_record(); self.wait_tasks()
        self.assertEqual(calls[-1], ('execute',))
        self.assertEqual(len(self.owner.state.data['fileRestores']), 1)
        self.assertEqual(self.page.tabs.currentIndex(), 2)
        self.assertIn('记录检查完成', self.page.log.toPlainText())
        self.assertIn('原生客户端恢复：未执行', self.page.record_details.toPlainText())

    def test_cancel_index_busy_guard_and_missing_directory(self):
        self.page.group_list.setCurrentRow(0)
        started = threading.Event()
        def blocked_index(group_id, progress, cancel):
            started.set()
            while not cancel.wait(.01):
                pass
            raise InterruptedError('已取消索引')
        self.page.catalog.index_group = blocked_index
        self.page.index_group()
        self.assertTrue(self.page.busy)
        self.assertFalse(self.page.preview_button.isEnabled())
        self.assertFalse(self.page.root_button.isEnabled())
        deadline = time.monotonic() + 2
        while not started.is_set() and time.monotonic() < deadline:
            APP.processEvents(); time.sleep(.005)
        self.page.cancel_task(); self.wait_tasks()
        self.assertFalse(self.owner.busy)
        self.assertIn('已取消索引', self.page.stage.text())
        self.page.root_field.setText(str(self.base / 'missing-volume'))
        self.page.load_root()
        self.assertIn('不可访问', self.page.stage.text())

    def test_cache_settings_reuses_owner_routes_busy_guard_and_selected_cache(self):
        with patch.object(self.owner, 'show_settings') as settings:
            self.page.cache_button.click()
            settings.assert_called_once_with()
            self.page.busy = self.owner.busy = True
            self.page.update_controls()
            self.assertFalse(self.page.cache_button.isEnabled())
            self.page.cache_button.click()
            self.page.cache_settings()
            settings.assert_called_once_with()
            self.page.busy = self.owner.busy = False
            self.page.update_controls()
        selected_cache = self.base / 'user-selected-cache'
        def change_cache():
            self.owner.state.data['cache'] = str(selected_cache)
        with patch.object(self.owner, 'show_settings', side_effect=change_cache):
            self.page.cache_button.click(); self.wait_tasks()
        self.assertEqual(self.page.catalog.cache, selected_cache / 'recovery')
        self.assertIn(str(selected_cache), self.page.cache_note.text())
        self.assertIn('移动磁盘', self.page.cache_note.text())

    def test_rollback_requires_confirmation_and_preserves_record(self):
        from PySide6.QtWidgets import QMessageBox
        report = dict(schema='asm-file-restore-1', status='complete', journal=str(self.base / 'report.json'),
            output=str(self.base / 'run'), target=str(self.base / 'target'), restored=1)
        self.page.save_report(report)
        api = types.SimpleNamespace(rollback_file_restore=lambda value, **kwargs: dict(value, status='rolled-back', rollbackVerified=True))
        with patch.dict('sys.modules', {'ai_session_migration.file_restore': api}):
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
                self.page.rollback()
                self.assertFalse(self.owner.tasks)
                self.assertEqual(self.owner.state.data['fileRestores'][0]['status'], 'complete')
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                self.page.rollback(); self.wait_tasks()
        self.assertEqual(len(self.owner.state.data['fileRestores']), 1)
        self.assertTrue(self.owner.state.data['fileRestores'][0]['rollbackVerified'])

    def test_shared_native_package_helper_opens_synthetic_portable(self):
        from fixtures import make_home
        from ai_session_migration.adapters import LocalLibrary
        from ai_session_migration.backup import make_backup, save_recovery_key
        from ai_session_migration.packages import PackageLibrary
        from test_core import fingerprints
        home = self.base / 'synthetic-home'; make_home(home)
        before = fingerprints(home)
        library = LocalLibrary(home, self.base / 'session-cache')
        catalog = library.scan()
        key = self.base / 'example.asmkey'; save_recovery_key(key)
        report = make_backup(library, catalog.records, self.base / 'exports', key)
        self.owner.open_package_path(report['portable']); self.wait_tasks()
        self.assertIsInstance(self.owner.library, PackageLibrary)
        self.assertEqual(len(self.owner.records), 3)
        self.assertEqual(before, fingerprints(home))
        self.assertEqual(self.owner.stack.currentWidget(), self.owner.pages['library'])

    def test_real_catalog_navigation_indexes_generic_zip_in_background(self):
        import zipfile
        bundle = self.base / 'bundle'; bundle.mkdir()
        with zipfile.ZipFile(bundle / 'project.zip', 'w') as archive:
            archive.writestr('folder/', '')
            archive.writestr('folder/file.txt', 'generic source-preserving fixture')
        self.owner.navigate('recovery')
        real = self.owner.recovery_center
        real.root_field.setText(str(bundle)); real.load_root(); self.wait_tasks()
        self.assertTrue(real.groups)
        group = next(g for g in real.groups if g['category'] == 'projects')
        for i in range(real.group_list.count()):
            if real.group_list.item(i).data(Qt.ItemDataRole.UserRole)['id'] == group['id']:
                real.group_list.setCurrentRow(i)
                break
        real.index_group(); self.wait_tasks()
        self.assertGreater(real.items.count(), 0)
        self.assertLessEqual(real.items.count(), real.PAGE_SIZE)
        paths = [real.items.item(i).data(Qt.ItemDataRole.UserRole)['path'] for i in range(real.items.count())]
        self.assertIn('folder/file.txt', paths)
        self.assertTrue((bundle / 'project.zip').is_file())
        real.catalog.close()

    def test_real_file_recovery_preview_backup_check_and_protected_rollback(self):
        import zipfile
        from PySide6.QtWidgets import QMessageBox
        from ai_session_migration.file_restore import plan_rows
        bundle = self.base / 'real-bundle'; bundle.mkdir()
        source = bundle / 'project.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('folder/', '')
            archive.writestr('folder/existing.txt', 'source value must never overwrite target')
            archive.writestr('folder/undo.txt', 'chosen original bytes')
            archive.writestr('folder/protected.txt', 'chosen protected bytes')
            archive.writestr('unselected.txt', 'this file was not chosen')
        source_before = source.read_bytes()
        target = self.base / 'real-target'; target.mkdir()
        (target / 'keep.txt').write_text('existing target data')
        self.owner.navigate('recovery')
        real = self.owner.recovery_center
        real.root_field.setText(str(bundle)); real.load_root(); self.wait_tasks()
        group_id = next(group['id'] for group in real.groups if group['category'] == 'projects')
        for i in range(real.group_list.count()):
            if real.group_list.item(i).data(Qt.ItemDataRole.UserRole)['id'] == group_id:
                real.group_list.setCurrentRow(i)
                break
        real.index_group(); self.wait_tasks()
        folder_item = next(real.items.item(i) for i in range(real.items.count())
            if real.items.item(i).data(Qt.ItemDataRole.UserRole)['kind'] == 'directory')
        folder_item.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(len(real.selected_items), 1)
        real.target.setText(str(target)); real.backup.setText(str(self.base / 'real-target-backups'))
        real.preview(); self.wait_tasks()
        self.assertIsNotNone(real.plan, real.stage.text())
        rows = plan_rows(real.plan, limit=100)
        destinations = {Path(row['path']).name: Path(row['destination']) for row in rows}
        self.assertNotIn('unselected.txt', destinations)
        existing = destinations['existing.txt']; existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text('target value must survive')
        # Recompute the preview after an explicit target change, before executing.
        real.preview(); self.wait_tasks()
        self.assertIsNotNone(real.plan, real.stage.text())
        self.assertGreater(real.plan['skip'], 0)
        self.assertFalse(real.restore_button.isEnabled())
        real.ack.setChecked(True); real.restore_button.click(); self.wait_tasks()
        report = self.owner.state.data['fileRestores'][0]
        self.assertEqual(report['status'], 'complete', real.stage.text())
        self.assertTrue(report['backupVerified'])
        self.assertTrue(report['readbackVerified'])
        self.assertFalse(report['nativeClientRestored'])
        self.assertEqual((target / 'keep.txt').read_text(), 'existing target data')
        self.assertEqual(existing.read_text(), 'target value must survive')
        self.assertEqual(destinations['undo.txt'].read_text(), 'chosen original bytes')
        self.assertEqual(destinations['protected.txt'].read_text(), 'chosen protected bytes')
        self.assertFalse(list(target.rglob('unselected.txt')))
        backup = Path(report['backup'])
        self.assertEqual((backup / 'keep.txt').read_text(), 'existing target data')
        self.assertEqual((backup / existing.relative_to(target)).read_text(), 'target value must survive')
        real.check_record(); self.wait_tasks()
        report = self.owner.state.data['fileRestores'][0]
        self.assertTrue(report['readbackVerified'])
        self.assertEqual(report['pendingFiles'], 0)
        self.assertIn('最近备份复核：已通过', real.record_details.toPlainText())
        (backup / 'keep.txt').write_text('tampered backup fixture')
        real.check_record(); self.wait_tasks()
        report = self.owner.state.data['fileRestores'][0]
        self.assertTrue(report['backupVerified'])
        self.assertFalse(report['backupRecheckVerified'])
        self.assertIn('初次备份校验：已通过', real.record_details.toPlainText())
        self.assertIn('最近备份复核：未通过', real.record_details.toPlainText())
        self.assertIn('需要检查', real.record_list.item(0).text())
        self.assertNotIn('needs-attention', real.record_list.item(0).text())
        (backup / 'keep.txt').write_text('existing target data')
        real.check_record(); self.wait_tasks()
        self.assertIn('最近备份复核：已通过', real.record_details.toPlainText())
        destinations['protected.txt'].write_text('subsequent user edit')
        real.check_record(); self.wait_tasks()
        report = self.owner.state.data['fileRestores'][0]
        self.assertFalse(report['readbackVerified'])
        self.assertGreater(report['pendingFiles'], 0)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            real.rollback(); self.wait_tasks()
        report = self.owner.state.data['fileRestores'][0]
        self.assertIn(str(destinations['protected.txt']), report['protectedUserChanges'])
        self.assertIn('用户后续修改：已保护 1 个文件', real.record_details.toPlainText())
        self.assertFalse(destinations['undo.txt'].exists())
        self.assertEqual(destinations['protected.txt'].read_text(), 'subsequent user edit')
        self.assertEqual(existing.read_text(), 'target value must survive')
        self.assertEqual((target / 'keep.txt').read_text(), 'existing target data')
        self.assertEqual(source.read_bytes(), source_before)
        real.catalog.close()

    def test_record_statuses_are_chinese_and_unchecked_backup_is_distinct(self):
        for status, display in [('complete', '已完成'), ('cancelled', '已取消'),
                ('needs-attention', '需要检查'), ('rolled-back', '已安全回滚')]:
            self.page.save_report(dict(schema='asm-file-restore-1', status=status,
                journal=str(self.base / f'{status}.json'), target=str(self.base / 'target'),
                backupVerified=True, readbackVerified=False, nativeClientRestored=False))
            self.assertIn(display, self.page.record_list.item(0).text())
            self.assertIn(f'状态：{display}', self.page.record_details.toPlainText())
            self.assertIn('最近备份复核：尚未复核', self.page.record_details.toPlainText())
            self.assertIn('恢复内容回读：未通过', self.page.record_details.toPlainText())
            self.assertIn(f'状态：{display}', self.page.log.toPlainText())

    def test_native_button_routes_existing_package_and_missing_package_is_visible(self):
        package = self.base / 'portable.zip'; package.write_bytes(b'fixture')
        self.page.current_group = dict(id='sessions', native_archive=str(package))
        with patch.object(self.owner, 'open_package_path') as open_package:
            self.page.open_native(); open_package.assert_called_once_with(str(package))
        package.unlink(); self.page.open_native()
        self.assertIn('不可访问', self.page.stage.text())


if __name__ == '__main__':
    unittest.main()

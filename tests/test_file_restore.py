"""Recovery boundaries using synthetic payloads and isolated temporary folders."""
from __future__ import annotations

from contextlib import contextmanager, closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from ai_session_migration import file_restore as restore
from ai_session_migration.recovery_catalog import RecoveryCatalog


def sha(data): return hashlib.sha256(data).hexdigest()


class FakeCatalog:
    def __init__(self, base):
        self.root = base / 'source'; self.root.mkdir()
        self.bundle_root = self.root; self.cache = base / 'cache'; self.cache.mkdir()
        self.items = {}; self.payloads = {}
    def add(self, name, data=b'', *, kind='file', status='ready', size=None, checksum=None, group='group'):
        iid = 'item-' + str(len(self.items))
        self.items[iid] = {'id': iid, 'group_id': group, 'path': name, 'original_path': '', 'kind': kind,
                           'size': len(data) if size is None else size,
                           'sha256': (None if kind == 'directory' else sha(data)) if checksum is None else checksum,
                           'category': 'projects', 'status': status, 'detail': ''}
        self.payloads[iid] = data
        return iid
    def get_item(self, iid): return dict(self.items[iid])
    def iter_items(self, item_ids=(), group_ids=()):
        selected = set(item_ids)
        for iid in item_ids:
            item = self.items[iid]
            if item['kind'] == 'directory':
                selected.update(key for key, r in self.items.items() if r['group_id'] == item['group_id'] and r['path'].startswith(item['path'] + '/'))
        for iid, item in self.items.items():
            if iid in selected or item['group_id'] in group_ids: yield dict(item)
    @contextmanager
    def open_item(self, iid):
        with io.BytesIO(self.payloads[iid]) as f: yield f


class FileRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.base = Path(self.temporary.name).resolve()
        self.catalog = FakeCatalog(self.base)
        self.target = self.base / 'target'; self.backup = self.base / 'backups'
    def tearDown(self): self.temporary.cleanup()
    def plan(self, items=(), groups=(), target=None, backup=None):
        return restore.build_file_plan(self.catalog, items, groups, target or self.target, backup or self.backup)
    def destinations(self, plan): return {r['id']: Path(r['destination']) for r in restore.plan_rows(plan, limit=1000)}
    def journal(self): return next(self.backup.glob('*/report.json'))

    def test_selected_directory_group_dedup_bytes_and_unchosen_absent(self):
        folder = self.catalog.add('selected', kind='directory')
        a = self.catalog.add('selected/a.txt', b'alpha')
        b = self.catalog.add('selected/b.txt', b'beta')
        unchosen = self.catalog.add('elsewhere.txt', b'no', group='other')
        plan = self.plan([folder, a], ['group'])
        self.assertEqual((plan['selected'], plan['bytes'], plan['blocked']), (3, 9, 0))
        self.assertEqual(len(restore.plan_rows(plan, offset=1, limit=1)), 1)
        report = restore.execute_file_restore(self.catalog, plan)
        self.assertEqual(report['status'], 'complete'); self.assertEqual(report['restored'], 2)
        self.assertTrue(report['backupVerified']); self.assertTrue(report['readbackVerified'])
        paths = self.destinations(plan)
        self.assertEqual(paths[a].read_bytes(), b'alpha'); self.assertEqual(paths[b].read_bytes(), b'beta')
        self.assertNotIn(unchosen, paths)
        self.assertEqual(list(self.target.rglob('elsewhere.txt')), [])
        self.assertFalse(report['sourceModified']); self.assertFalse(report['originalTargetFilesOverwritten'])
        self.assertEqual(restore.check_file_restore(report)['status'], 'complete')

    def test_existing_leaf_skipped_complete_target_backup_untouched(self):
        old = self.catalog.add('same.txt', b'new replacement')
        fresh = self.catalog.add('fresh.txt', b'new')
        self.target.mkdir(); group = self.target / '资料-group'; group.mkdir()
        same = group / 'same.txt'; same.write_bytes(b'old original')
        unrelated = self.target / 'unrelated' / 'notes.txt'; unrelated.parent.mkdir(); unrelated.write_bytes(b'keep all target files')
        initial = {p.relative_to(self.target).as_posix(): p.read_bytes() for p in self.target.rglob('*') if p.is_file()}
        plan = self.plan([old, fresh]); rows = {r['id']: r for r in restore.plan_rows(plan)}
        self.assertEqual(rows[old]['status'], 'skip'); self.assertEqual(plan['skip'], 1)
        report = restore.execute_file_restore(self.catalog, plan)
        backup = Path(report['backup'])
        for name, data in initial.items():
            self.assertEqual((self.target / name).read_bytes(), data)
            self.assertEqual((backup / name).read_bytes(), data)
        self.assertEqual(report['restored'], 1)
        self.assertEqual(restore.check_file_restore(report)['backupRecheckVerified'], True)
        undone = restore.rollback_file_restore(report)
        self.assertEqual(undone['status'], 'rolled-back')
        for name, data in initial.items(): self.assertEqual((self.target / name).read_bytes(), data)

    def test_preview_target_creation_content_and_addition_changes_rejected(self):
        item = self.catalog.add('a.txt', b'a')
        plan = self.plan([item]); self.target.mkdir()
        with self.assertRaisesRegex(ValueError, '变化'): restore.execute_file_restore(self.catalog, plan)
        existing = self.target / 'existing.txt'; existing.write_bytes(b'before')
        plan = self.plan([item]); existing.write_bytes(b'after')
        with self.assertRaisesRegex(ValueError, '变化'): restore.execute_file_restore(self.catalog, plan)
        plan = self.plan([item]); (self.target / 'unpreviewed.txt').write_bytes(b'new')
        with self.assertRaisesRegex(ValueError, '变化'): restore.execute_file_restore(self.catalog, plan)
        self.assertFalse(self.backup.exists()); self.assertEqual(existing.read_bytes(), b'after')

    def test_bad_source_sha_short_and_long_size_never_publish(self):
        for index, kwargs in enumerate([{'checksum': '0' * 64}, {'size': 1}, {'size': 100}]):
            with self.subTest(kwargs=kwargs):
                item = self.catalog.add('bad-' + str(index), b'payload', **kwargs)
                plan = self.plan([item], target=self.base / ('target-' + str(index)))
                destination = self.destinations(plan)[item]
                with self.assertRaises(restore.RestoreFailure) as context: restore.execute_file_restore(self.catalog, plan)
                self.assertFalse(destination.exists()); self.assertEqual(context.exception.report['restored'], 0)
                self.assertTrue(context.exception.report['backupVerified'])
                self.assertFalse(list(Path(plan['target']).rglob('*.part')))

    def test_windows_encoding_case_unicode_and_parent_conflicts(self):
        names = ['folder/Name.txt', 'folder/name.TXT', 'CON.txt', 'bad:name?.txt', '../escape', '/absolute',
                 'C:/absolute', 'same', 'same/child', 'café.txt', 'cafe\u0301.txt']
        ids = [self.catalog.add(name, name.encode()) for name in names]
        plan = self.plan(ids); rows = {r['id']: r for r in restore.plan_rows(plan)}
        for index in (1, 4, 5, 6, 8, 10): self.assertEqual(rows[ids[index]]['status'], 'blocked')
        self.assertEqual(rows[ids[2]]['status'], 'new'); self.assertIn('~005FCON.txt', rows[ids[2]]['path'])
        self.assertEqual(rows[ids[3]]['status'], 'new'); self.assertIn('~003A', rows[ids[3]]['path'])
        report = restore.execute_file_restore(self.catalog, plan)
        self.assertEqual(report['restored'], plan['new'])
        self.assertFalse((self.base / 'escape').exists())

    def test_legal_tilde_name_unchanged_and_encoded_name_collision_blocked(self):
        legal=self.catalog.add('~valid-module.py',b'original import name')
        literal=self.catalog.add('name~003Avalue.txt',b'literal spelling')
        encoded=self.catalog.add('name:value.txt',b'invalid Windows spelling')
        plan=self.plan([legal,literal,encoded])
        rows={r['id']:r for r in restore.plan_rows(plan)}
        self.assertTrue(rows[legal]['path'].replace('\\','/').endswith('/~valid-module.py'))
        self.assertEqual(rows[literal]['status'],'new')
        self.assertEqual(rows[encoded]['status'],'blocked')
        self.assertIn('编码',rows[encoded]['reason'])
        restore.execute_file_restore(self.catalog,plan)
        self.assertEqual(Path(rows[legal]['destination']).read_bytes(),b'original import name')
        self.assertEqual(Path(rows[literal]['destination']).read_bytes(),b'literal spelling')

    def test_symlink_descriptor_and_existing_target_link_rejected(self):
        descriptor = b'{"schema":"asm-preserved-symlink-1","target":"/outside","followed":false}\n'
        item = self.catalog.add('shortcut', descriptor, kind='symlink')
        plan = self.plan([item]); destination = self.destinations(plan)[item]
        self.assertTrue(str(destination).endswith('.symlink.json'))
        restore.execute_file_restore(self.catalog, plan)
        self.assertFalse(destination.is_symlink()); self.assertEqual(destination.read_bytes(), descriptor)
        outside = self.base / 'outside'; outside.mkdir(); (outside / 'keep').write_bytes(b'keep')
        linked = self.base / 'linked-target'; linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '链接'): self.plan([item], target=linked)
        nested = self.base / 'nested-target'; nested.mkdir(); (nested / 'escape').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '链接'): self.plan([item], target=nested)
        self.assertEqual((outside / 'keep').read_bytes(), b'keep')

    def test_location_overlaps_live_client_and_network_paths_rejected(self):
        item = self.catalog.add('file', b'x')
        for target, backup in [(self.catalog.root / 'out', self.backup), (self.catalog.cache / 'out', self.backup),
                               (self.target, self.target / 'backups'), (self.target / 'out', self.target),
                               (self.target, self.catalog.root / 'backup'), (self.target, self.catalog.cache / 'backup')]:
            with self.subTest(target=target, backup=backup), self.assertRaises(ValueError): self.plan([item], target=target, backup=backup)
        fake_home = self.base / 'home'; fake_home.mkdir()
        with patch('ai_session_migration.file_restore.Path.home', return_value=fake_home):
            for name in ('.claude', '.codex', '.dsh', 'Library/Application Support/Claude', 'Library/Application Support/Codex'):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, '原生会话'): self.plan([item], target=fake_home / name / 'data')
        with self.assertRaises(ValueError): self.plan([item], target=Path('//server/share/target'))

    def test_low_space_rejected_before_target_or_backup_writes(self):
        item = self.catalog.add('file', b'payload'); plan = self.plan([item])
        with patch.object(restore, '_parent_space', return_value=(0, 1)):
            with self.assertRaisesRegex(ValueError, '空间不足'): restore.execute_file_restore(self.catalog, plan)
        self.assertFalse(self.target.exists()); self.assertFalse(self.backup.exists())

    def test_cancel_after_first_publication_check_and_safe_rollback(self):
        a = self.catalog.add('a.txt', b'a'); b = self.catalog.add('b.txt', b'b')
        self.catalog.add('unchosen.txt', b'no')
        plan = self.plan([a, b]); destinations = self.destinations(plan); cancel = threading.Event()
        def progress(stage, percent):
            if stage == '逐项恢复并回读校验': cancel.set()
        with self.assertRaises(restore.RestoreFailure) as context: restore.execute_file_restore(self.catalog, plan, progress, cancel)
        report = context.exception.report
        self.assertEqual(report['status'], 'cancelled'); self.assertTrue(destinations[a].exists()); self.assertFalse(destinations[b].exists())
        checked = restore.check_file_restore(report); self.assertEqual(checked['pendingFiles'], 1)
        result = restore.rollback_file_restore(report)
        self.assertEqual(result['rollbackRemovedFiles'], 1); self.assertEqual(result['status'], 'rolled-back')
        self.assertFalse(destinations[a].exists()); self.assertEqual(list(self.base.rglob('unchosen.txt')), [])

    def test_crash_after_atomic_publish_persistent_check_reconciles(self):
        item = self.catalog.add('published.txt', b'recovered'); plan = self.plan([item]); destination = self.destinations(plan)[item]
        original = restore.rename_new
        def crash_after_rename(source, target):
            original(source, target)
            raise SystemExit('simulated process termination')
        with patch.object(restore, 'rename_new', side_effect=crash_after_rename):
            with self.assertRaises(SystemExit): restore.execute_file_restore(self.catalog, plan)
        self.assertEqual(destination.read_bytes(), b'recovered')
        record = json.loads(self.journal().read_text())
        with closing(sqlite3.connect(plan['database'])) as db:
            self.assertEqual(db.execute('SELECT phase FROM rows WHERE id=?', (item,)).fetchone()[0], 'publishing')
        checked = restore.check_file_restore(record)
        self.assertEqual(checked['status'], 'complete'); self.assertEqual(checked['checkedPublishedFiles'], 1)
        self.assertEqual(restore.rollback_file_restore(checked)['rollbackRemovedFiles'], 1)
        self.assertFalse(destination.exists())

    def test_check_detects_changed_target_backup_payload_and_index(self):
        item = self.catalog.add('new', b'new'); self.target.mkdir(); (self.target / 'original').write_bytes(b'keep')
        report = restore.execute_file_restore(self.catalog, self.plan([item]))
        backup = Path(report['backup']); (backup / 'original').write_bytes(b'changed')
        checked = restore.check_file_restore(report)
        self.assertFalse(checked['backupRecheckVerified']); self.assertEqual(checked['status'], 'needs-attention')
        (backup / 'original').write_bytes(b'keep')
        with (Path(report['output']) / 'backup-index.sqlite').open('ab') as f: f.write(b'tampered-index')
        self.assertFalse(restore.check_file_restore(report)['backupRecheckVerified'])

    def test_external_report_tamper_no_redirect_and_path_only_rollback_rejected(self):
        item = self.catalog.add('own.txt', b'own'); plan = self.plan([item]); report = restore.execute_file_restore(self.catalog, plan)
        outside = self.base / 'outside'; outside.mkdir(); victim = outside / 'victim'; victim.write_bytes(b'keep')
        original = dict(report); tampered = dict(report); tampered['target'] = str(outside)
        Path(report['journal']).write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, '不一致'): restore.rollback_file_restore(original)
        self.assertEqual(victim.read_bytes(), b'keep'); self.assertTrue(self.destinations(plan)[item].exists())
        Path(report['journal']).write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, '外部日志'): restore.rollback_file_restore(report['journal'])

    def test_user_modified_restored_files_preserved_rollback_other_files(self):
        a = self.catalog.add('a', b'original-a'); b = self.catalog.add('b', b'original-b')
        plan = self.plan([a, b]); report = restore.execute_file_restore(self.catalog, plan); destinations = self.destinations(plan)
        destinations[a].write_bytes(b'user change')
        checked = restore.check_file_restore(report); self.assertEqual(checked['status'], 'needs-attention')
        result = restore.rollback_file_restore(report)
        self.assertEqual(result['rollbackRemovedFiles'], 1); self.assertEqual(result['status'], 'needs-attention')
        self.assertEqual(destinations[a].read_bytes(), b'user change'); self.assertFalse(destinations[b].exists())
        self.assertIn(str(destinations[a]), result['protectedUserChanges'])

    def test_real_recovery_catalog_zip_to_generic_restore(self):
        archive = self.catalog.root / 'project.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('folder/chosen.txt', b'chosen bytes'); z.writestr('other/unchosen.txt', b'not selected')
        real = RecoveryCatalog(self.catalog.root, self.catalog.cache / 'real')
        try:
            group = next(g for g in real.discover() if g['label'] == 'project.zip'); real.index_group(group['id'])
            selected = next(r for r in real.list_items(group['id']) if r['path'] == 'folder')
            plan = restore.build_file_plan(real, [selected['id']], [], self.target, self.backup)
            self.assertEqual(plan['bytes'], len(b'chosen bytes'))
            report = restore.execute_file_restore(real, plan)
            self.assertEqual(report['status'], 'complete'); self.assertEqual(report['restored'], 1)
            self.assertEqual(len(list(self.target.rglob('chosen.txt'))), 1)
            self.assertFalse(list(self.target.rglob('unchosen.txt')))
            self.assertEqual(restore.rollback_file_restore(report)['status'], 'rolled-back')
        finally: real.close()


if __name__ == '__main__': unittest.main()

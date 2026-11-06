"""Synthetic only: no user paths, payloads or recovery keys."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from ai_session_migration.backup import encrypt_archive, save_recovery_key
from ai_session_migration.recovery_catalog import RecoveryCatalog
from ai_session_migration import archive_io


def digest(data): return hashlib.sha256(data).hexdigest()

def write_zip(path, files):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            # Model the source Mac ZIP, not Windows ZipInfo separator rewriting.
            info.filename = name; info.orig_filename = name
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, json.dumps(data).encode() if isinstance(data, (dict, list)) else data)
    return path


class RecoveryCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'bundle'; self.root.mkdir()
        self.key = self.root / 'recovery.asmkey'; save_recovery_key(self.key)
        self.catalogs = []
    def tearDown(self):
        for c in self.catalogs: c.close()
        self.temp.cleanup()
    def cat(self, root=None, keys=None):
        c = RecoveryCatalog(root or self.root, self.base / ('cache' + str(len(self.catalogs))), keys)
        self.catalogs.append(c); c.discover(); return c
    def enc(self, name, files):
        plain = self.base / 'temporary.zip'; write_zip(plain, files)
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
        encrypt_archive(plain, path, self.key, lambda *_: None, threading.Event())
        plain.unlink(); return path
    def group(self, cat, name): return next(g for g in cat.groups() if name in g['label'])
    def read(self, cat, item):
        with cat.open_item(item['id']) as stream:
            return b''.join(iter(lambda: stream.read(127), b''))
    def manifest(self, entries, schema='conversation-migration-1'):
        return {'schema': schema, 'entries': entries}
    def entry(self, name, data, **kw):
        return {'root': 'workspace', 'path': name, 'object': 'objects/' + name, 'capturedSize': len(data), 'sha256': digest(data), **kw}

    def test_global_full_volumes_and_thread_pagination_directory_selection(self):
        a = self.entry('dir/a.txt', b'alpha'); b = self.entry('dir/b.txt', b'beta')
        self.enc('full-preservation-vol01.zip.asmb', {a['object']: b'alpha'})
        self.enc('full-preservation-vol02.zip.asmb', {b['object']: b'beta', 'manifest.json': self.manifest([{'root': 'workspace', 'path': 'dir', 'type': 'directory'}, a, b])})
        c = self.cat(); g = self.group(c, 'full-preservation')
        with ThreadPoolExecutor(max_workers=1) as pool: result = pool.submit(c.index_group, g['id']).result()
        self.assertEqual(result['status'], 'ready'); self.assertEqual(c.count_items(g['id']), 4)
        self.assertEqual(len(c.list_items(g['id'], limit=1, offset=1)), 1)
        directory = next(r for r in c.list_items(g['id']) if r['kind'] == 'directory')
        selected = list(c.iter_items([directory['id']], [g['id']]))
        self.assertEqual(len(selected), 4)
        self.assertEqual(sum(r['size'] for r in selected), 9)
        self.assertEqual({self.read(c, r) for r in selected if r['kind'] == 'file'}, {b'alpha', b'beta'})

    def test_missing_volume_visible_not_payload_object_flattening(self):
        entry = self.entry('src/original.py', b'x')
        self.enc('full-preservation-vol02.zip.asmb', {'manifest.json': self.manifest([entry])})
        c = self.cat(); g = self.group(c, 'full-preservation'); c.index_group(g['id'])
        item = next(r for r in c.list_items(g['id']) if r['kind'] == 'file')
        self.assertEqual(item['path'], 'workspace/src/original.py'); self.assertEqual(item['status'], 'missing')

    def test_encrypted_corruption_rejected_before_manifest(self):
        path = self.enc('sessions.asmb', {'manifest.json': b'{invalid'})
        with path.open('r+b') as f:
            f.seek(-18, 2); old = f.read(1); f.seek(-1, 1); f.write(bytes([old[0] ^ 1]))
        c = self.cat(); g = self.group(c, 'sessions')
        with self.assertRaisesRegex(ValueError, '认证失败'): c.index_group(g['id'])
        self.assertEqual(c.count_items(g['id']), 0)

    def test_missing_wrong_then_explicit_key(self):
        self.enc('sessions.asmb', {'manifest.json': self.manifest([])})
        key = self.base / 'picked.asmkey'; self.key.rename(key)
        wrong = self.root / 'wrong.asmkey'; save_recovery_key(wrong)
        c = self.cat(); g = self.group(c, 'sessions')
        self.assertEqual(c.index_group(g['id'])['status'], 'key-required')
        c.keys = [key]
        self.assertEqual(c.index_group(g['id'])['status'], 'ready')

    def test_source_changed_after_index_rejected(self):
        path = write_zip(self.root / 'project.zip', {'file.txt': b'abc'})
        c = self.cat(); g = self.group(c, 'project.zip'); c.index_group(g['id']); item = c.list_items(g['id'])[0]
        path.touch()
        with self.assertRaisesRegex(ValueError, '改变'): self.read(c, item)

    def test_original_sha_and_crc_checked(self):
        entry = self.entry('file', b'original')
        write_zip(self.root / 'sessions.zip', {'manifest.json': self.manifest([entry]), entry['object']: b'changed!'})
        c = self.cat(); g = self.group(c, 'sessions'); c.index_group(g['id'])
        with self.assertRaisesRegex(ValueError, '校验失败'): self.read(c, next(r for r in c.list_items(g['id']) if r['kind'] == 'file'))

    def test_unsafe_path_and_uncaptured_visible(self):
        write_zip(self.root / 'project.zip', {'../escape': b'bad', 'safe.txt': b'ok', '._mac': b'metadata'})
        c = self.cat(); g = self.group(c, 'project.zip'); c.index_group(g['id'])
        rows = {r['path']: r for r in c.list_items(g['id'])}
        self.assertEqual(rows['../escape']['status'], 'unsupported'); self.assertEqual(rows['._mac']['status'], 'unsupported')
        with self.assertRaises(ValueError): self.read(c, rows['../escape'])

    def test_symlink_json_and_not_captured_records(self):
        entries = [{'root': 'project', 'path': 'shortcut', 'type': 'symlink', 'target': '/outside/private', 'size': 99},
                   {'root': 'project', 'path': 'live.db-wal', 'capture': 'included-in-consistent-sqlite-main-snapshot', 'size': 99},
                   {'root': 'project', 'path': 'socket', 'type': 'runtime-special'}]
        self.enc('full-preservation.asmb', {'manifest.json': self.manifest(entries)})
        c = self.cat(); g = self.group(c, 'full-preservation'); c.index_group(g['id']); rows = c.list_items(g['id'])
        symlink = next(r for r in rows if r['kind'] == 'symlink'); data = self.read(c, symlink)
        self.assertEqual(len(data), symlink['size']); self.assertEqual(digest(data), symlink['sha256'])
        self.assertFalse(json.loads(data)['followed'])
        self.assertEqual({r['status'] for r in rows}, {'ready', 'unsupported', 'not-captured'})

    def test_old_sessions_reference_moved_bundle(self):
        data = b'latest'; older = b'older'
        e1 = self.entry('a', data); e1['size'] = e1.pop('capturedSize')
        e2 = self.entry('b', older); e2['size'] = e2.pop('capturedSize')
        write_zip(self.root / 'sessions.zip', {'objects/latest': data, 'manifest.json': self.manifest([])})
        self.enc('old-session-delta.asmb', {'original-manifest.json': self.manifest([e1, e2]), e2['object']: older,
            'coverage.json': {'schema': 'asm-old-export-delta-1', 'newSessionPackage': '/old/machine/bundle/sessions.zip', 'mapping': [
                {'oldObject': e1['object'], 'newObject': 'objects/latest', 'source': 'latest-session-package', 'sha256': digest(data), 'bytes': len(data)},
                {'oldObject': e2['object'], 'source': 'this-delta', 'sha256': digest(older), 'bytes': len(older)}]}})
        moved = self.base / 'moved'; shutil.move(self.root, moved)
        c = self.cat(moved); g = self.group(c, 'old-session'); c.index_group(g['id'])
        self.assertEqual({self.read(c, r) for r in c.list_items(g['id']) if r['kind'] != 'directory'}, {data, older})

    def test_failed_delta_full_refs_and_original_manifest_mapping(self):
        data = b'full'; old = b'delta'; entry = self.entry('source.txt', data)
        old_manifest = json.dumps(self.manifest([entry])).encode()
        self.enc('full-preservation-vol01.asmb', {'objects/0000': data})
        self.enc('failed-delta.asmb', {'objects/0001': old_manifest, 'objects/0002': old,
            'coverage.jsonl': b'\n'.join(json.dumps(r).encode() for r in [
                {'batch': 'old-job', 'archive': 'data.zip', 'originalObject': 'manifest.json', 'source': 'this-delta', 'object': 'objects/0001', 'bytes': len(old_manifest), 'sha256': digest(old_manifest)},
                {'batch': 'old-job', 'archive': 'data.zip', 'originalObject': entry['object'], 'source': 'latest-full', 'object': 'objects/0000', 'bytes': len(data), 'sha256': digest(data)},
                {'batch': 'old-job', 'archive': 'full.zip', 'originalObject': 'old.txt', 'source': 'this-delta', 'object': 'objects/0002', 'bytes': len(old), 'sha256': digest(old)}])})
        c = self.cat(); g = self.group(c, 'failed-delta'); c.index_group(g['id']); rows = c.list_items(g['id'])
        source = next(r for r in rows if r['path'].endswith('workspace/source.txt'))
        self.assertEqual(self.read(c, source), data)
        self.assertIn(old, {self.read(c, r) for r in rows if r['kind'] != 'directory'})

    def test_exact_old_full_source_graph_and_missing_reference(self):
        data = b'history'; entry = self.entry('original/f.txt', data)
        self.enc('full-preservation-vol01.asmb', {'objects/000': data})
        write_zip(self.root / 'unused.zip', {'some': b'x'})
        (self.root / 'old-full-manifest.json').write_text(json.dumps(self.manifest([entry])))
        self.enc('old-version.asmb', {'保全来源.json': {'1': '/previous/full-preservation-vol01.asmb'},
            '原完整包对象覆盖.jsonl': json.dumps({'oldObject': entry['object'], 'sourceId': 1, 'object': 'objects/000', 'sha256': digest(data), 'bytes': len(data)}).encode()})
        c = self.cat(); g = self.group(c, 'old-version'); c.index_group(g['id']); row = next(r for r in c.list_items(g['id']) if r['kind'] == 'file')
        self.assertEqual(row['path'], 'workspace/original/f.txt'); self.assertEqual(self.read(c, row), data)
        c.close(); (self.root / 'full-preservation-vol01.asmb').unlink()
        c2 = self.cat(); g = self.group(c2, 'old-version'); c2.index_group(g['id'])
        self.assertEqual(next(r for r in c2.list_items(g['id']) if r['kind'] == 'file')['status'], 'missing')

    def test_databases_snapshots_plain_project_software_keys(self):
        data = b'db-bytes'
        for name, schema in [('database.asmb', 'asm-complete-native-databases-1'), ('snapshot.asmb', 'asm-old-validation-snapshots-1')]:
            self.enc(name, {'000-data.sqlite': data, 'manifest.json': self.manifest([{'file': '000-data.sqlite', 'originalPath': '/source/db.sqlite', 'size': len(data), 'sha256': digest(data)}], schema)})
        (self.root / 'client.dmg').write_bytes(b'software')
        c = self.cat()
        for name in ('database.asmb', 'snapshot.asmb'):
            g = self.group(c, name); c.index_group(g['id']); self.assertEqual(self.read(c, next(r for r in c.list_items(g['id']) if r['kind'] == 'file')), data)
        for category in ('software', 'keys'):
            g = next(g for g in c.groups() if g['category'] == category); c.index_group(g['id'])
            row = c.list_items(g['id'])[0]
            self.assertNotIn(json.loads(self.key.read_text())['key'], json.dumps(row))
            self.assertEqual(row['status'], 'ready')

    def test_bounded_json_records_and_no_unbounded_archive_read(self):
        payload = json.dumps({'schema': 'test', 'entries': [{'n': n, 'unicode': '你好'} for n in range(20000)]}).encode()
        rows = [value for key, value, array in archive_io.json_fields(io.BytesIO(payload)) if array]
        self.assertEqual(len(rows), 20000); self.assertEqual(rows[-1]['n'], 19999)
        with self.assertRaisesRegex(ValueError, '过大'):
            list(archive_io.json_fields(io.BytesIO(b'{"entries":["' + b'x' * (archive_io.MAX_VALUE + 1))))

    def test_missing_manifest_volume_is_explicit(self):
        self.enc('full-preservation-vol01.asmb', {'objects/0000': b'preserved'})
        c = self.cat(); g = self.group(c, 'full-preservation')
        self.assertEqual(c.index_group(g['id'])['status'], 'missing')
        self.assertEqual(c.list_items(g['id'])[0]['kind'], 'notice')

    def test_stored_zip_crc_corruption_is_detected(self):
        p = self.root / 'project.zip'
        with zipfile.ZipFile(p, 'w', zipfile.ZIP_STORED) as z: z.writestr('file.txt', b'original')
        raw = p.read_bytes().replace(b'original', b'modified', 1); p.write_bytes(raw)
        c = self.cat(); g = self.group(c, 'project.zip'); c.index_group(g['id'])
        with self.assertRaises(zipfile.BadZipFile): self.read(c, next(r for r in c.list_items(g['id']) if r['kind'] == 'file'))

    def test_legacy_cbc_authenticated_random_reader(self):
        import hmac
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        data = b'legacy original' * 2000
        plain = self.base / 'legacy.zip'; write_zip(plain, {'manifest.json': self.manifest([self.entry('file', data)]), 'objects/file': data})
        password = b'synthetic-password\n'; key = self.root / 'legacy.recovery.key'; key.write_bytes(password)
        salt = b'12345678'; material = hashlib.pbkdf2_hmac('sha256', password.splitlines()[0], salt, 200000, 48)
        raw = plain.read_bytes(); pad = 16 - len(raw) % 16
        encoder = Cipher(algorithms.AES(material[:32]), modes.CBC(material[32:])).encryptor()
        cipher = b'Salted__' + salt + encoder.update(raw + bytes([pad]) * pad) + encoder.finalize()
        p = self.root / 'legacy.zip.enc'; p.write_bytes(cipher)
        mac = hmac.new(hashlib.sha256(b'backup-auth-v1\0' + password).digest(), cipher, 'sha256').hexdigest()
        auth = self.root / 'backup-auth.json'; auth.write_text(json.dumps({'hmac': mac, 'ciphertextSha256': digest(cipher)}))
        c = self.cat(); g = self.group(c, 'legacy.zip.enc'); self.assertEqual(c.index_group(g['id'])['status'], 'ready')
        self.assertEqual(self.read(c, next(r for r in c.list_items(g['id']) if r['kind'] == 'file')), data)
        # Authentication metadata changing invalidates the pinned source cache.
        auth.write_text(json.dumps({'hmac': '0' * 64, 'ciphertextSha256': digest(cipher)}))
        with self.assertRaisesRegex(ValueError, '改变'): self.read(c, next(r for r in c.list_items(g['id']) if r['kind'] == 'file'))

    def test_manifest_array_key_inside_string_not_mistaken_for_entries(self):
        source = io.BytesIO(json.dumps({'note': '\"entries\":[{\"wrong\":true}]', 'entries': [{'right': True}]}).encode())
        actual = [v for k,v,array in archive_io.json_fields(source) if k == 'entries']
        self.assertEqual(actual, [{'right': True}])

    def test_unknown_schema_is_visible_unsupported(self):
        write_zip(self.root / 'future.zip', {'manifest.json': {'schema': 'future-format', 'entries': []}})
        c = self.cat(); g = self.group(c, 'future.zip')
        self.assertEqual(c.index_group(g['id'])['status'], 'unsupported')
        self.assertEqual(c.count_items(g['id']), 0)

    def test_parent_folder_synthesis_reports_progress_and_cancels_mid_phase(self):
        write_zip(self.root / 'project.zip', {'tree/file-' + str(n): b'x' for n in range(400)})
        c = self.cat(); g = self.group(c, 'project.zip'); event = threading.Event(); stages = []
        def progress(stage, percent):
            if stage == '建立可选择的逻辑文件夹':
                stages.append(stage)
                if len(stages) == 2: event.set()
        with self.assertRaisesRegex(Exception, '取消'): c.index_group(g['id'], progress, event)
        self.assertEqual(len(stages), 2)
        self.assertEqual(c.count_items(g['id']), 0)

    def test_posix_colon_backslash_names_remain_available_and_encode_on_restore(self):
        from ai_session_migration import file_restore
        names = ['folder/time:12.txt', r'folder/name\literal.txt', r'folder/literal\..\name.txt']
        write_zip(self.root / 'project.zip', {name: name.encode() for name in names})
        c = self.cat(); g = self.group(c, 'project.zip'); c.index_group(g['id'])
        rows = [r for r in c.list_items(g['id']) if r['kind'] == 'file']
        self.assertEqual({r['path'] for r in rows}, set(names))
        for r in rows:
            self.assertEqual(r['status'], 'ready'); self.assertEqual(self.read(c, r), r['path'].encode())
        plan = file_restore.build_file_plan(c, [], [g['id']], (self.base / 'output').resolve(), (self.base / 'target-backup').resolve())
        self.assertEqual(plan['blocked'], 0)
        report = file_restore.execute_file_restore(c, plan)
        self.assertEqual(report['restored'], 3)
        published = [r for r in file_restore.plan_rows(plan) if r['size']]
        self.assertTrue(any('~003A' in r['path'] for r in published))
        self.assertTrue(any('~005C' in r['path'] for r in published))
        for r in published:
            self.assertTrue(Path(r['destination']).is_file())
        self.assertEqual(file_restore.rollback_file_restore(report)['status'], 'rolled-back')

    def test_original_posix_backslash_is_not_turned_into_directory_separator(self):
        data = b'original mac bytes'
        original = r'/source/literal\name:backup.sqlite'
        self.enc('database.asmb', {'data.sqlite': data, 'manifest.json': self.manifest([
            {'file': 'data.sqlite', 'originalPath': original, 'size': len(data), 'sha256': digest(data)}], 'asm-complete-native-databases-1')})
        c = self.cat(); g = self.group(c, 'database.asmb'); c.index_group(g['id'])
        item = next(r for r in c.list_items(g['id']) if r['kind'] == 'file')
        self.assertEqual(item['path'], 'original' + original)
        self.assertEqual(self.read(c, item), data)

    def test_windows_drive_rooted_and_nul_source_paths_stay_visible_unsupported(self):
        from ai_session_migration.recovery_catalog import safe_relative
        for name in [r'C:\escape', 'C:relative', r'\rooted', r'\\server\share', '/absolute', '../escape', 'nul\0name'.replace('\\0', '\0')]:
            with self.subTest(name=name), self.assertRaises(ValueError): safe_relative(name)
        write_zip(self.root / 'project.zip', {r'C:\escape': b'a', r'\rooted': b'b'})
        c = self.cat(); g = self.group(c, 'project.zip'); c.index_group(g['id'])
        self.assertEqual({r['status'] for r in c.list_items(g['id'])}, {'unsupported'})

    def test_unmapped_failed_objects_explicitly_do_not_claim_original_name_restore(self):
        data = b'raw retained object'
        self.enc('failed-delta.asmb', {'objects/0000': data, 'coverage.jsonl': json.dumps({
            'batch': 'failed-job', 'archive': 'data.zip', 'originalObject': 'objects/0042',
            'source': 'this-delta', 'object': 'objects/0000', 'bytes': len(data), 'sha256': digest(data)}).encode()})
        c = self.cat(); g = self.group(c, 'failed-delta'); c.index_group(g['id'])
        item = next(r for r in c.list_items(g['id']) if r['kind'] == 'file')
        self.assertEqual(item['path'], 'failed-job/data.zip/objects/0042')
        self.assertIn('未保存原文件名', item['detail'])
        self.assertIn('不代表已恢复到原项目', item['detail'])
        self.assertEqual(self.read(c, item), data)

    def test_authenticated_roots_resolve_display_and_readable_distinct_tree(self):
        data = b'project bytes'; other = b'other project bytes'
        entries = [
            {'root': 'workspace-opaque-one', 'path': '.', 'type': 'directory'},
            {'root': 'workspace-opaque-one', 'path': 'src', 'type': 'directory'},
            {'root': 'workspace-opaque-one', 'path': 'src/file:name.py', 'type': 'file', 'object': 'objects/000', 'capturedSize': len(data), 'sha256': digest(data)},
            {'root': 'workspace-opaque-two', 'path': 'src/file:name.py', 'type': 'file', 'object': 'objects/001', 'capturedSize': len(other), 'sha256': digest(other)},
            {'root': 'provider-opaque', 'path': 'sessions', 'type': 'directory'},
        ]
        roots = [{'label': 'workspace-opaque-one', 'kind': 'workspace', 'path': '/unmounted/first/Project Name'},
                 {'label': 'workspace-opaque-two', 'kind': 'workspace', 'path': '/unmounted/second/Project Name'},
                 {'label': 'provider-opaque', 'kind': 'codex', 'path': '/unmounted/home/.codex'}]
        # roots deliberately follow entries; the mapping must not depend on order.
        manifest = {'schema': 'conversation-migration-1', 'kind': 'mac-full-preservation', 'entries': entries, 'roots': roots}
        self.enc('full-preservation.asmb', {'manifest.json': manifest, 'objects/000': data, 'objects/001': other})
        c = self.cat(); g = self.group(c, 'full-preservation'); c.index_group(g['id'])
        rows = c.list_items(g['id']); files = [r for r in rows if r['kind'] == 'file']
        self.assertEqual(len(files), 2)
        self.assertEqual({r['original_path'] for r in files}, {'/unmounted/first/Project Name/src/file:name.py', '/unmounted/second/Project Name/src/file:name.py'})
        self.assertTrue(all(r['path'].startswith('Project Name-') for r in files))
        self.assertEqual(len({r['path'].split('/')[0] for r in files}), 2)
        self.assertEqual({r['_metadata']['root'] for r in files}, {'workspace-opaque-one', 'workspace-opaque-two'})
        self.assertEqual({self.read(c, r) for r in files}, {data, other})
        root_dir = next(r for r in rows if r['original_path'] == '/unmounted/first/Project Name')
        chosen = list(c.iter_items([root_dir['id']]))
        self.assertEqual(sum(r['size'] for r in chosen), len(data))
        self.assertEqual(len([r for r in chosen if r['kind'] == 'file']), 1)
        provider = next(r for r in rows if r['original_path'].endswith('/.codex/sessions'))
        self.assertTrue(provider['path'].startswith('.codex-'))
        self.assertEqual(provider['kind'], 'directory')

    def test_exact_old_manifest_dictionary_roots_resolve_without_source_access(self):
        data = b'legacy exact bytes'
        self.enc('full-preservation-vol01.asmb', {'objects/000': data})
        entry = {'root': 'legacy-opaque', 'path': 'genuine/file.txt', 'type': 'file', 'object': 'objects/old', 'capturedSize': len(data), 'sha256': digest(data)}
        manifest = {'entries': [entry], 'roots': {'legacy-opaque': {'path': '/unmounted/Archive Project', 'kind': 'workspace'}}}
        (self.root / 'old-full-manifest.json').write_text(json.dumps(manifest))
        self.enc('old-version.asmb', {'保全来源.json': {'1': '/moved/full-preservation-vol01.asmb'},
            '原完整包对象覆盖.jsonl': json.dumps({'oldObject': 'objects/old', 'sourceId': 1, 'object': 'objects/000', 'sha256': digest(data), 'bytes': len(data)}).encode()})
        c = self.cat(); g = self.group(c, 'old-version'); c.index_group(g['id'])
        row = next(r for r in c.list_items(g['id']) if r['kind'] == 'file')
        self.assertEqual(row['original_path'], '/unmounted/Archive Project/genuine/file.txt')
        self.assertTrue(row['path'].startswith('Archive Project-'))
        self.assertTrue(row['path'].endswith('/genuine/file.txt'))
        self.assertEqual(self.read(c, row), data)

    def test_discovery_skips_mock_junction_directories_and_files(self):
        junction = self.root / 'junction'; junction.mkdir()
        write_zip(junction / 'unselected.zip', {'private': b'not selected'})
        file_junction = self.root / 'linked.asmkey'; file_junction.write_bytes(b'never read')
        write_zip(self.root / 'chosen.zip', {'chosen': b'ok'})
        with patch.object(Path, 'is_junction', lambda p: p in {junction, file_junction}, create=True):
            c = self.cat()
            names = {Path(g['path']).name for g in c.groups()}
            self.assertIn('chosen.zip', names)
            self.assertNotIn('unselected.zip', names)
            self.assertNotIn('linked.asmkey', c._files)
            self.assertNotIn('unselected.zip', c._files)

    def test_discovery_skips_resolved_directory_escape_even_without_junction_flag(self):
        escaped = self.root / 'escaped'; escaped.mkdir()
        write_zip(escaped / 'unselected.zip', {'private': b'outside bytes'})
        outside = self.base / 'outside'; outside.mkdir()
        c = self.cat(); original = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == escaped: return original(outside)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve): c.discover()
        self.assertNotIn('unselected.zip', c._files)

    def test_post_discovery_parent_symlink_swap_refuses_before_payload_read(self):
        nested = self.root / 'nested'; nested.mkdir()
        write_zip(nested / 'project.zip', {'chosen.txt': b'inside'})
        c = self.cat(); g = self.group(c, 'project.zip')
        c.index_group(g['id']); item = next(r for r in c.list_items(g['id']) if r['kind'] == 'file')
        held = self.base / 'held'; nested.rename(held)
        outside = self.base / 'outside'; outside.mkdir()
        write_zip(outside / 'project.zip', {'private': b'outside payload must not be read'})
        nested.symlink_to(outside, target_is_directory=True)
        with patch.object(archive_io, 'ArchiveReader') as reader, patch.object(archive_io, 'authenticate') as authenticate:
            with self.assertRaisesRegex(ValueError, '链接'): self.read(c, item)
            with self.assertRaisesRegex(ValueError, '链接'): c._ensure_archive(nested / 'project.zip')
            reader.assert_not_called(); authenticate.assert_not_called()
        self.assertIsNone(c._resolve(str(nested / 'project.zip')))

    def test_cached_archive_rechecks_mock_junction_before_cache_reuse(self):
        nested = self.root / 'nested'; nested.mkdir(); archive = write_zip(nested / 'project.zip', {'file': b'x'})
        c = self.cat(); g = self.group(c, 'project.zip'); c.index_group(g['id'])
        with patch.object(Path, 'is_junction', lambda p: p == nested, create=True):
            with patch.object(c, '_source_unchanged', return_value=True) as cached:
                with self.assertRaisesRegex(ValueError, '联接'): c._ensure_archive(archive)
                cached.assert_not_called()
            with patch.object(archive_io, 'ArchiveReader') as reader:
                with self.assertRaisesRegex(ValueError, '联接'):
                    with c._member(archive, 'file'): pass
                reader.assert_not_called()

    def test_swapped_auto_key_parent_not_read_explicit_outside_key_allowed(self):
        self.enc('sessions.asmb', {'manifest.json': self.manifest([])})
        keydir = self.root / 'keys'; keydir.mkdir(); self.key.rename(keydir / self.key.name)
        c = self.cat(); g = self.group(c, 'sessions')
        selected = self.base / 'explicit-keys'; keydir.rename(selected)
        outside = self.base / 'outside'; outside.mkdir(); (outside / self.key.name).write_bytes(b'unselected key body')
        keydir.symlink_to(outside, target_is_directory=True)
        with patch('ai_session_migration.recovery_catalog.read_key', side_effect=AssertionError('must not read auto key through link')) as read:
            self.assertEqual(c.index_group(g['id'])['status'], 'key-required')
            read.assert_not_called()
        explicit = self.cat(keys=[selected / self.key.name]); group = self.group(explicit, 'sessions')
        self.assertEqual(explicit.index_group(group['id'])['status'], 'ready')

    def test_insufficient_cache_rolls_back_current_index_and_preserves_ready_group_source(self):
        from ai_session_migration.recovery_catalog import CACHE_RESERVE, CacheSpaceError
        write_zip(self.root / 'previous.zip', {'keep.txt': b'previous ready bytes'})
        large = write_zip(self.root / 'large.zip', {'folder/file-' + str(n): b'x' for n in range(100)})
        before = digest(large.read_bytes())
        c = self.cat(); good = self.group(c, 'previous.zip'); c.index_group(good['id'])
        old_item = next(r for r in c.list_items(good['id']) if r['kind'] == 'file')
        group = self.group(c, 'large.zip'); checks = []
        def space(path):
            checks.append(path)
            return SimpleNamespace(free=CACHE_RESERVE + 1000000 if len(checks) == 1 else CACHE_RESERVE - 1)
        with patch('ai_session_migration.recovery_catalog.shutil.disk_usage', side_effect=space):
            with self.assertRaisesRegex(CacheSpaceError, '选择其他.*缓存目录'): c.index_group(group['id'])
        self.assertGreaterEqual(len(checks), 2)
        failed = next(g for g in c.groups() if g['id'] == group['id'])
        self.assertEqual(failed['status'], 'error'); self.assertIn('512 MiB', failed['detail'])
        self.assertEqual(c.count_items(group['id']), 0)
        self.assertEqual(c.db.execute('SELECT count(*) FROM members WHERE archive=?', (str(large),)).fetchone()[0], 0)
        self.assertFalse(c.db.in_transaction)
        self.assertEqual(next(g for g in c.groups() if g['id'] == good['id'])['status'], 'ready')
        self.assertEqual(self.read(c, old_item), b'previous ready bytes')
        self.assertEqual(digest(large.read_bytes()), before)
        with self.assertRaisesRegex(ValueError, '先建立'): list(c.iter_items(group_ids=[group['id']]))
        # Only this catalog's WAL is truncated; no source/cache deletion occurs.
        for wal in c.cache.glob('*.sqlite-wal'): self.assertEqual(wal.stat().st_size, 0)

    def test_cache_equal_bundle_rejected_descendant_cache_excluded(self):
        with self.assertRaisesRegex(ValueError, '不能与备份目录相同'): RecoveryCatalog(self.root, self.root)
        nested = self.root / 'catalog-cache'
        c = RecoveryCatalog(self.root, nested); self.catalogs.append(c)
        (nested / 'not-source.zip').write_bytes(b'cache is not backup source')
        c.discover()
        self.assertNotIn('not-source.zip', c._files)

    def test_authenticated_encrypted_only_session_has_native_route(self):
        archive = self.enc('preservation.asmb', {'manifest.json': self.manifest([], 'ai-session-migration-gui-1')})
        c = self.cat(); group = self.group(c, 'preservation.asmb')
        self.assertFalse(group.get('native_archive'))
        indexed = c.index_group(group['id'])
        self.assertEqual(indexed['category'], 'sessions')
        self.assertEqual(indexed['native_archive'], str(archive))

    def test_existing_portable_native_route_preferred_after_encrypted_session_auth(self):
        archive = self.enc('session.asmb', {'manifest.json': self.manifest([], 'ai-session-migration-gui-1')})
        portable = write_zip(self.root / 'session.zip', {'manifest.json': self.manifest([], 'ai-session-migration-gui-1')})
        c = self.cat(); group = self.group(c, 'session.asmb')
        self.assertEqual(group.get('native_archive'), str(portable))
        indexed = c.index_group(group['id'])
        self.assertEqual(indexed['native_archive'], str(portable))
        self.assertNotEqual(indexed['native_archive'], str(archive))

    def test_descriptor_identity_uses_one_api_when_path_stat_differs(self):
        path = self.root / 'unchanged.bin'; path.write_bytes(b'unchanged')
        with path.open('rb') as f:
            actual = os.fstat(f.fileno())
        differing = SimpleNamespace(st_dev=actual.st_dev + 1, st_ino=actual.st_ino + 99, st_size=actual.st_size,
                                    st_mtime_ns=actual.st_mtime_ns + 100, st_ctime_ns=actual.st_ctime_ns + 100,
                                    st_mode=actual.st_mode)
        with patch.object(Path, 'stat', return_value=differing):
            expected = archive_io.identity(path)
            with archive_io.ArchiveReader(path, expected) as reader:
                self.assertEqual(reader.read(9), b'unchanged')
        self.assertEqual(expected, [actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns])

    def test_descriptor_identity_still_rejects_replacement_and_content_changes(self):
        path = self.root / 'source.bin'; path.write_bytes(b'original')
        expected = archive_io.identity(path)
        replacement = self.root / 'replacement.bin'; replacement.write_bytes(b'original')
        os.replace(replacement, path)
        self.assertNotEqual(archive_io.identity(path), expected)
        with self.assertRaisesRegex(ValueError, '改变'): archive_io.ArchiveReader(path, expected)
        current = archive_io.identity(path); path.write_bytes(b'changed-length')
        with self.assertRaisesRegex(ValueError, '改变'): archive_io.check_identity(path, current)

    def test_identity_rejects_special_source_before_open_no_fifo_block(self):
        with patch.object(archive_io.os, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFIFO | 0o600)):
            with patch.object(archive_io.os, 'open') as opened:
                with self.assertRaisesRegex(ValueError, '普通文件'): archive_io.identity(self.root / 'fifo')
                opened.assert_not_called()

    def test_explicit_encrypted_reindex_authenticates_same_size_restored_mtime_corruption(self):
        path = self.enc('session.asmb', {'manifest.json': self.manifest([], 'ai-session-migration-gui-1')})
        c = self.cat(); group = self.group(c, 'session.asmb'); c.index_group(group['id'])
        expected = archive_io.identity(path); time_before = path.stat()
        with path.open('r+b') as f:
            f.seek(-18, 2); byte = f.read(1); f.seek(-1, 1); f.write(bytes([byte[0] ^ 1]))
        os.utime(path, ns=(time_before.st_atime_ns, time_before.st_mtime_ns))
        descriptor = archive_io.descriptor_identity
        def creation_time_stat(fd):
            current = descriptor(fd)
            if current[:2] == expected[:2]: current[-1] = expected[-1]
            return current
        # Model Windows creation-time ctime: metadata matches despite mutation.
        with patch.object(archive_io, 'descriptor_identity', side_effect=creation_time_stat):
            self.assertTrue(c._source_unchanged(path))
            with patch.object(archive_io, 'authenticate', wraps=archive_io.authenticate) as authenticate:
                with self.assertRaisesRegex(ValueError, '认证失败'): c.index_group(group['id'])
                self.assertTrue(authenticate.called)
        self.assertNotEqual(next(g for g in c.groups() if g['id'] == group['id'])['status'], 'ready')

    def test_key_group_hash_detects_same_size_restored_mtime_key_edit(self):
        c = self.cat(); group = next(g for g in c.groups() if g['category'] == 'keys'); c.index_group(group['id'])
        item = next(r for r in c.list_items(group['id']) if r['kind'] == 'file')
        original = self.key.read_bytes(); expected = archive_io.identity(self.key); before = self.key.stat()
        self.assertEqual(item['sha256'], digest(original))
        key_body = json.loads(original)['key'].encode(); position = original.index(key_body)
        changed = original[:position] + (b'A' if original[position:position + 1] != b'A' else b'B') + original[position + 1:]
        self.key.write_bytes(changed); os.utime(self.key, ns=(before.st_atime_ns, before.st_mtime_ns))
        descriptor = archive_io.descriptor_identity
        def creation_time_stat(fd):
            current = descriptor(fd)
            if current[:2] == expected[:2]: current[-1] = expected[-1]
            return current
        with patch.object(archive_io, 'descriptor_identity', side_effect=creation_time_stat):
            with self.assertRaisesRegex(ValueError, '校验失败'): self.read(c, item)
        self.assertEqual(len(changed), len(original))

    def test_cancellation_does_not_expose_partial_items(self):
        self.enc('sessions.asmb', {'manifest.json': self.manifest([self.entry('x', b'x')]), 'objects/x': b'x'})
        c = self.cat(); g = self.group(c, 'sessions'); event = threading.Event(); event.set()
        with self.assertRaises(Exception): c.index_group(g['id'], cancel=event)
        self.assertEqual(c.count_items(g['id']), 0)


if __name__ == '__main__': unittest.main()

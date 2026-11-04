import hashlib
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('migration', Path(__file__).resolve().parents[1] / 'scripts/migrate.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.code = self.home / '.claude'
        (self.code / 'projects' / 'project').mkdir(parents=True)
        self.transcript = self.code / 'projects/project/id.jsonl'
        self.content = b'{"type":"assistant","content":[{"type":"thinking","thinking":"synthetic-test-only","signature":"original"}]}\n'
        self.transcript.write_bytes(self.content)
        (self.code / '.credentials.json').write_text('{"token":"synthetic-secret"}')
        self.codex = self.home / '.codex'
        self.codex.mkdir()
        (self.codex / 'auth.json').write_text('{"token":"synthetic-secret"}')
        (self.codex / 'sessions').mkdir()
        (self.codex / 'sessions/a.jsonl.zst').write_bytes(b'compressed-fixture-bytes')

    def tearDown(self):
        self.temp.cleanup()

    def test_inventory_never_follows_symlinks_and_keeps_lockfiles(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'private').write_text('not-captured')
        (self.code / 'projects/link').symlink_to(outside)
        (self.code / 'projects/yarn.lock').write_text('lockfile')
        p = m.plan(self.home)
        rows = p['entries']
        self.assertTrue(any(e['type'] == 'symlink' for e in rows))
        self.assertFalse(any(e['path'].endswith('private') for e in rows))
        self.assertTrue(any(e['path'].endswith('yarn.lock') and e['type'] == 'file' for e in rows))

    def test_plan_tamper_and_traversal_rejected(self):
        p = m.plan(self.home)
        target = self.base / 'plan.json'
        target.write_bytes(m.dump(p))
        self.assertEqual(m.load_plan(target)['digest'], p['digest'])
        p['entries'][0]['path'] = '../escape'
        p.pop('digest')
        p['digest'] = m.digest(p)
        target.write_bytes(m.dump(p))
        with self.assertRaises(ValueError):
            m.load_plan(target)

    def test_extra_claude_profile_plan_can_be_loaded(self):
        profile = self.home / 'Library/Application Support/Claude-3p'
        profile.mkdir(parents=True)
        (profile / 'config.json').write_text('{}')
        p = m.plan(self.home)
        target = self.base / 'extra-profile.plan.json'
        target.write_bytes(m.dump(p))
        loaded = m.load_plan(target)
        self.assertTrue(any(r['path'] == str(profile) for r in loaded['roots']))

    def test_wal_snapshot_keeps_committed_uncheckpointed_history(self):
        source = self.base / 'live.sqlite'
        db = sqlite3.connect(source)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE messages (body TEXT)')
        db.execute("INSERT INTO messages VALUES ('committed-in-wal')")
        db.commit()
        before = m.hash_file(source)
        snap = self.base / 'snapshot.sqlite'
        m.sqlite_snapshot(source, snap)
        dest = sqlite3.connect(snap)
        self.assertEqual(dest.execute('SELECT * FROM messages').fetchall(), [('committed-in-wal',)])
        dest.close()
        self.assertEqual(m.hash_file(source), before)
        db.close()

    def test_checkpointed_database_does_not_create_source_sidecars(self):
        source = self.base / 'checkpointed.sqlite'
        fixture = self.base / 'wal-fixture.sqlite'
        db = sqlite3.connect(fixture)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE data (value TEXT)')
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.close()
        source.write_bytes(fixture.read_bytes())
        before = {p.name: m.hash_file(p) for p in self.base.glob('checkpointed*')}
        method = m.sqlite_snapshot(source, self.base / 'copy.sqlite')
        self.assertEqual(method, 'sqlite-checkpointed-file-snapshot')
        self.assertEqual(before, {p.name: m.hash_file(p) for p in self.base.glob('checkpointed*')})

    def test_locked_file_set_copy_keeps_wal_commit_and_source_bytes(self):
        source = self.base / 'exclusive.sqlite'
        db = sqlite3.connect(source)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA locking_mode=EXCLUSIVE')
        db.execute('CREATE TABLE data (value TEXT)')
        db.execute("INSERT INTO data VALUES ('wal-value')")
        db.commit()
        before = {p.name: m.hash_file(p) for p in self.base.glob('exclusive.sqlite*')}
        method = m.sqlite_snapshot(source, self.base / 'exclusive-copy.sqlite', prefer_copy=True)
        self.assertEqual(method, 'sqlite-stable-file-set-backup')
        snapshot = sqlite3.connect(self.base / 'exclusive-copy.sqlite')
        self.assertEqual(snapshot.execute('SELECT * FROM data').fetchall(), [('wal-value',)])
        snapshot.close()
        self.assertEqual(before, {p.name: m.hash_file(p) for p in self.base.glob('exclusive.sqlite*')})
        db.close()

    def test_resume_verifies_captured_objects_and_keeps_prior_run(self):
        profile = self.home / 'Library/Application Support/Claude'
        index = profile / 'claude-code-sessions/account/org/local_fixture.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'sessionId': 'desktop-id', 'cliSessionId': 'cli-id', 'title': 'fixture', 'permissionMode': 'old-mode'}))
        (profile / 'zzzz-sentinel').write_text('not yet captured')
        p = m.plan(self.home)
        cutoff = next(i for i, e in enumerate(p['entries']) if e['path'] == 'zzzz-sentinel')
        failed = self.base / 'failed'
        failed.mkdir()
        roots = {r['label']: r for r in p['roots']}
        for filename, win in [('mac-preservation.zip.part', False), ('windows-portable.zip.part', True)]:
            with zipfile.ZipFile(failed / filename, 'w') as z:
                for i, e in enumerate(p['entries'][:cutoff]):
                    if e['type'] != 'file' or (win and not m.portable(roots[e['root']], e['path'])):
                        continue
                    root = Path(roots[e['root']]['path'])
                    z.writestr('objects/' + str(i).zfill(8), (root / e['path']).read_bytes())
        journal = {'schema': m.SCHEMA, 'status': 'failed', 'startedAt': m.now(), 'failedAt': m.now(),
                   'planDigest': p['digest'], 'lastAttempt': {k: p['entries'][cutoff][k] for k in ['root', 'path']}}
        (failed / 'journal.json').write_bytes(m.dump(journal))
        original = {f.name: m.hash_file(f) for f in failed.iterdir()}
        with patch.object(m, 'RESERVE', 0):
            result = m.capture(p, self.base / 'resumed', self.base / 'resumed.key', live=True, resume_from=failed)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['resumedEntriesVerified'], cutoff)
        self.assertEqual(original, {f.name: m.hash_file(f) for f in failed.iterdir()})
        self.assertTrue(m.verify_archive(self.base / 'resumed/windows-portable.zip')['verified'])
        with zipfile.ZipFile(self.base / 'resumed/windows-portable.zip') as z:
            catalog = json.loads(z.read('session-catalog.json'))['claudeDesktop']
            self.assertEqual(catalog[0]['cliSessionId'], 'cli-id')
            self.assertNotIn('permissionMode', catalog[0])

    def test_source_can_grow_while_stable_snapshot_is_compressed(self):
        source = self.transcript
        original = self.content
        class GrowingArchive:
            def __init__(self, archive):
                self.archive = archive
            @contextmanager
            def open(self, *args, **kwargs):
                with self.archive.open(*args, **kwargs) as writer:
                    class GrowingWriter:
                        def write(self, block):
                            with open(source, 'ab') as f:
                                f.write(b'new-message\n')
                            return writer.write(block)
                    yield GrowingWriter()
        expected = {'size': source.stat().st_size, 'mtime_ns': source.stat().st_mtime_ns}
        archive = self.base / 'growing.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            row = m.archive_source(source, [GrowingArchive(z)], 'object', expected, True)
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(z.read('object'), original)
        self.assertEqual(row['sha256'], hashlib.sha256(original).hexdigest())
        self.assertNotEqual(source.read_bytes(), original)

    def test_portable_excludes_known_credentials_and_runnable_config(self):
        roots = {r['label']: r for r in m.discover(self.home)}
        self.assertFalse(m.portable(roots['codex-home'], 'auth.json'))
        self.assertFalse(m.portable(roots['codex-home'], 'config.toml'))
        self.assertFalse(m.portable(roots['claude-code'], '.credentials.json'))
        self.assertTrue(m.portable(roots['claude-code'], 'projects/project/id.jsonl'))
        self.assertTrue(m.portable(roots['codex-home'], 'thread_history_1.sqlite'))
        self.assertFalse(m.portable({'kind': 'workspace'}, '.env.production'))

    def test_changed_plan_refuses_cold_copy_and_source_link(self):
        expected = {'size': self.transcript.stat().st_size, 'mtime_ns': self.transcript.stat().st_mtime_ns}
        self.transcript.write_bytes(self.content + b'new')
        with self.assertRaises(ValueError):
            m.stable_copy(self.transcript, self.base / 'stage', expected, False)
        link = self.base / 'link'
        link.symlink_to(self.transcript)
        with self.assertRaises(OSError):
            m.stable_copy(link, self.base / 'stage', expected, True)

    def test_capture_encrypted_round_trip_preserves_thinking_and_source(self):
        p = m.plan(self.home)
        output = self.base / 'output'
        key = self.base / 'recovery.key'
        before = {str(f): m.hash_file(f) for f in self.home.rglob('*') if f.is_file()}
        with patch.object(m, 'RESERVE', 0):
            result = m.capture(p, output, key, live=True)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['needsColdFinalBackup'])
        self.assertTrue(result['encryptedBackupReadbackVerified'])
        self.assertEqual(before, {str(f): m.hash_file(f) for f in self.home.rglob('*') if f.is_file()})
        portable = output / 'windows-portable.zip'
        self.assertTrue(m.verify_archive(portable)['verified'])
        with zipfile.ZipFile(portable) as z:
            manifest = json.loads(z.read('manifest.json'))
            self.assertFalse(any(e['path'].endswith('auth.json') for e in manifest['entries']))
            row = next(e for e in manifest['entries'] if e['path'].endswith('id.jsonl'))
            self.assertEqual(z.read(row['object']), self.content)
        decoded = self.base / 'decoded.zip'
        m.decrypt_backup(output / 'mac-preservation.zip.enc', key, output / 'backup-auth.json', decoded)
        with zipfile.ZipFile(decoded) as z:
            manifest = json.loads(z.read('manifest.json'))
            self.assertTrue(any(e['path'] == 'auth.json' for e in manifest['entries']))
        self.assertTrue(m.verify_archive(decoded)['verified'])
        with self.assertRaises(FileExistsError):
            m.decrypt_backup(output / 'mac-preservation.zip.enc', key, output / 'backup-auth.json', decoded)
        with self.assertRaises(FileExistsError):
            m.capture(p, output, key, live=True)
        auth = output / 'backup-auth.json'
        damaged = json.loads(auth.read_text())
        damaged['hmac'] = '0' * 64
        auth.write_bytes(m.dump(damaged))
        with self.assertRaises(ValueError):
            m.decrypt_backup(output / 'mac-preservation.zip.enc', key, auth, self.base / 'bad.zip')

    def test_cold_capture_refuses_live_apps_and_output_inside_source(self):
        p = m.plan(self.home)
        with patch.object(m, 'process_state', return_value={'inspection': 'available', 'running': ['Claude']}):
            with self.assertRaises(ValueError):
                m.capture(p, self.base / 'output', self.base / 'key')
        with self.assertRaises(ValueError):
            m.capture(p, self.code / 'output', self.base / 'key', live=True)
        self.assertFalse((self.code / 'output').exists())

    def test_verify_rejects_unmanifested_payload_and_duplicate_names(self):
        path = self.base / 'bad.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('../escape', b'x')
        with self.assertRaises(ValueError):
            m.verify_archive(path)


if __name__ == '__main__':
    unittest.main()

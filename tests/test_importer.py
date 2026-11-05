import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import zstandard

from ai_session_migration.adapters import LocalLibrary
from ai_session_migration.backup import make_backup, save_recovery_key, Cancelled
from ai_session_migration.importer import build_plan, execute_import, rollback_import, inventory, dsh_project_key, target_locks
from ai_session_migration.packages import PackageLibrary
from ai_session_migration.safeio import open_lines, hash_file
from fixtures import make_home
from test_core import fingerprints


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'source'
        make_home(self.source)
        self.library = LocalLibrary(self.source, self.base / 'source-cache')
        records = self.library.scan().records
        save_recovery_key(self.base / 'key.asmkey')
        exported = make_backup(self.library, records, self.base / 'export', self.base / 'key.asmkey')
        self.package = PackageLibrary(Path(exported['portable']), self.base / 'cache')
        self.selected = self.package.scan().records
        self.target = self.base / 'target'
        files = make_home(self.target)
        # Existing native histories use different IDs. Keep their bytes/rows intact.
        files['claude'].rename(files['claude'].with_name('existing-claude.jsonl'))
        for database, table, key in [('state_5.sqlite', 'threads', 'id'), ('thread_history_1.sqlite', 'thread_items', 'thread_id')]:
            with closing(sqlite3.connect(self.target / '.codex' / database)) as c, c:
                c.execute(f'UPDATE {table} SET {key}=?', ('existing-codex',))
        path = files['deepseek']
        rows = list(open_lines(path)); rows[0]['id'] = 'existing-harness'
        compressor = zstandard.ZstdCompressor(write_checksum=True)
        path.write_bytes(b''.join(compressor.compress((json.dumps(r) + '\n').encode()) for r in rows))
        path.parent.rename(path.parent.with_name('existing-harness'))
        registry = self.target / '.dsh/storages/workspace.json'
        registry.write_text(registry.read_text().replace('harness-session', 'existing-harness'))
        self.targets = {p: str(self.target / name) for p, name in [('claude', '.claude'), ('codex', '.codex'), ('deepseek', '.dsh')]}
        self.mappings = {r.cwd: str(files['project']) for r in self.selected}
        self.before = {p: inventory(Path(root)) for p, root in self.targets.items()}
        self.source_before = fingerprints(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, selected=None):
        return build_plan(self.package, selected or self.selected, self.targets, self.mappings, self.base / 'cache')

    def execute(self, plan=None, **kwargs):
        return execute_import(plan or self.plan(), self.base / 'target-backups', self.base / 'cache', confirmed_offline=True, **kwargs)

    def assert_original_targets(self):
        self.assertEqual(self.before, {p: inventory(Path(root)) for p, root in self.targets.items()})
        self.assertEqual(self.source_before, fingerprints(self.source))

    def test_three_native_sources_backup_merge_readback_and_conditional_undo(self):
        plan = self.plan()
        self.assertEqual(len(plan.ready), 3)
        self.assert_original_targets()
        report = self.execute(plan)
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(report['backupVerified'])
        with zipfile.ZipFile(report['backup']) as z:
            for provider, tree in self.before.items():
                for name, info in tree['files'].items():
                    if not info.get('directory'):
                        import hashlib
                        self.assertEqual(hashlib.sha256(z.read(provider + '/' + name)).hexdigest(), info['sha256'])
        reader = LocalLibrary(self.target, self.base / 'readback-cache')
        records = reader.scan().records
        self.assertEqual(len(records), 6)
        for imported in self.selected:
            record = next(r for r in records if r.provider == imported.provider and r.session_id == imported.session_id)
            self.assertEqual(record.cwd, str(self.target / '示例项目'))
            page = reader.page(record)
            self.assertTrue(any(m.kind == 'thinking' for m in page.messages))
            if record.provider == 'deepseek':
                self.assertTrue(record.archived)
                self.assertTrue(page.attachments)
        with closing(sqlite3.connect(self.target / '.codex/thread_history_1.sqlite')) as c, c:
            old = c.execute('SELECT item_json FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal', ('existing-codex',)).fetchall()
            new = c.execute('SELECT item_json FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal', ('codex-session',)).fetchall()
            self.assertEqual(old, new)  # byte-identical item_json, including reasoning
        duplicate = self.plan()
        self.assertEqual(len(duplicate.ready), 0)
        self.assertTrue(all(i['action'] == 'skip' for i in duplicate.items))
        restored = rollback_import(Path(report['journal']), self.base / 'cache', confirmed_offline=True)
        self.assertTrue(restored['rollbackVerified'])
        self.assert_original_targets()

    def test_publish_failure_restores_every_provider_and_keeps_backup(self):
        def fail(stage):
            if stage == 'before-publish-codex':
                raise OSError('synthetic publication failure')
        with self.assertRaises(OSError) as raised:
            self.execute(fault=fail)
        self.assertEqual(raised.exception.report['status'], 'rolled-back')
        self.assertTrue(Path(raised.exception.report['backup']).is_file())
        self.assert_original_targets()

    def test_abrupt_process_exit_is_recoverable_from_persisted_journal(self):
        plan = self.plan()
        path = self.base / 'synthetic-plan.json'
        path.write_text(json.dumps(plan.to_dict()), encoding='utf-8')
        script = '''
import json,os,sys
from pathlib import Path
from ai_session_migration.importer import ImportPlan,execute_import
base=Path(sys.argv[1])
plan=ImportPlan(**json.loads((base/'synthetic-plan.json').read_text(encoding='utf-8')))
def crash(stage):
    if stage=='before-publish-codex':os._exit(23)
execute_import(plan,base/'crash-backups',base/'cache',confirmed_offline=True,fault=crash)
'''
        child = subprocess.run([sys.executable, '-B', '-c', script, str(self.base)], capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 23, child.stderr.decode(errors='replace'))
        journals = list((self.base / 'crash-backups').glob('*/导入结果.json'))
        self.assertEqual(len(journals), 1)
        partial = json.loads(journals[0].read_text(encoding='utf-8'))
        self.assertEqual(partial['status'], 'publishing')
        self.assertTrue(partial['backupVerified'])
        restored = rollback_import(journals[0], self.base / 'cache', confirmed_offline=True)
        self.assertTrue(restored['rollbackVerified'])
        self.assert_original_targets()

    def test_target_drift_after_preview_and_during_staging_never_overwrites(self):
        plan = self.plan()
        new = self.target / '.claude/new-conversation.jsonl'
        new.write_text('new work after preview')
        with self.assertRaisesRegex(ValueError, '预览后已变化'):
            self.execute(plan)
        self.assertEqual(new.read_text(), 'new work after preview')
        new.unlink()  # fixture only
        def drift(stage):
            if stage == 'staged':
                new.write_text('concurrent new work')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            self.execute(fault=drift)
        self.assertEqual(new.read_text(), 'concurrent new work')
        self.assertEqual(self.source_before, fingerprints(self.source))

    def test_undo_refuses_to_overwrite_new_work(self):
        report = self.execute()
        new = self.target / '.codex/added-after-import.jsonl'
        new.write_text('later user conversation')
        with self.assertRaisesRegex(ValueError, '不覆盖后续新对话'):
            rollback_import(Path(report['journal']), self.base / 'cache', confirmed_offline=True)
        self.assertEqual(new.read_text(), 'later user conversation')
        self.assertTrue(Path(report['backup']).is_file())

    def test_cancel_before_publish_and_unconfirmed_import_keep_targets(self):
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, '确认'):
            execute_import(plan, self.base / 'target-backups', self.base / 'cache')
        event = threading.Event()
        def progress(stage, _):
            if '在独立副本' in stage:
                event.set()
        with self.assertRaises(Cancelled):
            self.execute(plan, cancel=event, progress=progress)
        self.assert_original_targets()

    def test_unknown_target_schema_is_blocked_at_preview_without_original_writes(self):
        with closing(sqlite3.connect(self.target / '.codex/state_5.sqlite')) as c, c:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='threads'").fetchone()[0]
            columns = [r[1] for r in c.execute('PRAGMA table_info(threads)')]
            c.execute('ALTER TABLE threads RENAME TO old_threads')
            c.execute(sql[:-1] + ', future_required TEXT NOT NULL)')
            c.execute('INSERT INTO threads SELECT ' + ','.join(columns) + ", 'existing' FROM old_threads")
            c.execute('DROP TABLE old_threads')
        self.before = {p: inventory(Path(root)) for p, root in self.targets.items()}
        plan = self.plan()
        codex = next(i for i in plan.items if i['record']['provider'] == 'codex')
        self.assertEqual(codex['action'], 'blocked')
        self.assertIn('必填字段', codex['reason'])
        self.assert_original_targets()

    def test_missing_codex_database_and_mapping_are_visible_blockers(self):
        targets = dict(self.targets, codex=str(self.base / 'uninitialized-codex'))
        plan = build_plan(self.package, self.selected, targets, {}, self.base / 'cache')
        self.assertEqual(len(plan.ready), 0)
        self.assertTrue(any('尚未初始化' in i['reason'] for i in plan.items))
        self.assertTrue(any('映射' in i['reason'] for i in plan.items))
        self.assert_original_targets()

    def test_nested_backup_symlink_and_shared_target_are_rejected(self):
        with self.assertRaisesRegex(ValueError, '之外'):
            execute_import(self.plan(), self.target / '.claude/backup', self.base / 'cache', confirmed_offline=True)
        targets = dict(self.targets, codex=self.targets['claude'])
        with self.assertRaisesRegex(ValueError, '不能重叠'):
            build_plan(self.package, self.selected, targets, self.mappings, self.base / 'cache')
        link = self.target / '.claude/link'
        try:
            link.symlink_to(self.source, target_is_directory=True)
        except OSError:
            return  # Windows runner may lack symlink privilege.
        with self.assertRaisesRegex(ValueError, '链接'):
            self.plan()

    def test_advisory_locks_release_and_never_wait_on_concurrent_import(self):
        with target_locks(self.targets, self.base / 'cache'):
            with self.assertRaisesRegex(ValueError, '已有导入'):
                with target_locks(self.targets, self.base / 'cache'):
                    self.fail('concurrent lock admitted')
        with target_locks(self.targets, self.base / 'cache'):
            pass

    def test_corrupt_extracted_cache_is_not_trusted(self):
        entry = next(e for e in self.package.entries.values() if e.get('role') == 'original-session')
        cached = self.package._extract(entry['object'], 'log.jsonl')
        cached.write_bytes(b'X' * cached.stat().st_size)
        restored = self.package._extract(entry['object'], 'log.jsonl')
        self.assertEqual(hash_file(restored), entry['sha256'])

    def test_dsh_paths_match_first_party_windows_and_unicode_rules(self):
        self.assertEqual(dsh_project_key('C:\\work\\demo'), '--C-work-demo--')
        self.assertEqual(dsh_project_key('/工作/🙂'), '--~5DE5~4F5C-~D83D~DE42--')

    def reexport(self):
        records = self.library.scan().records
        exported = make_backup(self.library, records, self.base / 'export', self.base / 'key.asmkey')
        self.package = PackageLibrary(Path(exported['portable']), self.base / 'cache')
        self.selected = self.package.scan().records
        self.source_before = fingerprints(self.source)

    def test_claude_desktop_index_uses_explicit_account_container_and_discards_permissions(self):
        index = self.source / 'Library/Application Support/Claude/claude-code-sessions/source-account/source-folder/local_desktop-fixture.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'sessionId': 'local_desktop-fixture', 'cliSessionId': 'claude-session', 'title': 'Synthetic desktop title', 'cwd': str(self.source / '示例项目'), 'permissionMode': 'bypassPermissions', 'remoteMcpServersConfig': {'synthetic': 'do-not-restore'}, 'sessionPermissionUpdates': ['unsafe']}))
        self.reexport()
        desktop = self.target / 'chosen-target-account'
        desktop.mkdir()
        existing = desktop / 'local_existing.json'
        existing.write_text(json.dumps({'cliSessionId': 'unrelated-native-session', 'title': 'keep me'}))
        before = existing.read_bytes()
        self.targets['claude_desktop'] = str(desktop)
        records = [r for r in self.selected if r.provider == 'claude']
        plan = self.plan(records)
        self.assertEqual(len(plan.ready), 1)
        report = self.execute(plan)
        restored = json.loads((desktop / index.name).read_text())
        self.assertEqual(restored['cliSessionId'], 'claude-session')
        self.assertEqual(restored['cwd'], str(self.target / '示例项目'))
        self.assertEqual(restored['permissionMode'], 'default')
        self.assertNotIn('remoteMcpServersConfig', restored)
        self.assertNotIn('sessionPermissionUpdates', restored)
        self.assertEqual(existing.read_bytes(), before)
        self.assertTrue(report['backupVerified'])
        self.assertEqual(self.source_before, fingerprints(self.source))

    def test_child_session_requires_parent_and_keeps_native_claude_layout(self):
        original = self.source / '.claude/projects/example/claude-session/subagents/agent-child.jsonl'
        original.parent.mkdir(parents=True)
        original.write_text(json.dumps({'type': 'user', 'sessionId': 'agent-child', 'cwd': str(self.source / '示例项目'), 'message': {'role': 'user', 'content': 'synthetic child task'}}) + '\n')
        self.reexport()
        child = next(r for r in self.selected if r.session_id == 'agent-child')
        alone = self.plan([child])
        self.assertEqual(alone.items[0]['action'], 'blocked')
        self.assertIn('父会话', alone.items[0]['reason'])
        report = self.execute()
        self.assertEqual(report['status'], 'complete')
        reader = LocalLibrary(self.target, self.base / 'child-readback')
        restored = next(r for r in reader.scan().records if r.session_id == 'agent-child')
        self.assertTrue(restored.child)
        self.assertEqual(Path(restored.files[0]).parts[-3:], ('claude-session', 'subagents', 'agent-child.jsonl'))
        self.assertEqual(self.source_before, fingerprints(self.source))

    def test_native_projects_turns_compressed_rollout_offsets_and_policy_reset(self):
        source_log = self.source / '.codex/sessions/rollout-codex-session.jsonl.zst'
        source_log.parent.mkdir()
        lines = [json.dumps({'type': 'session_meta', 'payload': {'id': 'codex-session', 'cwd': str(self.source / '示例项目')}}) + '\n', json.dumps({'type': 'turn_context', 'payload': {'cwd': str(self.source / '示例项目'), 'approval_policy': 'never', 'sandbox_policy': {'type': 'danger-full-access'}}}) + '\n']
        raw = ''.join(lines).encode()
        source_log.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(raw))
        for home in [self.source, self.target]:
            with closing(sqlite3.connect(home / '.codex/state_5.sqlite')) as c, c:
                c.execute('CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL, position INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)')
                c.execute('CREATE TABLE project_roots (project_id TEXT REFERENCES projects(id), position INTEGER, path TEXT, PRIMARY KEY(project_id, position))')
                c.execute('INSERT INTO projects VALUES (?,?,?,?,?,?)', ('project-1', 'fixture', '{"privateConfig":"do-not-restore"}', 0, 1, 1))
                c.execute('INSERT INTO project_roots VALUES (?,?,?)', ('project-1', 0, str(home / '示例项目')))
                c.execute('ALTER TABLE threads ADD COLUMN project_id TEXT REFERENCES projects(id)')
                c.execute('ALTER TABLE threads ADD COLUMN sandbox_policy TEXT DEFAULT "unsafe"')
                c.execute('ALTER TABLE threads ADD COLUMN approval_mode TEXT DEFAULT "never"')
                c.execute('ALTER TABLE threads ADD COLUMN memory_mode TEXT DEFAULT "enabled"')
                c.execute('ALTER TABLE threads ADD COLUMN creator_account_id TEXT DEFAULT "source-account-fixture"')
                c.execute('UPDATE threads SET project_id=?', ('project-1',))
                if home == self.source:
                    c.execute('UPDATE threads SET rollout_path=?', (str(source_log.with_suffix('')),))
            with closing(sqlite3.connect(home / '.codex/thread_history_1.sqlite')) as c, c:
                c.execute('CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT, rollout_byte_offset INTEGER, rollout_end_byte_offset INTEGER, PRIMARY KEY(thread_id,turn_id))')
                c.execute('CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER NOT NULL, next_rollout_ordinal INTEGER NOT NULL)')
                sid = 'codex-session' if home == self.source else 'existing-codex'
                c.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?,?)', (sid, 'turn', 0, 'in_progress', len(lines[0].encode()), len(raw)))
                c.execute('INSERT INTO thread_history_projection_state VALUES (?,?,?)', (sid, len(raw), 2))
        self.reexport()
        report = self.execute()
        self.assertEqual(report['status'], 'complete')
        with closing(sqlite3.connect(self.target / '.codex/state_5.sqlite')) as c, c:
            row = c.execute('SELECT rollout_path,sandbox_policy,approval_mode,memory_mode,creator_account_id,project_id FROM threads WHERE id=?', ('codex-session',)).fetchone()
            self.assertTrue(Path(row[0]).is_file())
            self.assertEqual(row[1:5], ('{"type":"read-only"}', 'on-request', 'disabled', None))
            self.assertNotEqual(row[5], 'project-1')
            self.assertEqual(c.execute('SELECT path FROM project_roots WHERE project_id=?', (row[5],)).fetchone()[0], str(self.target / '示例项目'))
            self.assertEqual(c.execute('SELECT approval_mode FROM threads WHERE id=?', ('existing-codex',)).fetchone()[0], 'never')
        restored_lines = list(open_lines(Path(row[0])))
        self.assertEqual(restored_lines[1]['payload']['approval_policy'], 'on-request')
        with zstandard.ZstdDecompressor().stream_reader(Path(row[0]).open('rb'), read_across_frames=True) as reader:
            restored_raw = reader.read()
        with closing(sqlite3.connect(self.target / '.codex/thread_history_1.sqlite')) as c, c:
            self.assertEqual(c.execute('SELECT status FROM thread_turns WHERE thread_id=?', ('codex-session',)).fetchone()[0], 'interrupted')
            self.assertEqual(c.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?', ('codex-session',)).fetchone()[0], len(restored_raw))
        self.assertEqual(self.source_before, fingerprints(self.source))


if __name__ == '__main__':
    unittest.main()

"""Exercise the Windows guide with isolated data stores."""

import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from ai_session_migration.adapters import LocalLibrary
from ai_session_migration.backup import make_backup, save_recovery_key
from ai_session_migration.importer import build_plan, execute_import, rollback_import
from ai_session_migration.packages import PackageLibrary
from ai_session_migration.windows_restore import claude_desktop, desktop, guide, planning, projects
from fixtures import make_home, make_import_target


class WindowsRestoreTests(unittest.TestCase):
    def test_claude_desktop_registration_stops_before_writes(self):
        home = self.root / "desktop-home"
        desktop = home / "AppData/Local/Claude-Data/claude_fixture"
        account = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        previous = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        organization = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        profile = desktop / "claude-code-sessions" / account / organization
        old_profile = desktop / "claude-code-sessions" / previous / organization
        profile.mkdir(parents=True)
        old_profile.mkdir(parents=True)
        (desktop / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account}), encoding="utf-8")
        cli = home / ".claude"
        session = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        old_session = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        same_title = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        local = "local_eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        worktree = self.project / ".claude" / "worktrees" / "economy-review"
        worktree.mkdir(parents=True)
        source_worktree = "/Users/source/project/.claude/worktrees/economy-review"
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(worktree))
        transcript = cli / "projects" / slug / (session + ".jsonl")
        transcript.parent.mkdir(parents=True)
        for sid in (session, old_session, same_title):
            (transcript.parent / (sid + ".jsonl")).write_bytes(b'{"type":"user"}\n')
        source = self.root / "desktop-index.zip"
        with zipfile.ZipFile(source, "w") as output:
            output.writestr("index.json", json.dumps({"sessionId": local, "cliSessionId": session,
                            "title": "Imported preview", "createdAt": 123, "accountUuid": "old account",
                            "cwd": source_worktree, "originCwd": "/Users/source/project",
                            "priorCliSessionIds": [old_session],
                            "permissionMode": "bypassPermissions", "remoteMcpServersConfig": ["ignore"]}))
        record = SimpleNamespace(provider="claude", child=False, uid="source", session_id=session,
                                 cwd=source_worktree, title="Preview", updated=456, archived=False)
        earlier = SimpleNamespace(**dict(vars(record), uid="earlier", session_id=old_session))
        unrelated = SimpleNamespace(**dict(vars(record), uid="unrelated", session_id=same_title, title="Imported preview"))
        draft = {"targets": {"claude": {"directory": str(cli)}}, "mappings": {record.cwd: str(worktree)}}
        library = SimpleNamespace(archive=source, entries={"source": {"sessionUid": record.uid,
                                  "role": "claude-desktop-index", "object": "index.json"}},
                                  scan=lambda: SimpleNamespace(records=[record, earlier, unrelated]))
        state = {"bulk": {"outcomes": {row.uid: {"status": "imported"} for row in (record, earlier, unrelated)}}}
        with self.assertRaisesRegex(ValueError, "Claude 历史"):
            claude_desktop.restore_sidebar(draft, library, state, self.root / "sidebar")
        self.assertEqual(list(profile.iterdir()), [])
        self.assertEqual(list(old_profile.iterdir()), [])
        self.assertEqual(transcript.read_bytes(), b'{"type":"user"}\n')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.target = self.root / "target"
        source_files = make_home(self.source)
        self.project = make_import_target(self.target)["project"]
        self.source_project = source_files["project"]

        claude_file = source_files["claude"]
        events = [json.loads(line) for line in claude_file.read_text(encoding="utf-8").splitlines()]
        events[1]["accountUuid"] = "test-mac-account"
        events[2]["ownerOrganizationUuid"] = "test-mac-org"
        claude_file.write_text("".join(json.dumps(line) + "\n" for line in events), encoding="utf-8")
        rollout = self.source / ".codex" / "sessions" / "test.jsonl"
        rollout.parent.mkdir()
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "codex-session", "cwd": str(self.source_project), "creator_account_id": "test-mac-account",
        }}) + "\n", encoding="utf-8")
        storage = self.source / ".dsh" / "storages" / "workspace.json"
        contents = json.loads(storage.read_bytes())
        contents["global"]["archivedSessionIds"].clear()
        storage.write_text(json.dumps(contents), encoding="utf-8")

        self.auth = self.target / ".codex" / "auth.json"
        self.auth.write_text('{"local":"test-windows-auth"}', encoding="utf-8")
        self.credentials = self.target / ".dsh" / ".credentials.yaml"
        self.credentials.write_text("token: test-windows-dsh", encoding="utf-8")
        self.targets = {p: self.target / name for p, name in (
            ("codex", ".codex"), ("claude", ".claude"), ("deepseek", ".dsh"),
        )}
        library = LocalLibrary(self.source, self.root / "source-cache")
        key = self.root / "key.asmkey"
        save_recovery_key(key)
        self.archive = Path(make_backup(library, library.scan().records, self.root / "export", key)["portable"])

    @unittest.skipUnless(sys.platform == "win32", "Windows guide entry point")
    def test_fresh_guide_creates_plan_trials_publishes_and_resumes(self):
        run = self.root / "run"
        manual = self.root / "manual.json"
        package = PackageLibrary(self.archive, self.root / 'mapping-cache')
        manual.write_text(json.dumps({r.cwd: str(self.project) for r in package.scan().records}), encoding='utf-8')
        command = ["guide", "--archive", str(self.archive), "--run-dir", str(run),
                   "--project-roots", str(self.target), str(self.root), "--mapping-file", str(manual), "--auto"]
        app = SimpleNamespace(app_id="test-app")
        with (
            patch.object(planning, "client_targets", return_value=self.targets),
            patch.object(guide, "guide_lock_cache", return_value=self.root / "global-lock"),
            patch.object(guide, "verify_claude", return_value={"resumed": True}),
            patch.object(guide, "verify_dsh", return_value={"listed": True, "resumed": True}),
            patch.object(guide, "verify_codex", return_value={"listed": True, "resumed": True}),
            patch.object(desktop, "installed_desktop", return_value=app),
            patch.object(desktop, "request_quit", return_value=True) as quit_app,
            patch.object(desktop, "reopen", return_value={"pid": 5}) as reopen_app,
            patch.object(projects, "restore_projects", return_value={"status": "verified"}),
        ):
            self.assertEqual(guide.main(command), 0)
            state = json.loads((run / "progress.json").read_bytes())
            self.assertTrue(all(row["nativeVerified"] for row in state["pilot"].values()))
            self.assertEqual(state["bulk"]["verifiedNativeIds"], {"codex": 1, "deepseek": 1})
            self.assertEqual(state["desktop"]["state"], "reopened")
            quit_app.assert_called_once_with(app)
            reopen_app.assert_called_once_with(app)
            self.assertEqual(guide.main(command), 0)
            self.assertEqual(json.loads((run / "progress.json").read_bytes())["bulk"]["batches"], [])
        audit = json.loads((run / "account-and-dsh-audit.json").read_bytes())
        self.assertNotIn("claude", audit)
        self.assertFalse(audit["deepseek"]["thirdPartyPluginsRestored"])
        self.assertEqual(self.auth.read_text(encoding="utf-8"), '{"local":"test-windows-auth"}')
        self.assertEqual(self.credentials.read_text(encoding="utf-8"), "token: test-windows-dsh")

    def test_scoped_codex_rollback_preserves_later_authentication_file_changes(self):
        cache = self.root / "cache"
        package = PackageLibrary(self.archive, cache)
        picked = [row for row in package.scan().records if row.provider == "codex"]
        plan = build_plan(package, picked, {"codex": str(self.targets["codex"])},
                          {row.cwd: str(self.project) for row in picked}, cache, codex_sessions=True)
        report = execute_import(plan, self.root / "backups", cache, confirmed_offline=True)
        self.assertEqual(report["roots"][0]["scope"], "codex-sessions")
        self.assertNotIn("auth.json", report["roots"][0]["before"]["files"])
        self.auth.write_text('{"local":"changed after the restore"}', encoding="utf-8")
        self.assertTrue(rollback_import(Path(report["journal"]), cache, confirmed_offline=True)["rollbackVerified"])
        self.assertEqual(self.auth.read_text(encoding="utf-8"), '{"local":"changed after the restore"}')

from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from ai_session_migration.adapters import LocalLibrary
from ai_session_migration.backup import decrypt_for_preview, estimate_backup, make_backup, rebuild_portable, save_recovery_key
from ai_session_migration.backup_versions import switch_backup_version
from ai_session_migration.file_restore import build_file_plan, execute_file_restore
from ai_session_migration.handoff_model import CONTEXT, CodexModel, CustomModel, GenerationError, compose_handoff
from ai_session_migration.history import ReadingCatalog, load_prompts
from ai_session_migration.importer import build_plan, execute_import
from ai_session_migration.packages import PackageLibrary
from ai_session_migration.portable import PortablePrivacyError, privacy_reference
from ai_session_migration.reading_transfer import ReadingSource, materialize_readings
from ai_session_migration.transfer_gui import mapped_project
from ai_session_migration.work_projects import ProjectView, _git, discover_projects, git_remote, prepare_git, restore_git
from fixtures import make_home, make_import_target
from test_history import source_fixture


class PortableMigrationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / "home"
        self.paths = make_home(self.home)
        self.library = LocalLibrary(self.home, self.base / "source-cache")
        self.key = self.base / "recovery.asmkey"
        save_recovery_key(self.key)

    def export(self, library=None, **options):
        library = library or self.library
        result = make_backup(library, library.scan().records, self.base / "exports", self.key, **options)
        return PackageLibrary(Path(result["portable"]), self.base / "target-cache"), result

    def test_switch_preserves_the_complete_old_backup_and_recovers_an_interrupted_move(self):
        _, old = self.export()
        old_root = Path(old["output"])
        old_only = old_root / "older-note.txt"
        old_only.write_bytes(b"remain with the previous version")
        records = self.library.scan().records
        new = make_backup(self.library, [r for r in records if r.provider == "codex"], old_root.parent, self.key)
        new_root = Path(new["output"])
        original_files = {p.name: p.read_bytes() for p in old_root.iterdir() if p.is_file()}
        new_zip = Path(new["portable"]).read_bytes()
        wrong = self.base / "wrong.asmkey"
        save_recovery_key(wrong)
        with self.assertRaises(ValueError):
            switch_backup_version(new, old, wrong, self.base / "switch-cache")
        self.assertEqual(old_only.read_bytes(), original_files[old_only.name])
        self.assertTrue(new_root.is_dir())
        self.assertFalse(list(old_root.parent.glob("会话备份-旧版-*")))

        from ai_session_migration.importer import rename_new

        def interrupted(source, target):
            if source == new_root:
                raise SystemExit("synthetic interruption after preserving old version")
            rename_new(source, target)

        with patch("ai_session_migration.backup_versions.rename_new", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                switch_backup_version(new, old, self.key, self.base / "switch-cache")
        self.assertFalse(old_root.exists())
        self.assertTrue(new_root.is_dir())
        active, earlier = switch_backup_version(new, old, self.key, self.base / "switch-cache")
        self.assertEqual(Path(active["output"]), old_root)
        self.assertEqual(Path(active["portable"]).read_bytes(), new_zip)
        self.assertFalse(new_root.exists())
        preserved = Path(earlier["output"])
        self.assertEqual({p.name: p.read_bytes() for p in preserved.iterdir() if p.is_file()}, original_files)
        self.assertEqual(active["sessions"], 1)
        self.assertEqual(earlier["sessions"], 3)
        self.assertEqual(switch_backup_version(new, old, self.key, self.base / "switch-cache"), (active, earlier))

    def test_legacy_archive_converts_codex_and_reads_claude_without_desktop_index(self):
        account = "fixture-legacy-account-21be"
        state_db = self.home / ".codex/state_5.sqlite"
        history_db = self.home / ".codex/thread_history_1.sqlite"
        rollout = self.home / ".codex/sessions/test.jsonl"
        rollout.parent.mkdir()
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "codex-session", "cwd": str(self.paths["project"]), "creator_account_id": account,
        }}) + "\n", encoding="utf-8")
        original = rollout.read_bytes()
        with closing(sqlite3.connect(state_db)) as db, db:
            db.execute("ALTER TABLE threads ADD COLUMN creator_account_id TEXT")
            db.execute("UPDATE threads SET creator_account_id=?", (account,))
        child = self.paths["claude"].with_suffix("") / "subagents/agent-fixture.jsonl"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "legacy child fixture"}}) + "\n",
                         encoding="utf-8")
        roots = {"codex-home": ("codex", self.home / ".codex"), "claude-code": ("claude-code", self.home / ".claude")}
        old = self.base / "conversation-migration-1.zip"
        entries = []
        with zipfile.ZipFile(old, "x") as archive:
            for n, (label, path) in enumerate([["codex-home", item] for item in (state_db, history_db, rollout)]
                                                + [["claude-code", item] for item in (self.paths["claude"], child)]):
                data = path.read_bytes()
                name = f"objects/{n:08d}"
                entries.append({"root": label, "path": path.relative_to(roots[label][1]).as_posix(), "object": name,
                                "capturedSize": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                archive.writestr(name, data)
            manifest = {"schema": "conversation-migration-1", "entries": entries,
                        "roots": [{"kind": kind, "label": label, "path": str(path)} for label, (kind, path) in roots.items()]}
            archive.writestr("manifest.json", json.dumps(manifest))
        old_bytes = old.read_bytes()
        rollout.write_bytes(b"newer live content outside old package\n")

        legacy = PackageLibrary(old, self.base / 'legacy-cache')
        records = legacy.scan().records
        self.assertEqual(sorted((r.provider, r.child) for r in records), [("claude", False), ("claude", True), ("codex", False)])
        codex = next(r for r in records if r.provider == "codex")
        with patch.object(legacy, "legacy_rollout", side_effect=AssertionError("reading SQLite should not extract a rollout")):
            self.assertGreater(legacy.page(codex).total, 0)
        agent = next(r for r in records if r.child)
        self.assertIn("legacy child fixture", legacy.page(agent).messages[0].text)
        with self.assertRaisesRegex(ValueError, "只支持预览"):
            build_plan(legacy, [codex], {}, {}, self.base / "target-cache")

        package, report = self.export(legacy)
        self.assertTrue(report["privacyCheck"]["verified"])
        self.assertEqual(old.read_bytes(), old_bytes)
        with zipfile.ZipFile(package.archive) as archive:
            self.assertTrue(all(account.encode() not in archive.read(name) for name in archive.namelist()))
        decrypted = decrypt_for_preview(Path(report["encrypted"]), self.key, self.base / "private-readback")
        with zipfile.ZipFile(decrypted) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            old_rollout = next(e for e in manifest["entries"] if e["role"] == "original-session"
                               and e["sessionUid"] == codex.uid)
            self.assertEqual(archive.read(old_rollout["object"]), original)
        self.assertEqual(rollout.read_bytes(), b"newer live content outside old package\n")
        target = self.base / "target"
        make_import_target(target)
        converted = package.scan().records
        codex = next(r for r in converted if r.provider == "codex")
        mappings = {codex.cwd: str(target / "示例项目")}
        targets = {"codex": str(target / ".codex")}
        plan = build_plan(package, [codex], targets, mappings, self.base / "target-cache")
        self.assertEqual(len(plan.ready), 1, plan.items)
        self.assertTrue(plan.ready[0]["codex_rollout"])
        restored = execute_import(plan, self.base / "target-backups", self.base / "target-cache", confirmed_offline=True)
        self.assertEqual(restored["imported"], 1)
        self.assertTrue(restored["nativeReadback"][0]["historyReadback"])
        with self.assertRaisesRegex(ValueError, "Claude 历史仅供阅读"):
            build_plan(package, [r for r in converted if r.provider == "claude"], targets, mappings, self.base / "target-cache")
        readings = package.package_catalog["readings"]
        main = next(r for r in converted if r.provider == "claude" and not r.child)
        self.assertEqual(readings[agent.uid]["parentUid"], main.uid)

    def test_codex_missing_rollout_blocks_import_if_pagination_depends_on_it(self):
        db_path = self.home / ".codex/thread_history_1.sqlite"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("ALTER TABLE thread_items ADD COLUMN rollout_byte_offset INTEGER")
            db.execute("UPDATE thread_items SET rollout_byte_offset=17 WHERE item_id='1'")
        package, _ = self.export()
        target = self.base / "target"
        make_import_target(target)
        codex = next(r for r in package.scan().records if r.provider == "codex")
        plan = build_plan(package, [codex], {"codex": str(target / ".codex")},
                          {codex.cwd: str(target / "示例项目")}, self.base / "target-cache")
        self.assertFalse(plan.ready)
        self.assertIn("分页索引引用了原始 rollout", plan.items[0]["reason"])

    def test_account_fields_cleared_from_catalog_native_rows_and_rollouts(self):
        old_creator = "fixture-account-92b0ac"
        old_org = "fixture-org-840cf6"
        log = self.home / ".codex/sessions/rollout-codex-session.jsonl"
        log.parent.mkdir()
        log.write_text(
            "\n".join(json.dumps({"type": "session_meta", "payload": {
                "id": sid, "creator_account_id": old_creator, "organizationId": old_org,
                "cwd": str(self.paths["project"]),
            }}) for sid in ("codex-session", "parent-synthetic")) + "\n", encoding="utf-8",
        )
        with closing(sqlite3.connect(self.home / ".codex/state_5.sqlite")) as db, db:
            db.execute("ALTER TABLE threads ADD COLUMN creator_account_id TEXT")
            db.execute("UPDATE threads SET creator_account_id=?, rollout_path=?", (old_creator, str(log)))
        package, result = self.export()
        self.assertTrue(result["privacyCheck"]["verified"])
        with zipfile.ZipFile(package.archive) as source:
            for name in source.namelist():
                body = source.read(name)
                self.assertNotIn(old_creator.encode(), body)
                self.assertNotIn(old_org.encode(), body)
        private = decrypt_for_preview(Path(result["encrypted"]), self.key, self.base / "private")
        with zipfile.ZipFile(private) as source:
            manifest = json.loads(source.read("manifest.json"))
            entry = next(e for e in manifest["entries"] if e.get("originalPath") == str(log))
            self.assertEqual(source.read(entry["object"]), log.read_bytes())

    def test_codex_export_and_native_restore_follow_structured_attachment_paths(self):
        attachment = self.home / ".codex/generated_images" / "fixture image.png"
        attachment.parent.mkdir()
        attachment.write_bytes(b"synthetic image bytes")
        outside = self.base / "outside workflow.png"
        outside.write_bytes(b"outside capture roots")
        email = "account-fixture@example.test"
        item = {"type": "userMessage", "id": "u", "content": [
            {"type": "input_image", "filePath": str(attachment)},
            {"type": "image_url", "image_url": {"url": attachment.as_uri()}},
            {"type": "input_file", "localPath": str(attachment)},
            {"type": "input_file", "filePath": str(outside)},
            {"type": "text", "text": "这行是用户原文，不应改写。"},
        ], "metadata": ' \n  ' + json.dumps({"userEmail": email, "caption": "safe fixture"})}
        with closing(sqlite3.connect(self.home / ".codex/thread_history_1.sqlite")) as db, db:
            db.execute("UPDATE thread_items SET item_json=? WHERE item_id='0'", (json.dumps(item),))
        package, report = self.export()
        self.assertTrue(report["privacyCheck"]["verified"])
        objects = [e for e in package.entries.values() if e["role"] == "attachment"]
        self.assertEqual(sum(e.get("displayName") == attachment.name for e in objects), 1)
        self.assertFalse(any(e.get("displayName") == outside.name for e in objects))
        with zipfile.ZipFile(package.archive) as archive:
            for name in archive.namelist():
                payload = archive.read(name)
                self.assertNotIn(email.encode(), payload)
                self.assertNotIn(attachment.as_uri().encode(), payload)
                self.assertNotIn(str(outside).replace("\\", "/").encode(), payload)

        target = self.base / "target"
        make_import_target(target)
        record = next(r for r in package.scan().records if r.provider == "codex")
        plan = build_plan(package, [record], {"codex": str(target / ".codex")},
                          {record.cwd: str(target / "示例项目")}, self.base / "plan-cache")
        self.assertEqual(len(plan.ready), 1, plan.items)
        result = execute_import(plan, self.base / "restore-backups", self.base / "plan-cache", confirmed_offline=True)
        self.assertEqual(result["status"], "complete")
        with closing(sqlite3.connect(target / ".codex/thread_history_1.sqlite")) as db:
            row = db.execute("SELECT item_json FROM thread_items WHERE thread_id='codex-session' AND item_id='0'").fetchone()
        restored = json.loads(row[0])
        content = restored["content"]
        local = Path(content[0]["filePath"])
        self.assertEqual(local.read_bytes(), attachment.read_bytes())
        self.assertEqual(content[1]["image_url"]["url"], local.as_uri())
        self.assertEqual(content[2]["localPath"], str(local))
        self.assertTrue(content[3]["filePath"].startswith("/unmapped/"))
        self.assertEqual(content[4], item["content"][4])
        self.assertEqual(json.loads(restored["metadata"]), {"caption": "safe fixture"})

    def test_three_provider_project_is_stored_once_and_existing_file_is_preserved(self):
        project = self.paths["project"]
        for name in ("present.txt", "new.txt"):
            (project / name).write_text("archived " + name)
        self.library.scan()
        projects = discover_projects(self.library.catalog.records, self.home)
        self.assertEqual(len(projects), 1)
        pid = projects[0].id
        package, _ = self.export(project_choices={pid: ("files", False)})
        items = [e for e in package.entries.values() if e["role"] == "project-work-file"]
        self.assertEqual(sorted(e["projectPath"] for e in items), ["new.txt", "present.txt"])
        self.assertTrue(all(len(e["sessionUids"]) == 3 for e in items))
        view = ProjectView(package, pid)
        target = self.base / "restored"
        target.mkdir()
        (target / "present.txt").write_text("local value")
        plan = build_file_plan(view, list(view.items), (), target, self.base / "target-backups")
        self.assertEqual(plan["new"], 1)
        result = execute_file_restore(view, plan)
        self.assertTrue(result["backupVerified"])
        self.assertEqual((target / "present.txt").read_text(), "local value")
        self.assertEqual((target / "new.txt").read_text(), "archived new.txt")

    def test_backup_space_preview_counts_shared_work_files_and_uses_disk_headroom(self):
        project = self.paths["project"]
        data = b"work data" * 2000
        (project / "notes.txt").write_bytes(data)
        (project / ".env").write_bytes(b"excluded synthetic credentials")
        records = self.library.scan().records
        project_id = discover_projects(records, self.home)[0].id
        output = self.base / "not-created-yet"
        linked = estimate_backup(self.library, records, output, self.key)
        selected = estimate_backup(self.library, records, output, self.key, project_choices={project_id: ("files", False)})
        self.assertFalse(output.exists())
        self.assertEqual(linked["projects"][0]["bytes"], 0)
        self.assertEqual(selected["projects"], [{"title": project.name, "mode": "files", "files": 1, "bytes": len(data)}])
        self.assertGreater(selected["portableBytes"], linked["portableBytes"])
        self.assertGreater(selected["encryptedBytes"], linked["encryptedBytes"])
        self.assertGreater(selected["outputTemporaryBytes"], linked["outputTemporaryBytes"])
        self.assertTrue(selected["sameVolume"])

        with patch("ai_session_migration.backup.shutil.disk_usage", return_value=SimpleNamespace(free=2)):
            low = estimate_backup(self.library, records, output, self.key)
        self.assertEqual(low["outputFree"], 2)
        self.assertFalse(low["enough"])

        package, result = self.export(project_choices={project_id: ("files", False)})
        with patch.object(package, "_extract", side_effect=AssertionError("preview must use only the package manifest")):
            packaged = estimate_backup(package, package.scan().records, output, self.key, include_projects=True)
        self.assertEqual(packaged["projects"], selected["projects"])
        window = result["snapshotWindow"]
        self.assertEqual(window["selectedSessions"], 3)
        self.assertEqual(window["nativeDatabaseObjects"], 1)
        self.assertLessEqual(window["startedAt"], window["finishedAt"])
        self.assertEqual(package.manifest["snapshotWindow"], window)

    def test_git_restore_cannot_write_inside_configured_client_storage(self):
        package, _ = self.export()
        for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "DSH_HOME"):
            root = self.base / variable
            root.mkdir()
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: str(root)}):
                with patch("ai_session_migration.work_projects._fetch_git") as fetch:
                    with self.assertRaisesRegex(ValueError, "原生会话资料"):
                        restore_git({}, package, root / "checked-out")
                    fetch.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_prepared_reading_is_self_contained_and_reused_without_model_call(self):
        directory, drafts = source_fixture(self.base)
        view = ReadingSource({"readingRoot": str(directory), "preparedPath": str(drafts)}, self.base / "reader")
        def unexpected(*args):
            self.fail("prepared draft should be reused")
        package, result = self.export(view, model=unexpected)
        self.assertEqual(result["handoff"], {"reused": 1, "child": 1})
        detached = self.base / "detached.zip"
        shutil.copyfile(package.archive, detached)
        package = PackageLibrary(detached, self.base / "detached-cache")
        config = materialize_readings(package)
        prompts, fingerprint = load_prompts(extra=config["extraPromptsPath"])
        target = ReadingCatalog(config["readingRoot"], prompts, fingerprint).scan()
        self.assertTrue(any(r.kind == "agent" for r in target.entries))
        self.assertIn("待复核", prompts["0001"].prompt)
        self.assertEqual(target.read(next(r for r in target.entries if r.number == "0001"))[0][0].start, 5)

    def test_request_failure_keeps_reading_and_can_retry_only_selected_session(self):
        def offline(*args):
            raise GenerationError("synthetic offline")
        failed, result = self.export(model=offline)
        self.assertEqual(result["handoff"], {"failed": 1})
        reading = materialize_readings(failed)
        self.assertTrue(reading["readingRoot"])
        self.assertEqual(reading["handoffStates"]["0001"]["status"], "failed")
        selected = [r for r in failed.scan().records if r.provider == "claude"]
        def response(text, check):
            check()
            self.assertNotIn("fixture-claude-thinking", text)
            return "请核对历史消息 #1 和项目当前状态。"
        retried = make_backup(failed, selected, self.base / "retry", self.key, model=response)
        self.assertEqual(retried["handoff"], {"generated": 1})
        self.assertEqual(retried["sessions"], 1)

    def test_model_input_excludes_known_account_marker_from_catalog(self):
        marker = "fixture-catalog-only-account-2b61"
        event = {"type": "user", "message": {"role": "user", "content": f"历史中记录了 {marker}"}}
        with self.paths["claude"].open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        catalog = self.library.scan()
        record = next(r for r in catalog.records if r.provider == "codex")
        record.metadata["creator_account_id"] = marker
        requests = []

        def response(text, check):
            check()
            requests.append(text)
            return "请核对历史消息 #1。"

        with self.assertRaisesRegex(ValueError, "迁移包账号检查命中") as failed:
            make_backup(self.library, catalog.records, self.base / "exports", self.key, model=response)
        self.assertEqual(len(requests), 1)
        self.assertIn("历史中记录了 [已隐去]", requests[0])
        self.assertNotIn(marker, requests[0])
        self.assertTrue(failed.exception.report["encryptedReadbackVerified"])

        # The original catalog is still the authority for known values on every retry.
        report = failed.exception.report
        self.assertNotIn(marker, json.dumps(report, ensure_ascii=False))
        with self.assertRaises(PortablePrivacyError) as retry:
            rebuild_portable(report, self.key, {"sessions": [privacy_reference(record.uid)]})
        self.assertTrue(any(hit.get("line") and hit.get("field") == "message.content"
                            and hit["sessions"][0]["provider"] == "claude" for hit in retry.exception.findings))
        claude = next(r for r in catalog.records if r.provider == "claude")
        rebuilt = rebuild_portable(retry.exception.report, self.key, {"sessions": [privacy_reference(claude.uid)]})
        migrated = PackageLibrary(Path(rebuilt["portable"]), self.base / "filtered-cache")
        self.assertEqual([r.provider for r in migrated.scan().records], ["deepseek"])
        self.assertEqual(len(rebuilt["privacyExclusions"]["sessions"]), 2)
        self.assertEqual(rebuilt["encrypted"], report["encrypted"])

    def test_privacy_retry_excludes_one_project_file_from_same_encrypted_capture(self):
        key_text = "sk-proj-" + "z" * 32
        flagged = self.paths["project"] / "excluded.txt"
        kept = self.paths["project"] / "kept.txt"
        flagged.write_text(key_text, encoding="utf-8")
        kept.write_bytes(b"keep original work bytes")
        records = self.library.scan().records
        choice = discover_projects(records, self.home)[0]
        with self.assertRaises(PortablePrivacyError) as error:
            make_backup(self.library, records, self.base / "exports", self.key,
                        project_choices={choice.id: ("files", False)})
        report = error.exception.report
        flagged_result = next(f for f in report["privacyCheck"]["findings"] if f.get("projectPath") == flagged.name)
        self.assertEqual(flagged_result["role"], "project-work-file")
        self.assertEqual(len(flagged_result["sessions"]), 3)
        self.assertNotIn(key_text, json.dumps(report, ensure_ascii=False))
        self.assertFalse(list(Path(report["output"]).glob("*.zip")))
        encrypted = Path(report["encrypted"])
        prior = encrypted.read_bytes()
        old_report = (encrypted.parent / "结果.json").read_bytes()
        kept.write_bytes(b"new source content after capture")
        result = rebuild_portable(dict(report, privacyExclusions=None), self.key, {"objects": [flagged_result["locator"]]})
        self.assertTrue(result["privacyCheck"]["verified"])
        self.assertEqual(prior, encrypted.read_bytes())
        self.assertEqual(old_report, (encrypted.parent / "结果.json").read_bytes())
        self.assertEqual(flagged.read_text(encoding="utf-8"), key_text)
        self.assertFalse(list(Path(result["output"]).glob(".snapshot-*")))
        package = PackageLibrary(Path(result["portable"]), self.base / "rebuilt-cache")
        self.assertEqual(len(package.scan().records), 3)
        view = ProjectView(package, choice.id)
        self.assertEqual(len(view.items), 1)
        with view.open_item(next(iter(view.items))) as stream:
            self.assertEqual(stream.read(), b"keep original work bytes")

    def test_model_reply_credential_fails_only_the_draft(self):
        credential = "sk-proj-" + "x" * 32
        package, result = self.export(model=lambda *_: credential)
        self.assertEqual(result["handoff"], {"failed": 1})
        self.assertTrue(result["privacyCheck"]["verified"])
        self.assertTrue(materialize_readings(package)["readingRoot"])
        self.assertFalse(any(e.get("role") == "handoff-draft" for e in package.entries.values()))

    def test_custom_model_timeout_keeps_the_original_reading(self):
        model = CustomModel("http://localhost:4321/v1", "fixture", "chat/completions")

        def timeout(*args, **kwargs):
            raise TimeoutError("synthetic model timed out")

        with patch("ai_session_migration.handoff_model.build_opener", return_value=SimpleNamespace(open=timeout)):
            package, report = self.export(model=model)
        self.assertEqual(report["handoff"], {"failed": 1})
        self.assertTrue(report["encryptedReadbackVerified"])
        self.assertTrue(materialize_readings(package)["readingRoot"])

    def test_oversize_message_carries_its_source_location_through_all_chunks(self):
        body = "合成的长会话文本。" * (CONTEXT * 4)
        message = SimpleNamespace(number=7, start=18, end=21, body=body)
        anchor = "[消息 #7，第 18–21 行]\n"
        parts, merges = [], []

        def model(prompt, check):
            check()
            self.assertLessEqual(len(prompt), CONTEXT)
            if "合并以下段落纪要" in prompt:
                merges.append(prompt)
            else:
                self.assertIn(anchor, prompt)
                parts.append(prompt.split(anchor, 1)[1][:-2])
            return "请核对消息 #7，第 18–21 行。" + "摘要" * 6000

        draft = compose_handoff([message], model, lambda: None)
        self.assertGreater(len(parts), 25)
        self.assertGreater(len(merges), 5)
        self.assertEqual("".join(parts), body)
        self.assertIn("消息 #7，第 18–21 行", draft)

    def test_custom_protocols_use_only_selected_endpoint_and_do_not_log_key(self):
        from io import BytesIO
        for protocol, output in (("chat/completions", {"choices": [{"message": {"content": "draft"}}]}),
                                 ("responses", {"output_text": "draft"})):
            client = CustomModel("https://model.example.test/v1", "fixture", protocol, "synthetic-secret")
            with patch("ai_session_migration.handoff_model.build_opener") as opener:
                opener.return_value.open.return_value = BytesIO(json.dumps(output).encode())
                self.assertEqual(client("sample dialogue", lambda: None), "draft")
                req = opener.return_value.open.call_args.args[0]
                self.assertEqual(req.full_url, "https://model.example.test/v1/" + protocol)
                self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-secret")
                self.assertNotIn(b"synthetic-secret", req.data)
            self.assertNotIn("synthetic-secret", repr(client))
        self.assertEqual(git_remote("https://name:fake-key@git.example.test/group/repo.git?auth=value"),
                         "https://git.example.test/group/repo.git")

    def test_codex_handoff_accepts_model_metadata_notice_and_requires_completed_turn(self):
        model = CodexModel("codex-fixture", "fixture", "custom-model", (), "http://127.0.0.1:1234/v1")
        notice = {"type": "item.completed", "item": {"type": "error", "message":
                  "Model metadata for `custom-model` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."}}
        draft = {"type": "item.completed", "item": {"type": "agent_message", "text": "Draft"}}
        finished = {"type": "turn.completed"}
        tool = {"type": "item.completed", "item": {"type": "command_execution", "status": "completed"}}
        with patch("ai_session_migration.handoff_model.subprocess.Popen") as launch:
            worker = launch.return_value
            worker.returncode = worker.poll.return_value = 0
            for stream, succeeds in (([notice, draft, finished], True), ([draft], False),
                                     ([draft, tool, finished], False)):
                worker.communicate.return_value = (b"\n".join(json.dumps(e).encode() for e in stream), None)
                if succeeds:
                    self.assertEqual(model("fixture", lambda: None), "Draft")
                    args = launch.call_args.args[0]
                    self.assertIn("goals", args)
                    self.assertEqual(worker.communicate.call_args.kwargs["input"], b"fixture")
                else:
                    with self.assertRaises(GenerationError):
                        model("fixture", lambda: None)

    @unittest.skipUnless(shutil.which("git"), "testing a worktree requires Git")
    def test_git_mode_carries_local_changes_over_the_exact_base_commit(self):
        from ai_session_migration.restore_batch import review_projects
        from threading import Event

        project = self.paths["project"]
        _git(project, "init", "--quiet")
        for filename in ("tracked.txt", "deleted.txt"):
            (project / filename).write_text("committed")
        _git(project, "add", ".")
        _git(project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
             "commit", "--quiet", "-m", "Test base")
        _git(project, "remote", "add", "origin", "https://git.example.test/repo.git")
        (project / "tracked.txt").write_text("local edit")
        (project / "deleted.txt").unlink()
        (project / "new.txt").write_text("untracked")
        (project / "staged.txt").write_text("staged addition")
        _git(project, "add", "staged.txt")
        choices = discover_projects(self.library.scan().records, self.home)
        self.assertEqual(len(choices), 1)
        choice = choices[0]
        self.assertEqual(choice.remote_status, "unverified-or-local-only")
        package, _ = self.export(project_choices={choice.id: ("git", True)})
        item = package.package_catalog["projects"][0]
        self.assertEqual(item["changes"], {"tracked.txt": "modified", "deleted.txt": "deleted", "new.txt": "untracked", "staged.txt": "added"})

        def local_git(cwd, *args, **kwargs):
            if "fetch" in args:
                return _git(cwd, "fetch", "--quiet", "--", str(project), choice.commit)
            return _git(cwd, *args, **kwargs)
        destination = self.base / "git-target"
        with patch("ai_session_migration.work_projects._git", side_effect=local_git):
            operation, = review_projects(package, {item["id"]: ("git", str(destination))}, (),
                                         self.base / "restore-backups", lambda *_: None, Event())
            review = operation.git
            self.addCleanup(operation.close)
            self.assertEqual(review.commit, choice.commit)
            self.assertFalse(destination.exists())
            self.assertEqual((review.staged / "tracked.txt").read_text(), "committed")
            self.assertEqual({row.name: row.action for row in review.changes}, item["changes"])
            self.assertEqual(operation.planned_directories({destination}), {destination})
            report = operation.execute(lambda *_: None, Event())
            self.assertEqual(report["commit"], choice.commit)
            self.assertEqual((report["added"], report["modified"], report["deleted"]), (2, 1, 1))
            operation.close()

            bad = dict(item, changes={"tracked.txt": "untracked"})
            conflict_target = self.base / "conflict-target"
            conflict = prepare_git(bad, package, conflict_target)
            self.addCleanup(conflict.close)
            self.assertIn("基线已存在", conflict.changes[0].conflict)
            with self.assertRaisesRegex(ValueError, "存在冲突"):
                conflict.publish()
            self.assertFalse(conflict_target.exists())
            conflict.close()

            stale = prepare_git(item, package, self.base / "drift-target")
            self.addCleanup(stale.close)
            (stale.staged / "tracked.txt").write_text("changed after preview")
            with self.assertRaisesRegex(ValueError, "在预览后变化"):
                stale.publish()
            self.assertFalse(stale.destination.exists())
            stale.close()
        self.assertEqual(_git(destination, "rev-parse", "HEAD"), choice.commit)
        self.assertEqual((destination / "tracked.txt").read_text(), "local edit")
        self.assertEqual((destination / "new.txt").read_text(), "untracked")
        self.assertEqual((destination / "staged.txt").read_text(), "staged addition")
        self.assertFalse((destination / "deleted.txt").exists())

        remote = self.base / "remote-without-local-commit"
        remote.mkdir()
        _git(remote, "init", "--bare", "--quiet")

        def unreachable_commit(cwd, *args, **kwargs):
            if "fetch" in args:
                return _git(cwd, "fetch", "--quiet", "--", str(remote), choice.commit)
            return _git(cwd, *args, **kwargs)

        deleted_target = self.base / "unavailable-target"
        with patch("ai_session_migration.work_projects._git", side_effect=unreachable_commit):
            with self.assertRaisesRegex(ValueError, "可改为关联已有目录"):
                prepare_git(item, package, deleted_target)
        self.assertFalse(deleted_target.exists())
        self.assertFalse(any(self.base.glob(".asm-git-*")))
        self.assertTrue(all(mapped_project(record, {choice.id: str(destination)}) == str(destination)
                            for record in package.scan().records))

    @unittest.skipUnless(shutil.which("git"), "testing Git projects requires Git")
    def test_git_project_reports_local_lfs_content_and_pinned_submodule(self):
        project = self.paths["project"]
        _git(project, "init", "--quiet")
        (project / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
        pointer = "version https://git-lfs.github.com/spec/v1\noid sha256:" + "b" * 64 + "\nsize 10\n"
        for filename in ("pointer.bin", "materialized.bin"):
            (project / filename).write_text(pointer, encoding="utf-8")
        _git(project, "-c", "filter.lfs.process=", "-c", "filter.lfs.clean=", "-c", "filter.lfs.required=false", "add", ".")
        identity = ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test")
        _git(project, *identity, "commit", "--quiet", "-m", "Initial tree")
        pin = _git(project, "rev-parse", "HEAD")
        _git(project, "update-index", "--add", "--cacheinfo", f"160000,{pin},vendor/module")
        _git(project, *identity, "commit", "--quiet", "-m", "Pin a submodule")
        _git(project, "remote", "add", "origin", "https://git.example.test/repo.git")
        (project / "materialized.bin").write_bytes(b"local synthetic LFS data")
        scanned = discover_projects(self.library.scan().records, self.home)[0]
        self.assertTrue(scanned.dependencies_checked)
        self.assertEqual(scanned.lfs_files, {"materialized.bin": "content", "pointer.bin": "pointer"})
        self.assertEqual(scanned.submodules, {"vendor/module": {"commit": pin, "localCheckout": False}})
        package, _ = self.export(project_choices={scanned.id: ("git", False)})
        git = package.package_catalog["projects"][0]["git"]
        self.assertEqual(git["lfsFiles"], scanned.lfs_files)
        self.assertEqual(git["submodules"], scanned.submodules)

    @unittest.skipUnless(shutil.which("git"), "testing Git worktrees requires Git")
    def test_linked_worktrees_and_independent_projects_with_same_name_stay_distinct(self):
        primary = self.paths["project"]
        _git(primary, "init", "--quiet")
        _git(primary, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
             "commit", "--allow-empty", "--quiet", "-m", "Initial tree")
        linked = self.base / "linked" / primary.name
        linked.parent.mkdir()
        _git(primary, "worktree", "add", "--quiet", "--detach", str(linked))
        independent = self.base / "independent" / primary.name
        independent.mkdir(parents=True)
        selected = [replace(record, cwd=str(root))
                    for record, root in zip(self.library.scan().records, (primary, linked, independent))]
        projects = discover_projects(selected, self.home)
        self.assertEqual({project.root for project in projects}, {primary, linked, independent})
        self.assertEqual(len({project.id for project in projects}), 3)
        self.assertEqual(sum(len(project.members) for project in projects), 3)

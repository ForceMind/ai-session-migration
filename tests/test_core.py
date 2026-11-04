import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import zipfile

from ai_session_migration.adapters import LocalLibrary
from ai_session_migration.backup import make_backup, save_recovery_key, verify_package, verify_encrypted, decrypt_for_preview, resume_backup, Cancelled
from ai_session_migration.packages import PackageLibrary
from ai_session_migration.safeio import open_lines, hash_file
from fixtures import make_home


def fingerprints(root):
    return {str(p.relative_to(root)): hash_file(p) for p in root.rglob('*') if p.is_file()}


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'source'
        self.files = make_home(self.home)
        self.library = LocalLibrary(self.home, self.base / 'cache')

    def tearDown(self):
        self.temp.cleanup()

    def test_three_sources_read_thinking_and_leave_every_source_byte_unchanged(self):
        before = fingerprints(self.home)
        catalog = self.library.scan()
        self.assertEqual(len(catalog.records), 3)
        self.assertEqual({r.provider for r in catalog.records}, {'claude', 'codex', 'deepseek'})
        for r in catalog.records:
            page = self.library.page(r)
            self.assertTrue(any(m.kind == 'thinking' for m in page.messages), r.provider)
            self.assertTrue(any(m.role == 'user' for m in page.messages), r.provider)
            self.assertTrue(any(m.role == 'assistant' for m in page.messages), r.provider)
        self.assertEqual(before, fingerprints(self.home))

    def test_deepseek_concatenated_frames_ownership_archive_and_attachment(self):
        self.assertEqual(len(list(open_lines(self.files['deepseek']))), 3)
        record = next(r for r in self.library.scan().records if r.provider == 'deepseek')
        self.assertEqual(record.title, '梳理文档的阅读顺序')
        self.assertTrue(record.archived)
        self.assertEqual(record.metadata['workspace_id'], 'project')
        page = self.library.page(record)
        self.assertEqual(page.attachments[0].name, '设置页参考.png')
        self.assertTrue(Path(page.attachments[0].path).is_file())

    def test_sqlite_committed_wal_visible_without_source_sidecar_writes(self):
        dbfile = self.home / '.codex/thread_history_1.sqlite'
        db = sqlite3.connect(dbfile)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('INSERT INTO thread_items VALUES (?,?,?,?,?)', ('codex-session', 'later', 'later', 99, json.dumps({'type':'agentMessage','text':'committed-later-in-wal'})))
        db.commit()
        before = fingerprints(self.home)
        record = next(r for r in self.library.scan().records if r.provider == 'codex')
        self.assertTrue(any(m.text == 'committed-later-in-wal' for m in self.library.page(record).messages))
        self.assertEqual(before, fingerprints(self.home))
        db.close()

    def test_backup_roundtrip_keeps_original_files_and_all_three_histories(self):
        before = fingerprints(self.home)
        catalog = self.library.scan()
        key = self.base / 'recovery.asmkey'
        save_recovery_key(key)
        report = make_backup(self.library, catalog.records, self.base / 'backups', key)
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(report['encryptedReadbackVerified'])
        self.assertTrue(verify_package(Path(report['portable']))['verified'])
        self.assertTrue(verify_encrypted(Path(report['encrypted']), key, report['plainSha256']))
        package = PackageLibrary(Path(report['portable']), self.base / 'package-cache')
        self.assertEqual(len(package.scan().records), 3)
        for record in package.catalog.records:
            self.assertTrue(any(m.kind == 'thinking' for m in package.page(record).messages))
            if record.provider in {'claude','deepseek'}:
                self.assertTrue(package.page(record).attachments)
        with zipfile.ZipFile(report['portable']) as z:
            manifest = json.loads(z.read('manifest.json'))
            self.assertFalse(any(e.get('originalPath', '').endswith('.credentials.yaml') for e in manifest['entries']))
            c = next(e for e in manifest['entries'] if e.get('originalPath') == str(self.files['claude']))
            self.assertEqual(z.read(c['object']), self.files['claude'].read_bytes())
            d = next(e for e in manifest['entries'] if e.get('originalPath') == str(self.files['deepseek']))
            self.assertEqual(z.read(d['object']), self.files['deepseek'].read_bytes())
        self.assertEqual(before, fingerprints(self.home))

    def test_key_never_overwritten_and_wrong_key_rejected(self):
        key = self.base / 'key.asmkey'
        save_recovery_key(key)
        original = key.read_bytes()
        with self.assertRaises(FileExistsError): save_recovery_key(key)
        self.assertEqual(key.read_bytes(), original)
        records = self.library.scan().records[:1]
        report = make_backup(self.library, records, self.base / 'backups', key)
        other = self.base / 'other.asmkey'
        save_recovery_key(other)
        with self.assertRaises(ValueError):verify_encrypted(Path(report['encrypted']), other, report['plainSha256'])

    def test_cancel_keeps_failure_report_without_touching_sources(self):
        records = self.library.scan().records
        before = fingerprints(self.home)
        key = self.base / 'key.asmkey';save_recovery_key(key)
        cancel = threading.Event();cancel.set()
        with self.assertRaises(Cancelled) as e:
            make_backup(self.library, records, self.base / 'backups', key, cancel=cancel)
        self.assertEqual(e.exception.report['status'], 'cancelled')
        self.assertEqual(before, fingerprints(self.home))

    def test_cache_and_backup_must_stay_outside_sources(self):
        with self.assertRaises(ValueError):LocalLibrary(self.home, self.home / '.dsh/cache/private')
        records=self.library.scan().records
        key=self.base/'key.asmkey';save_recovery_key(key)
        with self.assertRaises(ValueError):make_backup(self.library, records, self.home/'.dsh/backup', key)
        self.assertFalse((self.home/'.dsh/backup').exists())

    def test_unknown_deepseek_generation_is_preserved_not_interpreted(self):
        import zstandard
        path=self.files['deepseek'].with_name('session.v5.jsonl.zstd')
        path.write_bytes(zstandard.ZstdCompressor().compress((json.dumps({'type':'session','version':5,'id':'harness-session','createdAt':1000,'cwd':str(self.files['project'])})+'\n').encode()))
        record=next(r for r in self.library.scan().records if r.provider=='deepseek')
        self.assertEqual(record.metadata['format_version'],5)
        self.assertFalse(self.library.page(record).messages)
        self.assertTrue(self.library.page(record).warnings)
        self.assertEqual(len(record.files),2)

    def test_arbitrary_tool_schema_type_is_not_treated_as_image(self):
        record=next(r for r in self.library.scan().records if r.provider=='claude')
        attachments=[]
        self.library._event_attachments(record, {'tool_schema':{'type':{'enum':['image']},'path':'/not-an-attachment'}}, attachments)
        self.assertFalse(attachments)

    def test_corrupt_package_payload_fails_verification(self):
        records=self.library.scan().records
        key=self.base/'key.asmkey';save_recovery_key(key)
        report=make_backup(self.library, records, self.base/'backups', key)
        damaged=self.base/'damaged.zip'
        with zipfile.ZipFile(report['portable']) as src,zipfile.ZipFile(damaged,'w') as out:
            changed=False
            for name in src.namelist():
                data=src.read(name)
                if name.startswith('objects/') and not changed:data+=b'broken';changed=True
                out.writestr(name,data)
        with self.assertRaises(ValueError):verify_package(damaged)

    def test_encrypted_preview_and_tamper_rejection(self):
        records=self.library.scan().records
        key=self.base/'key.asmkey';save_recovery_key(key)
        report=make_backup(self.library, records, self.base/'backups', key)
        decoded=decrypt_for_preview(Path(report['encrypted']),key,self.base/'decrypted')
        self.assertEqual(hash_file(decoded),report['plainSha256'])
        self.assertEqual(len(PackageLibrary(decoded,self.base/'view-cache').scan().records),3)
        broken=self.base/'tampered.asmb';data=bytearray(Path(report['encrypted']).read_bytes());data[-30]^=1;broken.write_bytes(data)
        with self.assertRaises(Exception):decrypt_for_preview(broken,key,self.base/'broken-cache')
        self.assertFalse(list((self.base/'broken-cache').glob('*.zip')))

    def test_legacy_aes_backup_preview_without_external_openssl(self):
        import hmac,secrets
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        from cryptography.hazmat.primitives import padding
        key=self.base/'old.recovery.key';key.write_bytes((secrets.token_hex(48)+'\n').encode())
        salt=secrets.token_bytes(8);material=hashlib.pbkdf2_hmac('sha256',key.read_bytes().splitlines()[0],salt,200000,48)
        plain=b'legacy-original-bytes-for-preview'
        pad=padding.PKCS7(128).padder();padded=pad.update(plain)+pad.finalize();enc=Cipher(algorithms.AES(material[:32]),modes.CBC(material[32:])).encryptor();cipher=b'Salted__'+salt+enc.update(padded)+enc.finalize()
        archive=self.base/'old.zip.enc';archive.write_bytes(cipher)
        auth=self.base/'backup-auth.json';auth.write_text(json.dumps({'hmac':hmac.new(hashlib.sha256(b'backup-auth-v1\0'+key.read_bytes()).digest(),cipher,'sha256').hexdigest(),'ciphertextSha256':hashlib.sha256(cipher).hexdigest()}))
        decoded=decrypt_for_preview(archive,key,self.base/'old-cache',auth_path=auth)
        self.assertEqual(decoded.read_bytes(),plain)

    def test_finished_archive_can_resume_after_encryption_failure(self):
        from unittest.mock import patch
        records=self.library.scan().records
        before=fingerprints(self.home)
        key=self.base/'key.asmkey';save_recovery_key(key)
        with patch('ai_session_migration.backup.encrypt_archive',side_effect=RuntimeError('synthetic encryption failure')):
            with self.assertRaises(RuntimeError) as failure:
                make_backup(self.library,records,self.base/'backups',key)
        report=failure.exception.report
        self.assertTrue(report['archiveReady'])
        self.assertTrue(Path(report['pendingArchive']).is_file())
        recovered=resume_backup(report,key,protected_roots=[self.home/'.claude',self.home/'.codex',self.home/'.dsh'])
        self.assertEqual(recovered['status'],'complete')
        self.assertTrue(recovered['encryptedReadbackVerified'])
        self.assertTrue(verify_package(Path(recovered['portable']))['verified'])
        self.assertEqual(before,fingerprints(self.home))

    def test_optional_project_work_files_exclude_git_and_separate_credentials(self):
        project=self.files['project'];(project/'note.md').write_text('original-work-file',encoding='utf-8');(project/'.env').write_text('TOKEN=synthetic-secret',encoding='utf-8');(project/'.git').mkdir();(project/'.git/HEAD').write_text('private-git-metadata',encoding='utf-8')
        records=self.library.scan().records;key=self.base/'key.asmkey';save_recovery_key(key)
        report=make_backup(self.library,records,self.base/'backups',key,include_projects=True)
        with zipfile.ZipFile(report['portable']) as package:
            entries=json.loads(package.read('manifest.json'))['entries']
            work=[e for e in entries if e.get('role')=='project-work-file']
            self.assertEqual([Path(e['originalPath']).name for e in work],['note.md'])
            self.assertEqual(package.read(work[0]['object']),b'original-work-file')

    def test_unsafe_legacy_source_label_is_rejected_before_materialization(self):
        path=self.base/'unsafe.zip'
        with zipfile.ZipFile(path,'w') as z:z.writestr('manifest.json',json.dumps({'schema':'conversation-migration-1','roots':[{'label':'../../.codex','kind':'codex'}]}))
        before=fingerprints(self.home)
        with self.assertRaises(ValueError):PackageLibrary(path,self.base/'view')
        self.assertEqual(before,fingerprints(self.home))


if __name__=='__main__':unittest.main()

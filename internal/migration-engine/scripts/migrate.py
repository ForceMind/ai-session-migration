#!/usr/bin/env python3
"""Source-preserving local backup. No import, restore, cleanup or thinking conversion."""
import argparse
import collections
from contextlib import ExitStack
import io
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

VERSION = '0.5.0'
SCHEMA = 'conversation-migration-1'
CHUNK = 1024 * 1024
RESERVE = 2 * 1024 ** 3
TRANSIENT = {'SingletonSocket', 'SingletonLock', 'SingletonCookie'}
# These are not copied as runnable configuration. Transcripts themselves are unmodified.
PORTABLE_CODEX = {'sessions', 'archived_sessions', 'attachments', 'generated_images',
                  'memories', 'memories_v2', 'visualizations', 'pets', '.chatgpt-projects'}
PORTABLE_CLAUDE = {'projects', 'file-history', 'uploads', 'plans', 'skills', 'agents', 'rules'}
PROJECT_REBUILDABLE = {'node_modules', '.venv', 'venv', '__pycache__', '.next', '.nuxt', '.turbo', '.pytest_cache'}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def dump(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def digest(value):
    return hashlib.sha256(dump(value)).hexdigest()


def hash_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def new_file(path, data):
    path = Path(path)
    with open(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def within(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def regular_path(path):
    """Refuse symlink components for writable paths."""
    path = Path(os.path.abspath(path))
    for p in [path, *path.parents]:
        if p.is_symlink():
            raise ValueError('Symlink component in destination: ' + str(p))
    return path


def process_state():
    if sys.platform != 'darwin':
        return {'inspection': 'unsupported', 'running': []}
    try:
        r = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True, text=True, timeout=10)
        if r.returncode:
            return {'inspection': 'unavailable', 'running': []}
        patterns = [r'/Claude\.app/', r'/ChatGPT\.app/', r'/Codex\.app/',
                    r'/codex-cli/bin/codex$', r'/claude/versions/', r'(^|/)(claude|codex)$']
        return {'inspection': 'available', 'running': sorted(set(
            line.strip() for line in r.stdout.splitlines() if any(re.search(p, line) for p in patterns)))}
    except (OSError, subprocess.TimeoutExpired):
        return {'inspection': 'unavailable', 'running': []}


def discover(home):
    h = Path(home).resolve()
    candidates = [('codex-home', h / '.codex', 'codex'),
                  ('claude-code', h / '.claude', 'claude-code'),
                  ('claude-config', h / '.claude.json', 'config'),
                  ('codex-desktop', h / 'Library/Application Support/Codex', 'desktop')]
    app = h / 'Library/Application Support'
    candidates += [(('claude-desktop' if p.name == 'Claude' else 'claude-desktop-' + p.name), p, 'claude-desktop')
                   for p in sorted(app.glob('Claude*')) if p.is_dir() and (p.name == 'Claude' or p.name.startswith('Claude-'))]
    for variable, label, kind in [('CODEX_HOME', 'codex-custom', 'codex'), ('CLAUDE_CONFIG_DIR', 'claude-custom', 'claude-code')]:
        if os.environ.get(variable):
            candidates.append((label, Path(os.environ[variable]).expanduser(), kind))
    roots, seen = [], set()
    for label, p, kind in candidates:
        if not p.exists():
            continue
        p = p.resolve()
        key = (p.stat().st_dev, p.stat().st_ino)
        if key not in seen:
            seen.add(key)
            roots.append({'label': label, 'path': str(p), 'kind': kind})
    return roots


def referenced_workspaces(roots, home):
    paths = set()
    for root in roots:
        p = Path(root['path'])
        if root['kind'] == 'codex':
            for db in p.glob('state_*.sqlite'):
                c = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
                try:
                    paths.update(x[0] for x in c.execute('SELECT DISTINCT cwd FROM threads'))
                    try:
                        paths.update(x[0] for x in c.execute('SELECT path FROM project_roots'))
                    except sqlite3.OperationalError:
                        pass
                finally:
                    c.close()
        if root['kind'] == 'claude-desktop':
            for f in (p / 'claude-code-sessions').rglob('local_*.json'):
                try:
                    d = json.loads(f.read_text())
                    paths.update(v for k, v in d.items() if k in {'cwd', 'originCwd', 'worktreePath'} and isinstance(v, str))
                except (OSError, ValueError):
                    pass
    records = []
    seen = set()
    for value in sorted(paths, key=len):
        p = Path(value).resolve()
        record = {'original': value, 'resolved': str(p), 'exists': p.is_dir()}
        if not p.is_dir():
            record['status'] = 'missing-at-source'
        elif p == Path(home).resolve() or len(p.parts) < 4:
            record['status'] = 'broad-root-not-auto-included'
        elif any(within(p, r['path']) for r in roots):
            record['status'] = 'covered-by-application-root'
        else:
            key = (p.stat().st_dev, p.stat().st_ino)
            if key in seen:
                record['status'] = 'alias'
            elif any(within(p, x['resolved']) for x in records if x['status'] == 'candidate'):
                record['status'] = 'covered-by-parent'
            else:
                seen.add(key)
                record['status'] = 'candidate'
        records.append(record)
    return records


def portable(root, relative):
    parts = PurePosixPath(relative).parts
    top = parts[0] if parts else ''
    name = parts[-1] if parts else ''
    if root['kind'] == 'codex':
        return top in PORTABLE_CODEX or bool(re.fullmatch(r'(state|thread_history|memories(?:_v2)?|goals)_\d+\.sqlite', relative)) or relative in {'session_index.jsonl', 'AGENTS.md'}
    if root['kind'] == 'claude-code':
        return top in PORTABLE_CLAUDE or relative == 'CLAUDE.md'
    if root['kind'] == 'claude-desktop':
        # Index metadata can contain permissions/remote configuration. Preserve it only in encrypted backup.
        return top in {'scratch-workspaces', 'side-session-notes', 'git-shadow'}
    if root['kind'] == 'desktop':
        return top in {'artifact-sessions'}
    if root['kind'] == 'workspace':
        return not any(x == '.git' or x.startswith('.env') or x.lower().endswith(('.pem', '.key', '.p12', '.pfx')) for x in parts)
    return False


def inventory(roots, exclusions=()):
    entries, omitted = [], []
    exclusion_prefixes = [str(Path(x).resolve()).rstrip(os.sep) for x in exclusions]
    for root in roots:
        base = Path(root['path'])
        def visit(p, relative):
            if any(str(p) == ex or str(p).startswith(ex + os.sep) for ex in exclusion_prefixes):
                omitted.append({'root': root['label'], 'path': relative, 'reason': 'explicit-output-exclusion'})
                return
            s = p.lstat()
            row = {'root': root['label'], 'path': relative, 'mode': stat.S_IMODE(s.st_mode), 'mtime_ns': s.st_mtime_ns}
            if stat.S_ISLNK(s.st_mode):
                row.update(type='symlink', target=os.readlink(p))
            elif stat.S_ISDIR(s.st_mode):
                row['type'] = 'directory'
                entries.append(row)
                for child in sorted(p.iterdir()):
                    if root['kind'] == 'workspace' and child.name in PROJECT_REBUILDABLE:
                        omitted.append({'root': root['label'], 'path': str(child.relative_to(base)), 'reason': 'rebuildable-project-dependency'})
                    else:
                        visit(child, child.relative_to(base).as_posix())
                return
            elif stat.S_ISREG(s.st_mode):
                if p.name in TRANSIENT or (p.name.endswith('-shm') and p.with_name(p.name[:-4]).is_file()):
                    row['type'] = 'runtime-lock'
                else:
                    row.update(type='file', size=s.st_size, inode=s.st_ino, device=s.st_dev)
            else:
                row['type'] = 'runtime-special'
            entries.append(row)
        visit(base, '.' if base.is_dir() else base.name)
    return entries, omitted


def plan(home, include_projects=False, exclusions=()):
    roots = discover(home)
    refs = referenced_workspaces(roots, home)
    if include_projects:
        for r in refs:
            if r['status'] == 'candidate':
                roots.append({'label': 'workspace-' + str(len(roots)).zfill(3), 'path': r['resolved'], 'kind': 'workspace'})
    entries, omitted = inventory(roots, exclusions)
    result = {'schema': SCHEMA, 'version': VERSION, 'createdAt': now(), 'home': str(Path(home).resolve()),
              'roots': roots, 'workspaces': refs, 'includeProjects': include_projects,
              'exclusions': [str(Path(x).resolve()) for x in exclusions], 'entries': entries, 'omitted': omitted,
              'sourceProcessState': process_state(), 'keychainExported': False, 'thinkingTransform': False,
              'windowsImported': False, 'modelVerified': False}
    result['digest'] = digest(result)
    return result


def load_plan(path):
    p = json.loads(Path(path).read_text())
    saved = p.pop('digest')
    if p.get('schema') != SCHEMA or p.get('version') != VERSION or digest(p) != saved:
        raise ValueError('Modified or incompatible plan')
    p['digest'] = saved
    labels = set()
    for root in p['roots']:
        if not re.fullmatch(r'[A-Za-z0-9-]+', root['label']) or root['label'] in labels:
            raise ValueError('Invalid root label')
        labels.add(root['label'])
    for e in p['entries']:
        if e['root'] not in labels or PurePosixPath(e['path']).is_absolute() or '..' in PurePosixPath(e['path']).parts:
            raise ValueError('Invalid source entry')
    return p


def sqlite_file(path):
    try:
        with open(path, 'rb') as f:
            return f.read(16) == b'SQLite format 3\x00'
    except OSError:
        return False


def sqlite_snapshot(source, target, timeout=180, prefer_copy=False):
    deadline = time.monotonic() + timeout
    src = dst = None
    copied = None
    pair_temp = None
    method = 'sqlite-online-backup'
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite snapshot deadline exceeded')
    try:
        if prefer_copy:
            # Chromium can hold an exclusive connection. Freeze a stable main/
            # WAL/journal set, then perform recovery and backup on our copy.
            pair_temp = tempfile.TemporaryDirectory(prefix='.sqlite-pair-', dir=target.parent)
            private = Path(pair_temp.name) / 'source.sqlite'
            def state():
                result = {}
                for suffix in ['', '-wal', '-journal']:
                    f = Path(str(source) + suffix)
                    if f.exists():
                        s = f.stat()
                        result[suffix] = {'size': s.st_size, 'mtime_ns': s.st_mtime_ns, 'inode': s.st_ino}
                return result
            for attempt in range(5):
                before_pair = state()
                for suffix, details in before_pair.items():
                    stable_copy(Path(str(source) + suffix), Path(str(private) + suffix), details, True)
                if state() == before_pair:
                    break
                for f in Path(pair_temp.name).iterdir():
                    f.unlink()
            else:
                raise ValueError('Cannot freeze an unchanged database file set')
            src = sqlite3.connect(str(private), timeout=5)
            method = 'sqlite-stable-file-set-backup'
        elif not Path(str(source) + '-wal').exists() and not Path(str(source) + '-journal').exists():
            # Do not create missing WAL/SHM sidecars in a checkpointed source.
            # Immutable applies only to a stable private copy, never a live WAL.
            copied = target.with_name(target.name + '.checkpointed-source')
            info = source.stat()
            stable_copy(source, copied, {'size': info.st_size, 'mtime_ns': info.st_mtime_ns}, True)
            if Path(str(source) + '-wal').exists() or Path(str(source) + '-journal').exists():
                raise ValueError('Database gained a journal during snapshot; retry: ' + str(source))
            src = sqlite3.connect(copied.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=5)
            method = 'sqlite-checkpointed-file-snapshot'
        else:
            src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)
        dst = sqlite3.connect(str(target))
        src.execute('PRAGMA query_only=ON')
        src.backup(dst, pages=256, progress=progress, sleep=0.01)
        result = dst.execute('PRAGMA quick_check').fetchall()
        if result != [('ok',)]:
            raise ValueError('SQLite snapshot integrity check failed')
        return method
    except Exception as error:
        raise RuntimeError('SQLite snapshot failed for ' + str(source) + ': ' + str(error)) from error
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        if copied is not None and copied.exists():
            copied.unlink()
        if pair_temp is not None:
            pair_temp.cleanup()


def stable_copy(source, target, expected, live):
    """Never follow a source symlink; stage bytes before placing them into either archive."""
    for attempt in range(3):
        fd = os.open(str(source), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Source is no longer a regular file')
            if not live and any(getattr(before, 'st_' + k) != expected[k] for k in ['size', 'mtime_ns']):
                raise ValueError('Plan expired; source changed')
            with os.fdopen(fd, 'rb', closefd=False) as src, open(target, 'wb') as dst:
                remaining = before.st_size
                while remaining:
                    block = src.read(min(CHUNK, remaining))
                    if not block:
                        break
                    dst.write(block)
                    remaining -= len(block)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if remaining == 0 and (before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino):
            return {'changedSincePlan': before.st_size != expected['size'] or before.st_mtime_ns != expected['mtime_ns']}
        if not live:
            break
    raise ValueError('Source changed during capture; no complete archive was produced: ' + str(source))


def add_file(z, path, name):
    info = zipfile.ZipInfo(name, time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info._compresslevel = 3
    info.external_attr = 0o100600 << 16
    with open(path, 'rb') as src, z.open(info, 'w', force_zip64=True) as dst:
        shutil.copyfileobj(src, dst, CHUNK)


def archive_source(source, archives, name, expected, live):
    """Read a regular source once; send identical bytes to both archives and hash them."""
    fd = os.open(str(source), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Source is no longer a regular file')
        if not live and (before.st_size != expected['size'] or before.st_mtime_ns != expected['mtime_ns']):
            raise ValueError('Plan expired; source changed')
        buffered = before.st_size <= 8 * CHUNK or (source.suffix in {'.jsonl', '.json', '.log'} and before.st_size <= 256 * CHUNK)
        snapshot = None
        if buffered:
            for attempt in range(5):
                os.lseek(fd, 0, os.SEEK_SET)
                before = os.fstat(fd)
                if before.st_size > 256 * CHUNK:
                    raise ValueError('Growing source exceeds memory snapshot bound: ' + str(source))
                blocks, remaining = [], before.st_size
                while remaining:
                    block = os.read(fd, min(CHUNK, remaining))
                    if not block:
                        break
                    blocks.append(block)
                    remaining -= len(block)
                after_read = os.fstat(fd)
                if not remaining and (before.st_size, before.st_mtime_ns) == (after_read.st_size, after_read.st_mtime_ns):
                    snapshot = b''.join(blocks)
                    del blocks
                    break
                if not live:
                    break
            if snapshot is None:
                raise ValueError('Cannot obtain stable bytes from active source: ' + str(source))
        remaining = before.st_size
        checksum = hashlib.sha256()
        with ExitStack() as stack:
            src = stack.enter_context(io.BytesIO(snapshot) if buffered else os.fdopen(fd, 'rb', closefd=False))
            writers = []
            for archive in archives:
                info = zipfile.ZipInfo(name, time.localtime()[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info._compresslevel = 3
                info.external_attr = 0o100600 << 16
                writers.append(stack.enter_context(archive.open(info, 'w', force_zip64=True)))
            while remaining:
                block = src.read(min(CHUNK, remaining))
                if not block:
                    break
                checksum.update(block)
                for writer in writers:
                    writer.write(block)
                remaining -= len(block)
        after = os.fstat(fd)
        if remaining or (not buffered and (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino)):
            raise ValueError('Source changed during archive streaming; partial archive is not complete: ' + str(source))
        return {'sha256': checksum.hexdigest(), 'capturedSize': before.st_size,
                'capturedMtimeNs': before.st_mtime_ns, 'capturedMode': stat.S_IMODE(before.st_mode),
                'changedSincePlan': before.st_size != expected['size'] or before.st_mtime_ns != expected['mtime_ns']}
    finally:
        os.close(fd)


def verify_archive(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive member')
        for name in names:
            p = PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
                raise ValueError('Unsafe archive member')
        m = json.loads(z.read('manifest.json'))
        if m.get('schema') != SCHEMA:
            raise ValueError('Unsupported manifest')
        expected = {'manifest.json'}
        support = m.get('supportFiles', {})
        if set(support) != {'migrate.py', 'VERIFY-WINDOWS.cmd', 'README-WINDOWS.txt', 'session-catalog.json'}:
            raise ValueError('Missing portable verification/catalog files')
        for name, checksum in support.items():
            if hashlib.sha256(z.read(name)).hexdigest() != checksum:
                raise ValueError('Support file checksum mismatch')
            expected.add(name)
        for e in m['entries']:
            if 'object' not in e:
                continue
            name = e['object']
            if not re.fullmatch(r'objects/\d{8}', name) or name in expected:
                raise ValueError('Invalid or repeated payload object')
            expected.add(name)
            h, size = hashlib.sha256(), 0
            with z.open(name) as f:
                for block in iter(lambda: f.read(CHUNK), b''):
                    h.update(block)
                    size += len(block)
            if h.hexdigest() != e['sha256'] or size != e['capturedSize']:
                raise ValueError('Payload checksum mismatch: ' + name)
        if set(names) != expected:
            raise ValueError('Unmanifested or missing archive payload')
    return {'verified': True, 'kind': m['kind'], 'files': sum('object' in e for e in m['entries']),
            'uncompressedBytes': sum(e.get('capturedSize', 0) for e in m['entries']),
            'consistency': m['consistency'], 'sourceDataModifiedByTool': False,
            'windowsImported': False, 'modelVerified': False}


WINDOWS_README = '''Claude / Codex portable conversation data — version 0.5.0
This is an offline data package, NOT an automatic application import.
No thinking blocks were removed or rewritten. Source files remain on the Mac.
Transcripts can contain private information and secrets from past tool output.
Known standalone login credential files and runnable app configuration are not
included in this portable package. This is not a guarantee that history is secret-free.

Keep windows-portable.zip intact. Install Python 3.9+ if necessary, then run:
  py -3 migrate.py verify --archive windows-portable.zip
  py -3 migrate.py inspect --archive windows-portable.zip
Alternatively use: python migrate.py ...
VERIFY-WINDOWS.cmd only verifies the archive; it never imports or changes apps.
Payloads use numbered objects to preserve Mac names that Windows cannot extract.
manifest.json maps every object to its original root/path/mode/mtime/hash.
Do not copy numbered objects into .codex or .claude. Path mapping, duplicate
resolution, account/provider compatibility and imports are a separate next step.

Mac recovery backup: mac-preservation.zip.enc + backup-auth.json + a separately
stored recovery key. It contains full application data, including credentials.
Do not put that recovery key in this portable package. Reauthenticate on Windows.
No Windows import, browser interaction or real model continuation was tested.
An online snapshot is not an application-wide atomic/cold snapshot. The manifest
records its time range, changed/new/disappeared files and database snapshots.
For final backup, quit Claude and Codex and run capture without --live from a
normal terminal using a fresh plan. Keychain contents are never exported.
'''


def capture(p, output, key_file, live=False, resume_from=None):
    output, key_file = regular_path(output), regular_path(key_file)
    for r in p['roots']:
        for candidate in [output, key_file]:
            if within(candidate, r['path']) and not any(within(candidate, x) for x in p['exclusions']):
                raise ValueError('Output/key must be outside sources or explicitly excluded in the plan')
    state = process_state()
    if not live and (state['inspection'] != 'available' or state['running']):
        raise ValueError('Cold capture requires Claude/Codex stopped and process inspection available; use --live only for an explicitly labelled online snapshot')
    if output.exists() or key_file.exists():
        raise FileExistsError('Output or recovery key exists; never overwrite')
    if within(key_file, output):
        raise ValueError('Recovery key must be stored separately from backup/package')
    resume_index = 0
    resume_dir = None
    resume_journal = None
    if resume_from:
        if not live:
            raise ValueError('A resumed online snapshot cannot be labelled a cold backup')
        resume_dir = regular_path(resume_from)
        resume_journal = json.loads((resume_dir / 'journal.json').read_text())
        if resume_journal.get('status') != 'failed' or resume_journal.get('planDigest') != p['digest'] or not resume_journal.get('lastAttempt'):
            raise ValueError('Resume requires a matching failed run with an exact lastAttempt')
        attempt = resume_journal['lastAttempt']
        resume_index = next(i for i, e in enumerate(p['entries']) if e['root'] == attempt['root'] and e['path'] == attempt['path'])
        for filename in ['mac-preservation.zip.part', 'windows-portable.zip.part']:
            with zipfile.ZipFile(resume_dir / filename) as old:
                names = old.namelist()
                if len(names) != len(set(names)) or any(not re.fullmatch(r'objects/\d{8}', n) or int(n[8:]) >= resume_index for n in names):
                    raise ValueError('Partial run contains an ambiguous/incomplete object; not resumable')
    source_bytes = sum(e.get('size', 0) for e in p['entries'])
    output.parent.mkdir(parents=True, exist_ok=True)
    remaining_bytes = sum(e.get('size', 0) for e in p['entries'][resume_index:])
    copy_bytes = sum((resume_dir / n).stat().st_size for n in ['mac-preservation.zip.part', 'windows-portable.zip.part']) if resume_dir else 0
    if shutil.disk_usage(output.parent).free < remaining_bytes * 2 + copy_bytes + RESERVE:
        raise ValueError('Insufficient destination space for both archives and verification')
    output.mkdir(mode=0o700)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    new_file(key_file, (secrets.token_hex(48) + '\n').encode())
    journal = {'schema': SCHEMA, 'status': 'capturing', 'startedAt': now(), 'planDigest': p['digest'],
               'sourceDataModifiedByTool': False, 'thinkingTransform': False, 'windowsImported': False, 'modelVerified': False}
    def save_journal():
        temp = output / 'journal.json.tmp'
        temp.write_bytes(dump(journal))
        os.chmod(temp, 0o600)
        os.replace(temp, output / 'journal.json')
    save_journal()
    full_path, win_path = output / 'mac-preservation.zip.part', output / 'windows-portable.zip.part'
    try:
        if resume_dir:
            print(json.dumps({'phase': 'copy-resumable-archives', 'resumeAt': resume_index}), flush=True)
            for filename in ['mac-preservation.zip.part', 'windows-portable.zip.part']:
                shutil.copyfile(resume_dir / filename, output / filename)
            journal['resumedFrom'] = str(resume_dir)
            journal['startedAt'] = resume_journal['startedAt']
            save_journal()
        with tempfile.TemporaryDirectory(prefix='.capture-stage-', dir=output) as scratch:
            scratch = Path(scratch)
            full_entries, win_entries, differences, catalog = [], [], [], []
            roots = {r['label']: r for r in p['roots']}
            # WAL is represented by the consistent SQLite backup, not replayed a second time.
            databases = set()
            for e in p['entries']:
                sqlite_name = Path(e['path']).name
                likely_db = sqlite_name.endswith(('.sqlite', '.sqlite3', '.db')) or sqlite_name in {'Cookies', 'History', 'Web Data', 'Login Data', 'DIPS', 'SharedStorage', 'Trust Tokens', 'Favicons', 'Top Sites', 'Shortcuts', 'Network Action Predictor'}
                if e['type'] == 'file' and likely_db:
                    src = Path(roots[e['root']]['path'])
                    if src.is_dir():
                        src = src / e['path']
                    if sqlite_file(src):
                        databases.add((e['root'], e['path']))
            with zipfile.ZipFile(full_path, 'a' if resume_dir else 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as full, \
                    zipfile.ZipFile(win_path, 'a' if resume_dir else 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as win:
                if resume_dir:
                    available = set(full.namelist())
                    win_available = set(win.namelist())
                    for i, e in enumerate(p['entries'][:resume_index]):
                        row = dict(e)
                        name = 'objects/' + str(i).zfill(8)
                        if name in available:
                            checksum = hashlib.sha256()
                            with full.open(name) as f:
                                for block in iter(lambda: f.read(CHUNK), b''):
                                    checksum.update(block)
                            row.update(object=name, sha256=checksum.hexdigest(), capturedSize=full.getinfo(name).file_size,
                                       capture='resumed-verified-partial', capturedTimeRange=[resume_journal['startedAt'], resume_journal['failedAt']])
                            if portable(roots[e['root']], e['path']) and name not in win_available:
                                raise ValueError('Resume portable archive is missing a captured object')
                            if roots[e['root']]['kind'] == 'claude-desktop' and e['path'].startswith('claude-code-sessions/') and Path(e['path']).name.startswith('local_') and e['path'].endswith('.json'):
                                try:
                                    data = json.loads(full.read(name))
                                    keys = {'sessionId', 'cliSessionId', 'cwd', 'originCwd', 'worktreePath', 'createdAt', 'lastActivityAt', 'title', 'isArchived', 'model'}
                                    catalog.append({'root': e['root'], 'sourceIndex': e['path'], **{k: v for k, v in data.items() if k in keys and isinstance(v, (str, int, float, bool, type(None)))}})
                                except (ValueError, UnicodeError):
                                    catalog.append({'root': e['root'], 'sourceIndex': e['path'], 'status': 'unparsed-original-preserved-in-encrypted-backup'})
                        elif e['type'] == 'file':
                            row['capture'] = 'incorporated-in-sqlite-snapshot' if e['path'].endswith('-wal') else 'not-present-in-resumed-partial'
                        full_entries.append(row)
                        if portable(roots[e['root']], e['path']):
                            win_entries.append(row)
                    journal['resumedEntriesVerified'] = resume_index
                    save_journal()
                for i, e in enumerate(p['entries']):
                    if i < resume_index:
                        continue
                    row = dict(e)
                    root = roots[e['root']]
                    if e['type'] in {'runtime-lock', 'runtime-special'}:
                        row['capture'] = 'runtime-metadata-only'
                    elif e['type'] == 'file':
                        if e['path'].endswith('-wal') and (e['root'], e['path'][:-4]) in databases:
                            row['capture'] = 'incorporated-in-sqlite-snapshot'
                        else:
                            src = Path(root['path'])
                            if src.is_dir():
                                src = src / e['path']
                            stage = scratch / 'current-object'
                            try:
                                row['object'] = 'objects/' + str(i).zfill(8)
                                if (e['root'], e['path']) in databases:
                                    row['capture'] = sqlite_snapshot(src, stage, prefer_copy=root['kind'] in {'desktop', 'claude-desktop', 'workspace'})
                                    row.update(sha256=hash_file(stage), capturedSize=stage.stat().st_size)
                                    add_file(full, stage, row['object'])
                                    if portable(root, e['path']):
                                        add_file(win, stage, row['object'])
                                else:
                                    archives = [full, win] if portable(root, e['path']) else [full]
                                    detail = archive_source(src, archives, row['object'], e, live)
                                    row.update(detail, capture='byte-preserving-copy')
                                if root['kind'] == 'claude-desktop' and e['path'].startswith('claude-code-sessions/') and Path(e['path']).name.startswith('local_') and e['path'].endswith('.json'):
                                    try:
                                        data = json.loads(full.read(row['object']))
                                        safe_keys = {'sessionId', 'cliSessionId', 'cwd', 'originCwd', 'worktreePath', 'createdAt', 'lastActivityAt', 'title', 'isArchived', 'model'}
                                        catalog.append({'root': root['label'], 'sourceIndex': e['path'], **{k: v for k, v in data.items() if k in safe_keys and isinstance(v, (str, int, float, bool, type(None)))}})
                                    except (ValueError, UnicodeError):
                                        catalog.append({'root': root['label'], 'sourceIndex': e['path'], 'status': 'unparsed-original-preserved-in-encrypted-backup'})
                            except FileNotFoundError:
                                if not live:
                                    raise
                                row['capture'] = 'disappeared-during-online-capture'
                                row.pop('object', None)
                                differences.append({'root': e['root'], 'path': e['path'], 'change': 'disappeared'})
                            finally:
                                if stage.exists():
                                    stage.unlink()
                    full_entries.append(row)
                    if portable(root, e['path']):
                        win_entries.append(row)
                    if i % 2500 == 0:
                        print(json.dumps({'phase': 'capture', 'entries': i, 'total': len(p['entries'])}), flush=True)
                fresh, _ = inventory(p['roots'], p['exclusions'])
                before = {(e['root'], e['path']): e for e in p['entries']}
                after = {(e['root'], e['path']): e for e in fresh}
                for k in before.keys() | after.keys():
                    a, b = before.get(k), after.get(k)
                    if a != b:
                        differences.append({'root': k[0], 'path': k[1], 'change': 'new' if a is None else 'removed' if b is None else 'changed'})
                if differences and not live:
                    raise ValueError('Source tree changed during cold capture')
                consistency = {'mode': 'online-per-file-and-per-database' if live else 'applications-stopped',
                               'applicationWideAtomic': False, 'startedAt': journal['startedAt'], 'finishedAt': now(),
                               'sourceDifferences': differences, 'processState': state}
                support = {'migrate.py': Path(__file__).read_bytes(), 'README-WINDOWS.txt': WINDOWS_README.encode(),
                           'session-catalog.json': dump({'claudeDesktop': catalog, 'rawIndexLocation': 'encrypted-mac-backup-only', 'permissionsAndRemoteStateImported': False}),
                           'VERIFY-WINDOWS.cmd': b'@echo off\r\ncd /d "%~dp0"\r\npy -3 -B migrate.py verify --archive windows-portable.zip\r\nif errorlevel 1 exit /b 1\r\npause\r\n'}
                for z, kind, entries in [(full, 'mac-preservation', full_entries), (win, 'windows-portable', win_entries)]:
                    manifest = {'schema': SCHEMA, 'version': VERSION, 'kind': kind, 'roots': p['roots'], 'entries': entries,
                                'workspaces': p['workspaces'], 'projectsIncluded': p['includeProjects'], 'omitted': p['omitted'],
                                'consistency': consistency, 'sourceDataModifiedByTool': False, 'thinkingTransform': False,
                                'keychainExported': False, 'windowsImported': False, 'modelVerified': False,
                                'supportFiles': {n: hashlib.sha256(b).hexdigest() for n, b in support.items()}}
                    z.writestr('manifest.json', dump(manifest))
                    for n, b in support.items():
                        z.writestr(n, b)
            print(json.dumps({'phase': 'verify-full-archive'}), flush=True)
            full_check = verify_archive(full_path)
            win_check = verify_archive(win_path)
            win_final = output / 'windows-portable.zip'
            os.rename(win_path, win_final)
            enc_path = output / 'mac-preservation.zip.enc'
            if shutil.disk_usage(output).free < full_path.stat().st_size + RESERVE:
                raise ValueError('Insufficient space for encrypted backup; verified archives retained')
            with open(os.open(str(enc_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as enc:
                r = subprocess.run(['openssl', 'enc', '-aes-256-cbc', '-salt', '-pbkdf2', '-iter', '200000',
                                    '-pass', 'file:' + str(key_file), '-in', str(full_path)], stdout=enc, stderr=subprocess.PIPE)
            if r.returncode:
                raise ValueError('Backup encryption failed')
            secret = key_file.read_bytes()
            mac = hmac.new(hashlib.sha256(b'backup-auth-v1\x00' + secret).digest(), digestmod='sha256')
            with open(enc_path, 'rb') as f:
                for block in iter(lambda: f.read(CHUNK), b''):
                    mac.update(block)
            new_file(output / 'backup-auth.json', dump({'algorithm': 'AES-256-CBC/PBKDF2-SHA256-200000/HMAC-SHA256',
                                                       'hmac': mac.hexdigest(), 'ciphertextSha256': hash_file(enc_path)}))
            # Actual decrypt/read-back, not just a successful encryption exit code.
            decrypt = subprocess.Popen(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000',
                                        '-pass', 'file:' + str(key_file), '-in', str(enc_path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            back_hash = hashlib.sha256()
            for block in iter(lambda: decrypt.stdout.read(CHUNK), b''):
                back_hash.update(block)
            decrypt.stdout.close()
            decrypt.stderr.read()
            decrypt.stderr.close()
            if decrypt.wait() or back_hash.hexdigest() != hash_file(full_path):
                raise ValueError('Encrypted backup read-back mismatch')
            full_path.unlink()  # only our verified, temporary plaintext archive
            journal.update(status='complete', finishedAt=now(), fullBackup=full_check, portable=win_check,
                           encryptedBackupReadbackVerified=True, sourceDifferenceCount=len(differences),
                           needsColdFinalBackup=live, backupKeyStoredSeparately=True)
            new_file(output / 'README-WINDOWS.txt', WINDOWS_README.encode())
            new_file(output / 'migrate.py', Path(__file__).read_bytes())
            with zipfile.ZipFile(win_final) as z:
                new_file(output / 'VERIFY-WINDOWS.cmd', z.read('VERIFY-WINDOWS.cmd'))
            sums = ''.join(hash_file(output / n) + '  ' + n + '\n' for n in ['windows-portable.zip', 'mac-preservation.zip.enc', 'backup-auth.json', 'migrate.py', 'README-WINDOWS.txt', 'VERIFY-WINDOWS.cmd'])
            new_file(output / 'SHA256SUMS', sums.encode())
            save_journal()
            return journal
    except BaseException as e:
        journal.update(status='failed', failedAt=now(), error=type(e).__name__ + ': ' + str(e))
        if 'row' in locals():
            journal['lastAttempt'] = {'root': row['root'], 'path': row['path']}
        save_journal()
        raise


def decrypt_backup(archive, key, auth, destination):
    archive, key, auth, destination = map(Path, [archive, key, auth, destination])
    meta = json.loads(auth.read_text())
    mac = hmac.new(hashlib.sha256(b'backup-auth-v1\x00' + key.read_bytes()).digest(), digestmod='sha256')
    with open(archive, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            mac.update(block)
    if not hmac.compare_digest(mac.hexdigest(), meta['hmac']) or hash_file(archive) != meta['ciphertextSha256']:
        raise ValueError('Encrypted backup authentication failed')
    if destination.exists():
        raise FileExistsError('Decryption never overwrites existing files')
    with open(os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
        r = subprocess.run(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000',
                            '-pass', 'file:' + str(key), '-in', str(archive)], stdout=out, stderr=subprocess.PIPE)
    if r.returncode:
        raise ValueError('Backup decryption failed')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pp = commands.add_parser('plan', help='Read source metadata; write a new private plan only')
    pp.add_argument('--home', default=str(Path.home()))
    pp.add_argument('--out', required=True)
    pp.add_argument('--include-projects', action='store_true')
    pp.add_argument('--exclude', action='append', default=[])
    cp = commands.add_parser('capture', help='Create encrypted Mac backup and Windows data package; never imports')
    cp.add_argument('--plan', required=True)
    cp.add_argument('--out', required=True)
    cp.add_argument('--key-file', required=True)
    cp.add_argument('--live', action='store_true', help='Explicit online snapshot; not a final cold backup')
    cp.add_argument('--resume-from', help='Reuse verified objects from a matching failed online run; never overwrite it')
    vp = commands.add_parser('verify', help='Verify all archive payload hashes without extracting/importing')
    vp.add_argument('--archive', required=True)
    ip = commands.add_parser('inspect', help='Print archive summary only; no transcript content')
    ip.add_argument('--archive', required=True)
    dp = commands.add_parser('decrypt', help='Authenticate/decrypt a backup to a NEW archive, never restore apps')
    for name in ['archive', 'key-file', 'auth', 'out']:
        dp.add_argument('--' + name, required=True)
    args = parser.parse_args()
    if args.command == 'plan':
        p = plan(args.home, args.include_projects, args.exclude)
        out = regular_path(args.out)
        for r in p['roots']:
            if within(out, r['path']) and not any(within(out, x) for x in p['exclusions']):
                raise ValueError('Plan output must be outside source roots')
        new_file(out, dump(p))
        print(json.dumps({'plan': str(out), 'roots': len(p['roots']), 'entries': len(p['entries']),
                          'sourceBytes': sum(e.get('size', 0) for e in p['entries']),
                          'projectsIncluded': p['includeProjects'], 'digest': p['digest']}))
    elif args.command == 'capture':
        result = capture(load_plan(args.plan), args.out, args.key_file, args.live, args.resume_from)
        print(json.dumps({k: result[k] for k in ['status', 'sourceDifferenceCount', 'needsColdFinalBackup', 'encryptedBackupReadbackVerified']}))
    elif args.command == 'verify':
        result = verify_archive(args.archive)
        result['consistency'] = {k: v for k, v in result['consistency'].items() if k != 'sourceDifferences'}
        print(json.dumps(result))
    elif args.command == 'inspect':
        with zipfile.ZipFile(args.archive) as z:
            m = json.loads(z.read('manifest.json'))
        print(json.dumps({'kind': m['kind'], 'version': m['version'], 'entries': len(m['entries']),
                          'projectsIncluded': m['projectsIncluded'], 'roots': [{k: r[k] for k in ['label', 'kind']} for r in m['roots']],
                          'consistencyMode': m['consistency']['mode'], 'windowsImported': False, 'modelVerified': False}))
    elif args.command == 'decrypt':
        decrypt_backup(args.archive, args.key_file, args.auth, regular_path(args.out))
        print(json.dumps(verify_archive(args.out)))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        sys.exit(1)

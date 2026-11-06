"""Disk-indexed recovery inventory for preserved sessions and backup bundles.

All paths embedded in manifests are display metadata. Dependencies are resolved
only inside the explicitly selected bundle (or explicit key picker selections).
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import threading

from . import archive_io as aio
from .backup import read_key


def _json(value): return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
def _id(value): return hashlib.sha256(value.encode()).hexdigest()[:32]


class UnsupportedFormat(ValueError):
    pass


class CacheSpaceError(RuntimeError):
    pass


CACHE_RESERVE = 512 * 1024 * 1024


def safe_relative(value):
    value = str(value)
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or '\x00' in value or value.startswith('\\') or re.match(r'^[A-Za-z]:', value):
        raise ValueError('备份路径不安全')
    if any(part in {'', '.'} for part in value.split('/')):
        raise ValueError('备份路径不安全')
    return p.as_posix()


def _display_layout(original):
    # Original absolute names never become host paths. POSIX backslashes are
    # literal filename characters; translate separators only for unmistakable
    # Windows source paths, and let the output encoder escape illegal characters.
    value = str(original)
    if re.match(r'^[A-Za-z]:[\\/]', value):
        value = value[0] + '/' + value[3:].replace('\\', '/')
    elif value.startswith('\\\\'):
        value = 'windows-unc/' + value.lstrip('\\').replace('\\', '/')
    return 'original/' + value.lstrip('/')


class RecoveryCatalog:
    def __init__(self, root: Path, cache: Path, keys: list[Path] | None = None):
        self.root = Path(root).resolve(); self.bundle_root = self.root
        self.cache = Path(cache).resolve()
        if self.root == self.cache:
            raise ValueError('工具缓存不能与备份目录相同，请选择其他缓存目录')
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.keys = [Path(p).resolve() for p in (keys or [])]
        self._lock = threading.RLock(); self._closed = False
        self.db = sqlite3.connect(self.cache / ('recovery-' + _id(str(self.root)) + '.sqlite'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA cache_size=-16384;
          CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY,identity TEXT,key_path TEXT,key_identity TEXT,auth_path TEXT,auth_identity TEXT);
          CREATE TABLE IF NOT EXISTS members(archive TEXT,name TEXT,info TEXT,PRIMARY KEY(archive,name));
          CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,group_id TEXT,path TEXT,original_path TEXT,kind TEXT,size INTEGER,sha256 TEXT,category TEXT,status TEXT,detail TEXT,locator TEXT,metadata TEXT);
          CREATE INDEX IF NOT EXISTS item_group_path ON items(group_id,path);
          CREATE TABLE IF NOT EXISTS mappings(owner TEXT,name TEXT,data TEXT,PRIMARY KEY(owner,name));
          CREATE TABLE IF NOT EXISTS pending(group_id TEXT,path TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS manifest_entries(owner TEXT,ordinal INTEGER,data TEXT,PRIMARY KEY(owner,ordinal));
          CREATE TABLE IF NOT EXISTS root_metadata(owner TEXT,label TEXT,data TEXT,PRIMARY KEY(owner,label));
        ''')
        self._groups = {}; self._files = {}; self._archives = []
        self._indexing = set()
        self._cache_operations = 0

    def _cache_guard(self, force=False):
        self._cache_operations += 1
        if force or self._cache_operations % 32 == 0:
            if shutil.disk_usage(self.cache).free < CACHE_RESERVE:
                raise CacheSpaceError('工具缓存磁盘余量不足 512 MiB；已停止索引，请选择其他空间充足的缓存目录')

    def _checkpoint_cache(self):
        # This connection owns only this bundle's catalog. Never delete another
        # cache or source; an active cursor can legitimately defer truncation.
        try:
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        except sqlite3.Error:
            pass

    def _save_group(self, group):
        self._groups[group['id']] = group
        self.db.execute('INSERT OR REPLACE INTO groups VALUES (?,?)', (group['id'], _json(group)))
        self.db.commit()
        return dict(group)

    def _bundle_path(self, path, directory=False):
        """Recheck lexical parents and resolved containment before source reads.

        Explicit picker keys use a separate path; this guard applies only to
        files discovered inside the selected backup bundle.
        """
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(self.root):
            raise ValueError('备份来源超出所选目录')
        current = path
        while True:
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise ValueError('备份来源经过链接或目录联接')
            if current == self.root: break
            current = current.parent
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError('备份来源超出所选目录')
        if directory and not path.is_dir() or not directory and not path.is_file():
            raise ValueError('备份来源类型不受支持')
        return path

    def _in_bundle(self, path, directory=False):
        try:
            self._bundle_path(path, directory)
            return True
        except (OSError, ValueError):
            return False

    def discover(self):
        with self._lock:
            self._bundle_path(self.root, directory=True)
            self._files = {}; self._archives = []; candidates = {}
            for current, dirs, files in os.walk(self.root, followlinks=False):
                folder = Path(current)
                if not self._in_bundle(folder, directory=True):
                    dirs[:] = []
                    continue
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and self._in_bundle(folder / d, directory=True) and (folder / d).resolve() != self.cache)
                for name in sorted(files):
                    p = folder / name
                    if not self._in_bundle(p): continue
                    rel = p.relative_to(self.root).as_posix()
                    self._files.setdefault(name, []).append(p)
                    lower = name.lower()
                    is_key = lower.endswith(('.asmkey', '.recovery.key'))
                    is_software = lower.endswith(('.dmg', '.exe', '.msi', '.app')) or ('ai-session-migration' in lower and lower.endswith('.zip'))
                    archive = lower.endswith(('.zip', '.asmb', '.zip.enc')) and not is_software and not name.startswith('._')
                    category = 'keys' if is_key else 'software' if is_software else 'other'
                    if archive:
                        self._archives.append(p)
                        if any(s in lower for s in ('会话', 'session')): category = 'sessions'
                        if any(s in lower for s in ('项目', 'project')): category = 'projects' if lower.endswith('.zip') else 'applications'
                        if any(s in lower for s in ('快照', 'snapshot')): category = 'snapshots'
                        if any(s in lower for s in ('数据库', 'database')) and '旧验收' not in lower: category = 'databases'
                        if any(s in lower for s in ('差异', '不同版本', 'delta', 'version')) or '失败批次' in rel: category = 'versions'
                        base = re.sub(r'(?:[-_]?卷\d+|[-_]?(?:part|vol(?:ume)?)[-_]?\d+)(?=\.)', '', name, flags=re.I)
                        token = folder.relative_to(self.root).as_posix() + '/' + base
                    else:
                        token = category + ':' + folder.relative_to(self.root).as_posix()
                    candidates.setdefault(token, {'paths': [], 'category': category, 'archive': archive})['paths'].append(str(p))
            updated = {}
            for token, data in candidates.items():
                gid = _id(token); previous = self.db.execute('SELECT data FROM groups WHERE id=?', (gid,)).fetchone()
                group = json.loads(previous[0]) if previous else {}
                paths = data['paths']; category = data['category']; archive = data['archive']
                # A moved bundle's cached locators are intentionally rebuilt.
                unchanged = group.get('_paths') == paths and all(self._source_unchanged(p) for p in paths) if archive else False
                status = group.get('status', 'unindexed') if unchanged else 'unindexed'
                group.update(id=gid, label=Path(paths[0]).name if archive else (Path(paths[0]).parent.name + ' / ' + category),
                             category=category, path=paths[0] if archive else str(Path(paths[0]).parent), status=status,
                             count=group.get('count', 0) if unchanged else 0, size=sum(Path(p).stat().st_size for p in paths),
                             encrypted=archive and any(p.endswith(('.asmb', '.enc')) for p in paths), detail='', _paths=paths, _archive=archive)
                if category == 'sessions':
                    native = next((p for p in paths if p.endswith('.zip')), None)
                    if native: group['native_archive'] = native
                    elif not any(s in group['label'] for s in ('差异', '不同版本')):
                        for p in self._archives:
                            if p.parent == Path(paths[0]).parent and p.suffix == '.zip' and ('会话' in p.name or 'session' in p.name.lower()):
                                group['native_archive'] = str(p); break
                updated[gid] = self._save_group(group)
            self._groups = updated
            return self.groups()

    def groups(self):
        with self._lock: return [dict(g) for g in self._groups.values()]

    def _source_unchanged(self, path):
        row = self.db.execute('SELECT * FROM sources WHERE path=?', (str(path),)).fetchone()
        try:
            self._bundle_path(path)
            if row and row['key_path'] and Path(row['key_path']) not in self.keys:
                self._bundle_path(row['key_path'])
            if row and row['auth_path']:
                self._bundle_path(row['auth_path'])
            return bool(row and aio.identity(path) == json.loads(row['identity']) and
                        (not row['key_path'] or aio.identity(row['key_path']) == json.loads(row['key_identity'])) and
                        (not row['auth_path'] or aio.identity(row['auth_path']) == json.loads(row['auth_identity'])))
        except (OSError, ValueError): return False

    def _resolve(self, recorded):
        if not recorded: return None
        p = Path(str(recorded))
        if p.is_absolute() and self._in_bundle(p): return p
        direct = self.root / p
        if not p.is_absolute() and self._in_bundle(direct): return direct
        found = [candidate for candidate in self._files.get(p.name, []) if self._in_bundle(candidate)]
        return found[0] if len(found) == 1 else None

    def _key_for(self, path, progress=lambda *_: None, cancel=None):
        explicit = self.keys
        auto = [p for paths in self._files.values() for p in paths if p.name.lower().endswith(('.asmkey', '.recovery.key')) and self._in_bundle(p)]
        if path.suffix.lower() == '.asmb':
            kid = aio.asmb_header(path)[0]['keyId']
            for p in dict.fromkeys(explicit + auto):
                try:
                    if read_key(p)[1] == kid: return p, None
                except (OSError, ValueError, KeyError, json.JSONDecodeError): continue
            raise KeyError('需要匹配的恢复密钥；可选择密钥文件')
        if path.suffix.lower() == '.enc':
            candidates = [p for p in explicit + auto if p.name.lower().endswith('.recovery.key')]
            auth = self._resolve(str(path.parent / 'backup-auth.json'))
            for p in dict.fromkeys(candidates):
                try:
                    expected = aio.authenticate(path, p, auth, progress, cancel)
                    return p, expected
                except (OSError, ValueError, KeyError): continue
            raise KeyError('旧加密备份需要匹配密钥与认证文件')
        return None, None

    def _ensure_archive(self, path, progress=lambda *_: None, cancel=None, *, force_auth=False):
        path = self._bundle_path(path)
        if not force_auth and self._source_unchanged(path): return
        key, authenticated = self._key_for(path, progress, cancel)
        auth = self._resolve(str(path.parent / 'backup-auth.json')) if path.suffix.lower() == '.enc' else None
        expected = authenticated or aio.authenticate(path, key, auth, progress, cancel)
        self.db.execute('DELETE FROM members WHERE archive=?', (str(path),))
        with aio.ArchiveReader(path, expected, key) as reader:
            for index, fields in enumerate(aio.central_entries(reader)):
                aio.cancelled(cancel)
                self._cache_guard()
                try: self.db.execute('INSERT INTO members VALUES (?,?,?)', (str(path), fields['filename'], _json(fields)))
                except sqlite3.IntegrityError as error: raise ValueError('ZIP 含有重复对象名') from error
                if index % 2000 == 0: progress('建立备份对象索引', 0)
        aio.check_identity(path, expected)
        self._cache_guard()
        self.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?)', (str(path), _json(expected), str(key) if key else None,
                         _json(aio.identity(key)) if key else None, str(auth) if auth else None, _json(aio.identity(auth)) if auth else None))
        self.db.commit()

    @contextmanager
    def _member(self, path, name):
        path = self._bundle_path(path)
        row = self.db.execute('SELECT * FROM sources WHERE path=?', (str(path),)).fetchone()
        member = self.db.execute('SELECT info FROM members WHERE archive=? AND name=?', (str(path), name)).fetchone()
        if not row or not member: raise FileNotFoundError('缺少备份数据对象')
        if not self._source_unchanged(path): raise ValueError('备份来源已改变，请重新建立索引')
        with aio.ArchiveReader(path, json.loads(row['identity']), row['key_path']) as reader:
            with aio.zip_member(reader, json.loads(member[0])) as stream:
                yield stream
        aio.check_identity(path, json.loads(row['identity']))

    def _has(self, archive, name):
        return self.db.execute('SELECT 1 FROM members WHERE archive=? AND name=?', (str(archive), name)).fetchone() is not None

    def _small_json(self, archive, name):
        with self._member(archive, name) as f:
            data = f.read(aio.MAX_VALUE + 1)
            if len(data) > aio.MAX_VALUE: raise ValueError('单项元数据过大')
            return json.loads(data)

    def _object(self, archives, name):
        if name:
            safe_relative(name)
        for archive in archives:
            if self._has(archive, name): return {'archive': str(archive), 'member': name}
        return None

    def _put(self, group, entry, locator=None, prefix=None, forced_path=None, status=None):
        if not isinstance(entry, dict): raise ValueError('备份条目结构无效')
        original = str(entry.get('originalPath', entry.get('absolute', '')))
        kind = entry.get('type', entry.get('kind', 'file'))
        if kind in {'dir', 'folder'}: kind = 'directory'
        logical = forced_path or (None if original and group['category'] in {'databases', 'snapshots'} else entry.get('file'))
        if logical is None and entry.get('path') is not None:
            root = str(entry.get('_rootPrefix', entry.get('root', entry.get('provider', 'data'))))
            logical = root + ('/' + str(entry['path']) if entry['path'] not in {'', '.'} else '')
        if logical is None and original: logical = _display_layout(original)
        if logical is None:
            uid = str(entry.get('sessionUid', 'metadata'))
            logical = 'sessions/' + uid + '/' + str(entry.get('logicalName', entry.get('role', 'data'))) + '-' + Path(str(entry.get('object', 'notice'))).name
        if prefix: logical = prefix + '/' + logical
        size = int(entry.get('capturedSize', entry.get('bytes', entry.get('size', 0))) or 0)
        sha = entry.get('sha256'); detail = str(entry.get('capture', entry.get('note', '')))
        if kind not in {'file', 'directory', 'symlink', 'notice'}:
            kind = 'notice'; status = status or 'unsupported'; detail = detail or '原生运行时特殊文件不可作为普通文件恢复'
        if original.endswith(('.app', '.dmg')) or '/Library/' in original:
            detail = (detail + '；' if detail else '') + 'Mac 原生资料；复制不代表 Windows 原生恢复'
        if kind == 'symlink':
            payload = (_json({'schema': 'asm-preserved-symlink-1', 'originalPath': original, 'target': entry.get('target', ''), 'followed': False}) + '\n').encode()
            locator = {'descriptor': payload.decode()}; size = len(payload); sha = hashlib.sha256(payload).hexdigest()
            detail = '符号链接以 JSON 描述文件保全，不跟随链接'
        if kind == 'directory': size = 0; sha = None
        status = status or ('ready' if locator or kind == 'directory' else 'not-captured')
        if entry.get('capture') in {'online-source-not-captured', 'runtime-index-rebuilt-by-native-client', 'included-in-consistent-sqlite-main-snapshot'} and not locator:
            status = 'not-captured'
        try: logical = safe_relative(logical)
        except ValueError: status = 'unsupported'; detail = '路径含不安全或不受支持的字符，保留原始名称供查看'
        if size < 0: raise ValueError('备份条目长度无效')
        iid = _id(group['id'] + '\0' + str(logical))
        self._cache_guard()
        self.db.execute('INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (iid, group['id'], logical, original, kind, size, sha, group['category'], status, detail,
                         _json(locator or {}), _json(entry)))

    def _stage_manifest(self, owner, stream, cancel):
        # A manifest may put roots after its large entries array. Keep both in
        # SQLite, so root resolution is order-independent and bounded in RAM.
        self.db.execute('DELETE FROM manifest_entries WHERE owner=?', (owner,))
        self.db.execute('DELETE FROM root_metadata WHERE owner=?', (owner,))
        metadata = {}; ordinal = 0
        for key, value, array in aio.json_fields(stream):
            aio.cancelled(cancel)
            if key == 'entries' and array:
                if not isinstance(value, dict): raise ValueError('备份条目结构无效')
                self._cache_guard()
                self.db.execute('INSERT INTO manifest_entries VALUES (?,?,?)', (owner, ordinal, _json(value)))
                ordinal += 1
            elif key == 'roots':
                roots = [(value.get('label'), value)] if array and isinstance(value, dict) else list(value.items()) if isinstance(value, dict) else []
                for label, root in roots:
                    if not isinstance(root, dict) or not isinstance(root.get('path'), str) or label is None:
                        raise ValueError('备份根目录映射无效')
                    label = str(label); root = dict(root); root.setdefault('label', label)
                    old = self.db.execute('SELECT data FROM root_metadata WHERE owner=? AND label=?', (owner, label)).fetchone()
                    if old and json.loads(old[0]) != root: raise ValueError('备份根目录映射包含冲突标识')
                    self._cache_guard()
                    self.db.execute('INSERT OR REPLACE INTO root_metadata VALUES (?,?,?)', (owner, label, _json(root)))
            elif key in {'schema', 'kind'} and not array:
                metadata[key] = value
        return metadata

    def _mapped_root(self, owner, entry):
        label = str(entry.get('root', ''))
        row = self.db.execute('SELECT data FROM root_metadata WHERE owner=? AND label=?', (owner, label)).fetchone()
        if row is None: return entry
        root = json.loads(row[0]); source = root['path']; kind = str(root.get('kind', 'data'))
        # Interpret unmistakable Windows separators only in display metadata.
        source_parts = source.replace('\\', '/') if re.match(r'^[A-Za-z]:[\\/]', source) or source.startswith('\\\\') else source
        basename = PurePosixPath(source_parts.rstrip('/')).name or kind
        readable = basename if kind in {'workspace', 'project', 'data', ''} or basename.lstrip('.').casefold() == kind.casefold() else kind + '-' + basename
        prefix = readable + '-' + _id(label + '\0' + source)[:12]
        result = dict(entry); result['_rootPrefix'] = prefix
        result['_rootMetadata'] = root
        if not result.get('originalPath') and not result.get('absolute'):
            relative = str(result.get('path', ''))
            is_file_root = root.get('type') == 'file' or root.get('isFile') is True
            if relative in {'', '.'} or is_file_root:
                original = source
            else:
                original = source.rstrip('/') + '/' + relative
            result['originalPath'] = original
        return result

    def _manifest(self, group, archive, name, archives, cancel, prefix=None):
        owner = str(archive) + '\0' + name
        with self._member(archive, name) as stream:
            metadata = self._stage_manifest(owner, stream, cancel)
        schema = metadata.get('schema')
        known = {'ai-session-migration-gui-1': 'sessions', 'asm-complete-native-databases-1': 'databases',
                 'asm-old-validation-snapshots-1': 'snapshots', 'conversation-migration-1': group['category']}
        if schema is not None:
            if schema not in known: raise UnsupportedFormat('备份清单版本尚未支持')
            group['category'] = known[schema]
            if schema == 'ai-session-migration-gui-1' and not group.get('native_archive'):
                group['native_archive'] = str(archive)
        if metadata.get('kind') == 'mac-full-preservation': group['category'] = 'applications'
        for row in self.db.execute('SELECT data FROM manifest_entries WHERE owner=? ORDER BY ordinal', (owner,)):
            aio.cancelled(cancel)
            entry = self._mapped_root(owner, json.loads(row[0]))
            obj = entry.get('object', entry.get('file'))
            loc = self._object(archives, obj) if obj else None
            self._put(group, entry, loc, prefix, status='missing' if obj and not loc else None)

    def _reference_sources(self, archive, member):
        value = self._small_json(archive, member)
        if not isinstance(value, dict): raise ValueError('引用来源格式无效')
        return {str(k): self._resolve(v) for k, v in value.items()}

    def _full_archives(self, owner=None):
        # Writers preserve the exact dependency list in the adjacent result
        # report. Prefer it over names, especially when a parent directory holds
        # several complete backup jobs with overlapping global object numbers.
        if owner is not None:
            for report in Path(owner).parent.glob('*.json'):
                if report.name.startswith('._') or not self._in_bundle(report) or report.stat().st_size > 2 * aio.CHUNK:
                    continue
                try:
                    value = json.loads(aio.bounded_bytes(report, 2 * aio.CHUNK))
                except (OSError, ValueError):
                    continue
                if isinstance(value, dict) and Path(str(value.get('archive', ''))).name == Path(owner).name:
                    recorded = value.get('latestFullParts') or ([value['latestFullReferenced']] if value.get('latestFullReferenced') else [])
                    if recorded:
                        return [p for name in recorded if (p := self._resolve(name)) is not None]
        candidates = [p for p in self._archives if any(t in p.name.lower() for t in ('完整保全', 'full-preservation', 'full_preservation', 'full-part', 'full-vol'))]
        if owner is not None:
            for folder in [Path(owner).parent, *Path(owner).parents]:
                if folder != self.root and not folder.is_relative_to(self.root): break
                nearby = [p for p in candidates if p.parent == folder]
                if nearby: return nearby
                if folder == self.root: break
        return candidates

    def _load_dependencies(self, paths, progress, cancel):
        available = []
        for p in dict.fromkeys(p for p in paths if p):
            try: self._ensure_archive(p, progress, cancel); available.append(p)
            except (FileNotFoundError, KeyError): pass
        return available

    def _old_session(self, group, archive, progress, cancel):
        package = None
        # coverage.mapping can be large; keep it in the disk index.
        self.db.execute('DELETE FROM mappings WHERE owner=?', (str(archive),))
        with self._member(archive, 'coverage.json') as stream:
            for k, value, array in aio.json_fields(stream):
                aio.cancelled(cancel)
                if k == 'newSessionPackage': package = self._resolve(value)
                elif k == 'mapping' and array:
                    self._cache_guard()
                    self.db.execute('INSERT INTO mappings VALUES (?,?,?)', (str(archive), value['oldObject'], _json(value)))
        deps = self._load_dependencies([package], progress, cancel)
        with self._member(archive, 'original-manifest.json') as stream:
            for k, entry, array in aio.json_fields(stream):
                if k != 'entries' or not array: continue
                aio.cancelled(cancel)
                row = self.db.execute('SELECT data FROM mappings WHERE owner=? AND name=?', (str(archive), entry.get('object'))).fetchone()
                mapping = json.loads(row[0]) if row else {}
                locations = [archive] if mapping.get('source') == 'this-delta' else deps
                obj = mapping.get('newObject', mapping.get('oldObject'))
                loc = self._object(locations, obj) if obj else None
                if mapping and (mapping.get('sha256') != entry.get('sha256') or mapping.get('bytes') != entry.get('size', entry.get('capturedSize'))):
                    raise ValueError('历史会话引用与原清单不一致')
                self._put(group, entry, loc, status='missing' if not loc else None)

    def _failed_delta(self, group, archive, progress, cancel):
        deps = self._load_dependencies(self._full_archives(archive), progress, cancel)
        # Metadata is parsed first to recover object names back to original paths.
        self.db.execute('DELETE FROM mappings WHERE owner=?', (str(archive),))
        with self._member(archive, 'coverage.jsonl') as stream:
            for row in aio.json_lines(stream):
                aio.cancelled(cancel)
                self._cache_guard()
                self.db.execute('INSERT OR REPLACE INTO mappings VALUES (?,?,?)', (str(archive), row['batch'] + '/' + row['archive'] + '/' + row['originalObject'], _json(row)))
        self.db.execute('DELETE FROM pending WHERE group_id=?', (group['id'],))
        rows = self.db.execute('SELECT data FROM mappings WHERE owner=?', (str(archive),))
        for row in rows:
            mapping = json.loads(row[0]); old = mapping['originalObject']
            locations = [archive] if mapping.get('source') == 'this-delta' else deps
            loc = self._object(locations, mapping.get('object')) if mapping.get('object') else None
            logical = mapping['batch'] + '/' + mapping['archive'] + '/' + old
            if old.endswith('manifest.json') and loc:
                try:
                    with self._member(loc['archive'], loc['member']) as f:
                        for k, value, array in aio.json_fields(f):
                            if k == 'entries' and array:
                                self._cache_guard()
                                self.db.execute('INSERT INTO pending VALUES (?,?,?)', (group['id'], mapping['batch'] + '/' + mapping['archive'] + '/' + str(value.get('object', '')), _json(value)))
                except (ValueError, json.JSONDecodeError):
                    raise ValueError('历史失败批次的原始清单损坏')
            # All payloads remain represented even if their original manifest was
            # never captured; these rows clearly retain their original ZIP names.
            entry = {'file': logical, 'bytes': mapping['bytes'], 'sha256': mapping['sha256'],
                     'capture': '原失败批次未保存原文件名；可按原对象提取，不代表已恢复到原项目'}
            if mapping.get('source') == 'legacy-key-retained-original-no-copy':
                entry['originalPath'] = mapping.get('originalPath', ''); entry['capture'] = '原密钥保留；此包未复制'
                self._put(group, entry, status='not-captured')
            else: self._put(group, entry, loc, status='missing' if not loc else None)
        # Replace numbered objects with source hierarchy when metadata provides it.
        for row in self.db.execute('SELECT path,data FROM pending WHERE group_id=?', (group['id'],)):
            reference = self.db.execute('SELECT data FROM mappings WHERE owner=? AND name=?', (str(archive), row['path'])).fetchone()
            if not reference: continue
            mapping = json.loads(reference[0]); entry = json.loads(row['data'])
            if entry.get('sha256') != mapping['sha256'] or entry.get('capturedSize', entry.get('size')) != mapping['bytes']:
                raise ValueError('历史失败批次引用与原清单不一致')
            locations = [archive] if mapping.get('source') == 'this-delta' else deps
            loc = self._object(locations, mapping.get('object')) if mapping.get('object') else None
            self.db.execute('DELETE FROM items WHERE group_id=? AND path=?', (group['id'], row['path']))
            prefix = mapping['batch'] + '/' + mapping['archive']
            self._put(group, entry, loc, prefix, status='missing' if not loc else None)

    def _exact_old(self, group, archive, progress, cancel):
        sources = self._reference_sources(archive, '保全来源.json')
        available = self._load_dependencies(sources.values(), progress, cancel)
        self.db.execute('DELETE FROM mappings WHERE owner=?', (str(archive),))
        with self._member(archive, '原完整包对象覆盖.jsonl') as stream:
            for row in aio.json_lines(stream):
                aio.cancelled(cancel)
                self._cache_guard()
                self.db.execute('INSERT INTO mappings VALUES (?,?,?)', (str(archive), row['oldObject'], _json(row)))
        manifest = self._resolve('old-full-manifest.json')
        if not manifest:
            # Payload rows remain visible, but a numbered object is never claimed
            # to have a reconstructed original name without its source manifest.
            for record in self.db.execute('SELECT data FROM mappings WHERE owner=?', (str(archive),)):
                row = json.loads(record[0]); self._put(group, {'file': row['oldObject'], 'bytes': row['bytes'], 'sha256': row['sha256']}, status='missing')
            group['detail'] = '缺少原完整包清单，无法还原原文件名'
            return
        self._bundle_path(manifest)
        expected = aio.identity(manifest); owner = str(archive) + '\0legacy-manifest'
        with manifest.open('rb') as stream:
            self._stage_manifest(owner, stream, cancel)
        aio.check_identity(manifest, expected)
        for row in self.db.execute('SELECT data FROM manifest_entries WHERE owner=? ORDER BY ordinal', (owner,)):
            aio.cancelled(cancel)
            entry = self._mapped_root(owner, json.loads(row[0]))
            record = self.db.execute('SELECT data FROM mappings WHERE owner=? AND name=?', (str(archive), entry.get('object'))).fetchone()
            mapping = json.loads(record[0]) if record else {}
            source = sources.get(str(mapping.get('sourceId')))
            loc = self._object([source], mapping.get('object')) if source in available and mapping.get('object') else None
            if mapping and (mapping.get('sha256') != entry.get('sha256') or mapping.get('bytes') != entry.get('capturedSize', entry.get('size'))):
                raise ValueError('原完整包引用与原清单不一致')
            status = 'not-captured' if mapping.get('legacyKeyOriginalKept') else 'missing' if entry.get('object') and not loc else None
            self._put(group, entry, loc, status=status)

    def index_group(self, group_id: str, progress=lambda *_: None, cancel=None):
        with self._lock:
            group = self._groups[group_id]
            paths = [Path(p) for p in group['_paths']]
            group.update(status='unindexed', count=0, detail='')
            self._save_group(group)
            self.db.execute('DELETE FROM items WHERE group_id=?', (group_id,))
            try:
                self._cache_guard(force=True)
                if not group['_archive']:
                    for p in paths:
                        aio.cancelled(cancel)
                        self._bundle_path(p)
                        expected = aio.identity(p)
                        entry = {'file': p.name, 'size': expected[2]}
                        if group['category'] == 'keys':
                            entry['sha256'] = hashlib.sha256(aio.bounded_bytes(p, 16384)).hexdigest()
                            aio.check_identity(p, expected)
                        status = 'unsupported' if p.name.startswith('._') else None
                        self._put(group, entry, {'file': str(p), 'identity': expected}, status=status)
                else:
                    for p in paths:
                        # Reinterpreting an encrypted manifest requires a fresh
                        # full authentication. Stat caches cannot prove content
                        # identity when Windows creation time and restored mtime
                        # remain unchanged after same-length ciphertext edits.
                        self._ensure_archive(p, progress, cancel, force_auth=p.suffix.lower() in {'.asmb', '.enc'})
                    for archive in paths:
                        if self._has(archive, 'original-manifest.json') and self._has(archive, 'coverage.json'):
                            group['category'] = 'versions'; self._old_session(group, archive, progress, cancel)
                        elif self._has(archive, 'coverage.jsonl'):
                            group['category'] = 'versions'; self._failed_delta(group, archive, progress, cancel)
                        elif self._has(archive, '原完整包对象覆盖.jsonl'):
                            group['category'] = 'versions'; self._exact_old(group, archive, progress, cancel)
                        elif self._has(archive, 'manifest.json'):
                            self._manifest(group, archive, 'manifest.json', paths, cancel)
                        else:
                            # A first full volume carries payloads only. Its sibling
                            # volume's manifest maps the global object namespace.
                            sibling_manifest = any(self._has(p, 'manifest.json') for p in paths)
                            if sibling_manifest: continue
                            if any(t in archive.name.lower() for t in ('完整保全', 'full-preservation', 'full_preservation')):
                                group['_manifest_missing'] = True
                                group['detail'] = '缺少包含原始文件清单的完整备份分卷'
                                self._put(group, {'file': 'missing-manifest.json', 'type': 'notice', 'note': group['detail']}, status='missing')
                                continue
                            for row in self.db.execute('SELECT name,info FROM members WHERE archive=?', (str(archive),)):
                                fields = json.loads(row['info']); name = row['name']
                                kind = 'directory' if name.endswith('/') else 'file'
                                status = 'unsupported' if name.startswith('__MACOSX/') or Path(name).name.startswith('._') else None
                                mode = fields.get('external_attr', 0) >> 16
                                if stat.S_ISLNK(mode): kind = 'notice'; status = 'unsupported'
                                self._put(group, {'file': name.rstrip('/'), 'size': fields['file_size'], 'type': kind},
                                          {'archive': str(archive), 'member': name} if kind == 'file' else None, status=status)
                # ZIP producers need not emit directory entries. Index virtual
                # parents on disk so a folder is selectable in every format.
                progress('建立可选择的逻辑文件夹', 0)
                parent_rows = 0
                for row in self.db.execute('SELECT path,status FROM items WHERE group_id=?', (group_id,)):
                    aio.cancelled(cancel)
                    parent_rows += 1
                    if parent_rows % 256 == 0:
                        progress('建立可选择的逻辑文件夹', 0)
                    if row['status'] == 'unsupported': continue
                    parent = PurePosixPath(row['path']).parent
                    while str(parent) != '.':
                        aio.cancelled(cancel)
                        name = parent.as_posix(); iid = _id(group_id + '\0' + name)
                        self._cache_guard()
                        self.db.execute('INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                        (iid, group_id, name, '', 'directory', 0, None, group['category'], 'ready', '逻辑文件夹', '{}', '{}'))
                        parent = parent.parent
                totals = self.db.execute('SELECT count(*),coalesce(sum(size),0) FROM items WHERE group_id=?', (group_id,)).fetchone()
                group.update(status='missing' if group.pop('_manifest_missing', False) else 'ready', count=totals[0], size=totals[1])
                self.db.execute('UPDATE items SET category=? WHERE group_id=?', (group['category'], group_id))
                progress('索引完成', 100)
            except CacheSpaceError as error:
                self.db.rollback()
                self._checkpoint_cache()
                count = self.db.execute('SELECT count(*) FROM items WHERE group_id=?', (group_id,)).fetchone()[0]
                group.update(status='error', detail=str(error), count=count)
                self._save_group(group)
                self._checkpoint_cache()
                raise
            except UnsupportedFormat:
                self.db.rollback()
                self.db.execute('DELETE FROM items WHERE group_id=?', (group_id,)); group.update(status='unsupported', detail='备份清单版本尚未支持；原始备份保留', count=0)
            except KeyError:
                self.db.rollback()
                self.db.execute('DELETE FROM items WHERE group_id=?', (group_id,)); group.update(status='key-required', detail='需要匹配的恢复密钥；可选择密钥文件', count=0)
            except FileNotFoundError:
                self.db.rollback()
                self.db.execute('DELETE FROM items WHERE group_id=?', (group_id,)); group.update(status='missing', detail='备份来源或依赖文件缺失', count=0)
            except BaseException as error:
                self.db.rollback()
                self.db.execute('DELETE FROM items WHERE group_id=?', (group_id,)); group.update(status='error', detail='备份验证或索引失败：' + type(error).__name__, count=0)
                self._save_group(group)
                raise
            return self._save_group(group)

    def _item(self, row):
        value = dict(row); value['_locator'] = json.loads(value.pop('locator')); value['_metadata'] = json.loads(value.pop('metadata'))
        return value

    def list_items(self, group_id, search='', offset=0, limit=200):
        with self._lock:
            if offset < 0 or not 1 <= limit <= 1000: raise ValueError('分页范围无效')
            rows = self.db.execute('SELECT * FROM items WHERE group_id=? AND (instr(lower(path),lower(?))>0 OR instr(lower(original_path),lower(?))>0) ORDER BY path,id LIMIT ? OFFSET ?',
                                   (group_id, search, search, limit, offset))
            return [self._item(r) for r in rows]

    def count_items(self, group_id, search=''):
        with self._lock:
            return self.db.execute('SELECT count(*) FROM items WHERE group_id=? AND (instr(lower(path),lower(?))>0 OR instr(lower(original_path),lower(?))>0)', (group_id, search, search)).fetchone()[0]

    def get_item(self, item_id):
        with self._lock:
            row = self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
            if row is None: raise KeyError('恢复条目不存在')
            return self._item(row)

    def iter_items(self, item_ids=(), group_ids=()):
        # A temporary disk-backed selection table prevents all-selection RAM use
        # and de-duplicates overlapping group/folder/file selections.
        with self._lock:
            self.db.execute('CREATE TEMP TABLE IF NOT EXISTS selected_items(id TEXT PRIMARY KEY)')
            self.db.execute('DELETE FROM selected_items')
            for gid in group_ids:
                if self._groups[gid]['status'] != 'ready': raise ValueError('请先建立所选备份组的索引')
                self.db.execute('INSERT OR IGNORE INTO selected_items SELECT id FROM items WHERE group_id=?', (gid,))
            for iid in item_ids:
                item = self.get_item(iid)
                if self._groups[item['group_id']]['status'] != 'ready': raise ValueError('请先建立所选备份组的索引')
                self.db.execute('INSERT OR IGNORE INTO selected_items VALUES (?)', (iid,))
                if item['kind'] == 'directory':
                    prefix = item['path'] + '/'
                    self.db.execute('INSERT OR IGNORE INTO selected_items SELECT id FROM items WHERE group_id=? AND substr(path,1,?)=?', (item['group_id'], len(prefix), prefix))
            cursor = self.db.execute('SELECT i.* FROM items i JOIN selected_items s ON s.id=i.id ORDER BY i.group_id,i.path,i.id')
            for row in cursor: yield self._item(row)

    @contextmanager
    def open_item(self, item_id):
        with self._lock:
            item = self.get_item(item_id)
            if item['status'] != 'ready' or item['kind'] in {'directory', 'notice'}:
                raise ValueError('所选条目没有可恢复的文件内容')
            loc = item['_locator']
            if 'descriptor' in loc:
                stream = aio.VerifiedStream(io.BytesIO(loc['descriptor'].encode()), item['size'], item['sha256'])
                try: yield stream; stream.finish()
                finally: stream.stream.close()
            elif 'file' in loc:
                self._bundle_path(loc['file'])
                aio.check_identity(loc['file'], loc['identity'])
                with Path(loc['file']).open('rb') as f:
                    stream = aio.VerifiedStream(f, item['size'], item['sha256'])
                    yield stream; stream.finish()
                aio.check_identity(loc['file'], loc['identity'])
            else:
                with self._member(loc['archive'], loc['member']) as f:
                    stream = aio.VerifiedStream(f, item['size'], item['sha256'])
                    yield stream; stream.finish()

    def close(self):
        with self._lock:
            if not self._closed: self.db.close(); self._closed = True

"""Selected file recovery into ordinary folders, without replacing existing data."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid

from .backup import Cancelled, check_cancel
from .importer import rename_new, target_locks
from .safeio import CHUNK, atomic_json, hash_file, remote_path, stable_bytes, within

SCHEMA = 'asm-file-restore-1'
RESERVE = 128 * CHUNK


class RestoreFailure(RuntimeError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def _linked(p):
    return p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())


def _directory(value):
    p = Path(value).expanduser()
    if remote_path(p) or not p.is_absolute() or '..' in p.parts:
        raise ValueError('请选择本机专用绝对目录，不使用网络路径或上级跳转')
    if any(_linked(x) for x in [p, *p.parents]):
        raise ValueError('目录不能经过符号链接或目录联接')
    p = p.resolve()
    if len(p.parts) < (2 if os.name == 'nt' else 3) or p == Path.home().resolve():
        raise ValueError('请选择专用恢复目录，不能选择磁盘根目录或用户目录')
    if p.exists() and not p.is_dir():
        raise ValueError('所选位置不是目录')
    return p


def _overlap(a, b):
    return within(a, b) or within(b, a)


def _locations(catalog, target, backup):
    target, backup = _directory(target), _directory(backup)
    cache = Path(catalog.cache).resolve()
    root = Path(getattr(catalog, 'root', getattr(catalog, 'bundle_root', cache))).resolve()
    if _overlap(target, backup) or any(_overlap(target, p) or _overlap(backup, p) for p in [root, cache]):
        raise ValueError('恢复目标、目标备份、源备份与工具缓存必须分开，不能互相包含')
    home = Path.home()
    live = [home/x for x in ['.claude', '.codex', '.dsh']]
    for base in [home/'Library/Application Support', Path(os.environ.get('APPDATA', home/'AppData/Roaming')), Path(os.environ.get('LOCALAPPDATA', home/'AppData/Local'))]:
        live += [base/'Claude', base/'Codex']
        if base.is_dir():live += list(base.glob('Claude-*'))
    if any(within(target, p) or target == p.resolve() for p in live):
        raise ValueError('应用资料只能提取到独立文件夹；原生会话请使用会话导入向导')
    return target, backup, cache


def _component(value):
    # Escape '~' as well so encoding is reversible and injective.
    trailing = len(value.rstrip('. '))
    out = ''.join('~'+format(ord(c),'04X') if c in '~<>:"/\\|?*' or ord(c)<32 or i>=trailing else c for i,c in enumerate(value))
    if out.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        out = '~005F'+out
    if len(out.encode('utf-8')) > 200:
        suffix = '~'+hashlib.sha256(value.encode()).hexdigest()[:20]
        while len(out.encode('utf-8')) > 170:out = out[:-1]
        out += suffix
    return out or '~0000'


def _relative(item):
    raw = str(item.get('path', ''))
    p = PurePosixPath(raw)
    if p.is_absolute() or '..' in p.parts or re.match(r'^[A-Za-z]:',raw):
        raise ValueError('备份中的相对路径不安全')
    parts = [_component(x) for x in p.parts if x not in {'','.'}]
    if not parts and item.get('kind') != 'directory':raise ValueError('文件缺少名称')
    if item.get('kind') == 'symlink':
        parts[-1] += '.symlink.json'
    return Path('资料-'+_component(str(item['group_id']))[:80], *parts)


def _canonical(path):
    return '/'.join(unicodedata.normalize('NFC',p).casefold() for p in PurePosixPath(str(path).replace(os.sep,'/')).parts)


def _connect(path):
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    return c


def _parent_space(p):
    while not p.exists():p=p.parent
    return shutil.disk_usage(p).free, p.stat().st_dev


def _snapshot(root, cancel):
    if not root.exists():return
    _directory(root)
    for base, dirs, files in os.walk(root,followlinks=False):
        for name in sorted(dirs+files):
            check_cancel(cancel)
            p=Path(base)/name;s=p.lstat()
            if _linked(p) or not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
                raise ValueError('目标含链接或特殊文件；请选择独立空目录')
            kind='directory' if stat.S_ISDIR(s.st_mode) else 'file'
            digest=hash_file(p) if kind=='file' else None
            after=p.lstat()
            if (s.st_ino,s.st_size,s.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
                raise ValueError('目标资料仍在变化，请停止目标写入后重新预览')
            yield (p.relative_to(root).as_posix(),kind,s.st_size if kind=='file' else 0,digest,stat.S_IMODE(s.st_mode))


def _before_same(db, target, existed, cancel):
    if target.exists()!=existed:raise ValueError('预览后目标目录发生变化，请重新预览')
    count=0
    for row in _snapshot(target,cancel):
        saved=db.execute('SELECT path,kind,size,sha,mode FROM before_files WHERE path=?',(row[0],)).fetchone()
        if saved is None or tuple(saved)!=row:raise ValueError('预览后目标内容发生变化，请重新预览')
        count+=1
    if count!=db.execute('SELECT count(*) FROM before_files').fetchone()[0]:
        raise ValueError('预览后目标内容发生变化，请重新预览')


def build_file_plan(catalog, item_ids, group_ids, target, backup_root, progress=lambda *_:None, cancel=None):
    cancel=cancel or threading.Event()
    target,backup,cache=_locations(catalog,target,backup_root)
    plans=cache/'file-restore-plans';plans.mkdir(parents=True,exist_ok=True,mode=0o700)
    token=uuid.uuid4().hex;path=plans/(token+'.sqlite');db=_connect(path)
    plan={'id':token,'database':str(path),'target':str(target),'backupRoot':str(backup),'new':0,'skip':0,'blocked':0,'bytes':0,'selected':0,'createdAt':time.time(),'targetExisted':target.exists(),'cache':str(cache)}
    try:
        db.executescript('CREATE TABLE meta(value TEXT); CREATE TABLE before_files(path TEXT PRIMARY KEY,kind TEXT,size INTEGER,sha TEXT,mode INTEGER,canonical TEXT); CREATE INDEX before_canonical ON before_files(canonical); CREATE TABLE destinations(path TEXT PRIMARY KEY,kind TEXT); CREATE TABLE rows(id TEXT PRIMARY KEY,path TEXT,destination TEXT,canonical TEXT,status TEXT,reason TEXT,size INTEGER,item TEXT,phase TEXT DEFAULT "pending",digest TEXT);')
        progress('检查目标资料并准备完整备份清单',0)
        for row in _snapshot(target,cancel):
            db.execute('INSERT INTO before_files VALUES(?,?,?,?,?,?)',(*row,_canonical(row[0])))
        db.commit()
        for item in catalog.iter_items(item_ids=item_ids,group_ids=group_ids):
            check_cancel(cancel);item=dict(item)
            if db.execute('SELECT 1 FROM rows WHERE id=?',(str(item['id']),)).fetchone():continue
            status='new';reason='';relative=None;destination='';canonical=''
            kind=item.get('kind','file')
            try:
                if item.get('status','ready')!='ready' or kind=='notice':raise ValueError(item.get('detail') or '此项未备份或当前不能恢复')
                if kind not in {'file','directory','symlink'}:raise ValueError('不支持的文件类型')
                relative=_relative(item);destination=str(target/relative);canonical=_canonical(relative.as_posix())
                for prefix in [*relative.parents,relative]:
                    if str(prefix)=='.':continue
                    key=_canonical(prefix.as_posix());is_leaf=prefix==relative
                    wanted='directory' if not is_leaf or kind=='directory' else 'file'
                    known=db.execute('SELECT kind FROM destinations WHERE path=?',(key,)).fetchone()
                    if known and (known[0]!='directory' or wanted!='directory'):
                        raise ValueError('所选项存在同名、大小写或父子路径冲突')
                    old=db.execute('SELECT path,kind FROM before_files WHERE canonical=?',(key,)).fetchone()
                    if old:
                        if old['path']!=prefix.as_posix() and not is_leaf:raise ValueError('现有目录大小写或Unicode名称冲突')
                        if not is_leaf and old['kind']!='directory':raise ValueError('现有父路径是文件')
                        if is_leaf:
                            if wanted=='file' and old['kind']=='directory':raise ValueError('现有同名目录阻止文件恢复')
                            if wanted=='directory' and old['kind']!='directory':raise ValueError('现有同名文件阻止目录恢复')
                            status='skip';reason='现有资料保留，不覆盖'
                if status=='new':
                    for prefix in [*reversed(relative.parents),relative]:
                        if str(prefix)=='.':continue
                        db.execute('INSERT OR IGNORE INTO destinations VALUES(?,?)',(_canonical(prefix.as_posix()),'directory' if prefix!=relative or kind=='directory' else 'file'))
            except (ValueError,OSError) as error:status='blocked';reason=str(error)
            size=int(item.get('size') or 0)
            if size<0:status='blocked';reason='文件大小不合法';size=0
            db.execute('INSERT INTO rows(id,path,destination,canonical,status,reason,size,item) VALUES(?,?,?,?,?,?,?,?)',(str(item['id']),str(relative or item.get('path','')),destination,canonical,status,reason,size,json.dumps(item,ensure_ascii=False,default=str)))
            plan[status]+=1;plan['selected']+=1
            if status=='new' and kind!='directory':plan['bytes']+=size
            if plan['selected']%256==0:
                db.commit();progress('生成逐项恢复与冲突预览',0)
                if _parent_space(path.parent)[0]<RESERVE:raise ValueError('工具缓存空间不足，请更换缓存目录')
        if not plan['selected']:raise ValueError('请先勾选至少一项资料')
        db.execute('INSERT INTO meta VALUES(?)',(json.dumps(plan),));db.commit()
        progress('恢复预览已生成',100)
        return plan
    finally:db.close()


def plan_rows(plan,offset=0,limit=100):
    with closing(_connect(plan['database'])) as db:
        return [dict(r) for r in db.execute('SELECT id,path,destination,status,reason,size,phase FROM rows ORDER BY rowid LIMIT ? OFFSET ?',(max(1,min(int(limit),1000)),max(0,int(offset))))]


def _guard_parent(destination, root):
    if not within(destination,root):raise ValueError('目标路径越出恢复目录')
    for p in [destination,*destination.parents]:
        if _linked(p):raise ValueError('目标路径出现链接，恢复已停止')


def _mkdir(path, root, report, db):
    missing=[];p=path
    while not p.exists():missing.append(p);p=p.parent
    for p in reversed(missing):
        _guard_parent(p,root)
        if _parent_space(p.parent)[0]<RESERVE:raise ValueError('恢复磁盘余量不足，未创建新目录')
        p.mkdir(mode=0o700)
        db.execute('INSERT OR IGNORE INTO made_dirs VALUES(?)',(str(p),));db.commit()


def _copy_stream(source, target, expected_size, expected_sha, cancel, progress):
    digest=hashlib.sha256();count=0;check_at=0
    if _parent_space(target.parent)[0]<expected_size+RESERVE:raise ValueError('磁盘空间不足，未写入当前副本')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    owned=os.fstat(fd)
    try:
        with os.fdopen(fd,'wb') as dst:
            for block in iter(lambda:source.read(CHUNK),b''):
                check_cancel(cancel);count+=len(block)
                if count>expected_size:raise ValueError('数据长度超出清单，未发布文件')
                if count-check_at>=64*CHUNK:
                    if _parent_space(target.parent)[0]<RESERVE:raise ValueError('恢复磁盘余量不足，未发布当前文件')
                    progress('校验并提取所选文件',0);check_at=count
                dst.write(block);digest.update(block)
            dst.flush();os.fsync(dst.fileno())
        if count!=expected_size or expected_sha and digest.hexdigest()!=expected_sha:
            raise ValueError('源数据内容校验失败，未发布文件')
        if hash_file(target)!=digest.hexdigest():raise ValueError('写入后的内容回读不一致')
        return digest.hexdigest()
    except BaseException:
        if target.exists() and not _linked(target):
            current=target.lstat()
            if (current.st_dev,current.st_ino)==(owned.st_dev,owned.st_ino):target.unlink()
        raise


def _backup_target(db, plan, output, cancel, progress):
    target=Path(plan['target']);root=output/'target-backup';root.mkdir(mode=0o700)
    for row in db.execute('SELECT * FROM before_files ORDER BY path'):
        check_cancel(cancel);p=root/row['path'];p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if row['kind']=='directory':p.mkdir(exist_ok=True,mode=0o700);continue
        original=target/row['path'];_guard_parent(original,target)
        before=original.stat()
        with original.open('rb') as src:_copy_stream(src,p,row['size'],row['sha'],cancel,progress)
        after=original.stat()
        if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('目标在备份时发生变化')
    _before_same(db,target,plan['targetExisted'],cancel)
    atomic_json(output/'target-backup.json',{'schema':SCHEMA,'target':str(target),'targetExisted':plan['targetExisted'],'index':'backup-index.sqlite','backup':'target-backup','verified':True,'encrypted':False})
    clone=sqlite3.connect(output/'backup-index.sqlite')
    try:db.backup(clone)
    finally:clone.close()
    return root


def _load_report(value):
    path=Path(value['journal'] if isinstance(value,dict) else value)
    if path.is_symlink():raise ValueError('恢复日志不能是链接')
    report=json.loads(stable_bytes(path,4*CHUNK))
    if report.get('schema')!=SCHEMA or Path(report['journal']).resolve()!=path.resolve() or Path(report['output']).resolve()!=path.parent.resolve():raise ValueError('不是受支持的文件恢复记录')
    if isinstance(value,dict):
        for key in ['id','planDatabase','target','output','journal','backupIndexSha256']:
            if key in value and value[key]!=report.get(key):raise ValueError('恢复日志与本机已保存记录不一致，未执行文件操作')
    index=Path(report['planDatabase'])
    if not index.is_file() or _linked(index):raise ValueError('本机恢复计划缺失或为链接，不能继续操作')
    return path,report


def execute_file_restore(catalog,plan,progress=lambda *_:None,cancel=None):
    cancel=cancel or threading.Event()
    target,backup,cache=_locations(catalog,plan['target'],plan['backupRoot'])
    db=_connect(plan['database']);report=None
    try:
        saved=json.loads(db.execute('SELECT value FROM meta').fetchone()[0])
        if saved!=plan:raise ValueError('恢复计划已变化，请重新预览')
        db.executescript('CREATE TABLE IF NOT EXISTS made_dirs(path TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS run(value TEXT);')
        with target_locks({'files':str(target)},cache):
            previous=db.execute('SELECT value FROM run').fetchone()
            if previous:
                report=json.loads(previous[0]);checked=check_file_restore(report,progress)
                if checked['status']=='complete':return checked
                raise RestoreFailure('此计划已执行；请检查恢复记录或重新预览，现有文件不会覆盖',checked)
            _before_same(db,target,plan['targetExisted'],cancel)
            needed=db.execute('SELECT coalesce(sum(size),0) FROM before_files WHERE kind="file"').fetchone()[0]
            free_target,device_target=_parent_space(target);free_backup,device_backup=_parent_space(backup)
            overhead=plan['selected']*4096
            if free_target<plan['bytes']+overhead+RESERVE or free_backup<needed+RESERVE or device_target==device_backup and free_target<plan['bytes']+needed+overhead+RESERVE:
                raise ValueError('恢复或目标完整备份空间不足，未修改目标')
            backup.mkdir(parents=True,exist_ok=True,mode=0o700)
            output=backup/('文件恢复-'+time.strftime('%Y%m%d-%H%M%S')+'-'+plan['id'][:10]);output.mkdir(mode=0o700)
            report={'schema':SCHEMA,'id':plan['id'],'status':'backing-up','output':str(output),'journal':str(output/'report.json'),'planDatabase':plan['database'],'target':str(target),'backup':None,'backupVerified':False,'restored':0,'skipped':plan['skip'],'blocked':plan['blocked'],'bytes':0,'readbackVerified':False,'sourceModified':False,'originalTargetFilesOverwritten':False,'nativeClientRestored':False,'startedAt':time.time()}
            db.execute('INSERT INTO run VALUES(?)',(json.dumps(report),));db.commit();atomic_json(Path(report['journal']),report)
            try:
                stage=part=None;digest=None
                root=_backup_target(db,plan,output,cancel,progress)
                report.update(backup=str(root),backupVerified=True,backupIndexSha256=hash_file(output/'backup-index.sqlite'),status='restoring');atomic_json(Path(report['journal']),report)
                _mkdir(target,target,report,db)
                stage=target/('.asm-stage-'+plan['id']);stage.mkdir(mode=0o700)
                report['stage']=str(stage);atomic_json(Path(report['journal']),report)
                total=max(1,plan['new'])
                for row in db.execute('SELECT * FROM rows WHERE status="new" ORDER BY rowid'):
                    check_cancel(cancel);item=json.loads(row['item']);current=dict(catalog.get_item(row['id']))
                    if any(current.get(k)!=item.get(k) for k in ['group_id','path','kind','size','sha256']):raise ValueError('源条目在预览后变化，请重新预览')
                    dest=Path(row['destination']);_guard_parent(dest,target)
                    if dest!=target/_relative(item):raise ValueError('计划中的目标路径与所选源条目不一致')
                    if dest.exists():
                        db.execute('UPDATE rows SET phase="skipped-race" WHERE id=?',(row['id'],));db.commit();report['skipped']+=1;continue
                    if item.get('kind')=='directory':
                        _mkdir(dest,target,report,db);db.execute('UPDATE rows SET phase="directory" WHERE id=?',(row['id'],));db.commit();continue
                    _mkdir(dest.parent,target,report,db)
                    part=stage/(uuid.uuid4().hex+'.part')
                    with catalog.open_item(row['id']) as src:digest=_copy_stream(src,part,row['size'],item.get('sha256'),cancel,progress)
                    db.execute('UPDATE rows SET phase="publishing",digest=? WHERE id=?',(digest,row['id']));db.commit()
                    _guard_parent(dest,target)
                    try:rename_new(part,dest)
                    except FileExistsError:
                        part.unlink();db.execute('UPDATE rows SET phase="skipped-race" WHERE id=?',(row['id'],));db.commit();report['skipped']+=1;continue
                    if hash_file(dest)!=digest:raise ValueError('已发布文件回读不一致，请检查恢复记录')
                    db.execute('UPDATE rows SET phase="verified" WHERE id=?',(row['id'],));db.commit()
                    report['restored']+=1;report['bytes']+=row['size']
                    progress('逐项恢复并回读校验',min(99,int(report['restored']*100/total)))
                if not list(stage.iterdir()):stage.rmdir()
                report.update(status='complete',readbackVerified=True,finishedAt=time.time());atomic_json(Path(report['journal']),report)
                db.execute('UPDATE run SET value=?',(json.dumps(report),));db.commit()
                progress('所选文件副本已恢复，未覆盖原资料',100)
                return report
            except Exception as error:
                if part is not None and part.is_file() and not _linked(part) and digest and hash_file(part)==digest:part.unlink()
                if stage is not None and stage.is_dir() and not _linked(stage) and not list(stage.iterdir()):stage.rmdir()
                report.update(status='cancelled' if isinstance(error,Cancelled) else 'needs-attention',error=type(error).__name__+': '+str(error),finishedAt=time.time())
                atomic_json(Path(report['journal']),report)
                db.execute('UPDATE run SET value=?',(json.dumps(report),));db.commit()
                raise RestoreFailure(str(error),report) from error
    finally:db.close()


def check_file_restore(value,progress=lambda *_:None):
    path,report=_load_report(value);db=_connect(report['planDatabase'])
    try:
        target=_directory(report['target']);verified=0;pending=0;restored_bytes=0
        if report['status']=='rolled-back':return report
        backup_ok=bool(report.get('backupVerified'))
        if backup_ok:
            root=path.parent/'target-backup';index=path.parent/'backup-index.sqlite'
            backup_ok=root.is_dir() and not _linked(root) and index.is_file() and not _linked(index) and hash_file(index)==report.get('backupIndexSha256')
            if backup_ok:
                for r in db.execute('SELECT path,kind,sha FROM before_files'):
                    p=root/r['path'];_guard_parent(p,root)
                    if r['kind']=='directory':backup_ok=backup_ok and p.is_dir()
                    else:backup_ok=backup_ok and p.is_file() and hash_file(p)==r['sha']
                    if not backup_ok:break
        for row in db.execute('SELECT destination,digest,phase,size FROM rows WHERE status="new"'):
            if row['phase']=='skipped-race':continue
            dest=Path(row['destination']);_guard_parent(dest,target)
            if row['phase']=='directory':
                if not dest.is_dir():pending+=1
                continue
            if row['digest'] and dest.is_file() and hash_file(dest)==row['digest']:verified+=1;restored_bytes+=row['size']
            else:pending+=1
        report.update(checkedAt=time.time(),checkedPublishedFiles=verified,pendingFiles=pending,backupRecheckVerified=backup_ok,restored=verified,bytes=restored_bytes)
        if report['status']!='rolled-back':report['status']='complete' if pending==0 and backup_ok else 'needs-attention'
        report['readbackVerified']=pending==0 and backup_ok
        atomic_json(path,report);return report
    finally:db.close()


def rollback_file_restore(value,progress=lambda *_:None):
    if not isinstance(value,dict):raise ValueError('请从本客户端保存的恢复记录选择撤销，不能用外部日志发起删除')
    path,report=_load_report(value);db=_connect(report['planDatabase']);protected=[];removed=0
    try:
        target=_directory(report['target'])
        with target_locks({'files':str(target)},Path(json.loads(db.execute('SELECT value FROM meta').fetchone()[0])['cache'])):
            for row in db.execute('SELECT id,destination,digest,phase,item FROM rows WHERE phase IN ("publishing","verified") ORDER BY rowid DESC'):
                dest=Path(row['destination']);_guard_parent(dest,target)
                if not dest.exists():continue
                if dest!=target/_relative(json.loads(row['item'])) or db.execute('SELECT 1 FROM before_files WHERE canonical=?',(_canonical(dest.relative_to(target).as_posix()),)).fetchone():
                    protected.append(str(dest));continue
                if not dest.is_file() or not row['digest'] or hash_file(dest)!=row['digest']:
                    protected.append(str(dest));continue
                dest.unlink();removed+=1
                db.execute('UPDATE rows SET phase="rolled-back" WHERE id=?',(row['id'],));db.commit();progress('仅撤销本次创建且未被修改的文件',0)
            for row in db.execute('SELECT path FROM made_dirs ORDER BY length(path) DESC'):
                p=Path(row[0]);_guard_parent(p,target)
                if p.is_dir() and not list(p.iterdir()):p.rmdir()
            report.update(status='needs-attention' if protected else 'rolled-back',rollbackVerified=not protected,rollbackRemovedFiles=removed,protectedUserChanges=protected,finishedAt=time.time())
            atomic_json(path,report);return report
    finally:db.close()

"""Worktree discovery and explicit, per-project transfer choices."""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

from .migration_policy import forbid_client_storage
from .safeio import hash_file, owned_file, remote_path, within

OPTIONS = {"link", "files", "git"}
EXCLUDED = {".git", ".claude", ".codex", ".dsh", "node_modules", ".venv", "venv",
            "__pycache__", ".next", ".nuxt", ".turbo", ".pytest_cache"}
KEY_SUFFIXES = {".key", ".asmkey", ".pem", ".p12", ".pfx", ".keystore"}
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


def _git(cwd, *args, checkout=False, stdin=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if checkout:
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
    process = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args], cwd=cwd, env=env,
        input=stdin, capture_output=True, timeout=45, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return process.stdout.decode("utf-8", "surrogateescape").rstrip("\r\n")


def git_remote(value):
    if not value or any(ord(ch) < 33 for ch in value):
        return ""
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./~%-]+", value):
        return value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or not parsed.path:
        return ""
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    user = "git@" if parsed.scheme == "ssh" and parsed.username == "git" else ""
    return urlunsplit((parsed.scheme, user + host + (":" + str(port) if port else ""), parsed.path, "", ""))


def _local_directory(value, home):
    if not value or remote_path(value):
        return None
    path = Path(value)
    if not path.is_absolute() or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        return None
    if not path.is_dir():
        return None
    path = path.resolve()
    if path == Path(path.anchor) or within(home, path):
        return None
    return path


@dataclass
class WorkProject:
    id: str
    root: Path
    members: dict = field(default_factory=dict)
    providers: set = field(default_factory=set)
    commit: str = ""
    remote: str = ""
    branch: str = ""
    changes: dict = field(default_factory=dict)
    remote_status: str = "unknown"
    submodules: dict = field(default_factory=dict)
    lfs_files: dict = field(default_factory=dict)
    dependencies_checked: bool = False

    def manifest(self, mode, include_changes):
        return {"id": self.id, "root": str(self.root), "title": self.root.name, "mode": mode,
                "members": self.members, "providers": sorted(self.providers),
                "git": {"commit": self.commit, "remote": self.remote, "branch": self.branch,
                        "remoteStatus": self.remote_status, "changeFormat": 1,
                        "submodules": self.submodules, "lfsFiles": self.lfs_files,
                        "dependenciesChecked": self.dependencies_checked} if mode == "git" else {},
                "changes": self.changes if mode == "git" and include_changes else {}}


def discover_projects(records, home, check=lambda: None):
    by_path = defaultdict(list)
    for record in records:
        if record.metadata.get("preview_only"):
            continue
        directory = _local_directory(record.cwd, home)
        if directory:
            by_path[directory].append(record)
    projects = {}
    for directory, session_list in by_path.items():
        check()
        root = directory
        try:
            git_root = Path(_git(directory, "rev-parse", "--show-toplevel")).resolve()
            if _local_directory(str(git_root), home) and within(directory, git_root):
                root = git_root
        except (OSError, subprocess.SubprocessError):
            pass
        if root not in projects:
            pid = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:16]
            projects[root] = WorkProject(pid, root)
            if (root / ".git").exists():
                _read_git(projects[root])
        project = projects[root]
        for record in session_list:
            project.members[record.uid] = directory.relative_to(root).as_posix()
            project.providers.add(record.provider)
    return sorted(projects.values(), key=lambda p: str(p.root).casefold())


def configured_projects(projects, choices, home, include_projects=False):
    grouped = {}
    for project in projects:
        options = (choices or {}).get(project.id, ("files" if include_projects else "link", False))
        mode, carry, *overrides = options
        manual = overrides[0] if overrides else ""
        if manual:
            directory = _local_directory(manual, home)
            if not directory or project.commit or not within(project.root, directory):
                raise ValueError("手选项目目录应包含所选的非 Git 工作目录")
            members = {uid: (project.root / sub).relative_to(directory).as_posix()
                       for uid, sub in project.members.items()}
            pid = hashlib.sha256(os.path.normcase(str(directory)).encode()).hexdigest()[:16]
            project = WorkProject(pid, directory, members, set(project.providers))
        if mode not in OPTIONS or mode == "git" and (not project.commit or not project.remote):
            raise ValueError("项目方式与当前目录状态不一致，请重新选择")
        if mode == "git" and isinstance(carry, (list, tuple, set)):
            if not set(carry) <= project.changes.keys():
                raise ValueError("已选择的 Git 工作文件发生变化，请重新预览")
            project.changes = {path: project.changes[path] for path in carry}
            carry = bool(carry)
        existing = grouped.get(project.id)
        if existing:
            if existing[1:] != (mode, carry):
                raise ValueError("该目录已被多个项目引用，请为它们选择一致的导出方式")
            existing[0].members.update(project.members)
            existing[0].providers.update(project.providers)
        else:
            grouped[project.id] = (project, mode, carry)
    return list(grouped.values())


def _read_git(project):
    try:
        commit = _git(project.root, "rev-parse", "HEAD")
        if not COMMIT.fullmatch(commit):
            return
        project.commit = commit
        project.branch = _git(project.root, "rev-parse", "--abbrev-ref", "HEAD")
        try:
            _inspect_git_dependencies(project)
            project.dependencies_checked = True
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
        changed = _git(project.root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
        for line in changed.split("\0"):
            if len(line) < 4:
                continue
            flags, name = line[:2], line[3:]
            if flags == "!!" or not safe_work_path(name) or name in project.submodules:
                continue
            if flags == "??":
                project.changes[name] = "untracked"
            elif flags.startswith("A"):
                if (project.root / name).is_file():
                    project.changes[name] = "added"
            else:
                project.changes[name] = "deleted" if "D" in flags and not (project.root / name).exists() else "modified"
        project.remote = git_remote(_git(project.root, "remote", "get-url", "origin"))
        tracking = _git(project.root, "for-each-ref", "--contains", commit, "--format=%(refname)", "refs/remotes/origin")
        project.remote_status = "seen-in-tracking-ref" if tracking else "unverified-or-local-only"
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass


def _inspect_git_dependencies(project):
    paths = []
    for line in _git(project.root, "ls-tree", "-r", "-z", project.commit).split("\0"):
        if not line:
            continue
        metadata, name = line.split("\t", 1)
        mode, _, oid = metadata.split()
        if not safe_work_path(name):
            continue
        if mode == "160000":
            local = project.root / name
            checkout = local / ".git"
            project.submodules[name] = {"commit": oid, "localCheckout": owned_file(checkout, project.root) or (
                not any(p.is_symlink() or p.is_junction() for p in (checkout, *checkout.parents)) and checkout.is_dir()
            )}
        elif mode in {"100644", "100755"}:
            paths.append(name)
    if not paths:
        return
    data = "\0".join(paths).encode("utf-8", "surrogateescape") + b"\0"
    attributes = iter(_git(project.root, "check-attr", "-z", "--stdin", "filter", stdin=data).split("\0"))
    for name, _, value in zip(attributes, attributes, attributes):
        if value != "lfs":
            continue
        path = project.root / name
        state = "missing"
        if owned_file(path, project.root):
            with path.open("rb") as stream:
                state = "pointer" if stream.readline(128).rstrip(b"\r\n") == LFS_POINTER else "content"
        project.lfs_files[name] = state


def safe_work_path(name):
    parts = PurePosixPath(name).parts
    if not parts or any(x in {"..", ".", "/"} or x.casefold() in EXCLUDED or ":" in x or "\\" in x
                        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in x) for x in parts):
        return False
    p = PurePosixPath(name)
    name = p.name.casefold()
    return not (name.startswith((".env", ".credentials")) or name in {"auth.json", "credentials.json"}
                or p.suffix.casefold() in KEY_SUFFIXES)


def project_files(project, mode, include_changes, excluded, check):
    root = project.root

    def safe_file(p, relative):
        return (safe_work_path(relative) and not any(p == x or within(p, x) for x in excluded)
                and all(not x.is_symlink() and not x.is_junction() for x in (p, *p.parents))
                and stat.S_ISREG(p.stat().st_mode))

    if mode == "git" and include_changes:
        for relative, change in project.changes.items():
            check()
            p = root.joinpath(*PurePosixPath(relative).parts)
            if change != "deleted" and p.is_file() and safe_file(p, relative):
                yield p, relative
    if mode != "files":
        return
    for base, dirs, files in os.walk(root, followlinks=False):
        check()
        folder = Path(base)
        dirs[:] = [d for d in dirs if d.casefold() not in EXCLUDED and not (folder / d).is_symlink()
                   and not (folder / d).is_junction() and not (folder / d / ".git").exists()
                   and not any(within(folder / d, x) for x in excluded)]
        for name in files:
            path = folder / name
            relative = path.relative_to(root).as_posix()
            if safe_file(path, relative):
                yield path, relative


def _fetch_git(destination, remote, commit):
    object_format = "sha256" if len(commit) == 64 else "sha1"
    _git(destination, "init", "--quiet", "--template=", "--object-format=" + object_format)
    _git(destination, "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
         "-c", "protocol.ssh.allow=always", "fetch", "--no-tags", "--", remote, commit)
    if _git(destination, "rev-parse", "FETCH_HEAD") != commit:
        raise ValueError("Git 获取的提交与迁移包记录不一致")
    _git(destination, "-c", "core.hooksPath=" + str(destination / ".git" / "no-hooks"),
         "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
         "checkout", "--detach", "--quiet", commit, checkout=True)


@dataclass(frozen=True)
class GitChange:
    name: str
    action: str
    object_id: str = ""
    baseline_sha: str = ""
    conflict: str = ""


def _plan_git_changes(staged, project, package):
    view = ProjectView(package, project["id"])
    payload = {item["path"]: item["id"] for item in view.items.values()}
    if len(payload) != len(view.items):
        raise ValueError("迁移包存在重复的 Git 改动文件")
    seen, rows = set(), []
    legacy = not project.get("git", {}).get("changeFormat")
    for name, action in sorted(project.get("changes", {}).items()):
        action = action if isinstance(action, str) else ""
        relative = PurePosixPath(name)
        path = staged.joinpath(*relative.parts)
        key = name.casefold()
        reason = ""
        digest = ""
        if not safe_work_path(name) or relative.as_posix() != name or action not in {"added", "untracked", "modified", "deleted"}:
            reason = "文件路径或改动类型不受支持"
        elif key in seen:
            reason = "多个改动的文件名存在大小写冲突"
        elif any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            reason = "目标文件或父目录经过链接"
        elif any(p.exists() and not p.is_dir() for p in path.parents):
            reason = "父路径在基线中是文件"
        else:
            if legacy and action == "modified" and not path.exists():
                action = "added"
            if action in {"added", "untracked"}:
                if path.exists():
                    reason = "基线已存在同名文件或目录"
            elif not path.is_file():
                reason = "基线缺少需要修改或删除的文件"
            else:
                digest = hash_file(path)
            if action != "deleted" and name not in payload:
                reason = reason or "迁移包缺少这项改动的文件内容"
        seen.add(key)
        rows.append(GitChange(name, action, payload.get(name, ""), digest, reason))
    return tuple(rows)


@dataclass
class GitRestore:
    scratch: tempfile.TemporaryDirectory
    destination: Path
    commit: str
    project: dict
    package: object
    changes: tuple[GitChange, ...]

    @property
    def staged(self):
        return Path(self.scratch.name) / "checkout"

    def close(self):
        self.scratch.cleanup()

    def publish(self):
        from .file_restore import _directory
        from .importer import rename_new
        destination = _directory(self.destination)
        forbid_client_storage(destination)
        if destination != self.destination or destination.exists():
            raise ValueError("预览后目标目录已发生变化，请重新选择")
        if any(change.conflict for change in self.changes):
            raise ValueError("本地改动与指定提交存在冲突，请复核预览或关联已有项目")
        if (_git(self.staged, "rev-parse", "HEAD") != self.commit
                or _plan_git_changes(self.staged, self.project, self.package) != self.changes):
            raise ValueError("Git 暂存目录在预览后变化，请重新预览")
        view = ProjectView(self.package, self.project["id"])
        for change in self.changes:
            path = self.staged / change.name
            if change.action == "deleted":
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                mode = "wb" if change.action == "modified" else "xb"
                with view.open_item(change.object_id) as source, path.open(mode) as target:
                    shutil.copyfileobj(source, target)
        rename_new(self.staged, destination)
        return destination


def prepare_git(project, package, destination):
    """Check out the exact base in a sibling directory and preview local changes."""
    if not destination.is_absolute() or destination.exists() or remote_path(destination):
        raise ValueError("Git 获取需要尚未使用的本机目标目录")
    if any(p.is_symlink() or p.is_junction() for p in destination.parents):
        raise ValueError("Git 目标不能经过链接目录")
    from .file_restore import _directory, _overlap
    destination = _directory(destination)
    forbid_client_storage(destination)
    if any(_overlap(destination, other) for other in (package.archive.parent, package.cache)):
        raise ValueError("Git 目标必须与备份目录和缓存分开")
    config = project.get("git", {})
    remote, commit = config.get("remote", ""), config.get("commit", "")
    if not remote or git_remote(remote) != remote or not COMMIT.fullmatch(commit):
        raise ValueError("Git 地址或提交无效，请关联已有项目")
    scratch = tempfile.TemporaryDirectory(prefix=".asm-git-", dir=destination.parent)
    staged = Path(scratch.name) / "checkout"
    try:
        staged.mkdir()
        _fetch_git(staged, remote, commit)
        if _git(staged, "rev-parse", "HEAD") != commit:
            raise ValueError("Git 暂存版本与迁移包记录不一致")
        reviewed = _plan_git_changes(staged, project, package)
        return GitRestore(scratch, destination, commit, project, package, reviewed)
    except BaseException as error:
        scratch.cleanup()
        if isinstance(error, (OSError, subprocess.SubprocessError)):
            raise ValueError("未能取得指定 Git 提交。请检查远端访问、登录和提交是否已推送。可改为关联已有目录") from error
        raise


def restore_git(project, package, destination):
    reviewed = prepare_git(project, package, destination)
    try:
        return reviewed.publish()
    finally:
        reviewed.close()


class ProjectView:
    """Read-only projection consumed by the existing file restoration engine."""
    def __init__(self, package, pid):
        self.package, self.cache, self.root = package, package.cache, package.archive.parent
        self.items = {}
        for name, entry in package.entries.items():
            if entry.get("projectId") != pid or entry.get("role") not in {"project-work-file", "project-local-change"}:
                continue
            relative = entry.get("projectPath", "")
            if not safe_work_path(relative):
                raise ValueError("迁移包项目路径不安全")
            self.items[name] = {"id": name, "group_id": pid, "path": relative, "kind": "file", "layout": "project",
                                "status": "ready", "size": entry["size"], "sha256": entry["sha256"]}

    def get_item(self, key):
        return self.items[key]

    def iter_items(self, item_ids, group_ids):
        yield from (self.items[key] for key in item_ids)

    @contextmanager
    def open_item(self, key):
        with self.package._extract(key, "payload").open("rb") as stream:
            yield stream

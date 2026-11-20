"""Register restored local Claude Code transcripts in the Windows Desktop sidebar."""

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import tempfile
import uuid
import zipfile

from ..importer import checked_directory, claude_project_origin, native_processes, rename_new
from ..safeio import atomic_json, hash_file, stable_bytes
from .planning import index_entries


DISPLAY_FIELDS = ("title", "titleSource", "createdAt", "lastActivityAt", "lastFocusedAt",
                  "model", "effort", "completedTurns", "lastAssistantUuid")
INDEX_NAME = re.compile(r"local_[A-Za-z0-9_-]+\.json\Z")
SUCCESS = {"imported", "existing"}


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def active_profile(home):
    """Select the single Desktop account and organization recorded as current."""
    container = home / "AppData" / "Local" / "Claude-Data"
    configured = []
    if not container.is_dir():
        return None
    for app in container.iterdir():
        sessions = app / "claude-code-sessions"
        config_path = app / "config.json"
        if not sessions.is_dir() or not config_path.is_file():
            continue
        config = json.loads(stable_bytes(config_path))
        account = config.get("lastKnownAccountUuid")
        if not valid_uuid(account):
            continue
        account_dir = sessions / account
        if not account_dir.is_dir():
            continue
        orgs = [path for path in account_dir.iterdir() if valid_uuid(path.name) and path.is_dir()]
        if len(orgs) == 1:
            candidate = checked_directory(orgs[0])
            if candidate.is_relative_to(container.resolve()):
                configured.append(candidate)
    return configured[0] if len(configured) == 1 else None


def existing_indexes(profile):
    before = {}
    sessions = set()
    for file in sorted(profile.glob("local_*.json")):
        if not INDEX_NAME.fullmatch(file.name) or file.resolve(strict=True) != file or file.is_symlink():
            raise ValueError("Claude Desktop 索引包含不安全的文件名或链接")
        data = stable_bytes(file)
        row = json.loads(data)
        if not isinstance(row, dict):
            raise ValueError("已有 Claude Desktop 索引不是对象")
        before[file.name] = data
        if row.get("cliSessionId"):
            sessions.add(row["cliSessionId"])
    return before, sessions


def make_payload(record, cwd, source, mappings=None):
    sid = record.session_id
    if not valid_uuid(sid):
        raise ValueError("Claude CLI 会话 ID 与桌面格式不兼容")
    payload = {key: source[key] for key in DISPLAY_FIELDS if key in source}
    local = source.get("sessionId", "")
    source_id = local.removeprefix("local_") if isinstance(local, str) else ""
    payload["sessionId"] = f"local_{source_id if valid_uuid(source_id) else sid}"
    payload.setdefault("title", record.title)
    time_ms = int(record.updated * 1000)
    payload.setdefault("createdAt", time_ms)
    payload.setdefault("lastActivityAt", time_ms)
    origin = claude_project_origin(cwd, source, mappings or {})
    payload.update(cliSessionId=sid, cwd=str(cwd), originCwd=str(origin), isArchived=record.archived,
                   permissionMode="default", adoptedFromOtherSurface=True, fromCliTranscript=True)
    return payload


def session_catalog(draft, archive, state, cli):
    outcomes = state["bulk"]["outcomes"]
    entries = index_entries(archive)
    sessions = {}
    totals = Counter()
    with zipfile.ZipFile(archive.archive) as package:
        for record in archive.scan().records:
            if record.provider != "claude" or outcomes.get(record.uid, {}).get("status") not in SUCCESS:
                continue
            if record.child:
                totals["children"] += 1
                continue
            totals["main"] += 1
            if not valid_uuid(record.session_id):
                raise ValueError("Claude CLI 会话 ID 与桌面格式不兼容")
            cwd = checked_directory(Path(draft["mappings"][record.cwd]))
            slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
            logfile = cli / "projects" / slug / (record.session_id + ".jsonl")
            if not cwd.is_dir() or not logfile.is_file() or logfile.resolve(strict=True) != logfile:
                raise ValueError(f"Windows 原生日志或项目目录缺失：{record.session_id}")
            decoded = [json.loads(package.read(entry["object"])) for entry in entries[record.uid]
                       if entry["role"] == "claude-desktop-index"]
            sources = sorted((row for row in decoded if row.get("cliSessionId") == record.session_id),
                             key=lambda row: row.get("lastActivityAt", 0))
            source = dict(sources[-1]) if sources else {}
            if source:
                prior = []
                for row in sources:
                    history = row.get("priorCliSessionIds") or []
                    if not isinstance(history, list):
                        raise ValueError("Claude Desktop 的旧历史链格式不兼容")
                    prior.extend(item for item in history if valid_uuid(item))
                source["priorCliSessionIds"] = list(dict.fromkeys(prior))
            sessions[record.session_id] = (record, cwd, source, logfile)
    return sessions, totals


def history_links(sessions):
    owner_groups = defaultdict(list)
    for sid, (_, _, source, _) in sessions.items():
        local_id = source.get("sessionId")
        if isinstance(local_id, str) and local_id.startswith("local_") and valid_uuid(local_id[6:]):
            owner_groups[local_id].append(sid)

    preferred = {}
    links = defaultdict(list)
    for group in owner_groups.values():
        owner = max(group, key=lambda sid: (sessions[sid][2].get("lastActivityAt", 0), sessions[sid][0].updated, sid))
        for sid in group:
            preferred[sid] = owner
        older = sorted((sid for sid in group if sid != owner), key=lambda sid: sessions[sid][0].updated)
        for sid in [*older, owner]:
            source = sessions[sid][2]
            prior = source.get("priorCliSessionIds") or []
            if not isinstance(prior, list):
                raise ValueError("Claude Desktop 的旧历史链格式不兼容")
            links[owner].extend(previous for previous in prior if valid_uuid(previous) and previous in sessions)
            if sid != owner:
                links[owner].append(sid)

    claims = defaultdict(set)
    for owner, history in links.items():
        for sid in history:
            if sid != owner and (not sessions[sid][2] or preferred.get(sid) == owner):
                claims[sid].add(owner)
    owners = {sid: next(iter(options)) for sid, options in claims.items() if len(options) == 1}
    ordered = {owner: list(dict.fromkeys(sid for sid in history if owners.get(sid) == owner))
               for owner, history in links.items()}
    return owners, {owner: history for owner, history in ordered.items() if history}


def build_indexes(draft, archive, state, cli, before, already):
    sessions, totals = session_catalog(draft, archive, state, cli)
    owners, prior = history_links(sessions)
    has_desktop_history = any(source for _, _, source, _ in sessions.values())
    linked = set(owners)
    for data in before.values():
        history = json.loads(data).get("priorCliSessionIds")
        if isinstance(history, list):
            linked.update(sid for sid in history if valid_uuid(sid))
    planned = {}
    for sid, (record, cwd, source, _) in sessions.items():
        if sid in linked:
            totals["linkedHistory"] += 1
            continue
        if sid in already:
            totals["alreadyListed"] += 1
            continue
        payload = make_payload(record, cwd, source, draft["mappings"])
        if has_desktop_history and not source:
            payload["isArchived"] = True
        if sid in prior:
            payload["priorCliSessionIds"] = prior[sid]
        local = payload["sessionId"] + ".json"
        if local in before or local in planned:
            payload["sessionId"] = "local_" + sid
            local = payload["sessionId"] + ".json"
            if local in before or local in planned:
                raise ValueError(f"Claude Desktop 索引 ID 冲突：{sid}")
        planned[local] = payload
        totals["fromMacDesktop" if source else "fromCliHistory"] += 1
        totals["archived" if payload["isArchived"] else "active"] += 1
    return planned, dict(totals)


def restore_sidebar(draft, archive, state, run):
    from ..migration_policy import CLAUDE_READ_ONLY
    raise ValueError(CLAUDE_READ_ONLY)

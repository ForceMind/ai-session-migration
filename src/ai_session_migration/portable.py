"""Build and check a transfer copy of an already captured session archive."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
from urllib.parse import quote
import zipfile

import zstandard

from .safeio import CHUNK, MAX_LINE
from .attachments import ATTACHMENT_KINDS, ATTACHMENT_PATH_FIELDS, local_file_uri_path

PORTABLE_SCHEMA = "ai-session-migration-gui-2"
PRIVACY_VERSION = 1
OFFSET_COLUMNS = {"rollout_byte_offset", "rollout_end_byte_offset", "next_rollout_byte_offset"}
NATIVE_TABLES = {
    "threads", "projects", "project_roots", "thread_attachments", "thread_spawn_edges",
    "thread_turns", "thread_items", "thread_history_projection_state", "thread_realtime_items",
}
STRUCTURED = {"item_json", "source", "metadata", "arguments"}
PATH_FIELDS = {"cwd", "path", "files", "root", "sourceRoot", "originalPath", "primary_file", "originCwd",
               "origin_cwd", "runtime_workspace_roots", "workspace_path", "rollout_path", "local_path",
               "project_root", "working_directory"} | (ATTACHMENT_PATH_FIELDS - {"url"})
PRIVATE_KEYS = {
    "creatoruserid", "creatoraccountid", "accountid", "accountuuid", "owneraccountuuid",
    "owneraccountid", "owneruserid", "ownerorganizationuuid", "organizationuuid",
    "organizationid", "orgid", "userid", "email", "accesstoken", "refreshtoken",
    "idtoken", "apikey", "openaikey", "openaiapikey", "authorization", "password",
    "clientsecret", "credentials", "authcookie", "sessiontoken", "bearertoken",
    "ownerorganizationid", "orguuid", "accountemail", "useremail", "owneremail", "creatoremail",
    "vetoedagainstaccountuuid",
}
SECRET = re.compile(rb"(?i)(?:sk-(?:proj-|ant-api[0-9]+-)?[a-z0-9_-]{24,}|gh[pousr]_[a-z0-9]{30,}|github_pat_[a-z0-9_]{40,}|-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----)")


def _private(key):
    return re.sub(r"[^a-z0-9]", "", key.casefold()) in PRIVATE_KEYS


class PortablePrivacyError(ValueError):
    def __init__(self, findings):
        self.findings = findings
        super().__init__(f"迁移包账号检查命中 {len(findings)} 项。请在结果页核对并选择排除范围。")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _path_key(value):
    return str(value or "").replace("\\", "/").rstrip("/")


class TransferPrivacy:
    def __init__(self, aliases=None):
        self.aliases = {}
        for path, alias in (aliases or {}).items():
            self.add_alias(path, alias)
        self.removed = set()
        self.private_fields = 0
        self.field_names = set()

    def add_alias(self, path, alias):
        key = path.lower() if re.match(r"^[a-zA-Z]:/", path) else path
        self.aliases[key] = alias

    def lookup_path(self, value):
        windows = bool(re.match(r"^[a-zA-Z]:/", value))
        prefix = value
        while prefix:
            key = prefix.lower() if windows else prefix
            if key in self.aliases:
                return self.aliases[key] + value[len(prefix):]
            prefix, _, _ = prefix.rpartition("/")
        return None

    def path(self, text):
        uri = local_file_uri_path(text)
        if uri is not None:
            alias = self.path(uri)
            return "file://" + quote(alias) if alias != uri else text
        value = _path_key(text)
        known = self.lookup_path(value)
        if known is not None:
            return known
        if self.aliases and value and not value.startswith(("/assets/", "/projects/", "/sources/", "/readings/", "/unmapped/")):
            if value.startswith("/") or re.match(r"^[a-zA-Z]:/", value):
                suffix = PurePosixPath(value).suffix
                return "/unmapped/" + _id(value) + (suffix if re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", suffix) else "")
        return text

    def clean(self, value, field="", attachment=False):
        if isinstance(value, dict):
            copy = {}
            attachment = attachment or str(value.get("type", "")).lower() in ATTACHMENT_KINDS
            for key, member in value.items():
                if _private(key):
                    self.private_fields += 1
                    self.field_names.add(re.sub(r"[^a-z0-9]", "", key.casefold()))
                    if isinstance(member, str) and len(member) >= 4:
                        self.removed.add(member.encode("utf-8"))
                    continue
                copy[key] = self.clean(member, key, attachment)
            return copy
        if isinstance(value, list):
            return [self.clean(v, field, attachment) for v in value]
        if isinstance(value, str):
            if field in STRUCTURED and value.lstrip().startswith(("{", "[")):
                try:
                    nested = json.loads(value)
                except ValueError:
                    pass
                else:
                    updated = self.clean(nested)
                    return value if updated == nested else _json(updated).decode("utf-8")
            if field in PATH_FIELDS or field.endswith("_path") or attachment and field == "url" and local_file_uri_path(value):
                return self.path(value)
            return value
        return value


def _id(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def privacy_reference(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_lines(stream):
    while line := stream.readline(MAX_LINE + 1):
        if len(line) > MAX_LINE or not line.endswith(b"\n"):
            raise ValueError("导出记录包含超长或未完成的行")
        yield line, json.loads(line)


def _reader(enter, stream, name):
    if name.endswith((".zst", ".zstd")):
        reader = enter(zstandard.ZstdDecompressor().stream_reader(stream, read_across_frames=True, closefd=False))
        return enter(io.BufferedReader(reader))
    return stream


def _records(source, entry):
    with ExitStack() as stack:
        stream = stack.enter_context(source.open(entry["object"]))
        reader = _reader(stack.enter_context, stream, entry.get("originalPath", ""))
        yield from _json_lines(reader)


def _export_log(source, output, entry, privacy, offsets, check):
    old_position = position = 0
    offsets[0] = 0
    with ExitStack() as stack:
        target = output
        if entry.get("originalPath", "").endswith((".zst", ".zstd")):
            target = stack.enter_context(zstandard.ZstdCompressor(write_checksum=True).stream_writer(output, closefd=False))
        for line, event in _records(source, entry):
            check()
            old_position += len(line)
            updated = privacy.clean(event)
            data = line if updated == event else _json(updated) + b"\n"
            target.write(data)
            position += len(data)
            offsets[old_position] = position


def _json_records(source, output, entry, privacy, offset_map, check):
    for _, event in _records(source, entry):
        check()
        if entry["role"] == "native-database-rows":
            if event.get("table") not in NATIVE_TABLES or "row" not in event:
                continue
            for field in OFFSET_COLUMNS & event["row"].keys():
                old = event["row"][field]
                if old is not None and offset_map:
                    # Keep the conversation available if the online rollout changed.
                    # The invalid offset is rejected by the native importer.
                    event["row"][field] = offset_map.get(old, -1)
        output.write(_json(privacy.clean(event)) + b"\n")


class _DigestWriter:
    def __init__(self, output):
        self.output, self.digest, self.size = output, hashlib.sha256(), 0

    def write(self, data):
        self.digest.update(data)
        self.size += len(data)
        return self.output.write(data)

    def flush(self):
        return self.output.flush()


def _filter_capture(manifest, catalog, excluded):
    skipped = set(excluded.get("sessions", ()))
    objects = set(excluded.get("objects", ()))
    projects = set(excluded.get("projects", ()))
    records = [r for r in catalog["records"] if privacy_reference(r["uid"]) not in skipped]
    if not records:
        raise ValueError("排除后没有可迁移的会话。已校验的原样加密保全仍可使用。")
    ids = {r["uid"] for r in records}
    projects = {p["id"] for p in catalog.get("projects", []) if privacy_reference(p["id"]) in projects}
    native = {}
    for record in records:
        if record["provider"] == "deepseek":
            native.setdefault(record["root"], set()).add(record["session_id"])
    entries, removed_changes = [], {}
    for entry in manifest["entries"]:
        owners = ({entry.get("sessionUid")} | set(entry.get("sessionUids", ()))) & ids
        if not owners or entry.get("projectId") in projects:
            continue
        if entry["object"] in objects:
            if entry["role"] not in {"project-work-file", "project-local-change"}:
                raise ValueError("会话日志和附件须按所属会话排除")
            removed_changes.setdefault(entry.get("projectId"), set()).add(entry.get("projectPath"))
            continue
        entries.append(dict(entry, sessionUid=entry.get("sessionUid") if entry.get("sessionUid") in owners else min(owners),
                            sessionUids=sorted(owners)))
    kept_projects = []
    for item in catalog.get("projects", []):
        if item["id"] in projects:
            continue
        members = {uid: rel for uid, rel in item["members"].items() if uid in ids}
        if members:
            changes = {name: kind for name, kind in item.get("changes", {}).items() if name not in removed_changes.get(item["id"], ())}
            kept_projects.append(dict(item, members=members, changes=changes))
    for record in records:
        metadata = record.get("metadata", {})
        if metadata.get("project_ref") in projects:
            for key in ("project_ref", "project_relative", "project_title"):
                metadata.pop(key, None)
    registries = []
    for registry in catalog.get("registries", []):
        session_ids = native.get(registry.get("sourceRoot"), set())
        if not session_ids:
            continue
        workspaces = registry.get("tables", {}).get("workspaces", {})
        for name, workspace in list(workspaces.items()):
            workspace["sessionIds"] = [sid for sid in workspace.get("sessionIds", []) if sid in session_ids]
            if not workspace["sessionIds"]:
                del workspaces[name]
        flags = registry.get("global", {})
        flags["workspaceIds"] = [name for name in flags.get("workspaceIds", []) if name in workspaces]
        for field in ("archivedSessionIds", "pinnedSessionIds"):
            flags[field] = [sid for sid in flags.get(field, []) if sid in session_ids]
        registries.append(registry)
    catalog.update(records=records, projects=kept_projects, registries=registries,
                   histories={uid: obj for uid, obj in catalog["histories"].items() if uid in ids})
    if "marks" in catalog.get("annotations", {}):
        catalog["annotations"]["marks"] = [uid for uid in catalog["annotations"]["marks"] if uid in ids]
    manifest["entries"] = entries


def build_portable(raw_archive, destination, check=lambda: None, progress=lambda *_: None, *, model=None, excluded=None,
                   generation_cancel=None):
    with zipfile.ZipFile(raw_archive) as source, zipfile.ZipFile(
        destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True
    ) as archive:
        manifest = json.loads(source.read("manifest.json"))
        catalog = json.loads(source.read("catalog.json"))
        previous_values = set()
        if excluded:
            # Markers can appear elsewhere even after their owning records are excluded.
            capture_privacy = TransferPrivacy()
            capture_privacy.clean(catalog)
            native_uids = {r["uid"] for r in catalog["records"] if r["provider"] in {"codex", "deepseek"}}
            for entry in manifest["entries"]:
                if entry["role"] in {"lossless-history-export", "native-database-rows"} or (
                    entry["role"] == "original-session" and entry.get("sessionUid") in native_uids
                ):
                    for _, event in _records(source, entry):
                        check()
                        capture_privacy.clean(event)
            previous_values = capture_privacy.removed
            _filter_capture(manifest, catalog, excluded)
        records = {r["uid"]: r for r in catalog["records"]}
        aliases = {}
        for record in records.values():
            root, cwd = (_path_key(record.get(field)) for field in ("root", "cwd"))
            if root and not root.startswith("/sources/"):
                aliases[root] = "/sources/" + record["provider"] + "-" + _id(root)
            if cwd and not cwd.startswith("/projects/"):
                aliases[cwd] = "/projects/" + _id(cwd)
        for project in catalog.get("projects", []):
            root = _path_key(project.get("root"))
            if root and not root.startswith("/projects/"):
                alias = "/projects/" + project["id"]
                aliases[root] = alias
                for record in records.values():
                    cwd = _path_key(record.get("cwd"))
                    if record.get("metadata", {}).get("project_ref") == project["id"] and cwd.startswith(root + "/"):
                        aliases[cwd] = alias + cwd[len(root):]
        privacy = TransferPrivacy(aliases)
        privacy.removed.update(previous_values)
        for entry in manifest["entries"]:
            path = _path_key(entry.get("originalPath"))
            if path and not path.startswith(("/sources/", "/projects/", "/assets/")) and privacy.lookup_path(path) is None:
                privacy.add_alias(path, "/assets/" + _id(path) + PurePosixPath(path).suffix)
        entries, offsets = [], {}
        ordered = sorted(manifest["entries"], key=lambda e: e["role"] != "original-session")
        for n, entry in enumerate(ordered):
            check()
            record = records.get(entry.get("sessionUid"), {})
            provider, role = record.get("provider"), entry["role"]
            if role in {"claude-desktop-index", "claude-reading", "handoff-draft"} or (provider == "claude" and role == "original-session"):
                continue
            if role in {"project-work-file", "project-local-change"}:
                entry["originalPath"] = "/projects/" + entry.get("projectId", "legacy") + "/" + entry.get(
                    "projectPath", PurePosixPath(_path_key(entry.get("originalPath"))).name)
            name = entry["object"]
            progress("生成可迁移副本", n * 100 // max(len(ordered), 1))
            with archive.open(name, "w", force_zip64=True) as target:
                writer = _DigestWriter(target)
                if role == "original-session" and provider in {"codex", "deepseek"}:
                    mapping = {}
                    _export_log(source, writer, entry, privacy, mapping, check)
                    if provider == "codex":
                        metadata = record.get("metadata", {})
                        preferred = metadata.get("native_thread_metadata", {}).get("rollout_path")
                        if entry.get("originalPath") == preferred or record["uid"] not in offsets:
                            offsets[record["uid"]] = mapping
                elif role in {"lossless-history-export", "native-database-rows"}:
                    _json_records(source, writer, entry, privacy, offsets.get(record.get("uid")), check)
                else:
                    with source.open(name) as data:
                        while block := data.read(CHUNK):
                            check()
                            writer.write(block)
            entries.append(dict(privacy.clean(entry), size=writer.size, sha256=writer.digest.hexdigest()))
        from .reading_transfer import write_readings
        # Account values in catalog metadata must be known before any model request.
        portable_catalog = privacy.clean(catalog)
        portable_catalog.update(schema=PORTABLE_SCHEMA,
                                readings=write_readings(source, archive, catalog, entries, manifest["entries"],
                                                        model, privacy, SECRET, check, progress, generation_cancel))
        for record in portable_catalog["records"]:
            record["issue"] = ""
            if record["provider"] == "claude":
                record["files"] = []
                record["metadata"].pop("indexes", None)
                record["metadata"].pop("desktop_ids", None)
            for key in ("state_database", "history_database"):
                record["metadata"].pop(key, None)
        raw = _json(portable_catalog)
        archive.writestr("catalog.json", raw)
        manifest.update(schema=PORTABLE_SCHEMA, purpose="migration", privacyVersion=PRIVACY_VERSION,
                        catalogSha256=hashlib.sha256(raw).hexdigest(), entries=entries, warnings=[])
        archive.writestr("manifest.json", _json(manifest))
    return check_portable(destination, privacy.removed, check)


def _metadata_parts(name, value):
    if name == "catalog.json":
        for record in value.get("records", []):
            yield {"role": "session-metadata", "sessionUid": record.get("uid")}, record
        for project in value.get("projects", []):
            yield {"role": "project-metadata", "projectId": project.get("id")}, project
        others = {k: v for k, v in value.items() if k not in {"records", "projects"}}
    else:
        for entry in value.get("entries", []):
            yield entry, entry
        others = {k: v for k, v in value.items() if k != "entries"}
    yield {"role": "metadata"}, others


def check_portable(path, removed=(), check=lambda: None):
    """Re-scan the published representation, including decompressed rollout logs."""
    state = TransferPrivacy()
    matches = []
    opaque = 0
    markers = tuple(removed)
    overlap = max([128, *(len(marker) for marker in markers)])

    def credential_match(data):
        return SECRET.search(data) or any(marker in data for marker in markers)

    def matching_field(value, path=()):
        known = {"type", "row", "message", "content", "text", "body", "item", "item_json", "payload", "metadata",
                 "title", "name", "originalPath", "projectPath", "cwd", "files", "native_thread_metadata"}
        if isinstance(value, dict):
            children = ((key if key in known else "field", child) for key, child in value.items())
        elif isinstance(value, list):
            children = ((str(index), child) for index, child in enumerate(value, 1))
        else:
            return ".".join(path[:12]) if isinstance(value, str) and credential_match(value.encode("utf-8")) else ""
        return next((name for key, child in children if (name := matching_field(child, (*path, key)))), "")

    def finding(entry, where, records, value=None, **position):
        role = entry.get("role", "metadata")
        allowed_roles = {"original-session", "native-database-rows", "lossless-history-export", "session-metadata",
                         "project-work-file", "project-local-change", "project-metadata", "claude-reading", "handoff-draft",
                         "attachment", "attachment-or-tool-result", "shared-attachment", "metadata"}
        role = role if role in allowed_roles else "metadata"
        name = entry.get("object", "")
        locator = name if re.fullmatch(r"objects/\d{8}", name) else (
            where if where in {"catalog.json", "manifest.json"} else "data-object"
        )
        uids = ({entry.get("sessionUid")} | set(entry.get("sessionUids", ()))) & records.keys()
        subject = [{"ref": privacy_reference(uid), "number": records[uid][0], "provider": records[uid][1]}
                   for uid in sorted(uids, key=lambda uid: records[uid][0])]
        item = dict(role=role, locator=locator, sessions=subject,
                    category="账号字段" if state.field_names else "已识别账号值或疑似凭据",
                    fields=sorted(state.field_names), **position)
        if value is not None and not state.field_names:
            field = matching_field(value)
            if field:
                item["field"] = field
        project = entry.get("projectId")
        if isinstance(project, str) and project:
            item["projectRef"] = privacy_reference(project)
        relative = entry.get("projectPath")
        if isinstance(relative, str) and relative and not credential_match(relative.encode("utf-8")):
            parts = PurePosixPath(relative).parts
            if parts and not PurePosixPath(relative).is_absolute() and ".." not in parts and not re.search(r"[\x00-\x1f:\\]", relative):
                item["projectPath"] = relative[:200]
        matches.append(item)
        state.removed.clear()
        state.private_fields = 0
        state.field_names.clear()

    with zipfile.ZipFile(path) as source:
        if source.getinfo("manifest.json").file_size > 512 * CHUNK:
            raise ValueError("迁移包元数据超过核查上限")
        manifest = json.loads(source.read("manifest.json"))
        if (manifest.get("schema"), manifest.get("purpose"), manifest.get("privacyVersion")) != (
            PORTABLE_SCHEMA, "migration", PRIVACY_VERSION
        ):
            raise ValueError("此包需要转换为经过账号检查的新版迁移包")
        if source.getinfo("catalog.json").file_size > 512 * CHUNK:
            raise ValueError("迁移包元数据超过核查上限")
        catalog = json.loads(source.read("catalog.json"))
        records = {r["uid"]: (i, r["provider"] if r["provider"] in {"codex", "deepseek", "claude"} else "unknown")
                   for i, r in enumerate(catalog["records"], 1)}
        for name, data in (("catalog.json", catalog), ("manifest.json", manifest)):
            for entry, value in _metadata_parts(name, data):
                check()
                state.clean(value)
                if state.private_fields or credential_match(_json(value)):
                    finding(entry, name, records, value)
        objects = manifest["entries"]
        for entry in objects:
            check()
            name, role = entry["object"], entry["role"]
            with ExitStack() as stack:
                stream = stack.enter_context(source.open(name))
                stream = _reader(stack.enter_context, stream, entry.get("originalPath", ""))
                if role in {"native-database-rows", "lossless-history-export", "original-session", "metadata"}:
                    if role == "metadata":
                        raw = stream.read(512 * CHUNK + 1)
                        if len(raw) > 512 * CHUNK:
                            raise ValueError("迁移包元数据超过核查上限")
                        values = ((raw, json.loads(raw)),)
                    else:
                        values = _json_lines(stream)
                    for position, (line, value) in enumerate(values, 1):
                        check()
                        state.clean(value)
                        if state.private_fields or credential_match(line):
                            finding(entry, name, records, value, line=position)
                            break
                else:
                    opaque += 1
                    tail = b""
                    while block := stream.read(CHUNK):
                        check()
                        value = tail + block
                        if credential_match(value):
                            finding(entry, name, records)
                            break
                        tail = value[-overlap:]
    if matches:
        raise PortablePrivacyError(matches)
    return {"verified": True, "opaqueObjects": opaque, "ruleVersion": PRIVACY_VERSION}

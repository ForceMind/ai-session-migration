"""Portable Claude reading files, source bindings and editable handoff drafts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

from .adapters import LocalLibrary, claude_messages
from .handoff_model import GenerationCancelled, GenerationError, compose_handoff
from .history import MAX_READING_BYTES, ReadingCatalog, load_prompts, parse_reading
from .models import Catalog, SessionRecord, identity


class ReadingSource(LocalLibrary):
    """An explicitly chosen numbered reading archive, kept separate from live Claude."""
    def __init__(self, config, cache, progress=lambda *_: None):
        prompts, fingerprints = load_prompts(config.get("preparedPath"), config.get("extraPromptsPath"))
        self.readings = ReadingCatalog(config["readingRoot"], prompts, fingerprints).scan(progress)
        super().__init__(Path.home(), cache)
        self.files_by_uid = {}
        for entry in self.readings.entries:
            if entry.kind not in {"main", "agent"}:
                continue
            uid = identity("claude", str(self.readings.root), entry.number)
            self.files_by_uid[uid] = entry
        self.catalog = Catalog([
            SessionRecord(uid=uid, provider="claude", session_id=entry.number, title=entry.title,
                          root=str(self.readings.root), child=entry.kind == "agent",
                          metadata={"project_title": entry.project, "reading_number": entry.number})
            for uid, entry in self.files_by_uid.items()
        ], [{"provider": "claude", "path": str(self.readings.root), "status": "阅读版", "count": len(self.files_by_uid)}])

    def scan(self, progress=lambda *_: None):
        return self.catalog

    def raw_events(self, record):
        messages, _ = self.readings.read(self.files_by_uid[record.uid])
        for message in messages:
            body = re.sub(r"^\d+  │  ", "", message.numbered_body(), flags=re.MULTILINE)
            role = "user" if message.role == "用户" else "assistant"
            yield {"type": role, "message": {"role": role, "content": body}}

    def original_files(self, record):
        entry = self.files_by_uid[record.uid]
        self.readings._path(entry)
        return [(entry.path, "claude-reading")]

    def draft(self, record, digest):
        item = self.readings.prompts.get(record.session_id)
        if item and not record.child:
            self.readings.check_unchanged(self.files_by_uid[record.uid], digest)
            return item.prompt
        return None


def reading_markdown(record, number, events, check):
    heading = "子代理记录" if record.get("child") else "Code 会话"
    parts, size, index = [f"# {heading} {number}\n\n"], 0, 0
    for event in events:
        check()
        for message in claude_messages(event):
            if message.kind != "message" or message.role not in {"user", "assistant"} or not message.text.strip():
                continue
            index += 1
            role = "用户" if message.role == "user" else "Claude"
            spaces = max((len(run) for run in re.findall(r"\x60+", message.text)), default=0)
            fence = chr(96) * max(3, spaces + 1)
            block = f"## {index} · {role}\n\n{fence}text\n{message.text}\n{fence}\n\n"
            size += len(block.encode("utf-8"))
            if size > MAX_READING_BYTES:
                return None
            parts.append(block)
    return "".join(parts).encode("utf-8") if index else None


def safe_model_messages(raw, markers, token_pattern):
    messages = []
    for item in parse_reading(raw.decode("utf-8-sig")):
        text = re.sub(r"^\d+  │  ", "", item.numbered_body(), flags=re.MULTILINE)
        body = text.encode("utf-8")
        for value in markers:
            body = body.replace(value, "[已隐去]".encode())
        body = token_pattern.sub("[已隐去]".encode(), body)
        body = re.sub(rb"(?im)^\s*.*(?:authorization|api[_ -]?key|password|refresh[_ -]?token)\s*[:=].*$",
                      "[含敏感字段的行已隐去]".encode(), body)
        messages.append(SimpleNamespace(number=item.number, start=item.start, end=item.end,
                                        body=body.decode("utf-8", errors="replace")))
    return messages


def generated_draft(raw, model, markers, token_pattern, check):
    messages = safe_model_messages(raw, markers, token_pattern)

    def checked_model(prompt, cancel):
        reply = model(prompt, cancel)
        encoded = reply.encode("utf-8")
        if token_pattern.search(encoded) or any(value in encoded for value in markers):
            raise GenerationError("生成稿包含账号标识或疑似凭据，请核对后重试")
        return reply

    draft = compose_handoff(messages, checked_model, check)
    references = {msg.number: (msg.start, msg.end) for msg in messages}
    for match in re.finditer(r"消息\s*#\s*(\d+)(?:\s*[，,]\s*第\s*(\d+)\s*[–—-]\s*(\d+)\s*行)?", draft):
        number = int(match[1])
        if number not in references or match[2] and (int(match[2]), int(match[3])) != references[number]:
            raise GenerationError("生成稿引用了无法在包内确认的消息位置")
    return draft


def write_readings(raw, archive, catalog, entries, original_entries, model, privacy, token_pattern, check, progress,
                   generation_cancel=None):
    originals = {}
    for entry in original_entries:
        if entry["role"] in {"claude-reading", "handoff-draft"}:
            originals.setdefault(entry["sessionUid"], {})[entry["role"]] = entry
    used = {entry["object"] for entry in original_entries}
    next_object = max((int(obj.rsplit("/", 1)[1]) for obj in used), default=-1) + 1

    def put(data, role, uid, path, **info):
        nonlocal next_object
        name = "objects/" + str(next_object).zfill(8)
        next_object += 1
        archive.writestr(name, data)
        digest = hashlib.sha256(data).hexdigest()
        entries.append({"object": name, "sessionUid": uid, "role": role, "originalPath": path,
                        "size": len(data), "sha256": digest, **info})
        return name, digest

    readings = {}

    def check_generation():
        check()
        if generation_cancel is not None and generation_cancel.is_set():
            raise GenerationCancelled("已停止接手稿生成")

    allocated = set()
    serial = 1
    claude = [r for r in catalog["records"] if r["provider"] == "claude"]
    main_by_id = {r["session_id"]: r["uid"] for r in claude if not r.get("child")}
    for index, record in enumerate(claude, 1):
        check()
        uid = record["uid"]
        prior = originals.get(uid, {})
        old = prior.get("claude-reading")
        number = old.get("readingNumber", "") if old else ""
        if not re.fullmatch(r"[0-9]{4,}", str(number)) or number in allocated:
            while (number := f"{serial:04d}") in allocated:
                serial += 1
            serial += 1
        allocated.add(number)
        progress(f"整理 Claude 阅读版 {index}/{len(claude)}", index * 100 // len(claude))
        if old:
            if old["size"] > MAX_READING_BYTES:
                readings[uid] = {"status": "unavailable"}
                continue
            data = raw.read(old["object"])
            first, sep, rest = data.partition(b"\n")
            data = f"# {'子代理记录' if record.get('child') else 'Code 会话'} {number}".encode() + sep + rest
        else:
            with raw.open(catalog["histories"][uid]) as stream:
                data = reading_markdown(record, number, (privacy.clean(json.loads(line)) for line in stream), check)
        if not data:
            readings[uid] = {"status": "unavailable"}
            continue
        source_path = f"/readings/{number}.md"
        name, digest = put(data, "claude-reading", uid, source_path, readingNumber=number)
        entry = {"object": name, "number": number, "sourceSha256": digest,
                 "status": "child" if record.get("child") else "pending"}
        for path in record.get("files", []):
            parts = path.replace("\\", "/").split("/")
            if "subagents" in parts:
                parent = main_by_id.get(parts[parts.index("subagents") - 1])
                if parent:
                    entry["parentUid"] = parent
        readings[uid] = entry
        if record.get("child"):
            continue
        draft = None
        existing = prior.get("handoff-draft")
        # A reused draft must belong to the exact captured reading representation.
        if existing and old and existing.get("sourceSha256") == old.get("sha256") and (
            old.get("readingNumber") == number
        ):
            draft = raw.read(existing["object"]).decode("utf-8")
            entry["status"] = "reused"
        elif model and (generation_cancel is None or not generation_cancel.is_set()):
            progress(f"生成 Claude 接手稿 {index}/{len(claude)}", index * 100 // len(claude))
            try:
                draft = generated_draft(data, model, privacy.removed, token_pattern, check_generation)
                entry["status"] = "generated"
            except GenerationCancelled:
                pass
            except (GenerationError, ValueError):
                entry["status"] = "failed"
        if draft:
            entry["draft"], _ = put(draft.encode("utf-8"), "handoff-draft", uid,
                                    f"/readings/{number}-接手稿.txt", sourceSha256=digest, readingNumber=number)
    return readings


def read_package_draft(package, uid, meta):
    from .history import read_text

    tracked = package.entries.get(meta["draft"], {})
    source = package.entries.get(meta.get("object"), {})
    if (source.get("role") != "claude-reading" or source.get("sessionUid") != uid
            or source.get("sha256") != meta.get("sourceSha256")
            or tracked.get("role") != "handoff-draft" or tracked.get("sessionUid") != uid
            or tracked.get("sourceSha256") != source.get("sha256")):
        raise ValueError("接手稿与包内原文的指纹不一致")
    return read_text(package._extract(meta["draft"], "prompt.txt"))[0]


def materialize_readings(package, progress=lambda *_: None):
    from .safeio import atomic_json, independent_copy

    catalog = package.package_catalog or {}
    reading_root = package.cache / "readings"
    reading_root.mkdir(exist_ok=True)
    drafts = []
    states = {}
    by_uid = {r["uid"]: r for r in catalog.get("records", [])}
    for n, (uid, meta) in enumerate(catalog.get("readings", {}).items(), 1):
        if meta.get("status") == "unavailable":
            continue
        number = meta.get("number", "")
        if not re.fullmatch(r"[0-9]{4,}", number) or uid not in by_uid:
            raise ValueError("阅读版引用无效")
        reading_object = meta["object"]
        source = package.entries.get(reading_object, {})
        if source.get("role") != "claude-reading" or source.get("sessionUid") != uid or source.get("sha256") != meta.get("sourceSha256"):
            raise ValueError("包内阅读版的出处无法核对")
        if number in states:
            raise ValueError("包内阅读版编号重复，请重新导出")
        states[number] = {"uid": uid, "status": meta.get("status", "pending"), "sourceSha256": source["sha256"]}
        independent_copy(package._extract(reading_object, "reading.md"), reading_root / (number + ".md"))
        if meta.get("draft"):
            row = by_uid[uid]
            drafts.append({"id": number, "project": row.get("metadata", {}).get("project_title") or "未关联项目",
                           "title": row["title"], "prompt": read_package_draft(package, uid, meta)})
        progress(f"读取包内 Claude 原文 {n}", n * 100 // len(catalog["readings"]))
    draft_path = reading_root / "drafts.json"
    atomic_json(draft_path, {"schema": 1, "prompts": drafts})
    return {"readingRoot": str(reading_root), "extraPromptsPath": str(draft_path), "handoffStates": states}

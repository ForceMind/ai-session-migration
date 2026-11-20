"""Read-only access to numbered Claude reading exports and prepared handoffs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from .archive_io import open_regular, descriptor_identity
from .safeio import owned_file, remote_path

MAX_READING_BYTES = 64 * 1024 * 1024
HEADER = re.compile(r"^## ([1-9][0-9]*) · (用户|Claude)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("读取已取消，原始资料未改变")


def read_text(path: Path, limit=MAX_READING_BYTES):
    path = Path(path)
    if remote_path(path):
        raise ValueError("请选择本地资料文件")
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("资料路径不能经过符号链接或目录联接")
    with open_regular(path) as stream:
        before = descriptor_identity(stream.fileno())
        if before[2] > limit:
            raise ValueError("阅读文件过大，请在原文中查看")
        raw = stream.read(limit + 1)
        if len(raw) > limit or descriptor_identity(stream.fileno()) != before:
            raise ValueError("来源在读取时改变，请重新读取")
    return raw.decode("utf-8-sig"), hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ReadingMessage:
    number: int
    role: str
    header: int
    start: int
    end: int
    body: str

    def numbered_body(self):
        rows = self.body.splitlines()
        start = self.start
        # Remove only the export's outer text wrapper, preserving physical lines.
        if len(rows) >= 2 and re.fullmatch(r"`{3,}text", rows[0]):
            marker = rows[0][:-4]
            if rows[-1] == marker:
                rows = rows[1:-1]
                start += 1
        return "\n".join(f"{start + i}  │  {row}" for i, row in enumerate(rows))


def parse_reading(text):
    rows = text.splitlines()
    messages, current, content = [], None, []
    fence_length, fence_char = 0, ""

    def finish():
        if current is None:
            return
        first, last = 0, len(content) - 1
        while first <= last and not content[first].strip():
            first += 1
        while last >= first and not content[last].strip():
            last -= 1
        if first > last:
            raise ValueError(f"消息 #{current[0]} 缺少可定位的正文")
        messages.append(ReadingMessage(*current, current[2] + 1 + first,
                                       current[2] + 1 + last, "\n".join(content[first:last + 1])))

    for line, row in enumerate(rows, 1):
        if not fence_length and re.match(r"^##\s+[0-9]+\s*·", row):
            match = HEADER.fullmatch(row)
            if not match or int(match[1]) != len(messages) + (2 if current else 1):
                raise ValueError(f"消息角色或顺序无法确认，原文第 {line} 行")
            finish()
            current, content = (int(match[1]), match[2], line), []
            continue
        fence = FENCE.match(row)
        if fence:
            marker, tail = fence.groups()
            if not fence_length and (marker[0] != "`" or "`" not in tail):
                fence_length, fence_char = len(marker), marker[0]
            elif marker[0] == fence_char and len(marker) >= fence_length and not tail.strip():
                fence_length = 0
        if current:
            content.append(row)
    finish()
    if not messages or fence_length:
        raise ValueError("没有可确认的消息，或原文代码围栏未闭合")
    return messages


@dataclass(frozen=True)
class PreparedPrompt:
    number: str
    project: str
    title: str
    prompt: str


def load_prompts(prepared=None, extra=None):
    prompts, fingerprints = {}, {}
    if prepared:
        text, digest = read_text(Path(prepared), 8 * 1024 * 1024)
        fingerprints[str(Path(prepared).absolute())] = digest
        for row in text.splitlines():
            if not row.strip():
                continue
            fields = row.split("\t")
            if len(fields) != 5 or any(not part.strip() for part in fields):
                raise ValueError("接手稿 TSV 必须每行包含五个非空字段")
            number, project, title, history, next_step = fields
            if not re.fullmatch(r"[0-9]{4,}", number) or number in prompts:
                raise ValueError("接手稿编号无效或重复")
            prompt = (f"请接手「{project}」的{title}。\n\n历史来源：本地阅读版 会话/{number}.md。\n\n"
                      f"【历史背景与停点 · 待复核】\n{history}\n\n【接手后的第一步】\n{next_step}\n\n"
                      "请先只读核对实际目录、相关说明与实现，给出证据位置和剩余工作。"
                      "旧版本、测试、部署和授权仅作历史背景。先报告结果，再等我确认修改或外部操作。")
            prompts[number] = PreparedPrompt(number, project, title, prompt)
    if extra:
        text, digest = read_text(Path(extra), 8 * 1024 * 1024)
        fingerprints[str(Path(extra).absolute())] = digest
        data = json.loads(text)
        if data.get("schema") != 1 or not isinstance(data.get("prompts"), list):
            raise ValueError("补充接手稿格式不支持")
        for row in data["prompts"]:
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("id", "project", "title", "prompt")):
                raise ValueError("补充接手稿缺少必要字段")
            number = row["id"]
            if not re.fullmatch(r"[0-9]{4,}", number) or number in prompts:
                raise ValueError("补充接手稿编号无效或重复")
            prompts[number] = PreparedPrompt(number, row["project"], row["title"], row["prompt"])
    return prompts, fingerprints


@dataclass(frozen=True)
class ReadingEntry:
    number: str
    kind: str
    path: Path
    title: str
    project: str


class ReadingCatalog:
    def __init__(self, root, prompts=None, fingerprints=None):
        raw = Path(root).expanduser().absolute()
        if remote_path(raw) or raw.is_symlink() or raw.is_junction():
            raise ValueError("阅读版目录必须是本地独立目录")
        self.root = raw.resolve(strict=True)
        self.prompts, self.fingerprints = prompts or {}, fingerprints or {}
        self.entries, self.warnings = [], []

    def _path(self, entry):
        path = Path(entry.path)
        self._guard_root()
        if not owned_file(path, self.root) or not path.is_relative_to(self.root):
            raise ValueError("阅读版路径越界或经过链接")
        return path

    def _guard_root(self):
        if not self.root.is_dir():
            raise ValueError("原文目录已移走，请重新选择")
        for path in (self.root, *self.root.parents):
            if path.is_symlink() or path.is_junction():
                raise ValueError("原文目录经过链接，请选择独立资料目录")

    def scan(self, progress=lambda *_: None, cancel=None):
        self._guard_root()
        self.entries, self.warnings = [], []
        files = sorted(self.root.glob("*.md"))
        for index, path in enumerate(files):
            cancelled(cancel)
            if not owned_file(path, self.root):
                self.warnings.append(path.name + " 路径不是独立普通文件")
                continue
            try:
                with open_regular(path) as stream:
                    head = stream.read(8192).decode("utf-8-sig", errors="replace")
                first = head.splitlines()[0] if head else ""
                number = path.stem
                numbered = bool(re.fullmatch(r"[0-9]{4,}", number))
                kind = "main" if numbered and first == "# Code 会话 " + number else "agent" if numbered and first == "# 子代理记录 " + number else "unknown"
                prompt = self.prompts.get(number) if kind == "main" else None
                snippet = next((s.strip()[:70] for s in head.splitlines()[4:] if s.strip() and not s.startswith(("#", "```", "~~~", "<"))), "")
                self.entries.append(ReadingEntry(number, kind, path, prompt.title if prompt else snippet or path.name,
                                                  prompt.project if prompt else "子代理记录" if kind == "agent" else "未整理"))
            except (OSError, ValueError) as error:
                self.warnings.append(path.name + " " + type(error).__name__)
            if index % 32 == 0:
                progress(f"读取历史目录，已检查 {index + 1} 份", (index + 1) * 100 // max(1, len(files)))
        progress("历史目录读取完成", 100)
        return self

    def read(self, entry, cancel=None):
        cancelled(cancel)
        text, digest = read_text(self._path(entry))
        expected = ("# Code 会话 " if entry.kind == "main" else "# 子代理记录 ") + entry.number
        if entry.kind == "unknown" or not text.splitlines() or text.splitlines()[0] != expected:
            raise ValueError("该文件的会话类型或编号未确认，请查看原文")
        messages = parse_reading(text)
        cancelled(cancel)
        return messages, digest

    def search(self, query="", kind="main", project="", full_text=False, cancel=None):
        term = query.strip().casefold()
        result = []
        for entry in self.entries:
            cancelled(cancel)
            if kind != "all" and entry.kind != kind or project and entry.project != project:
                continue
            haystack = " ".join((entry.number, entry.title, entry.project)).casefold()
            if not term or term in haystack:
                result.append(entry)
            elif full_text:
                text, _ = read_text(self._path(entry))
                if term in text.casefold():
                    result.append(entry)
        return result

    def check_unchanged(self, entry, digest):
        if read_text(self._path(entry))[1] != digest:
            raise ValueError("历史原文已变化，请重新选择会话后复制")
        for path, expected in self.fingerprints.items():
            if read_text(Path(path), 8 * 1024 * 1024)[1] != expected:
                raise ValueError("接手稿来源已变化，请重新读取目录")


def discover_reading_root(home):
    base = Path(os.environ.get("LOCALAPPDATA", Path(home) / "AppData/Local")) / "Claude Account Reset/protected-exports"
    return next((str(p / "会话") for p in sorted(base.glob("Claude-session-readable-*"), reverse=True)
                 if not p.is_symlink() and not p.is_junction() and (p / "会话").is_dir()), "")


def load_local_config(directory):
    path = Path(directory) / "history-config.json"
    if not path.is_file():
        return {}
    text, _ = read_text(path, 64 * 1024)
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("本地历史来源配置格式不支持")
    result = {}
    for key in ("readingRoot", "preparedPath", "extraPromptsPath"):
        value = data.get(key, "")
        if not isinstance(value, str):
            raise ValueError("历史来源路径必须是文本")
        if value:
            p = Path(value)
            result[key] = str(p if p.is_absolute() else path.parent / p)
    return result

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import hashlib
import math

PROVIDERS = {"claude": "Claude", "codex": "Codex", "deepseek": "DeepSeek Harness"}


def identity(provider: str, root: str, session_id: str) -> str:
    return provider + ":" + hashlib.sha256(root.encode()).hexdigest()[:12] + ":" + session_id


@dataclass
class SessionRecord:
    uid: str
    provider: str
    session_id: str
    title: str
    root: str
    cwd: str = ""
    updated: float = 0
    archived: bool = False
    pinned: bool = False
    child: bool = False
    issue: str = ""
    files: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def project(self) -> str:
        return self.metadata.get("project_title") or (Path(self.cwd).name if self.cwd else "未关联项目")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "SessionRecord":
        if not isinstance(value, dict) or value.get("provider") not in PROVIDERS:
            raise ValueError("数据包包含不支持的会话来源")
        if any(not isinstance(value.get(k), str) for k in ["uid", "session_id", "title", "root"]):
            raise ValueError("数据包的会话目录字段损坏")
        if not isinstance(value.get("metadata", {}), dict) or not isinstance(value.get("files", []), list) or any(not isinstance(x, str) for x in value.get("files", [])):
            raise ValueError("数据包的会话引用字段损坏")
        data = {k: v for k, v in value.items() if k in cls.__dataclass_fields__}
        data["updated"] = float(data.get("updated", 0))
        if not math.isfinite(data["updated"]):
            raise ValueError("数据包的时间字段损坏")
        return cls(**data)


@dataclass
class Message:
    role: str
    text: str
    kind: str = "message"
    label: str = ""
    time: str = ""


@dataclass
class Attachment:
    name: str
    path: str = ""
    kind: str = "file"
    note: str = ""
    data: str = ""
    media_type: str = ""


@dataclass
class SessionPage:
    messages: list[Message] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    total: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class Catalog:
    records: list[SessionRecord] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

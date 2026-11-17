"""Read and merge Codex Desktop's explicit standalone-conversation list."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .safeio import CHUNK, stable_bytes

INDEX_FILENAME = ".codex-global-state.json"
PROJECTLESS = "projectless-thread-ids"
ASSIGNMENTS = "thread-project-assignments"


def sidebar_state(root: Path):
    path = root / INDEX_FILENAME
    original = stable_bytes(path, 64 * CHUNK) if os.path.lexists(path) else None
    state = json.loads(original) if original is not None else {}
    if not isinstance(state, dict):
        raise ValueError("Codex 侧栏配置格式未识别")
    ids = state.get(PROJECTLESS, [])
    if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
        raise ValueError("Codex 独立会话列表格式未识别")
    if not isinstance(state.get(ASSIGNMENTS, {}), dict):
        raise ValueError("Codex 侧栏项目归属格式未识别")
    return original, state


def append_projectless(state, ids):
    if set(ids) & state.get(ASSIGNMENTS, {}).keys():
        raise ValueError("所选会话已有桌面项目归属，请重新预览")
    order = state.get(PROJECTLESS, [])
    existing = set(order)
    additions = [sid for sid in dict.fromkeys(ids) if sid not in existing]
    return {**state, PROJECTLESS: [*order, *additions]}


def explicitly_projectless(record):
    return (record.provider == "codex" and not record.child
            and record.metadata.get("codex_projectless") is True
            and not record.metadata.get("native_thread_metadata", {}).get("project_id")
            and not record.metadata.get("parent_id"))

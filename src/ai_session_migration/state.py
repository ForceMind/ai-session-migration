from __future__ import annotations

import os
from pathlib import Path
import sys

from .safeio import atomic_json, read_json


def state_directory():
    if os.environ.get("ASM_STATE_DIR"):
        return Path(os.environ["ASM_STATE_DIR"]).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/AI Session Migration"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "AI Session Migration"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "ai-session-migration"


class AppState:
    def __init__(self, root=None):
        self.root = Path(root or state_directory())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "local-state.json"
        self.data = read_json(self.path, {"schema": 1, "marks": [], "selection": [], "sources": [], "backups": []})
        if not isinstance(self.data, dict) or self.data.get("schema") != 1:
            self.data = {"schema": 1, "marks": [], "selection": [], "sources": [], "backups": []}

    @property
    def cache(self):
        return Path(self.data.get("cache") or self.root / "cache")

    def save(self):
        atomic_json(self.path, self.data)

    def record_backup(self, report):
        self.data.setdefault("backups", []).insert(0, {k: report.get(k) for k in ["schema", "status", "output", "portable", "encrypted", "startedAt", "finishedAt", "sessions", "files", "bytes", "warnings", "encryptedReadbackVerified", "keyPath", "plainSha256", "error", "archiveReady", "pendingArchive", "attempts"]})
        self.save()

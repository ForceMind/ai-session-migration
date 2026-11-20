import os
from pathlib import Path

from .safeio import within

NATIVE_PROVIDERS = frozenset({"codex", "deepseek"})
CLAUDE_READ_ONLY = "Claude 历史仅供阅读与接手。请选择 Codex 或 DeepSeek Harness 会话进行原生导入。"


def require_native_targets(providers):
    if not set(providers) <= NATIVE_PROVIDERS:
        raise ValueError(CLAUDE_READ_ONLY)


def app_data_directories(home):
    return (home / "Library/Application Support",
            Path(os.environ.get("APPDATA", home / "AppData/Roaming")),
            Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")))


def claude_directories():
    home = Path.home()
    candidates = [home / ".claude"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser())
    for directory in app_data_directories(home):
        candidates.extend((directory / "Claude", directory / "Claude-Data"))
        if directory.is_dir():
            candidates.extend(directory.glob("Claude-*"))
    return candidates


def forbid_claude_directory(path):
    path = Path(path)
    if any(part.casefold() in {".claude", "claude-Data".casefold(), "claude-code-sessions"} for part in path.parts) or any(
        within(path, candidate) or within(candidate, path) for candidate in claude_directories()
    ):
        raise ValueError(CLAUDE_READ_ONLY)


def forbid_client_storage(path):
    path = Path(path)
    message = "请选择独立的项目、恢复或备份目录。原生会话资料须使用对应的会话入口"
    try:
        forbid_claude_directory(path)
    except ValueError as error:
        raise ValueError(message) from error
    home = Path.home()
    candidates = [home / ".codex", home / ".dsh"]
    for key in ("CODEX_HOME", "DSH_HOME"):
        if os.environ.get(key):
            candidates.append(Path(os.environ[key]).expanduser())
    for directory in app_data_directories(home):
        candidates.append(directory / "Codex")
    if any(part.casefold() in {".codex", ".dsh"} for part in path.parts) or any(
        within(path, candidate) or within(candidate, path) for candidate in candidates
    ):
        raise ValueError(message)

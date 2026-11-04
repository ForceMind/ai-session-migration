from pathlib import Path
import base64
import hashlib
import json
import sqlite3
import zstandard


def make_home(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    project = home / "示例项目"
    project.mkdir(exist_ok=True)
    c = home / ".claude/projects/example/claude-session.jsonl"
    c.parent.mkdir(parents=True)
    events = [
        {"type": "custom-title", "sessionId": "claude-session", "customTitle": "核对按钮状态与反馈文案"},
        {"type": "user", "cwd": str(project), "message": {"role": "user", "content": [{"type": "text", "text": "把设置页分成账号、界面和快捷键三组。"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "fixture-existing-thinking", "signature": "fixture-signature"}, {"type": "text", "text": "可以。使用左侧分组导航，右侧显示当前分组内容。\n\n- 账号：身份与连接\n- 界面：主题与语言\n- 快捷键：常用操作"}]}},
    ]
    c.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in events))
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
    attachment = c.parent / "claude-session/tool-results/reference.png"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(image)
    codex = home / ".codex"
    codex.mkdir()
    db = sqlite3.connect(codex / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id TEXT, title TEXT, name TEXT, cwd TEXT, source TEXT, updated_at INTEGER, archived INTEGER, rollout_path TEXT, history_mode TEXT, model_provider TEXT, is_pinned INTEGER)")
    db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("codex-session", "整理设置页的交互细节", None, str(project), "vscode", 1793800000, 0, str(codex / "sessions/test.jsonl"), "paginated", "openai", 0))
    db.commit(); db.close()
    db = sqlite3.connect(codex / "thread_history_1.sqlite")
    db.execute("CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER, item_json TEXT)")
    for index, item in enumerate([
        {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "切换分组时保留没有保存的内容。"}]},
        {"type": "reasoning", "id": "r", "summary": [{"text": "fixture-existing-reasoning"}], "content": []},
        {"type": "commandExecution", "id": "t", "command": "read example.md", "aggregatedOutput": "示例文件内容，仅供测试"},
        {"type": "agentMessage", "id": "a", "text": "建议在切换时暂存输入，并提供清晰的保存反馈。"},
    ]):
        db.execute("INSERT INTO thread_items VALUES (?,?,?,?,?)", ("codex-session", "turn", str(index), index, json.dumps(item, ensure_ascii=False)))
    db.commit(); db.close()
    dsh = home / ".dsh"
    session = dsh / "sessions/example/harness-session"
    session.mkdir(parents=True)
    aid = hashlib.sha256(image).hexdigest()
    asset = dsh / "attachments/v1/objects" / aid[:2] / aid
    asset.parent.mkdir(parents=True)
    asset.write_bytes(image)
    rows = [
        {"type": "session", "version": 4, "id": "harness-session", "createdAt": 1000, "cwd": str(project), "isSeeded": False, "delegationDepth": 0},
        {"type": "user/message", "seq": 0, "data": {"role": "user", "content": [{"type": "text", "text": "梳理文档的阅读顺序。"}, {"type": "image", "attachment": {"attachmentId": aid, "name": "设置页参考.png", "mediaType": "image/png"}}]}},
        {"type": "assistant/message", "seq": 1, "data": {"message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "fixture-original-harness-thinking"}, {"type": "text", "text": "从来源选择开始，依次介绍会话浏览、备份和校验。"}]}}},
    ]
    compressor = zstandard.ZstdCompressor(write_checksum=True)
    with (session / "session.v4.jsonl.zstd").open("wb") as f:
        for row in rows:
            f.write(compressor.compress((json.dumps(row, ensure_ascii=False) + "\n").encode()))
    storage = dsh / "storages"
    storage.mkdir()
    (storage / "workspace.json").write_text(json.dumps({"unit": {"name": "workspace", "version": 2}, "global": {"workspaceIds": ["project"], "archivedSessionIds": ["harness-session"], "pinnedSessionIds": []}, "tables": {"workspaces": {"project": {"path": str(project), "title": "示例项目", "sessionIds": ["harness-session"], "createdAt": "2026-01-01"}}}}, ensure_ascii=False))
    cache = storage / "session_projcache/sessions"
    cache.mkdir(parents=True)
    (cache / "harness-session.json").write_text(json.dumps({"record": {"identity": {"formatVersion": 4, "createdAt": 1000, "isSeeded": False}, "rows": {"title": {"val": "梳理文档的阅读顺序"}}}}, ensure_ascii=False))
    (dsh / ".credentials.yaml").write_text("token: synthetic-secret-not-for-export\n")
    return {"claude": c, "deepseek": session / "session.v4.jsonl.zstd", "project": project}

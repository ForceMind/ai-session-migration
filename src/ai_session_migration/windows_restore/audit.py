"""Check the installed session data against the recorded import scope."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

from ..importer import checked_directory, strip_claude_account_metadata
from ..safeio import MAX_LINE, hash_file


def audit_has_issues(report):
    claude = report.get("claude", {})
    return (
        claude.get("importedLogsChecked") != claude.get("expectedImportedLogs")
        or any(claude.get(field) for field in ("accountFieldsFound", "invalidLines", "prefixMismatches"))
        or any(report.get(p, {}).get("changesOutsideSessionScope") for p in ("claude", "deepseek"))
    )


def inspect_transcript(path, size):
    """Read just the imported prefix; later Windows events can use the local account."""
    digest = hashlib.sha256()
    fields = invalid = lines = 0
    remaining = size
    with path.open("rb") as stream:
        while remaining:
            line = stream.readline(min(remaining, MAX_LINE + 1))
            if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise ValueError("已导入的 Claude 日志前缀不完整")
            digest.update(line)
            remaining -= len(line)
            lines += 1
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                invalid += 1
                continue
            if not isinstance(event, dict):
                invalid += 1
                continue
            fields += strip_claude_account_metadata(event)
    return {"sha256": digest.hexdigest(), "lines": lines, "accountFields": fields, "invalidLines": invalid}


def _receipts(state, run, provider, root):
    pilot = state.get("pilot", {}).get(provider, {})
    batches = state.get("bulk", {}).get("batches", [])
    items = ([pilot] if pilot.get("imported") else []) + [row for row in batches if row["provider"] == provider]
    backups = (run / "backups").resolve()
    for item in items:
        journal = Path(item["journal"]).resolve()
        if not journal.is_relative_to(backups):
            raise ValueError("导入回执超出本次恢复备份目录")
        report = json.loads(journal.read_bytes())
        if report["status"] != "complete" or not report.get("targetReadbackVerified"):
            raise ValueError("已记录入库的批次缺少完整回执")
        entry = next(row for row in report["roots"] if row["provider"] == provider)
        if Path(entry["target"]).resolve() != root:
            raise ValueError("导入回执与当前客户端资料目录不一致")
        yield entry


def audit_installed(draft, archive, state, run):
    outcomes = state.get("bulk", {}).get("outcomes", {})
    pilot = state.get("pilot", {})
    claude_pilot = draft["firstBatch"].get("claude", {}).get("sessionId") if pilot.get("claude", {}).get("imported") else None
    expected = {
        record.session_id for record in archive.scan().records if record.provider == "claude" and (
            outcomes.get(record.uid, {}).get("status") == "imported" or record.session_id == claude_pilot
        )
    }
    claude_logs = {}
    result = {"schema": "asm-windows-session-audit-1", "catalogSha256": draft["catalogSha256"]}
    for provider, prefixes in (("claude", ("projects/", "attachments/")), ("deepseek", ("sessions/", "attachments/"))):
        target = draft["targets"].get(provider)
        if not target:
            continue
        root = checked_directory(Path(target["directory"]))
        protected_changes = batches = 0
        baseline = {}
        for entry in _receipts(state, run, provider, root):
            before, after = entry["before"]["files"], entry["after"]["files"]
            allowed = {"storages/", "storages/workspace.json"} if provider == "deepseek" else set()
            changes = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
            protected_changes += sum(p not in allowed and not p.startswith(prefixes) for p in changes)
            if batches == 0:
                baseline = {
                    p: details["sha256"] for p, details in before.items()
                    if not details.get("directory") and p not in allowed and not p.startswith(prefixes)
                    and any(term in p.casefold() for term in ("settings", "credential", "auth", "profile", "config", "plugin", "channel"))
                }
            if provider == "claude":
                for relative in changes - before.keys():
                    path = root / relative
                    if relative.startswith("projects/") and path.suffix == ".jsonl" and path.stem in expected:
                        if path.stem in claude_logs or not path.resolve().is_relative_to(root):
                            raise ValueError("Claude 回执包含多个同 ID 日志或路径超出客户端目录")
                        claude_logs[path.stem] = (path, after[relative]["bytes"], after[relative]["sha256"])
            batches += 1
        matched = sum(
            (root / name).is_file() and (root / name).resolve().is_relative_to(root) and hash_file(root / name) == digest
            for name, digest in baseline.items()
        )
        result[provider] = {"receiptsChecked": batches, "changesOutsideSessionScope": protected_changes,
                            "preexistingConfigFilesChecked": len(baseline), "preexistingConfigFilesMatched": matched}

    if "claude" in result:
        # The first Windows run repaired a pilot imported by an earlier engine. Use
        # its verified cleaned prefix in place of that pilot's original receipt.
        cleaned_journal = pilot.get("claude", {}).get("accountCleanupJournal")
        if cleaned_journal and claude_pilot in claude_logs:
            path = Path(cleaned_journal).resolve()
            if not path.is_relative_to((run / "backups").resolve()):
                raise ValueError("Claude 清理回执超出本次恢复备份目录")
            clean = json.loads(path.read_bytes())
            log, _, _ = claude_logs[claude_pilot]
            if clean["status"] != "complete" or Path(clean["target"]).resolve() != log.resolve():
                raise ValueError("Claude 样本清理回执不匹配")
            size = clean.get("cleanedBytes")
            checksum = clean["cleanedSha256"]
            if size is None:
                # Older receipts kept the hash but not the length. A digest
                # match at a line boundary also works after Windows appends.
                digest = hashlib.sha256()
                with log.open("rb") as stream:
                    while line := stream.readline(MAX_LINE + 1):
                        if len(line) > MAX_LINE:
                            break
                        digest.update(line)
                        if digest.hexdigest() == checksum:
                            size = stream.tell()
                            break
                if size is None:
                    raise ValueError("Claude 样本当前日志无法核对到已清理前缀")
            claude_logs[claude_pilot] = (log, size, checksum)

        def check(item):
            path, size, checksum = item
            report = inspect_transcript(path, size)
            return report["lines"], report["accountFields"], report["invalidLines"], report["sha256"] != checksum

        with ThreadPoolExecutor(max_workers=4) as pool:
            checks = list(pool.map(check, claude_logs.values()))
        result["claude"].update(
            expectedImportedLogs=len(expected), importedLogsChecked=len(checks),
            importedLinesChecked=sum(c[0] for c in checks),
            accountFieldsFound=sum(c[1] for c in checks), invalidLines=sum(c[2] for c in checks),
            prefixMismatches=sum(c[3] for c in checks),
        )
    if "deepseek" in result:
        result["deepseek"].update(channelsRestored=False, thirdPartyPluginsRestored=False)
    return result

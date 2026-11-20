"""Build the Mac disk image and qualify the app directly from that image."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
APP = "AI Session Migration"
IMAGE = "AI-Session-Migration-macOS.dmg"


def run(*args, env=None, timeout=None):
    subprocess.run(args, cwd=ROOT, env=env, timeout=timeout, check=True)


def qualify(app, work, env):
    home = work / "fixture-home"
    state = work / "fixture-state"
    report = work / "packaged-smoke.json"
    env = dict(env, PYTHONPATH=str(ROOT / "tests"), ASM_ACCEPTANCE_TEST="1")
    run(
        sys.executable, "-B", "-c",
        "from pathlib import Path; import sys; from fixtures import make_acceptance_home, make_import_target; "
        "make_acceptance_home(Path(sys.argv[1])); make_import_target(Path(sys.argv[2]) / 'synthetic-import-target')",
        str(home), str(state), env=env,
    )
    run(
        str(app / "Contents" / "MacOS" / APP),
        "--smoke-home", str(home), "--smoke-state", str(state), "--smoke-report", str(report),
        "--smoke-import", "--smoke-recovery", env=env, timeout=240,
    )
    found = json.loads(report.read_text(encoding="utf-8"))
    imported = found.get("syntheticImport", {})
    readback = {row.get("provider") for row in imported.get("nativeReadback", [])}
    if not (
        found.get("ok") and found.get("packaged") and found.get("nativeQt")
        and found.get("realLocalData") is False and found.get("modelRequestSent") is False
        and imported.get("imported") == 2 and readback == {"codex", "deepseek"}
        and imported.get("rollbackVerified") and imported.get("mixedPackageVerified")
        and found.get("syntheticRecovery", {}).get("ok")
        and found.get("readChecks", {}).get("claude", {}).get("messages", 0) > 0
    ):
        raise RuntimeError("The mounted Mac app did not pass synthetic acceptance")
    return found


def build_release(output, work):
    if output.exists():
        raise FileExistsError(f"Use a new output directory: {output}")
    work.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="macos-release-", dir=work))
    app_dir = run_dir / "app"
    image_root = run_dir / "image"
    mount = run_dir / "mount"
    dmg = run_dir / IMAGE
    image_root.mkdir()
    mount.mkdir()
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "DSH_HOME", "ASM_STATE_DIR"}
    }
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1", QT_QPA_PLATFORM="offscreen")
    run(sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v", env=env)
    run(sys.executable, str(ROOT / "tools/build_app.py"), "--dist", str(app_dir), "--work", str(run_dir / "pyinstaller"), env=env)
    app = app_dir / (APP + ".app")
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    run("/usr/bin/ditto", str(app), str(image_root / app.name))
    run("/usr/bin/hdiutil", "create", "-volname", APP, "-srcfolder", str(image_root), "-format", "UDZO", str(dmg))
    run("/usr/bin/hdiutil", "verify", str(dmg))
    run("/usr/bin/hdiutil", "attach", str(dmg), "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", str(mount))
    try:
        mounted_app = mount / app.name
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(mounted_app))
        report = qualify(mounted_app, run_dir, env)
    finally:
        run("/usr/bin/hdiutil", "detach", str(mount))

    output.mkdir(parents=True, exist_ok=False)
    with dmg.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    partial = output / (IMAGE + ".partial")
    shutil.copyfile(dmg, partial)
    with partial.open("rb") as copied:
        if hashlib.file_digest(copied, "sha256").hexdigest() != digest:
            raise OSError("The staged disk image differs from the verified image")
    report.update(mountedReadOnly=True, imageVerified=True, architecture=platform.machine(), imageSha256=digest)
    (output / "packaged-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "macOS-SHA256.txt").write_text(f"{digest}  {IMAGE}\n", encoding="ascii")
    partial.rename(output / IMAGE)
    print(output / IMAGE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True, help="A new output directory")
    parser.add_argument("--work", type=Path, required=True, help="A temporary build workspace")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Building and qualifying the disk image requires macOS")
    build_release(args.dist.resolve(), args.work.resolve())


if __name__ == "__main__":
    main()

"""Maintainer build entry. End users receive a double-clickable app, not scripts."""
import argparse
from pathlib import Path
import platform
import os
import plistlib
import subprocess
import sys
import re

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/ai_session_migration/__init__.py").read_text(encoding="utf-8"))[1]
parser = argparse.ArgumentParser()
parser.add_argument("--dist", required=True)
parser.add_argument("--work", required=True)
args = parser.parse_args()
dist, work = Path(args.dist).resolve(), Path(args.work).resolve()
dist.mkdir(parents=True, exist_ok=True)
work.mkdir(parents=True, exist_ok=True)
icon = ROOT / "assets" / ("app-icon.icns" if sys.platform == "darwin" else "app-icon.ico")
command = [sys.executable, "-m", "PyInstaller", "--windowed", "--onedir", "--noconfirm", "--name", "AI Session Migration",
           "--distpath", str(dist), "--workpath", str(work / "build"), "--specpath", str(work),
           "--paths", str(ROOT / "src"),
           "--add-data", str(ROOT / "assets/app-icon.png") + ":assets",
           "--add-data", str(ROOT / "LICENSE") + ":licenses",
           "--add-data", str(ROOT / "THIRD-PARTY-NOTICES.txt") + ":licenses",
           "--add-data", str(ROOT / "assets/licenses") + ":licenses",
           "--copy-metadata", "PySide6-Essentials", "--copy-metadata", "shiboken6",
           "--copy-metadata", "cryptography", "--copy-metadata", "zstandard"]
if icon.exists():
    command += ["--icon", str(icon)]
if sys.platform == "darwin":
    command += ["--osx-bundle-identifier", "com.forcemind.ai-session-migration"]
command += [str(ROOT / "tools/launch.py")]
environment = os.environ.copy()
if sys.platform == "win32":
    # Keep unrelated toolchains' incompatible ICU/API-set DLLs out of the app.
    windows = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    environment["PATH"] = os.pathsep.join(str(p) for p in (
        Path(sys.executable).parent, Path(sys.base_prefix),
        Path(sys.base_prefix) / "DLLs", windows / "System32", windows))
environment["PYINSTALLER_CONFIG_DIR"] = str(work / "pyinstaller-cache")
subprocess.run(command, cwd=ROOT, env=environment, check=True)
if sys.platform == "darwin":
    app = dist / "AI Session Migration.app"
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update(CFBundleShortVersionString=VERSION, CFBundleVersion=VERSION, CFBundleDisplayName="AI Session Migration", LSMinimumSystemVersion="14.4", NSRequiresAquaSystemAppearance=False, NSHighResolutionCapable=True)
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
print("Built desktop client for", platform.system(), platform.machine())

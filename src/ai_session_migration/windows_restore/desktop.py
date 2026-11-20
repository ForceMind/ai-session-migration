"""Use the installed Codex desktop app and its local app-server."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from itertools import count
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import threading
import time


@dataclass(frozen=True)
class Desktop:
    directory: Path
    app_id: str

    @property
    def executable(self):
        return self.directory / "app" / "ChatGPT.exe"

    @property
    def cli(self):
        return self.directory / "app" / "resources" / "codex.exe"


def powershell_json(script, env=None):
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    installed = (program_files / "PowerShell/7/pwsh.exe", local_app_data / "Microsoft/WindowsApps/pwsh.exe")
    exe = next((path for path in installed if path.is_file()), None) or shutil.which("pwsh.exe")
    if exe is None:
        exe = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(exe), "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n" + script],
        env=env, encoding="utf-8", capture_output=True, timeout=12, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return json.loads(result.stdout)


def installed_desktop():
    package = powershell_json(r"""
$package = Get-AppxPackage -Name 'OpenAI.Codex' | Sort-Object Version -Descending | Select-Object -First 1
if (!$package) { throw 'OpenAI.Codex package is not installed.' }
$id = $package.PackageFamilyName + '!App'
$app = Get-StartApps | Where-Object AppID -eq $id | Select-Object -First 1
if (!$app) { throw 'OpenAI.Codex app is not registered.' }
@{ root = $package.InstallLocation; appId = $app.AppID } | ConvertTo-Json -Compress
""")
    desktop = Desktop(Path(package["root"]), package["appId"])
    if not desktop.executable.is_file() or not desktop.cli.is_file():
        raise FileNotFoundError("已安装的 Codex 缺少桌面程序或 app-server")
    return desktop


def desktop_processes(desktop):
    return powershell_json(r"""
$apps = @(Get-Process -Name 'ChatGPT' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $env:ASM_CODEX_DESKTOP_EXE } |
    ForEach-Object { @{ pid = $_.Id; window = $_.MainWindowHandle.ToInt64() } })
ConvertTo-Json -InputObject $apps -Compress
""", dict(os.environ, ASM_CODEX_DESKTOP_EXE=str(desktop.executable)))


def launch(desktop):
    explorer = Path(os.environ["SystemRoot"]) / "explorer.exe"
    subprocess.Popen([str(explorer), f"shell:AppsFolder\\{desktop.app_id}"])


def main_window(desktop, seconds):
    deadline = time.monotonic() + seconds
    while True:
        match = next((p for p in desktop_processes(desktop) if p["window"]), None)
        if match or time.monotonic() >= deadline:
            return match
        time.sleep(1)


class KeyInput(ctypes.Structure):
    _fields_ = [
        ("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("x", wintypes.LONG), ("y", wintypes.LONG), ("data", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
    ]


class InputPayload(ctypes.Union):
    _fields_ = [("keyboard", KeyInput), ("mouse", MouseInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("payload", InputPayload)]


def send_quit(window):
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.BringWindowToTop.argtypes = [wintypes.HWND]
    user.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    user.SendInput.restype = wintypes.UINT

    handle = window["window"]
    owner = wintypes.DWORD()
    user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
    if owner.value != window["pid"]:
        raise RuntimeError("Codex 窗口已变动，无法确认退出命令的目标")
    user.ShowWindowAsync(handle, 9)  # SW_RESTORE
    user.SetForegroundWindow(handle)
    foreground = user.GetForegroundWindow()
    if foreground and foreground != handle:
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        caller = kernel.GetCurrentThreadId()
        other = user.GetWindowThreadProcessId(foreground, None)
        # Create a message queue before temporarily joining the foreground input thread.
        user.PeekMessageW(ctypes.byref(wintypes.MSG()), None, 0, 0, 0)
        if other and other != caller and user.AttachThreadInput(caller, other, True):
            try:
                user.BringWindowToTop(handle)
                user.SetForegroundWindow(handle)
            finally:
                user.AttachThreadInput(caller, other, False)
    deadline = time.monotonic() + 4
    while user.GetForegroundWindow() != handle:
        if time.monotonic() >= deadline:
            raise RuntimeError("Windows 拒绝切换到 Codex 窗口，无法安全发送退出命令")
        time.sleep(0.1)

    # The installed application's Windows Quit menu binds Ctrl+Q.
    keys = (Input * 4)(*(Input(1, InputPayload(keyboard=KeyInput(vk, 0, flags, 0, 0)))
                         for vk, flags in ((0x11, 0), (0x51, 0), (0x51, 2), (0x11, 2))))
    sent = user.SendInput(len(keys), keys, ctypes.sizeof(Input))
    if sent != len(keys):
        raise RuntimeError("Windows 未完整发送 Codex 的退出快捷键")


def request_quit(desktop):
    processes = desktop_processes(desktop)
    if not processes:
        return False
    window = next((p for p in processes if p["window"]), None)
    if window is None:
        launch(desktop)
        window = main_window(desktop, 12)
    if window is None:
        raise RuntimeError("找不到 Codex 主窗口，无法执行程序内的退出命令")
    send_quit(window)
    return True


def reopen(desktop):
    launch(desktop)
    opened = main_window(desktop, 30)
    if opened is None:
        raise RuntimeError("Windows 已收到打开 Codex 的请求，尚未出现主窗口")
    return opened


@contextmanager
def app_server(exe, env, cwd, log, client_name):
    replies = Queue()
    with log.open("a", encoding="utf-8") as stderr:
        server = subprocess.Popen(
            [str(exe), "app-server", "--listen", "stdio://"], env=env, cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
        )

        def receive():
            for line in server.stdout:
                try:
                    replies.put(json.loads(line))
                except ValueError:
                    continue
            replies.put(None)

        threading.Thread(target=receive, daemon=True).start()
        requests = count()

        def request(method, params=None, *, notification=False):
            message = {"method": method}
            if params is not None:
                message["params"] = params
            if not notification:
                message["id"] = next(requests)
            server.stdin.write(json.dumps(message) + "\n")
            server.stdin.flush()
            if notification:
                return
            deadline = time.monotonic() + 90
            while True:
                reply = replies.get(timeout=max(0.05, deadline - time.monotonic()))
                if reply is None:
                    raise RuntimeError("Codex app-server 已退出")
                if reply.get("id") == message["id"]:
                    if "error" in reply:
                        raise RuntimeError(method + "：" + reply["error"]["message"])
                    return reply["result"]

        try:
            request("initialize", {
                "clientInfo": {"name": client_name, "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            })
            request("initialized", notification=True)
            yield request
        finally:
            try:
                server.stdin.close()
            except BrokenPipeError:
                pass
            try:
                server.wait(timeout=6)
            except subprocess.TimeoutExpired:
                server.terminate()
                server.wait(timeout=6)

from __future__ import annotations

import ctypes
import json
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765"
PORT = 8765
TITLE = "学习视频总结"
ICON = BASE_DIR / "assets" / "icon.ico"


def health() -> dict | None:
    request = urllib.request.Request(f"{URL}/api/health", headers={"X-Study-Buddy": "1"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None


def is_running() -> bool:
    return health() is not None


def local_build() -> str:
    from study_buddy.build_info import build_id

    return build_id()


def has_active_job() -> bool:
    request = urllib.request.Request(f"{URL}/api/jobs", headers={"X-Study-Buddy": "1"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return False
    return any(item.get("status") in {"queued", "running"} for item in payload.get("jobs", []))


def stop_server(port: int = PORT) -> bool:
    """结束占用端口的监听进程，用于替换掉跑着旧代码的后台服务。"""
    try:
        finished = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15
        )
        output = finished.stdout or ""
    except Exception:
        return False
    pids = {
        parts[4]
        for parts in (line.split() for line in output.splitlines())
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}")
    }
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(0.3)
    return not is_running()


def start_server() -> None:
    pythonw = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    server = BASE_DIR / "app.py"
    log_dir = BASE_DIR / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "server.log").open("a", encoding="utf-8")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(pythonw), str(server)],
        cwd=str(BASE_DIR),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def show_error(message: str) -> None:
    ctypes.windll.user32.MessageBoxW(0, message, TITLE, 0x10)


def ask(message: str) -> bool:
    # MB_YESNO(0x04) + MB_ICONQUESTION(0x20)，返回 6 表示选了「是」。
    return ctypes.windll.user32.MessageBoxW(0, message, TITLE, 0x04 | 0x20) == 6


def ensure_server() -> bool:
    """保证 8765 上跑的是磁盘上的当前代码。

    只判断「端口有响应」是不够的：改完 Python 再打开应用，会直接连回旧进程，
    用户看到的还是旧报错。所以要比对源码指纹，不一致就重启后台服务。
    """
    if not is_running():
        start_server()
        return True

    if (health() or {}).get("build") == local_build():
        return True

    if has_active_job():
        show_error(
            "程序已更新，但还有任务正在处理。\n"
            "请等任务结束后关闭应用再重新打开，新版本才会生效。"
        )
        return True

    if not ask("程序代码已更新，需要重启后台服务才能生效。\n\n现在重启吗？"):
        return True

    if not stop_server():
        show_error("旧的后台服务未能结束。请在任务管理器结束 pythonw.exe 后重新打开应用。")
        return False
    start_server()
    return True


def open_native_window() -> None:
    import threading

    import webview

    def apply_window_icon() -> None:
        try:
            import win32api
            import win32con
            import win32gui

            handle = None
            for _ in range(100):
                handle = win32gui.FindWindow(None, TITLE)
                if handle:
                    break
                time.sleep(0.1)
            if not handle:
                return
            flags = win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
            big = win32api.LoadImage(0, str(ICON), win32con.IMAGE_ICON, 0, 0, flags)
            small = win32api.LoadImage(
                0, str(ICON), win32con.IMAGE_ICON, 16, 16, win32con.LR_LOADFROMFILE
            )
            # Keep references alive so the handles are not garbage collected.
            setattr(apply_window_icon, "_icons", (big, small))
            if big:
                win32gui.SendMessage(handle, win32con.WM_SETICON, win32con.ICON_BIG, big)
            if small:
                win32gui.SendMessage(handle, win32con.WM_SETICON, win32con.ICON_SMALL, small)
        except Exception:
            pass

    window = webview.create_window(
        TITLE,
        URL,
        width=1900,
        height=1300,
        min_size=(1410, 930),
        background_color="#f7f4fc",
    )
    threading.Thread(target=apply_window_icon, daemon=True).start()
    webview.start()


def main() -> None:
    try:
        if not ensure_server():
            return
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if is_running():
                break
            time.sleep(0.5)
        else:
            show_error("应用启动超时。请打开项目 data 文件夹中的 server.log 查看原因。")
            return
        try:
            open_native_window()
        except Exception:
            webbrowser.open(URL)
    except Exception as exc:
        show_error(f"应用启动失败：{exc}")


if __name__ == "__main__":
    main()

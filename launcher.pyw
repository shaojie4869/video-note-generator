from __future__ import annotations

import ctypes
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765"
TITLE = "学习视频总结"
ICON = BASE_DIR / "assets" / "icon.ico"


def is_running() -> bool:
    request = urllib.request.Request(f"{URL}/api/health", headers={"X-Study-Buddy": "1"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


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
        if not is_running():
            start_server()
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

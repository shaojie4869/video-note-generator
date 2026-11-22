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
_ICON_HANDLES: list[int] = []


def bind_app_identity() -> None:
    """声明应用身份并注册任务栏图标。

    AppUserModelID 不统一时，固定到任务栏会变成 Python 图标、还会和桌面快捷方式分裂；
    注册表里的 IconUri 是不打包 exe 也能让固定项用对图标的办法。
    """
    from study_buddy.app_identity import bind_process, register

    bind_process()
    pythonw = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    register(ICON, BASE_DIR / "launcher.pyw", pythonw)


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


def apply_window_icon() -> None:
    """把窗口图标换成应用图标，任务栏、Alt+Tab、标题栏一起生效。

    pywebview 的 WinForms 后端默认从 pythonw.exe 抠图标，所以任务栏一直是 Python 的图标。
    主路径是在 webview.start(icon=...) 里传图标；这里再用 Win32 兜一层，
    因为 WinForms 在窗口激活、从最小化还原时会重新下发 WM_SETICON。
    """
    if not ICON.exists():
        return
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = ctypes.c_void_p

    image_icon, lr_loadfromfile, lr_defaultsize = 1, 0x0010, 0x0040
    wm_seticon, icon_small, icon_big = 0x0080, 0, 1
    gclp_hicon, gclp_hiconsm = -14, -34
    big = user32.LoadImageW(0, str(ICON), image_icon, 0, 0, lr_loadfromfile | lr_defaultsize)
    small = user32.LoadImageW(0, str(ICON), image_icon, 16, 16, lr_loadfromfile)
    # 句柄被回收后图标会变成空白，必须一直持有。
    _ICON_HANDLES.extend(handle for handle in (big, small) if handle)

    for _ in range(24):
        handle = user32.FindWindowW(None, TITLE)
        if handle:
            if big:
                user32.SendMessageW(handle, wm_seticon, icon_big, big)
            if small:
                user32.SendMessageW(handle, wm_seticon, icon_small, small)
            # 同时改窗口类图标：任务栏和 Alt+Tab 读的是它，只发 WM_SETICON 有时不刷新。
            for index, value in ((gclp_hicon, big), (gclp_hiconsm, small)):
                if not value:
                    continue
                try:
                    setter = getattr(user32, "SetClassLongPtrW", None) or user32.SetClassLongW
                    setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
                    setter.restype = ctypes.c_void_p
                    setter(handle, index, ctypes.c_void_p(value))
                except (AttributeError, OSError, TypeError):
                    pass
        time.sleep(0.5)


def open_native_window() -> None:
    import threading

    import webview

    webview.create_window(
        TITLE,
        URL,
        width=1900,
        height=1300,
        min_size=(1410, 930),
        background_color="#f7f4fc",
    )
    threading.Thread(target=apply_window_icon, daemon=True).start()
    # icon 参数在 WinForms 后端就是给窗口设 Icon，比事后 SendMessage 更早生效。
    webview.start(icon=str(ICON) if ICON.exists() else None)


def main() -> None:
    bind_app_identity()
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

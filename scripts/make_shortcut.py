# -*- coding: utf-8 -*-
"""创建 / 修复桌面与开始菜单快捷方式（装好后跑一次，移动目录后也可以再跑）。

除了图标，还要给快捷方式打上启动器使用的 AppUserModelID：
Windows 靠它判断「同一个应用」，不一致时会出现固定到任务栏后变回 Python 图标、
或者桌面快捷方式和任务栏按钮分裂成两个的问题。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pythoncom
import win32com.client

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 直接运行 scripts 下的脚本时，项目根目录不在 sys.path 里
sys.path.insert(0, BASE)
from study_buddy.app_identity import APP_NAME, APP_USER_MODEL_ID, register  # noqa: E402

NAME = APP_NAME
LAUNCHER = os.path.join(BASE, "launcher.pyw")
PYTHONW = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
ICON = os.path.join(BASE, "assets", "icon.ico")

GPS_READWRITE = 0x00000002
IID_IPROPERTYSTORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
FMTID_APP_USER_MODEL = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
PID_APP_USER_MODEL_ID = 5

shell = win32com.client.Dispatch("WScript.Shell")


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]


class PROPVARIANT(ctypes.Structure):
    # x64 下共 24 字节：vt + 3 个保留位 + 16 字节联合体
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("value", ctypes.c_void_p),
        ("padding", ctypes.c_void_p),
    ]


def _guid(text: str) -> GUID:
    value = GUID()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(value))
    return value


def set_app_user_model_id(path: str) -> bool:
    """给 .lnk 打上 AppUserModelID。

    pywin32 的 propsys 包装在这台机器上一直报「参数错误」，所以这里直接走 COM：
    取 IPropertyStore，SetValue(PKEY_AppUserModel_ID)，再 Commit。
    """
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHGetPropertyStoreFromParsingName.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetPropertyStoreFromParsingName.restype = ctypes.c_long

    store = ctypes.c_void_p()
    iid = _guid(IID_IPROPERTYSTORE)
    result = shell32.SHGetPropertyStoreFromParsingName(
        path, None, GPS_READWRITE, ctypes.byref(iid), ctypes.byref(store)
    )
    if result < 0 or not store:
        print("  ! 无法打开快捷方式属性：", hex(result & 0xFFFFFFFF))
        return False

    # VT_LPWSTR(31)：字符串必须用 CoTaskMemAlloc 分配，之后 PropVariantClear 才能安全释放。
    raw = ctypes.create_unicode_buffer(APP_USER_MODEL_ID)
    size = ctypes.sizeof(raw)
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
    ole32.CoTaskMemAlloc.restype = ctypes.c_void_p
    ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
    block = ole32.CoTaskMemAlloc(size)
    if not block:
        print("  ! 分配属性内存失败")
        return False
    ctypes.memmove(block, ctypes.byref(raw), size)
    value = PROPVARIANT()
    value.vt = 31
    value.value = block

    key = PROPERTYKEY()
    key.fmtid = _guid(FMTID_APP_USER_MODEL)
    key.pid = PID_APP_USER_MODEL_ID

    vtable = ctypes.cast(
        store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    prototype = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)
    )
    set_value = prototype(vtable[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    try:
        if set_value(store, ctypes.byref(key), ctypes.byref(value)) < 0:
            print("  ! 写入 AppUserModelID 失败")
            return False
        if commit(store) < 0:
            print("  ! 保存 AppUserModelID 失败")
            return False
        return True
    finally:
        ole32.PropVariantClear(ctypes.byref(value))
        release(store)


def desktop_dir() -> str:
    """优先走 API 拿真实桌面路径（OneDrive 重定向时 USERPROFILE\\Desktop 可能是空的）。"""
    buffer = ctypes.create_unicode_buffer(260)
    # CSIDL_DESKTOPDIRECTORY = 0x0010
    if ctypes.windll.shell32.SHGetFolderPathW(0, 0x0010, 0, 0, buffer) == 0 and buffer.value:
        return buffer.value
    return os.path.join(os.environ["USERPROFILE"], "Desktop")


def start_menu_dir() -> str:
    return os.path.join(
        os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs"
    )


def pinned_dir() -> str:
    return os.path.join(
        os.environ["APPDATA"],
        "Microsoft",
        "Internet Explorer",
        "Quick Launch",
        "User Pinned",
        "TaskBar",
    )


def write_shortcut(path: str, description: str) -> None:
    shortcut = shell.CreateShortCut(path)
    shortcut.TargetPath = PYTHONW
    shortcut.Arguments = f'"{LAUNCHER}"'
    shortcut.WorkingDirectory = BASE
    shortcut.Description = description
    shortcut.IconLocation = f"{ICON},0"
    shortcut.Save()
    set_app_user_model_id(path)
    print("  ->", path)


def points_to_app(path: str) -> bool:
    try:
        shortcut = shell.CreateShortCut(path)
    except Exception:
        return False
    target = os.path.normcase(shortcut.TargetPath or "")
    arguments = shortcut.Arguments or ""
    return (
        os.path.normcase(BASE) in os.path.normcase(arguments)
        or "launcher.pyw" in arguments
        or NAME in os.path.basename(path)
        or (target.endswith("pythonw.exe") and "学习视频总结" in path)
    )


def repair_pinned() -> None:
    """已经固定在任务栏的快捷方式是另一份文件，得单独改，否则图标还是旧的。"""
    directory = pinned_dir()
    if not os.path.isdir(directory):
        return
    for entry in os.listdir(directory):
        if not entry.lower().endswith(".lnk"):
            continue
        path = os.path.join(directory, entry)
        if not points_to_app(path):
            continue
        shortcut = shell.CreateShortCut(path)
        shortcut.IconLocation = f"{ICON},0"
        shortcut.Save()
        set_app_user_model_id(path)
        print("  -> 更新已固定:", path)


def refresh_icon_cache() -> None:
    for args in (["-show"], ["-ClearIconCache"]):
        try:
            subprocess.run(["ie4uinit.exe", *args], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass


def main() -> None:
    pythoncom.CoInitialize()
    try:
        print("应用标识：")
        ok = register(Path(ICON), Path(LAUNCHER), Path(PYTHONW))
        print("  ->", APP_USER_MODEL_ID, "已注册" if ok else "注册失败（不影响图标，但固定项可能仍显示 Python 图标）")
        print("桌面快捷方式：")
        write_shortcut(os.path.join(desktop_dir(), f"{NAME}.lnk"), "学习视频总结 - 本地启动并打开窗口")
        print("开始菜单快捷方式：")
        write_shortcut(
            os.path.join(start_menu_dir(), f"{NAME}.lnk"),
            "学习视频总结 - 本地启动并打开窗口",
        )
        print("任务栏固定项：")
        repair_pinned()
        refresh_icon_cache()
        print("完成。若任务栏图标仍是旧的，注销或重启资源管理器后即可刷新。")
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()

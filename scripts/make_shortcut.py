# -*- coding: utf-8 -*-
"""Create a desktop shortcut for the Study Buddy app (run once)."""
from __future__ import annotations

import ctypes
import os

import win32com.client


def get_desktop() -> str:
    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    if os.path.isdir(desktop):
        return desktop
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(0, 0, 0, 0, buf)
    return buf.value


# 项目根目录：由脚本自身位置推导，避免写死盘符路径（本目录位于 <项目根>/scripts）
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
lnk = os.path.join(get_desktop(), "学习视频总结.lnk")
shell = win32com.client.Dispatch("WScript.Shell")
shortcut = shell.CreateShortCut(lnk)
shortcut.TargetPath = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
shortcut.Arguments = f'"{os.path.join(BASE, "launcher.pyw")}"'
shortcut.WorkingDirectory = BASE
shortcut.Description = "学习视频总结 - 本地启动并打开窗口"
shortcut.IconLocation = f'{os.path.join(BASE, "assets", "icon.ico")},0'
shortcut.Save()
print("created:", lnk, os.path.exists(lnk))

"""Windows 应用身份（AppUserModelID）。

任务栏用 AppUserModelID 决定「哪些窗口属于同一个应用」。pythonw.exe 启动的脚本
默认没有身份，于是任务栏把它当成 Python：固定到任务栏后变回 Python 图标，
和桌面快捷方式也会分裂成两个按钮。这里统一在启动器和快捷方式里写同一个 ID。
"""
from __future__ import annotations

import ctypes
import sys
import winreg
from pathlib import Path

APP_USER_MODEL_ID = "Shaojie.StudyVideoSummary"
APP_NAME = "学习视频总结"


def bind_process() -> None:
    """把当前进程标记成这个应用；必须在创建窗口之前调用。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def register(icon: Path, launcher: Path, python: Path) -> bool:
    """注册 AppUserModelID → 图标 / 重启命令（仅当前用户，不需要管理员）。

    固定到任务栏时，Windows 找不到对应快捷方式就退回可执行文件的图标，
    而这里的可执行文件是 pythonw.exe，于是又变回 Python 图标。
    写上 IconUri 与 RelaunchCommand 后，固定项才会拿到应用自己的图标。
    """
    if sys.platform != "win32":
        return False
    path = rf"Software\Classes\AppUserModelId\{APP_USER_MODEL_ID}"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
            winreg.SetValueEx(
                key, "RelaunchCommand", 0, winreg.REG_SZ, f'"{python}" "{launcher}"'
            )
            winreg.SetValueEx(key, "RelaunchDisplayNameResource", 0, winreg.REG_SZ, APP_NAME)
        return True
    except OSError:
        return False

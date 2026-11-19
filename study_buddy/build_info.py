"""源码指纹：让启动器判断后台服务是不是还在跑旧代码。

背景：Python 代码只在进程启动时加载，而页面每次刷新都重读静态文件。
所以「改完 Python 后点重启」如果只是重新打开窗口、后台进程没换，
用户会一直看到旧行为（曾经表现为计数接口 404、B 站下载报格式不可用）。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def source_files() -> list[Path]:
    """参与指纹的源码文件：只有 Python 需要重启，静态文件刷新即可。"""
    files = [PROJECT_DIR / "app.py", PROJECT_DIR / "launcher.pyw"]
    files.extend(sorted((PROJECT_DIR / "study_buddy").glob("*.py")))
    return [path for path in files if path.exists()]


def build_id() -> str:
    """用「最新修改时间 + 文件数」组成指纹，够用且不依赖 git。"""
    latest = 0.0
    count = 0
    for path in source_files():
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
        count += 1
    return f"{int(latest)}-{count}"

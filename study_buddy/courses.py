"""课程归组规则：把「一次任务」归到「一门课」下。

课件的可靠信号是结构，不是标题语义：
- B 站：同一个 BV 的不同分P 必然属于同一门课；单个 BV 且没有分P 的视频
  多半是独立视频（A股复盘、心理科普之类），放进「杂项」。
- 本地：父目录就是课程名（课程文件夹里通常有多个视频），
  单独一个文件放在下载目录时不具备课程特征，同样进「杂项」。
"""

from __future__ import annotations

import re
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".webm", ".ts"}

# 1-5 / p11 3-4 / 3.4 之类；允许中文间隔号
SEQUENCE_RE = re.compile(r"(?:^|[\s_\-])p?(\d{1,3})\s*[-–—．.]\s*(\d{1,3})")
PREFIX_SEQUENCE_RE = re.compile(r"^\s*p?(\d{1,3})(?:\s|$)")
# 中文讲次：第 10 讲 / 第三章
CHAPTER_RE = re.compile(r"第\s*(\d{1,3})\s*[讲章课节]")
# 目录名带这些词，基本可以认定是课程资料目录
COURSE_DIR_RE = re.compile(r"(课|讲|教程|全集)")
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
P_SUFFIX_RE = re.compile(r"\s+p\d{1,3}\b.*$", re.IGNORECASE)


def parse_sequence(title: str) -> str:
    """从标题里取章节号，如 ``3-4``；取不到返回空串。

    排序用数字元组比较，不能用字符串比较，否则 ``1-10`` 会排在 ``1-2`` 前面。
    """
    text = title or ""
    match = SEQUENCE_RE.search(text)
    if match:
        return f"{int(match.group(1))}-{int(match.group(2))}"
    match = PREFIX_SEQUENCE_RE.match(text)
    if match:
        return f"{int(match.group(1))}-0"
    match = CHAPTER_RE.search(text)
    if match:
        return f"{int(match.group(1))}-0"
    return ""


def sequence_sort_key(sequence: str) -> tuple[int, int, str]:
    """章节号排序键：无编号的排最后，同号时按原字符串兜底。"""
    if not sequence:
        return (9999, 9999, "")
    major, _, minor = sequence.partition("-")
    try:
        return (int(major), int(minor or 0), sequence)
    except ValueError:
        return (9998, 9998, sequence)


def clean_course_name(title: str) -> str:
    """从分P 标题里还原课程名：去掉 ``p10 3-4 cache`` 这类尾部。"""
    name = P_SUFFIX_RE.sub("", (title or "").strip())
    return name.strip() or (title or "").strip()


def bilibili_bvid(url: str) -> str:
    match = BV_RE.search(url or "")
    return match.group(1) if match else ""


def local_video_count(folder: Path) -> int:
    try:
        return sum(
            1
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        )
    except OSError:
        return 0


def auto_group(
    source_type: str, source_ref: str, title: str, entry_index: int
) -> tuple[str, str, str]:
    """推断归属，返回 ``(kind, source_key, name)``。

    kind 为 ``course`` 或 ``misc``；source_key 用于把后续同类任务归到同一门课。
    """
    if source_type == "local":
        path = Path(source_ref)
        folder = path.parent
        name = folder.name or ""
        # 目录里已经放不下两个视频时（例如文件被移走），退回目录名特征：
        # 「操作系统精讲课」显然是课程，「A股复盘」不是。
        if local_video_count(folder) >= 2 or COURSE_DIR_RE.search(name):
            return ("course", f"dir:{folder}", name or "未命名课程")
        return ("misc", "", "")

    bv = bilibili_bvid(source_ref)
    has_part = "p=" in (source_ref or "") or int(entry_index or 0) > 0
    if bv and has_part:
        return ("course", f"bv:{bv}", clean_course_name(title))
    return ("misc", "", "")

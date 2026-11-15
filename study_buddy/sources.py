from __future__ import annotations

import html
import json
import math
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import imageio_ffmpeg

from .config import CACHE_DIR

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".m4v", ".webm", ".ts"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | {".m4a", ".mp3", ".wav", ".opus", ".aac", ".flac"}


class SourceError(Exception):
    pass


@dataclass
class PreparedSource:
    title: str
    media_path: Path | None
    transcript: str = ""
    transcript_origin: str = ""


def validate_local_video(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise SourceError("本地文件不存在，请重新选择。")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise SourceError("暂不支持该文件格式，请选择常见视频文件。")
    return path


def validate_bilibili_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host == "b23.tv" or host == "bilibili.com" or host.endswith(".bilibili.com")
    ):
        raise SourceError("请输入有效的 B站视频链接。")
    return url


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _clean_subtitle_line(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).replace("\xa0", " ").strip()


def parse_subtitle(content: bytes, format_name: str) -> str:
    text = content.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return ""
    if format_name == "json" or text.startswith("{"):
        return _parse_json_subtitle(text)
    if format_name == "ttml" or text.startswith("<?xml"):
        return _parse_ttml(text)
    return _parse_srt_vtt(text)


def _parse_json_subtitle(text: str) -> str:
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("body"), list):
        return "\n".join(
            f"[{format_timestamp(float(item.get('from', 0)))}] {_clean_subtitle_line(str(item.get('content', '')))}"
            for item in data["body"]
            if item.get("content")
        )
    events = data.get("events", []) if isinstance(data, dict) else []
    lines: list[str] = []
    for event in events:
        parts = event.get("segs") or []
        value = _clean_subtitle_line("".join(str(part.get("utf8", "")) for part in parts))
        if value:
            lines.append(f"[{format_timestamp(float(event.get('tStartMs', 0)) / 1000)}] {value}")
    return "\n".join(lines)


def _parse_ttml(text: str) -> str:
    root = ET.fromstring(text)
    lines: list[str] = []
    for node in root.iter():
        if not node.tag.lower().endswith("p"):
            continue
        value = _clean_subtitle_line("".join(node.itertext()))
        begin = node.attrib.get("begin", "0")
        match = re.match(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", begin)
        seconds = 0.0
        if match:
            seconds = (int(match.group(1) or 0) * 3600) + int(match.group(2)) * 60 + float(match.group(3))
        if value:
            lines.append(f"[{format_timestamp(seconds)}] {value}")
    return "\n".join(lines)


def _parse_srt_vtt(text: str) -> str:
    lines: list[str] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        rows = block.strip().splitlines()
        if not rows or rows[0].strip() == "WEBVTT":
            continue
        timestamp_row = next((i for i, row in enumerate(rows) if "-->" in row), None)
        if timestamp_row is None:
            continue
        match = re.search(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?", rows[timestamp_row])
        seconds = 0.0
        if match:
            seconds = (int(match.group(1) or 0) * 3600) + int(match.group(2)) * 60 + int(match.group(3))
        value = _clean_subtitle_line(" ".join(rows[timestamp_row + 1 :]))
        if value and (not lines or not lines[-1].endswith(value)):
            lines.append(f"[{format_timestamp(seconds)}] {value}")
    return "\n".join(lines)


def ffmpeg_executable() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def extract_audio(media_path: Path, output_path: Path) -> Path:
    command = [
        ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)
    if result.returncode != 0 or not output_path.exists():
        detail = (result.stderr or "无法读取音频").strip().splitlines()[-1]
        raise SourceError(f"音频提取失败：{detail}")
    return output_path


def _probe_duration(media_path: Path) -> float:
    """读取媒体总时长（秒）。imageio-ffmpeg 不带 ffprobe，从 ffmpeg 的 stderr 解析。"""
    command = [ffmpeg_executable(), "-hide_banner", "-i", str(media_path)]
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        return 0.0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def extract_keyframes(
    video_path: Path, output_dir: Path, interval_seconds: int, max_frames: int
) -> list[Path]:
    """按固定间隔从视频中截取关键帧，用于视觉模型读取课件画面。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    interval = max(5, int(interval_seconds))
    cap = max(1, int(max_frames))
    duration = _probe_duration(video_path)
    if duration > 0 and cap > 1:
        # 固定间隔配帧数上限，会让长视频只截到前 cap×interval 秒，后半节课没有画面。
        # 按实际时长反算间隔，保证整段视频都被覆盖到。
        interval = max(interval, math.ceil(duration / cap))
    pattern = output_dir / "frame_%03d.jpg"
    command = [
        ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps=1/{interval},scale='min(1280,iw)':-2",
        "-frames:v",
        str(cap),
        "-q:v",
        "3",
        str(pattern),
    ]
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)
    frames = sorted(output_dir.glob("frame_*.jpg"))
    if result.returncode != 0 and not frames:
        detail = (result.stderr or "无法截取画面").strip().splitlines()[-1]
        raise SourceError(f"画面截取失败：{detail}")
    return [frame for frame in frames if frame.stat().st_size > 0][:cap]


class BilibiliClient:
    def _options(self) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "socket_timeout": 30,
            "retries": 2,
        }

    @staticmethod
    def _select(info: dict[str, Any], entry_index: int) -> dict[str, Any]:
        entries = [entry for entry in info.get("entries", []) if entry]
        if entries:
            if entry_index >= len(entries):
                raise SourceError("选择的分P不存在，请重新读取视频信息。")
            return entries[entry_index]
        if entry_index:
            raise SourceError("该链接没有多个分P，请将分P序号设为 0。")
        return info

    @staticmethod
    def _entry_url(info: dict[str, Any], fallback: str) -> str:
        return info.get("webpage_url") or info.get("original_url") or fallback

    def info(self, url: str) -> dict[str, Any]:
        import yt_dlp

        url = validate_bilibili_url(url)
        try:
            with yt_dlp.YoutubeDL(self._options()) as ydl:
                extracted = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise SourceError(f"无法读取 B站视频：{exc}") from exc
        entries = [entry for entry in extracted.get("entries", []) if entry]
        if not entries:
            entries = [extracted]
        return {
            "title": extracted.get("title") or entries[0].get("title") or "B站课程",
            "entries": [
                {
                    "index": index,
                    "title": entry.get("title") or f"分P {index + 1}",
                    "duration": entry.get("duration"),
                    "url": self._entry_url(entry, url),
                }
                for index, entry in enumerate(entries[:200])
            ],
        }

    def prepare(self, url: str, entry_index: int, job_id: str, want_video: bool = False) -> PreparedSource:
        import yt_dlp

        url = validate_bilibili_url(url)
        work_dir = CACHE_DIR / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        options = self._options()
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                extracted = ydl.extract_info(url, download=False)
                is_playlist = bool([entry for entry in extracted.get("entries", []) if entry])
                selected = self._select(extracted, entry_index)
                selected_url = self._entry_url(selected, url)
                if "formats" not in selected and selected_url != url:
                    selected = ydl.extract_info(selected_url, download=False)
                title = selected.get("title") or extracted.get("title") or "B站课程"
                subtitle = self._read_available_subtitle(ydl, selected)
                if subtitle and not want_video:
                    return PreparedSource(title=title, media_path=None, transcript=subtitle, transcript_origin="subtitle")
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"读取 B站视频失败：{exc}") from exc
        try:
            download_options = self._options()
            if want_video:
                download_options.update(
                    {
                        "noplaylist": not is_playlist,
                        "format": "worst[height<=480][ext=mp4]/worst[height<=480]/worst",
                        "outtmpl": str(work_dir / "video.%(ext)s"),
                        "overwrites": True,
                    }
                )
            else:
                download_options.update(
                    {
                        "noplaylist": not is_playlist,
                        "format": "bestaudio/best",
                        "outtmpl": str(work_dir / "audio.%(ext)s"),
                        "overwrites": True,
                    }
                )
            if is_playlist:
                download_options["playlist_items"] = str(entry_index + 1)
                download_url = url
            else:
                download_url = selected_url
            with yt_dlp.YoutubeDL(download_options) as ydl:
                ydl.download([download_url])
        except Exception as exc:
            kind = "视频" if want_video else "音频"
            raise SourceError(f"该视频没有可用字幕，{kind}获取也失败：{exc}") from exc
        wanted = VIDEO_EXTENSIONS if want_video else MEDIA_EXTENSIONS
        media_files = [
            path for path in work_dir.iterdir() if path.suffix.lower() in wanted and path.stat().st_size > 0
        ]
        if not media_files:
            kind = "视频" if want_video else "音频"
            raise SourceError(f"未找到可处理的{kind}文件。")
        media_path = max(media_files, key=lambda p: p.stat().st_mtime)
        if subtitle:
            # 有字幕时以字幕为转写内容，同时保留下载的视频用于截取画面。
            return PreparedSource(title=title, media_path=media_path, transcript=subtitle, transcript_origin="subtitle")
        return PreparedSource(title=title, media_path=media_path)

    def _read_available_subtitle(self, ydl: Any, info: dict[str, Any]) -> str:
        groups = [info.get("subtitles") or {}, info.get("automatic_captions") or {}]
        for group in groups:
            if not group:
                continue
            languages = sorted(
                group,
                key=lambda name: (0 if name.lower().startswith(("zh", "cmn")) else 1, name),
            )
            for language in languages:
                formats = sorted(
                    group[language],
                    key=lambda item: (0 if item.get("ext") in {"json", "srt", "vtt"} else 1, item.get("ext", "")),
                )
                for subtitle_format in formats:
                    subtitle_url = subtitle_format.get("url")
                    if not subtitle_url:
                        continue
                    try:
                        response = ydl.urlopen(subtitle_url)
                        content = response.read()
                        parsed = parse_subtitle(content, subtitle_format.get("ext", ""))
                        if len(parsed.strip()) > 20:
                            return parsed
                    except Exception:
                        continue
        return ""

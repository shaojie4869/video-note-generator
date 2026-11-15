import json

import pytest

from study_buddy.sources import SourceError, parse_subtitle, validate_bilibili_url


def test_parse_bilibili_json_subtitle():
    content = json.dumps(
        {"body": [{"from": 1.2, "content": "第一章"}, {"from": 65, "content": "第二节"}]},
        ensure_ascii=False,
    ).encode()
    assert parse_subtitle(content, "json") == "[00:00:01] 第一章\n[00:01:05] 第二节"


def test_parse_srt_removes_tags_and_numbers():
    content = b"1\n00:00:01,000 --> 00:00:02,000\n<b>Hello</b>\n\n2\n00:00:03,000 --> 00:00:04,000\nWorld\n"
    assert parse_subtitle(content, "srt") == "[00:00:01] Hello\n[00:00:03] World"


def test_json3_subtitle():
    data = {"events": [{"tStartMs": 2500, "segs": [{"utf8": "中文"}, {"utf8": "课程"}]}]}
    assert parse_subtitle(json.dumps(data).encode(), "json3") == "[00:00:02] 中文课程"


@pytest.mark.parametrize("url", ["https://www.bilibili.com/video/BV1xx", "https://b23.tv/abc"])
def test_valid_bilibili_urls(url):
    assert validate_bilibili_url(url) == url


@pytest.mark.parametrize("url", ["https://example.com/video", "not-a-url", "file:///C:/a.mp4"])
def test_reject_non_bilibili_urls(url):
    with pytest.raises(SourceError):
        validate_bilibili_url(url)


def test_extract_keyframes_from_generated_video(tmp_path):
    import subprocess

    from study_buddy.sources import extract_keyframes, ffmpeg_executable

    video = tmp_path / "sample.mp4"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=6:size=320x240:rate=10",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    frames = extract_keyframes(video, tmp_path / "frames", interval_seconds=2, max_frames=3)
    assert 1 <= len(frames) <= 3
    assert all(frame.suffix == ".jpg" and frame.stat().st_size > 0 for frame in frames)

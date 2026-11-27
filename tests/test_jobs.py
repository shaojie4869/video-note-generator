import asyncio
import subprocess
import threading
import time
from types import SimpleNamespace

from study_buddy.db import Database
from study_buddy.hardware import HardwareProfile
from study_buddy.jobs import JobManager, _Media
from study_buddy.schemas import JobCreate


class StubSettings:
    def get(self):
        return {
            "frames_enabled": False,
            "frame_interval": 60,
            "max_frames": 24,
            "whisper_model": "small",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
        }


class StubSummary:
    """只验证流水线接线，不真的调模型。"""

    def summarize(self, title, transcript, visual_notes, progress, event, job_id=None):
        progress(50, "正在生成学习笔记")
        return SimpleNamespace(text="## 笔记正文", provider="api")

    def generate_questions(self, *args, **kwargs):
        return []


class StubTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio_path, model, device, compute_type, progress, event):
        self.calls += 1
        assert audio_path.exists() and audio_path.stat().st_size > 0
        if progress:
            progress(100)
        return "[00:00] 测试转写"


def _profile(*, prepare=2, transcribe=1, summarize=2) -> HardwareProfile:
    return HardwareProfile(
        cpu_count=8,
        ram_gb=16.0,
        gpu="",
        vram_gb=0.0,
        has_gpu=False,
        prepare_slots=prepare,
        transcribe_slots=transcribe,
        summarize_slots=summarize,
        max_batch=30,
    )


def _prepare_manager(tmp_path, name="history.db", profile=None):
    database = Database(tmp_path / name)
    manager = JobManager(
        database,
        settings=StubSettings(),
        summary=None,
        hardware=profile or _profile(),
    )
    manager._prepare_visual = lambda *args: None
    return database, manager


def _run_three(manager, jobs):
    for job in jobs:
        manager._start(job["id"], False)
    return asyncio.gather(*manager._tasks.values())


def test_batch_keeps_summary_concurrency_under_limit(tmp_path):
    """批量生成时「出笔记」并发不能超过闸门数，否则会把 API 和内存同时打满。"""

    async def scenario():
        database, manager = _prepare_manager(tmp_path, profile=_profile(summarize=2))
        manager._prepare_media = lambda job_id, event: _Media(transcript="转写")
        active, peak = 0, 0

        def fake_summarize(job_id, event):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.08)
            active -= 1
            database.update_job(job_id, status="completed", stage="已完成")

        manager._summarize = fake_summarize
        jobs = [database.create_job("local", f"D:/{i}.mp4", f"第{i}课", f"key{i}", 0) for i in range(5)]

        await _run_three(manager, jobs)

        assert peak == 2
        assert all(database.get_job(job["id"])["status"] == "completed" for job in jobs)

    asyncio.run(scenario())


def test_batch_serializes_transcription(tmp_path):
    """转写吃满显卡，多任务必须排队，不能同时跑两份。"""

    async def scenario():
        database, manager = _prepare_manager(tmp_path, profile=_profile(transcribe=1))
        manager._prepare_media = lambda job_id, event: _Media(audio_path=tmp_path / "audio.wav")
        active, peak = 0, 0

        def fake_transcribe(job_id, media, event):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.06)
            active -= 1
            return "转写完成"

        manager._run_transcription = fake_transcribe
        manager._summarize = lambda job_id, event: database.update_job(
            job_id, status="completed", stage="已完成"
        )
        jobs = [database.create_job("local", f"D:/{i}.mp4", f"第{i}课", f"key{i}", 0) for i in range(4)]

        await _run_three(manager, jobs)

        assert peak == 1
        assert all(database.get_job(job["id"])["transcript"] == "转写完成" for job in jobs)

    asyncio.run(scenario())


def _make_video(path):
    """用 ffmpeg 生成 2 秒带音轨的小视频，用来跑真实的取音轨流程。"""
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=160x120:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-shortest", "-c:v", "libx264", "-c:a", "aac",
            str(path),
        ],
        capture_output=True,
        check=True,
    )


def test_local_video_runs_through_every_stage(tmp_path):
    """单个视频的真实链路：读文件 → 抽音轨 → 转写 → 出笔记（转写和模型用桩）。"""

    async def scenario():
        video = tmp_path / "1-1 测试课.mp4"
        _make_video(video)
        database = Database(tmp_path / "history.db")
        transcriber = StubTranscriber()
        manager = JobManager(
            database,
            settings=StubSettings(),
            summary=StubSummary(),
            transcriber=transcriber,
            hardware=_profile(),
        )

        job, reused = await manager.create(
            JobCreate(source_type="local", source_ref=str(video), title="1-1 测试课")
        )
        assert reused is False

        await asyncio.wait_for(manager._tasks[job["id"]], timeout=60)

        final = database.get_job(job["id"])
        assert final["status"] == "completed", final["error"]
        assert "测试转写" in final["transcript"]
        assert final["summary"] == "## 笔记正文"
        assert transcriber.calls == 1

    asyncio.run(scenario())


def test_queued_job_can_be_cancelled_before_it_starts(tmp_path):
    """排队中的任务点取消要立刻生效，不能等前面那个跑完才响应。"""

    async def scenario():
        database, manager = _prepare_manager(tmp_path, profile=_profile(summarize=1))
        manager._prepare_media = lambda job_id, event: _Media(transcript="转写")
        release = threading.Event()

        def slow_summarize(job_id, event):
            release.wait(3)
            database.update_job(job_id, status="completed", stage="已完成")

        manager._summarize = slow_summarize
        first = database.create_job("local", "D:/a.mp4", "第一节", "key-a", 0)
        second = database.create_job("local", "D:/b.mp4", "第二节", "key-b", 0)

        manager._start(first["id"], False)
        manager._start(second["id"], False)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if "排队" in (database.get_job(second["id"])["stage"] or ""):
                break
            await asyncio.sleep(0.02)
        assert "排队" in database.get_job(second["id"])["stage"]

        manager.cancel(second["id"])
        await asyncio.wait_for(manager._tasks[second["id"]], timeout=2)
        assert database.get_job(second["id"])["status"] == "cancelled"

        release.set()
        await asyncio.wait_for(manager._tasks[first["id"]], timeout=3)
        assert database.get_job(first["id"])["status"] == "completed"

    asyncio.run(scenario())


def test_long_transcript_goes_straight_to_summary(tmp_path):
    """超过 5 万字也不再停下来等确认——用户点了开始就是要生成。"""
    database = Database(tmp_path / "history.db")
    manager = JobManager(database, settings=StubSettings(), summary=None)
    job = database.create_job("local", "D:/a.mp4", "长课", "key", 0)
    database.update_job(job["id"], transcript="字" * 60000)

    summarized = []
    manager._summarize = lambda job_id, event: summarized.append(job_id)

    manager._prepare_transcript(job["id"], threading.Event())

    assert summarized == [job["id"]]
    assert database.get_job(job["id"])["status"] != "waiting_confirmation"


def test_confirm_waits_for_previous_task_before_start(tmp_path):
    async def scenario():
        database = Database(tmp_path / "history.db")
        manager = JobManager(database, settings=None, summary=None)
        job = database.create_job("bilibili", "https://b23.tv/a", "课程", "key", 0)
        database.update_job(job["id"], status="waiting_confirmation", transcript="长转写")
        previous = asyncio.create_task(asyncio.sleep(0.03))
        manager._tasks[job["id"]] = previous
        started = []
        manager._start = lambda *args: started.append(args)
        result = await manager.confirm(job["id"])
        assert previous.done()
        assert result["status"] == "queued"
        assert started == [(job["id"], True)]

    asyncio.run(scenario())


def test_force_reuses_transcript_from_failed_job(tmp_path, monkeypatch):
    async def scenario():
        database = Database(tmp_path / "history.db")
        manager = JobManager(database, settings=None, summary=None)
        failed = database.create_job("local", "D:/a.mp4", "课程", "cache-key", 0)
        database.update_job(failed["id"], status="failed", transcript="旧转写内容")
        manager._start = lambda *args: None
        monkeypatch.setattr(
            JobManager, "cache_key", staticmethod(lambda payload: "cache-key")
        )
        payload = JobCreate(source_type="local", source_ref="D:/a.mp4", force=True)
        job, reused = await manager.create(payload)
        assert reused is False
        assert job["transcript"] == "旧转写内容"

    asyncio.run(scenario())


def test_duplicate_submit_is_blocked_while_running(tmp_path, monkeypatch):
    """同一个视频重复点「开始生成」不应该再起一个任务。"""

    async def scenario():
        database = Database(tmp_path / "history.db")
        manager = JobManager(database, settings=None, summary=None)
        manager._start = lambda *args: None
        monkeypatch.setattr(
            JobManager, "cache_key", staticmethod(lambda payload: "cache-key")
        )
        first, reused = await manager.create(
            JobCreate(source_type="local", source_ref="D:/a.mp4")
        )
        assert reused is False

        second, reused_again = await manager.create(
            JobCreate(source_type="local", source_ref="D:/a.mp4")
        )
        assert reused_again is True
        assert second["id"] == first["id"]

        # 正在跑的时候强制重新生成也要拒绝，否则会并发跑两份
        try:
            await manager.create(
                JobCreate(source_type="local", source_ref="D:/a.mp4", force=True)
            )
        except ValueError as exc:
            assert "正在生成中" in str(exc)
        else:
            raise AssertionError("运行中的任务不该允许 force 再建一个")

    asyncio.run(scenario())


def test_refresh_bilibili_title_replaces_placeholder(tmp_path):
    """重新生成会复制原任务标题，占位标题必须在这一步换成真实标题。"""

    class StubClient:
        def info(self, url):
            return {"title": "合集标题", "entries": [{"title": "BOK408 p10 3-4 cache"}]}

    database = Database(tmp_path / "history.db")
    manager = JobManager(database, settings=None, summary=None)
    manager.bilibili = StubClient()
    job = database.create_job(
        "bilibili", "https://www.bilibili.com/video/BV1x/", "B站课程", "key", 0
    )

    manager._refresh_bilibili_title(job["id"], job)

    assert database.get_job(job["id"])["title"] == "BOK408 p10 3-4 cache"


def test_concurrent_submit_creates_only_one_job(tmp_path, monkeypatch):
    """两个请求同时提交同一个视频，「查重 + 建档」必须原子，否则会各建一份。"""

    async def scenario():
        database = Database(tmp_path / "history.db")
        manager = JobManager(database, settings=None, summary=None)
        manager._start = lambda *args: None
        monkeypatch.setattr(
            JobManager, "cache_key", staticmethod(lambda payload: "same-key")
        )
        payload = JobCreate(source_type="local", source_ref="D:/a.mp4")
        results = await asyncio.gather(manager.create(payload), manager.create(payload))
        ids = {job["id"] for job, _ in results}
        assert len(ids) == 1
        assert sum(1 for _, reused in results if reused) == 1
        assert len(database.list_jobs()) == 1

    asyncio.run(scenario())

import asyncio

from study_buddy.db import Database
from study_buddy.jobs import JobManager
from study_buddy.schemas import JobCreate


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
        assert started == [(job["id"], True, True)]

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

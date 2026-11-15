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

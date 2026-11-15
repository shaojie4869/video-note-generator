from study_buddy.db import Database


def test_completed_history_is_reused(tmp_path):
    database = Database(tmp_path / "history.db")
    job = database.create_job("bilibili", "https://b23.tv/a", "课程", "cache-key", 0)
    assert database.find_completed("cache-key") is None
    database.update_job(job["id"], status="completed", summary="# 笔记", transcript="转写", provider="api", visual_notes="画面要点")
    reused = database.find_completed("cache-key")
    assert reused["id"] == job["id"]
    assert reused["summary"] == "# 笔记"
    database.mark_interrupted()
    assert database.get_job(job["id"])["status"] == "completed"
    assert database.delete_job(job["id"]) is True
    assert database.get_job(job["id"]) is None


def test_usage_log_records_and_aggregates(tmp_path):
    database = Database(tmp_path / "history.db")
    database.add_usage("job1", "summary", "glm-5.3-flash", 100, 50, 150)
    database.add_usage("job1", "vision", "glm-5.3-flash", 900, 100, 1000)
    database.add_usage("job2", "summary", "glm-4-flash", 10, 5, 15)

    single = database.job_usage("job1")
    assert single["total_tokens"] == 1150
    assert {item["purpose"]: item["tokens"] for item in single["by_purpose"]} == {
        "summary": 150,
        "vision": 1000,
    }

    summary = database.usage_summary()
    assert summary["month"]["tokens"] == 1165
    assert summary["month"]["calls"] == 3
    assert summary["month"]["prompt_tokens"] == 1010
    assert summary["today"]["tokens"] == 1165
    assert database.job_usage("missing")["total_tokens"] == 0

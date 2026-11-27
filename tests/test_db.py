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


def test_search_notes_matches_summary_visual_and_title(tmp_path):
    database = Database(tmp_path / "history.db")
    course = database.create_course("操作系统", "course", "os-key")["id"]
    first = database.create_job("local", "/videos/1.mp4", "1-1 进程", "ck-1", 0, course)
    database.update_job(
        first["id"],
        status="completed",
        summary="# 进程\n\n进程是资源分配的基本单位，进程和程序不同。",
    )
    second = database.create_job("local", "/videos/2.mp4", "1-2 线程", "ck-2", 0, course)
    database.update_job(
        second["id"],
        status="completed",
        summary="# 线程\n\n线程是调度单位。",
        visual_notes="课件：进程与线程对比表",
    )
    third = database.create_job("local", "/videos/3.mp4", "1-3 调度", "ck-3", 0, course)
    database.update_job(third["id"], status="completed", summary="# 调度\n\n进程调度算法。")

    results = database.search_notes("进程")
    by_id = {item["id"]: item for item in results}
    assert set(by_id) == {first["id"], second["id"], third["id"]}
    assert by_id[first["id"]]["field"] == "summary"
    assert by_id[second["id"]]["field"] == "visual"
    assert by_id[first["id"]]["course_name"] == "操作系统"
    assert "进程" in by_id[first["id"]]["snippet"]
    assert by_id[first["id"]]["occurrences"] >= 2

    title_only = database.search_notes("线程")
    assert [item["id"] for item in title_only][0] == second["id"]
    assert database.search_notes("") == []
    assert database.search_notes("不存在的知识点") == []


def test_search_notes_keeps_one_note_per_source(tmp_path):
    database = Database(tmp_path / "history.db")
    old = database.create_job("local", "/videos/1.mp4", "1-1 进程", "same-key", 0)
    database.update_job(old["id"], status="completed", summary="旧内容 进程")
    new = database.create_job("local", "/videos/1.mp4", "1-1 进程", "same-key", 0)
    database.update_job(new["id"], status="completed", summary="新内容 进程")

    results = database.search_notes("进程")
    assert [item["id"] for item in results] == [new["id"]]


def test_highlights_are_stored_listed_and_removed_with_job(tmp_path):
    database = Database(tmp_path / "history.db")
    job = database.create_job("local", "/videos/1.mp4", "课程", "ck", 0)
    saved = database.add_highlight(job["id"], "summary", "资源分配", 12, "green")
    assert saved["quote"] == "资源分配"
    assert saved["color"] == "green"
    assert [item["id"] for item in database.list_highlights(job["id"])] == [saved["id"]]

    assert database.delete_highlight(job["id"], saved["id"]) is True
    assert database.list_highlights(job["id"]) == []

    kept = database.add_highlight(job["id"], "visual", "对比表", 0, "blue")
    database.delete_job(job["id"])
    assert database.list_highlights(job["id"]) == []
    assert database.delete_highlight(job["id"], kept["id"]) is False


def test_usage_today_uses_local_midnight(tmp_path):
    """「今日」按本机时区的零点切，不能按 UTC 切（否则早上 8 点前算到昨天）。"""
    from datetime import datetime, timedelta, timezone

    database = Database(tmp_path / "history.db")
    local_start = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    inside = local_start + timedelta(minutes=30)
    before = local_start - timedelta(minutes=30)

    def insert(moment):
        with database._connect() as connection:
            connection.execute(
                "INSERT INTO usage_log (job_id, purpose, model, total_tokens, created_at) "
                "VALUES ('', 'test', 'm', 100, ?)",
                (moment.astimezone(timezone.utc).isoformat(),),
            )

    insert(inside)
    insert(before)
    summary = database.usage_summary()
    assert summary["today"]["calls"] == 1
    assert summary["today"]["tokens"] == 100

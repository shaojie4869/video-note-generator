from fastapi.testclient import TestClient

import app as application

HEADERS = {"X-Study-Buddy": "1"}


def test_opening_page_does_not_create_jobs_or_call_models():
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "学习视频总结" in page.text
        result = client.get("/api/jobs", headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["jobs"] == []


def test_local_api_requires_guard_header():
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/jobs").status_code == 403
        assert client.get("/api/health", headers=HEADERS).status_code == 200


def test_rejects_rebinding_host():
    with TestClient(application.app) as client:
        assert client.get("/", headers={"X-Study-Buddy": "1"}).status_code == 403


def test_job_creation_requires_explicit_post(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    video = tmp_path / "course.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/jobs", headers=HEADERS).json()["jobs"] == []
        response = client.post(
            "/api/jobs",
            headers=HEADERS,
            json={"source_type": "local", "source_ref": str(video), "title": "测试课程"},
        )
        assert response.status_code == 200
        assert response.json()["job"]["status"] == "queued"
        assert len(client.get("/api/jobs", headers=HEADERS).json()["jobs"]) == 1


def test_batch_creates_each_video_and_skips_duplicates(tmp_path, monkeypatch):
    """批量提交：每个视频一个任务；同一视频重复出现在同一次提交里只留一个。"""
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    videos = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for video in videos:
        video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/jobs/batch",
            headers=HEADERS,
            json={
                "items": [
                    {"source_type": "local", "source_ref": str(videos[0]), "title": "1-1 绪论"},
                    {"source_type": "local", "source_ref": str(videos[1]), "title": "1-2 发展"},
                    {"source_type": "local", "source_ref": str(videos[0]), "title": "重复提交"},
                ]
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["created"] == 2
        assert body["reused"] == 1
        assert body["errors"] == []
        listed = client.get("/api/jobs", headers=HEADERS).json()["jobs"]
        ours = [
            job for job in listed
            if job["source_ref"] in {str(videos[0]), str(videos[1])}
        ]
        assert len(ours) == 2
        for job in ours:
            client.delete(f"/api/jobs/{job['id']}", headers=HEADERS)


def test_batch_reports_single_failure_without_dropping_others(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    video = tmp_path / "good.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        body = client.post(
            "/api/jobs/batch",
            headers=HEADERS,
            json={
                "items": [
                    {"source_type": "local", "source_ref": str(video), "title": "正常"},
                    {"source_type": "local", "source_ref": str(tmp_path / "missing.mp4"), "title": "不存在"},
                ]
            },
        ).json()
        assert body["created"] == 1
        assert len(body["errors"]) == 1
        assert body["errors"][0]["index"] == 1
        listed = client.get("/api/jobs", headers=HEADERS).json()["jobs"]
        for job in listed:
            if job["source_ref"] == str(video):
                client.delete(f"/api/jobs/{job['id']}", headers=HEADERS)


def test_system_profile_exposes_concurrency_plan():
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        data = client.get("/api/system", headers=HEADERS).json()
        assert data["hardware"]["cpu_count"] >= 1
        assert data["slots"]["transcribe"] == 1
        assert data["slots"]["summarize"] >= 1
        assert data["max_batch"] == 30
        assert "model" in data["whisper"]


def test_batch_rejects_empty_or_oversized_payload():
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        empty = client.post("/api/jobs/batch", headers=HEADERS, json={"items": []})
        assert empty.status_code == 422
        oversized = client.post(
            "/api/jobs/batch",
            headers=HEADERS,
            json={"items": [{"source_type": "local", "source_ref": "D:/a.mp4"}] * 31},
        )
        assert oversized.status_code == 422


def test_rename_job_validates_and_updates_title(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    video = tmp_path / "lesson.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        created = client.post(
            "/api/jobs",
            headers=HEADERS,
            json={"source_type": "local", "source_ref": str(video), "title": "B站课程"},
        ).json()["job"]
        job_id = created["id"]
        try:
            renamed = client.patch(
                f"/api/jobs/{job_id}", headers=HEADERS, json={"title": "  操作系统 3-4  "}
            )
            assert renamed.status_code == 200
            assert renamed.json()["job"]["title"] == "操作系统 3-4"
            blank = client.patch(
                f"/api/jobs/{job_id}", headers=HEADERS, json={"title": "   "}
            )
            assert blank.status_code == 400
            missing = client.patch(
                "/api/jobs/does-not-exist", headers=HEADERS, json={"title": "x"}
            )
            assert missing.status_code == 404
        finally:
            client.delete(f"/api/jobs/{job_id}", headers=HEADERS)


def test_search_endpoint_matches_note_text(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    video = tmp_path / "os.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        job = client.post(
            "/api/jobs",
            headers=HEADERS,
            json={"source_type": "local", "source_ref": str(video), "title": "1-1 进程"},
        ).json()["job"]
        application.database.update_job(
            job["id"],
            status="completed",
            summary="# 进程\n\n进程是资源分配的基本单位。",
        )
        body = client.get(
            "/api/search", headers=HEADERS, params={"q": "资源分配"}
        ).json()
        hits = [item for item in body["results"] if item["id"] == job["id"]]
        assert hits and "资源分配" in hits[0]["snippet"]
        assert hits[0]["course_id"] == job["course_id"]

        assert client.get("/api/search", headers=HEADERS, params={"q": "  "}).json()["results"] == []
        assert client.get(
            "/api/search", headers=HEADERS, params={"q": "x" * 90}
        ).status_code == 422
        client.delete(f"/api/jobs/{job['id']}", headers=HEADERS)


def test_highlight_endpoints_create_list_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    video = tmp_path / "note.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        job = client.post(
            "/api/jobs",
            headers=HEADERS,
            json={"source_type": "local", "source_ref": str(video), "title": "1-2 线程"},
        ).json()["job"]
        created = client.post(
            f"/api/jobs/{job['id']}/highlights",
            headers=HEADERS,
            json={"field": "summary", "quote": "调度单位", "start": 8, "color": "blue"},
        )
        assert created.status_code == 200
        highlight = created.json()["highlight"]
        assert highlight["color"] == "blue"

        listed = client.get(f"/api/jobs/{job['id']}/highlights", headers=HEADERS).json()
        assert [item["id"] for item in listed["highlights"]] == [highlight["id"]]

        invalid = client.post(
            f"/api/jobs/{job['id']}/highlights",
            headers=HEADERS,
            json={"field": "summary", "quote": "x", "color": "purple"},
        )
        assert invalid.status_code == 422

        missing = client.get(
            "/api/jobs/not-a-job/highlights", headers=HEADERS
        )
        assert missing.status_code == 404

        removed = client.delete(
            f"/api/jobs/{job['id']}/highlights/{highlight['id']}", headers=HEADERS
        )
        assert removed.status_code == 200
        assert client.get(
            f"/api/jobs/{job['id']}/highlights", headers=HEADERS
        ).json()["highlights"] == []
        client.delete(f"/api/jobs/{job['id']}", headers=HEADERS)


def test_provider_failure_returns_friendly_error(monkeypatch):
    """API 出错要变成可读的 400，不能因为没导入 ProviderError 变成 500。"""
    from study_buddy.errors import ProviderError
    from study_buddy.summarizer import OpenAICompatibleProvider

    def boom(*args, **kwargs):
        raise ProviderError("密钥无效或余额不足")

    monkeypatch.setattr(OpenAICompatibleProvider, "list_models", boom)
    monkeypatch.setattr(OpenAICompatibleProvider, "complete", boom)
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        models = client.get("/api/models", headers=HEADERS)
        assert models.status_code == 400
        assert "密钥无效" in models.json()["detail"]

        tested = client.post("/api/config/api/test", headers=HEADERS)
        assert tested.status_code == 400
        assert "密钥无效" in tested.json()["detail"]


def test_rename_job_clears_stale_sequence(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    video = tmp_path / "seq.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        job = client.post(
            "/api/jobs",
            headers=HEADERS,
            json={"source_type": "local", "source_ref": str(video), "title": "1-2 旧标题"},
        ).json()["job"]
        assert job["seq"] == "1-2"
        renamed = client.patch(
            f"/api/jobs/{job['id']}", headers=HEADERS, json={"title": "没有编号的新标题"}
        ).json()["job"]
        assert renamed["seq"] == ""
        # 再改回带编号的标题，章节号要跟着回来
        again = client.patch(
            f"/api/jobs/{job['id']}", headers=HEADERS, json={"title": "3-5 虚拟存储器"}
        ).json()["job"]
        assert again["seq"] == "3-5"
        client.delete(f"/api/jobs/{job['id']}", headers=HEADERS)


def test_wrong_count_rejects_out_of_range_index(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    video = tmp_path / "quiz.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        job = client.post(
            "/api/jobs",
            headers=HEADERS,
            json={"source_type": "local", "source_ref": str(video), "title": "1-2 线程"},
        ).json()["job"]
        # 还没有题目时也不该写计数
        assert client.post(
            f"/api/jobs/{job['id']}/questions/0/wrong", headers=HEADERS
        ).status_code == 400

        application.database.update_job(
            job["id"],
            questions='[{"type": "judge", "question": "q", "answer": "对"}, '
            '{"type": "judge", "question": "q2", "answer": "对"}]',
        )
        assert client.post(
            f"/api/jobs/{job['id']}/questions/1/wrong", headers=HEADERS
        ).status_code == 200
        out_of_range = client.post(
            f"/api/jobs/{job['id']}/questions/99/wrong", headers=HEADERS
        )
        assert out_of_range.status_code == 400
        assert "题号超出范围" in out_of_range.json()["detail"]
        assert application.database.get_question_stats(job["id"]) == {"1": 1}
        client.delete(f"/api/jobs/{job['id']}", headers=HEADERS)


def test_regenerate_questions_reports_provider_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    from study_buddy.errors import ProviderError

    def boom(*args, **kwargs):
        raise ProviderError("模型返回了空内容")

    monkeypatch.setattr(application.summary_service, "generate_questions", boom)
    video = tmp_path / "quiz2.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        job = client.post(
            "/api/jobs",
            headers=HEADERS,
            json={"source_type": "local", "source_ref": str(video), "title": "1-3 调度"},
        ).json()["job"]
        application.database.update_job(job["id"], status="completed", summary="## 笔记")
        response = client.post(f"/api/jobs/{job['id']}/questions", headers=HEADERS)
        assert response.status_code == 400
        assert "空内容" in response.json()["detail"]
        client.delete(f"/api/jobs/{job['id']}", headers=HEADERS)

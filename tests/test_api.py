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

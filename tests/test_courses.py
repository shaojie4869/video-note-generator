from pathlib import Path

from fastapi.testclient import TestClient

import app as application
from study_buddy.courses import auto_group, parse_sequence, sequence_sort_key
from study_buddy.db import MISC_COURSE_ID, Database

HEADERS = {"X-Study-Buddy": "1"}


def test_parse_sequence_handles_common_formats():
    assert parse_sequence("1-2 操作系统的发展历程") == "1-2"
    assert parse_sequence("p11 3-5 虚拟存储器") == "3-5"
    assert parse_sequence("第10讲 文件系统") == "10-0"
    assert parse_sequence("A股复盘") == ""


def test_sequence_sort_is_numeric_not_lexical():
    ordered = sorted(["1-10", "1-2", "3-4", "2-1"], key=sequence_sort_key)
    assert ordered == ["1-2", "1-10", "2-1", "3-4"]


def test_local_folder_with_several_videos_becomes_course(tmp_path):
    folder = tmp_path / "操作系统精讲课"
    folder.mkdir()
    (folder / "1-2 发展历程.mp4").write_bytes(b"x")
    (folder / "1-3 运行环境.mp4").write_bytes(b"x")
    kind, key, name = auto_group("local", str(folder / "1-2 发展历程.mp4"), "1-2 发展历程", 0)
    assert kind == "course"
    assert name == "操作系统精讲课"
    assert key.startswith("dir:")


def test_single_local_file_goes_to_misc(tmp_path):
    solo = tmp_path / "A股复盘.mp4"
    solo.write_bytes(b"x")
    assert auto_group("local", str(solo), "A股复盘", 0) == ("misc", "", "")


def test_bilibili_part_belongs_to_bv_course():
    kind, key, name = auto_group(
        "bilibili",
        "https://www.bilibili.com/video/BV18TZuBQE5q/?p=10",
        "BOK408计算机组成原理精讲课（25/26版，24年录制） p10 3-4 cache",
        0,
    )
    assert kind == "course"
    assert key == "bv:BV18TZuBQE5q"
    assert name == "BOK408计算机组成原理精讲课（25/26版，24年录制）"


def test_course_notes_sort_by_sequence_and_drop_old_attempts(tmp_path):
    database = Database(tmp_path / "history.db")
    course = database.create_course("操作系统精讲课", "course", "dir:D:/os")
    for title in ("1-10 文件系统", "1-2 发展历程", "1-5 虚拟机"):
        job = database.create_job("local", "D:/os/a.mp4", title, title, 0, course["id"])
        database.update_job(job["id"], status="completed", summary="笔记内容")
    duplicate = database.create_job("local", "D:/os/a.mp4", "1-5 虚拟机", "1-5 虚拟机", 0, course["id"])
    database.update_job(duplicate["id"], status="completed", summary="新一版笔记")

    notes = database.course_notes(course["id"])
    assert [note["seq"] for note in notes] == ["1-2", "1-5", "1-10"]
    assert len(notes) == 3


def test_misc_course_sorts_by_time_desc(tmp_path):
    database = Database(tmp_path / "history.db")
    first = database.create_job("bilibili", "https://b23.tv/x", "A股复盘", "k1", 0, MISC_COURSE_ID)
    database.update_job(first["id"], status="completed", summary="笔记", created_at="2026-01-01T00:00:00")
    second = database.create_job("bilibili", "https://b23.tv/y", "心理学科普", "k2", 0, MISC_COURSE_ID)
    database.update_job(second["id"], status="completed", summary="笔记", created_at="2026-02-01T00:00:00")

    notes = database.course_notes(MISC_COURSE_ID)
    assert [note["title"] for note in notes] == ["心理学科普", "A股复盘"]


def test_api_course_listing_and_moving_note(tmp_path, monkeypatch):
    monkeypatch.setattr(application.jobs, "_start", lambda *args: None)
    video = tmp_path / "lesson.mp4"
    video.write_bytes(b"test")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        created = client.post(
            "/api/courses", headers=HEADERS, json={"name": "临时课程"}
        ).json()["course"]
        job = client.post(
            "/api/jobs",
            headers=HEADERS,
            json={
                "source_type": "local",
                "source_ref": str(video),
                "title": "1-2 测试",
                "course_id": created["id"],
            },
        ).json()["job"]
        assert job["course_id"] == created["id"]

        courses = client.get("/api/courses", headers=HEADERS).json()["courses"]
        assert any(course["id"] == created["id"] for course in courses)
        assert courses[-1]["id"] == MISC_COURSE_ID

        moved = client.patch(
            f"/api/jobs/{job['id']}", headers=HEADERS, json={"course_id": MISC_COURSE_ID}
        )
        assert moved.status_code == 200
        assert moved.json()["job"]["course_id"] == MISC_COURSE_ID

        client.delete(f"/api/jobs/{job['id']}", headers=HEADERS)


def test_suggest_prefers_existing_course(tmp_path):
    folder = tmp_path / "操作系统精讲课"
    folder.mkdir()
    (folder / "1-2 发展历程.mp4").write_bytes(b"x")
    (folder / "1-3 运行环境.mp4").write_bytes(b"x")
    database = application.database
    course = database.create_course("操作系统精讲课", "course", f"dir:{folder}")
    with TestClient(application.app, base_url="http://127.0.0.1:8765") as client:
        result = client.get(
            "/api/courses/suggest",
            headers=HEADERS,
            params={
                "source_type": "local",
                "source_ref": str(folder / "1-2 发展历程.mp4"),
                "entry_index": 0,
                "title": "1-2 发展历程",
            },
        ).json()
        assert result["course_id"] == course["id"]


def test_same_name_different_folder_reuses_course(tmp_path):
    """课程目录被移动后 source_key 变了，不能凭空多出一门同名课。"""
    database = Database(tmp_path / "history.db")
    first = database.create_course("操作系统精讲课", "course", "dir:D:/a/操作系统精讲课")
    second = database.create_course("操作系统精讲课", "course", "dir:D:/b/操作系统精讲课")
    assert second["id"] == first["id"]
    # 不同名字仍然各建一门
    other = database.create_course("数据结构精讲课", "course", "dir:D:/c/数据结构精讲课")
    assert other["id"] != first["id"]


def test_merge_duplicate_courses_keeps_the_one_with_notes(tmp_path):
    database = Database(tmp_path / "history.db")
    main = database.create_course("操作系统精讲课", "course", "dir:D:/old")
    empty = database.create_course("临时课程", "course", "dir:D:/new")
    # 用改名绕过 create_course 的名字去重，模拟历史数据里已经存在的同名课程
    database.rename_course(empty["id"], "操作系统精讲课")

    kept_jobs = [
        database.create_job("local", f"D:/old/{index}.mp4", f"1-{index} 课", f"k{index}", 0, main["id"])
        for index in (2, 3)
    ]
    moved_job = database.create_job("local", "D:/new/1.mp4", "1-4 课", "k4", 0, empty["id"])

    assert database.merge_duplicate_courses() == 1
    remaining = [c for c in database.list_courses() if c["kind"] == "course"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == main["id"]
    for job in kept_jobs + [moved_job]:
        assert database.get_job(job["id"])["course_id"] == main["id"]


def test_delete_course_moves_notes_to_misc(tmp_path):
    database = Database(tmp_path / "history.db")
    course = database.create_course("操作系统精讲课", "course", "dir:D:/os")
    job = database.create_job("local", "D:/os/a.mp4", "1-2 发展历程", "k1", 0, course["id"])

    ok, moved = database.delete_course(course["id"])
    assert ok is True and moved == 1
    assert database.get_course(course["id"]) is None
    assert database.get_job(job["id"])["course_id"] == MISC_COURSE_ID
    # 杂项不能删
    assert database.delete_course(MISC_COURSE_ID) == (False, 0)


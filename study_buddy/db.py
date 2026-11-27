from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .courses import auto_group, parse_sequence, sequence_sort_key

MISC_COURSE_ID = "misc"
MISC_COURSE_NAME = "杂项"
# 划重点只允许这几种底色，前端也按这套 class 渲染
HIGHLIGHT_COLORS = ("yellow", "green", "blue", "pink")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_MARKS = re.compile(r"[*_`>#|]+")


def _search_snippet(text: str, needle: str, width: int = 150) -> str:
    """截一段能看懂的上下文：去掉 markdown 记号、压平换行，前后补省略号。"""
    clean = _MARKDOWN_LINK.sub(r"\1", text or "")
    clean = _MARKDOWN_MARKS.sub(" ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return ""
    index = clean.lower().find(needle.lower())
    if index < 0:
        return clean[:width] + ("…" if len(clean) > width else "")
    start = max(0, index - width // 3)
    end = min(len(clean), index + len(needle) + (width * 2) // 3)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DATA_DIR / "history.db"
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    entry_index INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    transcript TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_cache ON jobs(cache_key, status)"
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "visual_notes" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN visual_notes TEXT NOT NULL DEFAULT ''"
                )
            if "questions" not in columns:
                # 练习题以 JSON 数组存放，随笔记一起生成，重新生成时整体替换。
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN questions TEXT NOT NULL DEFAULT ''"
                )
            if "course_id" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN course_id TEXT NOT NULL DEFAULT ''"
                )
            if "seq" not in columns:
                # 章节号在写入时解析一次，排序就不用每次从标题里猜。
                connection.execute("ALTER TABLE jobs ADD COLUMN seq TEXT NOT NULL DEFAULT ''")
            if "finished_at" not in columns:
                # 完成时间单独存：事后改名、移动会改 updated_at，用它算耗时会失真。
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN finished_at TEXT NOT NULL DEFAULT ''"
                )
            # 课程层：笔记归档到课程，「杂项」是固定的兜底课程。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS courses (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'course',
                    source_key TEXT NOT NULL DEFAULT '',
                    viewed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_source ON courses(source_key) "
                "WHERE source_key != ''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_course ON jobs(course_id)"
            )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO courses (id, name, kind, source_key, viewed_at, created_at, updated_at)
                VALUES (?, ?, 'misc', '', '', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (MISC_COURSE_ID, MISC_COURSE_NAME, now, now),
            )
            # 错题计数独立成表：题目重新生成时统计可以按索引重置，也不污染题目内容。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS question_stats (
                    job_id TEXT NOT NULL,
                    question_index INTEGER NOT NULL,
                    wrong_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, question_index)
                )
                """
            )
            # 划重点：按「原文片段 + 起始位置」存，笔记重新渲染时再贴回去。
            # 不存 HTML 偏移，是因为 markdown 一改（重新生成、表格修复）DOM 结构就变了。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS note_highlights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    field TEXT NOT NULL DEFAULT 'summary',
                    quote TEXT NOT NULL,
                    start INTEGER NOT NULL DEFAULT 0,
                    color TEXT NOT NULL DEFAULT 'yellow',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_highlights_job "
                "ON note_highlights(job_id, field)"
            )
            # 本地用量账本：平台只给余额汇总，看不到单次调用，只能自己记。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_job ON usage_log(job_id)"
            )

    def create_job(
        self,
        source_type: str,
        source_ref: str,
        title: str,
        cache_key: str,
        entry_index: int,
        course_id: str = "",
        seq: str | None = None,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        now = utc_now()
        sequence = parse_sequence(title) if seq is None else seq
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, cache_key, created_at, updated_at, source_type, source_ref,
                    entry_index, title, status, stage, course_id, seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', '等待开始', ?, ?)
                """,
                (
                    job_id,
                    cache_key,
                    now,
                    now,
                    source_type,
                    source_ref,
                    entry_index,
                    title,
                    course_id,
                    sequence,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, cache_key, created_at, updated_at, source_type, source_ref,
                       entry_index, title, status, stage, progress, provider, error,
                       course_id, seq, finished_at,
                       length(transcript) AS transcript_chars,
                       length(summary) AS summary_chars,
                       (SELECT COALESCE(SUM(total_tokens), 0) FROM usage_log u
                        WHERE u.job_id = jobs.id) AS tokens
                FROM jobs ORDER BY created_at DESC LIMIT 200
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def find_active(self, cache_key: str) -> dict[str, Any] | None:
        """同一视频是否已有任务在跑。用来挡住重复提交，避免白烧 GPU 和 token。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE cache_key = ?
                  AND status IN ('queued', 'running', 'waiting_confirmation')
                ORDER BY created_at DESC LIMIT 1
                """,
                (cache_key,),
            ).fetchone()
        return dict(row) if row else None

    def find_completed(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE cache_key = ? AND status = 'completed' AND summary != ''
                ORDER BY created_at DESC LIMIT 1
                """,
                (cache_key,),
            ).fetchone()
        return dict(row) if row else None

    def find_reusable_transcript(self, cache_key: str) -> dict[str, Any] | None:
        """查找同源失败任务中已保存的转写文字，供重新生成时复用。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT transcript FROM jobs
                WHERE cache_key = ? AND transcript != ''
                    AND status IN ('failed', 'cancelled')
                ORDER BY created_at DESC LIMIT 1
                """,
                (cache_key,),
            ).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        if values.get("status") in {"completed", "failed", "cancelled"}:
            # 收尾状态出现时记一次完成时间，后续的编辑不影响它。
            values.setdefault("finished_at", values["updated_at"])
        columns = ", ".join(f"{key} = ?" for key in values)
        parameters = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in values.values()]
        parameters.append(job_id)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE jobs SET {columns} WHERE id = ?", parameters)

    def delete_job(self, job_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            if cursor.rowcount > 0:
                connection.execute(
                    "DELETE FROM question_stats WHERE job_id = ?", (job_id,)
                )
                # 笔记都没了，划的重点也没有意义，一并清掉。
                connection.execute(
                    "DELETE FROM note_highlights WHERE job_id = ?", (job_id,)
                )
            return cursor.rowcount > 0

    def bump_question_wrong(self, job_id: str, index: int, delta: int = 1) -> int:
        """累加某道题的答错次数并返回累计值。delta 可为负，用于撤销误点。"""
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO question_stats (job_id, question_index, wrong_count, updated_at)
                VALUES (?, ?, MAX(0, ?), ?)
                ON CONFLICT(job_id, question_index) DO UPDATE SET
                    wrong_count = MAX(0, wrong_count + ?),
                    updated_at = excluded.updated_at
                """,
                (job_id, int(index), max(0, int(delta)), utc_now(), int(delta)),
            )
            row = connection.execute(
                """
                SELECT wrong_count FROM question_stats
                WHERE job_id = ? AND question_index = ?
                """,
                (job_id, int(index)),
            ).fetchone()
        return int(row["wrong_count"]) if row else 0

    def get_question_stats(self, job_id: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT question_index, wrong_count FROM question_stats WHERE job_id = ?",
                (job_id,),
            ).fetchall()
        return {str(row["question_index"]): int(row["wrong_count"]) for row in rows}

    def clear_question_stats(self, job_id: str) -> None:
        """题目重新生成后，旧的错题统计已无意义，一并清空。"""
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM question_stats WHERE job_id = ?", (job_id,))

    def mark_interrupted(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', stage = '已停止',
                    error = '应用曾关闭，任务未自动续跑。请确认后手动重新开始。',
                    updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (utc_now(),),
            )

    def add_usage(
        self,
        job_id: str,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """记录一次模型调用的 token 消耗。"""
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_log (
                    job_id, purpose, model, prompt_tokens, completion_tokens,
                    total_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id or "",
                    purpose,
                    model or "",
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    int(total_tokens or 0),
                    utc_now(),
                ),
            )

    def job_usage(self, job_id: str) -> dict[str, Any]:
        """单个任务的消耗明细，按用途分组。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT purpose, COUNT(*) AS calls,
                       COALESCE(SUM(total_tokens), 0) AS tokens
                FROM usage_log WHERE job_id = ? GROUP BY purpose
                """,
                (job_id,),
            ).fetchall()
            total = connection.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM usage_log WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return {
            "total_tokens": int(total["t"] if total else 0),
            "by_purpose": [
                {"purpose": row["purpose"], "calls": row["calls"], "tokens": int(row["tokens"])}
                for row in rows
            ],
        }

    def usage_summary(self) -> dict[str, Any]:
        """本月 + 今日消耗汇总，用于界面展示。

        「今日/本月」按本机时区算：库里存的是 UTC，直接拿 UTC 切零点会让
        早上 8 点前和月初几小时算到前一天/上一月。
        """
        local_now = datetime.now().astimezone()
        local_month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        local_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = local_month.astimezone(timezone.utc).isoformat()
        today_start = local_today.astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            month = connection.execute(
                """
                SELECT COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt,
                       COALESCE(SUM(completion_tokens), 0) AS completion,
                       COUNT(*) AS calls
                FROM usage_log WHERE created_at >= ?
                """,
                (month_start,),
            ).fetchone()
            today = connection.execute(
                """
                SELECT COALESCE(SUM(total_tokens), 0) AS tokens, COUNT(*) AS calls
                FROM usage_log WHERE created_at >= ?
                """,
                (today_start,),
            ).fetchone()
            by_purpose = connection.execute(
                """
                SELECT purpose, COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens
                FROM usage_log WHERE created_at >= ?
                GROUP BY purpose ORDER BY tokens DESC
                """,
                (month_start,),
            ).fetchall()
            by_model = connection.execute(
                """
                SELECT model, COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens
                FROM usage_log WHERE created_at >= ?
                GROUP BY model ORDER BY tokens DESC
                """,
                (month_start,),
            ).fetchall()
        return {
            "month": {
                "tokens": int(month["tokens"]),
                "prompt_tokens": int(month["prompt"]),
                "completion_tokens": int(month["completion"]),
                "calls": int(month["calls"]),
            },
            "today": {"tokens": int(today["tokens"]), "calls": int(today["calls"])},
            "by_purpose": [dict(row) for row in by_purpose],
            "by_model": [dict(row) for row in by_model],
        }

    # ---------- 课程 ----------

    def list_courses(self) -> list[dict[str, Any]]:
        """课程列表：带笔记数与未读数，杂项永远排最后。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.name, c.kind, c.source_key, c.viewed_at,
                       COUNT(DISTINCT j.cache_key) AS note_count,
                       COALESCE(MAX(j.updated_at), '') AS last_at,
                       COUNT(DISTINCT CASE
                           WHEN c.viewed_at = '' OR j.updated_at > c.viewed_at
                           THEN j.cache_key END) AS unread
                FROM courses c
                LEFT JOIN jobs j
                    ON j.course_id = c.id AND j.status = 'completed' AND j.summary != ''
                GROUP BY c.id
                ORDER BY CASE WHEN c.kind = 'misc' THEN 1 ELSE 0 END,
                         COALESCE(MAX(j.updated_at), c.created_at) DESC,
                         c.name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_course(self, course_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM courses WHERE id = ?", (course_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_course(
        self, name: str, kind: str = "course", source_key: str = ""
    ) -> dict[str, Any]:
        name = (name or "").strip() or "未命名课程"
        now = utc_now()
        with self._lock, self._connect() as connection:
            if source_key:
                row = connection.execute(
                    "SELECT * FROM courses WHERE source_key = ?", (source_key,)
                ).fetchone()
                if row:
                    return dict(row)
            if kind == "course":
                # 同一个文件夹被移动/改名后 source_key 会变，但课程名没变。
                # 只按 source_key 判重会凭空多出一门同名课，所以名字再兜一层。
                row = connection.execute(
                    """
                    SELECT * FROM courses
                    WHERE kind = 'course' AND trim(name) = ?
                    ORDER BY created_at LIMIT 1
                    """,
                    (name,),
                ).fetchone()
                if row:
                    return dict(row)
            course_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO courses (id, name, kind, source_key, viewed_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', ?, ?)
                """,
                (course_id, name, kind, source_key, now, now),
            )
        return self.get_course(course_id) or {}

    def rename_course(self, course_id: str, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE courses SET name = ?, updated_at = ? WHERE id = ?",
                (name, utc_now(), course_id),
            )
            return cursor.rowcount > 0

    def mark_course_viewed(self, course_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE courses SET viewed_at = ? WHERE id = ?", (utc_now(), course_id)
            )

    def delete_course(self, course_id: str) -> tuple[bool, int]:
        """删课程。里面的笔记不退场，统一挪进「杂项」，避免误删丢数据。

        返回 ``(是否删除, 被移动的笔记数)``。
        """
        if not course_id or course_id == MISC_COURSE_ID:
            return False, 0
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM courses WHERE id = ?", (course_id,)
            ).fetchone()
            if not row:
                return False, 0
            moved = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE course_id = ?", (course_id,)
            ).fetchone()[0]
            connection.execute(
                "UPDATE jobs SET course_id = ?, updated_at = ? WHERE course_id = ?",
                (MISC_COURSE_ID, utc_now(), course_id),
            )
            connection.execute("DELETE FROM courses WHERE id = ?", (course_id,))
        return True, int(moved)

    def merge_duplicate_courses(self) -> int:
        """把同名的课程并成一门，返回被并掉的课程数。

        历史遗留：同一个课程目录被移动后 source_key 变了，会多出一门同名课
        （一门有笔记、一门空）。这里统一并到笔记最多的那门上。
        """
        removed = 0
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, trim(c.name) AS name,
                       (SELECT COUNT(*) FROM jobs j WHERE j.course_id = c.id) AS notes,
                       c.created_at
                FROM courses c WHERE c.kind = 'course'
                ORDER BY c.created_at
                """
            ).fetchall()
            groups: dict[str, list[Any]] = {}
            for row in rows:
                groups.setdefault(row["name"], []).append(row)
            for name, items in groups.items():
                if len(items) < 2:
                    continue
                target = sorted(
                    items, key=lambda item: (-item["notes"], item["created_at"])
                )[0]
                for item in items:
                    if item["id"] == target["id"]:
                        continue
                    connection.execute(
                        """
                        UPDATE jobs SET course_id = ?, updated_at = ?
                        WHERE course_id = ?
                        """,
                        (target["id"], utc_now(), item["id"]),
                    )
                    connection.execute("DELETE FROM courses WHERE id = ?", (item["id"],))
                    removed += 1
        return removed

    def move_job(self, job_id: str, course_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET course_id = ?, updated_at = ? WHERE id = ?",
                (course_id, utc_now(), job_id),
            )
            return cursor.rowcount > 0

    def ensure_course_for_job(
        self, source_type: str, source_ref: str, title: str, entry_index: int
    ) -> str:
        """按规则把任务归到课程；识别不出的统一进杂项。"""
        kind, source_key, name = auto_group(source_type, source_ref, title, entry_index)
        if kind != "course" or not source_key:
            return MISC_COURSE_ID
        return self.create_course(name, "course", source_key)["id"]

    def course_notes(self, course_id: str) -> list[dict[str, Any]]:
        """课程下的笔记索引：只返回已生成笔记的任务，同一来源只留最新一份。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, cache_key, title, seq, created_at, updated_at, status, error,
                       provider,
                       length(summary) AS summary_chars,
                       questions,
                       (SELECT COALESCE(SUM(q.wrong_count), 0)
                        FROM question_stats q WHERE q.job_id = jobs.id) AS wrong_total
                FROM jobs
                WHERE course_id = ? AND status = 'completed' AND summary != ''
                ORDER BY created_at DESC
                """,
                (course_id,),
            ).fetchall()
            course = connection.execute(
                "SELECT kind FROM courses WHERE id = ?", (course_id,)
            ).fetchone()

        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            note = dict(row)
            latest.setdefault(note["cache_key"], note)
        notes = list(latest.values())
        kind = course["kind"] if course else "course"
        if kind == "misc":
            # 杂项里的视频没有章节号，按时间倒序更符合直觉。
            notes.sort(key=lambda item: item["created_at"], reverse=True)
        else:
            notes.sort(key=lambda item: (sequence_sort_key(item["seq"]), item["created_at"]))
        return notes

    # ---------- 划重点 ----------

    def add_highlight(
        self, job_id: str, field: str, quote: str, start: int, color: str
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO note_highlights (job_id, field, quote, start, color, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, field, quote, max(0, int(start)), color, utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM note_highlights WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row) if row else {}

    def list_highlights(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, job_id, field, quote, start, color, created_at
                FROM note_highlights WHERE job_id = ?
                ORDER BY start, id
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_highlight(self, job_id: str, highlight_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM note_highlights WHERE id = ? AND job_id = ?",
                (int(highlight_id), job_id),
            )
            return cursor.rowcount > 0

    # ---------- 搜索笔记 ----------

    def search_notes(self, query: str, limit: int = 40) -> list[dict[str, Any]]:
        """在已完成的笔记里搜知识点。

        只搜正文（笔记 / 画面笔记）和标题；同一来源只留最新一份，
        返回带上下文的片段，前端点一条就能跳到对应笔记再定位。
        """
        needle = (query or "").strip()
        if not needle:
            return []
        lowered = needle.lower()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT j.id, j.cache_key, j.title, j.seq, j.course_id, j.created_at,
                       j.summary, j.visual_notes,
                       COALESCE(c.name, '') AS course_name
                FROM jobs j
                LEFT JOIN courses c ON c.id = j.course_id
                WHERE j.status = 'completed' AND j.summary != ''
                ORDER BY j.created_at DESC
                """
            ).fetchall()

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            cache_key = row["cache_key"] or row["id"]
            if cache_key in seen:
                continue
            seen.add(cache_key)
            summary = row["summary"] or ""
            visual = row["visual_notes"] or ""
            title = row["title"] or ""
            if lowered in summary.lower():
                field, text = "summary", summary
            elif lowered in visual.lower():
                field, text = "visual", visual
            elif lowered in title.lower():
                field, text = "title", summary
            else:
                continue
            occurrences = (
                summary.lower().count(lowered)
                + visual.lower().count(lowered)
                + title.lower().count(lowered)
            )
            snippet_source = summary if summary else title
            if field == "visual":
                snippet_source = visual
            results.append(
                {
                    "id": row["id"],
                    "title": title,
                    "seq": row["seq"] or "",
                    "course_id": row["course_id"] or "",
                    "course_name": row["course_name"] or MISC_COURSE_NAME,
                    "field": field,
                    "snippet": _search_snippet(snippet_source, lowered),
                    "occurrences": occurrences,
                    "created_at": row["created_at"],
                }
            )
            if len(results) >= max(1, limit):
                break
        return results

    def assign_missing_courses(self) -> int:
        """给历史任务补课程归属与章节号，升级后第一次启动时执行。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source_type, source_ref, title, entry_index
                FROM jobs WHERE course_id = ''
                """
            ).fetchall()
            empty_seq = connection.execute(
                "SELECT id, title FROM jobs WHERE seq = ''"
            ).fetchall()
        for row in empty_seq:
            sequence = parse_sequence(row["title"])
            if sequence:
                self.update_job(row["id"], seq=sequence)
        for row in rows:
            course_id = self.ensure_course_for_job(
                row["source_type"], row["source_ref"], row["title"], row["entry_index"]
            )
            self.move_job(row["id"], course_id)
        return len(rows)

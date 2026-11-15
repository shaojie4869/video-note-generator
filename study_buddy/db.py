from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self, source_type: str, source_ref: str, title: str, cache_key: str, entry_index: int
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, cache_key, created_at, updated_at, source_type, source_ref,
                    entry_index, title, status, stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', '等待开始')
                """,
                (job_id, cache_key, now, now, source_type, source_ref, entry_index, title),
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
                       length(transcript) AS transcript_chars,
                       length(summary) AS summary_chars
                FROM jobs ORDER BY created_at DESC LIMIT 200
                """
            ).fetchall()
        return [dict(row) for row in rows]

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
        """本月 + 今日消耗汇总，用于界面展示。"""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
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

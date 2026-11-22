from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .config import CACHE_DIR, SettingsManager
from .db import Database
from .errors import JobCancelled, ProviderError
from .schemas import JobCreate
from .sources import (
    BilibiliClient,
    PreparedSource,
    SourceError,
    extract_audio,
    extract_keyframes,
    validate_bilibili_url,
    validate_local_video,
)
from .summarizer import OpenAICompatibleProvider, SummaryService
from .transcription import Transcriber, TranscriptionError

# 任务创建时还没有解析出真实标题，B 站任务先用这个占位。
BILIBILI_PLACEHOLDER_TITLE = "B站课程"

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(
        self,
        database: Database,
        settings: SettingsManager,
        summary: SummaryService,
        bilibili: BilibiliClient | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        self.db = database
        self.settings = settings
        self.summary = summary
        self.bilibili = bilibili or BilibiliClient()
        self.transcriber = transcriber or Transcriber()
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancelled: dict[str, threading.Event] = {}

    @staticmethod
    def cache_key(payload: JobCreate) -> str:
        if payload.source_type == "local":
            path = validate_local_video(payload.source_ref)
            stat = path.stat()
            return f"local:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
        url = validate_bilibili_url(payload.source_ref)
        return f"bilibili:{url}:entry:{payload.entry_index}"

    async def create(self, payload: JobCreate) -> tuple[dict[str, Any], bool]:
        cache_key = await asyncio.to_thread(self.cache_key, payload)
        # 同一个视频只允许一个任务在跑：误点「开始生成」不该重复下载、重复转写。
        # 前端也会在选中项已有任务时禁用按钮，这里是最后一道闸。
        active = await asyncio.to_thread(self.db.find_active, cache_key)
        if active:
            if payload.force:
                raise ValueError("这个视频正在生成中，请等它完成，或先取消当前任务。")
            return active, True
        if not payload.force:
            existing = await asyncio.to_thread(self.db.find_completed, cache_key)
            if existing:
                return existing, True
        title = payload.title or (
            Path(payload.source_ref).stem if payload.source_type == "local" else "B站课程"
        )
        course_id = (payload.course_id or "").strip()
        if course_id:
            # 前端指定了课程就照办，但课程可能已被删除，退回自动归组。
            if not await asyncio.to_thread(self.db.get_course, course_id):
                course_id = ""
        if not course_id:
            course_id = await asyncio.to_thread(
                self.db.ensure_course_for_job,
                payload.source_type,
                payload.source_ref.strip(),
                title,
                payload.entry_index,
            )
        job = await asyncio.to_thread(
            self.db.create_job,
            payload.source_type,
            payload.source_ref.strip(),
            title,
            cache_key,
            payload.entry_index,
            course_id,
        )
        if payload.force:
            reusable = await asyncio.to_thread(self.db.find_reusable_transcript, cache_key)
            if reusable:
                # 复用之前失败/取消任务已完成的转写，避免重复转写耗时。
                await asyncio.to_thread(
                    self.db.update_job, job["id"], transcript=reusable["transcript"]
                )
        self._start(job["id"], False)
        return self.db.get_job(job["id"]), False

    def _start(self, job_id: str, summary_only: bool) -> None:
        if job_id in self._tasks and not self._tasks[job_id].done():
            raise ValueError("该任务正在运行。")
        event = self._cancelled.setdefault(job_id, threading.Event())
        event.clear()
        coroutine = (
            self._run_summary_only(job_id, event)
            if summary_only
            else self._run_initial(job_id, event)
        )
        self._tasks[job_id] = asyncio.create_task(coroutine)

    async def confirm(self, job_id: str) -> dict[str, Any]:
        """兼容旧任务：早期版本遇到超长转写会停在「待确认」，现在只负责把它放行。

        新任务不再进入这个状态（见 _prepare_transcript），但库里已有的任务还要能继续。
        """
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError("任务不存在。")
        if job["status"] != "waiting_confirmation" or not job["transcript"]:
            raise ValueError("该任务当前不需要确认。")
        previous = self._tasks.get(job_id)
        if previous and not previous.done():
            await previous
        job = self.db.get_job(job_id)
        if not job or job["status"] != "waiting_confirmation":
            raise ValueError("任务状态已变化，请刷新后重试。")
        self.db.update_job(job_id, status="queued", stage="已确认，等待总结", error="")
        self._start(job_id, True)
        return self.db.get_job(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError("任务不存在。")
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        self._cancelled.setdefault(job_id, threading.Event()).set()
        if job["status"] == "waiting_confirmation":
            self.db.update_job(job_id, status="cancelled", stage="已取消")
        else:
            self.db.update_job(job_id, stage="正在取消")
        return self.db.get_job(job_id)

    def delete(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task and not task.done():
            raise ValueError("任务运行中，请先取消。")
        return self.db.delete_job(job_id)

    @staticmethod
    def _check_cancelled(event: threading.Event) -> None:
        if event.is_set():
            raise JobCancelled()

    def _refresh_bilibili_title(self, job_id: str, job: dict[str, Any]) -> None:
        """把占位标题换成真实标题。

        重新生成会复制原任务标题，如果原任务停在「B站课程」，就会一代代传下去，
        历史列表攒多了完全没法区分，所以复用转写这条路必须自己解析一次。
        """
        try:
            info = self.bilibili.info(job["source_ref"])
        except (SourceError, OSError) as exc:
            logger.warning("读取视频标题失败：%s", exc)
            return
        entries = info.get("entries") or []
        index = int(job.get("entry_index") or 0)
        title = entries[index]["title"] if index < len(entries) else info.get("title")
        if title and title != job.get("title"):
            self.db.update_job(job_id, title=title)

    async def _run_initial(self, job_id: str, event: threading.Event) -> None:
        try:
            await asyncio.to_thread(self._prepare_transcript, job_id, event)
        except JobCancelled:
            self.db.update_job(job_id, status="cancelled", stage="已取消", error="")
        except (SourceError, TranscriptionError, ProviderError, OSError) as exc:
            self.db.update_job(job_id, status="failed", stage="处理失败", error=str(exc))
        except Exception as exc:
            self.db.update_job(job_id, status="failed", stage="处理失败", error=f"未预期错误：{exc}")
        finally:
            self._tasks.pop(job_id, None)

    def _prepare_transcript(self, job_id: str, event: threading.Event) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        config = self.settings.get()
        frames_enabled = bool(config.get("frames_enabled"))
        video_path: Path | None = None
        if job["transcript"]:
            # 重新生成：转写已存在（复用自失败任务），直接进入总结流程。
            transcript = job["transcript"]
            self.db.update_job(job_id, status="running", stage="复用已完成的转写", progress=65)
            if job["source_type"] == "bilibili" and (job.get("title") or "").strip() in {
                "",
                BILIBILI_PLACEHOLDER_TITLE,
            }:
                self._refresh_bilibili_title(job_id, job)
            if frames_enabled and not job.get("visual_notes"):
                # 上次很可能就是失败在画面识别这一步：转写还在，画面笔记是空的。
                # 这时必须重新拿到视频，否则重试会静默丢掉整段课件画面。
                if job["source_type"] == "local":
                    try:
                        video_path = validate_local_video(job["source_ref"])
                    except SourceError:
                        video_path = None
                else:
                    self.db.update_job(job_id, stage="重新下载视频用于截帧", progress=65)
                    self._check_cancelled(event)
                    try:
                        prepared = self.bilibili.prepare(
                            job["source_ref"], job["entry_index"], job_id, want_video=True
                        )
                        video_path = prepared.media_path
                        if prepared.title and (job.get("title") or "").strip() in {
                            "",
                            BILIBILI_PLACEHOLDER_TITLE,
                        }:
                            self.db.update_job(job_id, title=prepared.title)
                    except (SourceError, OSError) as exc:
                        logger.warning("重新下载视频失败，跳过画面信息：%s", exc)
                        video_path = None
        else:
            self.db.update_job(job_id, status="running", stage="正在读取视频", progress=3)
            self._check_cancelled(event)
            if job["source_type"] == "local":
                media_path = validate_local_video(job["source_ref"])
                prepared = PreparedSource(title=job["title"], media_path=media_path)
                video_path = media_path if frames_enabled else None
            else:
                prepared = self.bilibili.prepare(
                    job["source_ref"], job["entry_index"], job_id, want_video=frames_enabled
                )
                self.db.update_job(job_id, title=prepared.title)
                if frames_enabled and prepared.media_path and prepared.media_path.suffix.lower() in {
                    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".m4v", ".webm", ".ts"
                }:
                    video_path = prepared.media_path
            self._check_cancelled(event)

            if prepared.transcript:
                transcript = prepared.transcript
                self.db.update_job(job_id, stage="已读取视频字幕", progress=65)
            else:
                self.db.update_job(job_id, stage="正在提取音频", progress=10)
                audio_path = CACHE_DIR / job_id / "audio.wav"
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                extract_audio(prepared.media_path, audio_path)
                self._check_cancelled(event)
                self.db.update_job(job_id, stage="正在本地转写（首次需下载模型）", progress=15)

                def transcribe_progress(value: int) -> None:
                    self.db.update_job(job_id, progress=15 + int(value * 0.45))

                transcript = self.transcriber.transcribe(
                    audio_path,
                    config["whisper_model"],
                    config["whisper_device"],
                    config["whisper_compute_type"],
                    transcribe_progress,
                    event,
                )
        self._check_cancelled(event)
        self.db.update_job(job_id, transcript=transcript, progress=65)
        if job.get("visual_notes") and frames_enabled:
            # 重新生成笔记时复用已识别的画面内容：截图和视觉调用都是重复开销。
            self.db.update_job(job_id, stage="复用已识别的课件画面", progress=67)
        else:
            self._prepare_visual(job_id, event, video_path)
        self._check_cancelled(event)
        # 不再按字数暂停等确认：用户点了「开始生成」就是要生成，
        # 多几万字只影响耗时和 token，不该变成一次额外交互。
        self._summarize(job_id, event)

    def _prepare_visual(self, job_id: str, event: threading.Event, video_path: Path | None) -> None:
        """截取课件画面并让视觉模型提取其中的知识信息。"""
        if video_path is None:
            return
        config = self.settings.get()
        frames = extract_keyframes(
            video_path,
            CACHE_DIR / job_id / "frames",
            int(config.get("frame_interval", 60)),
            int(config.get("max_frames", 24)),
        )
        if not frames:
            return
        self._check_cancelled(event)
        self.db.update_job(job_id, stage="正在识别课件画面", progress=66)
        provider = OpenAICompatibleProvider(self.settings, self.db.add_usage, job_id)
        try:
            notes = provider.describe_frames(frames, event)
        except JobCancelled:
            raise
        except ProviderError as exc:
            # 画面信息是补充材料：识别失败也要把转写和笔记正常交付出去，
            # 不能因为几张截图丢掉已经花掉几分钟的转写结果。
            logger.warning("课件画面识别失败，跳过画面信息：%s", exc)
            notes = ""
        self.db.update_job(job_id, visual_notes=notes)

    async def _run_summary_only(self, job_id: str, event: threading.Event) -> None:
        try:
            await asyncio.to_thread(self._summarize, job_id, event)
        except JobCancelled:
            self.db.update_job(job_id, status="cancelled", stage="已取消", error="")
        except ProviderError as exc:
            self.db.update_job(job_id, status="failed", stage="总结失败", error=str(exc))
        except Exception as exc:
            self.db.update_job(job_id, status="failed", stage="总结失败", error=f"未预期错误：{exc}")
        finally:
            self._tasks.pop(job_id, None)

    def _summarize(self, job_id: str, event: threading.Event) -> None:
        job = self.db.get_job(job_id)
        if not job or not job["transcript"]:
            raise ProviderError("没有可总结的转写内容。")
        self._check_cancelled(event)
        self.db.update_job(job_id, status="running", stage="正在生成学习笔记", progress=68, error="")

        def progress(value: int, stage: str) -> None:
            self.db.update_job(job_id, stage=stage, progress=68 + int(value * 0.32))

        result = self.summary.summarize(
            job["title"],
            job["transcript"],
            job.get("visual_notes", ""),
            progress,
            event,
            job_id=job_id,
        )
        self._check_cancelled(event)
        self.db.update_job(
            job_id,
            summary=result.text,
            provider=result.provider,
            stage="正在生成练习题",
            progress=95,
        )

        questions: list[dict] = []
        try:
            questions = self.summary.generate_questions(
                job["title"], result.text, None, event, job_id=job_id
            )
        except JobCancelled:
            raise
        except Exception:
            # 出题失败不该拖垮整条链路：笔记已经在手，用户可在界面上单独重试出题。
            questions = []
        if questions:
            self.db.clear_question_stats(job_id)

        self._check_cancelled(event)
        self.db.update_job(
            job_id,
            status="completed",
            stage="已完成",
            progress=100,
            summary=result.text,
            provider=result.provider,
            error="",
            questions=json.dumps(questions, ensure_ascii=False) if questions else "",
        )

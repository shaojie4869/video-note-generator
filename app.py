from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from study_buddy import __version__
from study_buddy.config import BASE_DIR, SettingsManager
from study_buddy.courses import auto_group, parse_sequence
from study_buddy.db import MISC_COURSE_ID, Database
from study_buddy.build_info import build_id
from study_buddy.hardware import profile as hardware_profile
from study_buddy.jobs import JobManager
from study_buddy.schemas import (
    ApiSettings,
    BatchJobCreate,
    BilibiliInfoRequest,
    CourseCreate,
    CourseRename,
    GeneralSettings,
    HighlightCreate,
    JobCreate,
    JobUpdate,
)
from study_buddy.sources import BilibiliClient, SourceError, ffmpeg_executable, validate_local_video
from study_buddy.summarizer import OpenAICompatibleProvider, SummaryService
from study_buddy.transcription import cuda_available

settings = SettingsManager()
database = Database()
summary_service = SummaryService(settings, database)
bilibili_client = BilibiliClient()
jobs = JobManager(database, settings, summary_service, bilibili_client)

# 只在启动时算一次：如果每次请求都从磁盘现算，旧进程会算出新指纹，
# 启动器就永远判断不出「我在跑旧代码」。
BUILD_ID = build_id()

# 视觉能力探测要逐个模型发图试探，结果缓存一段时间，避免反复消耗额度。
_VISION_PROBE: dict = {"signature": "", "vision": [], "at": 0.0}
_VISION_PROBE_TTL = 6 * 3600


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.mark_interrupted()
    # 升级到课程层后，历史任务需要补一次归属；只做本地推断，不发网络请求。
    await asyncio.to_thread(database.assign_missing_courses)
    # 同一目录被移动过会产生同名课程，启动时并掉，免得左栏出现两门一样的课。
    await asyncio.to_thread(database.merge_duplicate_courses)
    yield


app = FastAPI(title="学习视频总结", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def local_api_guard(request: Request, call_next):
    allowed_hosts = {"127.0.0.1:8765", "localhost:8765"}
    if request.headers.get("host", "").lower() not in allowed_hosts:
        return PlainTextResponse("Forbidden", status_code=403)
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 8765:
            return PlainTextResponse("Forbidden", status_code=403)
    if request.url.path.startswith("/api/"):
        if request.headers.get("X-Study-Buddy") != "1":
            return PlainTextResponse("Forbidden", status_code=403)
    return await call_next(request)


def error_http(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


@app.get("/api/health")
def health() -> dict:
    # features 用于前端判断后端是否已更新：静态文件每次刷新都重读，
    # 但 Python 进程只有重启才加载新路由，两者错配时前端要能说清原因。
    return {
        "ok": True,
        "version": __version__,
        "build": BUILD_ID,
        "ffmpeg": ffmpeg_executable(),
        "features": [
            "questions",
            "question_stats",
            "batch",
            "system",
            "highlights",
            "search",
        ],
    }


@app.get("/api/config")
def get_config() -> dict:
    return settings.public()


@app.post("/api/config")
def update_config(payload: GeneralSettings) -> dict:
    return settings.update_general(payload.model_dump(exclude_none=True))


@app.post("/api/config/api")
def update_api(payload: ApiSettings) -> dict:
    settings.update_api(
        payload.base_url,
        payload.model,
        payload.vision_model,
        payload.api_key,
        payload.reasoning_effort,
    )
    return settings.public()


@app.get("/api/usage")
def usage_summary() -> dict:
    """本地用量账本：平台不提供单次调用明细，这里按任务和用途记录。"""
    return database.usage_summary()


@app.get("/api/models")
def list_models(probe_vision: bool = False) -> dict:
    """列出账号可用的模型；probe_vision=true 时额外实测哪些模型能读图。"""
    provider = OpenAICompatibleProvider(settings)
    try:
        models = provider.list_models()
    except ProviderError as exc:
        raise error_http(exc) from exc
    result: dict = {"models": models, "vision_models": []}
    if not probe_vision:
        return result

    signature = f"{settings.get()['api'].get('base_url', '')}|{','.join(models)}"
    now = time.time()
    cached = _VISION_PROBE
    if cached["signature"] == signature and now - cached["at"] < _VISION_PROBE_TTL:
        result["vision_models"] = cached["vision"]
        result["cached"] = True
        return result

    try:
        vision = provider.probe_vision_models(models)
    except ProviderError as exc:
        raise error_http(exc) from exc
    _VISION_PROBE.update({"signature": signature, "vision": vision, "at": now})
    result["vision_models"] = vision
    return result


@app.post("/api/config/api/test")
def test_api() -> dict:
    try:
        provider = OpenAICompatibleProvider(settings)
        result = provider.complete("这是一次连通性测试。请只回复：连接成功")
        return {"ok": True, "message": result[:100]}
    except ProviderError as exc:
        raise error_http(exc) from exc


def choose_local_file(multiple: bool = False) -> dict:
    if sys.platform != "win32":
        raise SourceError("当前系统请直接在任务链接中填写视频路径。")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.OpenFileDialog; "
        "$dialog.Title = '选择学习视频（可按住 Ctrl 多选）'; "
        f"$dialog.Multiselect = ${'true' if multiple else 'false'}; "
        "$dialog.Filter = '视频文件|*.mp4;*.mkv;*.mov;*.avi;*.flv;*.wmv;*.m4v;*.webm;*.ts'; "
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "[Console]::Write(($dialog.FileNames -join \"`n\")) }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise SourceError("无法打开文件选择器。")
    value = result.stdout.decode("utf-8-sig", errors="replace").strip()
    if not value:
        return {"cancelled": True}
    files = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        path = validate_local_video(line)
        files.append({"path": str(path), "title": path.stem})
    if not files:
        return {"cancelled": True}
    # path / title 保留给旧前端：单文件时它们就是唯一那一项。
    return {"cancelled": False, "files": files, "path": files[0]["path"], "title": files[0]["title"]}


@app.post("/api/videos/browse")
async def browse_video(multiple: bool = False) -> dict:
    try:
        return await asyncio.to_thread(choose_local_file, multiple)
    except (SourceError, subprocess.TimeoutExpired) as exc:
        raise error_http(exc) from exc


@app.post("/api/bilibili/info")
async def bilibili_info(payload: BilibiliInfoRequest) -> dict:
    try:
        return await asyncio.to_thread(bilibili_client.info, payload.url)
    except SourceError as exc:
        raise error_http(exc) from exc


@app.post("/api/jobs")
async def create_job(payload: JobCreate) -> dict:
    try:
        job, reused = await jobs.create(payload)
        return {"job": job, "reused": reused}
    except (SourceError, ValueError) as exc:
        raise error_http(exc) from exc


@app.post("/api/jobs/batch")
async def create_jobs(payload: BatchJobCreate) -> dict:
    """批量提交：逐个建任务，单个失败不影响其余。

    并发不在这里控制：每个任务自己按阶段去抢闸门（见 JobManager._pipeline），
    这里只负责把「哪些建成了、哪些是复用的、哪些失败」如实回给界面。
    """
    created: list[dict] = []
    reused: list[dict] = []
    errors: list[dict] = []
    for index, item in enumerate(payload.items):
        try:
            job, was_reused = await jobs.create(item)
        except (SourceError, ValueError) as exc:
            errors.append(
                {
                    "index": index,
                    "title": item.title or item.source_ref,
                    "message": str(exc),
                }
            )
            continue
        (reused if was_reused else created).append(job)
    return {
        "jobs": created + reused,
        "created": len(created),
        "reused": len(reused),
        "errors": errors,
    }


@app.patch("/api/jobs/{job_id}")
def update_job(job_id: str, payload: JobUpdate) -> dict:
    """改标题或把笔记移到别的课程。"""
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空。")
        # 改名时顺带重算章节号：用户可能把「未命名」补成「3-5 虚拟存储器」
        sequence = parse_sequence(title)
        database.update_job(job_id, title=title, seq=sequence or job.get("seq", ""))
    if payload.course_id is not None:
        course_id = payload.course_id.strip()
        if not course_id:
            raise HTTPException(status_code=400, detail="课程不能为空。")
        if not database.get_course(course_id):
            raise HTTPException(status_code=404, detail="课程不存在。")
        database.move_job(job_id, course_id)
    return {"job": database.get_job(job_id)}


@app.get("/api/courses")
def list_courses() -> dict:
    return {"courses": database.list_courses(), "misc_id": MISC_COURSE_ID}


@app.post("/api/courses")
def create_course(payload: CourseCreate) -> dict:
    return {"course": database.create_course(payload.name)}


@app.patch("/api/courses/{course_id}")
def rename_course(course_id: str, payload: CourseRename) -> dict:
    if not database.get_course(course_id):
        raise HTTPException(status_code=404, detail="课程不存在。")
    if not database.rename_course(course_id, payload.name):
        raise HTTPException(status_code=400, detail="课程名不能为空。")
    return {"course": database.get_course(course_id)}


@app.delete("/api/courses/{course_id}")
def delete_course(course_id: str) -> dict:
    """删除课程。课里的笔记不会被删掉，统一移进「杂项」。"""
    if course_id == MISC_COURSE_ID:
        raise HTTPException(status_code=400, detail="「杂项」是固定容器，不能删除。")
    ok, moved = database.delete_course(course_id)
    if not ok:
        raise HTTPException(status_code=404, detail="课程不存在。")
    return {"deleted": True, "moved": moved, "misc_id": MISC_COURSE_ID}


@app.get("/api/courses/suggest")
def suggest_course(
    source_type: str, source_ref: str, entry_index: int = 0, title: str = ""
) -> dict:
    """生成前的归属预览：命中已有课程就返回它，否则给出将新建的课程名。"""
    kind, source_key, name = auto_group(source_type, source_ref, title, entry_index)
    if kind != "course" or not source_key:
        return {"kind": "misc", "name": "杂项", "course_id": MISC_COURSE_ID}
    for course in database.list_courses():
        if course["source_key"] == source_key:
            return {"kind": "course", "name": course["name"], "course_id": course["id"]}
    return {"kind": "course", "name": name, "course_id": ""}


@app.get("/api/courses/{course_id}")
def get_course(course_id: str) -> dict:
    course = database.get_course(course_id)
    if not course:
        raise HTTPException(status_code=404, detail="课程不存在。")
    notes = database.course_notes(course_id)
    # 打开课程即视为已读，左栏未读点随之消失。
    database.mark_course_viewed(course_id)
    return {
        "course": course,
        "notes": notes,
        "misc_id": MISC_COURSE_ID,
    }


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": database.list_jobs()}


@app.get("/api/search")
def search_notes(q: str = "", limit: int = 40) -> dict:
    """按知识点搜全部笔记：返回命中的笔记、课程名和上下文片段。"""
    query = (q or "").strip()
    if not query:
        return {"query": "", "results": []}
    if len(query) > 80:
        raise HTTPException(status_code=422, detail="搜索词太长了，请缩短到 80 字以内。")
    results = database.search_notes(query, max(1, min(limit, 100)))
    return {"query": query, "results": results}


@app.get("/api/system")
def system_profile() -> dict:
    """批量生成用的并发策略：给用户看清楚「为什么这样排队」。"""
    profile = hardware_profile()
    config = settings.get()
    cuda = False
    try:
        cuda = cuda_available()
    except Exception:  # 组件缺失时只是展示为不可用，不影响应用
        cuda = False
    return {
        "hardware": profile.as_dict(),
        "slots": {
            "prepare": profile.prepare_slots,
            "transcribe": profile.transcribe_slots,
            "summarize": profile.summarize_slots,
        },
        "max_batch": profile.max_batch,
        "whisper": {
            "model": config.get("whisper_model", "small"),
            "device": config.get("whisper_device", "cpu"),
            "compute_type": config.get("whisper_compute_type", "int8"),
            "cuda_available": cuda,
        },
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在。")
    job["usage"] = database.job_usage(job_id)
    job["question_stats"] = database.get_question_stats(job_id)
    return job


@app.get("/api/jobs/{job_id}/highlights")
def list_highlights(job_id: str) -> dict:
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return {"highlights": database.list_highlights(job_id)}


@app.post("/api/jobs/{job_id}/highlights")
def create_highlight(job_id: str, payload: HighlightCreate) -> dict:
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在。")
    quote = payload.quote.strip()
    if not quote:
        raise HTTPException(status_code=422, detail="没有选中文字。")
    highlight = database.add_highlight(
        job_id, payload.field, quote, payload.start, payload.color
    )
    return {"highlight": highlight}


@app.delete("/api/jobs/{job_id}/highlights/{highlight_id}")
def remove_highlight(job_id: str, highlight_id: int) -> dict:
    if not database.delete_highlight(job_id, highlight_id):
        raise HTTPException(status_code=404, detail="这个标记已经不存在了。")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/questions")
async def generate_job_questions(job_id: str) -> dict:
    """为已有笔记补出练习题（题目功能上线前生成的历史任务走这条）。"""
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if not job["summary"]:
        raise HTTPException(status_code=400, detail="该任务还没有笔记，无法出题。")
    try:
        questions = await asyncio.to_thread(
            summary_service.generate_questions,
            job["title"],
            job["summary"],
            None,
            None,
            job_id,
        )
    except ProviderError as exc:
        raise error_http(exc) from exc
    database.clear_question_stats(job_id)
    database.update_job(job_id, questions=json.dumps(questions, ensure_ascii=False))
    return {"questions": questions}


@app.post("/api/jobs/{job_id}/questions/{index}/wrong")
def mark_question_wrong(job_id: str, index: int, delta: int = 1) -> dict:
    """答错计数：delta=1 记一次错，delta=-1 撤销误点。"""
    if delta not in (-1, 1):
        raise HTTPException(status_code=400, detail="delta 只能是 1 或 -1。")
    if not database.get_job(job_id):
        raise HTTPException(status_code=404, detail="任务不存在。")
    count = database.bump_question_wrong(job_id, index, delta)
    return {"index": index, "wrong_count": count}


@app.post("/api/jobs/{job_id}/confirm")
async def confirm_job(job_id: str) -> dict:
    try:
        return await jobs.confirm(job_id)
    except KeyError as exc:
        raise error_http(exc, 404) from exc
    except ValueError as exc:
        raise error_http(exc) from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    try:
        return jobs.cancel(job_id)
    except KeyError as exc:
        raise error_http(exc, 404) from exc


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    try:
        if not jobs.delete(job_id):
            raise HTTPException(status_code=404, detail="任务不存在。")
        return {"ok": True}
    except ValueError as exc:
        raise error_http(exc) from exc


@app.get("/api/jobs/{job_id}/export")
def export_job(job_id: str) -> PlainTextResponse:
    job = database.get_job(job_id)
    if not job or not job["summary"]:
        raise HTTPException(status_code=404, detail="没有可导出的总结。")
    content = f"{job['summary']}\n"
    if job.get("visual_notes"):
        content += f"\n---\n\n## 课件画面提取内容\n\n{job['visual_notes']}\n"
    content += f"\n---\n\n<details><summary>原始转写</summary>\n\n```text\n{job['transcript']}\n```\n</details>\n"
    filename = f"{Path(job['title']).stem}-学习笔记.md"
    return PlainTextResponse(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/api/jobs/{job_id}/transcript")
def download_transcript(job_id: str) -> PlainTextResponse:
    job = database.get_job(job_id)
    if not job or not job["transcript"]:
        raise HTTPException(status_code=404, detail="没有可导出的转写。")
    filename = f"{Path(job['title']).stem}-转写.txt"
    return PlainTextResponse(
        job["transcript"],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")

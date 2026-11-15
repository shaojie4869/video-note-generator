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
from study_buddy.db import Database
from study_buddy.errors import ProviderError
from study_buddy.jobs import JobManager
from study_buddy.schemas import ApiSettings, BilibiliInfoRequest, GeneralSettings, JobCreate
from study_buddy.sources import BilibiliClient, SourceError, ffmpeg_executable, validate_local_video
from study_buddy.summarizer import OpenAICompatibleProvider, SummaryService

settings = SettingsManager()
database = Database()
summary_service = SummaryService(settings, database)
bilibili_client = BilibiliClient()
jobs = JobManager(database, settings, summary_service, bilibili_client)

# 视觉能力探测要逐个模型发图试探，结果缓存一段时间，避免反复消耗额度。
_VISION_PROBE: dict = {"signature": "", "vision": [], "at": 0.0}
_VISION_PROBE_TTL = 6 * 3600


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.mark_interrupted()
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
        "ffmpeg": ffmpeg_executable(),
        "features": ["questions", "question_stats"],
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


def choose_local_file() -> dict:
    if sys.platform != "win32":
        raise SourceError("当前系统请直接在任务链接中填写视频路径。")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.OpenFileDialog; "
        "$dialog.Title = '选择学习视频'; "
        "$dialog.Filter = '视频文件|*.mp4;*.mkv;*.mov;*.avi;*.flv;*.wmv;*.m4v;*.webm;*.ts'; "
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "[Console]::Write($dialog.FileName) }"
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
    path = validate_local_video(value)
    return {"cancelled": False, "path": str(path), "title": path.stem}


@app.post("/api/videos/browse")
async def browse_video() -> dict:
    try:
        return await asyncio.to_thread(choose_local_file)
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


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": database.list_jobs()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在。")
    job["usage"] = database.job_usage(job_id)
    job["question_stats"] = database.get_question_stats(job_id)
    return job


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

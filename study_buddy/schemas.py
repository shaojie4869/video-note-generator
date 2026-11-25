from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GeneralSettings(BaseModel):
    whisper_model: str | None = None
    whisper_device: Literal["cpu", "cuda", "auto"] | None = None
    whisper_compute_type: str | None = None
    chunk_chars: int | None = Field(default=None, ge=4000, le=50000)
    frames_enabled: bool | None = None
    frame_interval: int | None = Field(default=None, ge=5, le=600)
    max_frames: int | None = Field(default=None, ge=1, le=60)
    deep_refine: bool | None = None
    exam_mode: bool | None = None


class ApiSettings(BaseModel):
    base_url: str = ""
    model: str = ""
    vision_model: str = ""
    reasoning_effort: str = "low"
    api_key: str | None = None


class BilibiliInfoRequest(BaseModel):
    url: str


class JobCreate(BaseModel):
    source_type: Literal["local", "bilibili"]
    source_ref: str
    title: str | None = None
    entry_index: int = Field(default=0, ge=0)
    force: bool = False
    # 生成前指定的课程；留空表示交给自动归组
    course_id: str = ""


class BatchJobCreate(BaseModel):
    """批量生成：一次提交多个视频，后台按机器能力排队跑。"""

    items: list[JobCreate] = Field(min_length=1, max_length=30)


class CourseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CourseRename(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MessageResponse(BaseModel):
    message: str


class HighlightCreate(BaseModel):
    """划重点：只存原文片段和位置，渲染时再贴回 DOM。"""

    field: Literal["summary", "visual"] = "summary"
    quote: str = Field(min_length=1, max_length=2000)
    start: int = Field(default=0, ge=0)
    color: Literal["yellow", "green", "blue", "pink"] = "yellow"


class JobUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    course_id: str | None = None

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
    confirm_long: bool = False


class MessageResponse(BaseModel):
    message: str

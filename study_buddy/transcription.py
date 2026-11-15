from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

# HuggingFace 官方源在国内网络常超时；默认改用镜像站（用户可用环境变量覆盖）。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from .sources import format_timestamp
from .errors import JobCancelled

logger = logging.getLogger(__name__)

_CUDA_DLL_READY = False
# os.add_dll_directory 返回的句柄一旦被回收，目录就会从搜索路径移除，必须持有。
_DLL_HANDLES: list[object] = []


def _prepare_cuda_dlls() -> None:
    """把 pip 版 CUDA 运行库的 DLL 目录加入搜索路径。

    ctranslate2 的 CUDA 推理依赖 cublas64_12.dll / cudnn_ops64_9.dll，这些库若通过
    nvidia-cublas-cu12、nvidia-cudnn-cu12 安装在 site-packages/nvidia/*/bin 下，
    默认不在 Windows 的 DLL 搜索路径里，必须显式注册，否则报 "Library ... not found"。
    """
    global _CUDA_DLL_READY
    if _CUDA_DLL_READY:
        return
    _CUDA_DLL_READY = True
    if os.name != "nt":
        return
    try:
        import site

        roots: list[Path] = []
        for entry in list(site.getsitepackages()) + [site.getusersitepackages()]:
            if entry:
                roots.append(Path(entry) / "nvidia")
        for root in roots:
            if not root.is_dir():
                continue
            for bin_dir in root.glob("*/bin"):
                if not bin_dir.is_dir():
                    continue
                try:
                    _DLL_HANDLES.append(os.add_dll_directory(str(bin_dir)))
                except OSError:
                    pass
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    except Exception:  # 环境探测失败不应影响主流程，后续会自然回退到 CPU
        logger.debug("注册 CUDA 运行库目录失败", exc_info=True)


def cuda_available() -> bool:
    """判断当前环境能否真正跑 GPU 推理（设备可见 + cublas/cudnn 可加载）。"""
    _prepare_cuda_dlls()
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            return False
        if "float16" not in ctranslate2.get_supported_compute_types("cuda"):
            return False
        # get_supported_compute_types 只反映编译期支持，不代表运行时库真的能加载。
        # 这里实拉一次 cublas/cudnn，避免"探测说能用、加载时才报缺库"。
        if os.name == "nt":
            import ctypes

            for dll in ("cublas64_12.dll", "cudnn_ops64_9.dll"):
                ctypes.WinDLL(dll)
        return True
    except Exception:
        return False


class TranscriptionError(Exception):
    pass


class Transcriber:
    def __init__(self) -> None:
        self._models: dict[tuple[str, str, str], object] = {}
        self._lock = threading.Lock()
        # 转写吃满 CPU/GPU，多个任务同时转写会互相抢核反而更慢，这里串行化。
        self._run_lock = threading.Lock()

    def _load_model(self, model_name: str, device: str, compute_type: str):
        """加载并缓存模型；CUDA 不可用时自动降级到 CPU，不让整单失败。"""
        from faster_whisper import WhisperModel

        with self._lock:
            cached = self._models.get((model_name, device, compute_type))
            if cached is not None:
                return cached

            target_device, target_type = device, compute_type
            if device == "auto":
                # 交给运行时自己挑：有可用 GPU 就上 GPU，否则退回 CPU。
                if cuda_available():
                    target_device, target_type = "cuda", "float16"
                else:
                    target_device, target_type = "cpu", "int8"
            if target_device == "cuda" and not cuda_available():
                logger.info("CUDA 不可用，转写改用 CPU/int8")
                target_device, target_type = "cpu", "int8"
                cached = self._models.get((model_name, target_device, target_type))
                if cached is not None:
                    self._models[(model_name, device, compute_type)] = cached
                    return cached

            try:
                model = WhisperModel(model_name, device=target_device, compute_type=target_type)
            except Exception:
                if target_device != "cuda":
                    raise
                logger.warning("CUDA 模型加载失败，回退 CPU/int8", exc_info=True)
                target_device, target_type = "cpu", "int8"
                cached = self._models.get((model_name, target_device, target_type))
                if cached is not None:
                    self._models[(model_name, device, compute_type)] = cached
                    return cached
                model = WhisperModel(model_name, device=target_device, compute_type=target_type)

            entry = (model, target_device)
            self._models[(model_name, target_device, target_type)] = entry
            # 请求的组合也指向同一个实例，避免下次重复加载与重复试错。
            self._models[(model_name, device, compute_type)] = entry
            return entry

    def transcribe(
        self,
        audio_path: Path,
        model_name: str,
        device: str,
        compute_type: str,
        progress: Callable[[int], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> str:
        try:
            from faster_whisper import WhisperModel  # noqa: F401  仅用于提前暴露缺依赖
        except ImportError as exc:
            raise TranscriptionError("本地转写组件未安装，请重新安装项目依赖。") from exc

        try:
            with self._run_lock:
                model, actual_device = self._load_model(model_name, device, compute_type)
                logger.info("开始转写：模型 %s，设备 %s", model_name, actual_device)
                segments, info = model.transcribe(
                    str(audio_path),
                    language="zh",
                    vad_filter=True,
                    beam_size=5,
                    condition_on_previous_text=False,
                )
                lines: list[str] = []
                duration = float(getattr(info, "duration", 0) or 0)
                for segment in segments:
                    if cancelled and cancelled.is_set():
                        raise JobCancelled()
                    value = segment.text.strip()
                    if value:
                        lines.append(f"[{format_timestamp(segment.start)}] {value}")
                    if progress and duration > 0:
                        progress(min(100, int(segment.end / duration * 100)))
            text = "\n".join(lines).strip()
            if not text:
                raise TranscriptionError("没有识别到有效语音，请检查视频音轨。")
            return text
        except (TranscriptionError, JobCancelled):
            raise
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            if "hub" in lowered or "connect" in lowered or "timeout" in lowered or "timed out" in lowered:
                message = (
                    "下载转写模型失败：网络无法连接模型源。已切换国内镜像，"
                    "请稍后重试；若持续失败，可挂代理后再试一次。"
                )
            elif device == "cuda":
                message += "。如为 CUDA 组件问题，请在设置中改用 CPU。"
            raise TranscriptionError(f"语音转写失败：{message}") from exc

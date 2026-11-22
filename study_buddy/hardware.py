"""本机算力探测：批量生成时按这里的结论分配并发。

三种资源性质完全不同，不能用一个「最多同时跑几个任务」粗暴概括：

- 转写（faster-whisper）：吃满显卡或一半以上 CPU，多开只会互相抢资源而变慢，
  所以恒定 1 路；转写模型本身也是全局缓存，多实例还会重复占显存。
- 下载 / 抽音轨 / 截帧：网络与磁盘为主，ffmpeg 单进程能吃 1-2 核，
  普通机器给 2 路就够，低配机器 1 路避免拖慢转写。
- 调模型出笔记：绝大部分时间在等 API 返回，是 I/O 等待而非算力，
  并发 3 路基本能把排队时间摊平；核多内存大的机器给 4 路。
"""
from __future__ import annotations

import ctypes
import functools
import os
import subprocess
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class HardwareProfile:
    cpu_count: int
    ram_gb: float
    gpu: str
    vram_gb: float
    has_gpu: bool
    prepare_slots: int
    transcribe_slots: int
    summarize_slots: int
    max_batch: int

    def as_dict(self) -> dict:
        data = asdict(self)
        data["summary"] = self.description
        return data

    @property
    def description(self) -> str:
        parts = [f"CPU {self.cpu_count} 核", f"内存 {self.ram_gb:.0f} GB"]
        if self.gpu:
            parts.append(f"{self.gpu}" + (f" {self.vram_gb:.0f} GB 显存" if self.vram_gb else ""))
        else:
            parts.append("未检测到 NVIDIA 显卡")
        return " · ".join(parts)


def _total_ram_gb() -> float:
    if os.name != "nt":
        try:
            return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
        except (ValueError, OSError, AttributeError):
            return 0.0

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0.0
    except (AttributeError, OSError):
        return 0.0
    return round(status.ullTotalPhys / 1024**3, 1)


def _nvidia_gpu() -> tuple[str, float]:
    """读显卡型号与显存。只依赖 nvidia-smi，不 import ctranslate2。

    探测本身要足够轻：应用启动时就会用一次，不能为了拿型号把推理库加载一遍。
    """
    try:
        finished = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return "", 0.0
    if finished.returncode != 0:
        return "", 0.0
    line = (finished.stdout or "").strip().splitlines()
    if not line:
        return "", 0.0
    name, _, memory = line[0].partition(",")
    try:
        vram = round(float(memory.strip()) / 1024, 1)
    except ValueError:
        vram = 0.0
    return name.strip(), vram


@functools.lru_cache(maxsize=1)
def profile() -> HardwareProfile:
    cpu = os.cpu_count() or 4
    ram = _total_ram_gb()
    gpu, vram = _nvidia_gpu()
    has_gpu = bool(gpu)

    if has_gpu:
        prepare_slots = 2 if cpu >= 8 else 1
        summarize_slots = 4 if cpu >= 12 and ram >= 24 else 3
    else:
        # 纯 CPU 转写会长时间吃满核心，下载/截帧再抢核只会让转写更慢。
        prepare_slots = 1 if cpu < 8 else 2
        summarize_slots = 2 if cpu < 8 else 3

    return HardwareProfile(
        cpu_count=cpu,
        ram_gb=ram,
        gpu=gpu,
        vram_gb=vram,
        has_gpu=has_gpu,
        prepare_slots=prepare_slots,
        transcribe_slots=1,
        summarize_slots=summarize_slots,
        # 一次最多排这么多：再多也只是让队列变长，用户体验反而更差。
        max_batch=30,
    )

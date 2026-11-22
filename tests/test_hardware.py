from study_buddy import hardware


def _build(monkeypatch, *, cpu, ram, gpu, vram=0.0):
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: cpu)
    monkeypatch.setattr(hardware, "_total_ram_gb", lambda: ram)
    monkeypatch.setattr(hardware, "_nvidia_gpu", lambda: (gpu, vram))
    hardware.profile.cache_clear()
    try:
        return hardware.profile()
    finally:
        hardware.profile.cache_clear()


def test_gpu_machine_runs_transcription_serially_but_summaries_in_parallel(monkeypatch):
    """转写必须串行（会吃满显卡），出笔记是等网络，可以多路。"""
    profile = _build(monkeypatch, cpu=16, ram=32.0, gpu="NVIDIA GeForce RTX 4060", vram=8.0)

    assert profile.has_gpu is True
    assert profile.transcribe_slots == 1
    assert profile.prepare_slots == 2
    assert profile.summarize_slots == 4
    assert "RTX 4060" in profile.description


def test_low_end_machine_keeps_download_single_file(monkeypatch):
    """四核无显卡时，下载/截帧再抢核只会让转写更慢。"""
    profile = _build(monkeypatch, cpu=4, ram=8.0, gpu="")

    assert profile.has_gpu is False
    assert profile.transcribe_slots == 1
    assert profile.prepare_slots == 1
    assert profile.summarize_slots == 2
    assert profile.max_batch == 30
    assert "未检测到 NVIDIA 显卡" in profile.description


def test_profile_is_cached_between_calls(monkeypatch):
    calls = []

    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(hardware, "_total_ram_gb", lambda: calls.append("ram") or 16.0)
    monkeypatch.setattr(hardware, "_nvidia_gpu", lambda: ("GPU", 8.0))
    hardware.profile.cache_clear()
    try:
        hardware.profile()
        hardware.profile()
    finally:
        hardware.profile.cache_clear()
    assert calls == ["ram"]

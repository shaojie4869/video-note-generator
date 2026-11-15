from study_buddy.summarizer import (
    OpenAICompatibleProvider,
    SummaryService,
    _combine_prompt,
    _review_prompt,
    _single_prompt,
    split_transcript,
)


def test_split_transcript_preserves_all_lines():
    text = "\n".join(f"[00:00:{i:02d}] 第{i}段" for i in range(30))
    chunks = split_transcript(text, 80)
    assert len(chunks) > 1
    assert "\n".join(chunks) == text
    assert all(len(chunk) <= 80 for chunk in chunks)


def test_api_endpoint_normalization():
    assert OpenAICompatibleProvider.endpoint("https://a.example/v1/") == "https://a.example/v1/chat/completions"
    assert OpenAICompatibleProvider.endpoint("https://a.example/v1/chat/completions") == "https://a.example/v1/chat/completions"


def test_single_prompt_includes_visual_notes():
    prompt = _single_prompt("操作系统", "转写内容", "画面要点：进程状态图")
    assert "进程状态图" in prompt
    assert "转写内容" in prompt


def test_single_prompt_without_visual_notes():
    prompt = _single_prompt("操作系统", "转写内容", "")
    assert "课件画面" not in prompt


def test_combine_prompt_includes_visual_notes():
    prompt = _combine_prompt("操作系统", ["第一段", "第二段"], "板书：PV 操作")
    assert "PV 操作" in prompt
    assert "第一段" in prompt


def test_describe_frames_batches_images(monkeypatch):
    from pathlib import Path

    from study_buddy.errors import ProviderError
    from study_buddy.summarizer import OpenAICompatibleProvider

    calls = []

    def fake_complete_vision(self, prompt, images, cancelled=None):
        calls.append(len(images))
        return f"批次{len(calls)}结果"

    monkeypatch.setattr(
        OpenAICompatibleProvider, "complete_vision", fake_complete_vision
    )
    provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    frames = [Path(f"frame_{i}.jpg") for i in range(24)]
    result = provider.describe_frames(frames)
    assert calls == [8, 8, 8]
    assert "批次1结果" in result and "批次3结果" in result


def test_describe_frames_falls_back_to_single_images(monkeypatch):
    from pathlib import Path

    from study_buddy.errors import ProviderError
    from study_buddy.summarizer import OpenAICompatibleProvider

    calls = []

    def fake_complete_vision(self, prompt, images, cancelled=None):
        calls.append(len(images))
        if len(images) > 1:
            raise ProviderError("输入图片数量超过限制")
        return "单张结果"

    monkeypatch.setattr(
        OpenAICompatibleProvider, "complete_vision", fake_complete_vision
    )
    provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    frames = [Path(f"frame_{i}.jpg") for i in range(5)]
    result = provider.describe_frames(frames)
    assert calls[0] == 5
    assert calls[1:] == [1, 1, 1, 1, 1]
    assert result.count("单张结果") == 5


class StubSettings:
    def __init__(self, **overrides):
        self.config = {
            "chunk_chars": 14000,
            "deep_refine": True,
            "exam_mode": True,
            "api": {"base_url": "https://a.example/v1", "model": "m", "vision_model": "v"},
        }
        self.config.update(overrides)

    def get(self):
        return self.config

    def get_secret(self, name):
        return "test-key"


def test_single_prompt_requires_four_element_blocks():
    prompt = _single_prompt("操作系统", "转写内容", "")
    for keyword in ("要解决什么", "怎么做的", "代价与前提", "关键细节", "对比与辨析", "时间戳索引", "三句话回忆版"):
        assert keyword in prompt


def test_exam_mode_toggle_changes_prompt():
    assert "考点提示" in _single_prompt("操作系统", "转写", "", True)
    assert "考点提示" not in _single_prompt("操作系统", "转写", "", False)


def test_review_prompt_includes_draft_and_source():
    prompt = _review_prompt("操作系统", "草稿正文", "原始材料正文")
    assert "草稿正文" in prompt
    assert "原始材料正文" in prompt
    assert "自查" in prompt


def test_summarize_runs_review_pass(monkeypatch):
    calls = []

    def fake_complete(self, prompt, cancelled=None):
        calls.append(prompt)
        return "第一版" if len(calls) == 1 else "改写版"

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", fake_complete)
    result = SummaryService(StubSettings()).summarize("操作系统", "转写内容", "")
    assert len(calls) == 2
    assert "第一版" in calls[1]
    assert result.text == "改写版"


def test_summarize_skips_review_when_disabled(monkeypatch):
    calls = []

    def fake_complete(self, prompt, cancelled=None):
        calls.append(prompt)
        return "草稿"

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", fake_complete)
    result = SummaryService(StubSettings(deep_refine=False)).summarize("操作系统", "转写", "")
    assert len(calls) == 1
    assert result.text == "草稿"


def test_chunked_summarize_combines_then_reviews(monkeypatch):
    calls = []

    def fake_complete(self, prompt, cancelled=None):
        calls.append(prompt)
        return f"结果{len(calls)}"

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", fake_complete)
    settings = StubSettings(chunk_chars=4000)
    transcript = "\n".join(f"[00:0{i}:00] 第{i}段内容，这里补充足够长的文字以便触发分段。" * 3 for i in range(60))
    result = SummaryService(settings).summarize("操作系统", transcript, "")
    assert len(calls) >= 3
    assert "分段提取结果" in calls[-2]
    assert "【笔记草稿】" in calls[-1]
    assert result.text == f"结果{len(calls)}"


def test_list_models_reads_models_endpoint(monkeypatch):
    import httpx

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "glm-5.3-flash"}, {"id": "glm-4.5"}, {"id": "glm-5.3-flash"}]}

    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization")
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)
    provider = OpenAICompatibleProvider(StubSettings())
    assert provider.list_models() == ["glm-4.5", "glm-5.3-flash"]
    assert seen["url"].endswith("/models")
    assert seen["auth"] == "Bearer test-key"


def test_probe_vision_models_keeps_only_image_capable(monkeypatch):
    import httpx

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status

    def fake_post(url, headers=None, json=None, timeout=None):
        capable = {"glm-5.3-flash", "glm-5.3-flashx"}
        return FakeResponse(200 if json["model"] in capable else 400)

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = OpenAICompatibleProvider(StubSettings())
    names = ["glm-4.5", "glm-5.3-flash", "glm-5.3-flashx", "glm-5"]
    assert provider.probe_vision_models(names) == ["glm-5.3-flash", "glm-5.3-flashx"]


def test_probe_vision_models_handles_network_error(monkeypatch):
    import httpx

    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = OpenAICompatibleProvider(StubSettings())
    assert provider.probe_vision_models(["glm-5.3-flash"]) == []


def test_complete_records_usage(monkeypatch):
    import httpx

    recorded = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
            }

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: FakeResponse())
    provider = OpenAICompatibleProvider(StubSettings(), lambda *args: recorded.append(args), "job1")
    assert provider.complete("你好") == "ok"
    assert recorded == [("job1", "summary", "m", 12, 34, 46)]


def test_vision_records_usage(monkeypatch):
    import httpx
    from pathlib import Path

    recorded = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "板书"}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520},
            }

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr("study_buddy.summarizer._encode_frame", lambda path, max_side=1024: "data:image/jpeg;base64,x")
    provider = OpenAICompatibleProvider(StubSettings(), lambda *args: recorded.append(args), "job2")
    assert provider.complete_vision("看图", [Path("a.jpg")]) == "板书"
    assert recorded == [("job2", "vision", "v", 500, 20, 520)]


def test_usage_recorder_failure_does_not_break_call(monkeypatch):
    import httpx

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    def boom(*args):
        raise RuntimeError("disk full")

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: FakeResponse())
    provider = OpenAICompatibleProvider(StubSettings(), boom, "job3")
    assert provider.complete("你好") == "ok"

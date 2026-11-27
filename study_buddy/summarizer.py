from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import SettingsManager
from .errors import JobCancelled, ProviderError
from .syllabus408 import syllabus_block

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 408 计算机考研专业课的笔记整理专家。你的产出要能直接拿去复习和做题，不是内容摘要。

硬性规则：
1. 只使用给定材料中的事实；材料没有的，不补充、不推测。
2. 材料中的语音转写来自识别引擎，存在同音错字，必须按计算机课程语境纠正后再写。常见错法举例：脱节IO/脱机IO → 脱机 I/O（off-line I/O）、批出理 → 批处理、死琐 → 死锁、缓存区 → 缓冲区、间址 → 间接寻址、页表顶 → 页表项。
3. 无法判断的内容写“（转写不清）”，绝不编造。
4. 不写空话：“很重要”“不断提高”“不断发展”“具有重要意义”这类没有信息量的句子一律不要。
5. 保留老师强调的具体条件、数字、顺序、年代，这些往往就是考点。
6. 删除口语冗余（“对吧”“是不是”“这个这个”），不要复述老师的口头语。
7. 提示词里出现的句式、占位词（如“可能的选择题考法”“XX 维度”）只是格式说明，不是内容。必须替换成具体文字，禁止原样照抄。
8. 不确定的内容不要编造表格数值；宁可写“—”或“材料未提及”。"""


def _exam_block(exam_mode: bool) -> str:
    if not exam_mode:
        return ""
    return """
## 考点提示
逐个知识点写具体考法，格式示例（这是示例，不是让你照抄）：
- 脱机 I/O：选择题常考“它解决的是 CPU 与 I/O 速度不匹配，而不是网络问题”；与第 5 章 I/O 管理的缓冲技术放在一起辨析。
禁止写出“可能的选择题考法 / 可能的大题考法 / 与其他章节的关联”这种空白占位。若本段材料确实看不出具体考法，写“常见考法：概念辨析（本节材料未展开）”。"""


def _writing_rules() -> str:
    return """
写作要求（不满足等于不合格）：
1. 每个知识块 3-6 行，宁可少而实，不要多而空；不要把知识点压成一句定义。
2. 每条内容都要能回答“是什么 / 为什么需要 / 怎么做 / 有什么代价”中的至少两项。
3. 术语首次出现给出英文或缩写，例如 脱机 I/O (off-line I/O)、进程控制块 (PCB)。
4. 时间戳只出现在“时间戳索引”和必要的知识点标题后，正文里不要堆时间戳。
5. 结构里的章节若材料确实没讲，写“本节未涉及。”，不要留空标题，也不要拿无关内容凑数。"""


VISION_PROMPT = """以下是按时间顺序截取的课程视频画面。请提取其中的学习信息：
- PPT/课件上的标题、要点、定义、公式、图表内容
- 板书文字、代码、命令行内容
- 其他与课程内容直接相关的画面信息

按画面出现顺序整理为要点列表（可标注大致时间点）；与课程无关的画面（片头、广告、纯人脸特写等）直接忽略；看不清的内容不要编造。"""


def _encode_frame(path: Path, max_side: int = 1024) -> str:
    """把截图压缩并编码为 data URL，控制请求体积。"""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ProviderError("图像处理组件未安装，请重新安装项目依赖。") from exc
    try:
        image = Image.open(path).convert("RGB")
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=70)
    except Exception as exc:
        raise ProviderError(f"课件截图处理失败：{exc}") from exc
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _probe_image_data_url() -> str:
    """生成一张极小测试图，用于探测模型是否接受图片输入。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise ProviderError("图像处理组件未安装，请重新安装项目依赖。") from exc
    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).text((24, 52), "VISION TEST 42", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class OpenAICompatibleProvider:
    name = "api"

    def __init__(
        self,
        settings: SettingsManager,
        usage_recorder: Callable[..., None] | None = None,
        job_id: str = "",
    ) -> None:
        self.settings = settings
        # 用量记录器签名：(job_id, purpose, model, prompt_tokens, completion_tokens, total_tokens)
        self.usage_recorder = usage_recorder
        self.job_id = job_id

    def _record_usage(self, payload: dict, data: dict, purpose: str) -> None:
        """把服务端返回的 usage 落到本地账本，失败不影响主流程。"""
        if not self.usage_recorder:
            return
        usage = data.get("usage") or {}
        try:
            self.usage_recorder(
                self.job_id,
                purpose,
                str(payload.get("model") or ""),
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
            )
        except Exception:
            pass

    def _config(self) -> tuple[str, str, str]:
        config = self.settings.get()["api"]
        api_key = self.settings.get_secret("api_key") or ""
        if not config.get("base_url") or not config.get("model") or not api_key:
            raise ProviderError("API 尚未配置完整，请在设置中填写地址、模型和 Key。")
        return config["base_url"], config["model"], api_key

    @staticmethod
    def endpoint(base_url: str) -> str:
        base_url = base_url.strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def _post(
        self, payload: dict, cancelled: threading.Event | None, purpose: str = "summary"
    ) -> str:
        if cancelled and cancelled.is_set():
            raise JobCancelled()
        base_url, _, api_key = self._config()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = self.endpoint(base_url)
        response = None
        # 分块并发时很容易撞上限流：429 属于「等一会儿就好」，退避一次不够，
        # 平台按分钟计时，1.5 秒后重试往往还在窗口里，会把整单判死。
        attempts = 4
        for attempt in range(attempts):
            if cancelled and cancelled.is_set():
                raise JobCancelled()
            try:
                response = httpx.post(url, headers=headers, json=payload, timeout=600)
            except httpx.HTTPError as exc:
                if attempt < 2:
                    self._wait(1.5 * (attempt + 1), cancelled)
                    continue
                raise ProviderError(f"API 网络请求失败：{exc}") from exc
            if response.status_code == 429 and attempt < attempts - 1:
                self._wait(self._retry_after(response, attempt), cancelled)
                continue
            if response.status_code in {500, 502, 503, 504} and attempt < 2:
                self._wait(1.5 + attempt * 2, cancelled)
                continue
            break
        assert response is not None
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"API 返回格式异常（HTTP {response.status_code}）。") from exc
        if response.status_code >= 400:
            error = data.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ProviderError(f"API 请求失败：{message or response.status_code}")
        self._record_usage(payload, data, purpose)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("API 响应中没有总结内容，请检查接口是否为 OpenAI 兼容协议。") from exc
        if content is None:
            raise ProviderError("API 返回了空总结。")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
            )
        if not str(content).strip() or str(content).strip().lower() == "none":
            raise ProviderError("API 返回了空总结。")
        return str(content).strip()

    def _wait(self, seconds: float, cancelled: threading.Event | None) -> None:
        """可被取消的等待：限流退避可能几十秒，期间点取消要能立刻停下。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if cancelled and cancelled.is_set():
                raise JobCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))

    @staticmethod
    def _retry_after(response: Any, attempt: int) -> float:
        """429 优先听服务端给的 Retry-After，没给就指数退避（5/10/20 秒）。"""
        try:
            raw = response.headers.get("Retry-After")
        except (AttributeError, TypeError):
            raw = None
        if raw:
            try:
                return max(1.0, min(float(raw), 60.0))
            except (TypeError, ValueError):
                pass
        return min(5.0 * (2**attempt), 30.0)

    def complete(
        self, prompt: str, cancelled: threading.Event | None = None, purpose: str = "summary"
    ) -> str:
        config = self.settings.get()["api"]
        model = config["model"]
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        # 推理型模型（如 glm-5 系列）默认思考很慢，低档位速度可快 8 倍以上；
        # 非推理模型会忽略该参数。空值表示不传，交给服务端默认。
        effort = (config.get("reasoning_effort") or "").strip()
        if effort:
            payload["reasoning_effort"] = effort
        return self._post(payload, cancelled, purpose)

    def _post_vision(self, url: str, api_key: str, payload: dict) -> httpx.Response:
        """视觉请求补一次退避重试：连接抖动和 5xx 都常见，失败一次不值得整单报废。"""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = httpx.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=300,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise
            if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                time.sleep(2)
                continue
            return response
        raise last_error or ProviderError("视觉模型请求失败。")

    def complete_vision(
        self, prompt: str, images: list[Path], cancelled: threading.Event | None = None
    ) -> str:
        config = self.settings.get()["api"]
        vision_model = (config.get("vision_model") or "").strip()
        if not vision_model:
            raise ProviderError("尚未配置视觉模型，请在设置中填写（如 glm-4v-flash）。")
        content: list[dict] = [{"type": "text", "text": prompt}]
        for image in images:
            if cancelled and cancelled.is_set():
                raise JobCancelled()
            content.append({"type": "image_url", "image_url": {"url": _encode_frame(image)}})
        base_url, _, api_key = self._config()
        payload: dict = {
            "model": vision_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
            "stream": False,
        }
        # 视觉模型同样是推理模型：默认档的隐藏思考能吃掉几千 token，
        # 实测同样 8 张图，low 档 17 秒、默认档 80 秒以上，末批会直接顶到超时。
        # 注意不能用 max_tokens 限流——思考会把额度占满，正文变成空字符串。
        effort = (config.get("reasoning_effort") or "").strip()
        if effort:
            payload["reasoning_effort"] = effort
        try:
            response = self._post_vision(self.endpoint(base_url), api_key, payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"视觉模型网络请求失败：{exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"视觉模型返回格式异常（HTTP {response.status_code}）。") from exc
        if response.status_code >= 400:
            error = data.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ProviderError(f"视觉模型请求失败：{message or response.status_code}")
        self._record_usage({"model": vision_model}, data, "vision")
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("视觉模型响应中没有内容。") from exc
        if isinstance(text, list):
            text = "\n".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in text
            )
        return str(text or "").strip()

    def probe_vision_models(
        self, models: list[str], cancelled: threading.Event | None = None
    ) -> list[str]:
        """逐个模型发一张测试图，筛出真正接受图片输入的模型。

        /models 只返回账号可用的模型名，不标注多模态能力，只能靠试探区分。
        拒绝读图的模型会立刻返回参数错误，只有支持读图的模型才真正跑推理。
        """
        if not models:
            return []
        base_url, _, api_key = self._config()
        endpoint = self.endpoint(base_url)
        image_url = _probe_image_data_url()
        common = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "图中英文写的是什么？只回答文字。"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": 32,
            "reasoning_effort": "low",
        }

        def accepts(model: str) -> bool:
            if cancelled and cancelled.is_set():
                return False
            try:
                response = httpx.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": model, **common},
                    timeout=90,
                )
            except httpx.HTTPError:
                return False
            return response.status_code == 200

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=6) as pool:
            flags = list(pool.map(accepts, models))
        return [name for name, ok in zip(models, flags) if ok]

    def list_models(self) -> list[str]:
        """读取服务端开放给当前 Key 的模型名（OpenAI 兼容的 /models 接口）。"""
        base_url, _, api_key = self._config()
        base = base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        try:
            response = httpx.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"获取模型列表失败：{exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"该服务未提供模型列表接口（HTTP {response.status_code}），请手动填写模型名。"
            )
        try:
            items = response.json().get("data") or []
            return sorted({str(item.get("id")) for item in items if item.get("id")})
        except (ValueError, AttributeError) as exc:
            raise ProviderError("模型列表返回格式异常，请手动填写模型名。") from exc

    def describe_frames(
        self, frames: list[Path], cancelled: threading.Event | None = None
    ) -> str:
        """分批识别画面，单批失败就拆半重试，仍失败则跳过该批。

        画面只是笔记的辅助材料：转写已经花掉大部分时间，为几张截图丢掉整单不划算。
        拆半既能应对「模型限制单张图片」，也能应对偶发超时。
        """
        batch_size = 8
        results: list[str] = []
        for index, start in enumerate(range(0, len(frames), batch_size)):
            batch = frames[start : start + batch_size]
            results.extend(self._describe_batch(batch, index, cancelled, depth=0))
        return "\n".join(part for part in results if part).strip()

    def _describe_batch(
        self,
        batch: list[Path],
        index: int,
        cancelled: threading.Event | None,
        depth: int,
    ) -> list[str]:
        if cancelled and cancelled.is_set():
            raise JobCancelled()
        if not batch:
            return []
        header = f"{VISION_PROMPT}\n（本批次为第 {index + 1} 批画面，共 {len(batch)} 张）"
        try:
            part = self.complete_vision(header, batch, cancelled)
        except JobCancelled:
            raise
        except ProviderError as exc:
            message = str(exc)
            if len(batch) > 1 and depth < 4:
                if "图片数量" in message or "image" in message.lower():
                    # 模型只接受单张图片：直接逐张，不必浪费轮次去拆半。
                    return [
                        part
                        for frame in batch
                        for part in self._describe_batch([frame], index, cancelled, depth + 1)
                    ]
                logger.warning("画面第 %d 批 %d 张识别失败，拆半重试：%s", index + 1, len(batch), exc)
                middle = len(batch) // 2
                return self._describe_batch(
                    batch[:middle], index, cancelled, depth + 1
                ) + self._describe_batch(batch[middle:], index, cancelled, depth + 1)
            logger.warning("跳过一张画面（第 %d 批）：%s", index + 1, exc)
            return []
        return [part] if part else []


@dataclass
class SummaryResult:
    text: str
    provider: str


def split_transcript(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for raw_line in text.splitlines():
        lines = [raw_line[i : i + max_chars] for i in range(0, len(raw_line), max_chars)] or [""]
        for line in lines:
            extra = len(line) + 1
            if current and size + extra > max_chars:
                chunks.append("\n".join(current))
                current = []
                size = 0
            current.append(line)
            size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


CHUNK_CONCURRENCY = 4


def _extract_chunks(
    provider: "OpenAICompatibleProvider",
    title: str,
    chunks: list[str],
    progress: Callable[[int, str], None] | None,
    cancelled: threading.Event | None,
) -> list[str]:
    """并行提取各段知识点。

    分块之间互不依赖，串行只是历史写法。并发后总耗时从「块数 × 单块」降到
    「单块 + 少量排队」，长课（3 段以上）能省掉一半以上的等待。
    """
    total = len(chunks)
    partials = [""] * total
    if total <= 1:
        if total == 1:
            partials[0] = provider.complete(_chunk_prompt(title, chunks[0], 0, 1), cancelled)
        return partials

    def work(index: int) -> tuple[int, str]:
        if cancelled and cancelled.is_set():
            raise JobCancelled()
        return index, provider.complete(
            _chunk_prompt(title, chunks[index], index, total), cancelled
        )

    workers = max(1, min(CHUNK_CONCURRENCY, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, index): index for index in range(total)}
        completed = 0
        try:
            for future in as_completed(futures):
                if cancelled and cancelled.is_set():
                    for pending in futures:
                        pending.cancel()
                    raise JobCancelled()
                index, text = future.result()
                partials[index] = text
                completed += 1
                if progress:
                    progress(
                        int(completed / (total + 1) * 80),
                        f"已提取 {completed}/{total} 段知识点",
                    )
        except BaseException:
            for pending in futures:
                pending.cancel()
            raise
    return partials


def _visual_section(visual_notes: str) -> str:
    if not visual_notes.strip():
        return ""
    return f"""
另外，课程视频画面中还提取到以下内容（来自课件/板书截图，按时间顺序）：
{visual_notes}

画面里的表格、流程、板书结构往往比口述更接近考点，优先把画面里的结构还原进笔记；与讲述矛盾时以老师讲述为准，并在笔记里注明。"""


def _structure_block(title: str, exam_mode: bool) -> str:
    return f"""必须严格按以下结构输出 Markdown，标题措辞不要改动：

# {title} - 学习笔记

## 本节要解决什么问题
2-3 句说明这节课在回答什么实际问题、上一代方案差在哪里。禁止用“本节课介绍了……”这类复述式开头。

## 知识地图
6-12 条短句给出逻辑主线：前一个方案的什么缺陷，逼出了后一个方案。要写因果，不要只堆名词。

## 知识点详解
每个知识点一个小块，四要素固定顺序，缺一不可：
### 名称（英文/缩写）
- **要解决什么**：上一代方案的具体缺陷
- **怎么做的**：机制或流程，必要时 3-5 步
- **代价与前提**：需要什么硬件/机制支持，又带来什么新问题
- **关键细节**：容易考的条件、数量、顺序、年代

## 对比与辨析
给一张 Markdown 表格，**只对比老师在本节明确放在一起比较的方案**（例如 单道批处理 vs 多道批处理、分时 vs 实时），最多 5 列、3-4 个对比维度。
要求：表格单元格里的每个值都要能在材料里找到依据；材料没讲的维度直接不列这一行，不要为了凑满表格而填“高/低”这种没有依据的结论；确实没有可比对象时写“本节无同类对比”。

## 易错点
每条写成“容易错成 X → 实际是 Y，因为 Z”。如果 X 和 Y 说的是同一件事，说明这条不是易错点，删掉重找。至少 3 条，优先取老师反复强调或现场纠错的地方。

## 记忆锚点
每个知识点一句能在考场唤起记忆的话或口诀，不要重复定义。
{_exam_block(exam_mode)}
## 复习自测
5-10 题，考理解与辨析，禁止出“XX 的定义是什么”。每题给出答案和一句话解析。

## 三句话回忆版
三句话分别对应：①本节的主线（谁解决了谁的什么问题）②最关键的一个机制 ③最容易错的一处。禁止写“反映了技术的不断进步”“具有重要意义”这类总结腔。

## 时间戳索引
只保留真正开始讲一个新知识点的时刻，最多 10 条，格式 `[mm:ss] 知识点`。不要罗列“课程开始 / 开始讲解 / 结束本段 / 强调重要性”这类无信息量的节点。"""


def _writing_rules() -> str:
    return """
写作要求（不满足视为不合格）：
1. 每个知识块 3-6 行，宁可少而实，不要多而空；不要把知识点压成一句定义。
2. 每条内容至少要能回答“是什么 / 为什么需要 / 怎么做 / 有什么代价”中的两项。
3. 术语首次出现给出英文或缩写，例如 脱机 I/O (off-line I/O)、进程控制块 (PCB)。
4. 时间戳只出现在“时间戳索引”和必要的知识点标题后，正文里不要堆时间戳。
5. 结构里的章节若材料确实没讲，写“本节未涉及。”，不要留空标题，也不要拿无关内容凑数。"""


def _single_prompt(title: str, transcript: str, visual_notes: str, exam_mode: bool = True) -> str:
    return f"""请把课程《{title}》整理成能直接用于复习和做题的 Markdown 学习笔记。

{_structure_block(title, exam_mode)}
{_writing_rules()}{_visual_section(visual_notes)}

【转写材料】
{transcript}"""


def _chunk_prompt(title: str, chunk: str, index: int, total: int) -> str:
    return f"""这是课程《{title}》语音转写的第 {index + 1}/{total} 段。请做信息提取，不要写成文章。

要求：
1. 逐个列出本段出现的知识点，每个都包含：名称、老师讲的机制或步骤、举的例子、强调的易错点、出现时间 [mm:ss]。
2. 保留老师原话里的关键论断、数字、年代、顺序。
3. 纠正语音识别的同音错字（例如 脱节IO → 脱机 I/O）。
4. 不要概括成一句话，不要写过渡句和总结句；本段没讲的不要补。

本段转写：
{chunk}"""


def _combine_prompt(
    title: str, partials: list[str], visual_notes: str, exam_mode: bool = True
) -> str:
    joined = "\n\n".join(f"### 第 {i + 1} 段提取结果\n{value}" for i, value in enumerate(partials))
    return f"""下面是把课程《{title}》按段提取出的知识点。请合并成一份完整的学习笔记：合并重复的知识点、按讲解顺序重排、补全跨段才能讲清的因果关系。

{_structure_block(title, exam_mode)}
{_writing_rules()}{_visual_section(visual_notes)}

只依据下面的分段提取结果，不新增事实；分段之间重叠的内容只保留最完整的那一份。

【分段提取结果】
{joined}"""


def _review_prompt(title: str, draft: str, source: str) -> str:
    return f"""下面是课程《{title}》的学习笔记草稿，以及生成它所用的原始材料。请做一次严格自查，然后**直接输出改写后的完整笔记**：不要输出点评，不要解释改了什么，不要写“已按要求修改”。

逐项自查，发现一项改一项：
1. 有没有“一句话定义式”的空洞条目 → 按四要素展开成 3-6 行的知识块。
2. 有没有“很重要/不断提高/不断发展”这类空话 → 删除或替换成具体机制。
3. 对比表是否覆盖了全部同类方案（单道/多道、分时/实时、批处理/分时等）？
4. 易错点是否只是名词解释 → 改成“容易错成 X → 实际是 Y，因为 Z”。
5. 材料里明显讲过、草稿却漏掉的知识点 → 补上（只能依据材料）。
6. 语音识别错字是否已纠正（脱机 I/O、死锁、缓冲区、页表项……）？
7. 时间戳索引是否超过 10 条、是否混入无信息量节点 → 精简。
8. 自测题是否只是名词复述 → 改成理解题。
9. 有没有照抄提示词占位句（如“可能的选择题考法”“与其他章节的关联”）→ 必须替换成具体内容，写不出就按规则降级表述。
10. 对比表是否出现整列“高/高/高”、或填了材料没有的数值 → 删掉无依据的行/列，宁可只留 2 行。
11. 有没有“X 和 Y 其实是同一件事”的假易错点、“不断进步/具有重要意义”这类空话 → 删除重写。

结构保持不变，不要新增材料之外的内容。

【原始材料】
{source}

【笔记草稿】
{draft}"""


QUESTION_COUNT = 12
QUESTION_COUNT_MIN = 6
QUESTION_COUNT_MAX = 24
# 题量按笔记字数分档：一篇 2 千字的短笔记硬塞 12 题只能靠凑，
# 一篇 2 万字的大课只出 12 题又覆盖不到一半知识点。
_QUESTION_TIERS = (
    (2000, 6),
    (3500, 8),
    (5500, 10),
    (8000, 12),
    (11000, 14),
    (14000, 16),
    (18000, 18),
)


def question_count_for(summary: str) -> int:
    """按笔记字数决定出几道题（页面不传题量时的默认值）。"""
    text = (summary or "").strip()
    if not text:
        return QUESTION_COUNT_MIN
    chars = len(text)
    for limit, value in _QUESTION_TIERS:
        if chars < limit:
            return value
    # 一万八千字以上：再大的课也不无限加，最多 24 题（再多也复习不完）
    return 22 if chars < 24000 else QUESTION_COUNT_MAX


def question_mix(count: int) -> dict[str, int]:
    """按题量给出题型与难度目标，各项之和等于 count。"""
    count = max(QUESTION_COUNT_MIN, min(int(count), QUESTION_COUNT_MAX))
    singles = max(3, round(count * 0.5))
    judges = max(2, round(count * 0.25))
    shorts = count - singles - judges
    while shorts < 2 and singles > 2:
        singles -= 1
        shorts += 1
    easy = round(count / 3)
    medium = round(count / 3)
    hard = count - easy - medium
    return {
        "single": singles,
        "judge": judges,
        "short": shorts,
        "easy": easy,
        "medium": medium,
        "hard": hard,
    }

QUESTION_PROMPT = """你是 408 计算机考研的出题老师。请根据下面的学习笔记，为课程《{{TITLE}}》出 {{COUNT}} 道复习题。

【一、素材边界（最高优先级）】
1. 只考笔记里确实讲过的知识点和原理，不引入笔记之外的内容。
2. 严禁把「举例里的数字、配置、场景」当考点。例如「某台服务器 CPU 使用率是多少」「老师举的例子里有几台机器」这类题一律不要。
   只有两种情况可以出现数字：① 408 要求记住的常量（页大小 4KB、Cache 行 64B、常用端口号、复杂度量级等）；② 题干给出条件、需要考生计算。
3. 题干不得出现「笔记中提到」「老师举例」「视频里说」这类指向材料的措辞，也不能写成对原文的复述；必须能脱离笔记独立作答。
4. 举例只能用来构造情景题：给一个场景问「这对应什么原理 / 为什么这样做」，不能问「例子里是几」。

【二、出题重点】
5. 优先围绕笔记里的「考点提示」出题；它标了「只需掌握思路」「不考特别深」的，就只出概念判断题，不考细节。
6. 尽量覆盖不同知识点，同一考点最多出 2 题；优先覆盖会考、易混、易错的点，不要平均用力。
7. 选择题的每个错误选项都要对应一个真实的常见误解（例如把「提高利用率」错成「提高主频」），不要用互不相干的数字凑选项。

【三、难度与题型】
8. 难度按题量分配：easy {{EASY}} 道（概念归属与辨析，不背数字）；medium {{MEDIUM}} 道（因果与条件：为什么、在什么前提下成立）；hard {{HARD}} 道（对比、边界或计算：给条件推结论，或两种方案怎么选）。hard 不得少于全卷的三分之一。
9. 题型按题量分配：选择题 {{SINGLE}} 道、判断题 {{JUDGE}} 道、简答题 {{SHORT}} 道，合计 {{COUNT}} 道。
10. 题量已经按本篇笔记的篇幅定好（笔记越长、知识点越多，题目越多），不要再自行增减；请把每道题落在不同的知识点上，不要为了凑数重复同一考点。
11. 每题都能独立作答；答案写具体结论，禁止「见笔记」「参考原文」；解析用一句话说明考什么、为什么这样答。

{{SYLLABUS}}

只输出 JSON 数组，不要任何解释文字，不要 Markdown 代码块标记，不要注释。格式：
[
  {
    "type": "single 或 judge 或 short 或 blank",
    "question": "题干",
    "options": ["A. 选项一", "B. 选项二"],
    "answer": "标准答案",
    "explanation": "一句话解析",
    "topic": "所属知识点",
    "difficulty": "easy 或 medium 或 hard"
  }
]
说明：type 为 single 时 options 至少 2 项；其余题型 options 留空数组。

【学习笔记】
{{CONTENT}}"""

_QUESTION_TYPES = {"single", "judge", "short", "blank"}
_DIFFICULTIES = {"easy", "medium", "hard"}


def parse_questions(raw: str) -> list[dict[str, Any]]:
    """把模型返回的 JSON 解析成规范题目列表；任何异常都退化成空列表，不向上抛。"""
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []

    questions: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        if not question or not answer:
            continue
        qtype = str(entry.get("type") or "short").strip().lower()
        if qtype not in _QUESTION_TYPES:
            qtype = "short"
        raw_options = entry.get("options") or []
        options = (
            [str(item).strip() for item in raw_options if str(item).strip()]
            if isinstance(raw_options, list)
            else []
        )
        # 选择题没有选项就降级成简答题，避免前端渲染出空选项。
        if qtype == "single" and len(options) < 2:
            qtype = "short"
        difficulty = str(entry.get("difficulty") or "medium").strip().lower()
        if difficulty not in _DIFFICULTIES:
            difficulty = "medium"
        questions.append(
            {
                "type": qtype,
                "question": question,
                "options": options if qtype == "single" else [],
                "answer": answer,
                "explanation": str(entry.get("explanation") or "").strip(),
                "topic": str(entry.get("topic") or "").strip(),
                "difficulty": difficulty,
            }
        )
    return questions


# 指向材料的措辞：这类题干本质是「回忆原文」，真实考题不会这么问
_MATERIAL_CITATION = re.compile(
    r"笔记(?:中|里|上)?(?:提到|所说|所述|讲到)"
    r"|老师(?:说|讲|举例|强调|提到)"
    r"|视频(?:中|里)"
    r"|课上(?:说|讲|提到)"
    r"|教材(?:中|里)"
)
# 拿老师随口举的场景凑出来的题
_EXAMPLE_SCENE = re.compile(r"某(?:台|个)?(?:服务器|机器|机房|公司|学校)|假设有\s*\d+\s*台")
# 选项全是 10%、30% 这种纯数字，说明是从例子里抄的
_PURE_NUMBER_OPTION = re.compile(r"^(?:[A-Da-d][.、．)]\s*)?\d+(?:\.\d+)?\s*[%％]?$")
# 出现这些单位/概念时，数字可能是考纲要求记住的常量，不能一刀切
_NUMBER_HINTS = (
    "页", "块", "字节", "位", "KB", "MB", "GB", "ns", "ms", "us", "μs",
    "端口", "复杂度", "容量", "频率", "带宽", "速率",
)


def _is_low_value(question: dict[str, Any]) -> bool:
    """判断一道题是不是「背例子细节」的低价值题。"""
    body = str(question.get("question") or "")
    if _MATERIAL_CITATION.search(body) or _EXAMPLE_SCENE.search(body):
        return True
    options = question.get("options") or []
    if len(options) >= 2 and all(_PURE_NUMBER_OPTION.match(option) for option in options):
        if not any(hint in body for hint in _NUMBER_HINTS):
            return True
    return False


def filter_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剔除低价值题并去重。

    宁可比要求少一两道，也不要让用户去背老师随口举的数字。
    """
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for question in questions:
        if _is_low_value(question):
            continue
        key = re.sub(r"\s+", "", str(question.get("question") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(question)
    return kept


def question_gaps(questions: list[dict[str, Any]], count: int) -> list[str]:
    """检查题量、题型与难度配额，返回差距描述；空列表表示合格。

    模型经常偷懒：easy 一多、hard 就没了，所以光在提示词里写「各至少 3 题」不够，
    得真检查一遍。配额随题量浮动（见 question_mix），短笔记不强求 12 题的分布。
    """
    gaps: list[str] = []
    if len(questions) < count:
        gaps.append(f"题量只有 {len(questions)} 道，需要 {count} 道")
    target = question_mix(count)
    types: dict[str, int] = {}
    levels: dict[str, int] = {}
    for question in questions:
        types[question["type"]] = types.get(question["type"], 0) + 1
        levels[question["difficulty"]] = levels.get(question["difficulty"], 0) + 1
    if types.get("single", 0) < max(2, target["single"] - 1):
        gaps.append(f"选择题只有 {types.get('single', 0)} 道，至少 {max(2, target['single'] - 1)} 道")
    if types.get("judge", 0) < max(1, target["judge"] - 1):
        gaps.append(f"判断题只有 {types.get('judge', 0)} 道，至少 {max(1, target['judge'] - 1)} 道")
    if types.get("short", 0) + types.get("blank", 0) < max(1, target["short"] - 1):
        gaps.append(f"简答题少于 {max(1, target['short'] - 1)} 道")
    if levels.get("easy", 0) > target["easy"] + 1:
        gaps.append(f"easy 有 {levels.get('easy', 0)} 道，最多 {target['easy'] + 1} 道")
    if levels.get("medium", 0) < max(2, target["medium"] - 1):
        gaps.append(f"medium 只有 {levels.get('medium', 0)} 道，至少 {max(2, target['medium'] - 1)} 道")
    if levels.get("hard", 0) < max(2, target["hard"] - 1):
        gaps.append(f"hard 只有 {levels.get('hard', 0)} 道，至少 {max(2, target['hard'] - 1)} 道")
    return gaps


class SummaryService:
    def __init__(self, settings: SettingsManager, database=None) -> None:
        self.settings = settings
        self.database = database

    def summarize(
        self,
        title: str,
        transcript: str,
        visual_notes: str,
        progress: Callable[[int, str], None] | None = None,
        cancelled: threading.Event | None = None,
        job_id: str = "",
    ) -> SummaryResult:
        config = self.settings.get()
        exam_mode = bool(config.get("exam_mode", True))
        deep_refine = bool(config.get("deep_refine", True))
        chunks = split_transcript(transcript, int(config.get("chunk_chars", 14000)))
        provider = OpenAICompatibleProvider(
            self.settings,
            self.database.add_usage if self.database else None,
            job_id,
        )

        if len(chunks) == 1:
            if progress:
                progress(40, "正在生成笔记")
            draft = provider.complete(
                _single_prompt(title, transcript, visual_notes, exam_mode), cancelled
            )
            source = transcript
        else:
            if progress:
                progress(2, f"正在并行提取 {len(chunks)} 段知识点")
            partials = _extract_chunks(provider, title, chunks, progress, cancelled)
            if progress:
                progress(82, "正在合并分段笔记并去重")
            draft = provider.complete(
                _combine_prompt(title, partials, visual_notes, exam_mode), cancelled
            )
            source = "\n\n".join(partials)

        if deep_refine:
            if cancelled and cancelled.is_set():
                raise JobCancelled()
            if progress:
                progress(90, "正在自查补全细节")
            draft = provider.complete(_review_prompt(title, draft, source), cancelled)

        return SummaryResult(text=draft, provider=provider.name)

    def generate_questions(
        self,
        title: str,
        summary: str,
        progress: Callable[[int, str], None] | None = None,
        cancelled: threading.Event | None = None,
        job_id: str = "",
        count: int | None = None,
    ) -> list[dict[str, Any]]:
        """依据已生成的笔记出练习题。

        用笔记而不是原始转写：笔记已经去过噪、纠过错字、分好章节，出题命中率更高，
        也省一半输入 token。题量按笔记字数自动规划，长课多出、短课少出。
        """
        if not summary.strip():
            raise ProviderError("没有可出题的笔记内容。")
        count = count or question_count_for(summary)
        mix = question_mix(count)
        provider = OpenAICompatibleProvider(
            self.settings,
            self.database.add_usage if self.database else None,
            job_id,
        )
        if progress:
            progress(0, "正在生成练习题")
        course = self._course_name(job_id)
        prompt = (
            QUESTION_PROMPT.replace("{{TITLE}}", title or "本节课程")
            .replace("{{COUNT}}", str(count))
            .replace("{{EASY}}", str(mix["easy"]))
            .replace("{{MEDIUM}}", str(mix["medium"]))
            .replace("{{HARD}}", str(mix["hard"]))
            .replace("{{SINGLE}}", str(mix["single"]))
            .replace("{{JUDGE}}", str(mix["judge"]))
            .replace("{{SHORT}}", str(mix["short"]))
            .replace("{{SYLLABUS}}", syllabus_block(course, title or "", summary))
            .replace("{{CONTENT}}", summary)
        )
        raw = provider.complete(prompt, cancelled, purpose="questions")
        questions = filter_questions(parse_questions(raw))

        gaps = question_gaps(questions[:count], count)
        if gaps:
            # 配额不合格就补一次：把差距原样告诉模型，只重试一次，避免无限烧 token
            retry_prompt = (
                f"{prompt}\n\n【上一次输出不合格】{'；'.join(gaps)}。"
                "请重写整套题：hard 不够就补对比、边界条件、给条件计算的题"
                "（例如两种结构/两种算法的取舍、某种做法在什么情况下失效）；"
                "easy 太多就合并或改写为因果题；题型也按配额补齐。"
                "只输出完整的 JSON 数组。"
            )
            try:
                raw = provider.complete(retry_prompt, cancelled, purpose="questions")
                retried = filter_questions(parse_questions(raw))
            except ProviderError:
                retried = []
            if retried:
                retry_gaps = question_gaps(retried[:count], count)
                # 谁更接近配额就用谁：第一次重试仍不合格时，只要差距变小也算改进
                if len(retry_gaps) < len(gaps) or (
                    len(retry_gaps) == len(gaps) and len(retried) > len(questions)
                ):
                    questions = retried

        if not questions:
            raise ProviderError("练习题生成失败：模型没有返回可解析的题目。")
        return questions[:count]

    def _course_name(self, job_id: str) -> str:
        """出题时带上课程名，章节匹配会更准（同一份笔记在不同课程里可能意思不同）。"""
        if not job_id or not self.database:
            return ""
        try:
            job = self.database.get_job(job_id)
            if not job or not job.get("course_id"):
                return ""
            course = self.database.get_course(job["course_id"])
            return (course or {}).get("name", "") or ""
        except Exception:
            return ""

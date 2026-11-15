# 学习视频总结

把一节网课变成一个能直接复习的笔记本：本地转写 → 提取课件画面 → 生成结构化笔记 → 出练习题并记录错题。

单机运行，不需要服务器；视频、转写、笔记全部留在本机。

## 它能做什么

- **输入**：B 站视频链接（支持分 P）或本地视频文件
- **转写**：本地 Whisper 模型，可选 CPU 或 NVIDIA GPU 加速，中文字幕式时间戳
- **画面**：按均匀间隔截取课件/板书，交给视觉模型读出其中文字，补进笔记
- **笔记**：按固定结构输出 Markdown —— 本节要解决什么问题、知识地图、知识点详解（四要素）、对比辨析、易错点、记忆锚点、考点提示、复习自测、时间戳索引
- **题目**：依据笔记生成练习题（单选/判断/简答/填空），默认只显示题干，可展开答案；每题可记录答错次数，按累计次数标出「注意 / 易错 / 重点复习」，并按掌握度排序
- **导出**：笔记 Markdown、转写文本

## 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 / 11（依赖 Edge WebView2，系统自带） |
| Python | 3.10 – 3.13 |
| 网络 | 首次需下载 Whisper 模型权重（约 480 MB – 1.6 GB） |
| 显卡 | 可选。有 NVIDIA 显卡会明显更快；没有也能跑，走 CPU |
| API | 需要自备一个 OpenAI 兼容接口的 Key（如智谱） |

> 转写、截图、抽取音频都在本机完成，不消耗 API 额度；只有「生成笔记」和「出题目」会调用云端模型。

## 安装

```bash
git clone <本仓库地址>
cd 学习视频总结程序

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

启动（推荐，会打开独立应用窗口）：

```bash
.venv\Scripts\pythonw.exe launcher.pyw
```

也可以直接跑服务，然后用浏览器打开 <http://127.0.0.1:8765>：

```bash
.venv\Scripts\python.exe app.py
```

## 配置

首次启动后点右上角「设置」：

1. **API**：填 Base URL、文本模型、视觉模型、API Key
   智谱示例：`https://open.bigmodel.cn/api/paas/v4`，模型 `glm-5.3-flash`（这个模型同时支持读图和写笔记）
   Key 存进系统凭据管理器（Windows Credential Manager），不写进配置文件
2. **转写**：模型建议 `turbo`（速度与 large 级质量兼顾）；设备选「自动」——有可用 GPU 就走 GPU，否则自动退回 CPU
3. **画面**：帧间隔与最大帧数。间隔会按视频实际时长自动放宽，保证整节课都有画面

## 数据在哪

所有产物都在 `data/`，删掉即等于清空：

```
data/
├─ history.db          # 历史任务、笔记、题目、错题统计
├─ config.json         # 界面配置（不含 Key）
└─ cache/<任务ID>/     # 音频、截图
```

`data/` 已在 `.gitignore` 中排除，不会被提交。

## 已知限制

- **只支持 B 站和本地文件**。YouTube、抖音、快手没有适配（抖音/快手需要逆向签名，维护成本高，没做）
- **没有多用户**。这是单机单用户工具，同时跑两个任务会互相抢 CPU/GPU，转写在代码里做了串行化
- **长视频会慢**。转写是本地算力，40 分钟课程在 RTX 4060 上约 1.7 分钟，纯 CPU 约 10 分钟
- **笔记质量依赖模型**。转写里的同音错字靠提示词纠正，模型不够强时仍会残留
- **不做字幕直拉**。B 站有些视频自带 CC 字幕，本项目一律走本地转写（这是可以改进的点）

## 目录结构

```
app.py                  FastAPI 本地服务与静态页
launcher.pyw            桌面启动器（起服务 + pywebview 窗口）
study_buddy/
├─ config.py            配置与密钥（keyring）
├─ db.py                SQLite：任务、用量账本、错题统计
├─ jobs.py              任务流水线：下载 → 转写 → 截图 → 笔记 → 出题
├─ sources.py           B 站解析、音频提取、截帧
├─ transcription.py     faster-whisper 封装（CUDA 探测与回退）
└─ summarizer.py        LLM 调用、提示词、Markdown 解析、题目生成
static/                 前端（原生 HTML/CSS/JS，无框架）
tests/                  pytest
```

## 开发

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

> 改完 Python 代码必须**完全退出应用**再重新打开，只在窗口里刷新不会重新加载后端。
> 只改 `static/` 下的文件时，刷新页面即可。

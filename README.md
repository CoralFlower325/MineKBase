# 考研知识库 × 错题本

> 目标架构已重构为“图片优先错题本 × 共享 RAG”，以 [design.md](design.md) 为唯一目标基线。本 README 保留兼容接口和过渡命令说明；图片收录、分析草稿、候选/确认、错题筛选、资料解析和图片回测主链已实现，语义/图 RAG 与复杂版面解析后置。

一个单用户、本地优先的学习系统：资料和题目先保存，解析、检索和模型增强随后进行；错误变成下一次回测任务，作答和评价形成个人学习轨迹。

## 当前可以做什么

- 用 SQLite 保存课程、学习目标、题目版本、回测任务、会话、Attempt、Assessment 和 EvidenceEvent。
- 通过本地 Web API 或首页完成：建立/查看任务、开始回测、保存/继续编辑草稿、查看提示、提交作答、补充评价。
- 保存每次作答和帮助事件；评价不完整时记录为 `unassessed`，后续仍可继续补充。
- 用 SQL 投影显示今日任务、学习目标状态和北极星事件。
- 无需模型、OCR、RAG 或额外服务即可运行 P0 演示链。
- 可把搜索到的资料 passage 手动关联到题目，并在题目详情中查看出处定位。
- 可通过 `enrich` 按页提取文字 PDF、DOCX（段落、表格）和网页；扫描 PDF 的空文字页、直接上传的资料图片再按需使用可选 OCR，并保留页码或 DOCX 定位。
- 可用 `/api/answer` 基于题面、已关联出处和 FTS 命中生成普通文本回答；回答会保存到本地 SQLite，并由服务端返回真实 passage 出处。
- 首页“模型设置”可以保存 primary/fallback 的协议、地址、模型和 API key；没有保存配置时继续兼容现有 `LLM_*` / `LLM_FALLBACK_*` 环境变量。
- 资料上传后会按文件顺序自动调用现有 `/api/enrich`，图片题保存后会自动顺序执行分析和轻量资料匹配；失败只保留可重试状态，不阻断后续编辑。
- 资料 `retrieve()` 对数学和专业课提供轻量跨科补召回（主科优先、未分类其次、另一科最后）；英语和政治不自动跨科，当前仍是 FTS/LIKE，不是语义向量检索。已确认题目候选另走 SQLite 轻量 `LIKE`，当前不按科目再过滤。
- 图片 intake 已支持多图视觉分析草稿、三种模型协议、回退、字段编辑和失败重试；分析失败时原图仍保留，不会自动生成正式错题。
- 图片 intake 已支持基于现有 FTS/已确认题目的轻量候选召回；确认会保留题面、本人过程、参考答案原图并创建正式 Question/Attempt/ReviewTask；确认后仍可在错题详情补选题面、过程、参考答案角色并调整顺序，只同步当前 Question 展示引用和 grading 资产引用，不改历史 Attempt。历史 Attempt 按其快照中的 asset_id 展示，不受之后角色或顺序调整影响。没有 question/mixed 角色时也可确认，但正式题面明确显示“待补题面”；补选后可刷新看到更新。普通 intake 上传不会创建 `redo_process`，回测过程只能由 redo 上传路径创建；旧数据中的回测角色仍可在错题详情改回普通角色。专用 redo 上传会将新图保存为 `redo_process`，写入回测草稿并在提交后保留其 asset_id。到期回测只显示题面，可上传一张或多张 `redo_process` 图片，提交后显示比较/诊断，比较失败不阻塞保存。
- 确认不要求答案、错误原因或解题断点完整；缺少内容以“待补充”显示，确认后仍可在正式错题详情继续编辑和补充，不设置技术门禁。
- 正式错题本支持按科目、章节、知识点和题型做轻量精确筛选；不带筛选条件时仍显示分类为空的题目。
- 回测队列会显示全部 open 任务并按到期优先排序，标出科目、轮次、原因和状态；点击队列行可直接打开对应错题并开始闭卷重做。
- `notify_due.py` 和配套 LaunchAgent 脚本已提供每日一次、隐私友好的到期任务合并提醒；通知进程只读 SQLite，不写任务或调用模型。
- 资料与回答接入保持四件薄对象：`SourceArtifact`、`SourcePassage`、`QuestionSourceLink`、`Answer`；回答状态 `grounded/unlocated/unavailable` 是结果状态，不是流程门禁。
- 资料收录支持浏览器 multipart PDF/PNG/JPG/DOCX 和普通静态网页 URL；上传文件原件立即保存到 `objects/sources/`，网页 URL 先写入 `SourceArtifact`，增强抓取成功后再保存 HTML 原件；增强复用 `SourcePassage`/FTS。文字 PDF 按页解析，扫描 PDF 的空文字页和直接上传的资料图片在增强时懒加载 PaddleOCR；OCR 依赖不可用时保留原文件并标记 unavailable。DOCX 使用懒加载的 `python-docx` 提取段落和表格单元格并保留 locator；网页抓取优先使用可选 trafilatura，缺失时回退标准库 HTMLParser，HTML 原文件保留且 passage locator 含 URL。OCR 只服务资料检索，不处理手写解题事实。图片错题分析会先读原图；有资料召回时再把带页码/locator 的候选交给二次分析，无召回或模型不可用时仍保留原文件和可编辑草稿。

## 启动

双击 `start.command`（或在终端执行 `./start.command`）会启动/复用本地服务，并自动打开默认浏览器。

开发分支约定：当前唯一现役开发分支 `mzy` 是你的个人分支，默认分支是 `main`；`mzy` 变更先核查，再经 Pull Request 合并 `main`，不直接推送 `main`。当前没有协作者分支，也不把 `mzy` 当共享分支；以后若明确启用协作者分支，再为协作者使用独立工作目录并分别提交 PR。

```bash
python3 app.py seed --as-of 2026-09-01T00:00:00Z
python3 app.py serve
```

首次启动会使用 `fixtures/p0_fixture_manifest.json` 建立一条带“演示数据”标识的演示题目；再次运行 `seed` 会复用已有事实，不覆盖数据。旧文字链在回测投影中显示“演示/兼容”，不会与真实图片错题混淆。需要重置演示库时，显式指定临时数据库：

```bash
python3 app.py --db /tmp/knowledge-demo.sqlite seed --force --as-of 2026-09-01T00:00:00Z
```

浏览器打开 <http://127.0.0.1:8765/>。首页不要求复制 `artifact_id` 或 `question_id`；“高级操作”折叠区仅保留兼容的手动 ID 入口。

模型配置在首页“模型设置”区域完成，保存后立即生效：协议可选 `openai_chat`、`openai_responses`、`anthropic_messages`，主模型和回退模型均可留空。未配置或调用失败时保留 intake 草稿并可稍后重试，不阻断原图保存。API key 只在保存时写入本地 SQLite，读取接口只返回是否已配置；留空 key 表示保留原值。也可以直接使用 `GET/PATCH /api/settings/model`。

### macOS 到期提醒（E1）

安装脚本会为当前用户生成并加载每日 09:00 执行的 LaunchAgent，读取本项目的 `library.sqlite`，将到期回测合并为一条不含题面隐私的通知：

首次使用请先启动一次 `start.command` 让项目创建 `library.sqlite`，再安装提醒；也可先执行一次 `python3 app.py seed`。

```bash
./install_notifications.command
```

卸载时运行：

```bash
./uninstall_notifications.command
```

脚本只操作 `~/Library/LaunchAgents/com.kaoyan.wrongbook.due.plist` 和 launchd，不修改学习数据。需要临时检查通知查询时，可直接指定数据库和当前时间（不会写库）：

```bash
python3 notify_due.py --db /path/to/library.sqlite --now 2026-09-03T00:00:00Z
```

## 真实收录

首页“收录题目”和“收录资料”表单会立即把输入写入当前 `library.sqlite`，并提示“已保存，等待增强”。也可以直接调用：

```bash
curl -X POST http://127.0.0.1:8765/api/capture/question \
  -H 'Content-Type: application/json' \
  -d '{"question_text":"题面原文","response_text":"我的作答"}'
curl -X POST http://127.0.0.1:8765/api/capture/source \
  -H 'Content-Type: application/json' \
  -d '{"source_name":"笔记","raw_text":"第一段\n\n第二段"}'
```

资料收录后浏览器会为每个文件或网页 URL 顺序执行一次本地文本/PDF/DOCX/图片/静态网页增强；网页正文优先使用可选 trafilatura，缺失时回退标准库 HTMLParser。失败的资料可以在最近资料列表中单独重试。兼容脚本仍可手工指定 artifact：

```bash
python3 app.py enrich --artifact-id <id>
```

增强会把文本、PDF 文本层或 OCR 结果切成 `SourcePassage` 并同步到 SQLite FTS5 trigram 索引；PDF passage 带有 1-based `page_no` 和 `locator_json`，重复执行会复用原有 passage ID。也可以通过 `POST /api/enrich` 传入 `source_artifact_id` 手动增强，失败后保留资料并允许再次重试。搜索接口为 `GET /api/search?q=傅里叶`，优先尝试 trigram FTS，并合并 LIKE 兜底，结果包含 `source_artifact_id` 和 `locator_json`。旧库首次启动会补建并提交索引。搜索命中后可通过 `POST /api/question-source-link` 把已有 passage 关联到题目，再用 `GET /api/question/<id>/sources` 读取出处。`POST /api/answer` 会优先使用题目已关联 passage，再合并 FTS 命中，调用已保存的三协议模型配置或环境变量；没有 LLM 配置或调用失败时仍保存并返回 `unavailable`，没有来源但调用成功时返回 `unlocated`。服务重启后仍可通过 `/api/source/<id>` 查看资料、搜索和回链，通过 `GET /api/sources?limit=30` 查看最近资料。

资料详情使用 `GET /api/source/:id` 查看资料、解析状态和 passages。

回答详情使用 `GET /api/answer/:id` 读取已保存回答。

兼容的回答入口可读取 `LLM_PROTOCOL`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`，回退槽位对应 `LLM_FALLBACK_PROTOCOL`、`LLM_FALLBACK_BASE_URL`、`LLM_FALLBACK_API_KEY`、`LLM_FALLBACK_MODEL`；不要求模型返回 JSON 或引用字段，首页模型设置优先于这些环境变量：

```bash
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export LLM_API_KEY=local
export LLM_MODEL=your-model
curl -X POST http://127.0.0.1:8765/api/answer \
  -H 'Content-Type: application/json' \
  -d '{"question_id":"q-...","query":"如何使用傅里叶变换？"}'
```

## 过渡底座的可选检查

```bash
python3 -m py_compile app.py ocr_adapter.py notify_due.py run_p0_scenarios.py
python3 run_p0_scenarios.py
```

这组命令只用于旧文字底座需要时的定向检查，不是新图片主线的开发前置，也不是发布门禁。开发阶段按 design.md 只验证当前切片的真实路径，不要求全量回归或全量冒烟。

## 直接可落地的最终路线

1. 已完成真实收录：题目文字、本人作答、资料文本或路径会写入 SQLite，缺字段也保存。
2. FTS5 已完成：优先尝试 `trigram`，并合并 `LIKE` 兜底；`/api/search?q=...` 返回 passage 和出处定位，零命中仍返回空结果，重复 `enrich` 保留 passage_id。
3. 出处回链已完成：搜索命中可关联到题目，题目详情返回 passage 和 locator，不做自动对齐或评分。
4. 资料解析已完成最小闭环：文字 PDF 使用现有 `pypdf` 按页提取；扫描 PDF 的空文字页和直接上传的资料图片使用可选、懒加载的 PaddleOCR（PDF 光栅化需要 `pypdfium2` 或 `fitz`；依赖缺失时保留原文件并返回 `unavailable`）；DOCX 使用懒加载 `python-docx` 提取段落和表格单元格。所有结果写入同一套 `SourcePassage`、FTS 和 page/locator；OCR 只用于资料检索，复杂版面仍不宣称完全覆盖。浏览器会在保存后逐份自动增强，并提供最近资料列表。
5. Context Composer + LLM 薄切片已完成：先使用已关联 passage，再合并现有 FTS 命中，返回服务端实际出处；未定位或 LLM 不可用都不阻断保存。
6. 只有真实需要跨章节、多跳关系时才接入一个 [LightRAG](https://github.com/HKUDS/LightRAG) REST sidecar；不同时运行两套图/向量索引。若需要更强布局解析，再单独评估 [Docling](https://github.com/docling-project/docling)。
7. 图片错题按确认时间依次安排 +3/+7/+10/+14 天，第 14 天后停止自动排程；旧文字演示链仅用于迁移，不作为新的间隔规则基线。macOS 通知已实现，FSRS 后置。

原则只有一句：用户输入先落库，增强过程后补；状态和提示帮助用户判断，不把不完整变成阻碍。

“独立 Evidence”只是学习状态的结果分类；即使看过提示、资料未命中或评价不完整，Attempt/Assessment 仍会保存，用户仍可继续回测。

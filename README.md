# 考研知识库 × 错题本

> 当前基线是“数学一 × 可配置专业课图片错题本 + 政治章节选择题库”。政治只走客观题库和独立错答记录，英语暂不开发。以 [design.md](design.md) 为唯一产品与开发基线；图片收录、分析草稿、候选/确认、资料解析、课程选择、知识节点绑定、会/不会/不确定回测、真实题库筛选和政治刷题第一版已经落地。

一个单用户、本地优先的数学一与专业课错题系统：资料和题目先保存，解析、检索和模型增强随后进行；用户确认后的错题事实进入个人知识树，错误变成下一次回测任务。

## 当前可以做什么

当前产品主线是数学一、408、可自定义的电子类专业课，以及独立的政治选择题库。首次启动会提供数学一、408、信号与系统、政治四个默认课程，首页“新建专业课”或 `GET/POST /api/courses` 都可以创建自定义课程。课程选择会随题目和资料保存，旧 `subject_key` 仍作为兼容字段保留。

个人知识节点可以通过 `GET/POST /api/knowledge-nodes` 按课程创建和查看；已确认题目可绑定同课程节点。题库第一版支持 JSON API 或 multipart CSV/JSON 导入：先调用 `POST /api/question-bank/preview` 逐行校验，再调用 `POST /api/question-bank/import` 写入；行中提供 `chapter + knowledge_point` 且未指定节点时，导入会在对应课程下生成候选节点并绑定题目，仍需用户确认节点。首页维护列表和 `PATCH /api/question-bank/:id` 支持按课程逐题修正，`PATCH /api/question-bank` 支持批量更新，维护时不能更换课程。相似题查询为 `GET /api/wrong-questions/:id/similar`，按课程、知识节点、章节、题型和难度做轻量筛选。相似题可以通过 `POST /api/question-bank/:id/start` 转成待确认练习草稿，只有确认后才进入正式错题本。政治题库行可携带 `options`、`reference_answer`、`explanation`，首页支持按章节加载政治选择题并通过 `POST /api/question-bank/:id/answer` 判断对错，错答记录可从 `GET /api/question-bank/attempts` 查询。错题诊断中的错误类型固定为“知识点不会 / 方法选择错误 / 推导或计算出错”，同时保留原因和首次错误步骤文本。当前仍缺独立练习结果统计。

政治题库的 JSON 行可以写成 `{"course_id":"course-politics","chapter":"马克思主义基本原理","question_text":"题面","options":{"A":"选项一","B":"选项二"},"reference_answer":"B","explanation":"解析"}`；CSV 的 `options` 列也支持 `A. 选项一|B. 选项二` 这种简写。

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
- 资料 `retrieve()` 对数学和专业课提供轻量跨课程补召回（主课程优先、未分类其次、另一专业课程最后）；当前仍是 FTS/LIKE，不是语义向量检索。已确认题目候选另走 SQLite 轻量 `LIKE`，当前不按课程再过滤。
- 图片 intake 已支持多图视觉分析草稿、三种模型协议、回退、字段编辑和失败重试；分析失败时原图仍保留，不会自动生成正式错题。
- 图片 intake 已支持基于现有 FTS/已确认题目的轻量候选召回；确认会保留题面、本人过程、参考答案原图并创建正式 Question/Attempt/ReviewTask；确认后仍可在错题详情补选题面、过程、参考答案角色并调整顺序，只同步当前 Question 展示引用和 grading 资产引用，不改历史 Attempt。历史 Attempt 按其快照中的 asset_id 展示，不受之后角色或顺序调整影响。没有 question/mixed 角色时也可确认，但正式题面明确显示“待补题面”；补选后可刷新看到更新。普通 intake 上传不会创建 `redo_process`，回测过程只能由 redo 上传路径创建；旧数据中的回测角色仍可在错题详情改回普通角色。专用 redo 上传会将新图保存为 `redo_process`，写入回测草稿并在提交后保留其 asset_id。到期回测只显示题面，可上传一张或多张 `redo_process` 图片，提交后显示比较/诊断，比较失败不阻塞保存。
- 确认不要求答案、错误原因或解题断点完整；缺少内容以“待补充”显示，确认后仍可在正式错题详情继续编辑和补充，不设置技术门禁。
- 正式错题本支持按具体课程、科目、章节、知识点和题型做轻量精确筛选；不带筛选条件时仍显示分类为空的题目，408 与信号与系统等同属专业课的课程不会混在一起。
- 政治错答回顾会保留原题选项，支持从错答记录中直接再做一次；重做仍写入同一套客观判题记录并即时显示解析。
- 回测队列会显示全部 open 任务并按到期优先排序，标出科目、轮次、原因和状态；点击队列行可直接打开对应错题并开始闭卷重做。
- `notify_due.py` 和配套 LaunchAgent 脚本已提供每日一次、隐私友好的到期任务合并提醒；通知进程只读 SQLite，不写任务或调用模型。
- 资料与回答接入保持四件薄对象：`SourceArtifact`、`SourcePassage`、`QuestionSourceLink`、`Answer`；回答状态 `grounded/unlocated/unavailable` 是结果状态，不是流程门禁。
- 资料收录支持浏览器 multipart PDF/PNG/JPG/DOCX 和普通静态网页 URL；上传文件原件立即保存到 `objects/sources/`，网页 URL 先写入 `SourceArtifact`，增强抓取成功后再保存 HTML 原件；增强复用 `SourcePassage`/FTS。文字 PDF 按页解析，扫描 PDF 的空文字页和直接上传的资料图片在增强时懒加载 PaddleOCR；OCR 依赖不可用时保留原文件并标记 unavailable。DOCX 使用懒加载的 `python-docx` 提取段落和表格单元格并保留 locator；网页抓取优先使用可选 trafilatura，缺失时回退标准库 HTMLParser，HTML 原文件保留且 passage locator 含 URL。OCR 只服务资料检索，不处理手写解题事实。图片错题分析会先读原图；有资料召回时再把带页码/locator 的候选交给二次分析，无召回或模型不可用时仍保留原文件和可编辑草稿。
- `GET/POST /api/knowledge-nodes` 提供课程独立的个人知识树；错题分析中的章节/知识点会先自动写成候选节点，用户在分析编辑器中选择后再确认成正式节点，题目绑定也会随确认写入。更复杂的跨资料聚类仍以后续迭代为限。

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

`library.sqlite` 的 `ModelEndpoint` 表包含本地模型配置和 API key。它只适用于单用户、本地优先的运行方式，不应提交到 Git；备份或复制数据库时也会一并复制 API key。若未来支持多用户或网络暴露，再评估迁移到系统钥匙串或独立凭据存储。

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

增强会把文本、PDF 文本层或 OCR 结果切成 `SourcePassage` 并同步到 SQLite FTS5 trigram 索引；PDF passage 带有 1-based `page_no` 和 `locator_json`，重复执行会复用原有 passage ID。也可以通过 `POST /api/enrich` 传入 `source_artifact_id` 手动增强，失败后保留资料并允许再次重试。搜索接口为 `GET /api/search?q=傅里叶`，优先尝试 trigram FTS，并合并 LIKE 兜底，结果包含 `source_artifact_id` 和 `locator_json`。启动时如果 SQLite 不支持 FTS5/trigram，应用仍会启动并只使用 LIKE 检索；可用时会校验 passage 内容差异并修复索引，不只比较行数。旧库首次启动会按 `PRAGMA user_version` 执行当前的最小结构兼容步骤，再补建索引。搜索命中后可通过 `POST /api/question-source-link` 把已有 passage 关联到题目，再用 `GET /api/question/<id>/sources` 读取出处。`POST /api/answer` 会优先使用题目已关联 passage，再合并 FTS 命中，调用已保存的三协议模型配置或环境变量；没有 LLM 配置或调用失败时仍保存并返回 `unavailable`，没有来源但调用成功时返回 `unlocated`。服务重启后仍可通过 `/api/source/<id>` 查看资料、搜索和回链，通过 `GET /api/sources?limit=30` 查看最近资料。

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
7. 数学一与可配置专业课的图片错题按确认时间依次安排 +3/+7/+10/+14 天，第 14 天后停止自动排程；回测分流和真实题库相似题筛选、转待确认练习草稿的第一版已按 `design.md` 的 P3/P4 落地，后续补导入质量反馈和独立练习结果统计。政治选择题库后置，英语暂不开发；旧文字演示链仅用于迁移，FSRS 后置。

原则只有一句：用户输入先落库，增强过程后补；状态和提示帮助用户判断，不把不完整变成阻碍。

“独立 Evidence”只是学习状态的结果分类；即使看过提示、资料未命中或评价不完整，Attempt/Assessment 仍会保存，用户仍可继续回测。

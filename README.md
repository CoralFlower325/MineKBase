# 考研知识库 × 错题本

> 当前基线是“政治 / 英语 / 数学 / 专业课四个通用科目槽位 + 图片优先错题本”。具体考试方向、院校和专业课组合都由用户创建或导入，不能写入产品页面和核心 API。以 [design.md](design.md) 为唯一产品与开发基线；图片收录、分析草稿、候选/确认、资料解析、课程选择、知识节点绑定、会/不会/不确定回测、真实题库筛选和政治刷题第一版已经落地。

一个单用户、本地优先的考研错题系统：资料和题目先保存，解析、检索和模型增强随后进行；用户确认后的错题事实进入对应课程的个人知识树，错误变成下一次回测任务。

## 当前可以做什么

当前产品模型固定政治、英语、数学、专业课四个科目类别。首次启动提供四个通用课程槽位；首页“新建课程”或 `GET/POST /api/courses` 都可以创建任意名称、任意课程组的课程。具体考试方向只作为用户数据保存，旧 `subject_key` 仍作为兼容字段保留。

个人知识节点可以通过 `GET/POST /api/knowledge-nodes` 按课程创建和查看，候选节点可用 `PATCH /api/knowledge-nodes/:id` 确认或归档；首页知识导航会单独显示待确认候选。已确认题目可绑定同课程节点。题库第一版支持 JSON API 或 multipart CSV/JSON 导入：先调用 `POST /api/question-bank/preview` 逐行校验，再调用 `POST /api/question-bank/import` 写入；行中提供 `chapter + knowledge_point` 且未指定节点时，导入会在对应课程下生成候选节点并绑定题目，仍需用户确认节点；当前文件内重复题目 ID 会在预览阶段报错。首页维护列表和 `PATCH /api/question-bank/:id` 支持按课程逐题修正；勾选多题后可批量改章节、题型、难度和解析，底层 `PATCH /api/question-bank` 同样支持批量更新，维护时不能更换课程。相似题详情现在可以直接按知识点、章节、题型和难度调整筛选；`GET /api/wrong-questions/:id/similar` 接受这些筛选参数并始终限制在当前课程内。相似题可以通过 `POST /api/question-bank/:id/start` 转成待确认练习草稿，只有确认后才进入正式错题本。政治题库行可携带 `options`、`reference_answer`、`explanation`，首页支持按章节加载政治选择题并通过 `POST /api/question-bank/:id/answer` 判断对错，错答记录可从 `GET /api/question-bank/attempts` 查询。错题诊断中的错误类型固定为“知识点不会 / 方法选择错误 / 推导或计算出错”，同时保留原因和首次错误步骤文本。独立练习结果统计按当前用户边界后置，不记录大规模学习数据。

政治题库的 JSON 行可以写成 `{"course_id":"course-politics","chapter":"马克思主义基本原理","question_text":"题面","options":{"A":"选项一","B":"选项二"},"reference_answer":"B","explanation":"解析"}`；CSV 的 `options` 列也支持 `A. 选项一|B. 选项二` 这种简写。

- 用 SQLite 保存课程、学习目标、题目版本、回测任务、会话、Attempt、Assessment 和 EvidenceEvent。
- 通过本地 Web API 或首页完成：建立/查看任务、开始回测、保存/继续编辑草稿、查看提示、提交作答、补充评价。
- 保存每次作答和帮助事件；评价不完整时记录为 `unassessed`，后续仍可继续补充。
- 首页只显示回测队列、错题诊断、知识与题库入口；旧学习目标/证据投影仅保留兼容接口，不作为当前产品界面。
- 首页按“今日 / 复习 / 知识 / 收录”切成四个工作区，并提供独立“设置”页面，不再把所有功能连续堆叠在一条长页面；知识工作区展示四个通用科目槽位及用户创建的课程，侧栏课程选择会同步筛选入口。
- 无需模型、OCR、RAG 或额外服务即可运行 P0 演示链。
- 可把搜索到的资料 passage 手动关联到题目，并在题目详情中查看出处定位。
- 可通过 `enrich` 按页提取文字 PDF、DOCX（段落、表格）和网页；扫描 PDF 的空文字页、直接上传的资料图片再按需使用可选 OCR，并保留页码或 DOCX 定位。
- 可用 `/api/answer` 基于题面、已关联出处和 FTS 命中生成普通文本回答；回答会保存到本地 SQLite，并由服务端返回真实 passage 出处。
- 首页“模型设置”可以保存 primary/fallback 的协议、地址、模型和 API key；没有保存配置时继续兼容现有 `LLM_*` / `LLM_FALLBACK_*` 环境变量。
- 资料上传后会按文件顺序自动调用现有 `/api/enrich`；带课程的资料在增强时会从明确的章节标题生成课程内知识候选，候选仍需在知识导航中确认。图片题保存后会自动顺序执行分析和轻量资料匹配；失败只保留可重试状态，不阻断后续编辑。
- 资料 `retrieve()` 当前仍是 FTS/LIKE，不是语义向量检索；题目分析、匹配和同题追问会按 `course_id` 限制在当前课程和未分类旧资料，避免不同专业课课程互相串资料。已确认题目候选另走 SQLite 轻量 `LIKE`，并在查询层限制为当前课程。
- 图片 intake 已支持多图视觉分析草稿、三种模型协议、回退、字段编辑和失败重试；分析失败时原图仍保留，不会自动生成正式错题。
- 图片分析解析兼容模型常见的编号/Markdown 标题、结构化 JSON 和“原题、参考解、方法、错误分析”等标签变体；解析出的仍是可编辑候选，不能绕过用户确认。
- 图片 intake 已支持基于现有 FTS/已确认题目的轻量候选召回；确认会保留题面、本人过程、参考答案原图并创建正式 Question/Attempt/ReviewTask；确认后仍可在错题详情补选题面、过程、参考答案角色并调整顺序，只同步当前 Question 展示引用和 grading 资产引用，不改历史 Attempt。历史 Attempt 按其快照中的 asset_id 展示，不受之后角色或顺序调整影响。没有 question/mixed 角色时也可确认，但正式题面明确显示“待补题面”；补选后可刷新看到更新。普通 intake 上传不会创建 `redo_process`，回测过程只能由 redo 上传路径创建；旧数据中的回测角色仍可在错题详情改回普通角色。专用 redo 上传会将新图保存为 `redo_process`，写入回测草稿并在提交后保留其 asset_id。到期回测只显示题面，可上传一张或多张 `redo_process` 图片，提交后显示比较/诊断，比较失败不阻塞保存。
- 确认不要求答案、错误原因或首次出错步骤完整；缺少内容以“待补充”显示，确认后仍可在正式错题详情继续编辑和补充，不设置技术门禁。
- 答案候选按来源优先级并列展示；资料候选会优先提取明确的“参考答案/解析/解答”段落，保留完整资料原文供展开确认，未命中标记时仍显示需确认。
- 确认入档时会保存各字段的来源标记，正式错题详情和 Markdown 导出仍可看到模型、资料、题库或用户修改来源，便于复核确认依据。
- 正式错题本支持按具体课程、科目、章节、知识点、题型和错误类型做轻量精确筛选；列表与知识导航直接显示章节、知识点、三类错误、错误原因和首次出错步骤；两个 Markdown 导出入口沿用当前筛选条件；错题详情可以重新绑定或解除同课程个人知识节点，历史 Attempt 快照不变；不同用户课程不会混在一起。
- 知识导航可按课程切换，候选节点和已确认题目始终沿用同一课程过滤，不把不同专业课课程混在一棵可视树里。
- 错题列表还会显示答案来源和字段来源摘要，便于先扫描模型候选或待复核字段，再打开详情处理。
- 错题详情会展开初次作答和已提交回测历史，保留每轮文字过程与过程图片；进入闭卷回测后不展示这些历史记录、参考答案或比较诊断，提交后再恢复查看。
- 政治错答回顾会保留原题选项，支持从错答记录中直接再做一次；重做仍写入同一套客观判题记录并即时显示解析。
- 回测队列会显示全部 open 任务并按到期优先排序，标出科目、轮次、原因和状态；点击队列行可直接打开对应错题并开始闭卷重做。
- 闭卷重做使用专用的 `/api/wrong-questions/:id/review` 脱敏返回，只携带题面和当前草稿，不在网络响应中携带参考答案、历史 Attempt、历史过程图或比较诊断；提交后再读取完整错题详情。
- `notify_due.py` 和配套 LaunchAgent 脚本已提供每日一次、隐私友好的到期任务合并提醒；通知进程只读 SQLite，不写任务或调用模型。
- 资料与回答接入保持四件薄对象：`SourceArtifact`、`SourcePassage`、`QuestionSourceLink`、`Answer`；回答状态 `grounded/unlocated/unavailable` 是结果状态，不是流程门禁。
- 资料收录支持浏览器 multipart PDF/PNG/JPG/DOCX 和普通静态网页 URL；上传文件原件立即保存到 `objects/sources/`，网页 URL 先写入 `SourceArtifact`，增强抓取成功后再保存 HTML 原件；增强复用 `SourcePassage`/FTS。文字 PDF 按页解析，扫描 PDF 的空文字页和直接上传的资料图片在增强时懒加载 PaddleOCR；OCR 依赖不可用时保留原文件并标记 unavailable。DOCX 使用懒加载的 `python-docx` 提取段落和表格单元格并保留 locator；网页抓取优先使用可选 trafilatura，缺失时回退标准库 HTMLParser，HTML 原文件保留且 passage locator 含 URL。图片错题分析仍以原图为事实来源，同时在 PaddleOCR 可用时保存按图片序号回链的 OCR 辅助文字/框选，并把它作为“需核对原图”的提示交给模型；OCR、资料召回或模型不可用时仍保留原文件和可编辑草稿，不处理失败也不阻断确认。
- `GET/POST /api/knowledge-nodes` 提供课程独立的个人知识树；错题分析中的章节/知识点会先自动写成候选节点，首页知识导航支持在确认前修改候选名称和上级章节，确认后才成为正式节点，题目绑定也会随确认写入。已确认节点改名会同步当前正式错题和 intake 草稿，历史 Attempt 快照不变。资料中的明确章节、编号小节和“知识点/考点”行会先生成最多两级候选；资料和题库反复出现的同课程标签会做保守的空格/标点归并，并保留原标签别名供确认；更复杂的跨资料语义聚类仍以后续迭代为限。

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

## 开发验证

按本轮涉及的真实流程选择检查。同一批相关改动完成后运行一次；通过后仅在新改动、失败或未决问题出现时追加验证。

```bash
# 修改 Python 后确认语法
python3 -m py_compile app.py

# 错题整理：课程隔离、知识导航、筛选和导出
python3 -c "from run_p0_scenarios import run_wrong_course_filter_smoke; print(run_wrong_course_filter_smoke())"
```

`run_p0_scenarios.py` 同时包含旧文字演示链与图片错题、回测、知识候选、题库等定向场景。涉及多条链路或准备集成时，可以运行 `python3 run_p0_scenarios.py`；它不是每次字段或文案修改的前置条件。场景验证使用临时数据库，不覆盖用户数据；模型替身通过不能证明真实手写识别和诊断质量。

## 产品路线

产品范围、当前限制、后续顺序与验收统一见 [design.md 第 7–10 节](design.md#7-当前实现对照)。本文件只维护使用方式和接口，不另设一套开发路线。

旧版 Evidence/Assessment 结构只作为兼容记录；当前产品不据此生成学习计划或统计面板。回测过程即使看过提示、资料未命中或评价不完整，Attempt 仍会保存，用户仍可继续回测。

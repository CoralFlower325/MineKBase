# 图片优先考研错题本 × 共享 RAG

版本：2026-09-02（重构基线）
状态：本文替代旧版 design.md。旧版“单课程、文字优先、2/3/7/21 天、可选 LightRAG 主线”的设计不再是目标架构；现有代码只作为可运行的过渡底座。

## 0. 重构后的结论

本项目不是先建设题库、知识图谱或复杂学习平台，而是先把用户真正会用的闭环做对：

~~~text
多张题目/作答图片
→ 原图立即保存
→ 视觉模型 + 资料检索形成可编辑草稿
→ 用户确认题面、答案、分类和错误诊断
→ 进入错题本
→ 到期只看题面并上传新的解题过程
→ 记录重做结果，继续下一轮
~~~

最终架构收敛为：

~~~text
Mac 浏览器（首版）
        ↓
单体 HTTP 应用
  ├─ SQLite：唯一事实源
  ├─ objects/：原始图片和资料
  ├─ 共享检索器：FTS 优先，向量/图能力可后置
  ├─ 协议适配器：OpenAI Chat / Responses、Anthropic Messages
  └─ macOS launchd：低频到期提醒
~~~

不建设微服务、消息队列、独立图数据库、四套 RAG、强制 schema/gate 或发布审批平台。失败和缺失只改变状态，不阻止原始内容保存和用户继续操作。

“确认”是用户完成错题卡的正常业务动作，不是启动检查、接口门禁或技术阻断：未确认项仍可继续上传和重试，只是不进入正式错题本。

## 1. 产品边界

### 1.1 科目

首版支持四类科目：数学、英语、政治、专业课。专业课不是固定为“信号与系统”；现有演示题只代表旧底座数据，不代表产品范围。

每道题和每份资料都可以有：

~~~text
subject_key       math | english | politics | professional | null
chapter           可空、可编辑
knowledge_point   可空、可编辑
question_type     可空、可编辑
~~~

这些字段是帮助检索和整理的候选，不是入库条件；确认不要求答案或错误诊断字段完整，缺少内容显示“待补充”，确认后仍可继续编辑和补充，分类不完整时题目同样可以进入错题本。

### 1.2 输入

- 一次上传默认代表一张错题卡，可以包含一张或多张图片；保留上传顺序。
- 图片角色可以是 question（题面）、my_process（自己的解题过程）、reference（可选标准答案）、redo_process（回测时的新过程）或 mixed（同一张图同时含题面和过程）。角色可由用户指定或修正；确认时没有 question/mixed 仍允许进入错题本并标记“待补题面”，不设置门禁；确认后可在错题详情补选题面、解题过程、参考答案和顺序，只同步当前 Question 的展示/ grading 资产引用，不改历史 Attempt。历史 Attempt 和已开始 ReviewSession 按各自快照中的 asset_id 保持原图及顺序，不受后续角色编辑影响。普通 intake 上传不会创建 `redo_process`，专用 redo 上传才会将新图写入 `redo_process` 并保存到回测草稿；旧数据中的 `redo_process` 可在错题详情改回普通角色。未指定时模型按整组图片上下文理解，自动角色建议/回写留作后续薄补丁；不要求先裁剪图片。标准答案可以在首次上传时提供，也可以稍后追加到同一 intake。没有标准答案时先保存到 intake，等待模型解答或用户补充。
- 教材、讲义、笔记和网页作为资料输入，格式可以是文字 PDF、扫描 PDF、Word、图片或网页。
- 题库和答案库是可选资料源，不是系统前置条件。

### 1.3 不在当前边界

不做账户、多租户、云端同步、完整知识图谱编辑器、自动生成整套课程、复杂自适应排程和多端客户端。未来多端只要求 API 使用资产 ID 和相对媒体 URL，不在本轮实现网络暴露或同步服务。

## 2. 角色分工

| 部件 | 只负责什么 | 不负责什么 |
| --- | --- | --- |
| 原始资产存储 | 保存每张原图/资料文件、顺序和角色 | 不把识别结果当原图替换 |
| SQLite | 题目、作答、诊断、确认、回测和提醒等学习事实 | 不承担全文/向量索引的全部实现 |
| 共享 RAG | 从教材、讲义、笔记和已确认错题召回证据与标签候选 | 不决定答案是否正式入库 |
| 视觉/语言模型 | 读多图、整理题面、解题、比较过程、指出首次断点 | 不改写历史 Attempt，不伪造出处 |
| 用户 | 修改并确认答案、分类、错误原因和断点 | 不需要先补齐所有字段 |
| WebUI | 上传、预览、编辑、确认、回测和查看来源 | 不要求用户复制路径或手写 ID |
| launchd | 按 3/7/10/14 天检查并发出 macOS 提醒 | 不创建第二套任务队列 |

RAG 是证据和候选层；模型是读图、推理和诊断层；用户确认后的字段才是个人错题事实。

## 3. 目标架构

~~~mermaid
flowchart LR
    U[Mac 浏览器] --> API[单体 HTTP 应用]
    API --> OBJ[objects/ 原始资产]
    API --> DB[(SQLite 事实库)]
    API --> RET[共享 Retriever]
    RET --> FTS[SQLite FTS5]
    RET -. 真实需要时择一 .-> OPT[可选语义/图后端]
    API --> ADP[模型协议适配器]
    ADP --> OAI1[OpenAI Chat]
    ADP --> OAI2[OpenAI Responses]
    ADP --> ANT[Anthropic Messages]
    DB --> NOTI[macOS launchd 日提醒]
~~~

### 3.1 一个 RAG，四个逻辑命名空间

业务层只有一套 Retriever 和索引基础设施。subject_key 是元数据过滤和召回边界，不是四套服务；具体后端若不支持过滤，就由本项目先筛选或分 workspace，再合并结果：

统一的内部调用约定是：

~~~text
retrieve(query, primary_subject, related_subjects?)
analyze(image_assets, retrieved_context)
~~~

科目边界留在本项目适配层，不散落到某个 RAG 或模型供应商实现中。

1. 默认只检索当前题目的科目；未分类资料保留在同科结果之后。
2. 数学与专业课允许轻量跨学科：先完成主科目检索；只有模型识别出数学前置依赖，或用户明确说明相关时，才追加一个小窗口的另一命名空间教材/笔记候选，并在界面标记“跨学科参考”。跨学科资料不能改变题目的主科目。
3. 英语和政治不自动跨科检索。
4. 未指定科目时可以从四个命名空间产生分类候选；用户确认后再归入具体科目。
5. 不建立跨科关系表或图数据库。需要多跳关系时，再把同一批 passage 接给一个 LightRAG sidecar；sidecar 的物理 workspace/集合不改变“一套 Retriever”的业务接口。

### 3.2 资料和个人错题的层次

- 原始图片/文件默认保留，分析失败不会删除；用户可主动删除不需要的图片。
- OCR、视觉转写、切片、向量和图关系都是可重建的派生物。
- 教材/讲义/笔记 passage 提供出处；已确认错题的题面、诊断和重做记录可以作为个人检索材料。
- 未确认的模型答案和诊断只停留在 intake 草稿，不作为正式 RAG 事实。

## 4. 最小领域模型

下面是目标逻辑对象；可以逐步落到现有 SQLite，不要求一次性建立复杂表群。

| 对象 | 关键字段 | 说明 |
| --- | --- | --- |
| CaptureBatch | batch_id、created_at、subject_key? | 一次上传；默认一题，保留图片顺序 |
| ImageAsset | asset_id、batch_id、role、ordinal、path、mime | 原图文件；role 为题面/本人过程/标准答案/mixed/重做过程 |
| IntakeItem | intake_id、batch_id、state、draft_fields、failure_note | 未完整处理数据集中的一题；`state` 表示原图保存完整性，分析状态放在 `draft_fields.analysis_status` |
| AnalysisCandidate | 题面、参考答案、四个分类字段、错误原因、首次断点、正确思路、来源 | 模型/RAG 产生的候选；来源可指向 asset/区域或 passage locator，所有字段可空、可编辑 |
| WrongQuestion | wrong_question_id、已确认字段、原始资产引用 | 用户确认后的错题本卡片；参考答案、错误原因和首次断点按用户当前内容保存，可为空并显示“待补充”，确认后仍可补充，分类字段可以为空 |
| Attempt | attempt_id、wrong_question_id、kind、response_text?、asset_ids、submitted_at | 初次作答或回测重做；永不覆盖历史 |
| MaterialArtifact | artifact_id、kind、subject_key?、path/url、parse_state | 教材、讲义、笔记、网页等原始资料 |
| Passage | passage_id、artifact_id、text、page/locator、subject_key、chapter?、knowledge_point?、question_type? | 资料的可回源派生片段，进入 FTS/向量索引；分类元数据可空 |
| ReviewTask | task_id、wrong_question_id、round、due_at、status | 只表达下一次重做行动 |

实现时可以复用现有的 Question、QuestionRevision、Attempt、ReviewTask 和 SourcePassage，但不要再让 SourceArtifact 的 image_path 字符串冒充图片上传系统。图片必须有真实文件和题目/Attempt 关系；旧 P0 的十张学习账本表不是新的产品边界。

### 4.1 字段和事实规则

- question_text、reference_answer、error_reason、error_breakpoint、correct_approach 都是可编辑文本；没有识别结果就留空。
- 模型输出保存为候选和来源说明；不要求模型返回 JSON，也不因格式不完整而拒绝保存。服务端把能识别的部分填入普通字段，原始回答可作为普通文本留存。
- reference_answer、error_reason 和 error_breakpoint 写入正式错题卡时以用户当前确认的内容为准；字段可以为空，空值显示“待补充”，确认后仍可在错题详情继续编辑和补充。未经用户确认的模型答案或诊断只停留在 intake 草稿，不设置技术门禁，也不阻止用户先确认题目。
- 模型失败、RAG 零命中、识别乱码或网络不可用，都只把 IntakeItem 留在未完整处理数据集并显示原因；已上传的图片不回滚，也不生成未经确认的错题卡。
- 数据库中的快照若用于保存历史事实可以保留；不再维护外部 schema/policy JSON、版本门禁或未知字段拒绝器。

## 5. 图片错题主流程

~~~mermaid
flowchart TD
    A[浏览器选择一张或多张图片] --> B[立即复制到 objects/并写入 CaptureBatch/ImageAsset]
    B --> C[进入 IntakeItem，显示原图缩略图、顺序和可编辑角色]
    C --> D{分析}
    D --> E[视觉模型先读全部图片，得到粗题面并理解图片关系]
    E --> E2[可选标准答案图片/解析图加入上下文]
    E --> F[根据粗题面由共享 RAG 召回主科目资料]
    F -. 数学↔专业课可选 .-> G[追加少量跨学科资料]
    E --> H[题面/答案/分类/错误原因/首次断点/正确思路草稿]
    E2 --> H
    F --> H
    G --> H
    H --> I[WebUI 可编辑预览]
    I -->|确认答案和诊断| J[WrongQuestion 正式错题卡]
    I -->|未确认/稍后| K[Intake 未完整处理集，保留原图，可重试]
    J --> L[按序生成 ReviewTask：+3→+7→+10→+14]
    L --> M[回测只显示题面图片和题干]
    M --> N[上传新的 redo_process 图片并提交 Attempt]
    N --> O[显示新旧过程、答案和诊断，允许修改]
    O --> P[下一轮 ReviewTask 或结束]
~~~

### 5.1 匹配和模型解答

1. 先由视觉模型从图片得到粗题面，再在已确认错题（其题面/答案可作为个人检索片段）、已导入题库/答案库和资料 passage 中检索相似题面。
2. 有可用匹配时，界面同时显示匹配题目、出处和答案；用户仍可改正匹配。
3. 没有匹配时，模型根据原图和 RAG 资料生成解题草稿。没有资料也可以给出模型草稿，但明确标记“无资料依据”。
4. 两条路径都使用同一套候选编辑和确认界面，不能让“数据库命中”绕过用户确认。

### 5.2 错误诊断

诊断不是泛泛的分数，而是可回看的三件事：

- error_reason：例如概念、读题、计算、方法选择或暂不确定；
- error_breakpoint：本人过程第一次与正确路线分叉的步骤/图片区域；
- correct_approach：从断点继续的最短正确思路。

模型只提出初稿，用户可以直接修改。参考答案或诊断未填写时仍可确认进入错题本，正式卡片显示“待补充”，用户之后可以继续补齐；未经用户确认的模型内容不写入正式 grading，分类字段仍不要求完整。

## 6. 共享 RAG 设计

### 6.1 资料进入 RAG

~~~text
原始资料保存
→ 解析为统一文本/区域片段
→ passage 带 subject/chapter/knowledge_point/question_type/source locator
→ 写入 SQLite FTS5
→ 有真实需要再增加向量召回或 LightRAG
~~~

文字 PDF 先用现有 pypdf；浏览器上传的 PDF/PNG/JPG/DOCX 原文件先保存到 `objects/sources/`，文字层 PDF 继续按页解析，扫描/混合 PDF 的空文字页和资料图片在点击增强时懒加载单一 `ocr_adapter.py`（PaddleOCR 3.x），DOCX 懒加载 `python-docx` 提取段落和表格单元格。所有结果仍输出同一种 Passage，尽量保留 page_no、bbox 和 locator_json（DOCX locator 至少含 parser、paragraph 或 table/row/cell）；OCR 不可用只标记 unavailable/error，原文件和资料记录保留可重试。OCR 只生成资料检索派生文本，不替代视觉模型对手写题面和过程的理解。网页和复杂版面仍后置。

### 6.2 召回顺序

~~~text
统一 `Store.retrieve(query, primary_subject, related_subjects?, limit=8)`
→ 中文/英文片段 FTS5 召回（bm25 排序）
→ 短查询 LIKE 兜底
→ 当前科目过滤
→ （可选）向量补召回
→ （可选）数学/专业课轻量跨科补召回
→ 去重后交给模型
~~~

RAG 返回的是证据片段和标签候选，不返回“已确认答案”。所有来源都要能回到资料文件、页码或图片资产；找不到来源时仍可显示模型回答，但标注未定位。

RAG 不直接写入权威标签：它把带有可空 subject/chapter/knowledge_point/question_type 的资料候选交给模型，模型结合题面和过程生成分类草稿，用户确认后才写入错题事实；没有标签的来源也可以召回并标为“未分类来源”。

所谓知识网在首版只表现为 SQLite 元数据链：subject → chapter → knowledge_point（附可编辑别名）。它先服务于过滤、标签和回测统计；只有真实出现多跳需求时才考虑图检索，不提前建设图数据库。

## 7. 模型协议和失败处理

协议和模型 ID 分开配置：

| protocol | 用途 |
| --- | --- |
| openai_chat | OpenAI Chat Completions 形状，支持文本和多图 content parts |
| openai_responses | OpenAI Responses 形状，支持图像输入和响应内容 |
| anthropic_messages | Anthropic Messages 形状，支持多图和文本 |

每个配置另外保存 base_url、api_key、model_id。调用顺序是主配置，再按用户设置的回退配置；回退仍不可用时只留下 IntakeItem 的失败状态和原图，不产生正式答案、诊断或 ReviewTask。不要要求所有供应商返回同一 JSON，也不把供应商错误变成启动失败。

## 8. 回测和提醒

### 8.1 回测行为

- 确认错题后建立第一条 ReviewTask。
- 到期回测只显示题面图片/题干，不显示本人历史过程和参考答案。
- 用户提交一张或多张新的解题过程图片；保存为新的 Attempt，不覆盖初次作答。
- 提交后显示历史过程、参考答案和错误断点；可选调用模型比较新旧过程，调用失败仍保存 Attempt，新的比较/诊断草稿挂在该 Attempt 上待补，不回退或污染原错题卡，也不阻塞提交。
- 上传过程图先创建可继续编辑的 draft Attempt，提交动作再封存它；图片保存和提交不因比较服务不可用而回滚。
- 当前周期存在 open ReviewTask 时可以提前重做；这不删除已有历史。

### 8.2 间隔

以错题确认时间为本轮锚点，依次在 +3、+7、+10、+14 天到期；每次只生成下一条 open ReviewTask，第 14 天完成后停止自动排程。若未来提供手动新周期，再以手动开始时间作为新的 +3 天锚点。首版不接 FSRS，不把提醒算法做成独立系统。

### 8.3 macOS 通知

用一个轻量 launchd 定时检查 SQLite 的 due_at，每天合并成一次 macOS 通知；WebUI 关闭时也能提醒。通知只是把用户带回错题本，不在通知进程中调用模型或建立队列。

## 9. WebUI 目标

首版只需要四个明确页面/区域：

1. **收录**：多图选择、缩略图/原图、顺序和角色、科目候选；上传后原图立即保存，进入详情页即可手动开始分析，按钮也用于重试。
2. **待确认**：题面、模型/匹配答案、出处、科目/章节/知识点/题型、错误原因、首次断点、正确思路；所有字段可编辑；确认后进入错题本。
3. **错题本**：按科目和可选分类筛选；分类为空也能看到卡片；显示原题图、当前诊断和答案状态。
4. **回测**：只看题面，上传新的过程图，提交后再看比较结果和下一次日期。

模型、协议和主/回退配置当前通过 `LLM_*`/`LLM_FALLBACK_*` 环境变量提供，设置区留作后续薄 UX；数学↔专业课关系按上述自动规则处理，首版不增加额外开关。分析完成时优先给用户两个业务结果标签：“匹配题目完成”或“模型解答完成”；内部仍可显示“已保存 / 分析中 / 草稿 / 未完整 / 已确认”。不出现“校验失败所以不能继续”的阻断文案。

## 10. 接口边界

### 10.1 现有过渡接口（仍可运行）

当前代码提供文字/PDF 路径收录、FTS、手动出处关联、普通 /api/answer 和文字回测接口。这些接口保留用于迁移和回归，但它们不是图片主流程：现有 capture/source 不会复制浏览器上传的图片，现有回答也不是错题诊断。

### 10.2 目标接口（按图片主线补齐）

~~~text
POST  /api/intake/batches                 # multipart，多张图片，一批一题
GET   /api/intake                         # 未完整处理列表
GET   /api/intake/:id                     # 原图、草稿、来源、失败原因
POST  /api/intake/:id/analyze             # VLM 分析草稿，允许 retry
POST  /api/intake/:id/resolve             # 轻量 FTS/已确认题目候选召回
POST  /api/intake/:id/assets              # 向已有 intake 追加图片（例如稍后找到标准答案）
PATCH /api/intake/:id                     # 编辑候选字段，不要求完整
POST  /api/intake/:id/confirm             # 用户确认并晋级；分类可空，未完整草稿仍可继续处理
GET   /media/:asset_id                    # 原图/资料预览
GET   /api/wrong-questions                # 分类筛选和详情
GET   /api/wrong-questions/:id            # 错题卡、原图、答案和诊断详情
POST  /api/wrong-questions/:id/redo        # 上传并保存新的过程图片，返回 draft attempt_id
POST  /api/attempts/:id/submit            # 封存重做、可选比较并生成下一任务
GET   /api/reviews/due                   # 到期题面和日期
~~~

上传使用浏览器 multipart 文件，不把图片膨胀成 base64 JSON。capture/intake 缺字段照常保存；confirm 是用户业务动作，不要求答案、错误原因或断点完整，空字段以“待补充”保留，确认后可继续编辑和补充；不设置技术门禁阻断上传、重试或确认。模型协议适配器是服务端内部模块，不另造一套业务 API。

## 11. 直接开发路线

每一步都能运行并留下可见产物；没有阶段门禁，失败项留在数据集中继续处理。开发时以当前切片的真实用户路径为主，不把全量回归或全量冒烟作为常规负担；既有 smoke 仅在相关路径出现回归时按需诊断，任何测试通过都不是继续开发的前置条件。

### A. 图片收录底座（已完成）

已实现多文件上传、原图复制到 objects/、CaptureBatch/ImageAsset/IntakeItem 关系、缩略图和角色/顺序编辑，以及向已有 intake 追加图片。A 阶段不包含 OCR、RAG 或模型；既有默认库首次启动时会按 schema 创建这些新表。

### B. 一题分析草稿（已完成）

已把多图送入统一的分析入口，接入现有云端模型配置、三种协议适配和回退。分析结果写入 `draft_fields`：`question_text`、`reference_answer`、`subject_key`、`chapter`、`knowledge_point`、`question_type`、`error_reason`、`error_breakpoint`、`correct_approach` 和 `raw_analysis`；普通文本/Markdown 也可解析，原始回答始终保留。重试只补充空白候选，不静默覆盖用户已编辑的字段。无可用模型时只保留/查看 intake，模型恢复后可重试。用户上传标准答案或手工补全只是候选来源；答案和诊断的确认属于 C 的用户动作，空字段也可继续确认并以“待补充”留在正式快照，不用技术门禁阻断上传、重试或入库。本阶段不接共享 RAG，C 阶段再加入资料召回。

实现映射：`IntakeItem.state` 只表示原图保存完整性（`raw/saved/incomplete`），分析过程和结果放在 `draft_fields.analysis_status`（`analyzing/draft/failed`），不再增加另一张分析表或状态服务。

### C. 共享 RAG 与确认晋级（主链已完成）

已接通 `resolve_intake` 和 `confirm_intake`：使用现有 SourcePassage FTS/已确认题目做轻量候选召回，候选结果回到 intake 草稿供用户编辑；确认动作会创建 Question、确认版 QuestionRevision、ReviewPromptRevision、初始 Attempt 和首个 +3 天 ReviewTask。确认后的正式错题列表/详情、题面图片过滤和所选 passage 的 QuestionSourceLink 也已接入。没有新增 RAG 服务或业务表。

当前实现保持轻量：未指定角色时不静默采用过程/答案图，错题详情可补选角色和顺序；题面与过程混在同一张图时，回测会明确提示而不伪装成完全隔离。候选选择已统一处理并保存纯答案文本；确认缺少诊断不再阻断。确认后追加并标记为 `reference` 的已保存图片会作为新增 grading 输入同步到正式快照，不改写历史 Attempt。P0 fixture 带有“演示数据”标识，旧文字链在投影中带“演示/兼容”标识。当前 FTS 仍是轻量片段/整段召回，不宣称已经解决相似题语义匹配；科目元数据和数学↔专业课轻跨科先保持可空候选，等真实资料证明需要再补。

### D. 回测图片闭环（已完成）

已接通真实题面/本人过程/参考答案资产快照、到期只显示题面、`redo_process` 多图草稿、独立 Attempt 提交、提交后比较/可编辑诊断和下一日期；模型比较失败只记录失败草稿，不回滚 Attempt。ReviewTask 按确认锚点固定 +3/+7/+10/+14 顺序生成，第 14 天后停止。

### E1. macOS 提醒（已完成）

已加入每日一次的 LaunchAgent 检查：只读 SQLite 中已到期的 open ReviewTask，将数量、最早到期日期和 WebUI 提示合并成一条不含题面隐私的 macOS 通知；无到期任务时静默，不写数据库、不调用模型。

### E2. 资料扩展（已接通最小闭环）

文字 PDF、资料图片和 DOCX 已沿现有 SourceArtifact → SourcePassage → FTS 链路接通：原文件落盘，pypdf 按页解析，扫描/混合 PDF 空页和图片可选使用 PaddleOCR，DOCX 使用懒加载 python-docx 解析段落与表格并保留 locator；解析依赖缺失时保留原文件并标记 unavailable，可重试。图片题分析会复用现有 retrieve()，将真实 passage/page/locator 放入二次模型上下文。当前仍是 FTS/LIKE 轻量召回，不是语义相似题 RAG 或知识图谱；网页、向量库、LightRAG、FSRS 继续后置。

### F. 真实数据后再评估

只有出现跨章节、多跳或本地检索明显不足时，才引入一个 LightRAG sidecar；只有固定间隔无法满足实际复习时，才评估 FSRS。

### 11.1 验证方式

每个切片只做与它直接相关的一次人工路径：能启动、能保存原图、能在页面继续下一步即可。出现真实错误就修复该路径；不为尚未实现的科目、解析器、模型或通知预先建立全量回归矩阵。

## 12. GitHub 复用清单

以下是可直接复用或后置评估的项目；本轮重构不因为列出项目就安装一堆依赖。

| 项目 | 复用结论 | 何时引入 | 明确不吸收 |
| --- | --- | --- | --- |
| [py-pdf/pypdf](https://github.com/py-pdf/pypdf) | 已用于有文本层 PDF 的按页提取 | 现在继续用 | 图片理解、手写和复杂版面 |
| [microsoft/markitdown](https://github.com/microsoft/markitdown) | 将 Word、Office、HTML 等资料转为统一 Markdown/文本，接入 MaterialArtifact → Passage | B/C 稳定后、真实 Word/网页出现时 | 不负责题目图片和解题诊断 |
| [adbar/trafilatura](https://github.com/adbar/trafilatura) | 网页正文和元数据抽取，作为网页资料的轻量入口 | 真实网页资料出现且 MarkItDown 结果不够时 | 不负责题目图片和学习事实 |
| [docling-project/docling](https://github.com/docling-project/docling) | 复杂 PDF/Office/表格/版面统一解析，可作为资料旁路 | 扫描或版面错乱成为实际问题时 | 不与现有解析器同时常驻、不替代原图 |
| [opendatalab/MinerU](https://github.com/opendatalab/MinerU) | 扫描 PDF、公式、表格和图片的高保真解析备选 | Docling 不够且真实资料证明需要时二选一；先审许可证 | 不与 Docling/Pix2Text/PaddleOCR 全部并行 |
| [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) | 唯一可选的图/向量 RAG sidecar；当前上游已吸收 RAG-Anything 的多模态方向 | 出现多跳/跨章节需求时 | 不拥有 SQLite 学习事实，不与另一套图 RAG 并行 |
| [HKUDS/RAG-Anything](https://github.com/HKUDS/RAG-Anything) | 作为 LightRAG 多模态能力的历史来源/兼容参考 | 仅在需要核对旧部署时 | 不再单独维护第二套服务 |
| [asg017/sqlite-vec](https://github.com/asg017/sqlite-vec) | SQLite 内的语义召回旁路，可保留元数据分区 | FTS 对相似题召回确实不足时 | 不提前引入独立向量数据库；其 pre-v1 状态需接受 |
| [breezedeus/Pix2Text](https://github.com/breezedeus/Pix2Text) | 中文、公式和版面 OCR 的本地派生文本 | 扫描教材需要本地 OCR 时择用 | 不把 OCR 当手写解题理解主路径 |
| [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | 更重的中文/公式/文档 OCR 备选 | 需要批量 OCR 或 Pix2Text 不够时二选一 | 不与 Pix2Text 同时作为默认依赖 |
| [openai/openai-python](https://github.com/openai/openai-python) | OpenAI Chat/Responses 协议调用的官方 SDK 参考 | B 阶段实现协议适配时，可选替换现有 urllib | 不把 SDK 当模型路由或业务事实库 |
| [anthropics/anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python) | Anthropic Messages 协议调用的官方 SDK 参考 | B 阶段实现协议适配时 | 不把 SDK 当统一 RAG 或答案确认器 |
| [Blaizzy/mlx-vlm](https://github.com/Blaizzy/mlx-vlm) | Mac 本地视觉模型回退的可选参考 | 云端模型不可用且确有本地需求时 | 不替代首版云端协议适配 |
| [open-spaced-repetition/py-fsrs](https://github.com/open-spaced-repetition/py-fsrs) | 后续个性化间隔算法 | 有真实回测数据后 | 当前固定 3/7/10/14 天不引入 |

选择原则很简单：先复用解析器和协议事实，保留本项目自己的错题事实与 UI；RAG、OCR、排程库都只能解决已观察到的问题。

## 13. 当前状态和完成定义

### 当前代码已经有

- 本地 SQLite 学习账本和 API/fixture 级文字回测演示链；
- 文字/PDF 文本层收录、按页 passage、FTS5、手动出处关联；
- 可选 OpenAI-compatible 文本 Chat 回答；
- 图片收录底座：multipart 多图上传、objects/ 原图落盘、CaptureBatch/IntakeItem/ImageAsset、预览、追加、排序、角色编辑和媒体读取；
- 一题分析草稿：多图视觉请求、`openai_chat`/`openai_responses`/`anthropic_messages` 适配与回退、可编辑候选字段、原始回答保留和失败重试；
- C 主链：轻量 FTS/已确认题目候选、确认晋级、QuestionSourceLink 出处回链、题面/过程图片快照隔离、正式错题列表/详情和首个 +3 天任务；
- C 的角色收口已完成：未指定角色也可确认并显示“待补题面”，确认后可在错题详情补选题面、过程、参考答案和顺序；演示/兼容数据有明确标识；
- 错题本轻量筛选：正式错题列表支持按 subject_key、chapter、knowledge_point、question_type 精确筛选；无筛选时分类为空的题目仍显示；
- D 主链：真实题面显示、回测过程图草稿与提交、初次/本次过程比较、可编辑诊断和固定 +3/+7/+10/+14 后续任务；比较失败不阻塞 Attempt 保存。
- E1 macOS 提醒：只读 SQLite 的 open/due ReviewTask，合并为单条 LaunchAgent 通知；安装与卸载脚本不改学习数据。
- start.command 启动本地服务并打开浏览器。

### 当前代码还没有（不能假装已完成）

- FTS 仍不是语义相似题召回，数学↔专业课轻跨科仍按调用方显式传入；
- 网页/复杂版面资料扩展，以及真实需要出现前的 LightRAG/向量旁路。

### 最终验收场景

用一题真实材料完成：

~~~text
Mac 浏览器选 1～N 张图
→ 原图立即可回看且归为一题
→ 看到匹配或模型草稿
→ 可编辑题面、答案、分类、错误原因和首次断点
→ 即使分类不全也能确认进错题本
→ 第 3/7/10/14 天收到一次提醒
→ 回测只显示题面并上传新的解题过程
→ 提交后看到比较和下一次行动
~~~

这条链跑通，才算“可用的最终版骨架”；其余图谱、OCR 精度、向量重排和多端能力都是在真实使用中证明必要后再加。

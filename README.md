# 知债 · 考研错题工作台

本地优先、单用户的图片错题本与课程知识工作台。

> **当前状态**：可运行的个人版。主链已经落地：一题多图保存、分析草稿、用户确认、课程知识树、闭卷回测、题库导入和政治客观题。真实手写照片上的公式识别、首次错误步骤定位和模型诊断质量仍需持续用真实资料验证。
>
> **产品边界**：固定四个科目类别——政治、英语、数学、专业课。具体考试方向、院校和专业课组合都由用户创建或导入，不写入前端页面契约或后端业务分支。

## Overview

知债把“做错一道题”整理成一条可复核的记录：

```text
拍照上传一道题的多张图片
    ↓
原图先保存
    ↓
识别题面、我的过程、参考答案
    ↓
生成章节 / 知识点 / 题型 / 错误诊断候选
    ↓
用户修改并确认
    ↓
进入正式错题本和个人知识树
    ↓
到期只看题面，选择会 / 不会 / 不确定
    ↓
需要时上传新的完整过程并回测
```

模型、OCR、资料检索和题库都只提供候选或证据。增强失败不会删除原始输入，也不会绕过用户确认直接写入正式错题字段。

## Highlights

- **四科通用模型**：政治、英语、数学、专业课是固定类别；`course_id` 表示用户真正使用的课程。
- **图片优先收录**：一题可以上传多张题面、解题过程和参考解图片，原图按顺序保留。
- **可编辑分析草稿**：题面、答案、章节、知识点、题型、错误类型、错误原因和首次出错步骤都可以人工修正。
- **确认后入档**：只有确认后的题目才进入正式错题本、个人知识树和回测队列。
- **闭卷回测**：回测接口只返回题面和当前草稿；历史答案、过程图和诊断在闭卷阶段不可见。
- **课程隔离**：题目、资料、题库、知识节点和回测都沿用同一个 `course_id` 边界。
- **资料先存后增强**：支持 PDF、PNG/JPG、DOCX、普通静态网页和短文本；解析失败时原件仍然保留。
- **题库导入**：CSV / JSON 先预览校验，再确认写入；已有题库可按课程、章节、题型和难度筛选。
- **政治选择题**：政治题库使用独立客观题流程，不进入数学和专业课的图片诊断主链。
- **独立设置页**：模型连接设置与知识区分离，模型不可用时不阻塞图片保存和草稿编辑。
- **本地事实源**：SQLite 保存学习事实，`objects/` 保存原始图片和资料，不需要云端服务。

## Requirements

- Python 3
- macOS（`start.command` 和到期通知使用 macOS 工具；Linux/Windows 可直接运行 Python 服务）
- 不需要 Node.js、前端构建工具或独立数据库服务
- 使用模型增强时，需要一个兼容已选协议的本地或远程模型端点；不配置模型也能完成保存、编辑和确认

## Quick start

### Start the local Web UI

```bash
./start.command
```

脚本会创建演示数据库（如果本地还没有 `library.sqlite`），启动 `127.0.0.1:8765`，然后打开默认浏览器。

手动启动：

```bash
# 可选：建立演示数据
python3 app.py seed --as-of 2026-09-01T00:00:00Z

# 启动本地服务
python3 app.py serve
```

打开 <http://127.0.0.1:8765/>。

### Use a disposable demo database

```bash
python3 app.py --db /tmp/knowledge-demo.sqlite seed --force --as-of 2026-09-01T00:00:00Z
python3 app.py --db /tmp/knowledge-demo.sqlite serve
```

`library.sqlite`、`objects/` 和本地模型 API key 不提交到 Git，也不会被演示数据命令覆盖。

## The current Web UI

前端由五个独立视图组成：**今日、复习、收录、知识、设置**。视图通过 hash 路由切换，当前视图之外的模块不会继续堆叠在同一长页面中。

### 今日

- 查看最早到期的回测任务；
- 查看待回测、已确认错题和课程数量；
- 从课程卡片切换当前课程；
- 进入复习或收录新题。

### 复习

- 查看全部到期任务；
- 按课程和错误类型筛选正式错题；
- 进入闭卷回测，选择“会 / 不会 / 不确定”；
- 可上传新的文字过程或一张/多张回测过程图片；
- 保存新的 Attempt，不覆盖历史 Attempt。

### 收录

- 上传一题的多张图片，原图先落盘；
- 选择课程和科目类别，后续调整图片角色；
- 运行图片分析和资料匹配；
- 编辑分析草稿，确认后进入正式错题本；
- 保存教材、讲义、网页 URL 或短文本，并尝试自动增强。

### 知识

- 按课程查看知识树和已确认题目；
- 查看和筛选当前课程题库；
- 预览并导入 CSV / JSON 题库；
- 进入政治客观题刷题入口。

### 设置

模型设置是独立页面，不属于知识区。可以配置主模型、回退模型、协议、地址、模型名和 API key。API key 只存本地 SQLite，读取接口只返回是否已配置。

## Subject and course model

系统固定四个 `subject_key`：

| `subject_key` | 中文类别 | 当前状态 |
| --- | --- | --- |
| `politics` | 政治 | 客观题库和判题已接入 |
| `english` | 英语 | 保留科目槽位，具体题型后置 |
| `math` | 数学 | 图片错题主链和题库接口已接入 |
| `professional` | 专业课 | 支持多个用户自定义课程 |

具体课程由 `Course` 表保存：

```text
科目类别：subject_key
具体课程：course_id + course_name + course_group
题目 / 资料 / 题库 / 知识节点：必须关联 course_id
```

示例中的课程名称只是用户数据，不是产品代码分支。创建课程：

```bash
curl -X POST http://127.0.0.1:8765/api/courses \
  -H 'Content-Type: application/json' \
  -d '{"course_group":"我的考试方向","course_name":"专业课组合","subject_key":"professional"}'
```

## Architecture

```text
Browser
  ├── index.html       页面壳层和五个视图
  ├── frontend.css     视觉令牌、布局和响应式规则
  └── frontend.js      hash 路由、状态和 API 适配

Python HTTP service
  ├── app.py            Store、领域流程和本地 API
  ├── schema.sql        SQLite 结构
  └── objects/          原始图片和资料

SQLite
  ├── Course / KnowledgeNode
  ├── Question / QuestionRevision / Attempt
  ├── ReviewTask / ReviewSession / Assessment
  ├── SourceArtifact / SourcePassage / Answer
  └── QuestionBankItem / QuestionBankAttempt
```

SQLite 是唯一学习事实源。模型、资料检索和题库产生的结果先作为候选或证据保存，用户确认后才成为正式字段。

## Main API surface

所有接口返回 `{ "data": ... }` 或 `{ "error": ... }`。

### Courses and knowledge

```text
GET    /api/courses
POST   /api/courses
GET    /api/knowledge?course_id=<course_id>
GET    /api/knowledge-nodes?course_id=<course_id>
POST   /api/knowledge-nodes
PATCH  /api/knowledge-nodes/<node_id>
```

### Image intake

```text
POST   /api/intake/batches
GET    /api/intake
GET    /api/intake/<intake_id>
PATCH  /api/intake/<intake_id>
POST   /api/intake/<intake_id>/assets
POST   /api/intake/<intake_id>/analyze
POST   /api/intake/<intake_id>/resolve
POST   /api/intake/<intake_id>/confirm
```

确认入档的最小正式字段包括：原题图片、本人过程、参考答案或参考解、错误类型、首次出错步骤、关联知识点、关联章节、题型或方法。字段可以暂缺，页面显示“待补充”，不会阻止原图保存或后续编辑。

### Sources and answers

```text
POST   /api/capture/source
POST   /api/enrich
GET    /api/sources?limit=30
GET    /api/source/<source_artifact_id>
GET    /api/search?q=<keyword>
POST   /api/question-source-link
POST   /api/answer
GET    /api/answer/<answer_id>
```

资料支持 PDF、PNG/JPG、DOCX、普通静态网页 URL 和短文本。PDF 按页保存定位，DOCX 保留段落/表格定位，扫描 PDF 和图片在可用时使用 OCR。`grounded`、`unlocated`、`unavailable` 是回答结果状态，不是保存门禁。

### Reviews and wrong questions

```text
GET    /api/reviews/due
GET    /api/due-review
GET    /api/wrong-questions?course_id=<course_id>
GET    /api/wrong-questions/<question_id>
GET    /api/wrong-questions/<question_id>/review?review_task_id=<task_id>
POST   /api/start
POST   /api/wrong-questions/<question_id>/redo
POST   /api/attempts/<attempt_id>/submit
```

`/api/wrong-questions/<question_id>/review` 是闭卷脱敏接口，只返回题面、课程和当前草稿，不返回参考答案、历史 Attempt、历史过程图片或比较诊断。

### Question bank

```text
POST   /api/question-bank/preview
POST   /api/question-bank/import
GET    /api/question-bank?course_id=<course_id>
PATCH  /api/question-bank/<item_id>
PATCH  /api/question-bank
POST   /api/question-bank/<item_id>/start
POST   /api/question-bank/<item_id>/answer
GET    /api/question-bank/attempts?course_id=<course_id>
GET    /api/wrong-questions/<question_id>/similar
```

题库导入先预览校验，再确认写入。最小字段包括 `course_id`、`question_text`、`chapter`、`question_type`、`difficulty`、`reference_answer` 和 `explanation`；政治选择题可以额外提供 `options`。

### Model settings

```text
GET    /api/settings/model
PATCH  /api/settings/model
```

未保存配置时可使用 `LLM_*` 和 `LLM_FALLBACK_*` 环境变量。API key 不写入 README、脚本或 Git。

## Review scheduling and notifications

第一版回测使用固定的 `+3 / +7 / +10 / +14` 天排程。系统不会根据一次“会”自动宣布掌握，也不自动生成整体学习计划。

`notify_due.py` 只读 SQLite，发送到期数量和最早日期，不读取题面或答案：

```bash
python3 notify_due.py --db /path/to/library.sqlite --now 2026-09-03T00:00:00Z
./install_notifications.command
./uninstall_notifications.command
```

## Validation

```bash
python3 -m py_compile app.py
node --check frontend.js
python3 run_p0_scenarios.py
git diff --check
```

`run_p0_scenarios.py` 使用临时数据库，覆盖文字演示链、图片 intake、失败保留、课程隔离、知识候选、题库、政治客观题、回测和自评。它不能替代真实模型服务和真实手写照片验收。

## Known limitations and next steps

当前主链已接通，但以下内容仍明确后置或待补强：

- 真实手写照片上的公式、题面、参考答案和首次错误步骤识别质量；
- 复杂跨资料语义聚类、向量检索和知识树自动合并；
- 英语具体题型；
- 政治分析题和政治图片错题；
- 自动学习计划、FSRS、大规模学习统计和自动主观判卷；
- 自动生成新题；
- 题库维护、相似题练习、资料回链和问答的完整前端操作流。

后续推进顺序以真实失败证据为准：先验证图片收录和诊断质量，再补课程内知识候选确认和题库练习体验，最后再评估语义检索或更复杂的模型能力。

## Repository layout

```text
index.html              前端壳层和五个视图
frontend.css            前端视觉令牌、布局和响应式规则
frontend.js             前端路由、状态和 API 适配
app.py                  SQLite 存储、本地 HTTP API 和服务入口
schema.sql              数据库结构
fixtures/               可提交的演示数据
objects/                本地原始资产，不提交
library.sqlite          本地事实库，不提交
design.md               产品边界、架构、路线和前端设计基线
AGENTS.md               Agent 开发约束
start.command           macOS 启动入口
notify_due.py           到期回测只读通知
```

## Development notes

- 当前唯一现役开发分支是 `mzy`；`main` 只通过 Pull Request 合并。
- 不提交 `library.sqlite`、`objects/`、API key、密码或个人配置。
- 修改前先读本文件和 [design.md](design.md)；修改后至少运行 Python 语法检查，涉及流程时运行定向场景。
- 代码、运行态、文档和规则出现矛盾时，以当前代码和真实 API 为事实，再同步文档。

# MineKBase

<p align="center"><strong>Image-first mistake capture · Course knowledge trees · Closed-book review</strong><br>A local, single-user workspace for Chinese graduate entrance exam preparation</p>

<p align="center">English · <a href="README.zh-CN.md">简体中文</a></p>

<p align="center"><img src="https://img.shields.io/badge/status-personal%20preview-c46b2c" alt="Personal preview"> <img src="https://img.shields.io/badge/runtime-Python%203-3776ab" alt="Python 3"> <img src="https://img.shields.io/badge/storage-SQLite-003b57" alt="SQLite"> <img src="https://img.shields.io/badge/license-private%20use-lightgrey" alt="Private use"></p>

> **Status**: A runnable personal preview. Multi-image capture, analysis drafts, user confirmation, course knowledge trees, closed-book review, question-bank import, and political objective questions are connected. Handwritten formula OCR, first-error localization, and model diagnosis quality still require validation with real study material.

## Overview

MineKBase turns one missed question into an auditable, reviewable learning record:

```text
Upload several photos → preserve originals → generate analysis candidates
       → edit and confirm → formal mistake notebook / knowledge tree
       → closed-book review when due → submit a new attempt with history intact
```

The product has four generic subject categories: politics, English, mathematics, and professional courses. Exam directions, institutions, and professional-course combinations are user data, never hard-coded into frontend contracts or backend branches.

Models, OCR, material retrieval, and question banks provide candidates or evidence. Failed enhancement never blocks original capture and never bypasses user confirmation.

## Features

- **Multi-image capture**: keep question, personal work, and reference-solution images in order.
- **Editable analysis drafts**: edit prompt, answer, chapter, knowledge point, type, error type, reason, and first error step.
- **Controlled diagnosis**: knowledge gap, wrong method choice, or derivation/calculation error, with optional detail.
- **Course-scoped knowledge trees**: candidates stay within their course until confirmed.
- **Closed-book review**: show only the prompt, accept “know / do not know / unsure”, and preserve attempt history.
- **Material enrichment**: PDF, PNG/JPG, DOCX, static webpages, and short text with source locations where available.
- **Question-bank import**: preview and validate CSV/JSON before writing; filter by course, chapter, type, and difficulty.
- **Political objective bank**: separate grading and wrong-answer flow, isolated from image diagnosis.
- **Local-first storage**: SQLite is the source of truth and `objects/` keeps original assets.

## Web UI

Hash routing provides five focused workspaces: **Today, Review, Capture, Knowledge, and Settings**. Settings is separate from Knowledge, and course cards are driven by `/api/courses`.

| Workspace | Purpose |
| --- | --- |
| Today | See the earliest due task and enter the next action |
| Review | Filter mistakes, run closed-book reviews, submit attempts |
| Capture | Upload images, save materials, analyze, and confirm |
| Knowledge | Browse course trees, import banks, start political practice |
| Settings | Configure primary/fallback model connections |

Question-bank maintenance, similar-question practice, source backlinks, and Q&A APIs exist, while their complete page flows remain in progress.

## Subjects and courses

| `subject_key` | Category | Current role |
| --- | --- | --- |
| `politics` | Politics | Objective bank and grading |
| `english` | English | Reserved slot; concrete types are later |
| `math` | Mathematics | Image mistake flow and bank APIs |
| `professional` | Professional course | Multiple user-defined courses |

`subject_key` identifies a generic category. `course_id` is the fact boundary for questions, materials, banks, and knowledge nodes.

## Quick start

```bash
./start.command
```

The script creates demo data when needed, starts `127.0.0.1:8765`, and opens a browser. Manual startup:

```bash
python3 app.py seed --as-of 2026-09-01T00:00:00Z
python3 app.py serve
```

Open <http://127.0.0.1:8765/>. For a disposable database:

```bash
python3 app.py --db /tmp/knowledge-demo.sqlite seed --force --as-of 2026-09-01T00:00:00Z
python3 app.py --db /tmp/knowledge-demo.sqlite serve
```

Model enhancement is optional. Configure a compatible local or remote endpoint in Settings; capture, editing, and confirmation work without a model.

## Architecture

```text
Browser
  ├── index.html       shell and five views
  ├── frontend.css     visual tokens, layout, responsive rules
  └── frontend.js      routing, state, API adapter

Python HTTP service
  ├── app.py            domain flows, local API, server entrypoint
  ├── schema.sql        SQLite schema
  └── objects/          original images and materials

SQLite source of truth
  ├── Course / KnowledgeNode / Question
  ├── QuestionRevision / Attempt / ReviewTask
  ├── ReviewSession / Assessment
  ├── SourceArtifact / SourcePassage / Answer
  └── QuestionBankItem / QuestionBankAttempt
```

The implementation uses Python's standard HTTP library, SQLite, native HTML/CSS/JavaScript, optional PaddleOCR, and configurable local or remote model APIs.

## Validation

```bash
python3 -m py_compile app.py
node --check frontend.js
python3 run_p0_scenarios.py
git diff --check
```

The scenario runner uses a temporary database and covers image intake, failure retention, course isolation, knowledge candidates, question banks, political grading, review, and self-assessment. It does not replace real-model or handwritten-photo acceptance testing.

## API entry points

The complete contract is maintained in [design.md](design.md):

```text
GET/POST /api/courses                 GET/PATCH /api/knowledge-nodes
POST      /api/intake/batches         POST      /api/intake/<id>/analyze
POST      /api/intake/<id>/confirm    POST      /api/capture/source
POST      /api/enrich                 GET       /api/wrong-questions
GET/POST  /api/reviews/due            POST      /api/attempts/<id>/submit
POST      /api/question-bank/preview  POST      /api/question-bank/import
GET/PATCH /api/settings/model
```

## Roadmap and boundaries

1. Validate prompt, formula, reference-answer, and first-error extraction on real handwritten material.
2. Complete course-scoped candidate confirmation, bank maintenance, and similar-practice UI.
3. Complete political wrong-answer review, source backlinks, and Q&A pages.
4. Re-evaluate semantic clustering, vector retrieval, and more advanced model capabilities.

Out of scope for the current product: automatic study plans, FSRS, large-scale study statistics, automatic subjective grading, generated questions, and a concrete English question-type system.

## Repository layout

```text
index.html / frontend.css / frontend.js   frontend
app.py / schema.sql                        service and schema
fixtures/                                  committed demo data
objects/ / library.sqlite                  local data, ignored
design.md                                  product and architecture baseline
AGENTS.md                                  development constraints
start.command / notify_due.py              startup and due notification
```

## Development

- The active development line is `mzy`; changes reach `main` through a pull request.
- Never commit API keys, passwords, personal configuration, `library.sqlite`, or `objects/`.
- Read [design.md](design.md) before changes and run focused validation afterward.

## License and use

This repository is currently maintained for personal local study. Follow the licenses of third-party dependencies and referenced projects. The project has not published an independent open-source license declaration.

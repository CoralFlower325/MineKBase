#!/usr/bin/env python3
"""Local-first knowledge-debt loop.

The runtime is deliberately small and write-first. A user payload is saved
before parsing, retrieval, or model enrichment. Missing or incomplete
enrichment is represented in stored snapshots and can be filled later; it is
not a request-time gate.
"""
from __future__ import annotations

import argparse
import cgi
import csv
import datetime as dt
import io
import json
import mimetypes
import sqlite3
import tempfile
import threading
import unicodedata
import uuid
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "library.sqlite"
SNAPSHOT = "p0-snapshot-v1"  # label retained in historical snapshots
EVIDENCE = "evidence-v1"
VISIBILITY = "visibility-v1"
IMAGE_ROLES = {"question", "my_process", "reference", "redo_process", "mixed"}
SUBJECT_KEYS = {"math", "english", "politics", "professional"}
ERROR_TYPES = {
    "knowledge_gap": "知识点不会",
    "method_selection": "方法选择错误",
    "derivation_calculation": "推导或计算出错",
}
REVIEW_OFFSETS = (3, 7, 10, 14)
SCHEMA_VERSION = 4

TASK_PRIORITY = [
    "awaiting_assessment",
    "assisted_retry",
    "retry_after_fail",
    "incomplete_attempt",
    "initial_error",
    "manual_declaration",
    "spaced_confirmation",
]
SCENARIOS = [
    "P0-S1-debt-to-due",
    "P0-S2-start-abandon",
    "P0-S3-independent-submit",
    "P0-S4-independent-assessment",
    "P0-S5-assisted-or-unassessed",
    "P0-S6-finalize-chain",
]
def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_time(value: str | None) -> dt.datetime:
    try:
        return dt.datetime.strptime(value or "", "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except (TypeError, ValueError):
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def add_days(value: str, days: int) -> str:
    return (parse_time(value) + dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def knowledge_label_key(value: str) -> str:
    """Create a conservative course-local key for merging label variants."""
    text = unicodedata.normalize("NFKC", as_text(value)).strip().casefold()
    return "".join(char for char in text if not char.isspace() and char not in "·•:：,，;；-—_()（）[]【】")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(value, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def as_text(value, default: str = "") -> str:
    return value if isinstance(value, str) else default


def normalize_options(value):
    """Accept JSON/object, list, or simple A. text|B. text option formats."""
    if isinstance(value, dict):
        return {as_text(key).strip(): as_text(item).strip() for key, item in value.items() if as_text(key).strip()}
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            if isinstance(item, dict):
                key = as_text(item.get("key") or item.get("label") or item.get("value")).strip()
                text = as_text(item.get("text") or item.get("content") or item.get("label")).strip()
            else:
                key, text = "", as_text(item).strip()
            if not key:
                key = chr(ord("A") + index)
            if text:
                result[key] = text
        return result
    raw = as_text(value).strip()
    if not raw:
        return {}
    parsed = loads(raw, None)
    if isinstance(parsed, (dict, list)):
        return normalize_options(parsed)
    result = {}
    import re
    for index, part in enumerate(re.split(r"\s*[|；;]\s*|\n+", raw)):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"^([A-Ha-h])[.、)）:\s]+(.+)$", part)
        if match:
            result[match.group(1).upper()] = match.group(2).strip()
        else:
            result[chr(ord("A") + index)] = part
    return result


class DomainError(Exception):
    def __init__(self, code: str, message: str, details=None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class Store:
    """The SQLite learning ledger and its small command surface."""

    def __init__(self, path=DB_PATH, clock=None):
        self.path = Path(path)
        self.clock = clock or now_utc
        self.fts_available = False
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        # schema.sql is the only structural source. Re-running its CREATE IF
        # NOT EXISTS statements avoids a second startup contract.
        self.conn.executescript((ROOT / "schema.sql").read_text())
        self._ensure_schema_version()
        self._ensure_default_courses()
        self._ensure_source_fts()
        self.lock = threading.RLock()

    def _ensure_schema_version(self):
        current = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if current >= SCHEMA_VERSION:
            return
        if current < 1:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(SourceArtifact)")}
            if "subject_key" not in columns:
                self.conn.execute("ALTER TABLE SourceArtifact ADD COLUMN subject_key TEXT")
        if current < 2:
            source_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(SourceArtifact)")}
            if "course_id" not in source_columns:
                self.conn.execute("ALTER TABLE SourceArtifact ADD COLUMN course_id TEXT REFERENCES Course(course_id)")
            batch_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(CaptureBatch)")}
            if "course_id" not in batch_columns:
                self.conn.execute("ALTER TABLE CaptureBatch ADD COLUMN course_id TEXT REFERENCES Course(course_id)")
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def _ensure_default_courses(self):
        """Create the small first-run course set without constraining custom courses."""
        defaults = (
            ("course-math-1", "考研", "数学一", "math"),
            ("course-408", "计算机考研", "408", "professional"),
            ("course-signals", "电子类考研", "信号与系统", "professional"),
            ("course-politics", "公共课", "政治", "politics"),
        )
        now = self.clock()
        self.conn.executemany(
            "INSERT OR IGNORE INTO Course(course_id,course_group,course_name,subject_key,created_at,active) VALUES(?,?,?,?,?,1)",
            [(*row, now) for row in defaults],
        )
        self.conn.commit()

    def _ensure_source_fts(self):
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS SourcePassageFTS USING fts5(source_passage_id UNINDEXED, source_artifact_id UNINDEXED, text, tokenize='trigram')")
            # Backfill rows created by an older app version and repair stale
            # text, not only a differing row count. IDs remain those of
            # SourcePassage.
            mismatch = self.conn.execute(
                "SELECT EXISTS(SELECT source_passage_id,source_artifact_id,COALESCE(text,'') FROM SourcePassage EXCEPT SELECT source_passage_id,source_artifact_id,COALESCE(text,'') FROM SourcePassageFTS) OR EXISTS(SELECT source_passage_id,source_artifact_id,COALESCE(text,'') FROM SourcePassageFTS EXCEPT SELECT source_passage_id,source_artifact_id,COALESCE(text,'') FROM SourcePassage)"
            ).fetchone()[0]
            if mismatch:
                self.conn.execute("DELETE FROM SourcePassageFTS")
                self.conn.execute("INSERT INTO SourcePassageFTS(source_passage_id,source_artifact_id,text) SELECT source_passage_id,source_artifact_id,COALESCE(text,'') FROM SourcePassage ORDER BY source_artifact_id,ordinal")
                # Backfill before any normal write transaction so BEGIN
                # IMMEDIATE can start cleanly.
                self.conn.commit()
            self.fts_available = True
        except sqlite3.OperationalError:
            # FTS5/trigram is an optional acceleration. SourceArtifact and
            # SourcePassage remain usable through the LIKE path.
            self.conn.rollback()
            self.fts_available = False

    def one(self, query, args=()):
        with self.lock:
            return self.conn.execute(query, args).fetchone()

    def all(self, query, args=()):
        with self.lock:
            return self.conn.execute(query, args).fetchall()

    def begin(self):
        self.lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.conn.commit()
        self.lock.release()

    def rollback(self):
        self.conn.rollback()
        self.lock.release()

    # ---------- seed and normalization ----------

    def list_courses(self, include_inactive=False):
        clause = "" if include_inactive else " WHERE active=1"
        return [dict(row) for row in self.all(f"SELECT * FROM Course{clause} ORDER BY course_group,course_name,course_id")]

    def create_course(self, payload):
        payload = as_dict(payload)
        name = as_text(payload.get("course_name")).strip()
        group = as_text(payload.get("course_group"), "自定义").strip() or "自定义"
        subject = payload.get("subject_key") if payload.get("subject_key") in SUBJECT_KEYS else "professional"
        if not name:
            raise DomainError("invalid_course", "course_name is required")
        duplicate = self.one("SELECT course_id FROM Course WHERE course_name=? AND active=1", (name,))
        if duplicate:
            return dict(self.one("SELECT * FROM Course WHERE course_id=?", (duplicate["course_id"],)))
        course_id = uid("course")
        created = self.clock()
        self.begin()
        try:
            self.conn.execute("INSERT INTO Course(course_id,course_group,course_name,subject_key,created_at,active) VALUES(?,?,?,?,?,1)", (course_id, group, name, subject, created))
            row = self.one("SELECT * FROM Course WHERE course_id=?", (course_id,))
            self.commit()
            return dict(row)
        except Exception:
            self.rollback()
            raise

    def _course(self, course_id):
        if not course_id:
            return None
        return self.one("SELECT * FROM Course WHERE course_id=? AND active=1", (course_id,))

    def list_knowledge_nodes(self, course_id=None, include_candidates=True):
        clauses, args = [], []
        if course_id:
            clauses.append("course_id=?")
            args.append(course_id)
        if not include_candidates:
            clauses.append("confirmation_state='confirmed'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [dict(row) for row in self.all(f"SELECT * FROM KnowledgeNode{where} ORDER BY course_id,parent_id,name", args)]

    def create_knowledge_node(self, payload):
        payload = as_dict(payload)
        course_id = as_text(payload.get("course_id")).strip()
        name = as_text(payload.get("name")).strip()
        if not self._course(course_id):
            raise DomainError("invalid_course", "course_id is required")
        if not name:
            raise DomainError("invalid_knowledge_node", "name is required")
        parent_id = as_text(payload.get("parent_id")).strip() or None
        if parent_id:
            parent = self.one("SELECT knowledge_node_id FROM KnowledgeNode WHERE knowledge_node_id=? AND course_id=?", (parent_id, course_id))
            if not parent:
                raise DomainError("invalid_parent", "parent_id is not in the same course")
        node_id = uid("kn")
        created = self.clock()
        state = "confirmed" if payload.get("confirmation_state") == "confirmed" else "candidate"
        self.begin()
        try:
            self.conn.execute("INSERT INTO KnowledgeNode(knowledge_node_id,course_id,parent_id,name,aliases,origin,confirmation_state,created_at) VALUES(?,?,?,?,?,?,?,?)", (node_id, course_id, parent_id, name, dumps(as_list(payload.get("aliases"))), as_text(payload.get("origin"), "user"), state, created))
            row = self.one("SELECT * FROM KnowledgeNode WHERE knowledge_node_id=?", (node_id,))
            self.commit()
            return dict(row)
        except Exception:
            self.rollback()
            raise

    def update_knowledge_node(self, knowledge_node_id, payload):
        payload = as_dict(payload)
        row = self.one("SELECT * FROM KnowledgeNode WHERE knowledge_node_id=?", (knowledge_node_id,))
        if not row:
            raise DomainError("not_found", "knowledge node not found", {"knowledge_node_id": knowledge_node_id})
        state = as_text(payload.get("confirmation_state", row["confirmation_state"])).strip() or row["confirmation_state"]
        if state not in {"candidate", "confirmed", "archived"}:
            raise DomainError("invalid_knowledge_node", "confirmation_state is invalid")
        name = as_text(payload.get("name", row["name"])).strip()
        if not name and state != "archived":
            raise DomainError("invalid_knowledge_node", "name is required")
        parent_id = as_text(payload.get("parent_id", row["parent_id"])).strip() or None
        if parent_id:
            if parent_id == knowledge_node_id:
                raise DomainError("invalid_parent", "a knowledge node cannot parent itself")
            parent = self.one("SELECT knowledge_node_id FROM KnowledgeNode WHERE knowledge_node_id=? AND course_id=? AND confirmation_state!='archived'", (parent_id, row["course_id"]))
            if not parent:
                raise DomainError("invalid_parent", "parent_id is not in the same course")
        aliases = as_list(payload.get("aliases", loads(row["aliases"], [])))
        origin = "user" if state == "confirmed" else row["origin"]
        self.begin()
        try:
            self.conn.execute("UPDATE KnowledgeNode SET parent_id=?,name=?,aliases=?,origin=?,confirmation_state=? WHERE knowledge_node_id=?", (parent_id, name or row["name"], dumps(aliases), origin, state, knowledge_node_id))
            updated = self.one("SELECT * FROM KnowledgeNode WHERE knowledge_node_id=?", (knowledge_node_id,))
            self.commit()
            return dict(updated)
        except Exception:
            self.rollback()
            raise

    def _ensure_knowledge_candidates(self, course_id, candidates):
        """Materialize chapter/knowledge labels as reviewable candidate nodes."""
        course_id = as_text(course_id).strip()
        if not course_id:
            return []
        pairs = []
        for candidate in candidates or []:
            item = as_dict(candidate)
            chapter = as_text(item.get("chapter")).strip()
            point = as_text(item.get("knowledge_point")).strip()
            if chapter or point:
                pairs.append((chapter, point, as_text(item.get("origin"), "analysis_candidate")))
        if not pairs:
            return []
        created = self.clock()
        result = []
        self.begin()
        try:
            for chapter, point, origin in pairs:
                parent_id = None
                if chapter:
                    parents = self.all("SELECT * FROM KnowledgeNode WHERE course_id=? AND parent_id IS NULL AND confirmation_state!='archived' ORDER BY created_at", (course_id,))
                    parent = next((row for row in parents if knowledge_label_key(row["name"]) == knowledge_label_key(chapter) or chapter in loads(row["aliases"], [])), None)
                    if not parent:
                        parent_id = uid("kn")
                        self.conn.execute("INSERT INTO KnowledgeNode(knowledge_node_id,course_id,parent_id,name,aliases,origin,confirmation_state,created_at) VALUES(?,?,?,?,?,?,?,?)", (parent_id, course_id, None, chapter, dumps([]), origin, "candidate", created))
                    else:
                        parent_id = parent["knowledge_node_id"]
                        aliases = loads(parent["aliases"], [])
                        if chapter != parent["name"] and chapter not in aliases:
                            aliases.append(chapter)
                            self.conn.execute("UPDATE KnowledgeNode SET aliases=? WHERE knowledge_node_id=?", (dumps(aliases), parent_id))
                    result.append(dict(self.one("SELECT * FROM KnowledgeNode WHERE knowledge_node_id=?", (parent_id,))))
                if point:
                    if parent_id is None:
                        nodes = self.all("SELECT * FROM KnowledgeNode WHERE course_id=? AND confirmation_state!='archived' AND parent_id IS NULL ORDER BY created_at", (course_id,))
                    else:
                        nodes = self.all("SELECT * FROM KnowledgeNode WHERE course_id=? AND confirmation_state!='archived' AND parent_id=? ORDER BY created_at", (course_id, parent_id))
                    node = next((row for row in nodes if knowledge_label_key(row["name"]) == knowledge_label_key(point) or point in loads(row["aliases"], [])), None)
                    if not node:
                        node_id = uid("kn")
                        self.conn.execute("INSERT INTO KnowledgeNode(knowledge_node_id,course_id,parent_id,name,aliases,origin,confirmation_state,created_at) VALUES(?,?,?,?,?,?,?,?)", (node_id, course_id, parent_id, point, dumps([]), origin, "candidate", created))
                        node = self.one("SELECT * FROM KnowledgeNode WHERE knowledge_node_id=?", (node_id,))
                    elif point != node["name"]:
                        aliases = loads(node["aliases"], [])
                        if point not in aliases:
                            aliases.append(point)
                            self.conn.execute("UPDATE KnowledgeNode SET aliases=? WHERE knowledge_node_id=?", (dumps(aliases), node["knowledge_node_id"]))
                            node = self.one("SELECT * FROM KnowledgeNode WHERE knowledge_node_id=?", (node["knowledge_node_id"],))
                    result.append(dict(node))
            self.commit()
            unique = {}
            for node in result:
                unique[node["knowledge_node_id"]] = node
            return list(unique.values())
        except Exception:
            self.rollback()
            raise

    def import_question_bank(self, payload):
        payload = as_dict(payload)
        preview = self.preview_question_bank(payload)
        if not preview["valid"]:
            raise DomainError("invalid_question_bank", "题库存在无法导入的行", preview)
        rows = payload.get("items") if isinstance(payload.get("items"), list) else payload.get("rows")
        if not isinstance(rows, list):
            rows = [payload] if payload.get("course_id") else []
        prepared = []
        for raw in rows:
            item = as_dict(raw)
            course_id = as_text(item.get("course_id") or payload.get("course_id")).strip()
            if not self._course(course_id):
                raise DomainError("invalid_course", "each question bank item needs a valid course_id", {"course_id": course_id})
            node_id = as_text(item.get("knowledge_node_id")).strip() or None
            if not node_id and as_text(item.get("knowledge_point")).strip():
                candidates = self._ensure_knowledge_candidates(course_id, [{
                    "chapter": item.get("chapter"),
                    "knowledge_point": item.get("knowledge_point"),
                    "origin": "question_bank",
                }])
                point = as_text(item.get("knowledge_point")).strip()
                chapter = as_text(item.get("chapter")).strip()
                matching = [candidate for candidate in candidates if candidate.get("name") == point and (not chapter or candidate.get("parent_id"))]
                node_id = matching[-1]["knowledge_node_id"] if matching else None
            prepared.append((item, course_id, node_id))
        imported = []
        self.begin()
        try:
            for item, course_id, node_id in prepared:
                if node_id:
                    node = self.one("SELECT course_id FROM KnowledgeNode WHERE knowledge_node_id=? AND confirmation_state!='archived'", (node_id,))
                    if not node or node["course_id"] != course_id:
                        raise DomainError("invalid_knowledge_node", "question bank knowledge node must belong to its course", {"knowledge_node_id": node_id})
                item_id = as_text(item.get("question_bank_item_id")).strip() or uid("bank")
                self.conn.execute("INSERT OR REPLACE INTO QuestionBankItem(question_bank_item_id,course_id,question_text,image_path,chapter,knowledge_node_id,question_type,difficulty,reference_answer,explanation,source,year,raw_payload,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, course_id, as_text(item.get("question_text")), as_text(item.get("image_path")) or None, as_text(item.get("chapter")) or None, node_id, as_text(item.get("question_type")) or None, as_text(item.get("difficulty")) or None, as_text(item.get("reference_answer")), as_text(item.get("explanation")), as_text(item.get("source")), as_text(item.get("year")), dumps(item), self.clock()))
                imported.append(item_id)
            self.commit()
            return {"imported": len(imported), "question_bank_item_ids": imported}
        except Exception:
            self.rollback()
            raise

    def preview_question_bank(self, payload):
        """Validate imported rows without writing any QuestionBankItem."""
        payload = as_dict(payload)
        rows = payload.get("items") if isinstance(payload.get("items"), list) else payload.get("rows")
        if not isinstance(rows, list):
            rows = [payload] if payload.get("course_id") else []
        previews = []
        seen_ids = set()
        for index, raw in enumerate(rows, 1):
            item = as_dict(raw)
            course_id = as_text(item.get("course_id") or payload.get("course_id")).strip()
            errors = []
            course = self._course(course_id)
            if not course:
                errors.append({"field": "course_id", "code": "invalid_course", "message": "课程不存在或未指定"})
            question_text = as_text(item.get("question_text")).strip()
            image_path = as_text(item.get("image_path")).strip()
            if not question_text and not image_path:
                errors.append({"field": "question_text", "code": "missing_question", "message": "题面文字和图片路径至少填写一项"})
            item_id = as_text(item.get("question_bank_item_id")).strip()
            if item_id:
                if item_id in seen_ids:
                    errors.append({"field": "question_bank_item_id", "code": "duplicate_question_bank_item_id", "message": "当前文件中重复的题库题目 ID"})
                seen_ids.add(item_id)
            node_id = as_text(item.get("knowledge_node_id")).strip()
            if node_id:
                node = self.one("SELECT course_id,confirmation_state FROM KnowledgeNode WHERE knowledge_node_id=?", (node_id,))
                if not node or node["confirmation_state"] == "archived" or node["course_id"] != course_id:
                    errors.append({"field": "knowledge_node_id", "code": "invalid_knowledge_node", "message": "知识节点不存在或不属于所选课程"})
            normalized = {
                "question_bank_item_id": item_id or None,
                "course_id": course_id,
                "question_text": question_text,
                "image_path": image_path or None,
                "chapter": as_text(item.get("chapter")).strip() or None,
                "knowledge_point": as_text(item.get("knowledge_point")).strip() or None,
                "knowledge_node_id": node_id or None,
                "question_type": as_text(item.get("question_type")).strip() or None,
                "difficulty": as_text(item.get("difficulty")).strip() or None,
                "reference_answer": as_text(item.get("reference_answer")),
                "explanation": as_text(item.get("explanation")),
                "source": as_text(item.get("source")),
                "year": as_text(item.get("year")),
            }
            previews.append({"row": index, "status": "error" if errors else "ready", "errors": errors, "item": normalized})
        return {"valid": bool(previews) and not any(row["errors"] for row in previews), "rows": previews, "ready_count": sum(row["status"] == "ready" for row in previews), "error_count": sum(row["status"] == "error" for row in previews), "total": len(previews)}

    def list_question_bank(self, filters=None):
        filters = as_dict(filters)
        clauses, args = [], []
        for key in ("course_id", "knowledge_node_id", "chapter", "question_type", "difficulty"):
            value = as_text(filters.get(key)).strip()
            if value:
                clauses.append(f"{key}=?")
                args.append(value)
        query = "SELECT * FROM QuestionBankItem" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC,question_bank_item_id LIMIT ?"
        args.append(max(1, min(int(filters.get("limit", 100)), 500)))
        return [self._question_bank_item_dto(row) for row in self.all(query, args)]

    def _update_question_bank_item_in_tx(self, question_bank_item_id, payload):
        payload = as_dict(payload)
        row = self.one("SELECT * FROM QuestionBankItem WHERE question_bank_item_id=?", (question_bank_item_id,))
        if not row:
            raise DomainError("not_found", "question bank item not found", {"question_bank_item_id": question_bank_item_id})
        if "course_id" in payload and as_text(payload.get("course_id")).strip() not in {"", row["course_id"]}:
            raise DomainError("course_change_forbidden", "题库题目不能在维护时更换课程")
        course_id = row["course_id"]
        node_id = as_text(payload.get("knowledge_node_id", row["knowledge_node_id"])).strip() or None
        if node_id:
            node = self.one("SELECT course_id,confirmation_state FROM KnowledgeNode WHERE knowledge_node_id=?", (node_id,))
            if not node or node["course_id"] != course_id or node["confirmation_state"] == "archived":
                raise DomainError("invalid_knowledge_node", "知识节点不存在或不属于当前课程", {"knowledge_node_id": node_id})
        values = {key: payload[key] for key in ("question_text", "image_path", "chapter", "question_type", "difficulty", "reference_answer", "explanation", "source", "year") if key in payload}
        question_text = as_text(values.get("question_text", row["question_text"])).strip()
        image_path = as_text(values.get("image_path", row["image_path"])).strip()
        if not question_text and not image_path:
            raise DomainError("missing_question", "题面文字和图片路径至少填写一项")
        raw_payload = loads(row["raw_payload"], {})
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        raw_payload.update(payload)
        if "options" in payload:
            raw_payload["options"] = payload.get("options")
        updated = {
            "question_text": question_text,
            "image_path": image_path or None,
            "chapter": as_text(values.get("chapter", row["chapter"])).strip() or None,
            "knowledge_node_id": node_id,
            "question_type": as_text(values.get("question_type", row["question_type"])).strip() or None,
            "difficulty": as_text(values.get("difficulty", row["difficulty"])).strip() or None,
            "reference_answer": as_text(values.get("reference_answer", row["reference_answer"])),
            "explanation": as_text(values.get("explanation", row["explanation"])),
            "source": as_text(values.get("source", row["source"])),
            "year": as_text(values.get("year", row["year"])),
        }
        self.conn.execute("UPDATE QuestionBankItem SET question_text=?,image_path=?,chapter=?,knowledge_node_id=?,question_type=?,difficulty=?,reference_answer=?,explanation=?,source=?,year=?,raw_payload=? WHERE question_bank_item_id=?", (*updated.values(), dumps(raw_payload), question_bank_item_id))
        return self._question_bank_item_dto(self.one("SELECT * FROM QuestionBankItem WHERE question_bank_item_id=?", (question_bank_item_id,)))

    def update_question_bank_item(self, question_bank_item_id, payload):
        self.begin()
        try:
            result = self._update_question_bank_item_in_tx(question_bank_item_id, payload)
            self.commit()
            return result
        except Exception:
            self.rollback()
            raise

    def bulk_update_question_bank(self, payload):
        payload = as_dict(payload)
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        if not items:
            raise DomainError("invalid_question_bank", "items is required")
        updated = []
        self.begin()
        try:
            for item in items:
                item = as_dict(item)
                item_id = as_text(item.pop("question_bank_item_id", "")).strip()
                if not item_id:
                    raise DomainError("invalid_question_bank", "each update needs question_bank_item_id")
                updated.append(self._update_question_bank_item_in_tx(item_id, item))
            self.commit()
            return {"updated": len(updated), "items": updated}
        except Exception:
            self.rollback()
            raise

    def _question_bank_item_dto(self, row):
        item = dict(row)
        raw = loads(item.get("raw_payload"), {})
        item["options"] = normalize_options(as_dict(raw).get("options"))
        item["is_politics"] = item.get("course_id") == "course-politics"
        return item

    def answer_question_bank(self, question_bank_item_id, payload):
        payload = as_dict(payload)
        row = self.one("SELECT * FROM QuestionBankItem WHERE question_bank_item_id=?", (question_bank_item_id,))
        if not row:
            raise DomainError("not_found", "question bank item not found", {"question_bank_item_id": question_bank_item_id})
        item = self._question_bank_item_dto(row)
        selected = as_text(payload.get("selected_answer") or payload.get("answer")).strip().upper()
        correct = as_text(item.get("reference_answer")).strip().upper()
        if not selected:
            raise DomainError("invalid_answer", "请选择一个答案")
        if item.get("options") and selected not in {key.upper() for key in item["options"]}:
            raise DomainError("invalid_answer", "答案选项不在题目选项中", {"selected_answer": selected, "options": list(item["options"].keys())})
        is_correct = int(selected == correct)
        attempt_id = uid("qba")
        answered_at = self.clock()
        self.begin()
        try:
            self.conn.execute("INSERT INTO QuestionBankAttempt(question_bank_attempt_id,question_bank_item_id,course_id,selected_answer,correct_answer,is_correct,answered_at) VALUES(?,?,?,?,?,?,?)", (attempt_id, question_bank_item_id, row["course_id"], selected, correct, is_correct, answered_at))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return {"question_bank_attempt_id": attempt_id, "question_bank_item_id": question_bank_item_id, "selected_answer": selected, "correct_answer": correct, "is_correct": bool(is_correct), "explanation": item.get("explanation") or "", "reference_answer": item.get("reference_answer") or "", "answered_at": answered_at}

    def list_question_bank_attempts(self, filters=None):
        filters = as_dict(filters)
        clauses, args = [], []
        if filters.get("course_id"):
            clauses.append("a.course_id=?")
            args.append(filters["course_id"])
        if filters.get("question_bank_item_id"):
            clauses.append("a.question_bank_item_id=?")
            args.append(filters["question_bank_item_id"])
        if filters.get("incorrect_only") in {True, "1", 1, "true", "yes"}:
            clauses.append("a.is_correct=0")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.all("SELECT a.*,q.question_text,q.chapter,q.question_type,q.explanation,q.raw_payload FROM QuestionBankAttempt a JOIN QuestionBankItem q ON q.question_bank_item_id=a.question_bank_item_id" + where + " ORDER BY a.answered_at DESC LIMIT ?", args + [max(1, min(int(filters.get("limit", 100)), 500))])
        result = []
        for row in rows:
            item = dict(row)
            raw_payload = loads(item.pop("raw_payload", "{}"), {})
            item["options"] = normalize_options(as_dict(raw_payload).get("options"))
            result.append(item)
        return result

    def similar_question_bank(self, question_id, filters=None, limit=12):
        item = self._wrong_dto(question_id)
        if not item:
            raise DomainError("not_found", "wrong question not found", {"question_id": question_id})
        grading = as_dict(item.get("grading"))
        filters = as_dict(filters)
        values = {key: as_text(grading.get(key)).strip() for key in ("course_id", "chapter", "question_type", "difficulty")}
        node_id = as_text(grading.get("knowledge_node_id")).strip()
        explicit = {}
        for key in ("chapter", "question_type", "difficulty"):
            value = as_text(filters.get(key)).strip()
            if value:
                values[key] = value
                explicit[key] = value
        requested_node = as_text(filters.get("knowledge_node_id")).strip()
        if requested_node:
            node = self.one("SELECT course_id FROM KnowledgeNode WHERE knowledge_node_id=? AND confirmation_state!='archived'", (requested_node,))
            if not node or node["course_id"] != values["course_id"]:
                raise DomainError("invalid_knowledge_node", "knowledge node is not in the question course", {"knowledge_node_id": requested_node})
            node_id = requested_node
            explicit["knowledge_node_id"] = requested_node
        bank_filters = {"course_id": values["course_id"], "limit": limit * 4} if values["course_id"] else {}
        bank_filters.update({key: value for key, value in explicit.items() if key in {"knowledge_node_id", "chapter", "question_type", "difficulty"}})
        candidates = self.list_question_bank(bank_filters) if values["course_id"] else []
        scored = []
        for candidate in candidates:
            if candidate["question_bank_item_id"] == question_id:
                continue
            if explicit.get("knowledge_node_id") and candidate.get("knowledge_node_id") != explicit["knowledge_node_id"]: continue
            if explicit.get("chapter") and candidate.get("chapter") != explicit["chapter"]: continue
            if explicit.get("question_type") and candidate.get("question_type") != explicit["question_type"]: continue
            if explicit.get("difficulty") and candidate.get("difficulty") != explicit["difficulty"]: continue
            score = 0
            if node_id and candidate.get("knowledge_node_id") == node_id: score += 8
            if values["chapter"] and candidate.get("chapter") == values["chapter"]: score += 4
            if values["question_type"] and candidate.get("question_type") == values["question_type"]: score += 3
            if values.get("difficulty") and candidate.get("difficulty") == values["difficulty"]: score += 2
            candidate["match_score"] = score
            scored.append(candidate)
        scored.sort(key=lambda row: (-row["match_score"], row.get("created_at") or "", row["question_bank_item_id"]))
        return scored[:max(1, min(int(limit), 50))]

    def start_question_bank_item(self, question_bank_item_id):
        """Turn an imported bank row into an editable practice intake.

        This deliberately stops before Question/Attempt creation. The user
        must review the imported answer and confirm the intake before it can
        become a formal wrong question.
        """
        item = self.one("SELECT * FROM QuestionBankItem WHERE question_bank_item_id=?", (question_bank_item_id,))
        if not item:
            raise DomainError("not_found", "question bank item not found", {"question_bank_item_id": question_bank_item_id})
        course = self._course(item["course_id"])
        if not course:
            raise DomainError("invalid_course", "question bank item course not found", {"course_id": item["course_id"]})
        intake = self.create_intake_batch([], course_id=item["course_id"])
        draft_fields = {
            "analysis_status": "draft",
            "resolution_kind": "question_bank",
            "resolution_label": "题库练习候选",
            "question_text": as_text(item["question_text"]),
            "reference_answer": as_text(item["reference_answer"]),
            "answer_origin": "question_bank",
            "field_sources": {
                "question_text": "题库导入",
                "reference_answer": "题库导入",
                "chapter": "题库导入",
                "knowledge_point": "题库导入",
                "question_type": "题库导入",
            },
            "candidate_origin": "question_bank",
            "source_question_bank_item_id": item["question_bank_item_id"],
            "course_id": item["course_id"],
            "subject_key": course["subject_key"],
            "chapter": as_text(item["chapter"]),
            "knowledge_node_id": as_text(item["knowledge_node_id"]),
            "knowledge_point": "",
            "question_type": as_text(item["question_type"]),
            "difficulty": as_text(item["difficulty"]),
            "bank_explanation": as_text(item["explanation"]),
            "bank_source": as_text(item["source"]),
            "bank_year": as_text(item["year"]),
        }
        self.begin()
        try:
            self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft_fields), self.clock(), intake["intake_id"]))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return self._intake_detail(intake["intake_id"])

    def _demo_manifest(self) -> dict:
        path = ROOT / "fixtures" / "p0_fixture_manifest.json"
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            # Demo content is optional. This fallback keeps a new checkout
            # usable without making a JSON file a boot requirement.
            return {
                "course_pack_release": {"course_key": "signals_and_systems"},
                "learning_objective": {
                    "name": "待补充学习目标",
                    "description": "",
                    "observable_criteria": ["待补充"],
                },
                "question_revision": {
                    "question_units": [{"unit_ref": "whole", "label": "整题"}],
                    "question_unit_refs": ["whole"],
                    "observable_criteria": ["待补充"],
                    "origin_kind": "manual_seed",
                    "alignment_state": "pending",
                    "confidence": "unreviewed",
                },
                "review_prompt": {
                    "presentation_snapshot": {
                        "blocks": [{"kind": "text", "content": "待补充题面"}]
                    }
                },
                "grading_reference": {},
                "help_content_fixture": [],
                "historical_attempt": {
                    "response_text": "",
                    "response_selections": [],
                    "response_assets": [],
                    "completion_claim": "unknown",
                    "initial_debt_claim": "incomplete",
                    "submitted_at_offset_days": -2,
                },
            }

    def _presentation(self, value) -> dict:
        source = as_dict(value)
        blocks = []
        for index, raw in enumerate(as_list(source.get("blocks"))):
            block = as_dict(raw)
            blocks.append(
                {
                    **block,
                    "block_ref": as_text(block.get("block_ref"), f"block-{index + 1}"),
                    "kind": as_text(block.get("kind"), "text"),
                    "content": as_text(block.get("content")),
                    "leakage_state": as_text(block.get("leakage_state"), "unknown"),
                }
            )
        if not blocks:
            blocks = [
                {
                    "block_ref": "question",
                    "kind": "text",
                    "content": as_text(source.get("content"), "待补充题面"),
                    "leakage_state": as_text(source.get("leakage_state"), "unknown"),
                }
            ]
        return {**source, "schema_version": SNAPSHOT, "blocks": blocks}

    def _draft(self, value) -> dict:
        # Keep unknown user fields in the snapshot. Known fields get harmless
        # defaults, so a partial draft remains useful input.
        draft = dict(as_dict(value))
        draft["schema_version"] = SNAPSHOT
        draft["response_text"] = as_text(draft.get("response_text"))
        draft["response_selections"] = as_list(draft.get("response_selections"))
        draft["response_assets"] = as_list(draft.get("response_assets"))
        claim = draft.get("completion_claim")
        draft["completion_claim"] = claim if claim in {"complete", "partial", "incomplete", "unknown"} else "unknown"
        draft["external_help_reported"] = bool(draft.get("external_help_reported"))
        return draft

    def seed_p0(self, as_of=None, force=False):
        as_of = as_of or self.clock()
        # A destructive reset remains explicit and test-only; ordinary seed is
        # idempotent and never blocks a user who already has facts.
        if force and self.path.resolve() == DB_PATH.resolve():
            raise DomainError("seed_not_empty", "use --db for a disposable reset")
        self.begin()
        try:
            existing = self.one("SELECT * FROM Question ORDER BY created_at LIMIT 1")
            if existing and not force:
                revision = self.one("SELECT * FROM QuestionRevision WHERE question_id=? ORDER BY revision_no LIMIT 1", (existing["question_id"],))
                prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE question_id=? ORDER BY revision_no LIMIT 1", (existing["question_id"],))
                task = self.one("SELECT * FROM ReviewTask WHERE question_id=? ORDER BY created_at LIMIT 1", (existing["question_id"],))
                self.commit()
                return {
                    "question_id": existing["question_id"],
                    "question_revision_id": revision["question_revision_id"] if revision else None,
                    "prompt_id": prompt["review_prompt_revision_id"] if prompt else None,
                    "review_task_id": task["review_task_id"] if task else None,
                    "existing": True,
                    "as_of": as_of,
                }
            if force:
                # User-created answer and source links reference the learning
                # core. Remove those dependents first so the disposable force
                # reset keeps its existing semantics without disabling FKs.
                self.conn.execute("DELETE FROM QuestionSourceLink")
                self.conn.execute("DELETE FROM Answer")
                self.conn.execute("UPDATE Question SET current_question_revision_id=NULL")
                self.conn.execute("UPDATE QuestionRevision SET current_review_prompt_revision_id=NULL")
                self.conn.execute("UPDATE Attempt SET review_session_id=NULL")
                for table in ["EvidenceEvent", "Assessment", "Attempt", "ReviewSession", "ReviewTask", "ReviewPromptRevision", "QuestionRevision", "Question", "LearningObjective", "CoursePackRelease"]:
                    self.conn.execute(f"DELETE FROM {table}")

            manifest = self._demo_manifest()
            release_data = as_dict(manifest.get("course_pack_release"))
            lo_data = as_dict(manifest.get("learning_objective"))
            qr_data = as_dict(manifest.get("question_revision"))
            prompt_data = as_dict(manifest.get("review_prompt"))
            history = as_dict(manifest.get("historical_attempt"))
            release, objective, question, revision, prompt = [uid(prefix) for prefix in ("release", "lo", "q", "qr", "qpr")]
            criteria = as_list(lo_data.get("observable_criteria")) or ["待补充"]
            units = as_list(qr_data.get("question_units")) or [{"unit_ref": "whole", "label": "整题"}]
            unit_refs = as_list(qr_data.get("question_unit_refs")) or [as_text(as_dict(units[0]).get("unit_ref"), "whole")]
            revision_criteria = as_list(qr_data.get("observable_criteria")) or criteria
            mapping = [{
                "objective_ref": "obj-1",
                "learning_objective_id": objective,
                "role": "measured",
                "question_unit_refs": unit_refs,
                "observable_criteria": revision_criteria,
                "origin_kind": as_text(qr_data.get("origin_kind"), "manual_seed"),
                "alignment_state": as_text(qr_data.get("alignment_state"), "pending"),
                "confidence": as_text(qr_data.get("confidence"), "unreviewed"),
                "captured_at": as_of,
            }]
            grading = {**as_dict(manifest.get("grading_reference")), "data_origin": "demo", "display_label": "演示数据"}
            help_fixture = as_list(manifest.get("help_content_fixture"))
            presentation = self._presentation(prompt_data.get("presentation_snapshot"))
            presentation.update({"data_origin": "demo", "display_label": "演示数据"})
            self.conn.execute("INSERT INTO CoursePackRelease(release_id,course_key,created_at) VALUES(?,?,?)", (release, as_text(release_data.get("course_key"), "signals_and_systems"), as_of))
            self.conn.execute("INSERT INTO LearningObjective(learning_objective_id,course_pack_release_id,name,description,observable_criteria,created_at) VALUES(?,?,?,?,?,?)", (objective, release, as_text(lo_data.get("name"), "待补充学习目标"), as_text(lo_data.get("description")), dumps(criteria), as_of))
            self.conn.execute("INSERT INTO Question(question_id,course_pack_release_id,current_question_revision_id,lifecycle_state,created_at) VALUES(?,?,?,?,?)", (question, release, None, "active", as_of))
            self.conn.execute("INSERT INTO QuestionRevision(question_revision_id,question_id,revision_no,revision_state,supersedes_revision_id,current_review_prompt_revision_id,question_units,objective_mapping_snapshot,grading_reference_fixture_snapshot,help_content_fixture_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (revision, question, 1, "confirmed", None, None, dumps(units), dumps(mapping), dumps(grading), dumps(help_fixture), as_of))
            self.conn.execute("INSERT INTO ReviewPromptRevision(review_prompt_revision_id,question_id,question_revision_id,revision_no,presentation_snapshot,unresolved_critical_ambiguities,leakage_state,revision_state,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (prompt, question, revision, 1, dumps(presentation), dumps([]), "clean", "ready", as_of))
            self.conn.execute("UPDATE Question SET current_question_revision_id=? WHERE question_id=?", (revision, question))
            self.conn.execute("UPDATE QuestionRevision SET current_review_prompt_revision_id=? WHERE question_revision_id=?", (prompt, revision))

            submitted_at = add_days(as_of, int(history.get("submitted_at_offset_days", -2)))
            debt_kind = history.get("initial_debt_claim") if history.get("initial_debt_claim") in {"wrong", "incomplete", "uncertain"} else "incomplete"
            response = self._draft({"response_text": history.get("response_text"), "response_selections": history.get("response_selections"), "response_assets": history.get("response_assets"), "completion_claim": history.get("completion_claim"), "external_help_reported": False, "data_origin": "demo", "display_label": "演示数据"})
            attempt = uid("attempt")
            self.conn.execute("INSERT INTO Attempt(attempt_id,question_id,question_revision_id,review_session_id,origin_kind,submission_state,submitted_at,completion_claim,initial_debt_claim,initial_debt_claim_basis,initial_debt_claim_captured_at,response_snapshot,assistance_state,external_help_reported,submit_event_ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt, question, revision, None, "initial", "submitted", submitted_at, response["completion_claim"], debt_kind, "seed_fixture", submitted_at, dumps({**response, "assistance_state": "none_observed"}), "none_observed", 0, None, as_of))
            reason = {"wrong": "initial_error", "incomplete": "incomplete_attempt", "uncertain": "manual_declaration"}[debt_kind]
            task = self._schedule_in_tx(question, revision, prompt, {"trigger_ref": uid("trigger"), "trigger_kind": "attempt", "trigger_reason_kind": reason, "attempt_id": attempt, "source_question_revision_id": revision, "captured_at": submitted_at, "data_origin": "demo", "display_label": "演示数据"}, as_of)
            # Keep the disposable P0 fixture immediately due at its declared
            # as_of time; real image intakes use the fixed confirmation anchor.
            if task:
                self.conn.execute("UPDATE ReviewTask SET due_at=? WHERE review_task_id=?", (as_of, task["review_task_id"]))
            self.commit()
            return {"release_id": release, "objective_id": objective, "question_id": question, "question_revision_id": revision, "prompt_id": prompt, "seed_attempt_id": attempt, "review_task_id": task["review_task_id"], "as_of": as_of}
        except Exception:
            self.rollback()
            raise

    # ---------- real, write-first capture ----------

    def capture_source(self, payload, files=None):
        """Persist source text, paths, or multipart files before enrichment."""
        payload = payload if isinstance(payload, dict) else {"raw_payload": payload}
        files = files or []
        created = self.clock()
        subject = payload.get("subject_key") if payload.get("subject_key") in SUBJECT_KEYS else None
        course_id = as_text(payload.get("course_id")).strip() or None
        course = self._course(course_id)
        if course_id and not course:
            raise DomainError("invalid_course", "course_id not found", {"course_id": course_id})
        if not subject and course:
            subject = course["subject_key"]
        source_name = payload.get("source_name") if isinstance(payload.get("source_name"), str) else ""
        records = []
        for file in files:
            filename = (file.get("filename") or source_name or "material").strip() or "material"
            mime = file.get("mime") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            suffix = Path(filename).suffix[:12]
            kind = "pdf" if mime == "application/pdf" or suffix.lower() == ".pdf" else "docx" if suffix.lower() == ".docx" or "wordprocessingml.document" in mime else "image" if mime.startswith("image/") else "file"
            artifact_id = uid("source")
            target = ROOT / "objects" / "sources" / f"{artifact_id}{suffix}"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(file.get("data") or b"")
                stored_path = str(target.relative_to(ROOT))
            except Exception as error:
                # Preserve the DB record even if the copy fails; the failure is
                # visible and can be retried without rolling back other files.
                stored_path = None
                payload = {**payload, "save_error": str(error)}
            records.append((artifact_id, kind, filename, stored_path, None, {**payload, "filename": filename, "mime": mime}, subject, course_id))
        if not records:
            source_url = payload.get("source_url") if isinstance(payload.get("source_url"), str) else ""
            if source_url.strip():
                records.append((uid("source"), "webpage", source_name or source_url.strip(), None, None, {**payload, "source_url": source_url.strip()}, subject, course_id))
        if not records:
            raw_text = payload.get("raw_text")
            if not isinstance(raw_text, str):
                for key in ("text", "content"):
                    if isinstance(payload.get(key), str):
                        raw_text = payload[key]
                        break
            stored_path = payload.get("stored_path")
            if not isinstance(stored_path, str):
                for key in ("file_path", "image_path", "path"):
                    if isinstance(payload.get(key), str):
                        stored_path = payload[key]
                        break
            kind = payload.get("kind") if isinstance(payload.get("kind"), str) and payload.get("kind") else "image" if payload.get("image_path") else "text" if isinstance(raw_text, str) else "file"
            records.append((uid("source"), kind, source_name or (payload.get("name") if isinstance(payload.get("name"), str) else ""), stored_path if isinstance(stored_path, str) else None, raw_text if isinstance(raw_text, str) else None, payload, subject, course_id))
        self.begin()
        try:
            for artifact_id, kind, name, stored_path, raw_text, raw_payload, subject_key, record_course_id in records:
                self.conn.execute(
                    "INSERT INTO SourceArtifact(source_artifact_id,kind,source_name,stored_path,raw_text,raw_payload,subject_key,course_id,parse_state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (artifact_id, kind, name, stored_path, raw_text, dumps(raw_payload), subject_key, record_course_id, "pending", created),
                )
            self.commit()
            result = {"source_artifact_id": records[0][0], "parse_state": "pending"}
            if len(records) > 1:
                result["source_artifact_ids"] = [record[0] for record in records]
            return result
        except Exception:
            self.rollback()
            raise

    def _capture_default_context(self, created):
        release = self.one("SELECT * FROM CoursePackRelease ORDER BY created_at LIMIT 1")
        if release:
            objective = self.one("SELECT * FROM LearningObjective WHERE course_pack_release_id=? ORDER BY created_at LIMIT 1", (release["release_id"],))
            return release["release_id"], objective["learning_objective_id"] if objective else None
        release_id, objective_id = uid("release"), uid("lo")
        self.conn.execute("INSERT INTO CoursePackRelease(release_id,course_key,created_at) VALUES(?,?,?)", (release_id, "capture_default", created))
        self.conn.execute("INSERT INTO LearningObjective(learning_objective_id,course_pack_release_id,name,description,observable_criteria,created_at) VALUES(?,?,?,?,?,?)", (objective_id, release_id, "待补充学习目标", "", dumps(["待补充"]), created))
        return release_id, objective_id

    def capture_question(self, payload):
        """Create the question/revision/prompt immediately, with optional initial attempt."""
        payload = payload if isinstance(payload, dict) else {"raw_payload": payload}
        created = self.clock()
        question_text = ""
        for key in ("question_text", "question", "prompt", "text", "content"):
            if isinstance(payload.get(key), str):
                question_text = payload[key]
                break
        self.begin()
        try:
            release_id, objective_id = self._capture_default_context(created)
            question_id, revision_id, prompt_id = uid("q"), uid("qr"), uid("qpr")
            units = payload.get("question_units") if isinstance(payload.get("question_units"), list) else []
            units = units or [{"unit_ref": "whole", "label": "整题"}]
            mapping = [{"objective_ref": "obj-1", "learning_objective_id": objective_id, "role": "measured", "question_unit_refs": [as_text(as_dict(units[0]).get("unit_ref"), "whole")], "origin_kind": "capture", "alignment_state": "pending", "confidence": "unreviewed", "captured_at": created}]
            presentation = {"schema_version": SNAPSHOT, "content": question_text, "raw_payload": payload, "blocks": [{"block_ref": "question", "kind": "text", "content": question_text, "leakage_state": "unknown"}]}
            self.conn.execute("INSERT INTO Question(question_id,course_pack_release_id,current_question_revision_id,lifecycle_state,created_at) VALUES(?,?,?,?,?)", (question_id, release_id, None, "candidate", created))
            self.conn.execute("INSERT INTO QuestionRevision(question_revision_id,question_id,revision_no,revision_state,supersedes_revision_id,current_review_prompt_revision_id,question_units,objective_mapping_snapshot,grading_reference_fixture_snapshot,help_content_fixture_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (revision_id, question_id, 1, "candidate", None, None, dumps(units), dumps(mapping), dumps({}), dumps([]), created))
            self.conn.execute("INSERT INTO ReviewPromptRevision(review_prompt_revision_id,question_id,question_revision_id,revision_no,presentation_snapshot,unresolved_critical_ambiguities,leakage_state,revision_state,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (prompt_id, question_id, revision_id, 1, dumps(presentation), dumps([]), "unknown", "candidate", created))
            self.conn.execute("UPDATE Question SET current_question_revision_id=? WHERE question_id=?", (revision_id, question_id))
            self.conn.execute("UPDATE QuestionRevision SET current_review_prompt_revision_id=? WHERE question_revision_id=?", (prompt_id, revision_id))

            answer = None
            for key in ("response_text", "answer", "my_answer", "response"):
                if key in payload and payload.get(key) not in (None, "", [], {}):
                    answer = payload.get(key)
                    break
            debt = payload.get("initial_debt_claim", payload.get("debt_claim"))
            declaration = payload.get("declaration_kind", payload.get("knowledge_debt"))
            debt_kind_value = debt if isinstance(debt, str) else None
            declaration_value = declaration if isinstance(declaration, str) else None
            has_attempt = answer not in (None, "", [], {}) or debt_kind_value in {"wrong", "incomplete", "uncertain"} or declaration not in (None, "", False)
            result = {"question_id": question_id, "question_revision_id": revision_id, "review_prompt_revision_id": prompt_id}
            if has_attempt:
                claim = payload.get("completion_claim") if payload.get("completion_claim") in {"complete", "partial", "incomplete", "unknown"} else ("complete" if answer not in (None, "", [], {}) else "unknown")
                debt_kind = debt_kind_value if debt_kind_value in {"wrong", "incomplete", "uncertain"} else {"wrong": "wrong", "incomplete": "incomplete", "uncertain": "uncertain"}.get(declaration_value)
                response = self._draft({**payload, "response_text": answer if isinstance(answer, str) else as_text(answer), "completion_claim": claim})
                attempt_id = uid("attempt")
                self.conn.execute("INSERT INTO Attempt(attempt_id,question_id,question_revision_id,review_session_id,origin_kind,submission_state,submitted_at,completion_claim,initial_debt_claim,initial_debt_claim_basis,initial_debt_claim_captured_at,response_snapshot,assistance_state,external_help_reported,submit_event_ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, question_id, revision_id, None, "initial", "submitted", created, claim, debt_kind, "capture", created, dumps({**response, "assistance_state": "none_observed"}), "none_observed", int(bool(payload.get("external_help_reported"))), None, created))
                task = self._schedule_in_tx(question_id, revision_id, prompt_id, {"trigger_ref": uid("trigger"), "trigger_kind": "attempt", "attempt_id": attempt_id, "source_question_revision_id": revision_id, "captured_at": created}, created)
                result.update({"attempt_id": attempt_id, "review_task_id": task["review_task_id"]})
            self.commit()
            return result
        except Exception:
            self.rollback()
            raise

    # ---------- image-first intake foundation (A stage) ----------

    def _asset_dict(self, row):
        item = dict(row)
        item["media_url"] = f"/media/{item['asset_id']}" if item.get("path") else None
        return item

    def _confirmed_asset_ids(self, draft):
        """Return only the assets already captured by a confirmed snapshot.

        New files may still be appended to a confirmed intake (for example a
        reference answer found later); those files remain editable without
        changing the historical question or initial Attempt.
        """
        question_id = as_dict(draft).get("confirmed_question_id")
        if not question_id:
            return set()
        question = self.one("SELECT current_question_revision_id FROM Question WHERE question_id=?", (question_id,))
        revision = self.one("SELECT grading_reference_fixture_snapshot,current_review_prompt_revision_id FROM QuestionRevision WHERE question_revision_id=?", (question["current_question_revision_id"],)) if question and question["current_question_revision_id"] else None
        grading = as_dict(loads(revision["grading_reference_fixture_snapshot"], {})) if revision else {}
        ids = {as_dict(ref).get("asset_id") for ref in as_list(grading.get("asset_refs"))}
        if not ids and revision and revision["current_review_prompt_revision_id"]:
            prompt = self.one("SELECT presentation_snapshot FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (revision["current_review_prompt_revision_id"],))
            presentation = as_dict(loads(prompt["presentation_snapshot"], {})) if prompt else {}
            ids = {as_dict(ref).get("asset_id") for ref in as_list(presentation.get("asset_refs"))}
        for attempt in self.all("SELECT response_snapshot FROM Attempt WHERE question_id=? AND submission_state='submitted'", (question_id,)):
            ids.update(as_list(as_dict(loads(attempt["response_snapshot"], {})).get("response_assets")))
        return {asset_id for asset_id in ids if isinstance(asset_id, str)}

    def _sync_confirmed_asset_snapshots(self, draft, batch_id):
        """Sync current role/order presentation without changing history."""
        question_id = as_dict(draft).get("confirmed_question_id")
        if not question_id:
            return
        question = self.one("SELECT current_question_revision_id FROM Question WHERE question_id=?", (question_id,))
        revision = self.one("SELECT question_revision_id,current_review_prompt_revision_id,question_units,grading_reference_fixture_snapshot FROM QuestionRevision WHERE question_revision_id=?", (question["current_question_revision_id"],)) if question and question["current_question_revision_id"] else None
        if not revision:
            return
        assets = self.all("SELECT * FROM ImageAsset WHERE batch_id=? AND state='saved' AND path IS NOT NULL AND (role IS NULL OR role!='redo_process') ORDER BY ordinal,created_at,asset_id", (batch_id,))
        refs = [{"asset_id":a["asset_id"],"role":a["role"],"ordinal":a["ordinal"],"original_filename":a["original_filename"]} for a in assets]
        presentation_refs = [ref for ref in refs if ref.get("role") in ("question", "mixed")]
        grading = as_dict(loads(revision["grading_reference_fixture_snapshot"], {}))
        grading["asset_refs"] = refs
        grading["question_image_status"] = "已保存题面" if presentation_refs else "待补题面"
        units = loads(revision["question_units"], [])
        if isinstance(units, list) and units and isinstance(units[0], dict):
            units[0]["asset_ids"] = [ref["asset_id"] for ref in presentation_refs]
        self.conn.execute("UPDATE QuestionRevision SET grading_reference_fixture_snapshot=?,question_units=? WHERE question_revision_id=?", (dumps(grading), dumps(units), revision["question_revision_id"]))
        prompt = self.one("SELECT presentation_snapshot FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (revision["current_review_prompt_revision_id"],)) if revision["current_review_prompt_revision_id"] else None
        presentation = as_dict(loads(prompt["presentation_snapshot"], {})) if prompt else {}
        presentation["asset_refs"] = presentation_refs
        if prompt:
            self.conn.execute("UPDATE ReviewPromptRevision SET presentation_snapshot=? WHERE review_prompt_revision_id=?", (dumps(presentation), revision["current_review_prompt_revision_id"]))

    def _intake_detail(self, intake_id):
        row = self.one("SELECT i.*, b.subject_key, b.course_id, c.course_name, c.course_group FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id LEFT JOIN Course c ON c.course_id=b.course_id WHERE i.intake_id=?", (intake_id,))
        if not row:
            raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
        draft = loads(row["draft_fields"], {})
        confirmed = bool(as_dict(draft).get("confirmed_question_id"))
        confirmed_asset_ids = self._confirmed_asset_ids(draft)
        assets = [self._asset_dict(r) for r in self.all("SELECT * FROM ImageAsset WHERE batch_id=? ORDER BY ordinal, created_at, asset_id", (row["batch_id"],))]
        for asset in assets:
            asset["locked"] = confirmed and asset.get("state") == "saved" and (not confirmed_asset_ids or asset["asset_id"] in confirmed_asset_ids)
        result = dict(row)
        result["draft_fields"] = loads(result.get("draft_fields"), {})
        result["assets"] = assets
        result["batch_id"] = row["batch_id"]
        result.update(self._intake_status(row, result["draft_fields"], len(assets)))
        result["field_sources"] = self._draft_field_sources(result["draft_fields"])
        return result

    def _save_uploads(self, intake_id, files, subject_key=None, course_id=None, allow_redo=False):
        files = files or []
        created = self.clock()
        batch_id = None
        if intake_id:
            existing = self.one("SELECT batch_id FROM IntakeItem WHERE intake_id=?", (intake_id,))
            if not existing:
                raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
            batch_id = existing["batch_id"]
        else:
            if subject_key not in SUBJECT_KEYS and subject_key not in (None, ""):
                raise DomainError("invalid_subject", "subject_key must be math, english, politics, professional, or empty")
            if course_id and not self._course(course_id):
                raise DomainError("invalid_course", "course_id not found", {"course_id": course_id})
            if not subject_key and course_id:
                course = self._course(course_id)
                subject_key = course["subject_key"] if course else None
            batch_id, intake_id = uid("batch"), uid("intake")
            self.conn.execute("INSERT INTO CaptureBatch(batch_id,subject_key,course_id,created_at) VALUES(?,?,?,?)", (batch_id, subject_key or None, course_id or None, created))
            self.conn.execute("INSERT INTO IntakeItem(intake_id,batch_id,state,draft_fields,failure_note,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (intake_id, batch_id, "raw", "{}", None, created, created))
        existing_count = self.one("SELECT COALESCE(MAX(ordinal),0) FROM ImageAsset WHERE batch_id=?", (batch_id,))[0]
        failures = []
        objects_dir = ROOT / "objects"
        objects_dir.mkdir(parents=True, exist_ok=True)
        for offset, file in enumerate(files, 1):
            ordinal = existing_count + offset
            filename = (file.get("filename") or "image").strip() or "image"
            mime = file.get("mime") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            asset_id = uid("asset")
            # Ordinary intake uploads never create redo_process assets. The
            # dedicated redo path opts in explicitly for its internal files.
            requested_role = file.get("role")
            role = requested_role if requested_role in IMAGE_ROLES and (allow_redo or requested_role != "redo_process") else None
            try:
                data = file.get("data") or b""
                if not data:
                    raise ValueError("empty upload")
                suffix = Path(filename).suffix[:12]
                target = objects_dir / f"{asset_id}{suffix}"
                target.write_bytes(data)
                rel_path = str(target.relative_to(ROOT))
                self.conn.execute("INSERT INTO ImageAsset(asset_id,batch_id,original_filename,mime,ordinal,path,role,state,failure_note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (asset_id, batch_id, filename, mime, ordinal, rel_path, role, "saved", None, created))
            except Exception as error:
                failures.append(f"{filename}: {error}")
                self.conn.execute("INSERT INTO ImageAsset(asset_id,batch_id,original_filename,mime,ordinal,path,role,state,failure_note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (asset_id, batch_id, filename, mime, ordinal, None, role, "incomplete", str(error), created))
        state = "incomplete" if failures else "saved"
        note = "; ".join(failures) if failures else None
        self.conn.execute("UPDATE IntakeItem SET state=?,failure_note=?,updated_at=? WHERE intake_id=?", (state, note, self.clock(), intake_id))
        if batch_id:
            intake = self.one("SELECT draft_fields FROM IntakeItem WHERE intake_id=?", (intake_id,))
            self._sync_confirmed_asset_snapshots(loads(intake["draft_fields"], {}) if intake else {}, batch_id)
        self.commit()
        return self._intake_detail(intake_id)

    def create_intake_batch(self, files, subject_key=None, course_id=None):
        self.begin()
        try:
            return self._save_uploads(None, files, subject_key, course_id)
        except Exception:
            self.rollback()
            raise

    def create_intake_candidate(self, payload):
        """Save a generated practice question as an editable intake draft."""
        payload = as_dict(payload)
        question_text = as_text(payload.get("question_text")).strip()
        if not question_text:
            raise DomainError("invalid_candidate", "question_text is required")
        subject_key = payload.get("subject_key") if payload.get("subject_key") in SUBJECT_KEYS else None
        intake = self.create_intake_batch([], subject_key, as_text(payload.get("course_id")).strip() or None)
        draft_fields = {
            "analysis_status": "draft",
            "resolution_kind": "model",
            "resolution_label": "相似练习候选",
            "question_text": question_text,
            "reference_answer": as_text(payload.get("reference_answer")).strip(),
            "answer_origin": "model",
            "field_sources": {
                "question_text": "模型候选",
                "reference_answer": "模型候选",
            },
            "candidate_origin": "similar_practice",
            "source_question_id": as_text(payload.get("source_question_id")).strip() or None,
            "course_id": as_text(payload.get("course_id")).strip() or None,
            "raw_analysis": as_text(payload.get("raw")),
        }
        self.begin()
        try:
            self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft_fields), self.clock(), intake["intake_id"]))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return self._intake_detail(intake["intake_id"])

    def append_intake_assets(self, intake_id, files, allow_redo=False):
        self.begin()
        try:
            return self._save_uploads(intake_id, files, allow_redo=allow_redo)
        except Exception:
            self.rollback()
            raise

    def _intake_status(self, row, draft, asset_count=None):
        analysis_status = as_text(draft.get("analysis_status"))
        if draft.get("confirmed_question_id"):
            status_key, status_label = "confirmed", "已确认"
        elif row["state"] == "incomplete" and not draft.get("candidate_origin"):
            status_key, status_label = "incomplete", "部分图片保存失败"
        elif analysis_status == "analyzing":
            status_key, status_label = "analyzing", "分析中"
        elif analysis_status == "failed":
            status_key, status_label = "failed", "需要重试"
        elif analysis_status == "draft":
            status_key, status_label = "draft", "草稿已生成"
        else:
            status_key, status_label = "saved", "已保存，待处理"
        fields = [
            key for key in (
                "question_text", "reference_answer", "subject_key", "chapter",
                "knowledge_point", "question_type", "error_type", "error_reason",
                "error_breakpoint", "correct_approach",
            ) if draft.get(key) not in (None, "")
        ]
        return {
            "status_key": status_key,
            "status_label": status_label,
            "failure_reason": as_text(draft.get("analysis_error")) or as_text(row["failure_note"]),
            "recognized_fields": fields,
            "can_retry": status_key in {"saved", "failed", "incomplete"},
            "can_edit": status_key != "confirmed",
            "can_confirm": status_key in {"draft", "saved", "failed", "incomplete"},
            **({"asset_count": asset_count} if asset_count is not None else {}),
        }

    def _draft_field_sources(self, draft):
        draft = as_dict(draft)
        sources = dict(draft.get("field_sources")) if isinstance(draft.get("field_sources"), dict) else {}
        answer_origin = {
            "matched": "匹配题目",
            "reference_image": "参考答案图/模型",
            "user_material": "用户资料候选",
            "question_bank": "题库导入",
            "model": "模型候选",
        }.get(as_text(draft.get("answer_origin")), "模型候选")
        for key in (
            "question_text", "reference_answer", "subject_key", "chapter",
            "knowledge_point", "question_type", "error_type", "error_reason",
            "error_breakpoint", "correct_approach",
        ):
            if draft.get(key) in (None, "") or sources.get(key):
                continue
            if key == "question_text":
                sources[key] = "原图/用户"
            elif key == "reference_answer":
                sources[key] = answer_origin
            elif key in {"subject_key", "chapter", "knowledge_point", "question_type", "error_type"} and draft.get("tag_candidates"):
                sources[key] = "资料候选"
            else:
                sources[key] = "模型候选"
        return sources

    def _answer_candidates(self, draft, batch_id, candidates):
        """Build a transparent answer-source ladder for user confirmation."""
        draft = as_dict(draft)
        result = []
        seen = set()

        def add(origin, label, answer, priority, **extra):
            answer = as_text(answer).strip()
            if not answer or answer in seen:
                return
            seen.add(answer)
            result.append({"origin": origin, "label": label, "answer": answer, "priority": priority, **extra})

        def source_answer(text):
            import re
            normalized = "\n".join(line.strip() for line in as_text(text).replace("\r\n", "\n").split("\n") if line.strip())
            if not normalized:
                return "", False, ""
            marker = re.search(r"(?im)^(?:参考答案|标准答案|答案|解答|解析|解题思路|解题过程)\s*[:：]\s*(.*)$", normalized)
            excerpt = normalized
            if marker:
                excerpt = normalized[marker.start(1):].strip()
                next_section = re.search(r"\n\s*(?:题面|题目|知识点|章节|解析|参考答案|标准答案|补充|说明|注意)\s*[:：]", excerpt)
                if next_section:
                    excerpt = excerpt[:next_section.start()].strip()
            truncated = len(excerpt) > 1200
            if truncated:
                excerpt = excerpt[:1200].rstrip() + "…（资料较长，请展开原文确认）"
            return excerpt, bool(marker or truncated or excerpt != normalized), normalized

        reference_assets = self.all("SELECT asset_id,role FROM ImageAsset WHERE batch_id=? AND state='saved'", (batch_id,))
        if any(row["role"] == "reference" for row in reference_assets):
            add("reference_image", "参考答案图（模型提取，需确认）", draft.get("reference_answer"), 1, asset_ids=[row["asset_id"] for row in reference_assets if row["role"] == "reference"])

        for candidate in candidates:
            kind = as_text(candidate.get("kind"))
            if kind == "source":
                answer, excerpted, source_text = source_answer(candidate.get("text"))
                extra = {"source_passage_id": candidate.get("source_passage_id"), "source_name": candidate.get("source_name"), "page_no": candidate.get("page_no"), "source_excerpted": excerpted}
                if excerpted:
                    extra["source_text"] = source_text
                add("user_material", "用户资料候选（需确认）", answer, 2, **extra)
            elif kind == "question_bank":
                add("question_bank", "已导入题库答案", as_dict(candidate.get("grading")).get("reference_answer"), 3, question_bank_item_id=candidate.get("question_bank_item_id"))
            elif kind == "question":
                add("matched", "已确认题目答案", as_dict(candidate.get("grading")).get("reference_answer"), 3, question_id=candidate.get("question_id"))

        if not any(item["origin"] == "reference_image" for item in result):
            add("model", "LLM 解答（需确认）", draft.get("reference_answer"), 4)
        result.sort(key=lambda item: (item["priority"], item["label"], item["answer"]))
        return result

    def list_intakes(self):
        rows = self.all("SELECT i.*, b.subject_key, b.course_id, c.course_name, c.course_group, (SELECT COUNT(*) FROM ImageAsset a WHERE a.batch_id=i.batch_id) AS asset_count FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id LEFT JOIN Course c ON c.course_id=b.course_id ORDER BY i.updated_at DESC, i.created_at DESC")
        return [{**dict(row), "draft_fields": (draft := loads(row["draft_fields"], {})), **self._intake_status(row, draft, row["asset_count"])} for row in rows]

    def patch_intake(self, intake_id, payload):
        payload = payload if isinstance(payload, dict) else {}
        self.begin()
        try:
            row = self.one("SELECT * FROM IntakeItem WHERE intake_id=?", (intake_id,))
            if not row:
                raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
            batch_id = row["batch_id"]
            if "subject_key" in payload:
                subject = payload.get("subject_key") or None
                if subject not in SUBJECT_KEYS and subject is not None:
                    raise DomainError("invalid_subject", "invalid subject_key")
                self.conn.execute("UPDATE CaptureBatch SET subject_key=? WHERE batch_id=?", (subject, batch_id))
            draft = loads(row["draft_fields"], {})
            confirmed = bool(as_dict(draft).get("confirmed_question_id"))
            confirmed_asset_ids = self._confirmed_asset_ids(draft)
            if isinstance(payload.get("draft_fields"), dict):
                incoming_fields = payload["draft_fields"]
                field_sources = dict(draft.get("field_sources")) if isinstance(draft.get("field_sources"), dict) else {}
                for key in incoming_fields:
                    if key in {"question_text", "reference_answer", "subject_key", "chapter", "knowledge_point", "question_type", "error_type", "error_reason", "error_breakpoint", "correct_approach"} and incoming_fields.get(key) != draft.get(key):
                        field_sources[key] = "用户修改"
                draft.update(incoming_fields)
                if field_sources:
                    draft["field_sources"] = field_sources
                if confirmed and "subject_key" in payload["draft_fields"]:
                    subject_value = payload["draft_fields"].get("subject_key") or None
                    if subject_value not in SUBJECT_KEYS:
                        subject_value = None
                    self.conn.execute("UPDATE CaptureBatch SET subject_key=? WHERE batch_id=?", (subject_value, batch_id))
            if "subject_key" in payload:
                # Keep the top-level subject edit and the intake draft in
                # sync so confirmed grading receives the same value.
                draft["subject_key"] = payload.get("subject_key") or None
            changed_assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
            # Apply deletions first, then use temporary negative ordinals so a
            # swap (1↔2) never trips the UNIQUE(batch_id, ordinal) constraint.
            normalized = []
            current_assets = self.all("SELECT asset_id,ordinal FROM ImageAsset WHERE batch_id=? ORDER BY ordinal,asset_id", (batch_id,))
            final_ordinals = {r["asset_id"]: r["ordinal"] for r in current_assets}
            for change in changed_assets:
                if not isinstance(change, dict):
                    continue
                asset_id = change.get("asset_id")
                asset = self.one("SELECT * FROM ImageAsset WHERE asset_id=? AND batch_id=?", (asset_id, batch_id))
                if not asset:
                    continue
                if change.get("delete"):
                    if confirmed and (not confirmed_asset_ids or asset_id in confirmed_asset_ids):
                        continue
                    if asset["path"]:
                        try: (ROOT / asset["path"]).unlink(missing_ok=True)
                        except OSError: pass
                    self.conn.execute("DELETE FROM ImageAsset WHERE asset_id=?", (asset_id,))
                    continue
                # Historical Attempt snapshots remain immutable, while the
                # current Question presentation/grading references can be
                # corrected after confirmation. Keep old redo assets' order
                # stable, but allow legacy data to be reclassified.
                if confirmed and asset["role"] == "redo_process" and change.get("role") not in {"question", "my_process", "reference", "mixed"}:
                    continue
                # Omitted role means ordinal-only editing and retains the
                # persisted role. An explicit empty string clears it.
                role = change.get("role") if "role" in change else asset["role"]
                if role == "redo_process" and asset["role"] != "redo_process":
                    role = asset["role"]
                ordinal = change.get("ordinal")
                if role is not None and role != "" and role not in IMAGE_ROLES:
                    raise DomainError("invalid_role", "invalid image role")
                if ordinal is not None:
                    try: ordinal = int(ordinal)
                    except (TypeError, ValueError): ordinal = asset["ordinal"]
                    if ordinal < 1: ordinal = asset["ordinal"]
                normalized.append((asset_id, role or None, ordinal if ordinal is not None else asset["ordinal"]))
                final_ordinals[asset_id] = ordinal if ordinal is not None else asset["ordinal"]
            if normalized:
                # Free every ordinal in the batch before assigning the final
                # order so a swap (1↔2) never trips the UNIQUE constraint.
                desired = dict(final_ordinals)
                ordered_ids = [r["asset_id"] for r in sorted(current_assets, key=lambda r: (desired.get(r["asset_id"], r["ordinal"]), r["ordinal"], r["asset_id"])) if self.one("SELECT asset_id FROM ImageAsset WHERE asset_id=?", (r["asset_id"],))]
                final_ordinals = {asset_id: index for index, asset_id in enumerate(ordered_ids, 1)}
                for index, asset_id in enumerate(final_ordinals, 1):
                    self.conn.execute("UPDATE ImageAsset SET ordinal=? WHERE asset_id=?", (-1000000-index, asset_id))
                for asset_id, role, _ordinal in normalized:
                    self.conn.execute("UPDATE ImageAsset SET role=? WHERE asset_id=?", (role, asset_id))
                for asset_id, ordinal in final_ordinals.items():
                    self.conn.execute("UPDATE ImageAsset SET ordinal=? WHERE asset_id=?", (ordinal, asset_id))
            saved_count = self.one("SELECT COUNT(*) FROM ImageAsset WHERE batch_id=? AND state='saved'", (batch_id,))[0]
            incomplete_count = self.one("SELECT COUNT(*) FROM ImageAsset WHERE batch_id=? AND state='incomplete'", (batch_id,))[0]
            next_state = "saved" if saved_count and not incomplete_count else "incomplete"
            self.conn.execute("UPDATE IntakeItem SET state=?,draft_fields=?,updated_at=? WHERE intake_id=?", (next_state, dumps(draft), self.clock(), intake_id))
            if confirmed:
                self._sync_confirmed_draft_fields(draft)
            self._sync_confirmed_asset_snapshots(draft, batch_id)
            self.commit()
            return self._intake_detail(intake_id)
        except Exception:
            self.rollback()
            raise

    def _sync_confirmed_draft_fields(self, draft):
        """Apply editable intake fields to the current confirmed revision.

        The intake remains the editing surface, while the current grading
        snapshot is updated in place for an already-confirmed question. Older
        revisions and Attempt snapshots are historical records and are never
        touched here. Empty values are intentionally written as empty so a
        user can clear a previous value without making the field mandatory.
        """
        question_id = as_dict(draft).get("confirmed_question_id")
        if not question_id:
            return
        question = self.one("SELECT current_question_revision_id FROM Question WHERE question_id=?", (question_id,))
        revision = self.one("SELECT question_revision_id,grading_reference_fixture_snapshot FROM QuestionRevision WHERE question_revision_id=? AND question_id=?", (question["current_question_revision_id"], question_id)) if question and question["current_question_revision_id"] else None
        if not revision:
            return
        grading = as_dict(loads(revision["grading_reference_fixture_snapshot"], {}))
        for key in ("reference_answer", "error_reason", "error_breakpoint", "correct_approach", "chapter", "knowledge_point", "question_type"):
            if key in draft:
                grading[key] = as_text(draft.get(key))
        if "subject_key" in draft:
            value = draft.get("subject_key")
            grading["subject_key"] = value if isinstance(value, str) and value in SUBJECT_KEYS else None
        if "knowledge_node_id" in draft:
            knowledge_node_id = as_text(draft.get("knowledge_node_id")).strip() or None
            knowledge_node = self.one("SELECT knowledge_node_id,course_id,name,confirmation_state FROM KnowledgeNode WHERE knowledge_node_id=? AND confirmation_state!='archived'", (knowledge_node_id,)) if knowledge_node_id else None
            if knowledge_node_id and not knowledge_node:
                raise DomainError("invalid_knowledge_node", "knowledge node not found", {"knowledge_node_id": knowledge_node_id})
            if knowledge_node and knowledge_node["course_id"] != grading.get("course_id"):
                raise DomainError("invalid_knowledge_node", "knowledge node is not in the question course", {"knowledge_node_id": knowledge_node_id})
            if knowledge_node and knowledge_node["confirmation_state"] == "candidate":
                self.conn.execute("UPDATE KnowledgeNode SET confirmation_state='confirmed',origin='user' WHERE knowledge_node_id=?", (knowledge_node_id,))
            grading["knowledge_node_id"] = knowledge_node_id
            if knowledge_node:
                grading["knowledge_point"] = knowledge_node["name"]
            self.conn.execute("DELETE FROM QuestionKnowledgeLink WHERE question_id=?", (question_id,))
            if knowledge_node:
                self.conn.execute("INSERT INTO QuestionKnowledgeLink(question_id,knowledge_node_id,origin,created_at) VALUES(?,?,?,?)", (question_id, knowledge_node_id, "user", self.clock()))
        self.conn.execute("UPDATE QuestionRevision SET grading_reference_fixture_snapshot=? WHERE question_revision_id=?", (dumps(grading), revision["question_revision_id"]))

    def media_asset(self, asset_id):
        row = self.one("SELECT * FROM ImageAsset WHERE asset_id=?", (asset_id,))
        if not row or not row["path"] or row["state"] != "saved":
            return None
        path = (ROOT / row["path"]).resolve()
        try: path.relative_to((ROOT / "objects").resolve())
        except ValueError: return None
        if not path.is_file(): return None
        return path, (row["mime"] or "application/octet-stream"), row["original_filename"]

    def _source_heading_candidates(self, artifact_id, course_id):
        """Turn explicit material headings into reviewable course candidates."""
        course_id = as_text(course_id).strip()
        if not course_id:
            return []
        import re
        heading_pattern = re.compile(r"^(?:第[一二三四五六七八九十百千万0-9]+[章节篇]|[0-9]+(?:\.[0-9]+)*\s+|[一二三四五六七八九十百千万]+、).{1,70}$")
        candidates = []
        seen = set()
        chapter_by_prefix = {}
        current_chapter = None

        def number_key(value):
            value = as_text(value)
            simple = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
            return simple.get(value, value)

        def add_candidate(chapter, point=None):
            chapter, point = as_text(chapter).strip(), as_text(point).strip()
            marker = (chapter, point)
            if (chapter or point) and marker not in seen:
                seen.add(marker)
                candidates.append({"chapter": chapter, "knowledge_point": point, "origin": "source_heading"})

        rows = self.all("SELECT text FROM SourcePassage WHERE source_artifact_id=? ORDER BY ordinal", (artifact_id,))
        for row in rows:
            for raw_line in as_text(row["text"]).replace("\r\n", "\n").split("\n"):
                line = re.sub(r"^[#>*\-\s]+", "", raw_line).strip()
                line = re.sub(r"[：:；;，,。．.]+$", "", line).strip()
                if not line or len(line) < 3 or len(line) > 80 or not (heading_pattern.match(line) or re.match(r"^(?:知识点|考点)\s*[:：]\s*.+$", line)):
                    continue
                chapter_match = re.match(r"^第(?P<number>[一二三四五六七八九十百千万0-9]+)[章节篇]", line)
                numeric_match = re.match(r"^(?P<number>[0-9]+(?:\.[0-9]+)*)\s+", line)
                point_match = re.match(r"^(?:知识点|考点)\s*[:：]\s*(?P<point>.+)$", line)
                if chapter_match:
                    current_chapter = line
                    chapter_by_prefix[number_key(chapter_match.group("number"))] = line
                    add_candidate(line)
                elif numeric_match:
                    prefix = numeric_match.group("number")
                    if "." not in prefix:
                        current_chapter = line
                        chapter_by_prefix[prefix] = line
                        add_candidate(line)
                    else:
                        parent = chapter_by_prefix.get(prefix.split(".", 1)[0]) or current_chapter
                        add_candidate(parent or line, None if parent is None else line)
                elif point_match and current_chapter:
                    add_candidate(current_chapter, point_match.group("point"))
                else:
                    current_chapter = line
                    add_candidate(line)
                if len(candidates) >= 50:
                    break
            if len(candidates) >= 50:
                break
        return self._ensure_knowledge_candidates(course_id, candidates)

    def enrich_source(self, artifact_id):
        self.begin()
        try:
            artifact = self.one("SELECT * FROM SourceArtifact WHERE source_artifact_id=?", (artifact_id,))
            if not artifact:
                raise DomainError("not_found", "source artifact not found", {"source_artifact_id": artifact_id})
            path = artifact["stored_path"]
            file_path = (ROOT / path) if isinstance(path, str) and not Path(path).is_absolute() else Path(path) if path else None

            def mark_error(message):
                payload = loads(artifact["raw_payload"], {})
                if not isinstance(payload, dict):
                    payload = {"raw_payload": payload}
                payload["parse_error"] = message
                self.conn.execute("UPDATE SourceArtifact SET raw_payload=?,parse_state='error' WHERE source_artifact_id=?", (dumps(payload), artifact_id))
                self.commit()
                return {"source_artifact_id": artifact_id, "parse_state": "error", "error": message, "source_passage_ids": []}

            is_pdf = isinstance(path, str) and path.lower().endswith(".pdf")
            is_docx = as_text(artifact["kind"]) == "docx" or (isinstance(path, str) and path.lower().endswith(".docx"))
            is_webpage = as_text(artifact["kind"]) == "webpage"
            is_image = as_text(artifact["kind"]).startswith("image") or (isinstance(path, str) and Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
            passages = []
            parse_warning = None
            if is_webpage:
                payload = loads(artifact["raw_payload"], {})
                source_url = as_text(as_dict(payload).get("source_url")).strip()
                if not source_url:
                    return mark_error("网页缺少 URL")
                try:
                    request = urllib.request.Request(source_url, headers={"User-Agent": "MineKBase/1.0"})
                    with urllib.request.urlopen(request, timeout=20) as response:
                        html_bytes = response.read()
                        charset = response.headers.get_content_charset() or "utf-8"
                    html = html_bytes.decode(charset, errors="replace")
                    target = ROOT / "objects" / "sources" / f"{artifact_id}.html"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(html_bytes)
                    self.conn.execute("UPDATE SourceArtifact SET stored_path=? WHERE source_artifact_id=?", (str(target.relative_to(ROOT)), artifact_id))
                    try:
                        import trafilatura
                        extracted = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
                    except Exception:
                        extracted = ""
                    if not extracted:
                        from html.parser import HTMLParser
                        class _PageText(HTMLParser):
                            def __init__(self):
                                super().__init__()
                                self.skip = 0
                                self.parts = []
                            def handle_starttag(self, tag, attrs):
                                if tag.lower() in {"script", "style", "noscript", "template"}: self.skip += 1
                            def handle_endtag(self, tag):
                                if tag.lower() in {"script", "style", "noscript", "template"} and self.skip: self.skip -= 1
                            def handle_data(self, data):
                                if not self.skip and data.strip(): self.parts.append(" ".join(data.split()))
                        parser = _PageText(); parser.feed(html)
                        extracted = "\n\n".join(parser.parts)
                    if not extracted.strip():
                        return mark_error("网页正文为空")
                    pieces = [piece.strip() for piece in extracted.replace("\r\n", "\n").split("\n\n") if piece.strip()]
                    passages = [(piece, None, None, {"parser": "webpage", "url": source_url}) for piece in pieces]
                    text_value = "\n\n".join(piece for piece, *_ in passages)
                except Exception as error:
                    payload = loads(artifact["raw_payload"], {})
                    if not isinstance(payload, dict): payload = {"raw_payload": payload}
                    payload["parse_error"] = str(error)
                    self.conn.execute("UPDATE SourceArtifact SET raw_payload=?,parse_state='unavailable' WHERE source_artifact_id=?", (dumps(payload), artifact_id))
                    self.commit()
                    return {"source_artifact_id": artifact_id, "parse_state": "unavailable", "error": str(error), "source_passage_ids": []}
            elif is_docx:
                try:
                    from docx import Document
                    document = Document(str(file_path or path))
                    for index, paragraph in enumerate(document.paragraphs, 1):
                        text = paragraph.text.strip()
                        if text:
                            passages.append((text, None, None, {"parser": "docx", "paragraph": index}))
                    for table_index, table in enumerate(document.tables, 1):
                        for row_index, row in enumerate(table.rows, 1):
                            for cell_index, cell in enumerate(row.cells, 1):
                                text = cell.text.strip()
                                if text:
                                    passages.append((text, None, None, {"parser": "docx", "table": table_index, "row": row_index, "cell": cell_index}))
                    if not passages: return mark_error("DOCX 没有可提取文本")
                    text_value = "\n\n".join(piece for piece, *_ in passages)
                except Exception as error:
                    payload = loads(artifact["raw_payload"], {})
                    if not isinstance(payload, dict): payload = {"raw_payload": payload}
                    payload["parse_error"] = str(error)
                    self.conn.execute("UPDATE SourceArtifact SET raw_payload=?,parse_state='unavailable' WHERE source_artifact_id=?", (dumps(payload), artifact_id))
                    self.commit()
                    return {"source_artifact_id": artifact_id, "parse_state": "unavailable", "error": str(error), "source_passage_ids": []}
            elif is_pdf:
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(file_path or path)
                    page_texts = []
                    page_errors = []
                    for page_no, page in enumerate(reader.pages, 1):
                        try:
                            page_texts.append((page_no, page.extract_text() or ""))
                        except Exception as error:
                            page_errors.append(f"page {page_no}: {error}")
                            page_texts.append((page_no, ""))
                    for page_no, page_text in page_texts:
                        page_pieces = [piece.strip() for piece in page_text.replace("\r\n", "\n").split("\n\n") if piece.strip()]
                        for piece in page_pieces:
                            passages.append((piece, page_no))
                    empty_pages = [page_no for page_no, page_text in page_texts if not page_text.strip()]
                    if empty_pages:
                        try:
                            from ocr_adapter import extract_document
                            ocr_rows = extract_document(file_path or path, "pdf", pages=empty_pages)
                            passages.extend((row.get("text", "").strip(), row.get("page_no"), row.get("bbox"), {"parser": "ocr", "engine": "paddleocr", "page_no": row.get("page_no"), "bbox": row.get("bbox")}) for row in ocr_rows if row.get("text", "").strip())
                        except Exception as error:
                            # Keep text-layer pages searchable even when the
                            # optional OCR/rasterizer cannot handle blank pages.
                            parse_warning = f"PDF OCR unavailable: {error}"
                    if not passages:
                        return mark_error("PDF 没有可提取文本，OCR 未返回内容")
                    passages.sort(key=lambda item: (item[1] is None, item[1] or 0))
                    text_value = "\n\n".join(piece for piece, *_ in passages)
                except Exception as error:
                    message = f"PDF text/OCR extraction failed: {error}"
                    payload = loads(artifact["raw_payload"], {})
                    if not isinstance(payload, dict): payload = {"raw_payload": payload}
                    payload["parse_error"] = message
                    self.conn.execute("UPDATE SourceArtifact SET raw_payload=?,parse_state='unavailable' WHERE source_artifact_id=?", (dumps(payload), artifact_id))
                    self.commit()
                    return {"source_artifact_id": artifact_id, "parse_state": "unavailable", "error": message, "source_passage_ids": []}
            elif is_image:
                try:
                    from ocr_adapter import extract_document
                    ocr_rows = extract_document(file_path or path, "image")
                    passages = [(row.get("text", "").strip(), row.get("page_no"), row.get("bbox"), {"parser": "ocr", "engine": "paddleocr", "bbox": row.get("bbox")}) for row in ocr_rows if row.get("text", "").strip()]
                    if not passages:
                        return mark_error("图片 OCR 未返回文本")
                    text_value = "\n\n".join(piece for piece, *_ in passages)
                except Exception as error:
                    payload = loads(artifact["raw_payload"], {})
                    if not isinstance(payload, dict): payload = {"raw_payload": payload}
                    payload["parse_error"] = str(error)
                    self.conn.execute("UPDATE SourceArtifact SET raw_payload=?,parse_state='unavailable' WHERE source_artifact_id=?", (dumps(payload), artifact_id))
                    self.commit()
                    return {"source_artifact_id": artifact_id, "parse_state": "unavailable", "error": str(error), "source_passage_ids": []}
            else:
                text_value = artifact["raw_text"]
                if not isinstance(text_value, str) or not text_value:
                    if file_path:
                        try:
                            text_value = file_path.read_text(encoding="utf-8")
                        except Exception as error:
                            return mark_error(str(error))
                if not isinstance(text_value, str) or not text_value:
                    return mark_error("no plain text available")
                passages = [(piece.strip(), None, None, {"parser": "text"}) for piece in text_value.replace("\r\n", "\n").split("\n\n") if piece.strip()]
                if not passages:
                    passages = [(text_value, None, None, {"parser": "text"})]
            existing = {
                row["ordinal"]: row
                for row in self.all(
                    "SELECT * FROM SourcePassage WHERE source_artifact_id=?",
                    (artifact_id,),
                )
            }
            linked_ids = {row["source_passage_id"] for row in self.all("SELECT source_passage_id FROM QuestionSourceLink WHERE source_passage_id IN (SELECT source_passage_id FROM SourcePassage WHERE source_artifact_id=?)", (artifact_id,))}
            for ordinal, old in existing.items():
                if ordinal > len(passages) and old["source_passage_id"] not in linked_ids:
                    self.conn.execute("DELETE FROM SourcePassage WHERE source_passage_id=?", (old["source_passage_id"],))
            passage_ids = []
            for ordinal, entry in enumerate(passages, 1):
                piece, page_no = entry[0], entry[1]
                bbox = entry[2] if len(entry) > 2 else None
                parser_locator = entry[3] if len(entry) > 3 else {"parser": "pypdf" if is_pdf else "text"}
                current = existing.get(ordinal)
                passage_id = current["source_passage_id"] if current else uid("passage")
                passage_ids.append(passage_id)
                locator = {"ordinal": ordinal, **as_dict(parser_locator)}
                if page_no is not None:
                    locator["page_no"] = page_no
                if current and current["source_passage_id"] in linked_ids:
                    # Historical linked evidence is immutable; keep its text
                    # and locator even when a later parse produces different
                    # content at the same ordinal.
                    continue
                if current:
                    self.conn.execute(
                        "UPDATE SourcePassage SET text=?,page_no=?,bbox=?,locator_json=? WHERE source_passage_id=?",
                        (piece, page_no, dumps(bbox) if bbox is not None else None, dumps(locator), passage_id),
                    )
                else:
                    self.conn.execute("INSERT INTO SourcePassage(source_passage_id,source_artifact_id,ordinal,text,page_no,bbox,locator_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (passage_id, artifact_id, ordinal, piece, page_no, dumps(bbox) if bbox is not None else None, dumps(locator), self.clock()))
            # Re-running enrichment must not remove historical passages: a
            # QuestionSourceLink may still cite a trailing row.
            if self.fts_available:
                self.conn.execute("DELETE FROM SourcePassageFTS WHERE source_artifact_id=?", (artifact_id,))
                self.conn.execute("INSERT INTO SourcePassageFTS(source_passage_id,source_artifact_id,text) SELECT source_passage_id,source_artifact_id,COALESCE(text,'') FROM SourcePassage WHERE source_artifact_id=? ORDER BY ordinal", (artifact_id,))
            if parse_warning:
                payload = loads(artifact["raw_payload"], {})
                if not isinstance(payload, dict): payload = {"raw_payload": payload}
                payload["parse_error"] = parse_warning
            else:
                payload = None
            self.conn.execute("UPDATE SourceArtifact SET raw_text=?,raw_payload=COALESCE(?,raw_payload),parse_state=? WHERE source_artifact_id=?", (text_value, dumps(payload) if payload is not None else None, "unavailable" if parse_warning else "ready", artifact_id))
            self.commit()
            result = {"source_artifact_id": artifact_id, "parse_state": "unavailable" if parse_warning else "ready", "source_passage_ids": passage_ids}
            if not parse_warning:
                result["knowledge_node_candidates"] = self._source_heading_candidates(artifact_id, artifact["course_id"])
            if parse_warning:
                result["error"] = parse_warning
            return result
        except Exception:
            self.rollback()
            raise

    def get_source(self, artifact_id):
        artifact = self.one("SELECT * FROM SourceArtifact WHERE source_artifact_id=?", (artifact_id,))
        if not artifact:
            return None
        return {"artifact": dict(artifact), "passages": [dict(row) for row in self.all("SELECT * FROM SourcePassage WHERE source_artifact_id=? ORDER BY ordinal", (artifact_id,))]}

    def link_question_source(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        question_id = payload.get("question_id")
        passage_id = payload.get("source_passage_id")
        relation = payload.get("relation") if isinstance(payload.get("relation"), str) and payload.get("relation") else "supports"
        origin = payload.get("origin") if isinstance(payload.get("origin"), str) and payload.get("origin") else "manual"
        self.begin()
        try:
            question = self.one("SELECT question_id FROM Question WHERE question_id=?", (question_id,))
            if not question:
                raise DomainError("not_found", "question not found", {"question_id": question_id})
            passage = self.one("SELECT source_passage_id FROM SourcePassage WHERE source_passage_id=?", (passage_id,))
            if not passage:
                raise DomainError("not_found", "source passage not found", {"source_passage_id": passage_id})
            existing = self.one("SELECT * FROM QuestionSourceLink WHERE question_id=? AND source_passage_id=? AND relation=?", (question_id, passage_id, relation))
            if existing:
                self.commit()
                return dict(existing)
            created = self.clock()
            self.conn.execute("INSERT INTO QuestionSourceLink(question_id,learning_objective_id,source_passage_id,relation,origin,created_at) VALUES(?,?,?,?,?,?)", (question_id, None, passage_id, relation, origin, created))
            row = self.one("SELECT * FROM QuestionSourceLink WHERE question_id=? AND source_passage_id=? AND relation=?", (question_id, passage_id, relation))
            self.commit()
            return dict(row)
        except Exception:
            self.rollback()
            raise

    def get_question_sources(self, question_id):
        question = self.one("SELECT question_id FROM Question WHERE question_id=?", (question_id,))
        if not question:
            raise DomainError("not_found", "question not found", {"question_id": question_id})
        rows = self.all(
            "SELECT l.question_id,l.source_passage_id,p.source_artifact_id,p.text,p.page_no,p.bbox,p.locator_json,l.relation,l.origin,l.created_at "
            "FROM QuestionSourceLink l JOIN SourcePassage p ON p.source_passage_id=l.source_passage_id "
            "WHERE l.question_id=? ORDER BY p.source_artifact_id,p.ordinal,p.source_passage_id,l.relation",
            (question_id,),
        )
        return [dict(row) for row in rows]

    def _model_slot_payload(self, slot):
        """Return one effective provider config without exposing API keys."""
        prefix = "LLM" if slot == "primary" else "LLM_FALLBACK"
        env_protocol, env_base, env_key, env_model = self._provider_config_env(prefix)
        row = self.one("SELECT protocol,base_url,api_key,model,updated_at FROM ModelEndpoint WHERE slot=?", (slot,))
        if row:
            protocol = row["protocol"] if row["protocol"] is not None else env_protocol
            base_url = row["base_url"] or env_base
            api_key = row["api_key"] if row["api_key"] else env_key
            model = row["model"] or env_model
            updated_at = row["updated_at"]
            configured = bool(api_key)
        else:
            protocol, base_url, api_key, model = env_protocol, env_base, env_key, env_model
            updated_at = None
            configured = bool(api_key)
        return {"protocol": protocol or "openai_chat", "base_url": base_url or "", "model": model or "", "api_key_configured": configured, "updated_at": updated_at}

    def get_model_settings(self):
        return {"primary": self._model_slot_payload("primary"), "fallback": self._model_slot_payload("fallback")}

    def patch_model_settings(self, payload):
        payload = as_dict(payload)
        # Accept the natural {primary:{...},fallback:{...}} shape while also
        # tolerating a single-slot payload from a small client form.
        slots = {slot: as_dict(payload.get(slot)) for slot in ("primary", "fallback") if isinstance(payload.get(slot), dict)}
        if not slots and any(key in payload for key in ("slot", "protocol", "base_url", "api_key", "model")):
            slot = payload.get("slot") if payload.get("slot") in ("primary", "fallback") else "primary"
            slots = {slot: payload}
        if not slots:
            return self.get_model_settings()
        self.begin()
        try:
            for slot, values in slots.items():
                old = self.one("SELECT protocol,base_url,api_key,model FROM ModelEndpoint WHERE slot=?", (slot,))
                current = dict(old) if old else {}
                prefix = "LLM" if slot == "primary" else "LLM_FALLBACK"
                env_protocol, env_base, env_key, env_model = self._provider_config_env(prefix)
                protocol = values.get("protocol", current.get("protocol", env_protocol))
                if protocol not in {"openai_chat", "openai_responses", "anthropic_messages"}:
                    protocol = current.get("protocol") or env_protocol or "openai_chat"
                base_url = values.get("base_url", current.get("base_url", env_base))
                model = values.get("model", current.get("model", env_model))
                # An empty API key means “leave the current key alone”; this
                # avoids erasing a configured secret when a password input is
                # intentionally left blank.
                submitted_key = values.get("api_key")
                if isinstance(submitted_key, str) and submitted_key:
                    api_key = submitted_key
                else:
                    api_key = current.get("api_key") if current.get("api_key") is not None else (env_key or None)
                updated = self.clock()
                self.conn.execute(
                    "INSERT INTO ModelEndpoint(slot,protocol,base_url,api_key,model,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(slot) DO UPDATE SET protocol=excluded.protocol,base_url=excluded.base_url,api_key=excluded.api_key,model=excluded.model,updated_at=excluded.updated_at",
                    (slot, protocol, base_url or None, api_key, model or None, updated),
                )
            self.commit()
        except Exception:
            self.rollback()
            raise
        return self.get_model_settings()

    def list_sources(self, limit=30):
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 30
        rows = self.all("SELECT * FROM SourceArtifact ORDER BY created_at DESC,source_artifact_id DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            item = dict(row)
            payload = as_dict(loads(row["raw_payload"], {}))
            item["parse_error"] = as_text(payload.get("parse_error") or payload.get("save_error"))
            item["source_url"] = as_text(payload.get("source_url"))
            passages = self.all("SELECT source_passage_id,ordinal,text,page_no,locator_json FROM SourcePassage WHERE source_artifact_id=? ORDER BY ordinal", (row["source_artifact_id"],))
            item["passage_count"] = len(passages)
            item["passages"] = [dict(passage) for passage in passages]
            result.append(item)
        return result

    def compose_retrieval_query(self, draft=None, query=None, max_length=240):
        """Build the one lexical query shared by all retrieval entry points."""
        source = as_dict(draft)
        values = [
            source.get("question_text"),
            source.get("chapter"),
            source.get("knowledge_point"),
            source.get("question_type"),
            source.get("reference_answer"),
        ]
        if query and not values[0]:
            values[0] = query
        parts, seen = [], set()
        import re
        for value in values:
            text = re.sub(r"\s+", " ", as_text(value)).strip()
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            parts.append(text)
        return " ".join(parts)[:max(1, int(max_length))]

    def retrieve(self, query, primary_subject=None, related_subjects=None, limit=8):
        """Single FTS/LIKE retriever used by search, resolve and answering."""
        import re
        query = as_text(query).strip()
        if not query:
            return []
        primary_subject = primary_subject if primary_subject in SUBJECT_KEYS else None
        related = [s for s in as_list(related_subjects) if s in SUBJECT_KEYS and s != primary_subject]
        terms = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", query)
        chunks = []
        for term in terms:
            if re.fullmatch(r"[\u4e00-\u9fff]+", term):
                chunks.extend(term[i:i + 3] for i in range(max(1, len(term) - 2)))
            else:
                chunks.append(term)
        chunks = list(dict.fromkeys(chunks))[:24]
        subject_clause = ""
        subject_args = []
        if primary_subject:
            allowed_subjects = [primary_subject] + related
            subject_clause = " AND (a.subject_key IS NULL OR a.subject_key IN (" + ",".join("?" for _ in allowed_subjects) + "))"
            subject_args.extend(allowed_subjects)
        fts_rows = []
        if chunks and self.fts_available:
            match = " OR ".join('"' + token.replace('"', '""') + '"' for token in chunks)
            try:
                fts_rows = self.all(
                    "SELECT p.source_passage_id,p.source_artifact_id,p.text,p.page_no,p.bbox,p.locator_json,a.source_name,a.subject_key,bm25(SourcePassageFTS) AS score "
                    "FROM SourcePassage p JOIN SourcePassageFTS f ON f.source_passage_id=p.source_passage_id "
                    "JOIN SourceArtifact a ON a.source_artifact_id=p.source_artifact_id WHERE SourcePassageFTS MATCH ?" + subject_clause + " ORDER BY score LIMIT ?",
                    (match, *subject_args, max(limit * 6, 24)),
                )
            except sqlite3.OperationalError:
                fts_rows = []
        like_terms = []
        for term in terms:
            if re.fullmatch(r"[\u4e00-\u9fff]+", term):
                like_terms.extend(term[i:i + 2] for i in range(max(1, len(term) - 1)))
            elif len(term) >= 2:
                like_terms.append(term)
        like_terms = list(dict.fromkeys(like_terms + [query]))[:12]
        clauses = " OR ".join("(COALESCE(p.text,'') LIKE ? OR COALESCE(a.source_name,'') LIKE ?)" for _ in like_terms)
        like_rows = self.all(
            "SELECT p.source_passage_id,p.source_artifact_id,p.text,p.page_no,p.bbox,p.locator_json,a.source_name,a.subject_key,NULL AS score "
            f"FROM SourcePassage p JOIN SourceArtifact a ON a.source_artifact_id=p.source_artifact_id WHERE ({clauses})" + subject_clause + " ORDER BY p.source_artifact_id,p.ordinal",
            tuple(value for term in (like_terms or [query]) for value in (f"%{term}%", f"%{term}%")) + tuple(subject_args),
        ) if clauses else []
        rows = [*fts_rows, *like_rows]
        allowed = {primary_subject, None, *related} if primary_subject else None
        result, seen = [], set()
        for row in rows:
            item = dict(row); subject = item.get("subject_key")
            if allowed is not None and subject not in allowed:
                continue
            item["locator"] = loads(item.get("locator_json"), {}) or item.get("locator_json")
            item["score"] = item.get("score")
            item["_subject_rank"] = 0 if primary_subject and subject == primary_subject else 1 if subject is None else 2
            key = item.get("source_passage_id")
            if key in seen:
                continue
            seen.add(key); result.append(item)
        result.sort(key=lambda item: (item.pop("_subject_rank", 1), item.get("score") is None, item.get("score") if item.get("score") is not None else 0, item.get("source_artifact_id") or ""))
        return result[:max(1, int(limit))]

    def _light_cross_subjects(self, primary_subject):
        """Return the only implicit cross-subject fallback allowed in F1."""
        if primary_subject == "math":
            return ["professional"]
        if primary_subject == "professional":
            return ["math"]
        return []

    def _tag_candidates(self, draft, retrieved, primary_subject=None):
        """Expose coarse, editable labels without introducing a tag store."""
        current = {
            "subject_key": draft.get("subject_key") or primary_subject,
            "chapter": draft.get("chapter") or None,
            "knowledge_point": draft.get("knowledge_point") or None,
            "question_type": draft.get("question_type") or None,
        }
        candidates = []
        if any(value not in (None, "") for value in current.values()):
            candidates.append(current)
        seen = {dumps(item) for item in candidates}
        for item in retrieved or []:
            subject = item.get("subject_key")
            if subject not in SUBJECT_KEYS:
                continue
            candidate = {"subject_key": subject, "chapter": None, "knowledge_point": None, "question_type": None}
            marker = dumps(candidate)
            if marker not in seen:
                seen.add(marker)
                candidates.append(candidate)
        return candidates

    def search_sources(self, query):
        return self.retrieve(query)

    def _llm_chat(self, messages):
        prompt_parts = []
        for message in messages if isinstance(messages, list) else []:
            content = message.get("content") if isinstance(message, dict) else ""
            if isinstance(content, str):
                prompt_parts.append(content)
            elif isinstance(content, list):
                prompt_parts.extend(as_text(as_dict(part).get("text")) for part in content if isinstance(part, dict))
        raw, provider_label, errors = self._invoke_with_fallback("\n\n".join(part for part in prompt_parts if part), [])
        if raw:
            return raw, provider_label, True, None
        return "", provider_label, False, "；".join(errors) or "provider unavailable"

    def _provider_config_env(self, prefix="LLM"):
        protocol = os.environ.get(f"{prefix}_PROTOCOL", "openai_chat").strip() or "openai_chat"
        base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip()
        api_key = os.environ.get(f"{prefix}_API_KEY", "").strip()
        model = os.environ.get(f"{prefix}_MODEL", "").strip()
        return protocol, base_url, api_key, model

    def _provider_config(self, prefix="LLM"):
        """Resolve saved WebUI settings first, then environment variables."""
        slot = "primary" if prefix == "LLM" else "fallback"
        env = self._provider_config_env(prefix)
        row = self.one("SELECT protocol,base_url,api_key,model FROM ModelEndpoint WHERE slot=?", (slot,))
        if not row:
            return env
        protocol = row["protocol"] if row["protocol"] is not None else env[0]
        base_url = row["base_url"] or env[1]
        api_key = row["api_key"] if row["api_key"] else env[2]
        model = row["model"] or env[3]
        return protocol or "openai_chat", base_url or "", api_key or "", model or ""

    def _provider_text(self, body, protocol):
        if protocol == "openai_chat":
            choices = body.get("choices") if isinstance(body, dict) else None
            content = as_dict(choices[0]).get("message", {}).get("content") if isinstance(choices, list) and choices else ""
        elif protocol == "openai_responses":
            content = body.get("output_text", "") if isinstance(body, dict) else ""
            if not content and isinstance(body, dict):
                parts = []
                for output in body.get("output", []) or []:
                    for item in as_dict(output).get("content", []) or []:
                        if isinstance(item, dict) and isinstance(item.get("text"), str): parts.append(item["text"])
                content = "".join(parts)
        else:
            content = ""
            if isinstance(body, dict):
                content = "".join(as_text(as_dict(item).get("text")) for item in (body.get("content") or []))
        if isinstance(content, list):
            content = "".join(as_text(as_dict(part).get("text")) for part in content)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("provider response did not contain text content")
        return content.strip()

    def _call_provider(self, protocol, base_url, api_key, model, prompt, image_parts):
        if protocol not in {"openai_chat", "openai_responses", "anthropic_messages"}:
            raise ValueError(f"unsupported provider protocol: {protocol}")
        if not base_url or not model:
            raise ValueError("provider base URL or model is not configured")
        encoded = []
        for mime, data in image_parts:
            import base64
            encoded.append((mime, base64.b64encode(data).decode("ascii")))
        if protocol == "openai_chat":
            content = [{"type": "text", "text": prompt}]
            content += [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}} for mime, data in encoded]
            payload = {"model": model, "messages": [{"role": "user", "content": content}]}
            endpoint = base_url.rstrip("/")
            endpoint = endpoint if endpoint.endswith("/chat/completions") else endpoint + ("/chat/completions" if endpoint.endswith("/v1") else "/v1/chat/completions")
            headers = {"Content-Type": "application/json"}
            if api_key: headers["Authorization"] = f"Bearer {api_key}"
        elif protocol == "openai_responses":
            content = [{"type": "input_text", "text": prompt}]
            content += [{"type": "input_image", "image_url": f"data:{mime};base64,{data}"} for mime, data in encoded]
            payload = {"model": model, "input": [{"role": "user", "content": content}]}
            endpoint = base_url.rstrip("/")
            endpoint = endpoint if endpoint.endswith("/responses") else endpoint + ("/responses" if endpoint.endswith("/v1") else "/v1/responses")
            headers = {"Content-Type": "application/json"}
            if api_key: headers["Authorization"] = f"Bearer {api_key}"
        else:
            content = [{"type": "text", "text": prompt}]
            content += [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for mime, data in encoded]
            payload = {"model": model, "max_tokens": 4096, "messages": [{"role": "user", "content": content}]}
            endpoint = base_url.rstrip("/")
            endpoint = endpoint if endpoint.endswith("/messages") else endpoint + ("/messages" if endpoint.endswith("/v1") else "/v1/messages")
            headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            if api_key: headers["x-api-key"] = api_key
        request = urllib.request.Request(endpoint, data=dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=60) as response:
            body = loads(response.read(), {})
        return self._provider_text(body, protocol)

    def _invoke_with_fallback(self, prompt, image_parts):
        errors = []
        for label, config in (("LLM", self._provider_config("LLM")), ("LLM_FALLBACK", self._provider_config("LLM_FALLBACK"))):
            try:
                return self._call_provider(*config, prompt, image_parts), label, errors
            except Exception as error:
                errors.append(f"{label}: {error}")
        return "", None, errors

    def _extract_analysis(self, raw, subject_hint=None):
        import re
        fields = {"question_text":"", "reference_answer":"", "subject_key": subject_hint if subject_hint in SUBJECT_KEYS else None, "chapter":"", "knowledge_point":"", "question_type":"", "error_type":"", "error_reason":"", "error_breakpoint":"", "correct_approach":""}
        aliases = {
            "question_text": r"(?:题面|题目(?:要求)?|question(?:_text)?)",
            "reference_answer": r"(?:答案|标准答案|模型答案|参考答案|reference[_ ]?answer)",
            "subject_key": r"(?:科目|subject[_ ]?key)", "chapter": r"(?:章节|chapter)", "knowledge_point": r"(?:知识点|knowledge[_ ]?point)", "question_type": r"(?:题型|question[_ ]?type)",
            "error_type": r"(?:错误类型|error[_ ]?type)", "error_reason": r"(?:做错原因|错误原因|error[_ ]?reason)", "error_breakpoint": r"(?:解题断点|首次偏离|首次出错步骤|error[_ ]?breakpoint)", "correct_approach": r"(?:正确思路|correct[_ ]?approach)"
        }
        # Models commonly use either "标题：内容" or a Markdown heading followed
        # by content on the next line. Parse line starts only, and keep raw_analysis
        # as the lossless fallback when a section is still ambiguous.
        heading_prefix = r"(?:\*\*)?(?:#{1,6}[ \t]*|[-*][ \t]+)?(?:\*\*)?"
        subject_aliases = {
            "math": ("math", "数学", "高数"),
            "english": ("english", "英语"),
            "politics": ("politics", "政治"),
            "professional": ("professional", "专业课"),
        }
        label_union = "|".join(aliases.values())
        section_re = re.compile(
            rf"(?im)^[ \t]*{heading_prefix}(?P<label>{label_union})(?:\*\*)?[ \t]*(?:(?:[:：])[ \t]*(?P<inline>[^\n]*))?[ \t]*$"
        )
        matches = list(section_re.finditer(raw or ""))
        label_to_key = {}
        for key, label in aliases.items():
            # Matching is done against the same aliases without Markdown syntax.
            label_to_key[key] = re.compile(rf"^(?:{label})$", re.I)
        for index, match in enumerate(matches):
            label = match.group("label") or ""
            key = next((candidate for candidate, matcher in label_to_key.items() if matcher.match(label)), None)
            if not key:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw or "")
            continuation = (raw or "")[match.end():end].strip()
            inline = (match.group("inline") or "").strip()
            value = "\n".join(part for part in (inline, continuation) if part).strip()
            if key == "subject_key":
                lowered = value.lower()
                value = next((subject for subject, names in subject_aliases.items() if any(name in lowered for name in names)), None)
                if value is None and subject_hint in SUBJECT_KEYS:
                    value = subject_hint
            if key == "error_type":
                lowered = value.lower()
                aliases = {
                    "knowledge_gap": ("知识点不会", "知识点不熟", "知识缺失", "不会"),
                    "method_selection": ("方法选择错误", "方法错误", "思路错误", "选法错误"),
                    "derivation_calculation": ("推导或计算出错", "推导错误", "计算错误", "计算出错", "运算错误"),
                }
                value = next((error_type for error_type, names in aliases.items() if any(name in lowered for name in names)), "")
            fields[key] = value
        return fields

    def analyze_intake(self, intake_id):
        row = self.one("SELECT i.*,b.subject_key,b.course_id FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id WHERE i.intake_id=?", (intake_id,))
        if not row: raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
        draft = loads(row["draft_fields"], {})
        confirmed = bool(as_dict(draft).get("confirmed_question_id"))
        asset_query = "SELECT * FROM ImageAsset WHERE batch_id=? AND state='saved'"
        if confirmed:
            asset_query += " AND (role IS NULL OR role!='redo_process')"
        asset_query += " ORDER BY ordinal,created_at,asset_id"
        assets = self.all(asset_query, (row["batch_id"],))
        if not assets: raise DomainError("no_assets", "intake has no saved images", {"intake_id": intake_id})
        draft.update({"analysis_status": "analyzing", "analysis_error": ""})
        self.begin(); self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft), self.clock(), intake_id)); self.commit()
        prompt_lines = ["你是错题分析助手。请阅读按顺序提供的图片，输出普通文本或 Markdown，并尽量使用以下标题：题面、标准答案、科目、章节、知识点、题型、错误类型、做错原因、首次出错步骤、正确思路。", "区分题目要求、我的解题步骤、标准答案/模型答案；指出我具体在哪一步开始偏离。错误类型只能从三项中选择：知识点不会、方法选择错误、推导或计算出错；如果无法确定就留空并写待确认。", "不确定的内容标记为‘待确认’，不要猜测填满字段；分类字段可以为空。"]
        image_parts = []
        for asset in assets:
            path = ROOT / asset["path"]
            try: data = path.read_bytes()
            except OSError as error: image_parts = []; break
            role = asset["role"] or "未指定"
            prompt_lines.append(f"图片 {asset['ordinal']}，角色：{role}{'（可能的标准答案）' if role == 'reference' else ''}。")
            image_parts.append((asset["mime"] or "application/octet-stream", data))
        if not image_parts:
            error = "无法读取已保存图片"
            draft.update({"analysis_status":"failed", "analysis_error":error})
            self.begin(); self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft), self.clock(), intake_id)); self.commit()
            return self._intake_detail(intake_id)
        if not any(a["role"] == "reference" for a in assets): prompt_lines.append("没有标准答案图片，请根据题面和我的解题过程推导答案，并把推导结果标为模型答案。")
        prompt = "\n".join(prompt_lines)
        raw, provider_label, errors = self._invoke_with_fallback(prompt, image_parts)
        self.begin()
        try:
            if raw:
                try:
                    fields = self._extract_analysis(raw, row["subject_key"])
                except Exception as error:
                    # Keep the model response available for manual editing instead
                    # of leaving the intake stuck in an "analyzing" state.
                    draft.update({"raw_analysis": raw, "analysis_status": "failed", "analysis_error": f"分析结果解析失败：{error}"})
                else:
                    # A retry fills blank candidates but does not silently erase a
                    # value the user already edited. Clear a field explicitly when
                    # a fresh model value is desired.
                    for key, value in fields.items():
                        if value not in (None, "") and not draft.get(key):
                            draft[key] = value
                    draft.update({"raw_analysis": raw, "analysis_status": "draft", "analysis_error": ""})
            else:
                draft.update({"analysis_status":"failed", "analysis_error":"；".join(errors)[:500] or "provider unavailable"})
            self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft), self.clock(), intake_id)); self.commit()
        except Exception:
            self.rollback(); raise
        # Feed the coarse question understanding into the shared retriever,
        # then optionally ask the same model to re-check its draft against
        # real, server-resolved passages.
        primary_subject = draft.get("subject_key") if draft.get("subject_key") in SUBJECT_KEYS else row["subject_key"]
        query_text = self.compose_retrieval_query(draft, query=as_text(draft.get("raw_analysis")))
        related = self._light_cross_subjects(primary_subject)
        retrieved = self.retrieve(query_text, primary_subject=primary_subject, related_subjects=related, limit=8) if query_text else []
        source_refs = [{"source_passage_id": item.get("source_passage_id"), "source_artifact_id": item.get("source_artifact_id"), "source_name": item.get("source_name"), "page_no": item.get("page_no"), "bbox": item.get("bbox"), "locator": item.get("locator")} for item in retrieved]
        context = "\n\n".join(f"[S{index}] {item.get('source_name') or item.get('source_artifact_id') or '资料'} / 第{item.get('page_no') or '--'}页 / {item.get('locator') or '--'}\n{item.get('text') or ''}" for index, item in enumerate(retrieved, 1))
        draft.update({"retrieval_query": query_text, "retrieval_status": "ready" if retrieved else "empty", "retrieved_context": context, "source_refs": source_refs, "tag_candidates": self._tag_candidates(draft, retrieved, primary_subject), "grounding_label": "有资料依据" if retrieved else "未定位资料"})
        second_raw = ""
        second_errors = []
        if retrieved:
            second_prompt = "\n".join([
                "请基于原始题目图片和以下资料候选，输出可编辑的错题分析草稿。先区分题面、我的解题步骤、标准答案/模型答案；逐步还原过程并指出首次偏离步骤。",
                "给出错误原因、正确思路和科目/章节/知识点/题型候选。不确定内容标记‘待确认’，不要凭空制造出处或为了填满字段而猜测。资料编号只能作为参考。",
                "资料候选：", context,
            ])
            second_raw, _second_provider, second_errors = self._invoke_with_fallback(second_prompt, image_parts)
        if second_raw:
            try:
                fields = self._extract_analysis(second_raw, primary_subject)
                for key, value in fields.items():
                    if value not in (None, "") and not draft.get(key):
                        draft[key] = value
                draft.update({"raw_analysis": second_raw, "analysis_status": "draft", "analysis_error": "", "grounding_error": "", "grounding_label": "有资料依据", "tag_candidates": self._tag_candidates(draft, retrieved, primary_subject)})
            except Exception as error:
                draft.update({"analysis_status": "failed", "analysis_error": f"资料复核解析失败：{error}"})
        elif retrieved and second_errors:
            # The first image-only draft remains editable; grounding failure is
            # a separate enrichment status and never replaces that draft.
            draft.update({"grounding_error": "；".join(second_errors)[:500], "grounding_label": "资料复核失败"})
        self.begin()
        try:
            self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft), self.clock(), intake_id)); self.commit()
        except Exception:
            self.rollback(); raise
        return self._intake_detail(intake_id)

    def _subject_context(self, subject_key, created, course_id=None):
        """Return a course context for the confirmed subject, creating only
        the minimal local release/objective needed by the existing ledger."""
        subject_key = subject_key if subject_key in SUBJECT_KEYS else None
        course = self._course(course_id)
        course_key = course_id or subject_key or "capture_unspecified"
        release = self.one("SELECT * FROM CoursePackRelease WHERE course_key=?", (course_key,))
        if not release:
            release_id, objective_id = uid("release"), uid("lo")
            self.conn.execute("INSERT INTO CoursePackRelease(release_id,course_key,created_at) VALUES(?,?,?)", (release_id, course_key, created))
            label = course["course_name"] if course else (subject_key or "未指定")
            self.conn.execute("INSERT INTO LearningObjective(learning_objective_id,course_pack_release_id,name,description,observable_criteria,created_at) VALUES(?,?,?,?,?,?)", (objective_id, release_id, f"{label}待补充学习目标", "", dumps(["待补充"]), created))
            return release_id, objective_id
        objective = self.one("SELECT learning_objective_id FROM LearningObjective WHERE course_pack_release_id=? ORDER BY created_at LIMIT 1", (release["release_id"],))
        return release["release_id"], objective["learning_objective_id"] if objective else None

    def resolve_intake(self, intake_id):
        row = self.one("SELECT i.*,b.subject_key,b.course_id FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id WHERE i.intake_id=?", (intake_id,))
        if not row:
            raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
        draft = loads(row["draft_fields"], {})
        query = self.compose_retrieval_query(draft, query=as_text(draft.get("raw_analysis")))
        candidates, seen = [], set()
        if query:
            primary_subject = draft.get("subject_key") if draft.get("subject_key") in SUBJECT_KEYS else row["subject_key"]
            fts_rows = self.retrieve(query, primary_subject=primary_subject, related_subjects=self._light_cross_subjects(primary_subject), limit=8)
            for item in fts_rows[:8]:
                key = item.get("source_passage_id")
                if key in seen: continue
                seen.add(key)
                candidates.append({"kind":"source", "source_passage_id":key, "source_artifact_id":item.get("source_artifact_id"), "source_name":item.get("source_name"), "subject_key":item.get("subject_key"), "text":item.get("text"), "page_no":item.get("page_no"), "locator":item.get("locator") or item.get("locator_json")})
            import re
            fragments = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", query)
            fragments = list(dict.fromkeys(fragments))[:8] or [query[:30]]
            like_clauses = " OR ".join("qr.question_units LIKE ? OR qr.grading_reference_fixture_snapshot LIKE ?" for _ in fragments)
            like_args = tuple(arg for fragment in fragments for arg in (f"%{fragment}%", f"%{fragment}%"))
            course_clause = " AND qr.grading_reference_fixture_snapshot LIKE ?" if row["course_id"] else ""
            course_args = [f'%"course_id":"{row["course_id"]}"%'] if row["course_id"] else []
            qrows = self.all("SELECT q.question_id,qr.question_revision_id,qr.question_units,qr.grading_reference_fixture_snapshot FROM Question q JOIN QuestionRevision qr ON qr.question_revision_id=q.current_question_revision_id WHERE qr.revision_state='confirmed' AND (" + like_clauses + ")" + course_clause + " ORDER BY q.created_at DESC LIMIT 8", [*like_args, *course_args])
            for item in qrows:
                key = item["question_id"]
                if key in seen: continue
                seen.add(key)
                units = loads(item["question_units"], {})
                grading = loads(item["grading_reference_fixture_snapshot"], {})
                question_text = units[0].get("question_text", "") if isinstance(units, list) and units and isinstance(units[0], dict) else units.get("question_text", "") if isinstance(units, dict) else ""
                candidates.append({"kind":"question", "question_id":key, "question_revision_id":item["question_revision_id"], "question_text":question_text, "grading":{"course_id":as_text(grading.get("course_id")), "reference_answer":as_text(grading.get("reference_answer"))}})
            bank_filters = {"course_id": row["course_id"], "limit": 30} if row["course_id"] else {}
            bank_rows = self.list_question_bank(bank_filters) if bank_filters else []
            fragments = [fragment.lower() for fragment in fragments if len(fragment) >= 2]
            for item in bank_rows:
                haystack = " ".join(as_text(item.get(key)) for key in ("question_text", "chapter", "question_type", "difficulty")).lower()
                metadata_match = any([
                    as_text(draft.get("chapter")).strip() and as_text(item.get("chapter")).strip() == as_text(draft.get("chapter")).strip(),
                    as_text(draft.get("question_type")).strip() and as_text(item.get("question_type")).strip() == as_text(draft.get("question_type")).strip(),
                    as_text(draft.get("knowledge_node_id")).strip() and as_text(item.get("knowledge_node_id")).strip() == as_text(draft.get("knowledge_node_id")).strip(),
                ])
                text_match = any(fragment in haystack for fragment in fragments[:6])
                if not metadata_match and not text_match and len([c for c in candidates if c.get("kind") == "question_bank"]) >= 8:
                    continue
                candidates.append({"kind": "question_bank", "question_bank_item_id": item["question_bank_item_id"], "question_text": item.get("question_text"), "grading": {"reference_answer": item.get("reference_answer")}, "chapter": item.get("chapter"), "question_type": item.get("question_type"), "difficulty": item.get("difficulty"), "explanation": item.get("explanation"), "source": item.get("source"), "year": item.get("year")})
                if len([c for c in candidates if c.get("kind") == "question_bank"]) >= 8:
                    break
        subject = draft.get("subject_key") if draft.get("subject_key") in SUBJECT_KEYS else row["subject_key"]
        source_refs = [{k:c.get(k) for k in ("source_passage_id","source_artifact_id","locator") if c.get(k)} for c in candidates if c.get("kind")=="source"]
        tag_candidates = self._tag_candidates(draft, [c for c in candidates if c.get("kind") == "source"], subject)
        has_question = any(c.get("kind")=="question" for c in candidates)
        has_source = any(c.get("kind")=="source" for c in candidates)
        answer_candidates = self._answer_candidates(draft, row["batch_id"], candidates)
        knowledge_candidates = self._ensure_knowledge_candidates(row["course_id"], tag_candidates)
        manual_answer = as_text(as_dict(draft.get("field_sources")).get("reference_answer")).startswith(("用户修改", "用户选择"))
        current_origin = as_text(draft.get("answer_origin"))
        answer_origin = current_origin if manual_answer and current_origin else (answer_candidates[0]["origin"] if answer_candidates else "model")
        draft.update({"retrieval_query": query, "resolution_kind":"matched" if has_question else "model", "resolution_label":"匹配题目" if has_question else "资料参考" if has_source else "待补充", "match_candidates":candidates, "source_refs":source_refs, "tag_candidates":tag_candidates, "knowledge_node_candidates":knowledge_candidates, "answer_candidates":answer_candidates, "answer_conflict":len(answer_candidates) > 1, "answer_origin":answer_origin})
        self.begin()
        try:
            self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft), self.clock(), intake_id)); self.commit()
        except Exception:
            self.rollback(); raise
        return self._intake_detail(intake_id)

    def confirm_intake(self, intake_id):
        row = self.one("SELECT i.*,b.subject_key,b.course_id FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id WHERE i.intake_id=?", (intake_id,))
        if not row:
            raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
        draft = loads(row["draft_fields"], {})
        existing_id = draft.get("confirmed_question_id")
        if existing_id:
            return {"can_confirm": True, "confirmed": True, "question_id": existing_id, "review_task_id": draft.get("confirmed_review_task_id"), "intake": self._intake_detail(intake_id)}
        required = {key: as_text(draft.get(key)).strip() or "待补充" for key in ("reference_answer","error_reason","error_breakpoint")}
        created = self.clock(); subject = draft.get("subject_key") if draft.get("subject_key") in SUBJECT_KEYS else row["subject_key"]
        self.begin()
        try:
            release_id, objective_id = self._subject_context(subject, created, row["course_id"])
            question_id, revision_id, prompt_id = uid("q"), uid("qr"), uid("qpr")
            assets = self.all("SELECT * FROM ImageAsset WHERE batch_id=? AND state='saved' AND path IS NOT NULL ORDER BY ordinal,created_at,asset_id", (row["batch_id"],))
            question_assets = [a for a in assets if a["role"] in ("question", "mixed")]
            asset_refs = [{"asset_id":a["asset_id"],"role":a["role"],"ordinal":a["ordinal"],"original_filename":a["original_filename"]} for a in assets]
            presentation_refs = [{"asset_id":a["asset_id"],"role":a["role"],"ordinal":a["ordinal"],"original_filename":a["original_filename"]} for a in question_assets]
            question_text = as_text(draft.get("question_text"))
            units = [{"unit_ref":"whole","label":"整题","question_text":question_text,"intake_id":intake_id,"asset_ids":[a["asset_id"] for a in question_assets]}]
            mapping = [{"objective_ref":"obj-1","learning_objective_id":objective_id,"role":"measured","question_unit_refs":["whole"],"origin_kind":"intake_confirm","captured_at":created}]
            selected_source_ids = draft.get("selected_source_passage_ids") if isinstance(draft.get("selected_source_passage_ids"), list) else []
            knowledge_node_id = as_text(draft.get("knowledge_node_id")).strip() or None
            knowledge_node = self.one("SELECT knowledge_node_id,course_id,name,confirmation_state FROM KnowledgeNode WHERE knowledge_node_id=? AND confirmation_state!='archived'", (knowledge_node_id,)) if knowledge_node_id else None
            if knowledge_node_id and not knowledge_node:
                raise DomainError("invalid_knowledge_node", "knowledge node not found", {"knowledge_node_id": knowledge_node_id})
            if knowledge_node and knowledge_node["course_id"] != row["course_id"]:
                raise DomainError("invalid_knowledge_node", "knowledge node is not in the selected course", {"knowledge_node_id": knowledge_node_id})
            if knowledge_node and knowledge_node["confirmation_state"] == "candidate":
                self.conn.execute("UPDATE KnowledgeNode SET confirmation_state='confirmed',origin='user' WHERE knowledge_node_id=?", (knowledge_node_id,))
            grading = {"reference_answer":required["reference_answer"],"error_type":as_text(draft.get("error_type")),"error_reason":required["error_reason"],"error_breakpoint":required["error_breakpoint"],"correct_approach":as_text(draft.get("correct_approach")),"subject_key":subject,"course_id":row["course_id"],"knowledge_node_id":knowledge_node_id,"knowledge_point":knowledge_node["name"] if knowledge_node else draft.get("knowledge_point"),"chapter":draft.get("chapter"),"question_type":draft.get("question_type"),"intake_id":intake_id,"asset_refs":asset_refs,"selected_question_id":draft.get("selected_question_id"),"selected_source_passage_ids":selected_source_ids,"answer_origin":as_text(draft.get("answer_origin")) or "model","answer_candidates":as_list(draft.get("answer_candidates")),"question_image_status":"已保存题面" if question_assets else "待补题面"}
            presentation = {"schema_version":SNAPSHOT,"content":question_text,"blocks":[{"block_ref":"question","kind":"text","content":question_text,"leakage_state":"clean"}],"asset_refs":presentation_refs}
            self.conn.execute("INSERT INTO Question(question_id,course_pack_release_id,current_question_revision_id,lifecycle_state,created_at) VALUES(?,?,?,?,?)", (question_id,release_id,None,"active",created))
            self.conn.execute("INSERT INTO QuestionRevision(question_revision_id,question_id,revision_no,revision_state,supersedes_revision_id,current_review_prompt_revision_id,question_units,objective_mapping_snapshot,grading_reference_fixture_snapshot,help_content_fixture_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (revision_id,question_id,1,"confirmed",None,None,dumps(units),dumps(mapping),dumps(grading),dumps([]),created))
            self.conn.execute("INSERT INTO ReviewPromptRevision(review_prompt_revision_id,question_id,question_revision_id,revision_no,presentation_snapshot,unresolved_critical_ambiguities,leakage_state,revision_state,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (prompt_id,question_id,revision_id,1,dumps(presentation),dumps([]),"clean","ready",created))
            self.conn.execute("UPDATE Question SET current_question_revision_id=? WHERE question_id=?", (revision_id,question_id)); self.conn.execute("UPDATE QuestionRevision SET current_review_prompt_revision_id=? WHERE question_revision_id=?", (prompt_id,revision_id))
            if knowledge_node:
                self.conn.execute("INSERT INTO QuestionKnowledgeLink(question_id,knowledge_node_id,origin,created_at) VALUES(?,?,?,?)", (question_id, knowledge_node["knowledge_node_id"], "user", created))
            process_assets = [a["asset_id"] for a in assets if a["role"] in ("my_process","mixed")]
            response = {"schema_version":SNAPSHOT,"response_text":"","response_selections":[],"response_assets":process_assets,"completion_claim":"unknown","external_help_reported":False,"intake_id":intake_id,"asset_refs":[ref for ref in asset_refs if ref["asset_id"] in process_assets]}
            attempt_id = uid("attempt")
            self.conn.execute("INSERT INTO Attempt(attempt_id,question_id,question_revision_id,review_session_id,origin_kind,submission_state,submitted_at,completion_claim,initial_debt_claim,initial_debt_claim_basis,initial_debt_claim_captured_at,response_snapshot,assistance_state,external_help_reported,submit_event_ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id,question_id,revision_id,None,"initial","submitted",created,"unknown","uncertain","intake_confirm",created,dumps(response),"none_observed",0,None,created))
            task = self._schedule_in_tx(question_id,revision_id,prompt_id,{"trigger_ref":uid("trigger"),"trigger_kind":"intake_confirm","trigger_reason_kind":"initial_error","intake_id":intake_id,"captured_at":created},created)
            self.conn.execute("UPDATE ReviewTask SET due_at=? WHERE review_task_id=?", (add_days(created,3),task["review_task_id"]))
            for passage_id in selected_source_ids:
                if self.one("SELECT source_passage_id FROM SourcePassage WHERE source_passage_id=?", (passage_id,)):
                    self.conn.execute("INSERT OR IGNORE INTO QuestionSourceLink(question_id,learning_objective_id,source_passage_id,relation,origin,created_at) VALUES(?,?,?,?,?,?)", (question_id, objective_id, passage_id, "supports", "intake_resolve", created))
            draft.update({"confirmed_question_id":question_id,"confirmed_revision_id":revision_id,"confirmed_attempt_id":attempt_id,"confirmed_review_task_id":task["review_task_id"],"confirmed_at":created})
            self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft),created,intake_id)); self.commit()
            return {"can_confirm":True,"confirmed":True,"question_id":question_id,"attempt_id":attempt_id,"review_task_id":task["review_task_id"],"due_at":add_days(created,3),"intake":self._intake_detail(intake_id)}
        except sqlite3.IntegrityError:
            self.rollback()
            existing = self.one("SELECT draft_fields FROM IntakeItem WHERE intake_id=?", (intake_id,))
            saved = loads(existing["draft_fields"], {}) if existing else {}
            if saved.get("confirmed_question_id"):
                return {"can_confirm":True,"confirmed":True,"question_id":saved["confirmed_question_id"],"review_task_id":saved.get("confirmed_review_task_id"),"intake":self._intake_detail(intake_id)}
            raise
        except Exception:
            self.rollback(); raise

    def _task_summary(self, task):
        if not task:
            return None
        return {key: task[key] for key in ("review_task_id", "reason_kind", "review_round", "due_at", "status")}

    def _question_assets_from_refs(self, refs):
        """Return review-safe question assets, preferring explicitly tagged
        question images and falling back to mixed images when that is all the
        intake has.  A mixed fallback remains visible but is labelled so the
        UI can tell the user it may contain process marks.
        """
        refs = [as_dict(ref) for ref in as_list(refs)]
        tagged = [ref for ref in refs if ref.get("role") == "question"]
        mixed = [ref for ref in refs if ref.get("role") == "mixed"]
        selected = tagged + mixed if tagged else mixed
        assets = []
        for ref in selected:
            asset = self.one("SELECT * FROM ImageAsset WHERE asset_id=? AND state='saved' AND path IS NOT NULL", (ref.get("asset_id"),))
            if not asset:
                continue
            item = self._asset_dict(asset)
            item["review_role"] = ref.get("role") or asset["role"]
            assets.append(item)
        if mixed and assets:
            status = "题面+过程"
        elif tagged and assets:
            status = "已保存题面"
        else:
            status = "待补题面"
        return assets, status

    def _reference_assets_from_refs(self, refs):
        assets = []
        for ref in [as_dict(ref) for ref in as_list(refs) if as_dict(ref).get("role") == "reference"]:
            asset = self.one("SELECT * FROM ImageAsset WHERE asset_id=? AND state='saved' AND path IS NOT NULL", (ref.get("asset_id"),))
            if asset:
                item = self._asset_dict(asset)
                item["review_role"] = "reference"
                assets.append(item)
        return assets

    def _process_assets_from_refs(self, refs):
        assets = []
        for ref in [as_dict(ref) for ref in as_list(refs) if as_dict(ref).get("role") == "my_process"]:
            asset = self.one("SELECT * FROM ImageAsset WHERE asset_id=? AND state='saved' AND path IS NOT NULL", (ref.get("asset_id"),))
            if asset:
                item = self._asset_dict(asset)
                item["review_role"] = "my_process"
                assets.append(item)
        return assets

    def _wrong_dto(self, question_id):
        question = self.one("SELECT * FROM Question WHERE question_id=?", (question_id,))
        if not question: return None
        revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=? AND revision_state='confirmed'", (question["current_question_revision_id"],))
        if not revision: return None
        grading = loads(revision["grading_reference_fixture_snapshot"], {})
        knowledge_nodes = [dict(row) for row in self.all("SELECT n.knowledge_node_id,n.course_id,n.parent_id,n.name,n.aliases,n.confirmation_state,l.origin FROM QuestionKnowledgeLink l JOIN KnowledgeNode n ON n.knowledge_node_id=l.knowledge_node_id WHERE l.question_id=? ORDER BY n.name", (question_id,))]
        course = self._course(grading.get("course_id"))
        if not grading.get("intake_id"): return None
        data_origin = as_text(grading.get("data_origin")) or "real"
        prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (revision["current_review_prompt_revision_id"],))
        if not prompt:
            prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE question_revision_id=? AND revision_state='ready' ORDER BY revision_no DESC LIMIT 1", (revision["question_revision_id"],))
        presentation = loads(prompt["presentation_snapshot"], {}) if prompt else {}
        refs = presentation.get("asset_refs") if isinstance(presentation.get("asset_refs"), list) else []
        assets, question_image_status = self._question_assets_from_refs(refs)
        reference_assets = self._reference_assets_from_refs(as_dict(grading).get("asset_refs"))
        process_assets = self._process_assets_from_refs(as_dict(grading).get("asset_refs"))
        intake_row = self.one("SELECT batch_id FROM IntakeItem WHERE intake_id=?", (grading.get("intake_id"),))
        editable_assets = []
        if intake_row:
            editable_assets = [self._asset_dict(row) for row in self.all("SELECT * FROM ImageAsset WHERE batch_id=? AND state='saved' AND path IS NOT NULL ORDER BY ordinal,created_at,asset_id", (intake_row["batch_id"],))]
        task = self.one("SELECT * FROM ReviewTask WHERE question_id=? AND status='open' ORDER BY due_at LIMIT 1", (question_id,))
        sources = self.get_question_sources(question_id)
        redo_draft = None
        latest_redo = self.one("SELECT attempt_id FROM Attempt WHERE question_id=? AND origin_kind='review' AND submission_state='submitted' AND response_snapshot LIKE '%comparison_draft%' ORDER BY submitted_at DESC LIMIT 1", (question_id,))
        active_session = self.one("SELECT * FROM ReviewSession WHERE question_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1", (question_id,))
        if active_session:
            raw_draft = as_dict(loads(active_session["draft_payload_snapshot"], {}))
            draft_attempt_id = raw_draft.get("draft_attempt_id")
            if draft_attempt_id:
                draft_assets = self._redo_assets(grading.get("intake_id"), as_list(raw_draft.get("response_assets")))
                redo_draft = {"attempt_id": draft_attempt_id, "review_session_id": active_session["review_session_id"], "review_task_id": active_session["review_task_id"], "response_text": as_text(raw_draft.get("response_text")), "response_assets": [a["asset_id"] for a in draft_assets], "assets": draft_assets}
        return {"question_id":question_id,"question":dict(question),"question_revision":dict(revision),"question_text":as_text((presentation.get("content") if isinstance(presentation,dict) else "")),"presentation":presentation,"assets":assets,"process_assets":process_assets,"editable_assets":editable_assets,"intake_id":grading.get("intake_id"),"course_id":grading.get("course_id"),"course_name":course["course_name"] if course else None,"course_group":course["course_group"] if course else None,"reference_assets":reference_assets,"question_image_status":question_image_status,"data_origin":data_origin,"display_label":as_text(grading.get("display_label")) or "真实题目","grading":grading,"knowledge_nodes":knowledge_nodes,"sources":sources,"answers":self.get_question_answers(question_id),"next_due_at":task["due_at"] if task else None,"review_task_id":task["review_task_id"] if task else None,"next_task":self._task_summary(task),"redo_draft":redo_draft,"latest_redo_attempt_id":latest_redo["attempt_id"] if latest_redo else None}

    def list_wrong_questions(self, filters=None):
        """Return confirmed intake-backed questions, optionally filtered by tags.

        Filtering deliberately stays a small exact-match projection over the
        existing grading snapshot. Empty fields remain visible when no filter
        is supplied, and a missing/empty field simply does not match a
        non-empty filter value.
        """
        filters = {key: as_text(value).strip() for key, value in as_dict(filters).items()
                   if key in {"course_id", "subject_key", "chapter", "knowledge_point", "question_type"}
                   and as_text(value).strip()}
        rows = self.all("SELECT q.question_id FROM Question q JOIN QuestionRevision qr ON qr.question_revision_id=q.current_question_revision_id WHERE qr.revision_state='confirmed' AND qr.grading_reference_fixture_snapshot LIKE '%intake_id%'")
        result = []
        for row in rows:
            item = self._wrong_dto(row["question_id"])
            if item:
                grading = as_dict(item.get("grading"))
                if any(as_text(grading.get(key)).strip() != value for key, value in filters.items()):
                    continue
                result.append({"question_id":item["question_id"],"question_text":item["question_text"],"subject_key":grading.get("subject_key"),"course_id":item.get("course_id"),"course_name":item.get("course_name"),"chapter":grading.get("chapter"),"knowledge_point":grading.get("knowledge_point"),"question_type":grading.get("question_type"),"error_type":grading.get("error_type"),"asset_count":len(item["assets"]),"reference_answer":grading.get("reference_answer"),"error_reason":grading.get("error_reason"),"next_due_at":item["next_due_at"],"data_origin":item.get("data_origin"),"display_label":item.get("display_label")})
        return result

    def get_wrong_question(self, question_id):
        item = self._wrong_dto(question_id)
        if not item: raise DomainError("not_found", "wrong question not found", {"question_id":question_id})
        return item

    def _redo_session(self, question_id, review_task_id=None):
        """Return an active ReviewSession used as the editable redo draft."""
        if not review_task_id:
            task = self.one("SELECT review_task_id FROM ReviewTask WHERE question_id=? AND status='open' ORDER BY due_at, review_round, created_at LIMIT 1", (question_id,))
            review_task_id = task["review_task_id"] if task else None
        if not review_task_id:
            raise DomainError("not_found", "review task not found", {"question_id": question_id})
        task = self.one("SELECT question_id,status FROM ReviewTask WHERE review_task_id=?", (review_task_id,))
        if not task or task["question_id"] != question_id:
            raise DomainError("not_found", "review task not found", {"review_task_id": review_task_id})
        if task["status"] != "open":
            raise DomainError("review_closed", "该回测已提交，请使用尚未提交的回测草稿", {"review_task_id": review_task_id})
        session = self.start_review(review_task_id)
        if session.get("status") == "submitted":
            raise DomainError("review_closed", "该回测已提交，请使用尚未提交的回测草稿", {"review_task_id": review_task_id})
        return session

    def _redo_assets(self, intake_id, asset_ids):
        row = self.one("SELECT batch_id FROM IntakeItem WHERE intake_id=?", (intake_id,))
        if not row:
            return []
        wanted = [value for value in asset_ids if isinstance(value, str)]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        rows = self.all(f"SELECT * FROM ImageAsset WHERE batch_id=? AND role='redo_process' AND state='saved' AND path IS NOT NULL AND asset_id IN ({marks}) ORDER BY ordinal,created_at,asset_id", [row["batch_id"], *wanted])
        return [self._asset_dict(item) for item in rows]

    def redo_upload(self, question_id, files, payload=None):
        """Save redo images and an editable response in ReviewSession JSON."""
        payload = as_dict(payload)
        wrong = self.get_wrong_question(question_id)
        grading = as_dict(wrong.get("grading"))
        intake_id = grading.get("intake_id")
        if not intake_id:
            raise DomainError("not_found", "wrong question intake not found", {"question_id": question_id})
        session = self._redo_session(question_id, payload.get("review_task_id") or wrong.get("review_task_id"))
        existing_draft = as_dict(session.get("draft"))
        draft_attempt_id = existing_draft.get("draft_attempt_id") or uid("attempt")
        response_text = as_text(existing_draft.get("response_text"))
        if as_text(payload.get("response_text")).strip():
            response_text = as_text(payload.get("response_text"))
        # Force every uploaded file to the redo role. The ordinary intake
        # uploader still owns persistence and per-file failure handling.
        redo_files = [{**as_dict(item), "role": "redo_process"} for item in (files or [])]
        existing_asset_rows = self.all(
            "SELECT asset_id FROM ImageAsset WHERE batch_id=(SELECT batch_id FROM IntakeItem WHERE intake_id=?)",
            (intake_id,),
        )
        existing_asset_ids = {row["asset_id"] for row in existing_asset_rows}
        before_ids = as_list(existing_draft.get("response_assets"))
        if redo_files:
            detail = self.append_intake_assets(intake_id, redo_files, allow_redo=True)
            saved_new = [
                a["asset_id"] for a in detail.get("assets", [])
                if a.get("asset_id") not in existing_asset_ids
                and a.get("role") == "redo_process"
                and a.get("state") == "saved"
                and a.get("path")
            ]
        else:
            saved_new = []
        response_assets = []
        for asset_id in [*before_ids, *saved_new]:
            if asset_id not in response_assets:
                response_assets.append(asset_id)
        valid_assets = self._redo_assets(intake_id, response_assets)
        response_assets = [a["asset_id"] for a in valid_assets]
        draft = self._draft({"draft_attempt_id": draft_attempt_id, "draft_status": "draft", "response_text": response_text, "response_assets": response_assets, "completion_claim": existing_draft.get("completion_claim", "unknown"), "external_help_reported": False})
        self.begin()
        try:
            self.conn.execute("UPDATE ReviewSession SET draft_payload_snapshot=?,updated_at=?,status='active',ended_at=NULL WHERE review_session_id=?", (dumps(draft), self.clock(), session["review_session_id"]))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return {"attempt_id": draft_attempt_id, "question_id": question_id, "review_task_id": session["review_task_id"], "review_session_id": session["review_session_id"], "assets": valid_assets, "draft": draft}

    def patch_attempt_draft(self, attempt_id, payload):
        """Edit only the redo draft; the initial Attempt is never touched."""
        payload = as_dict(payload)
        stored_attempt = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (attempt_id,))
        if stored_attempt:
            current_response = as_dict(loads(stored_attempt["response_snapshot"], {}))
            comparison = current_response.get("comparison_draft")
            if not isinstance(comparison, dict):
                raise DomainError("review_closed", "该 Attempt 不是可编辑的比较草稿", {"attempt_id": attempt_id})
            for key in ("error_reason", "error_breakpoint", "correct_approach"):
                if key in payload:
                    comparison[key] = as_text(payload.get(key))
            comparison["status"] = comparison.get("status") or "draft"
            current_response["comparison_draft"] = comparison
            self.begin()
            try:
                self.conn.execute("UPDATE Attempt SET response_snapshot=? WHERE attempt_id=?", (dumps(current_response), attempt_id))
                self.commit()
            except Exception:
                self.rollback()
                raise
            return self._attempt_dto(self.one("SELECT * FROM Attempt WHERE attempt_id=?", (attempt_id,)))
        session = self.one("SELECT * FROM ReviewSession WHERE review_session_id=?", (attempt_id,))
        if not session:
            session = self.one("SELECT * FROM ReviewSession WHERE draft_payload_snapshot LIKE ? ORDER BY updated_at DESC LIMIT 1", (f'%"draft_attempt_id":"{attempt_id}"%',))
        if not session:
            raise DomainError("not_found", "redo draft not found", {"attempt_id": attempt_id})
        if session["status"] == "submitted":
            raise DomainError("review_closed", "该草稿已提交，不能覆盖正式 Attempt", {"attempt_id": attempt_id})
        draft = as_dict(loads(session["draft_payload_snapshot"], {}))
        if "response_text" in payload:
            draft["response_text"] = as_text(payload.get("response_text"))
        if isinstance(payload.get("response_assets"), list):
            draft["response_assets"] = payload.get("response_assets")
        intake_id = as_dict(self._wrong_dto(session["question_id"]).get("grading")).get("intake_id")
        valid_assets = self._redo_assets(intake_id, as_list(draft.get("response_assets"))) if intake_id else []
        draft["response_assets"] = [a["asset_id"] for a in valid_assets]
        draft["draft_attempt_id"] = as_text(draft.get("draft_attempt_id"), attempt_id)
        draft["draft_status"] = "draft"
        draft = self._draft(draft)
        self.begin()
        try:
            self.conn.execute("UPDATE ReviewSession SET draft_payload_snapshot=?,updated_at=?,status='active',ended_at=NULL WHERE review_session_id=?", (dumps(draft), self.clock(), session["review_session_id"]))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return {"attempt_id": draft["draft_attempt_id"], "question_id": session["question_id"], "review_task_id": session["review_task_id"], "review_session_id": session["review_session_id"], "assets": valid_assets, "draft": draft}

    def _attempt_asset_rows(self, asset_ids, roles=None):
        wanted = [value for value in as_list(asset_ids) if isinstance(value, str)]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        rows = self.all(f"SELECT * FROM ImageAsset WHERE asset_id IN ({marks}) AND state='saved' AND path IS NOT NULL", wanted)
        by_id = {row["asset_id"]: row for row in rows}
        # Historical snapshots are authoritative: current role and ordinal
        # edits may annotate an asset, but cannot hide or reorder it here.
        return [self._asset_dict(by_id[asset_id]) for asset_id in wanted if asset_id in by_id]

    def _attempt_dto(self, attempt):
        attempt = dict(attempt)
        response = as_dict(loads(attempt.get("response_snapshot"), {}))
        session = self.one("SELECT review_task_id FROM ReviewSession WHERE review_session_id=?", (attempt.get("review_session_id"),)) if attempt.get("review_session_id") else None
        initial = self.one("SELECT * FROM Attempt WHERE question_id=? AND origin_kind='initial' ORDER BY created_at,submitted_at LIMIT 1", (attempt["question_id"],))
        initial_response = as_dict(loads(initial["response_snapshot"], {})) if initial else {}
        revision = self.one("SELECT grading_reference_fixture_snapshot FROM QuestionRevision WHERE question_revision_id=?", (attempt["question_revision_id"],))
        grading = as_dict(loads(revision["grading_reference_fixture_snapshot"], {})) if revision else {}
        comparison = as_dict(response.get("comparison_draft"))
        reference_assets = self._reference_assets_from_refs(as_dict(grading).get("asset_refs"))
        next_task = self.one("SELECT * FROM ReviewTask WHERE question_id=? AND status='open' ORDER BY due_at, review_round, created_at LIMIT 1", (attempt["question_id"],))
        return {
            "attempt_id": attempt["attempt_id"], "question_id": attempt["question_id"],
            "review_task_id": session["review_task_id"] if session else None,
            "review_session_id": attempt.get("review_session_id"), "status": attempt.get("submission_state"),
            "submitted_at": attempt.get("submitted_at"),
            "response_text": as_text(response.get("response_text")),
            "response_assets": self._attempt_asset_rows(response.get("response_assets")),
            "initial_process": {"response_text": as_text(initial_response.get("response_text")), "response_assets": self._attempt_asset_rows(initial_response.get("response_assets"))},
            "reference_answer": as_text(grading.get("reference_answer")),
            "reference_assets": reference_assets,
            "comparison_draft": comparison,
            "next_task": self._task_summary(next_task),
        }

    def get_attempt(self, attempt_id):
        attempt = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (attempt_id,))
        if attempt:
            return self._attempt_dto(attempt)
        session = self.one("SELECT * FROM ReviewSession WHERE review_session_id=?", (attempt_id,))
        if not session:
            session = self.one("SELECT * FROM ReviewSession WHERE draft_payload_snapshot LIKE ? ORDER BY updated_at DESC LIMIT 1", (f'%"draft_attempt_id":"{attempt_id}"%',))
        if not session:
            raise DomainError("not_found", "attempt not found", {"attempt_id": attempt_id})
        draft = as_dict(loads(session["draft_payload_snapshot"], {}))
        intake_id = as_dict(self._wrong_dto(session["question_id"]).get("grading")).get("intake_id")
        assets = self._redo_assets(intake_id, as_list(draft.get("response_assets"))) if intake_id else []
        return {"attempt_id": as_text(draft.get("draft_attempt_id"), attempt_id), "question_id": session["question_id"], "review_task_id": session["review_task_id"], "review_session_id": session["review_session_id"], "status": "draft", "draft": {"response_text": as_text(draft.get("response_text")), "response_assets": [a["asset_id"] for a in assets], "assets": assets}}

    def _comparison_prompt(self, question_text, initial_text, redo_text, reference_answer, error_reason, error_breakpoint):
        return "\n".join([
            "你是回测比较助手。请比较初次解题过程和本次重新作答，不修改正式错题卡。",
            "区分题目要求、初次过程、本次过程、参考答案和已有错误诊断。指出本次是否修正了初次偏离，并说明依据。",
            "如果诊断不确定，请标记为‘待确认’，不要伪装成确定结论。只输出普通文本或 Markdown，可使用标题：错误原因、首次出错步骤、正确思路。",
            f"题面：{question_text or '（未提供）'}",
            f"初次过程：{initial_text or '（无文字过程，可能只有图片）'}",
            f"本次过程：{redo_text or '（无文字过程，可能只有图片）'}",
            f"参考答案：{reference_answer or '待补充'}",
            f"已有错误原因：{error_reason or '待补充'}",
            f"已有首次出错步骤：{error_breakpoint or '待补充'}",
        ])

    def _run_attempt_comparison(self, attempt_id):
        attempt = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (attempt_id,))
        if not attempt:
            return {"status": "failed", "comparison_error": "attempt not found", "raw_analysis": ""}
        response = as_dict(loads(attempt["response_snapshot"], {}))
        initial = self.one("SELECT * FROM Attempt WHERE question_id=? AND origin_kind='initial' ORDER BY created_at,submitted_at LIMIT 1", (attempt["question_id"],))
        initial_response = as_dict(loads(initial["response_snapshot"], {})) if initial else {}
        revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=?", (attempt["question_revision_id"],))
        grading = as_dict(loads(revision["grading_reference_fixture_snapshot"], {})) if revision else {}
        question = self.get_wrong_question(attempt["question_id"])
        question_text = question.get("question_text")
        image_parts = []
        question_assets = [
            as_dict(ref).get("asset_id")
            for ref in as_list(as_dict(question.get("presentation")).get("asset_refs"))
            if as_dict(ref).get("role") in ("question", "mixed")
        ]
        reference_assets = [
            as_dict(ref).get("asset_id")
            for ref in as_list(grading.get("asset_refs"))
            if as_dict(ref).get("role") == "reference"
        ]
        asset_ids = []
        for asset_id in [*question_assets, *as_list(initial_response.get("response_assets")), *as_list(response.get("response_assets")), *reference_assets]:
            if isinstance(asset_id, str) and asset_id not in asset_ids:
                asset_ids.append(asset_id)
        for asset_id in asset_ids:
            row = self.one("SELECT mime,path FROM ImageAsset WHERE asset_id=? AND state='saved' AND path IS NOT NULL", (asset_id,))
            if not row:
                continue
            try:
                image_parts.append((row["mime"] or "application/octet-stream", (ROOT / row["path"]).read_bytes()))
            except OSError:
                continue
        prompt = self._comparison_prompt(question_text, as_text(initial_response.get("response_text")), as_text(response.get("response_text")), as_text(grading.get("reference_answer")), as_text(grading.get("error_reason")), as_text(grading.get("error_breakpoint")))
        errors, raw, provider = [], "", None
        for label, config in [("LLM", self._provider_config("LLM")), ("LLM_FALLBACK", self._provider_config("LLM_FALLBACK"))]:
            try:
                raw = self._call_provider(*config, prompt, image_parts)
                provider = f"{label}:{config[0]}"
                break
            except Exception as error:
                errors.append(f"{label}: {error}")
        if not raw:
            return {"status": "failed", "raw_analysis": "", "comparison_error": "；".join(errors)[:500] or "provider unavailable", "reference_answer": as_text(grading.get("reference_answer")), "error_reason": as_text(grading.get("error_reason")), "error_breakpoint": as_text(grading.get("error_breakpoint")), "correct_approach": "", "provider": provider}
        fields = self._extract_analysis(raw, grading.get("subject_key"))
        return {"status": "draft", "raw_analysis": raw, "comparison_error": "", "reference_answer": as_text(grading.get("reference_answer")), "error_reason": as_text(fields.get("error_reason")) or as_text(grading.get("error_reason")), "error_breakpoint": as_text(fields.get("error_breakpoint")) or as_text(grading.get("error_breakpoint")), "correct_approach": as_text(fields.get("correct_approach")), "provider": provider}

    def submit_attempt(self, attempt_id, payload=None):
        payload = as_dict(payload)
        existing = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (attempt_id,))
        if existing:
            return self._attempt_dto(existing)
        session = self.one("SELECT * FROM ReviewSession WHERE review_session_id=?", (attempt_id,))
        if not session:
            session = self.one("SELECT * FROM ReviewSession WHERE draft_payload_snapshot LIKE ? ORDER BY updated_at DESC LIMIT 1", (f'%"draft_attempt_id":"{attempt_id}"%',))
        if not session:
            raise DomainError("not_found", "redo draft not found", {"attempt_id": attempt_id})
        if session["status"] == "submitted" and session["submitted_attempt_id"]:
            return self.get_attempt(session["submitted_attempt_id"])
        draft = self._draft(loads(session["draft_payload_snapshot"], {}))
        if "response_text" in payload:
            draft["response_text"] = as_text(payload.get("response_text"))
        if isinstance(payload.get("response_assets"), list):
            draft["response_assets"] = payload["response_assets"]
        draft_attempt_id = as_text(draft.get("draft_attempt_id"), attempt_id)
        draft["draft_attempt_id"] = draft_attempt_id
        response_assets = self._redo_assets(as_dict(self._wrong_dto(session["question_id"]).get("grading")).get("intake_id"), as_list(draft.get("response_assets")))
        draft["response_assets"] = [a["asset_id"] for a in response_assets]
        created = self.clock()
        response = {"schema_version": SNAPSHOT, "response_text": as_text(draft.get("response_text")), "response_selections": as_list(draft.get("response_selections")), "response_assets": draft["response_assets"], "completion_claim": draft.get("completion_claim", "unknown"), "external_help_reported": bool(draft.get("external_help_reported"))}
        self.begin()
        try:
            self.conn.execute("INSERT INTO Attempt(attempt_id,question_id,question_revision_id,review_session_id,origin_kind,submission_state,submitted_at,completion_claim,initial_debt_claim,initial_debt_claim_basis,initial_debt_claim_captured_at,response_snapshot,assistance_state,external_help_reported,submit_event_ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (draft_attempt_id, session["question_id"], session["question_revision_id"], session["review_session_id"], "review", "submitted", created, response["completion_claim"], None, None, None, dumps(response), "none_observed", int(response["external_help_reported"]), None, created))
            self.conn.execute("UPDATE ReviewSession SET status='submitted',submitted_attempt_id=?,ended_at=?,updated_at=?,draft_payload_snapshot=? WHERE review_session_id=?", (draft_attempt_id, created, created, dumps(draft), session["review_session_id"]))
            self.conn.execute("UPDATE ReviewTask SET status='completed',completed_by_attempt_id=? WHERE review_task_id=?", (draft_attempt_id, session["review_task_id"]))
            self._schedule_after_review_submit(session, draft_attempt_id, created)
            self.commit()
        except sqlite3.IntegrityError:
            self.rollback()
            saved = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (draft_attempt_id,))
            if saved:
                return self._attempt_dto(saved)
            raise
        except Exception:
            self.rollback()
            raise
        try:
            comparison = self._run_attempt_comparison(draft_attempt_id)
        except Exception as error:
            # Submission is already sealed; comparison is a best-effort draft.
            comparison = {"status": "failed", "raw_analysis": "", "comparison_error": str(error)[:500], "reference_answer": "", "error_reason": "", "error_breakpoint": "", "correct_approach": ""}
        attempt = self.one("SELECT response_snapshot FROM Attempt WHERE attempt_id=?", (draft_attempt_id,))
        response = as_dict(loads(attempt["response_snapshot"], {}))
        response["comparison_draft"] = comparison
        self.begin()
        try:
            self.conn.execute("UPDATE Attempt SET response_snapshot=? WHERE attempt_id=?", (dumps(response), draft_attempt_id))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return self._attempt_dto(self.one("SELECT * FROM Attempt WHERE attempt_id=?", (draft_attempt_id,)))

    def answer_question(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        requested_question_id = payload.get("question_id") if isinstance(payload.get("question_id"), str) else None
        query = payload.get("query") if isinstance(payload.get("query"), str) else ""
        query = query.strip()
        question_id = None
        question_text = ""
        primary_subject = None
        grading = {}
        linked = []
        if requested_question_id:
            question = self.one("SELECT * FROM Question WHERE question_id=?", (requested_question_id,))
            if question:
                question_id = requested_question_id
                revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=?", (question["current_question_revision_id"],))
                prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (revision["current_review_prompt_revision_id"],)) if revision else None
                presentation = self._presentation(loads(prompt["presentation_snapshot"], {})) if prompt else {}
                question_text = "\n".join(as_text(as_dict(block).get("content")) for block in as_list(presentation.get("blocks")))
                grading = as_dict(loads(revision["grading_reference_fixture_snapshot"], {})) if revision else {}
                primary_subject = grading.get("subject_key") if grading.get("subject_key") in SUBJECT_KEYS else None
                linked = self.get_question_sources(question_id)

        retrieval_query = self.compose_retrieval_query({
            "question_text": question_text,
            "chapter": grading.get("chapter"),
            "knowledge_point": grading.get("knowledge_point"),
            "question_type": grading.get("question_type"),
            "reference_answer": grading.get("reference_answer"),
        }, query=query)

        selected = []
        seen = set()
        for row in linked:
            passage_id = row.get("source_passage_id")
            if passage_id and passage_id not in seen:
                seen.add(passage_id)
                selected.append(dict(row))
        for row in self.retrieve(retrieval_query, primary_subject=primary_subject, related_subjects=self._light_cross_subjects(primary_subject)):
            passage_id = row.get("source_passage_id")
            if passage_id and passage_id not in seen:
                seen.add(passage_id)
                selected.append(dict(row))
        selected = selected[:8]
        sources = []
        context_parts = []
        for index, row in enumerate(selected, 1):
            source = {key: row.get(key) for key in ("source_passage_id", "source_artifact_id", "text", "page_no", "bbox", "locator_json")}
            source["ref"] = f"P{index}"
            sources.append(source)
            context_parts.append(f"[P{index}] {as_text(row.get('text'))}\n出处: artifact={row.get('source_artifact_id')} locator={row.get('locator_json') or '--'}")
        context = "\n\n".join(context_parts)
        if not context:
            context = "（没有可用的资料出处）"
        conversation = self.get_question_answers(question_id, limit=6) if question_id else []
        conversation_context = "\n\n".join(
            f"第 {index} 轮\n用户：{as_text(item.get('query'))}\n助手：{as_text(item.get('answer'))[:2000]}"
            for index, item in enumerate(conversation, 1)
        ) or "（这是第一轮提问）"
        messages = [
            {"role": "system", "content": "请用普通文本回答问题。只把给定资料作为参考，不要编造引用编号。保留对话上下文，但不要把先前回答当成不可修改的权威事实。"},
            {"role": "user", "content": f"题面：{question_text or '（未提供题面）'}\n问题：{query or '（未提供问题）'}\n此前对话：\n{conversation_context}\n资料上下文：\n{context}"},
        ]
        answer_text, model_provider, available, error = self._llm_chat(messages)
        status = "unavailable" if not available else "grounded" if sources else "unlocated"
        answer_id = uid("answer")
        source_snapshot = {"question_id": question_id, "requested_question_id": requested_question_id, "query": query, "retrieval_query": retrieval_query, "sources": sources, "context": context, "conversation_turn": len(conversation) + 1}
        if error:
            source_snapshot["error"] = error
        self.begin()
        try:
            self.conn.execute("INSERT INTO Answer(answer_id,question_id,query,answer_text,source_snapshot,model_provider,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (answer_id, question_id, query, answer_text, dumps(source_snapshot), model_provider, status, self.clock()))
            self.commit()
        except Exception:
            self.rollback()
            raise
        return {"answer_id": answer_id, "status": status, "answer": answer_text, "sources": sources, "context": context}

    def get_answer(self, answer_id):
        row = self.one("SELECT * FROM Answer WHERE answer_id=?", (answer_id,))
        if not row:
            return None
        result = dict(row)
        result["source_snapshot"] = loads(result.get("source_snapshot"), {})
        return result

    def get_question_answers(self, question_id, limit=30):
        question = self.one("SELECT question_id FROM Question WHERE question_id=?", (question_id,))
        if not question:
            raise DomainError("not_found", "question not found", {"question_id": question_id})
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 30
        rows = self.all("SELECT * FROM Answer WHERE question_id=? ORDER BY created_at,rowid LIMIT ?", (question_id, limit))
        result = []
        for row in rows:
            snapshot = loads(row["source_snapshot"], {})
            result.append({
                "answer_id": row["answer_id"],
                "question_id": row["question_id"],
                "query": row["query"],
                "answer": row["answer_text"],
                "status": row["status"],
                "model_provider": row["model_provider"],
                "created_at": row["created_at"],
                "sources": as_list(as_dict(snapshot).get("sources")),
                "error": as_text(as_dict(snapshot).get("error")),
            })
        return result

    # ---------- scheduling ----------

    def _triplet(self, question_id, revision_id=None, prompt_id=None):
        question = self.one("SELECT * FROM Question WHERE question_id=?", (question_id,))
        if not question:
            raise DomainError("not_found", "question not found", {"question_id": question_id})
        revision_id = revision_id or question["current_question_revision_id"]
        revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=?", (revision_id,))
        if not revision:
            raise DomainError("not_found", "question revision not found", {"question_revision_id": revision_id})
        prompt_id = prompt_id or revision["current_review_prompt_revision_id"]
        prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (prompt_id,))
        if not prompt:
            # Missing prompt is incomplete enrichment. Create a plain prompt
            # so the user can continue and edit it later.
            prompt_id = uid("qpr")
            prompt_data = self._presentation({"content": "待补充题面"})
            self.conn.execute("INSERT INTO ReviewPromptRevision(review_prompt_revision_id,question_id,question_revision_id,revision_no,presentation_snapshot,unresolved_critical_ambiguities,leakage_state,revision_state,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (prompt_id, question_id, revision["question_revision_id"], 1, dumps(prompt_data), dumps([]), "unknown", "candidate", self.clock()))
            self.conn.execute("UPDATE QuestionRevision SET current_review_prompt_revision_id=? WHERE question_revision_id=?", (prompt_id, revision["question_revision_id"]))
            prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (prompt_id,))
        return question, revision, prompt

    def _trigger_due(self, trigger):
        # All review timing is anchored to the confirmation cycle. This helper
        # remains for trigger prioritization, but never reintroduces the old
        # reason-specific +2/+3/+7/+21 offsets.
        return add_days(as_text(trigger.get("cycle_anchor_at") or trigger.get("captured_at"), self.clock()), REVIEW_OFFSETS[0])

    def _merge_triggers(self, snapshots):
        candidates = []
        for snapshot in as_list(snapshots):
            item = dict(as_dict(snapshot))
            reason = item.get("trigger_reason_kind") if item.get("trigger_reason_kind") in TASK_PRIORITY else "manual_declaration"
            item["trigger_reason_kind"] = reason
            candidates.append((TASK_PRIORITY.index(reason), self._trigger_due(item), item))
        if not candidates:
            return "manual_declaration", add_days(self.clock(), REVIEW_OFFSETS[0])
        candidates.sort(key=lambda value: (value[0], value[1]))
        return candidates[0][2]["trigger_reason_kind"], candidates[0][1]

    def _resolve_trigger(self, question_id, revision_id, prompt_id, value, as_of):
        raw = as_dict(value)
        kind = raw.get("kind") or "manual_declaration"
        trigger = {"trigger_ref": uid("trigger"), "trigger_kind": kind, "source_question_revision_id": revision_id, "captured_at": as_of}
        if kind == "attempt" and raw.get("attempt_id"):
            attempt = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (raw["attempt_id"],))
            if attempt:
                trigger["attempt_id"] = attempt["attempt_id"]
                trigger["captured_at"] = attempt["submitted_at"]
                if attempt["initial_debt_claim"] == "wrong":
                    trigger["trigger_reason_kind"] = "initial_error"
                elif attempt["assistance_state"] == "assisted" or attempt["external_help_reported"]:
                    trigger["trigger_reason_kind"] = "assisted_retry"
                elif attempt["completion_claim"] != "complete":
                    trigger["trigger_reason_kind"] = "incomplete_attempt"
                else:
                    trigger["trigger_reason_kind"] = "awaiting_assessment"
                return trigger
        if kind == "assessment" and raw.get("assessment_id"):
            assessment = self.one("SELECT * FROM Assessment WHERE assessment_id=?", (raw["assessment_id"],))
            if assessment:
                trigger.update({"assessment_id": assessment["assessment_id"], "trigger_reason_kind": "awaiting_assessment", "captured_at": assessment["finalized_at"] or as_of})
                return trigger
        if kind == "evidence" and raw.get("evidence_event_id"):
            evidence = self.one("SELECT * FROM EvidenceEvent WHERE evidence_event_id=?", (raw["evidence_event_id"],))
            if evidence:
                trigger.update({"evidence_event_id": evidence["evidence_event_id"], "trigger_reason_kind": "spaced_confirmation", "captured_at": evidence["captured_at"]})
                return trigger
        declaration_kind = raw.get("declaration_kind")
        trigger.update({"trigger_kind": "manual_declaration", "trigger_reason_kind": {"wrong": "initial_error", "incomplete": "incomplete_attempt"}.get(declaration_kind, "manual_declaration"), "declaration_id": as_text(raw.get("declaration_id"), uid("declaration")), "declaration_text": as_text(raw.get("declaration_text")), "declaration_kind": declaration_kind or "uncertain"})
        return trigger

    def _schedule_in_tx(self, question_id, revision_id, prompt_id, trigger, as_of, carry_triggers=None):
        trigger = dict(as_dict(trigger))
        trigger.setdefault("trigger_ref", uid("trigger"))
        trigger.setdefault("trigger_kind", "manual_declaration")
        trigger.setdefault("trigger_reason_kind", "manual_declaration")
        trigger.setdefault("source_question_revision_id", revision_id)
        trigger.setdefault("captured_at", as_of)
        if trigger.get("trigger_reason_kind") not in TASK_PRIORITY:
            trigger["trigger_reason_kind"] = "manual_declaration"
        self._triplet(question_id, revision_id, prompt_id)
        tasks = self.all("SELECT * FROM ReviewTask WHERE question_id=? ORDER BY review_round,created_at", (question_id,))
        first_task = tasks[0] if tasks else None
        first_snapshots = as_list(loads(first_task["trigger_snapshots"], [])) if first_task else []
        anchor = next((as_text(item.get("cycle_anchor_at")) for item in first_snapshots if as_text(item.get("cycle_anchor_at"))), None)
        anchor = anchor or next((as_text(item.get("captured_at")) for item in first_snapshots if as_text(item.get("captured_at"))), None)
        anchor = anchor or as_text(trigger.get("cycle_anchor_at") or trigger.get("captured_at"), as_of)
        trigger["cycle_anchor_at"] = anchor
        for task in tasks:
            for old in as_list(loads(task["trigger_snapshots"], [])):
                if any(key in trigger and trigger.get(key) and old.get(key) == trigger.get(key) for key in ("attempt_id", "assessment_id", "evidence_event_id", "declaration_id")):
                    return dict(task)
        open_task = self.one("SELECT * FROM ReviewTask WHERE question_id=? AND status='open' ORDER BY created_at LIMIT 1", (question_id,))
        if open_task:
            snapshots = as_list(loads(open_task["trigger_snapshots"], []))
            snapshots.append(trigger)
            reason, _ = self._merge_triggers(snapshots)
            expected_due = add_days(anchor, REVIEW_OFFSETS[min(max(int(open_task["review_round"]), 1), len(REVIEW_OFFSETS)) - 1])
            self.conn.execute("UPDATE ReviewTask SET trigger_snapshots=?,reason_kind=?,due_at=?,schedule_policy_version=? WHERE review_task_id=?", (dumps(snapshots), reason, expected_due, "fixed-3-7-10-14", open_task["review_task_id"]))
            return dict(self.one("SELECT * FROM ReviewTask WHERE review_task_id=?", (open_task["review_task_id"],)))
        max_round = max((int(task["review_round"]) for task in tasks), default=0)
        if max_round >= len(REVIEW_OFFSETS):
            # The fixed cycle ends at +14. Do not create a fifth task.
            return None
        snapshots = as_list(carry_triggers) + [trigger]
        reason, due = self._merge_triggers(snapshots)
        task_id = uid("task")
        review_round = max_round + 1
        due = add_days(anchor, REVIEW_OFFSETS[review_round - 1])
        self.conn.execute("INSERT INTO ReviewTask(review_task_id,question_id,question_revision_id,review_prompt_revision_id,kind,reason_kind,trigger_snapshots,snapshot_schema_version,review_round,due_at,schedule_policy_version,created_at,status,completed_by_attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, question_id, revision_id, prompt_id, "closed_book_review", reason, dumps(snapshots), SNAPSHOT, review_round, due, "fixed-3-7-10-14", as_of, "open", None))
        return dict(self.one("SELECT * FROM ReviewTask WHERE review_task_id=?", (task_id,)))

    def _schedule_after_review_submit(self, session, attempt_id, submitted_at):
        """Advance the fixed review cycle as soon as a redo is sealed."""
        trigger = {
            "trigger_ref": uid("trigger"),
            "trigger_kind": "attempt",
            "trigger_reason_kind": "spaced_confirmation",
            "attempt_id": attempt_id,
            "source_question_revision_id": session["question_revision_id"],
            "captured_at": submitted_at,
        }
        return self._schedule_in_tx(
            session["question_id"],
            session["question_revision_id"],
            session["review_prompt_revision_id"],
            trigger,
            submitted_at,
        )

    def schedule_review(self, payload):
        payload = as_dict(payload)
        question_id = payload.get("question_id")
        if not question_id:
            raise DomainError("not_found", "question_id is needed to schedule a review")
        self.begin()
        try:
            question = self.one("SELECT * FROM Question WHERE question_id=?", (question_id,))
            if not question:
                raise DomainError("not_found", "question not found", {"question_id": question_id})
            revision_id = payload.get("question_revision_id") or question["current_question_revision_id"]
            revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=?", (revision_id,))
            prompt_id = payload.get("review_prompt_revision_id") or (revision["current_review_prompt_revision_id"] if revision else None)
            trigger = self._resolve_trigger(question_id, revision_id, prompt_id, payload.get("trigger_input"), self.clock())
            task = self._schedule_in_tx(question_id, revision_id, prompt_id, trigger, self.clock())
            self.commit()
            if not task:
                return {"review_task_id": None, "reason_kind": trigger.get("trigger_reason_kind", "manual_declaration"), "review_round": len(REVIEW_OFFSETS), "due_at": None, "status": "completed"}
            return {key: task[key] for key in ("review_task_id", "reason_kind", "review_round", "due_at", "status")}
        except Exception:
            self.rollback()
            raise

    # ---------- review session and attempts ----------

    def start_review(self, task_id):
        self.begin()
        try:
            task = self.one("SELECT * FROM ReviewTask WHERE review_task_id=?", (task_id,))
            if not task:
                raise DomainError("not_found", "review task not found", {"review_task_id": task_id})
            submitted = self.one("SELECT * FROM ReviewSession WHERE review_task_id=? AND status='submitted' ORDER BY started_at LIMIT 1", (task_id,))
            if submitted:
                self.commit()
                return self._session_dto(submitted)
            active = self.one("SELECT * FROM ReviewSession WHERE review_task_id=? AND status='active' ORDER BY started_at LIMIT 1", (task_id,))
            if active:
                self.commit()
                return self._session_dto(active)
            # An abandoned session is resumable. Reusing it keeps the local
            # history intact and also works with older databases that had a
            # one-session-per-task unique constraint.
            abandoned = self.one("SELECT * FROM ReviewSession WHERE review_task_id=? AND status='abandoned' ORDER BY started_at DESC LIMIT 1", (task_id,))
            if abandoned:
                resumed = self.clock()
                self.conn.execute("UPDATE ReviewSession SET status='active',ended_at=NULL,updated_at=? WHERE review_session_id=?", (resumed, abandoned["review_session_id"]))
                self.commit()
                return self._session_dto(self.one("SELECT * FROM ReviewSession WHERE review_session_id=?", (abandoned["review_session_id"],)))
            if task["status"] != "open":
                raise DomainError("review_closed", "该回测已提交，请使用尚未提交的回测草稿", {"review_task_id": task_id})
            _, revision, prompt = self._triplet(task["question_id"], task["question_revision_id"], task["review_prompt_revision_id"])
            session_id = uid("session")
            started = self.clock()
            presentation = self._presentation(loads(prompt["presentation_snapshot"], {}))
            eligibility = {"schema_version": SNAPSHOT, "task_due_at_snapshot": task["due_at"], "eligibility_checked_at": started, "clean_ready": all(block.get("leakage_state") == "clean" for block in presentation["blocks"]), "presentation_snapshot": presentation}
            help_snapshot = as_list(loads(revision["help_content_fixture_snapshot"], []))
            self.conn.execute("INSERT INTO ReviewSession(review_session_id,review_task_id,question_id,question_revision_id,review_prompt_revision_id,prompt_eligibility_snapshot,draft_payload_snapshot,exposure_event_snapshots,visibility_policy_version,help_content_snapshot,prior_attempts_hidden,solutions_hidden,explanations_hidden,objective_hints_hidden,started_at,ended_at,updated_at,status,submitted_attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (session_id, task_id, task["question_id"], task["question_revision_id"], task["review_prompt_revision_id"], dumps(eligibility), dumps(self._draft({})), dumps([]), VISIBILITY, dumps(help_snapshot), 1, 1, 1, 1, started, None, started, "active", None))
            self.commit()
            return self._session_dto(self.one("SELECT * FROM ReviewSession WHERE review_session_id=?", (session_id,)))
        except Exception:
            self.rollback()
            raise

    def _session_dto(self, session):
        prompt = self.one("SELECT presentation_snapshot FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (session["review_prompt_revision_id"],))
        events = as_list(loads(session["exposure_event_snapshots"], []))
        eligibility = as_dict(loads(session["prompt_eligibility_snapshot"], {}))
        # A started session owns the presentation it exposed. Later role
        # edits update the current Question only and must not remove this
        # session's original question image.
        snapshot = eligibility.get("presentation_snapshot") if isinstance(eligibility.get("presentation_snapshot"), dict) else (loads(prompt["presentation_snapshot"], {}) if prompt else {})
        presentation = self._presentation(snapshot)
        question_assets, question_image_status = self._question_assets_from_refs(presentation.get("asset_refs"))
        question_text = as_text(presentation.get("content"))
        if not question_text:
            question_text = "\n".join(as_text(as_dict(block).get("content")) for block in as_list(presentation.get("blocks")))
        return {"review_session_id": session["review_session_id"], "review_task_id": session["review_task_id"], "status": session["status"], "presentation_snapshot": presentation, "question_text": question_text, "question_assets": question_assets, "question_image_status": question_image_status, "prompt_eligibility_snapshot": loads(session["prompt_eligibility_snapshot"], {}), "draft": loads(session["draft_payload_snapshot"], {}), "exposure_events": [{key: value for key, value in event.items() if key != "content"} for event in events], "assistance_state": "assisted" if events else "none_observed"}

    def session_action(self, session_id, action, payload=None):
        payload = as_dict(payload)
        self.begin()
        try:
            session = self.one("SELECT * FROM ReviewSession WHERE review_session_id=?", (session_id,))
            if not session:
                raise DomainError("not_found", "review session not found", {"review_session_id": session_id})
            if action == "save_draft":
                draft = self._draft(payload.get("draft", loads(session["draft_payload_snapshot"], {})))
                self.conn.execute("UPDATE ReviewSession SET draft_payload_snapshot=?,updated_at=?,status=CASE WHEN status='abandoned' THEN 'active' ELSE status END,ended_at=CASE WHEN status='abandoned' THEN NULL ELSE ended_at END WHERE review_session_id=?", (dumps(draft), self.clock(), session_id))
                self.commit()
                return {"review_session_id": session_id, "updated_at": self.clock(), "draft": draft}
            if action == "self_assess":
                state = as_text(payload.get("state")).strip()
                if state not in {"know", "dont_know", "uncertain", "full_redo"}:
                    raise DomainError("invalid_self_assessment", "state must be know, dont_know, uncertain, or full_redo")
                draft = self._draft(loads(session["draft_payload_snapshot"], {}))
                draft["self_assessment"] = state
                # “不会” follows the product flow into a complete redo by
                # default; “会/不确定” remain finishable without forcing
                # another upload, while “完整重做” is always explicit.
                draft["full_redo_requested"] = state in {"dont_know", "full_redo"} or bool(payload.get("full_redo"))
                self.conn.execute("UPDATE ReviewSession SET draft_payload_snapshot=?,updated_at=?,status=CASE WHEN status='abandoned' THEN 'active' ELSE status END,ended_at=CASE WHEN status='abandoned' THEN NULL ELSE ended_at END WHERE review_session_id=?", (dumps(draft), self.clock(), session_id))
                self.commit()
                return {"review_session_id": session_id, "status": "active", "self_assessment": state, "full_redo_requested": draft["full_redo_requested"], "draft": draft}
            if action == "expose":
                event_type = payload.get("event_type") if payload.get("event_type") in {"hint_revealed", "answer_revealed"} else "hint_revealed"
                content_ref = as_text(payload.get("content_ref"), "unlocated")
                fixture = as_list(loads(session["help_content_snapshot"], []))
                item = next((as_dict(x) for x in fixture if as_dict(x).get("content_ref") == content_ref), None)
                events = as_list(loads(session["exposure_event_snapshots"], []))
                event = {"exposure_event_id": uid("exposure"), "request_id": as_text(payload.get("request_id"), uid("request")), "event_type": event_type, "content_ref": content_ref, "content": as_text(item.get("content")) if item else as_text(payload.get("content")), "event_ordinal": len(events) + 1, "occurred_at": self.clock()}
                events.append(event)
                self.conn.execute("UPDATE ReviewSession SET exposure_event_snapshots=?,updated_at=? WHERE review_session_id=?", (dumps(events), self.clock(), session_id))
                self.commit()
                return {key: event[key] for key in ("exposure_event_id", "content_ref", "content", "event_ordinal")} | {"assistance_state": "assisted"}
            if action == "abandon":
                draft = self._draft(payload.get("draft", loads(session["draft_payload_snapshot"], {})))
                if session["status"] == "active":
                    self.conn.execute("UPDATE ReviewSession SET draft_payload_snapshot=?,status='abandoned',ended_at=?,updated_at=? WHERE review_session_id=?", (dumps(draft), self.clock(), self.clock(), session_id))
                self.commit()
                return {"review_session_id": session_id, "status": "abandoned", "attempt_id": None, "draft": draft}
            if action == "submit":
                if session["status"] == "submitted":
                    self.commit()
                    next_task = self.one("SELECT * FROM ReviewTask WHERE question_id=? AND status='open' ORDER BY due_at, review_round, created_at LIMIT 1", (session["question_id"],))
                    return {"review_session_id": session_id, "status": "submitted", "attempt_id": session["submitted_attempt_id"], "next_task": self._task_summary(next_task)}
                draft = self._draft(payload.get("draft", loads(session["draft_payload_snapshot"], {})))
                events = as_list(loads(session["exposure_event_snapshots"], []))
                attempt_id = uid("attempt")
                submitted_at = self.clock()
                response = {**draft, "assistance_state": "assisted" if events else "none_observed", "submit_event_ordinal": len(events) + 1}
                self.conn.execute("INSERT INTO Attempt(attempt_id,question_id,question_revision_id,review_session_id,origin_kind,submission_state,submitted_at,completion_claim,initial_debt_claim,initial_debt_claim_basis,initial_debt_claim_captured_at,response_snapshot,assistance_state,external_help_reported,submit_event_ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, session["question_id"], session["question_revision_id"], session_id, "review", "submitted", submitted_at, draft["completion_claim"], None, None, None, dumps(response), response["assistance_state"], int(draft["external_help_reported"]), response["submit_event_ordinal"], submitted_at))
                self.conn.execute("UPDATE ReviewSession SET status='submitted',submitted_attempt_id=?,ended_at=?,updated_at=?,draft_payload_snapshot=? WHERE review_session_id=?", (attempt_id, submitted_at, submitted_at, dumps(draft), session_id))
                self.conn.execute("UPDATE ReviewTask SET status='completed',completed_by_attempt_id=? WHERE review_task_id=?", (attempt_id, session["review_task_id"]))
                next_task = self._schedule_after_review_submit(session, attempt_id, submitted_at)
                self.commit()
                return {"review_session_id": session_id, "status": "submitted", "attempt_id": attempt_id, "next_task": self._task_summary(next_task)}
            raise DomainError("invalid_action", "unknown session action", {"action": action})
        except Exception:
            self.rollback()
            raise

    # ---------- assessment and evidence ----------

    def _assessment_reason(self, assessment, attempt):
        observations = as_list(loads(assessment["observations_snapshot"], []))
        observation = as_dict(observations[0] if observations else {})
        refs = as_list(loads(assessment["reference_inputs_snapshot"], []))
        has_fixture = any(as_dict(ref).get("accepted_for_grading") is True for ref in refs)
        if attempt["assistance_state"] != "none_observed" or bool(attempt["external_help_reported"]):
            return "assisted_retry"
        if assessment["assessor_kind"] in ("model", "user_self") or assessment["result"] == "unassessed":
            return "awaiting_assessment"
        if not has_fixture or observation.get("observation_state") != "observed":
            return "awaiting_assessment"
        if attempt["completion_claim"] != "complete":
            return "incomplete_attempt"
        if assessment["result"] == "incorrect" or observation.get("performance_state") in ("failure", "partial"):
            return "retry_after_fail"
        return "spaced_confirmation"

    def assess_attempt(self, payload):
        payload = as_dict(payload)
        attempt_id = payload.get("attempt_id")
        if not attempt_id:
            raise DomainError("not_found", "attempt_id is needed to assess")
        self.begin()
        try:
            attempt = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (attempt_id,))
            if not attempt:
                raise DomainError("not_found", "attempt not found", {"attempt_id": attempt_id})
            old = self.one("SELECT * FROM Assessment WHERE attempt_id=?", (attempt_id,))
            if old:
                self.commit()
                return self._assessment_result(old)
            revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=?", (attempt["question_revision_id"],))
            mapping = as_list(loads(revision["objective_mapping_snapshot"], [])) if revision else []
            measured = next((as_dict(item) for item in mapping if as_dict(item).get("role") == "measured"), as_dict(mapping[0]) if mapping else {})
            objective_input = {"objective_input_ref": uid("oi"), "objective_ref": as_text(measured.get("objective_ref"), "unmapped"), "learning_objective_id": measured.get("learning_objective_id"), "usage_role": "measured", "question_revision_id": attempt["question_revision_id"], "mapping_snapshot_ref": as_text(measured.get("objective_ref"), "unmapped"), "mapping_snapshot": measured}
            raw_observation = as_dict((as_list(payload.get("observations")) or [{}])[0])
            observation = {**raw_observation, "observation_ref": uid("obs"), "objective_input_ref": objective_input["objective_input_ref"], "objective_ref": objective_input["objective_ref"], "learning_objective_id": objective_input["learning_objective_id"], "question_revision_id": attempt["question_revision_id"], "evidence_policy_version": EVIDENCE, "observation_state": raw_observation.get("observation_state", "not_observed"), "performance_state": raw_observation.get("performance_state", "indeterminate"), "error_attributed": bool(raw_observation.get("error_attributed")), "criterion_results": as_list(raw_observation.get("criterion_results")), "supporting_attempt_fragments": as_list(raw_observation.get("supporting_attempt_fragments")), "exposure_event_refs": as_list(raw_observation.get("exposure_event_refs")), "judgment_basis_kind": raw_observation.get("judgment_basis_kind", "unavailable"), "completion_claim_snapshot": attempt["completion_claim"], "assistance_state_snapshot": attempt["assistance_state"], "external_help_reported_snapshot": bool(attempt["external_help_reported"]), "rationale": as_text(raw_observation.get("rationale"))}
            raw_result = payload.get("raw_result", payload.get("result", "unassessed"))
            if raw_result not in {"correct", "incorrect", "partially_correct", "conflicted", "unassessed"}:
                raw_result = "unassessed"
            assessor = payload.get("assessor_kind") if payload.get("assessor_kind") in {"human", "deterministic", "model", "user_self"} else "user_self"
            refs = []
            fixture = loads(revision["grading_reference_fixture_snapshot"], {}) if revision else {}
            if isinstance(fixture, dict) and fixture:
                refs.append({"reference_ref": uid("ref"), **fixture, "accepted_for_grading": True, "question_revision_id": attempt["question_revision_id"], "usage_role": "primary", "captured_at": self.clock()})
            for ref in as_list(payload.get("reference_inputs")):
                refs.append({**as_dict(ref), "reference_ref": uid("ref"), "accepted_for_grading": False, "captured_at": self.clock()})
            assessment_id = uid("assessment")
            prompt_id = None
            if attempt["review_session_id"]:
                session = self.one("SELECT review_prompt_revision_id FROM ReviewSession WHERE review_session_id=?", (attempt["review_session_id"],))
                prompt_id = session["review_prompt_revision_id"] if session else None
            created = self.clock()
            self.conn.execute("INSERT INTO Assessment(assessment_id,attempt_id,question_id,question_revision_id,review_prompt_revision_id,snapshot_schema_version,assessment_policy_version,assessor_kind,raw_result,result,attempt_interpretation_state,normalization_note,attempt_response_snapshot,objective_inputs_snapshot,reference_inputs_snapshot,observations_snapshot,proposal_snapshot,status,invalidation_reason_snapshot,created_at,finalized_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (assessment_id, attempt_id, attempt["question_id"], attempt["question_revision_id"], prompt_id, SNAPSHOT, EVIDENCE, assessor, raw_result, None, "not_available", None, as_text(attempt["response_snapshot"], "{}"), dumps([objective_input]), dumps(refs), dumps([observation]), dumps(payload), "draft", None, created, created))
            result = self._finalize(self.one("SELECT * FROM Assessment WHERE assessment_id=?", (assessment_id,)), created)
            self.commit()
            return result
        except Exception:
            self.rollback()
            raise

    def _finalize(self, assessment, finalized_at):
        attempt = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (assessment["attempt_id"],))
        observations = as_list(loads(assessment["observations_snapshot"], []))
        observation = as_dict(observations[0] if observations else {})
        refs = as_list(loads(assessment["reference_inputs_snapshot"], []))
        fixture_ok = any(as_dict(ref).get("accepted_for_grading") is True for ref in refs)
        basis_ok = observation.get("judgment_basis_kind") == "manual_accepted" and assessment["assessor_kind"] in ("human", "deterministic")
        clean_attempt = attempt["assistance_state"] == "none_observed" and not bool(attempt["external_help_reported"])
        complete = attempt["completion_claim"] == "complete"
        observed = observation.get("observation_state") == "observed"
        performance = observation.get("performance_state")
        criteria = as_list(observation.get("criterion_results"))
        states = [as_dict(item).get("state") for item in criteria]
        can_evidence = fixture_ok and basis_ok and clean_attempt and complete and observed and performance in ("success", "failure")
        if performance == "success":
            can_evidence = can_evidence and bool(criteria) and all(state == "met" for state in states)
        if performance == "failure":
            can_evidence = can_evidence and bool(criteria) and any(state == "unmet" for state in states)
        result = "correct" if can_evidence and performance == "success" else "incorrect" if can_evidence and performance == "failure" else "unassessed"
        note = None if result == assessment["raw_result"] else dumps({"raw_result": assessment["raw_result"], "derived_result": result})
        self.conn.execute("UPDATE Assessment SET result=?,normalization_note=?,status='final',finalized_at=? WHERE assessment_id=?", (result, note, finalized_at, assessment["assessment_id"]))
        evidence_ids = []
        if result in ("correct", "incorrect") and observation.get("learning_objective_id"):
            evidence_id = uid("evidence")
            self.conn.execute("INSERT INTO EvidenceEvent(evidence_event_id,assessment_id,observation_ref,learning_objective_id,event_type,performance_state,evidence_policy_version,captured_at) VALUES(?,?,?,?,?,?,?,?)", (evidence_id, assessment["assessment_id"], observation.get("observation_ref", uid("obs")), observation["learning_objective_id"], "observation", "success" if result == "correct" else "failure", EVIDENCE, finalized_at))
            evidence_ids.append(evidence_id)
        next_task = None
        if attempt["origin_kind"] == "review":
            final_assessment = self.one("SELECT * FROM Assessment WHERE assessment_id=?", (assessment["assessment_id"],))
            reason = self._assessment_reason(final_assessment, attempt)
            if reason == "spaced_confirmation" and evidence_ids:
                trigger = {"trigger_ref": uid("trigger"), "trigger_kind": "evidence", "trigger_reason_kind": reason, "evidence_event_id": evidence_ids[0], "source_question_revision_id": attempt["question_revision_id"], "captured_at": finalized_at}
            else:
                trigger = {"trigger_ref": uid("trigger"), "trigger_kind": "assessment", "trigger_reason_kind": reason, "assessment_id": assessment["assessment_id"], "source_question_revision_id": attempt["question_revision_id"], "captured_at": finalized_at}
            session = self.one("SELECT review_prompt_revision_id FROM ReviewSession WHERE review_session_id=?", (attempt["review_session_id"],))
            if session:
                next_task = self._schedule_in_tx(attempt["question_id"], attempt["question_revision_id"], session["review_prompt_revision_id"], trigger, finalized_at)
        return {"assessment_id": assessment["assessment_id"], "result": result, "evidence_event_ids": evidence_ids, "next_task": {key: next_task[key] for key in ("review_task_id", "reason_kind", "review_round", "due_at", "status")} if next_task else None}

    def _assessment_result(self, assessment):
        evidence = [row["evidence_event_id"] for row in self.all("SELECT evidence_event_id FROM EvidenceEvent WHERE assessment_id=?", (assessment["assessment_id"],))]
        next_task = None
        attempt = self.one("SELECT * FROM Attempt WHERE attempt_id=?", (assessment["attempt_id"],))
        if attempt and attempt["origin_kind"] == "review":
            for task in self.all("SELECT * FROM ReviewTask WHERE question_id=? ORDER BY review_round", (attempt["question_id"],)):
                if any(item.get("assessment_id") == assessment["assessment_id"] or item.get("evidence_event_id") in evidence for item in as_list(loads(task["trigger_snapshots"], []))):
                    next_task = {key: task[key] for key in ("review_task_id", "reason_kind", "review_round", "due_at", "status")}
                    break
        return {"assessment_id": assessment["assessment_id"], "result": assessment["result"], "evidence_event_ids": evidence, "next_task": next_task}

    # ---------- reads ----------

    def get_due_reviews(self):
        rows = self.all("SELECT * FROM ReviewItemProjection WHERE status='open' AND due_at<=?", (self.clock(),))
        rows = sorted(rows, key=lambda row: (TASK_PRIORITY.index(row["reason_kind"]) if row["reason_kind"] in TASK_PRIORITY else 99, row["due_at"], row["review_round"], row["created_at"] or ""))
        result = []
        for row in rows:
            question = self.one("SELECT * FROM Question WHERE question_id=?", (row["question_id"],))
            revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=?", (row["question_revision_id"],)) if question else None
            prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (row["review_prompt_revision_id"],)) if row["review_prompt_revision_id"] else None
            presentation = loads(prompt["presentation_snapshot"], {}) if prompt else {}
            refs = presentation.get("asset_refs") if isinstance(presentation, dict) else []
            question_assets, question_image_status = self._question_assets_from_refs(refs)
            grading = as_dict(loads(revision["grading_reference_fixture_snapshot"], {})) if revision else {}
            data_origin = as_text(grading.get("data_origin")) or ("real" if grading.get("intake_id") else "legacy")
            result.append({"review_task_id":row["review_task_id"],"question_id":row["question_id"],"question_text":as_text(presentation.get("content")) if isinstance(presentation, dict) else "","question_assets":question_assets,"question_image_status":question_image_status,"is_image_intake":bool(grading.get("intake_id")),"data_origin":data_origin,"display_label":as_text(grading.get("display_label")) or ("演示/兼容" if data_origin in ("demo", "legacy") else "真实题目"),"review_round":row["review_round"],"reason_kind":row["reason_kind"],"due_at":row["due_at"],"status":row["status"],"state":"due"})
        # Keep a real image intake ahead of an optional demo/legacy task when
        # the singular UI asks for the next item, while still returning every
        # due row to callers of the plural endpoint.
        result.sort(key=lambda item: (0 if item["is_image_intake"] else 1, item["due_at"], item["review_round"], item["question_id"]))
        return result

    def get_due_review(self):
        """Legacy singular DTO retained for /api/due-review callers."""
        rows = self.get_due_reviews()
        return rows[0] if rows else None

    def projections(self):
        now = self.clock()
        items = []
        for row in self.all("SELECT * FROM ReviewItemProjection ORDER BY due_at,review_round,created_at"):
            item = dict(row)
            if item["status"] == "open" and parse_time(item["due_at"]) <= parse_time(now):
                item["state"] = "due"
            prompt = self.one("SELECT presentation_snapshot FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (item.get("review_prompt_revision_id"),)) if item.get("review_prompt_revision_id") else None
            presentation = loads(prompt["presentation_snapshot"], {}) if prompt else {}
            item["question_text"] = as_text(presentation.get("content")) if isinstance(presentation, dict) else ""
            revision = self.one("SELECT grading_reference_fixture_snapshot FROM QuestionRevision WHERE question_revision_id=?", (item.get("question_revision_id"),)) if item.get("question_revision_id") else None
            grading = as_dict(loads(revision["grading_reference_fixture_snapshot"], {})) if revision else {}
            data_origin = as_text(grading.get("data_origin")) or ("real" if grading.get("intake_id") else "legacy")
            item["subject_key"] = grading.get("subject_key") if grading.get("subject_key") in SUBJECT_KEYS else None
            item["data_origin"] = data_origin
            item["display_label"] = as_text(grading.get("display_label")) or ("演示/兼容" if data_origin in ("demo", "legacy") else "真实题目")
            items.append(item)
        objectives = []
        for row in self.all("SELECT * FROM LearnerObjectiveProjection"):
            item = dict(row)
            item["supporting_evidence_event_ids"] = loads(item.get("supporting_evidence_event_ids"), []) or []
            objectives.append(item)
        return {"review_items": items, "learner_objectives": objectives}

    def knowledge_navigation(self, filters=None):
        filters = as_dict(filters)
        subject_filter = as_text(filters.get("subject_key")).strip()
        chapter_filter = as_text(filters.get("chapter")).strip()
        knowledge_filter = as_text(filters.get("knowledge_point")).strip()
        subjects = {}
        question_count = 0
        source_count = 0
        rows = self.all("SELECT q.question_id,qr.question_revision_id,qr.question_units,qr.grading_reference_fixture_snapshot FROM Question q JOIN QuestionRevision qr ON qr.question_revision_id=q.current_question_revision_id WHERE qr.revision_state='confirmed' ORDER BY q.created_at,q.question_id")
        labels = {"math": "数学", "english": "英语", "politics": "政治", "professional": "专业课", "unclassified": "未分类"}
        for row in rows:
            grading = as_dict(loads(row["grading_reference_fixture_snapshot"], {}))
            subject = as_text(grading.get("subject_key")).strip() or "unclassified"
            chapter = as_text(grading.get("chapter")).strip() or "待补充"
            knowledge = as_text(grading.get("knowledge_point")).strip() or "待补充"
            if subject_filter and subject != subject_filter:
                continue
            if chapter_filter and chapter != chapter_filter:
                continue
            if knowledge_filter and knowledge != knowledge_filter:
                continue
            units = loads(row["question_units"], [])
            if isinstance(units, list) and units and isinstance(units[0], dict):
                question_text = as_text(units[0].get("question_text"))
            else:
                question_text = as_text(as_dict(units).get("question_text"))
            links = self.get_question_sources(row["question_id"])
            source_count += len(links)
            attempts = self.all("SELECT attempt_id,origin_kind,submitted_at,completion_claim,response_snapshot FROM Attempt WHERE question_id=? AND submission_state='submitted' ORDER BY submitted_at", (row["question_id"],))
            attempt_items = []
            for attempt in attempts:
                response = as_dict(loads(attempt["response_snapshot"], {}))
                assessment = self.one("SELECT result FROM Assessment WHERE attempt_id=? AND status='final' ORDER BY created_at DESC LIMIT 1", (attempt["attempt_id"],))
                attempt_items.append({"attempt_id": attempt["attempt_id"], "origin_kind": attempt["origin_kind"], "submitted_at": attempt["submitted_at"], "completion_claim": attempt["completion_claim"], "result": assessment["result"] if assessment else None, "response_text": as_text(response.get("response_text"))})
            item = {
                "question_id": row["question_id"],
                "question_text": question_text or "待补题面",
                "error_reason": as_text(grading.get("error_reason")),
                "error_breakpoint": as_text(grading.get("error_breakpoint")),
                "attempts": attempt_items,
                "sources": [{"source_passage_id": link.get("source_passage_id"), "source_artifact_id": link.get("source_artifact_id"), "text": link.get("text"), "page_no": link.get("page_no"), "locator_json": link.get("locator_json")} for link in links],
            }
            subject_node = subjects.setdefault(subject, {"subject_key": subject, "label": labels.get(subject, subject), "chapters": {}})
            chapter_node = subject_node["chapters"].setdefault(chapter, {"chapter": chapter, "knowledge_points": {}})
            knowledge_node = chapter_node["knowledge_points"].setdefault(knowledge, {"knowledge_point": knowledge, "questions": [], "sources": []})
            knowledge_node["questions"].append(item)
            for source in item["sources"]:
                if source not in knowledge_node["sources"]:
                    knowledge_node["sources"].append(source)
            question_count += 1
        subject_items = []
        for subject in subjects.values():
            chapters = []
            for chapter in subject["chapters"].values():
                chapter["knowledge_points"] = list(chapter["knowledge_points"].values())
                chapters.append(chapter)
            subject["chapters"] = chapters
            subject_items.append(subject)
        return {"subjects": subject_items, "question_count": question_count, "source_count": source_count}

    def weak_points(self):
        from collections import defaultdict
        aggregates = defaultdict(lambda: {"count": 0, "question_ids": [], "examples": []})
        rows = self.all("SELECT q.question_id,qr.question_units,qr.grading_reference_fixture_snapshot FROM Question q JOIN QuestionRevision qr ON qr.question_revision_id=q.current_question_revision_id WHERE qr.revision_state='confirmed'")
        for row in rows:
            grading = as_dict(loads(row["grading_reference_fixture_snapshot"], {}))
            question_id = row["question_id"]
            units = loads(row["question_units"], [])
            question_text = as_text(units[0].get("question_text")) if isinstance(units, list) and units and isinstance(units[0], dict) else as_text(as_dict(units).get("question_text"))
            for kind, key in (("错误原因", "error_reason"), ("首次断点", "error_breakpoint")):
                label = as_text(grading.get(key)).strip()
                if not label or label == "待补充":
                    continue
                label = label.splitlines()[0][:120]
                entry = aggregates[(kind, label)]
                entry["count"] += 1
                if question_id not in entry["question_ids"]:
                    entry["question_ids"].append(question_id)
                if len(entry["examples"]) < 3:
                    entry["examples"].append({"question_id": question_id, "question_text": question_text or "待补题面"})
            incomplete = self.one("SELECT COUNT(*) AS count FROM Attempt WHERE question_id=? AND submission_state='submitted' AND completion_claim!='complete'", (question_id,))["count"]
            if incomplete:
                entry = aggregates[("过程不完整", "过程不完整")]
                entry["count"] += incomplete
                if question_id not in entry["question_ids"]:
                    entry["question_ids"].append(question_id)
        result = []
        for (kind, label), item in aggregates.items():
            result.append({"kind": kind, "label": label, **item})
        result.sort(key=lambda item: (-item["count"], item["kind"], item["label"]))
        return result

    def export_wrong_questions(self, question_ids=None, include_answers=True):
        wanted = {value for value in as_list(question_ids) if isinstance(value, str)}
        rows = self.list_wrong_questions()
        if wanted:
            rows = [row for row in rows if row["question_id"] in wanted]
        lines = ["# 错题本" if include_answers else "# 无答案自测", ""]
        for index, row in enumerate(rows, 1):
            detail = self.get_wrong_question(row["question_id"])
            grading = as_dict(detail.get("grading"))
            lines.extend([f"## {index}. {detail.get('question_text') or '待补题面'}", ""])
            lines.append(f"- 题目 ID：`{row['question_id']}`")
            lines.append(f"- 科目：{grading.get('subject_key') or '未分类'}")
            lines.append(f"- 章节：{grading.get('chapter') or '待补充'}")
            lines.append(f"- 知识点：{grading.get('knowledge_point') or '待补充'}")
            for asset in detail.get("assets", []):
                lines.append(f"- 题面图片：![{asset.get('original_filename', 'image')}]({asset.get('media_url')})")
            if include_answers:
                lines.extend(["", f"**参考答案**：{grading.get('reference_answer') or '待补充'}", f"**错误类型**：{ERROR_TYPES.get(grading.get('error_type'), '待确认')}", f"**错误原因**：{grading.get('error_reason') or '待补充'}", f"**首次出错步骤**：{grading.get('error_breakpoint') or '待补充'}", f"**正确思路**：{grading.get('correct_approach') or '待补充'}"])
            lines.append("\n---\n")
        return "\n".join(lines).rstrip() + "\n"

    def similar_practice(self, payload):
        payload = as_dict(payload)
        question_id = as_text(payload.get("question_id")).strip()
        if not question_id:
            raise DomainError("invalid_question", "question_id is required")
        detail = self.get_wrong_question(question_id)
        grading = as_dict(detail.get("grading"))
        prompt = "\n".join([
            "请为下面这道考研错题生成一道难度和知识点相近、但题面不同的练习题。",
            "只输出普通文本，明确写出‘练习题’和‘参考答案’两个部分；这是候选草稿，不要声称它已进入错题本。",
            f"原题：{detail.get('question_text') or '待补题面'}",
            f"科目：{grading.get('subject_key') or '未分类'}；章节：{grading.get('chapter') or '待补充'}；知识点：{grading.get('knowledge_point') or '待补充'}",
            f"原题错误原因：{grading.get('error_reason') or '待补充'}；断点：{grading.get('error_breakpoint') or '待补充'}",
            f"用户补充要求：{as_text(payload.get('instruction')) or '保持同一知识点并更换数字或情境'}",
        ])
        raw, provider, errors = self._invoke_with_fallback(prompt, [])
        if not raw:
            return {"status": "failed", "error": "；".join(errors)[:500] or "provider unavailable", "provider": provider}
        fields = self._extract_analysis(raw, grading.get("subject_key"))
        candidate = as_text(fields.get("question_text")).strip() or raw.strip()
        reference_answer = as_text(fields.get("reference_answer")).strip()
        return {"status": "draft", "question_text": candidate, "reference_answer": reference_answer, "raw": raw, "provider": provider, "save_payload": {"source_question_id": question_id, "subject_key": grading.get("subject_key"), "course_id": grading.get("course_id"), "question_text": candidate, "reference_answer": reference_answer, "raw": raw}}

    def get_session(self, session_id):
        row = self.one("SELECT * FROM ReviewSession WHERE review_session_id=?", (session_id,))
        return self._session_dto(row) if row else None

    def get_history(self, question_id):
        question = self.one("SELECT * FROM Question WHERE question_id=?", (question_id,))
        if not question:
            return None
        return {
            "question": dict(question),
            "revisions": [dict(row) for row in self.all("SELECT * FROM QuestionRevision WHERE question_id=? ORDER BY revision_no", (question_id,))],
            "tasks": [dict(row) for row in self.all("SELECT * FROM ReviewTask WHERE question_id=? ORDER BY review_round", (question_id,))],
            "sessions": [dict(row) for row in self.all("SELECT * FROM ReviewSession WHERE question_id=? ORDER BY started_at", (question_id,))],
            "attempts": [dict(row) for row in self.all("SELECT * FROM Attempt WHERE question_id=? ORDER BY submitted_at", (question_id,))],
            "assessments": [dict(row) for row in self.all("SELECT * FROM Assessment WHERE question_id=? ORDER BY created_at", (question_id,))],
            "sources": self.get_question_sources(question_id),
            "answers": self.get_question_answers(question_id),
        }

    def north_star_events(self):
        rows = self.all("SELECT a.attempt_id,a.submitted_at,a.question_id,rs.review_session_id,rs.prompt_eligibility_snapshot FROM Attempt a JOIN ReviewSession rs ON rs.submitted_attempt_id=a.attempt_id WHERE rs.status='submitted' AND a.origin_kind='review' AND a.submission_state='submitted' AND a.assistance_state='none_observed' AND a.external_help_reported=0")
        result = []
        for row in rows:
            snapshot = loads(row["prompt_eligibility_snapshot"], {})
            response_row = self.one("SELECT response_snapshot FROM Attempt WHERE attempt_id=?", (row["attempt_id"],))
            response = loads(response_row[0] if response_row else None, {})
            if parse_time(snapshot.get("task_due_at_snapshot")) <= parse_time(row["submitted_at"]) and (as_text(response.get("response_text")).strip() or response.get("response_selections") or response.get("response_assets")):
                result.append(dict(row))
        return result


def run_scenario(scenario_id, db_path=None, as_of="2026-09-01T00:00:00Z"):
    if scenario_id not in SCENARIOS:
        raise DomainError("invalid_scenario", "unknown scenario")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(db_path) if db_path else Path(directory) / "scenario.sqlite"
        store = Store(path, lambda: as_of)
        seed = store.seed_p0(as_of)
        due = store.get_due_review()
        output = {"scenario_id": scenario_id, "seed": seed, "due_task": due}
        if scenario_id == "P0-S1-debt-to-due":
            return output
        session = store.start_review(due["review_task_id"])
        output["session"] = session
        if scenario_id == "P0-S2-start-abandon":
            output["abandon"] = store.session_action(session["review_session_id"], "abandon")
            return output
        draft = {"response_text": "积分区间是 0 到 2，结果为 2。", "response_selections": [], "response_assets": [], "completion_claim": "complete", "external_help_reported": False}
        if scenario_id == "P0-S5-assisted-or-unassessed":
            exposure = store.session_action(session["review_session_id"], "expose", {"request_id": "r1", "event_type": "hint_revealed", "content_ref": "hint-1"})
            submitted = store.session_action(session["review_session_id"], "submit", {"draft": draft})
            output["assessment"] = store.assess_attempt({"attempt_id": submitted["attempt_id"], "assessor_kind": "model", "result": "unassessed", "objective_inputs": [{"objective_ref": "obj-1", "usage_role": "measured"}], "observations": [{"observation_state": "observed", "performance_state": "success", "error_attributed": False, "criterion_results": [{"criterion": "能正确确定积分区间", "state": "met"}], "supporting_attempt_fragments": ["whole"], "exposure_event_refs": [exposure["exposure_event_id"]], "judgment_basis_kind": "model_provisional", "rationale": "model"}]})
            return output
        output["submit"] = store.session_action(session["review_session_id"], "submit", {"draft": draft})
        output["repeat_submit"] = store.session_action(session["review_session_id"], "submit", {"draft": draft})
        failure = scenario_id == "P0-S6-finalize-chain"
        output["assessment"] = store.assess_attempt({"attempt_id": output["submit"]["attempt_id"], "assessor_kind": "human", "result": "incorrect" if failure else "correct", "objective_inputs": [{"objective_ref": "obj-1", "usage_role": "measured"}], "observations": [{"observation_state": "observed", "performance_state": "failure" if failure else "success", "error_attributed": failure, "criterion_results": [{"criterion": "能正确确定积分区间", "state": "unmet" if failure else "met"}], "supporting_attempt_fragments": ["whole"], "exposure_event_refs": [], "judgment_basis_kind": "manual_accepted", "rationale": "人工按固定 rubric 判定"}]})
        output["projections"] = store.projections()
        return output


class Handler(BaseHTTPRequestHandler):
    store = None

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status, body, content_type="text/plain; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _multipart(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        form = cgi.FieldStorage(fp=io.BytesIO(body), headers=self.headers,
                                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type,
                                         "CONTENT_LENGTH": str(length)})
        files = []
        values = {}
        for field in form.list or []:
            if field.filename is not None:
                files.append({"filename": field.filename, "mime": field.type, "data": field.file.read()})
            else:
                values[field.name] = field.value
        return values, files

    def _request_payload(self):
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            return self._multipart()
        length = int(self.headers.get("Content-Length", 0))
        return loads(self.rfile.read(length) or b"{}", {})

    def _question_bank_payload(self, payload):
        """Parse JSON or multipart question-bank input once for preview/import."""
        if not isinstance(payload, tuple):
            return as_dict(payload)
        values, files = payload
        if not files:
            raise DomainError("invalid_question_bank", "CSV or JSON file is required")
        upload = files[0]
        try:
            raw = (upload.get("data") or b"").decode("utf-8-sig")
            if (upload.get("filename") or "").lower().endswith(".json"):
                parsed = json.loads(raw)
                rows = parsed.get("items", parsed) if isinstance(parsed, dict) else parsed
            else:
                rows = list(csv.DictReader(io.StringIO(raw)))
        except (UnicodeDecodeError, ValueError, csv.Error) as error:
            raise DomainError("invalid_question_bank", f"cannot parse question bank: {error}")
        if not isinstance(rows, list):
            raise DomainError("invalid_question_bank", "question bank must contain a list of rows")
        fallback_course = values.get("course_id") or None
        return {"course_id": fallback_course, "items": [{**as_dict(row), **({"course_id": fallback_course} if fallback_course and not row.get("course_id") else {})} for row in rows]}

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/due-review":
                return self._json(200, {"data": self.store.get_due_review()})
            if path == "/api/reviews/due":
                return self._json(200, {"data": self.store.get_due_reviews()})
            if path == "/api/projections":
                return self._json(200, {"data": self.store.projections()})
            if path == "/api/courses":
                return self._json(200, {"data": self.store.list_courses()})
            if path == "/api/knowledge-nodes":
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                course_id = query.get("course_id", [None])[0]
                return self._json(200, {"data": self.store.list_knowledge_nodes(course_id)})
            if path == "/api/question-bank":
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                filters = {key: values[0] for key, values in query.items() if values}
                return self._json(200, {"data": self.store.list_question_bank(filters)})
            if path == "/api/question-bank/attempts":
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                filters = {key: values[0] for key, values in query.items() if values}
                return self._json(200, {"data": self.store.list_question_bank_attempts(filters)})
            if path == "/api/knowledge":
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                filters = {key: values[0] for key, values in query.items() if values}
                return self._json(200, {"data": self.store.knowledge_navigation(filters)})
            if path == "/api/insights/weak-points":
                return self._json(200, {"data": self.store.weak_points()})
            if path == "/api/export/wrong-questions":
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                question_ids = query.get("question_id", [])
                include_answers = query.get("answers", ["1"])[0] not in {"0", "false", "no"}
                return self._text(200, self.store.export_wrong_questions(question_ids, include_answers), "text/markdown; charset=utf-8")
            if path == "/api/north-star":
                return self._json(200, {"data": self.store.north_star_events()})
            if path == "/api/intake":
                return self._json(200, {"data": self.store.list_intakes()})
            if path == "/api/sources":
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                limit = query.get("limit", [30])[0]
                return self._json(200, {"data": self.store.list_sources(limit)})
            if path == "/api/settings/model":
                return self._json(200, {"data": self.store.get_model_settings()})
            if path == "/api/wrong-questions":
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                filters = {key: values[0] for key, values in query.items() if values}
                return self._json(200, {"data": self.store.list_wrong_questions(filters)})
            if path.startswith("/api/attempts/"):
                return self._json(200, {"data": self.store.get_attempt(path.rsplit("/", 1)[1])})
            if path.startswith("/api/wrong-questions/") and path.endswith("/similar"):
                question_id = path[len("/api/wrong-questions/"):-len("/similar")].strip("/")
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                filters = {key: values[0] for key, values in query.items() if values and values[0]}
                return self._json(200, {"data": self.store.similar_question_bank(question_id, filters)})
            if path.startswith("/api/wrong-questions/"):
                return self._json(200, {"data": self.store.get_wrong_question(path.rsplit("/", 1)[1])})
            if path.startswith("/api/intake/"):
                return self._json(200, {"data": self.store._intake_detail(path.rsplit("/", 1)[1])})
            if path.startswith("/media/"):
                media = self.store.media_asset(path.rsplit("/", 1)[1])
                if not media:
                    return self._json(404, {"error": {"code": "not_found", "message": "media not found", "details": {}}})
                file_path, mime, filename = media
                size = file_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'inline; filename="{filename.replace(chr(34), "")}"')
                self.end_headers()
                with file_path.open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk: break
                        self.wfile.write(chunk)
                return
            if path.startswith("/api/session/"):
                return self._json(200, {"data": self.store.get_session(path.rsplit("/", 1)[1])})
            if path.startswith("/api/answer/"):
                return self._json(200, {"data": self.store.get_answer(path.rsplit("/", 1)[1])})
            if path.startswith("/api/question/") and path.endswith("/sources"):
                question_id = path[len("/api/question/") : -len("/sources")].rstrip("/")
                return self._json(200, {"data": self.store.get_question_sources(question_id)})
            if path.startswith("/api/question/") and path.endswith("/answers"):
                question_id = path[len("/api/question/") : -len("/answers")].rstrip("/")
                return self._json(200, {"data": self.store.get_question_answers(question_id)})
            if path.startswith("/api/question/"):
                return self._json(200, {"data": self.store.get_history(path.rsplit("/", 1)[1])})
            if path.startswith("/api/source/"):
                return self._json(200, {"data": self.store.get_source(path.rsplit("/", 1)[1])})
            if path == "/api/search":
                query = urlparse(self.path).query
                value = parse_qs(query).get("q", [""])[0]
                return self._json(200, {"data": self.store.search_sources(value)})
            if path == "/":
                body = (ROOT / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(404, {"error": {"code": "not_found", "message": "not found", "details": {}}})
        except DomainError as error:
            self._json(400, {"error": {"code": error.code, "message": error.message, "details": error.details}})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            payload = self._request_payload()
            if path in {"/api/question-bank/preview", "/api/question-bank/import"}:
                question_bank_payload = self._question_bank_payload(payload)
                if path.endswith("/preview"):
                    return self._json(200, {"data": self.store.preview_question_bank(question_bank_payload)})
                return self._json(200, {"data": self.store.import_question_bank(question_bank_payload)})
            if isinstance(payload, tuple):
                values, files = payload
                if path == "/api/capture/source":
                    return self._json(200, {"data": self.store.capture_source(values, files)})
                if path == "/api/intake/batches":
                    return self._json(200, {"data": self.store.create_intake_batch(files, values.get("subject_key") or None, values.get("course_id") or None)})
                if path == "/api/question-bank/import":
                    if not files:
                        raise DomainError("invalid_question_bank", "CSV or JSON file is required")
                    upload = files[0]
                    try:
                        raw = (upload.get("data") or b"").decode("utf-8-sig")
                        if (upload.get("filename") or "").lower().endswith(".json"):
                            parsed = json.loads(raw)
                            rows = parsed.get("items", parsed) if isinstance(parsed, dict) else parsed
                        else:
                            rows = list(csv.DictReader(io.StringIO(raw)))
                    except (UnicodeDecodeError, ValueError, csv.Error) as error:
                        raise DomainError("invalid_question_bank", f"cannot parse question bank: {error}")
                    if not isinstance(rows, list):
                        raise DomainError("invalid_question_bank", "question bank must contain a list of rows")
                    fallback_course = values.get("course_id") or None
                    rows = [{**as_dict(row), **({"course_id": fallback_course} if fallback_course and not row.get("course_id") else {})} for row in rows]
                    return self._json(200, {"data": self.store.import_question_bank({"items": rows})})
                if path.startswith("/api/wrong-questions/") and path.endswith("/redo"):
                    question_id = path[len("/api/wrong-questions/"):-len("/redo")].strip("/")
                    return self._json(200, {"data": self.store.redo_upload(question_id, files, values)})
                if path.startswith("/api/intake/") and path.endswith("/assets"):
                    intake_id = path[len("/api/intake/"):-len("/assets")].strip("/")
                    return self._json(200, {"data": self.store.append_intake_assets(intake_id, files)})
                payload = values
            if path == "/api/intake/batches":
                return self._json(200, {"data": self.store.create_intake_batch([], payload.get("subject_key"), payload.get("course_id"))})
            if path == "/api/courses":
                return self._json(200, {"data": self.store.create_course(payload)})
            if path == "/api/knowledge-nodes":
                return self._json(200, {"data": self.store.create_knowledge_node(payload)})
            if path == "/api/question-bank/import":
                return self._json(200, {"data": self.store.import_question_bank(payload)})
            if path.startswith("/api/question-bank/") and path.endswith("/answer"):
                item_id = path[len("/api/question-bank/"):-len("/answer")].strip("/")
                return self._json(200, {"data": self.store.answer_question_bank(item_id, payload)})
            if path.startswith("/api/question-bank/") and path.endswith("/start"):
                item_id = path[len("/api/question-bank/"):-len("/start")].strip("/")
                return self._json(200, {"data": self.store.start_question_bank_item(item_id)})
            if path.startswith("/api/wrong-questions/") and path.endswith("/similar"):
                question_id = path[len("/api/wrong-questions/"):-len("/similar")].strip("/")
                return self._json(200, {"data": self.store.similar_question_bank(question_id, payload)})
            if path.startswith("/api/wrong-questions/") and path.endswith("/redo"):
                question_id = path[len("/api/wrong-questions/"):-len("/redo")].strip("/")
                return self._json(200, {"data": self.store.redo_upload(question_id, [], payload)})
            if path.startswith("/api/attempts/") and path.endswith("/submit"):
                attempt_id = path[len("/api/attempts/"):-len("/submit")].strip("/")
                return self._json(200, {"data": self.store.submit_attempt(attempt_id, payload)})
            if path.startswith("/api/intake/") and path.endswith("/analyze"):
                intake_id = path[len("/api/intake/"):-len("/analyze")].strip("/")
                return self._json(200, {"data": self.store.analyze_intake(intake_id)})
            if path.startswith("/api/intake/") and path.endswith("/resolve"):
                intake_id = path[len("/api/intake/"):-len("/resolve")].strip("/")
                return self._json(200, {"data": self.store.resolve_intake(intake_id)})
            if path.startswith("/api/intake/") and path.endswith("/confirm"):
                intake_id = path[len("/api/intake/"):-len("/confirm")].strip("/")
                return self._json(200, {"data": self.store.confirm_intake(intake_id)})
            if path == "/api/seed":
                return self._json(200, {"data": self.store.seed_p0(payload.get("as_of"), bool(payload.get("force")))})
            if path == "/api/capture/source":
                return self._json(200, {"data": self.store.capture_source(payload)})
            if path == "/api/enrich":
                return self._json(200, {"data": self.store.enrich_source(payload.get("source_artifact_id"))})
            if path == "/api/capture/question":
                return self._json(200, {"data": self.store.capture_question(payload)})
            if path == "/api/intake/candidates":
                return self._json(200, {"data": self.store.create_intake_candidate(payload)})
            if path == "/api/answer":
                return self._json(200, {"data": self.store.answer_question(payload)})
            if path == "/api/practice/similar":
                return self._json(200, {"data": self.store.similar_practice(payload)})
            if path == "/api/question-source-link":
                return self._json(200, {"data": self.store.link_question_source(payload)})
            if path == "/api/schedule":
                return self._json(200, {"data": self.store.schedule_review(payload)})
            if path == "/api/start":
                return self._json(200, {"data": self.store.start_review(payload.get("review_task_id"))})
            if path.startswith("/api/session/"):
                return self._json(200, {"data": self.store.session_action(path.rsplit("/", 1)[1], payload.get("action"), payload.get("payload"))})
            if path == "/api/assess":
                return self._json(200, {"data": self.store.assess_attempt(payload)})
            self._json(404, {"error": {"code": "not_found", "message": "not found", "details": {}}})
        except DomainError as error:
            self._json(400, {"error": {"code": error.code, "message": error.message, "details": error.details}})
        except Exception as error:
            self._json(400, {"error": {"code": "request_error", "message": str(error), "details": {}}})

    def do_PATCH(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/settings/model":
                length = int(self.headers.get("Content-Length", 0))
                payload = loads(self.rfile.read(length) or b"{}", {})
                return self._json(200, {"data": self.store.patch_model_settings(payload)})
            if path == "/api/question-bank":
                length = int(self.headers.get("Content-Length", 0))
                payload = loads(self.rfile.read(length) or b"{}", {})
                return self._json(200, {"data": self.store.bulk_update_question_bank(payload)})
            if path.startswith("/api/knowledge-nodes/"):
                length = int(self.headers.get("Content-Length", 0))
                payload = loads(self.rfile.read(length) or b"{}", {})
                node_id = path[len("/api/knowledge-nodes/"):].strip("/")
                return self._json(200, {"data": self.store.update_knowledge_node(node_id, payload)})
            if path.startswith("/api/question-bank/"):
                length = int(self.headers.get("Content-Length", 0))
                payload = loads(self.rfile.read(length) or b"{}", {})
                item_id = path[len("/api/question-bank/"):].strip("/")
                return self._json(200, {"data": self.store.update_question_bank_item(item_id, payload)})
            if path.startswith("/api/attempts/"):
                length = int(self.headers.get("Content-Length", 0))
                payload = loads(self.rfile.read(length) or b"{}", {})
                attempt_id = path[len("/api/attempts/"):].strip("/")
                return self._json(200, {"data": self.store.patch_attempt_draft(attempt_id, payload)})
            if path.startswith("/api/intake/"):
                length = int(self.headers.get("Content-Length", 0))
                payload = loads(self.rfile.read(length) or b"{}", {})
                intake_id = path[len("/api/intake/"):].strip("/")
                return self._json(200, {"data": self.store.patch_intake(intake_id, payload)})
            self._json(404, {"error": {"code": "not_found", "message": "not found", "details": {}}})
        except DomainError as error:
            self._json(400, {"error": {"code": error.code, "message": error.message, "details": error.details}})
        except Exception as error:
            self._json(400, {"error": {"code": "request_error", "message": str(error), "details": {}}})

    def log_message(self, *_):
        pass


class LocalHTTPServer(ThreadingHTTPServer):
    # A browser may open several local requests at once; keep a small buffer
    # without introducing a separate request queue or worker layer.
    request_queue_size = 32


def serve(db=DB_PATH):
    Handler.store = Store(db)
    # Reads and writes share the Store RLock, so threaded HTTP requests can
    # use the one local SQLite connection without a pool or second protocol.
    LocalHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB_PATH))
    sub = parser.add_subparsers(dest="command")
    seed = sub.add_parser("seed")
    seed.add_argument("--as-of")
    seed.add_argument("--force", action="store_true")
    scenario = sub.add_parser("scenario")
    scenario.add_argument("scenario_id")
    enrich = sub.add_parser("enrich")
    enrich.add_argument("--artifact-id", required=True)
    sub.add_parser("serve")
    args = parser.parse_args()
    if args.command == "seed":
        print(json.dumps(Store(Path(args.db)).seed_p0(args.as_of, args.force), ensure_ascii=False, indent=2))
    elif args.command == "scenario":
        print(json.dumps(run_scenario(args.scenario_id), ensure_ascii=False, indent=2))
    elif args.command == "enrich":
        print(json.dumps(Store(Path(args.db)).enrich_source(args.artifact_id), ensure_ascii=False, indent=2))
    else:
        serve(Path(args.db))


if __name__ == "__main__":
    main()

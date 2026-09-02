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
import datetime as dt
import io
import json
import mimetypes
import sqlite3
import tempfile
import threading
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
SCHEDULE_POLICY = {
    "initial_error_delay_days": 2,
    "incomplete_attempt_delay_days": 2,
    "manual_declaration_delay_days": 2,
    "awaiting_assessment_delay_days": 3,
    "assisted_retry_delay_days": 3,
    "retry_after_fail_delay_days": 2,
    "independent_confirmation_days": [7, 21],
}


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


class DomainError(Exception):
    def __init__(self, code: str, message: str, details=None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class Store:
    """The ten-table learning ledger and its small command surface."""

    def __init__(self, path=DB_PATH, clock=None):
        self.path = Path(path)
        self.clock = clock or now_utc
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        # schema.sql is the only structural source. Re-running its CREATE IF
        # NOT EXISTS statements avoids a second startup contract.
        self.conn.executescript((ROOT / "schema.sql").read_text())
        self._ensure_source_fts()
        self.lock = threading.RLock()

    def _ensure_source_fts(self):
        self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS SourcePassageFTS USING fts5(source_passage_id UNINDEXED, source_artifact_id UNINDEXED, text, tokenize='trigram')")
        # Backfill rows created by an older app version without introducing a
        # migration framework.  IDs remain those of SourcePassage.
        passage_count = self.conn.execute("SELECT COUNT(*) FROM SourcePassage").fetchone()[0]
        fts_count = self.conn.execute("SELECT COUNT(*) FROM SourcePassageFTS").fetchone()[0]
        if passage_count != fts_count:
            self.conn.execute("DELETE FROM SourcePassageFTS")
            self.conn.execute("INSERT INTO SourcePassageFTS(source_passage_id,source_artifact_id,text) SELECT source_passage_id,source_artifact_id,COALESCE(text,'') FROM SourcePassage ORDER BY source_artifact_id,ordinal")
            # The backfill is part of startup repair.  Commit it before any
            # normal write transaction so BEGIN IMMEDIATE can start cleanly.
            self.conn.commit()

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
            grading = as_dict(manifest.get("grading_reference"))
            help_fixture = as_list(manifest.get("help_content_fixture"))
            presentation = self._presentation(prompt_data.get("presentation_snapshot"))
            self.conn.execute("INSERT INTO CoursePackRelease(release_id,course_key,created_at) VALUES(?,?,?)", (release, as_text(release_data.get("course_key"), "signals_and_systems"), as_of))
            self.conn.execute("INSERT INTO LearningObjective(learning_objective_id,course_pack_release_id,name,description,observable_criteria,created_at) VALUES(?,?,?,?,?,?)", (objective, release, as_text(lo_data.get("name"), "待补充学习目标"), as_text(lo_data.get("description")), dumps(criteria), as_of))
            self.conn.execute("INSERT INTO Question(question_id,course_pack_release_id,current_question_revision_id,lifecycle_state,created_at) VALUES(?,?,?,?,?)", (question, release, None, "active", as_of))
            self.conn.execute("INSERT INTO QuestionRevision(question_revision_id,question_id,revision_no,revision_state,supersedes_revision_id,current_review_prompt_revision_id,question_units,objective_mapping_snapshot,grading_reference_fixture_snapshot,help_content_fixture_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (revision, question, 1, "confirmed", None, None, dumps(units), dumps(mapping), dumps(grading), dumps(help_fixture), as_of))
            self.conn.execute("INSERT INTO ReviewPromptRevision(review_prompt_revision_id,question_id,question_revision_id,revision_no,presentation_snapshot,unresolved_critical_ambiguities,leakage_state,revision_state,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (prompt, question, revision, 1, dumps(presentation), dumps([]), "clean", "ready", as_of))
            self.conn.execute("UPDATE Question SET current_question_revision_id=? WHERE question_id=?", (revision, question))
            self.conn.execute("UPDATE QuestionRevision SET current_review_prompt_revision_id=? WHERE question_revision_id=?", (prompt, revision))

            submitted_at = add_days(as_of, int(history.get("submitted_at_offset_days", -2)))
            debt_kind = history.get("initial_debt_claim") if history.get("initial_debt_claim") in {"wrong", "incomplete", "uncertain"} else "incomplete"
            response = self._draft({"response_text": history.get("response_text"), "response_selections": history.get("response_selections"), "response_assets": history.get("response_assets"), "completion_claim": history.get("completion_claim"), "external_help_reported": False})
            attempt = uid("attempt")
            self.conn.execute("INSERT INTO Attempt(attempt_id,question_id,question_revision_id,review_session_id,origin_kind,submission_state,submitted_at,completion_claim,initial_debt_claim,initial_debt_claim_basis,initial_debt_claim_captured_at,response_snapshot,assistance_state,external_help_reported,submit_event_ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt, question, revision, None, "initial", "submitted", submitted_at, response["completion_claim"], debt_kind, "seed_fixture", submitted_at, dumps({**response, "assistance_state": "none_observed"}), "none_observed", 0, None, as_of))
            reason = {"wrong": "initial_error", "incomplete": "incomplete_attempt", "uncertain": "manual_declaration"}[debt_kind]
            task = self._schedule_in_tx(question, revision, prompt, {"trigger_ref": uid("trigger"), "trigger_kind": "attempt", "trigger_reason_kind": reason, "attempt_id": attempt, "source_question_revision_id": revision, "captured_at": submitted_at}, as_of)
            self.commit()
            return {"release_id": release, "objective_id": objective, "question_id": question, "question_revision_id": revision, "prompt_id": prompt, "seed_attempt_id": attempt, "review_task_id": task["review_task_id"], "as_of": as_of}
        except Exception:
            self.rollback()
            raise

    # ---------- real, write-first capture ----------

    def capture_source(self, payload):
        """Persist any source payload before attempting to understand it."""
        payload = payload if isinstance(payload, dict) else {"raw_payload": payload}
        created = self.clock()
        artifact_id = uid("source")
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
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind:
            kind = "image" if payload.get("image_path") else "text" if isinstance(raw_text, str) else "file"
        source_name = payload.get("source_name")
        if not isinstance(source_name, str):
            source_name = payload.get("name") if isinstance(payload.get("name"), str) else ""
        self.begin()
        try:
            self.conn.execute(
                "INSERT INTO SourceArtifact(source_artifact_id,kind,source_name,stored_path,raw_text,raw_payload,parse_state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (artifact_id, kind, source_name, stored_path if isinstance(stored_path, str) else None,
                 raw_text if isinstance(raw_text, str) else None, dumps(payload), "pending", created),
            )
            self.commit()
            return {"source_artifact_id": artifact_id, "parse_state": "pending"}
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

    def _intake_detail(self, intake_id):
        row = self.one("SELECT i.*, b.subject_key FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id WHERE i.intake_id=?", (intake_id,))
        if not row:
            raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
        assets = [self._asset_dict(r) for r in self.all("SELECT * FROM ImageAsset WHERE batch_id=? ORDER BY ordinal, created_at, asset_id", (row["batch_id"],))]
        result = dict(row)
        result["draft_fields"] = loads(result.get("draft_fields"), {})
        result["assets"] = assets
        result["batch_id"] = row["batch_id"]
        return result

    def _save_uploads(self, intake_id, files, subject_key=None):
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
            batch_id, intake_id = uid("batch"), uid("intake")
            self.conn.execute("INSERT INTO CaptureBatch(batch_id,subject_key,created_at) VALUES(?,?,?)", (batch_id, subject_key or None, created))
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
            role = file.get("role") if file.get("role") in IMAGE_ROLES else None
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
        self.commit()
        return self._intake_detail(intake_id)

    def create_intake_batch(self, files, subject_key=None):
        self.begin()
        try:
            return self._save_uploads(None, files, subject_key)
        except Exception:
            self.rollback()
            raise

    def append_intake_assets(self, intake_id, files):
        self.begin()
        try:
            return self._save_uploads(intake_id, files)
        except Exception:
            self.rollback()
            raise

    def list_intakes(self):
        rows = self.all("SELECT i.*, b.subject_key, (SELECT COUNT(*) FROM ImageAsset a WHERE a.batch_id=i.batch_id) AS asset_count FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id ORDER BY i.updated_at DESC, i.created_at DESC")
        return [{**dict(r), "draft_fields": loads(r["draft_fields"], {})} for r in rows]

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
            if isinstance(payload.get("draft_fields"), dict):
                draft.update(payload["draft_fields"])
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
                    if asset["path"]:
                        try: (ROOT / asset["path"]).unlink(missing_ok=True)
                        except OSError: pass
                    self.conn.execute("DELETE FROM ImageAsset WHERE asset_id=?", (asset_id,))
                    continue
                role = change.get("role")
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
            self.commit()
            return self._intake_detail(intake_id)
        except Exception:
            self.rollback()
            raise

    def media_asset(self, asset_id):
        row = self.one("SELECT * FROM ImageAsset WHERE asset_id=?", (asset_id,))
        if not row or not row["path"] or row["state"] != "saved":
            return None
        path = (ROOT / row["path"]).resolve()
        try: path.relative_to((ROOT / "objects").resolve())
        except ValueError: return None
        if not path.is_file(): return None
        return path, (row["mime"] or "application/octet-stream"), row["original_filename"]

    def enrich_source(self, artifact_id):
        self.begin()
        try:
            artifact = self.one("SELECT * FROM SourceArtifact WHERE source_artifact_id=?", (artifact_id,))
            if not artifact:
                raise DomainError("not_found", "source artifact not found", {"source_artifact_id": artifact_id})
            path = artifact["stored_path"]

            def mark_error(message):
                payload = loads(artifact["raw_payload"], {})
                if not isinstance(payload, dict):
                    payload = {"raw_payload": payload}
                payload["parse_error"] = message
                self.conn.execute("UPDATE SourceArtifact SET raw_payload=?,parse_state='error' WHERE source_artifact_id=?", (dumps(payload), artifact_id))
                self.commit()
                return {"source_artifact_id": artifact_id, "parse_state": "error", "error": message, "source_passage_ids": []}

            is_pdf = isinstance(path, str) and path.lower().endswith(".pdf")
            passages = []
            if is_pdf:
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(path)
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
                    if not passages:
                        message = "PDF has no extractable text layer"
                        if page_errors:
                            message += "; " + "; ".join(page_errors)
                        return mark_error(message)
                    text_value = "\n\n".join(piece for piece, _ in passages)
                except Exception as error:
                    return mark_error(f"PDF text extraction failed: {error}")
            else:
                text_value = artifact["raw_text"]
                if not isinstance(text_value, str) or not text_value:
                    if path:
                        try:
                            text_value = Path(path).read_text(encoding="utf-8")
                        except Exception as error:
                            return mark_error(str(error))
                if not isinstance(text_value, str) or not text_value:
                    return mark_error("no plain text available")
                passages = [(piece.strip(), None) for piece in text_value.replace("\r\n", "\n").split("\n\n") if piece.strip()]
                if not passages:
                    passages = [(text_value, None)]
            existing = {
                row["ordinal"]: row
                for row in self.all(
                    "SELECT * FROM SourcePassage WHERE source_artifact_id=?",
                    (artifact_id,),
                )
            }
            passage_ids = []
            for ordinal, (piece, page_no) in enumerate(passages, 1):
                current = existing.get(ordinal)
                passage_id = current["source_passage_id"] if current else uid("passage")
                passage_ids.append(passage_id)
                locator = {"ordinal": ordinal}
                if page_no is not None:
                    locator["page_no"] = page_no
                if current:
                    self.conn.execute(
                        "UPDATE SourcePassage SET text=?,page_no=?,locator_json=? WHERE source_passage_id=?",
                        (piece, page_no, dumps(locator), passage_id),
                    )
                else:
                    self.conn.execute("INSERT INTO SourcePassage(source_passage_id,source_artifact_id,ordinal,text,page_no,bbox,locator_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (passage_id, artifact_id, ordinal, piece, page_no, None, dumps(locator), self.clock()))
            # Re-running enrichment must not remove historical passages: a
            # QuestionSourceLink may still cite a trailing row.
            self.conn.execute("DELETE FROM SourcePassageFTS WHERE source_artifact_id=?", (artifact_id,))
            self.conn.execute("INSERT INTO SourcePassageFTS(source_passage_id,source_artifact_id,text) SELECT source_passage_id,source_artifact_id,COALESCE(text,'') FROM SourcePassage WHERE source_artifact_id=? ORDER BY ordinal", (artifact_id,))
            self.conn.execute("UPDATE SourceArtifact SET raw_text=?,parse_state='ready' WHERE source_artifact_id=?", (text_value, artifact_id))
            self.commit()
            return {"source_artifact_id": artifact_id, "parse_state": "ready", "source_passage_ids": passage_ids}
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

    def search_sources(self, query):
        """Search passages with trigram FTS for 3+ chars and LIKE otherwise."""
        query = query if isinstance(query, str) else ""
        query = query.strip()
        if not query:
            return []
        if len(query) >= 3:
            try:
                match = '"' + query.replace('"', '""') + '"'
                rows = self.all(
                    "SELECT p.source_passage_id,p.source_artifact_id,p.text,p.page_no,p.bbox,p.locator_json "
                    "FROM SourcePassage p JOIN SourcePassageFTS f ON f.source_passage_id=p.source_passage_id "
                    "WHERE SourcePassageFTS MATCH ? ORDER BY p.source_artifact_id,p.ordinal,p.source_passage_id",
                    (match,),
                )
            except sqlite3.OperationalError:
                rows = []
        else:
            rows = self.all(
                "SELECT source_passage_id,source_artifact_id,text,page_no,bbox,locator_json "
                "FROM SourcePassage WHERE COALESCE(text,'') LIKE ? ORDER BY source_artifact_id,ordinal,source_passage_id",
                (f"%{query}%",),
            )
        return [dict(row) for row in rows]

    def _llm_chat(self, messages):
        base_url = os.environ.get("LLM_BASE_URL", "").strip()
        api_key = os.environ.get("LLM_API_KEY", "").strip()
        model = os.environ.get("LLM_MODEL", "").strip()
        provider_name = urlparse(base_url).netloc or base_url.rstrip("/")
        model_provider = f"{provider_name}/{model}" if (base_url or model) else None
        if not base_url or not model:
            return "", model_provider, False, "LLM_BASE_URL or LLM_MODEL is not configured"
        normalized_base = base_url.rstrip("/")
        if normalized_base.endswith("/chat/completions"):
            endpoint = normalized_base
        elif normalized_base.endswith("/v1"):
            endpoint = normalized_base + "/chat/completions"
        else:
            endpoint = normalized_base + "/v1/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=dumps({"model": model, "messages": messages}).encode("utf-8"),
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = loads(response.read(), {})
            choices = body.get("choices") if isinstance(body, dict) else None
            message = as_dict(choices[0]).get("message") if isinstance(choices, list) and choices else {}
            answer = message.get("content") if isinstance(message, dict) else ""
            if isinstance(answer, list):
                answer = "".join(as_text(as_dict(part).get("text")) for part in answer)
            if not isinstance(answer, str):
                raise ValueError("chat completion did not contain text content")
            return answer, model_provider, True, None
        except (OSError, ValueError, TypeError, IndexError) as error:
            return "", model_provider, False, str(error)

    def _provider_config(self, prefix="LLM"):
        protocol = os.environ.get(f"{prefix}_PROTOCOL", "openai_chat").strip() or "openai_chat"
        base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip()
        api_key = os.environ.get(f"{prefix}_API_KEY", "").strip()
        model = os.environ.get(f"{prefix}_MODEL", "").strip()
        return protocol, base_url, api_key, model

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

    def _extract_analysis(self, raw, subject_hint=None):
        import re
        fields = {"question_text":"", "reference_answer":"", "subject_key": subject_hint if subject_hint in SUBJECT_KEYS else None, "chapter":"", "knowledge_point":"", "question_type":"", "error_reason":"", "error_breakpoint":"", "correct_approach":""}
        aliases = {
            "question_text": r"(?:题面|题目(?:要求)?|question(?:_text)?)",
            "reference_answer": r"(?:标准答案|模型答案|参考答案|reference[_ ]?answer)",
            "subject_key": r"(?:科目|subject[_ ]?key)", "chapter": r"(?:章节|chapter)", "knowledge_point": r"(?:知识点|knowledge[_ ]?point)", "question_type": r"(?:题型|question[_ ]?type)",
            "error_reason": r"(?:做错原因|错误原因|error[_ ]?reason)", "error_breakpoint": r"(?:解题断点|首次偏离|error[_ ]?breakpoint)", "correct_approach": r"(?:正确思路|correct[_ ]?approach)"
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
            fields[key] = value
        return fields

    def analyze_intake(self, intake_id):
        row = self.one("SELECT i.*,b.subject_key FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id WHERE i.intake_id=?", (intake_id,))
        if not row: raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
        assets = self.all("SELECT * FROM ImageAsset WHERE batch_id=? AND state='saved' ORDER BY ordinal,created_at,asset_id", (row["batch_id"],))
        if not assets: raise DomainError("no_assets", "intake has no saved images", {"intake_id": intake_id})
        draft = loads(row["draft_fields"], {})
        draft.update({"analysis_status": "analyzing", "analysis_error": ""})
        self.begin(); self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft), self.clock(), intake_id)); self.commit()
        prompt_lines = ["你是错题分析助手。请阅读按顺序提供的图片，输出普通文本或 Markdown，并尽量使用以下标题：题面、标准答案、科目、章节、知识点、题型、做错原因、解题断点、正确思路。", "区分题目要求、我的解题步骤、标准答案/模型答案；指出我具体在哪一步开始偏离，错误属于概念、条件理解、公式使用、计算、推理、表达或其他原因。", "不确定的内容标记为‘待确认’，不要猜测填满字段；分类字段可以为空。"]
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
        attempts = [("LLM", self._provider_config("LLM")), ("LLM_FALLBACK", self._provider_config("LLM_FALLBACK"))]
        errors = []
        raw = ""
        for label, config in attempts:
            try:
                raw = self._call_provider(*config, prompt, image_parts)
                break
            except Exception as error:
                errors.append(f"{label}: {error}")
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
        return self._intake_detail(intake_id)

    def _subject_context(self, subject_key, created):
        """Return a course context for the confirmed subject, creating only
        the minimal local release/objective needed by the existing ledger."""
        subject_key = subject_key if subject_key in SUBJECT_KEYS else None
        course_key = subject_key or "capture_unspecified"
        release = self.one("SELECT * FROM CoursePackRelease WHERE course_key=?", (course_key,))
        if not release:
            release_id, objective_id = uid("release"), uid("lo")
            self.conn.execute("INSERT INTO CoursePackRelease(release_id,course_key,created_at) VALUES(?,?,?)", (release_id, course_key, created))
            self.conn.execute("INSERT INTO LearningObjective(learning_objective_id,course_pack_release_id,name,description,observable_criteria,created_at) VALUES(?,?,?,?,?,?)", (objective_id, release_id, f"{subject_key or '未指定'}待补充学习目标", "", dumps(["待补充"]), created))
            return release_id, objective_id
        objective = self.one("SELECT learning_objective_id FROM LearningObjective WHERE course_pack_release_id=? ORDER BY created_at LIMIT 1", (release["release_id"],))
        return release["release_id"], objective["learning_objective_id"] if objective else None

    def resolve_intake(self, intake_id):
        row = self.one("SELECT i.*,b.subject_key FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id WHERE i.intake_id=?", (intake_id,))
        if not row:
            raise DomainError("not_found", "intake not found", {"intake_id": intake_id})
        draft = loads(row["draft_fields"], {})
        query = as_text(draft.get("question_text")).strip() or as_text(draft.get("raw_analysis")).strip()
        candidates, seen = [], set()
        if query:
            terms = [part.strip() for part in query.replace("\n", " ").split() if len(part.strip()) >= 2][:8]
            fts_rows = self.search_sources(query[:120])
            for item in fts_rows[:8]:
                key = item.get("source_passage_id")
                if key in seen: continue
                seen.add(key)
                candidates.append({"kind":"source", "source_passage_id":key, "source_artifact_id":item.get("source_artifact_id"), "text":item.get("text"), "page_no":item.get("page_no"), "locator":item.get("locator_json")})
            like = "%" + (terms[0] if terms else query[:30]) + "%"
            qrows = self.all("SELECT q.question_id,qr.question_revision_id,qr.question_units,qr.grading_reference_fixture_snapshot FROM Question q JOIN QuestionRevision qr ON qr.question_revision_id=q.current_question_revision_id WHERE qr.revision_state='confirmed' AND (qr.question_units LIKE ? OR qr.grading_reference_fixture_snapshot LIKE ?) ORDER BY q.created_at DESC LIMIT 8", (like, like))
            for item in qrows:
                key = item["question_id"]
                if key in seen: continue
                seen.add(key)
                units = loads(item["question_units"], {})
                grading = loads(item["grading_reference_fixture_snapshot"], {})
                question_text = units[0].get("question_text", "") if isinstance(units, list) and units and isinstance(units[0], dict) else units.get("question_text", "") if isinstance(units, dict) else ""
                candidates.append({"kind":"question", "question_id":key, "question_revision_id":item["question_revision_id"], "question_text":question_text, "grading":{"reference_answer":as_text(grading.get("reference_answer"))}})
        subject = draft.get("subject_key") if draft.get("subject_key") in SUBJECT_KEYS else row["subject_key"]
        source_refs = [{k:c.get(k) for k in ("source_passage_id","source_artifact_id","locator") if c.get(k)} for c in candidates if c.get("kind")=="source"]
        tag_candidates = [{"subject_key":subject, "chapter":draft.get("chapter") or None, "knowledge_point":draft.get("knowledge_point") or None, "question_type":draft.get("question_type") or None}]
        has_question = any(c.get("kind")=="question" for c in candidates)
        has_source = any(c.get("kind")=="source" for c in candidates)
        draft.update({"resolution_kind":"matched" if has_question else "model", "resolution_label":"匹配题目" if has_question else "资料参考" if has_source else "待补充", "match_candidates":candidates, "source_refs":source_refs, "tag_candidates":tag_candidates, "answer_origin":"matched" if has_question else "reference_image" if any(a["role"]=="reference" for a in self.all("SELECT role FROM ImageAsset WHERE batch_id=?", (row["batch_id"],))) else "model"})
        self.begin()
        try:
            self.conn.execute("UPDATE IntakeItem SET draft_fields=?,updated_at=? WHERE intake_id=?", (dumps(draft), self.clock(), intake_id)); self.commit()
        except Exception:
            self.rollback(); raise
        return self._intake_detail(intake_id)

    def confirm_intake(self, intake_id):
        row = self.one("SELECT i.*,b.subject_key FROM IntakeItem i JOIN CaptureBatch b ON b.batch_id=i.batch_id WHERE i.intake_id=?", (intake_id,))
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
            release_id, objective_id = self._subject_context(subject, created)
            question_id, revision_id, prompt_id = uid("q"), uid("qr"), uid("qpr")
            assets = self.all("SELECT * FROM ImageAsset WHERE batch_id=? AND state='saved' AND path IS NOT NULL ORDER BY ordinal,created_at,asset_id", (row["batch_id"],))
            question_assets = [a for a in assets if a["role"] in ("question", "mixed")]
            asset_refs = [{"asset_id":a["asset_id"],"role":a["role"],"ordinal":a["ordinal"],"original_filename":a["original_filename"]} for a in assets]
            presentation_refs = [{"asset_id":a["asset_id"],"role":a["role"],"ordinal":a["ordinal"],"original_filename":a["original_filename"]} for a in question_assets]
            question_text = as_text(draft.get("question_text"))
            units = [{"unit_ref":"whole","label":"整题","question_text":question_text,"intake_id":intake_id,"asset_ids":[a["asset_id"] for a in question_assets]}]
            mapping = [{"objective_ref":"obj-1","learning_objective_id":objective_id,"role":"measured","question_unit_refs":["whole"],"origin_kind":"intake_confirm","captured_at":created}]
            selected_source_ids = draft.get("selected_source_passage_ids") if isinstance(draft.get("selected_source_passage_ids"), list) else []
            grading = {"reference_answer":required["reference_answer"],"error_reason":required["error_reason"],"error_breakpoint":required["error_breakpoint"],"correct_approach":as_text(draft.get("correct_approach")),"subject_key":subject,"chapter":draft.get("chapter"),"knowledge_point":draft.get("knowledge_point"),"question_type":draft.get("question_type"),"intake_id":intake_id,"asset_refs":asset_refs,"selected_question_id":draft.get("selected_question_id"),"selected_source_passage_ids":selected_source_ids,"question_image_status":"已保存题面" if question_assets else "待补题面"}
            presentation = {"schema_version":SNAPSHOT,"content":question_text,"blocks":[{"block_ref":"question","kind":"text","content":question_text,"leakage_state":"clean"}],"asset_refs":presentation_refs}
            self.conn.execute("INSERT INTO Question(question_id,course_pack_release_id,current_question_revision_id,lifecycle_state,created_at) VALUES(?,?,?,?,?)", (question_id,release_id,None,"active",created))
            self.conn.execute("INSERT INTO QuestionRevision(question_revision_id,question_id,revision_no,revision_state,supersedes_revision_id,current_review_prompt_revision_id,question_units,objective_mapping_snapshot,grading_reference_fixture_snapshot,help_content_fixture_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (revision_id,question_id,1,"confirmed",None,None,dumps(units),dumps(mapping),dumps(grading),dumps([]),created))
            self.conn.execute("INSERT INTO ReviewPromptRevision(review_prompt_revision_id,question_id,question_revision_id,revision_no,presentation_snapshot,unresolved_critical_ambiguities,leakage_state,revision_state,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (prompt_id,question_id,revision_id,1,dumps(presentation),dumps([]),"clean","ready",created))
            self.conn.execute("UPDATE Question SET current_question_revision_id=? WHERE question_id=?", (revision_id,question_id)); self.conn.execute("UPDATE QuestionRevision SET current_review_prompt_revision_id=? WHERE question_revision_id=?", (prompt_id,revision_id))
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

    def _wrong_dto(self, question_id):
        question = self.one("SELECT * FROM Question WHERE question_id=?", (question_id,))
        if not question: return None
        revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=? AND revision_state='confirmed'", (question["current_question_revision_id"],))
        if not revision: return None
        grading = loads(revision["grading_reference_fixture_snapshot"], {})
        if not grading.get("intake_id"): return None
        prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (revision["current_review_prompt_revision_id"],))
        if not prompt:
            prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE question_revision_id=? AND revision_state='ready' ORDER BY revision_no DESC LIMIT 1", (revision["question_revision_id"],))
        presentation = loads(prompt["presentation_snapshot"], {}) if prompt else {}
        refs = presentation.get("asset_refs") if isinstance(presentation.get("asset_refs"), list) else []
        assets = []
        for ref in refs:
            asset = self.one("SELECT * FROM ImageAsset WHERE asset_id=? AND state='saved' AND path IS NOT NULL", (ref.get("asset_id"),))
            if asset: assets.append(self._asset_dict(asset))
        task = self.one("SELECT * FROM ReviewTask WHERE question_id=? AND status='open' ORDER BY due_at LIMIT 1", (question_id,))
        sources = self.get_question_sources(question_id)
        return {"question_id":question_id,"question":dict(question),"question_revision":dict(revision),"question_text":as_text((presentation.get("content") if isinstance(presentation,dict) else "")),"presentation":presentation,"assets":assets,"grading":grading,"sources":sources,"next_due_at":task["due_at"] if task else None,"review_task_id":task["review_task_id"] if task else None}

    def list_wrong_questions(self):
        rows = self.all("SELECT q.question_id FROM Question q JOIN QuestionRevision qr ON qr.question_revision_id=q.current_question_revision_id WHERE qr.revision_state='confirmed' AND qr.grading_reference_fixture_snapshot LIKE '%intake_id%'")
        result = []
        for row in rows:
            item = self._wrong_dto(row["question_id"])
            if item:
                result.append({"question_id":item["question_id"],"question_text":item["question_text"],"subject_key":item["grading"].get("subject_key"),"asset_count":len(item["assets"]),"reference_answer":item["grading"].get("reference_answer"),"error_reason":item["grading"].get("error_reason"),"next_due_at":item["next_due_at"]})
        return result

    def get_wrong_question(self, question_id):
        item = self._wrong_dto(question_id)
        if not item: raise DomainError("not_found", "wrong question not found", {"question_id":question_id})
        return item

    def answer_question(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        requested_question_id = payload.get("question_id") if isinstance(payload.get("question_id"), str) else None
        query = payload.get("query") if isinstance(payload.get("query"), str) else ""
        query = query.strip()
        question_id = None
        question_text = ""
        linked = []
        if requested_question_id:
            question = self.one("SELECT * FROM Question WHERE question_id=?", (requested_question_id,))
            if question:
                question_id = requested_question_id
                revision = self.one("SELECT * FROM QuestionRevision WHERE question_revision_id=?", (question["current_question_revision_id"],))
                prompt = self.one("SELECT * FROM ReviewPromptRevision WHERE review_prompt_revision_id=?", (revision["current_review_prompt_revision_id"],)) if revision else None
                presentation = self._presentation(loads(prompt["presentation_snapshot"], {})) if prompt else {}
                question_text = "\n".join(as_text(as_dict(block).get("content")) for block in as_list(presentation.get("blocks")))
                linked = self.get_question_sources(question_id)

        selected = []
        seen = set()
        for row in linked:
            passage_id = row.get("source_passage_id")
            if passage_id and passage_id not in seen:
                seen.add(passage_id)
                selected.append(dict(row))
        for row in self.search_sources(query):
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
        messages = [
            {"role": "system", "content": "请用普通文本回答问题。只把给定资料作为参考，不要编造引用编号。"},
            {"role": "user", "content": f"题面：{question_text or '（未提供题面）'}\n问题：{query or '（未提供问题）'}\n资料上下文：\n{context}"},
        ]
        answer_text, model_provider, available, error = self._llm_chat(messages)
        status = "unavailable" if not available else "grounded" if sources else "unlocated"
        answer_id = uid("answer")
        source_snapshot = {"question_id": question_id, "requested_question_id": requested_question_id, "query": query, "sources": sources, "context": context}
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
        reason = trigger.get("trigger_reason_kind", "manual_declaration")
        delays = {
            "initial_error": SCHEDULE_POLICY["initial_error_delay_days"],
            "incomplete_attempt": SCHEDULE_POLICY["incomplete_attempt_delay_days"],
            "manual_declaration": SCHEDULE_POLICY["manual_declaration_delay_days"],
            "awaiting_assessment": SCHEDULE_POLICY["awaiting_assessment_delay_days"],
            "assisted_retry": SCHEDULE_POLICY["assisted_retry_delay_days"],
            "retry_after_fail": SCHEDULE_POLICY["retry_after_fail_delay_days"],
            "spaced_confirmation": SCHEDULE_POLICY["independent_confirmation_days"][0],
        }
        delay = delays.get(reason, 2)
        if reason == "spaced_confirmation":
            evidence = self.one("SELECT learning_objective_id FROM EvidenceEvent WHERE evidence_event_id=?", (trigger.get("evidence_event_id"),))
            if evidence and self.one("SELECT COUNT(*) AS n FROM EvidenceEvent WHERE learning_objective_id=? AND performance_state='success'", (evidence["learning_objective_id"],))["n"] >= 2:
                delay = SCHEDULE_POLICY["independent_confirmation_days"][1]
        return add_days(as_text(trigger.get("captured_at"), self.clock()), delay)

    def _merge_triggers(self, snapshots):
        candidates = []
        for snapshot in as_list(snapshots):
            item = dict(as_dict(snapshot))
            reason = item.get("trigger_reason_kind") if item.get("trigger_reason_kind") in TASK_PRIORITY else "manual_declaration"
            item["trigger_reason_kind"] = reason
            candidates.append((TASK_PRIORITY.index(reason), self._trigger_due(item), item))
        if not candidates:
            return "manual_declaration", add_days(self.clock(), 2)
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
        for task in tasks:
            for old in as_list(loads(task["trigger_snapshots"], [])):
                if any(key in trigger and trigger.get(key) and old.get(key) == trigger.get(key) for key in ("attempt_id", "assessment_id", "evidence_event_id", "declaration_id")):
                    return dict(task)
        open_task = self.one("SELECT * FROM ReviewTask WHERE question_id=? AND status='open' ORDER BY created_at LIMIT 1", (question_id,))
        if open_task:
            snapshots = as_list(loads(open_task["trigger_snapshots"], []))
            snapshots.append(trigger)
            reason, merged_due = self._merge_triggers(snapshots)
            self.conn.execute("UPDATE ReviewTask SET trigger_snapshots=?,reason_kind=?,due_at=? WHERE review_task_id=?", (dumps(snapshots), reason, min(open_task["due_at"], merged_due), open_task["review_task_id"]))
            return dict(self.one("SELECT * FROM ReviewTask WHERE review_task_id=?", (open_task["review_task_id"],)))
        snapshots = as_list(carry_triggers) + [trigger]
        reason, due = self._merge_triggers(snapshots)
        task_id = uid("task")
        self.conn.execute("INSERT INTO ReviewTask(review_task_id,question_id,question_revision_id,review_prompt_revision_id,kind,reason_kind,trigger_snapshots,snapshot_schema_version,review_round,due_at,schedule_policy_version,created_at,status,completed_by_attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, question_id, revision_id, prompt_id, "closed_book_review", reason, dumps(snapshots), SNAPSHOT, max((task["review_round"] for task in tasks), default=0) + 1, due, "inline", as_of, "open", None))
        return dict(self.one("SELECT * FROM ReviewTask WHERE review_task_id=?", (task_id,)))

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
            _, revision, prompt = self._triplet(task["question_id"], task["question_revision_id"], task["review_prompt_revision_id"])
            session_id = uid("session")
            started = self.clock()
            presentation = self._presentation(loads(prompt["presentation_snapshot"], {}))
            eligibility = {"schema_version": SNAPSHOT, "task_due_at_snapshot": task["due_at"], "eligibility_checked_at": started, "clean_ready": all(block.get("leakage_state") == "clean" for block in presentation["blocks"])}
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
        return {"review_session_id": session["review_session_id"], "review_task_id": session["review_task_id"], "status": session["status"], "presentation_snapshot": self._presentation(loads(prompt["presentation_snapshot"], {}) if prompt else {}), "prompt_eligibility_snapshot": loads(session["prompt_eligibility_snapshot"], {}), "draft": loads(session["draft_payload_snapshot"], {}), "exposure_events": [{key: value for key, value in event.items() if key != "content"} for event in events], "assistance_state": "assisted" if events else "none_observed"}

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
                    return {"review_session_id": session_id, "status": "submitted", "attempt_id": session["submitted_attempt_id"]}
                draft = self._draft(payload.get("draft", loads(session["draft_payload_snapshot"], {})))
                events = as_list(loads(session["exposure_event_snapshots"], []))
                attempt_id = uid("attempt")
                response = {**draft, "assistance_state": "assisted" if events else "none_observed", "submit_event_ordinal": len(events) + 1}
                self.conn.execute("INSERT INTO Attempt(attempt_id,question_id,question_revision_id,review_session_id,origin_kind,submission_state,submitted_at,completion_claim,initial_debt_claim,initial_debt_claim_basis,initial_debt_claim_captured_at,response_snapshot,assistance_state,external_help_reported,submit_event_ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, session["question_id"], session["question_revision_id"], session_id, "review", "submitted", self.clock(), draft["completion_claim"], None, None, None, dumps(response), response["assistance_state"], int(draft["external_help_reported"]), response["submit_event_ordinal"], self.clock()))
                self.conn.execute("UPDATE ReviewSession SET status='submitted',submitted_attempt_id=?,ended_at=?,updated_at=?,draft_payload_snapshot=? WHERE review_session_id=?", (attempt_id, self.clock(), self.clock(), dumps(draft), session_id))
                self.conn.execute("UPDATE ReviewTask SET status='completed',completed_by_attempt_id=? WHERE review_task_id=?", (attempt_id, session["review_task_id"]))
                self.commit()
                return {"review_session_id": session_id, "status": "submitted", "attempt_id": attempt_id}
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

    def get_due_review(self):
        rows = self.all("SELECT * FROM ReviewItemProjection WHERE status='open' AND due_at<=?", (self.clock(),))
        if not rows:
            return None
        result = dict(sorted(rows, key=lambda row: (TASK_PRIORITY.index(row["reason_kind"]) if row["reason_kind"] in TASK_PRIORITY else 99, row["due_at"], row["review_round"], row["created_at"] or ""))[0])
        result["state"] = "due"
        return result

    def projections(self):
        now = self.clock()
        items = []
        for row in self.all("SELECT * FROM ReviewItemProjection ORDER BY due_at,review_round,created_at"):
            item = dict(row)
            if item["status"] == "open" and parse_time(item["due_at"]) <= parse_time(now):
                item["state"] = "due"
            items.append(item)
        objectives = []
        for row in self.all("SELECT * FROM LearnerObjectiveProjection"):
            item = dict(row)
            item["supporting_evidence_event_ids"] = loads(item.get("supporting_evidence_event_ids"), []) or []
            objectives.append(item)
        return {"review_items": items, "learner_objectives": objectives}

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

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/due-review":
                return self._json(200, {"data": self.store.get_due_review()})
            if path == "/api/projections":
                return self._json(200, {"data": self.store.projections()})
            if path == "/api/north-star":
                return self._json(200, {"data": self.store.north_star_events()})
            if path == "/api/intake":
                return self._json(200, {"data": self.store.list_intakes()})
            if path == "/api/wrong-questions":
                return self._json(200, {"data": self.store.list_wrong_questions()})
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
            if isinstance(payload, tuple):
                values, files = payload
                if path == "/api/intake/batches":
                    return self._json(200, {"data": self.store.create_intake_batch(files, values.get("subject_key") or None)})
                if path.startswith("/api/intake/") and path.endswith("/assets"):
                    intake_id = path[len("/api/intake/"):-len("/assets")].strip("/")
                    return self._json(200, {"data": self.store.append_intake_assets(intake_id, files)})
                payload = values
            if path == "/api/intake/batches":
                return self._json(200, {"data": self.store.create_intake_batch([], payload.get("subject_key"))})
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
            if path == "/api/answer":
                return self._json(200, {"data": self.store.answer_question(payload)})
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

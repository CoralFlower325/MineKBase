#!/usr/bin/env python3
"""Thin deterministic runners for the legacy P0 and image-first paths."""
import tempfile
from pathlib import Path
import json
import sys
import app
from app import SCENARIOS, run_scenario


def run_image_smoke():
    """Exercise the image path without a model or the real user data tree."""
    original_root = app.ROOT
    with tempfile.TemporaryDirectory(prefix="image-smoke-") as directory:
        root = Path(directory)
        (root / "schema.sql").write_text((original_root / "schema.sql").read_text(), encoding="utf-8")
        app.ROOT = root
        store = app.Store(root / "image.sqlite", lambda: "2026-09-05T00:00:00Z")
        try:
            files = [
                {"filename": "question.png", "mime": "image/png", "data": b"question-image", "role": "question"},
                {"filename": "process.png", "mime": "image/png", "data": b"process-image", "role": "my_process"},
            ]
            intake = store.create_intake_batch(files, "math")
            asset_ids = [asset["asset_id"] for asset in intake["assets"]]
            assert len(asset_ids) == 2
            store.patch_intake(intake["intake_id"], {"assets": [
                {"asset_id": asset_ids[0], "ordinal": 2},
                {"asset_id": asset_ids[1], "ordinal": 1, "role": "question"},
            ]})
            failed = store.analyze_intake(intake["intake_id"])
            assert failed["draft_fields"]["analysis_status"] == "failed"
            store.resolve_intake(intake["intake_id"])
            confirmed = store.confirm_intake(intake["intake_id"])
            assert confirmed["confirmed"] and confirmed["due_at"].startswith("2026-09-08")
            question_id = confirmed["question_id"]
            initial = store.one("SELECT response_snapshot FROM Attempt WHERE question_id=? AND origin_kind='initial'", (question_id,))
            initial_assets = set(app.as_dict(app.loads(initial["response_snapshot"], {})).get("response_assets", []))
            task = store.one("SELECT review_task_id FROM ReviewTask WHERE question_id=? AND status='open'", (question_id,))
            session = store.start_review(task["review_task_id"])
            redo = store.redo_upload(question_id, [{"filename": "redo.png", "mime": "image/png", "data": b"redo-image"}], {"review_task_id": task["review_task_id"]})
            redo_asset_ids = [asset["asset_id"] for asset in redo["assets"]]
            submitted = store.submit_attempt(redo["attempt_id"], {"response_assets": redo_asset_ids, "response_text": "redo"})
            assert submitted["status"] == "submitted" and submitted["comparison_draft"]["status"] == "failed"
            assert initial_assets == set(app.as_dict(app.loads(store.one("SELECT response_snapshot FROM Attempt WHERE question_id=? AND origin_kind='initial'", (question_id,))["response_snapshot"], {})).get("response_assets", []))
            return {"status": "passed", "question_id": question_id, "initial_assets": len(initial_assets), "comparison": submitted["comparison_draft"]["status"]}
        finally:
            store.conn.close()
            app.ROOT = original_root


def run_fts_fallback_smoke():
    """Exercise the lexical fallback when the optional FTS table is absent."""
    original_root = app.ROOT
    with tempfile.TemporaryDirectory(prefix="fts-smoke-") as directory:
        root = Path(directory)
        (root / "schema.sql").write_text((original_root / "schema.sql").read_text(), encoding="utf-8")
        app.ROOT = root
        store = app.Store(root / "fts.sqlite", lambda: "2026-09-05T00:00:00Z")
        try:
            assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 3
            artifact = store.capture_source({"source_name": "教材", "raw_text": "傅里叶变换用于频域分析"})
            store.fts_available = False
            store.conn.execute("DROP TABLE SourcePassageFTS")
            store.conn.commit()
            assert store.enrich_source(artifact["source_artifact_id"])["parse_state"] == "ready"
            rows = store.retrieve("频域分析")
            assert rows and rows[0]["source_artifact_id"] == artifact["source_artifact_id"]
            return {"status": "passed", "fts_available": store.fts_available}
        finally:
            store.conn.close()
            app.ROOT = original_root


def run_similar_practice_intake_smoke():
    """Ensure similar-practice drafts use IntakeItem until confirmation."""
    original_root = app.ROOT
    with tempfile.TemporaryDirectory(prefix="similar-practice-",) as directory:
        root = Path(directory)
        (root / "schema.sql").write_text((original_root / "schema.sql").read_text(), encoding="utf-8")
        app.ROOT = root
        store = app.Store(root / "similar.sqlite", lambda: "2026-09-05T00:00:00Z")
        try:
            base = store.create_intake_batch([], "math")
            store.patch_intake(base["intake_id"], {"draft_fields": {
                "analysis_status": "draft",
                "question_text": "原题：求极限",
                "reference_answer": "答案：按定义计算",
                "error_reason": "概念混淆",
                "error_breakpoint": "第一步",
            }})
            confirmed = store.confirm_intake(base["intake_id"])
            question_id = confirmed["question_id"]
            question_count = store.one("SELECT COUNT(*) AS count FROM Question")["count"]
            intake_count = store.one("SELECT COUNT(*) AS count FROM IntakeItem")["count"]

            store._invoke_with_fallback = lambda _prompt, _assets: ("", "", ["provider unavailable"])
            failed = store.similar_practice({"question_id": question_id})
            assert failed["status"] == "failed"
            assert store.one("SELECT COUNT(*) AS count FROM Question")["count"] == question_count
            assert store.one("SELECT COUNT(*) AS count FROM IntakeItem")["count"] == intake_count

            store._invoke_with_fallback = lambda _prompt, _assets: ("练习题：求导数\n参考答案：使用导数定义。", "test", [])
            candidate = store.similar_practice({"question_id": question_id})
            assert candidate["status"] == "draft"
            intake = store.create_intake_candidate(candidate["save_payload"])
            assert intake["status_key"] == "draft"
            assert intake["draft_fields"]["candidate_origin"] == "similar_practice"
            assert intake["field_sources"]["question_text"] == "模型候选"
            assert store.one("SELECT COUNT(*) AS count FROM Question")["count"] == question_count
            assert len(store.list_wrong_questions()) == 1

            store.patch_intake(intake["intake_id"], {"draft_fields": {
                "question_text": "用户修改后的练习题",
                "reference_answer": "用户补充的参考答案",
            }})
            confirmed_candidate = store.confirm_intake(intake["intake_id"])
            assert confirmed_candidate["confirmed"]
            assert store.one("SELECT COUNT(*) AS count FROM Question")["count"] == question_count + 1
            assert any(item["question_text"] == "用户修改后的练习题" for item in store.list_wrong_questions())
            return {"status": "passed", "intake_id": intake["intake_id"], "question_id": confirmed_candidate["question_id"]}
        finally:
            store.conn.close()
            app.ROOT = original_root


def run_question_bank_smoke():
    """Ensure imported bank items are scoped and ranked by the confirmed question metadata."""
    original_root = app.ROOT
    with tempfile.TemporaryDirectory(prefix="question-bank-") as directory:
        root = Path(directory)
        (root / "schema.sql").write_text((original_root / "schema.sql").read_text(), encoding="utf-8")
        app.ROOT = root
        store = app.Store(root / "question-bank.sqlite", lambda: "2026-09-05T00:00:00Z")
        try:
            course = store.create_course({"course_group": "电子类考研", "course_name": "数字电路", "subject_key": "professional"})
            node = store.create_knowledge_node({"course_id": course["course_id"], "name": "时序逻辑"})
            intake = store.create_intake_batch([], course_id=course["course_id"])
            store.patch_intake(intake["intake_id"], {"draft_fields": {
                "analysis_status": "draft",
                "question_text": "判断时序逻辑电路的状态转移",
                "reference_answer": "按状态方程分析",
                "chapter": "数字逻辑",
                "question_type": "选择题",
                "difficulty": "中",
                "knowledge_node_id": node["knowledge_node_id"],
            }})
            confirmed = store.confirm_intake(intake["intake_id"])
            imported = store.import_question_bank({"course_id": course["course_id"], "items": [
                {"question_text": "同知识点相似题", "chapter": "数字逻辑", "knowledge_node_id": node["knowledge_node_id"], "question_type": "选择题", "difficulty": "中", "reference_answer": "答案 A", "explanation": "看状态转移"},
                {"question_text": "同课程不同知识点", "chapter": "数字逻辑", "question_type": "选择题", "difficulty": "中", "reference_answer": "答案 B"},
            ]})
            similar = store.similar_question_bank(confirmed["question_id"])
            assert imported["imported"] == 2
            assert similar and similar[0]["question_text"] == "同知识点相似题"
            assert similar[0]["match_score"] > similar[1]["match_score"]
            filtered = store.list_question_bank({"course_id": course["course_id"], "knowledge_node_id": node["knowledge_node_id"]})
            assert len(filtered) == 1 and filtered[0]["question_text"] == "同知识点相似题"
            answer_intake = store.create_intake_batch([{"filename": "reference.png", "mime": "image/png", "data": b"reference", "role": "reference"}], course_id=course["course_id"])
            store.patch_intake(answer_intake["intake_id"], {"draft_fields": {
                "analysis_status": "draft",
                "question_text": "判断时序逻辑电路的状态转移",
                "reference_answer": "参考答案图提取的答案",
                "error_reason": "方法选择错误",
                "error_breakpoint": "第一次列状态方程时",
                "chapter": "数字逻辑",
                "question_type": "选择题",
                "knowledge_node_id": node["knowledge_node_id"],
            }})
            resolved = store.resolve_intake(answer_intake["intake_id"])
            answer_candidates = resolved["draft_fields"]["answer_candidates"]
            assert answer_candidates and answer_candidates[0]["origin"] == "reference_image"
            confirmed_answer = store.confirm_intake(answer_intake["intake_id"])
            grading = store.one("SELECT grading_reference_fixture_snapshot FROM QuestionRevision WHERE question_id=?", (confirmed_answer["question_id"],))
            assert app.loads(grading[0], {})["answer_origin"] == "reference_image"
            before_questions = store.one("SELECT COUNT(*) AS count FROM Question")["count"]
            practice = store.start_question_bank_item(filtered[0]["question_bank_item_id"])
            practice_draft = practice["draft_fields"]
            assert practice_draft["candidate_origin"] == "question_bank"
            assert practice_draft["knowledge_node_id"] == node["knowledge_node_id"]
            assert store.one("SELECT COUNT(*) AS count FROM Question")["count"] == before_questions
            return {"status": "passed", "imported": imported["imported"], "similar": len(similar), "practice_intake_id": practice["intake_id"]}
        finally:
            store.conn.close()
            app.ROOT = original_root


def run_knowledge_candidate_smoke():
    """Ensure analysis labels create candidates before the user confirms a node."""
    original_root = app.ROOT
    with tempfile.TemporaryDirectory(prefix="knowledge-candidate-") as directory:
        root = Path(directory)
        (root / "schema.sql").write_text((original_root / "schema.sql").read_text(), encoding="utf-8")
        app.ROOT = root
        store = app.Store(root / "knowledge.sqlite", lambda: "2026-09-05T00:00:00Z")
        try:
            course = store.create_course({"course_group": "计算机考研", "course_name": "408-数据结构", "subject_key": "professional"})
            intake = store.create_intake_batch([], course_id=course["course_id"])
            store.patch_intake(intake["intake_id"], {"draft_fields": {
                "analysis_status": "draft",
                "question_text": "判断树的遍历顺序",
                "reference_answer": "按递归定义展开",
                "chapter": "树与图",
                "knowledge_point": "二叉树遍历",
                "error_type": "method_selection",
                "error_reason": "方法选择错误",
                "error_breakpoint": "第一次选择遍历方法时",
            }})
            resolved = store.resolve_intake(intake["intake_id"])
            nodes = resolved["draft_fields"]["knowledge_node_candidates"]
            point = next(node for node in nodes if node["name"] == "二叉树遍历")
            assert point["confirmation_state"] == "candidate" and point["course_id"] == course["course_id"]
            store.patch_intake(intake["intake_id"], {"draft_fields": {"knowledge_node_id": point["knowledge_node_id"]}})
            confirmed = store.confirm_intake(intake["intake_id"])
            state = store.one("SELECT confirmation_state FROM KnowledgeNode WHERE knowledge_node_id=?", (point["knowledge_node_id"],))[0]
            link = store.one("SELECT question_id FROM QuestionKnowledgeLink WHERE question_id=? AND knowledge_node_id=?", (confirmed["question_id"], point["knowledge_node_id"]))
            assert state == "confirmed" and link
            return {"status": "passed", "knowledge_node_id": point["knowledge_node_id"]}
        finally:
            store.conn.close()
            app.ROOT = original_root

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "SIMILAR-PRACTICE-INTAKE":
        print(json.dumps({"runner": "run_p0_scenarios", "results": [{"scenario_id": "SIMILAR-PRACTICE-INTAKE", "status": run_similar_practice_intake_smoke()["status"]}]}, ensure_ascii=False, indent=2))
        return
    results=[]
    for sid in SCENARIOS:
        r=run_scenario(sid)
        assert r['due_task']['status']=='open'
        if sid=='P0-S2-start-abandon': assert r['abandon']['attempt_id'] is None
        if sid=='P0-S3-independent-submit': assert r['submit']['attempt_id']==r['repeat_submit']['attempt_id']
        if sid=='P0-S4-independent-assessment': assert len(r['assessment']['evidence_event_ids'])==1
        if sid=='P0-S5-assisted-or-unassessed': assert r['assessment']['result']=='unassessed' and not r['assessment']['evidence_event_ids']
        results.append({'scenario_id':sid,'status':'passed'})
    results.append({'scenario_id':'IMAGE-SMOKE','status':run_image_smoke()['status']})
    results.append({'scenario_id':'FTS-FALLBACK-SMOKE','status':run_fts_fallback_smoke()['status']})
    results.append({'scenario_id':'SIMILAR-PRACTICE-INTAKE','status':run_similar_practice_intake_smoke()['status']})
    results.append({'scenario_id':'QUESTION-BANK-SMOKE','status':run_question_bank_smoke()['status']})
    results.append({'scenario_id':'KNOWLEDGE-CANDIDATE-SMOKE','status':run_knowledge_candidate_smoke()['status']})
    print(json.dumps({'runner':'run_p0_scenarios','results':results},ensure_ascii=False,indent=2))
if __name__=='__main__': main()

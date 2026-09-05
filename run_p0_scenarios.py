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
                {"asset_id": asset_ids[0], "ordinal": 2, "role": "my_process"},
                {"asset_id": asset_ids[1], "ordinal": 1, "role": "question"},
            ]})
            failed = store.analyze_intake(intake["intake_id"])
            assert failed["draft_fields"]["analysis_status"] == "failed"
            store.resolve_intake(intake["intake_id"])
            confirmed = store.confirm_intake(intake["intake_id"])
            assert confirmed["confirmed"] and confirmed["due_at"].startswith("2026-09-08")
            question_id = confirmed["question_id"]
            wrong_detail = store.get_wrong_question(question_id)
            assert any((asset.get("review_role") or asset.get("role")) == "my_process" for asset in wrong_detail["process_assets"])
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
            assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 4
            course = store.create_course({"course_group": "电子类考研", "course_name": "信号与系统", "subject_key": "professional"})
            artifact = store.capture_source({"source_name": "教材", "course_id": course["course_id"], "raw_text": "第1章 信号与系统\n1.1 连续时间信号\n1.2 离散时间信号\n知识点：傅里叶变换\n\n傅里叶变换用于频域分析"})
            store.fts_available = False
            store.conn.execute("DROP TABLE SourcePassageFTS")
            store.conn.commit()
            assert store.enrich_source(artifact["source_artifact_id"])["parse_state"] == "ready"
            heading = store.one("SELECT * FROM KnowledgeNode WHERE course_id=? AND name=?", (course["course_id"], "第1章 信号与系统"))
            assert heading and heading["confirmation_state"] == "candidate" and heading["origin"] == "source_heading"
            subsection = store.one("SELECT * FROM KnowledgeNode WHERE course_id=? AND name=?", (course["course_id"], "1.1 连续时间信号"))
            point = store.one("SELECT * FROM KnowledgeNode WHERE course_id=? AND name=?", (course["course_id"], "傅里叶变换"))
            assert subsection and subsection["parent_id"] == heading["knowledge_node_id"]
            assert point and point["parent_id"] == heading["knowledge_node_id"]
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
            course = store.create_course({"course_group": "电子类考研", "course_name": "信号与系统", "subject_key": "professional"})
            base = store.create_intake_batch([], course_id=course["course_id"])
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
            assert intake["course_id"] == course["course_id"]
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
            preview = store.preview_question_bank({"course_id": course["course_id"], "items": [{"question_text": "有效题"}, {"question_text": "", "reference_answer": "缺少题面"}]})
            assert preview["ready_count"] == 1 and preview["error_count"] == 1 and not preview["valid"]
            duplicate_preview = store.preview_question_bank({"course_id": course["course_id"], "items": [{"question_bank_item_id": "duplicate-id", "question_text": "题一"}, {"question_bank_item_id": "duplicate-id", "question_text": "题二"}]})
            assert not duplicate_preview["valid"] and duplicate_preview["rows"][1]["errors"][0]["code"] == "duplicate_question_bank_item_id"
            try:
                store.import_question_bank({"course_id": course["course_id"], "items": [{"question_text": "", "reference_answer": "拒绝导入"}]})
                raise AssertionError("invalid question bank row should be rejected")
            except app.DomainError as error:
                assert error.code == "invalid_question_bank" and error.details["error_count"] == 1
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
                {"question_text": "同课程不同知识点", "chapter": "数字逻辑", "knowledge_point": "组合逻辑", "question_type": "选择题", "difficulty": "中", "reference_answer": "答案 B"},
            ]})
            generated_node = store.one("SELECT * FROM KnowledgeNode WHERE course_id=? AND name=?", (course["course_id"], "组合逻辑"))
            assert generated_node and generated_node["confirmation_state"] == "candidate"
            generated_bank = store.one("SELECT knowledge_node_id FROM QuestionBankItem WHERE question_bank_item_id=?", (imported["question_bank_item_ids"][1],))
            assert generated_bank["knowledge_node_id"] == generated_node["knowledge_node_id"]
            updated_item = store.update_question_bank_item(imported["question_bank_item_ids"][0], {"question_text": "同知识点相似题（已维护）", "options": "A. 正确|B. 错误", "reference_answer": "A"})
            bulk = store.bulk_update_question_bank({"items": [{"question_bank_item_id": imported["question_bank_item_ids"][1], "explanation": "补充说明"}]})
            similar = store.similar_question_bank(confirmed["question_id"])
            strict_similar = store.similar_question_bank(confirmed["question_id"], {"knowledge_node_id": node["knowledge_node_id"], "question_type": "选择题", "difficulty": "中"})
            assert imported["imported"] == 2
            assert "已维护" in updated_item["question_text"] and updated_item["options"]["A"] == "正确" and bulk["updated"] == 1
            assert similar and "已维护" in similar[0]["question_text"]
            assert similar[0]["match_score"] > similar[1]["match_score"]
            assert len(strict_similar) == 1 and "已维护" in strict_similar[0]["question_text"]
            filtered = store.list_question_bank({"course_id": course["course_id"], "knowledge_node_id": node["knowledge_node_id"]})
            assert len(filtered) == 1 and "已维护" in filtered[0]["question_text"]
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
            answer_candidates = store._answer_candidates({}, intake["batch_id"], [{"kind": "source", "text": "教材说明\n参考答案：先列出递归关系，再按定义展开。\n补充：检查边界条件。", "source_passage_id": "passage-test", "source_name": "教材"}])
            assert answer_candidates[0]["answer"] == "先列出递归关系，再按定义展开。" and answer_candidates[0]["source_text"]
            extracted = store._extract_analysis("首次出错步骤：第二步选择了错误的遍历方法\n错误类型：方法选择错误", "professional")
            assert extracted["error_breakpoint"] == "第二步选择了错误的遍历方法" and extracted["error_type"] == "method_selection"
            long_candidates = store._answer_candidates({}, intake["batch_id"], [{"kind": "source", "text": "长资料" * 500, "source_passage_id": "passage-long", "source_name": "长资料"}])
            assert long_candidates[0]["source_excerpted"] and "资料较长" in long_candidates[0]["answer"] and long_candidates[0]["source_text"]
            point = next(node for node in nodes if node["name"] == "二叉树遍历")
            assert point["confirmation_state"] == "candidate" and point["course_id"] == course["course_id"]
            merged = store._ensure_knowledge_candidates(course["course_id"], [{"chapter": "树与图 ", "knowledge_point": "二叉树 遍历", "origin": "source_heading"}])
            merged_point = next(node for node in merged if node["name"] == "二叉树遍历")
            assert merged_point["knowledge_node_id"] == point["knowledge_node_id"]
            assert "二叉树 遍历" in app.loads(store.one("SELECT aliases FROM KnowledgeNode WHERE knowledge_node_id=?", (point["knowledge_node_id"],))[0], [])
            promoted = store.update_knowledge_node(point["knowledge_node_id"], {"confirmation_state": "confirmed"})
            assert promoted["confirmation_state"] == "confirmed" and promoted["origin"] == "user"
            ignored = store.create_knowledge_node({"course_id": course["course_id"], "name": "不相关候选", "origin": "source_heading"})
            archived = store.update_knowledge_node(ignored["knowledge_node_id"], {"confirmation_state": "archived"})
            assert archived["confirmation_state"] == "archived"
            store.patch_intake(intake["intake_id"], {"draft_fields": {"knowledge_node_id": point["knowledge_node_id"]}})
            confirmed = store.confirm_intake(intake["intake_id"])
            state = store.one("SELECT confirmation_state FROM KnowledgeNode WHERE knowledge_node_id=?", (point["knowledge_node_id"],))[0]
            link = store.one("SELECT question_id FROM QuestionKnowledgeLink WHERE question_id=? AND knowledge_node_id=?", (confirmed["question_id"], point["knowledge_node_id"]))
            assert state == "confirmed" and link
            replacement = store.create_knowledge_node({"course_id": course["course_id"], "name": "树的遍历方法"})
            store.patch_intake(intake["intake_id"], {"draft_fields": {"knowledge_node_id": replacement["knowledge_node_id"], "knowledge_point": replacement["name"]}})
            replacement_link = store.one("SELECT question_id FROM QuestionKnowledgeLink WHERE question_id=? AND knowledge_node_id=?", (confirmed["question_id"], replacement["knowledge_node_id"]))
            old_link = store.one("SELECT question_id FROM QuestionKnowledgeLink WHERE question_id=? AND knowledge_node_id=?", (confirmed["question_id"], point["knowledge_node_id"]))
            assert replacement_link and old_link is None
            return {"status": "passed", "knowledge_node_id": point["knowledge_node_id"]}
        finally:
            store.conn.close()
            app.ROOT = original_root


def run_politics_bank_smoke():
    """Exercise objective political-bank checking and incorrect-attempt storage."""
    original_root = app.ROOT
    with tempfile.TemporaryDirectory(prefix="politics-bank-") as directory:
        root = Path(directory)
        (root / "schema.sql").write_text((original_root / "schema.sql").read_text(), encoding="utf-8")
        app.ROOT = root
        store = app.Store(root / "politics.sqlite", lambda: "2026-09-05T00:00:00Z")
        try:
            item = store.import_question_bank({"course_id": "course-politics", "items": [{
                "question_text": "下列哪项属于政治选择题测试？",
                "chapter": "马克思主义基本原理",
                "question_type": "单选题",
                "difficulty": "易",
                "options": {"A": "选项一", "B": "选项二", "C": "选项三", "D": "选项四"},
                "reference_answer": "B",
                "explanation": "答案依据题干中的基本概念。",
            }]})
            bank = store.list_question_bank({"course_id": "course-politics"})
            assert bank[0]["options"]["B"] == "选项二"
            wrong = store.answer_question_bank(item["question_bank_item_ids"][0], {"selected_answer": "A"})
            right = store.answer_question_bank(item["question_bank_item_ids"][0], {"selected_answer": "B"})
            assert wrong["is_correct"] is False and right["is_correct"] is True
            attempts = store.list_question_bank_attempts({"course_id": "course-politics"})
            incorrect = store.list_question_bank_attempts({"course_id": "course-politics", "incorrect_only": "1"})
            assert len(attempts) == 2 and len(incorrect) == 1 and incorrect[0]["selected_answer"] == "A"
            assert incorrect[0]["options"]["B"] == "选项二" and incorrect[0]["explanation"] == "答案依据题干中的基本概念。"
            return {"status": "passed", "attempts": len(attempts), "incorrect": len(incorrect)}
        finally:
            store.conn.close()
            app.ROOT = original_root


def run_wrong_course_filter_smoke():
    """Ensure professional-course wrong questions remain separable by course."""
    original_root = app.ROOT
    with tempfile.TemporaryDirectory(prefix="wrong-course-filter-") as directory:
        root = Path(directory)
        (root / "schema.sql").write_text((original_root / "schema.sql").read_text(), encoding="utf-8")
        app.ROOT = root
        store = app.Store(root / "wrong-course-filter.sqlite", lambda: "2026-09-05T00:00:00Z")
        try:
            courses = [
                store.create_course({"course_group": "电子类考研", "course_name": "信号与系统", "subject_key": "professional"}),
                store.create_course({"course_group": "电子类考研", "course_name": "数字电路", "subject_key": "professional"}),
            ]
            intakes = []
            for index, course in enumerate(courses, start=1):
                intake = store.create_intake_batch([], course_id=course["course_id"])
                intakes.append(intake)
                store.patch_intake(intake["intake_id"], {"draft_fields": {
                    "analysis_status": "draft",
                    "question_text": f"课程隔离测试题 {index}",
                    "reference_answer": "参考解",
                    "error_type": "knowledge_gap",
                    "chapter": "测试章节",
                    "knowledge_point": "测试知识点",
                }})
                assert store.confirm_intake(intake["intake_id"])["confirmed"]
            resolved = store.resolve_intake(intakes[0]["intake_id"])
            question_candidates = [item for item in resolved["draft_fields"].get("match_candidates", []) if item.get("kind") == "question"]
            assert question_candidates and all(item.get("grading", {}).get("course_id") in {None, courses[0]["course_id"]} for item in question_candidates)
            all_rows = store.list_wrong_questions()
            filtered = store.list_wrong_questions({"course_id": courses[0]["course_id"]})
            assert len(all_rows) == 2 and len(filtered) == 1
            assert filtered[0]["course_id"] == courses[0]["course_id"]
            assert filtered[0]["question_text"] == "课程隔离测试题 1"
            return {"status": "passed", "all": len(all_rows), "filtered": len(filtered)}
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
    results.append({'scenario_id':'POLITICS-BANK-SMOKE','status':run_politics_bank_smoke()['status']})
    results.append({'scenario_id':'WRONG-COURSE-FILTER-SMOKE','status':run_wrong_course_filter_smoke()['status']})
    print(json.dumps({'runner':'run_p0_scenarios','results':results},ensure_ascii=False,indent=2))
if __name__=='__main__': main()

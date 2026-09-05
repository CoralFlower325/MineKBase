PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS ModelEndpoint (slot TEXT PRIMARY KEY CHECK(slot IN ('primary','fallback')), protocol TEXT, base_url TEXT, api_key TEXT, model TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS Course (
    course_id TEXT PRIMARY KEY,
    course_group TEXT NOT NULL,
    course_name TEXT NOT NULL,
    subject_key TEXT CHECK(subject_key IN ('math','english','politics','professional') OR subject_key IS NULL),
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS KnowledgeNode (
    knowledge_node_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES Course(course_id),
    parent_id TEXT REFERENCES KnowledgeNode(knowledge_node_id),
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'user',
    confirmation_state TEXT NOT NULL DEFAULT 'candidate' CHECK(confirmation_state IN ('candidate','confirmed','archived')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_course_parent ON KnowledgeNode(course_id,parent_id,name);
CREATE TABLE IF NOT EXISTS QuestionKnowledgeLink (
    question_id TEXT NOT NULL REFERENCES Question(question_id),
    knowledge_node_id TEXT NOT NULL REFERENCES KnowledgeNode(knowledge_node_id),
    origin TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    PRIMARY KEY(question_id,knowledge_node_id)
);
CREATE INDEX IF NOT EXISTS idx_question_knowledge_node ON QuestionKnowledgeLink(knowledge_node_id,question_id);
CREATE TABLE IF NOT EXISTS QuestionBankItem (
    question_bank_item_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES Course(course_id),
    question_text TEXT,
    image_path TEXT,
    chapter TEXT,
    knowledge_node_id TEXT REFERENCES KnowledgeNode(knowledge_node_id),
    question_type TEXT,
    difficulty TEXT,
    reference_answer TEXT,
    explanation TEXT,
    source TEXT,
    year TEXT,
    raw_payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_bank_lookup ON QuestionBankItem(course_id,knowledge_node_id,chapter,question_type,difficulty);
CREATE TABLE IF NOT EXISTS QuestionBankAttempt (
    question_bank_attempt_id TEXT PRIMARY KEY,
    question_bank_item_id TEXT NOT NULL REFERENCES QuestionBankItem(question_bank_item_id),
    course_id TEXT NOT NULL REFERENCES Course(course_id),
    selected_answer TEXT NOT NULL,
    correct_answer TEXT NOT NULL,
    is_correct INTEGER NOT NULL CHECK(is_correct IN (0,1)),
    answered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_bank_attempt_item ON QuestionBankAttempt(question_bank_item_id,answered_at);
CREATE INDEX IF NOT EXISTS idx_question_bank_attempt_course ON QuestionBankAttempt(course_id,is_correct,answered_at);
CREATE TABLE IF NOT EXISTS CoursePackRelease (release_id TEXT PRIMARY KEY, course_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS LearningObjective (learning_objective_id TEXT PRIMARY KEY, course_pack_release_id TEXT NOT NULL REFERENCES CoursePackRelease(release_id), name TEXT NOT NULL, description TEXT NOT NULL, observable_criteria TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS Question (question_id TEXT PRIMARY KEY, course_pack_release_id TEXT NOT NULL REFERENCES CoursePackRelease(release_id), current_question_revision_id TEXT, lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('candidate','active','archived')), created_at TEXT NOT NULL, FOREIGN KEY(current_question_revision_id) REFERENCES QuestionRevision(question_revision_id));
CREATE TABLE IF NOT EXISTS QuestionRevision (question_revision_id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES Question(question_id), revision_no INTEGER NOT NULL, revision_state TEXT NOT NULL CHECK(revision_state IN ('candidate','confirmed','superseded','rejected')), supersedes_revision_id TEXT REFERENCES QuestionRevision(question_revision_id), current_review_prompt_revision_id TEXT, question_units TEXT NOT NULL, objective_mapping_snapshot TEXT NOT NULL, grading_reference_fixture_snapshot TEXT, help_content_fixture_snapshot TEXT, created_at TEXT NOT NULL, UNIQUE(question_id,revision_no), FOREIGN KEY(current_review_prompt_revision_id) REFERENCES ReviewPromptRevision(review_prompt_revision_id));
CREATE UNIQUE INDEX IF NOT EXISTS uq_confirmed_question_revision ON QuestionRevision(question_id) WHERE revision_state='confirmed';
CREATE TABLE IF NOT EXISTS ReviewPromptRevision (review_prompt_revision_id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES Question(question_id), question_revision_id TEXT NOT NULL REFERENCES QuestionRevision(question_revision_id), revision_no INTEGER NOT NULL, presentation_snapshot TEXT NOT NULL, unresolved_critical_ambiguities TEXT NOT NULL, leakage_state TEXT NOT NULL CHECK(leakage_state IN ('clean','leaky','unknown')), revision_state TEXT NOT NULL CHECK(revision_state IN ('candidate','ready','superseded','rejected')), created_at TEXT NOT NULL, UNIQUE(question_revision_id,revision_no));
CREATE UNIQUE INDEX IF NOT EXISTS uq_ready_review_prompt ON ReviewPromptRevision(question_revision_id) WHERE revision_state='ready';
CREATE TABLE IF NOT EXISTS ReviewTask (review_task_id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES Question(question_id), question_revision_id TEXT NOT NULL REFERENCES QuestionRevision(question_revision_id), review_prompt_revision_id TEXT NOT NULL REFERENCES ReviewPromptRevision(review_prompt_revision_id), kind TEXT NOT NULL CHECK(kind='closed_book_review'), reason_kind TEXT NOT NULL, trigger_snapshots TEXT NOT NULL, snapshot_schema_version TEXT NOT NULL, review_round INTEGER NOT NULL, due_at TEXT NOT NULL, schedule_policy_version TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','completed','superseded')), completed_by_attempt_id TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_task_question_kind ON ReviewTask(question_id,kind) WHERE status='open';
CREATE TABLE IF NOT EXISTS ReviewSession (review_session_id TEXT PRIMARY KEY, review_task_id TEXT NOT NULL REFERENCES ReviewTask(review_task_id), question_id TEXT NOT NULL REFERENCES Question(question_id), question_revision_id TEXT NOT NULL REFERENCES QuestionRevision(question_revision_id), review_prompt_revision_id TEXT NOT NULL REFERENCES ReviewPromptRevision(review_prompt_revision_id), prompt_eligibility_snapshot TEXT NOT NULL, draft_payload_snapshot TEXT NOT NULL, exposure_event_snapshots TEXT NOT NULL, visibility_policy_version TEXT NOT NULL, help_content_snapshot TEXT NOT NULL, prior_attempts_hidden INTEGER NOT NULL, solutions_hidden INTEGER NOT NULL, explanations_hidden INTEGER NOT NULL, objective_hints_hidden INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, updated_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','submitted','abandoned','superseded')), submitted_attempt_id TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_session ON ReviewSession(review_task_id) WHERE status='active';
CREATE UNIQUE INDEX IF NOT EXISTS uq_submitted_session ON ReviewSession(review_task_id) WHERE status='submitted';
CREATE TABLE IF NOT EXISTS Attempt (attempt_id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES Question(question_id), question_revision_id TEXT NOT NULL REFERENCES QuestionRevision(question_revision_id), review_session_id TEXT REFERENCES ReviewSession(review_session_id), origin_kind TEXT NOT NULL CHECK(origin_kind IN ('initial','practice','review')), submission_state TEXT NOT NULL CHECK(submission_state='submitted'), submitted_at TEXT NOT NULL, completion_claim TEXT NOT NULL CHECK(completion_claim IN ('complete','partial','incomplete','unknown')), initial_debt_claim TEXT, initial_debt_claim_basis TEXT, initial_debt_claim_captured_at TEXT, response_snapshot TEXT NOT NULL, assistance_state TEXT NOT NULL CHECK(assistance_state IN ('none_observed','assisted')), external_help_reported INTEGER NOT NULL, submit_event_ordinal INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS Assessment (assessment_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES Attempt(attempt_id), question_id TEXT NOT NULL REFERENCES Question(question_id), question_revision_id TEXT NOT NULL REFERENCES QuestionRevision(question_revision_id), review_prompt_revision_id TEXT REFERENCES ReviewPromptRevision(review_prompt_revision_id), snapshot_schema_version TEXT NOT NULL, assessment_policy_version TEXT NOT NULL, assessor_kind TEXT NOT NULL CHECK(assessor_kind IN ('human','deterministic','model','user_self')), raw_result TEXT NOT NULL, result TEXT CHECK(result IN ('correct','incorrect','unassessed') OR result IS NULL), attempt_interpretation_state TEXT NOT NULL, normalization_note TEXT, attempt_response_snapshot TEXT NOT NULL, objective_inputs_snapshot TEXT NOT NULL, reference_inputs_snapshot TEXT NOT NULL, observations_snapshot TEXT NOT NULL, proposal_snapshot TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','final','invalidated')), invalidation_reason_snapshot TEXT, created_at TEXT NOT NULL, finalized_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_review_session ON Attempt(review_session_id) WHERE review_session_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_submitted_attempt ON ReviewSession(submitted_attempt_id) WHERE submitted_attempt_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_task_completed_attempt ON ReviewTask(completed_by_attempt_id) WHERE completed_by_attempt_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS EvidenceEvent (evidence_event_id TEXT PRIMARY KEY, assessment_id TEXT NOT NULL REFERENCES Assessment(assessment_id), observation_ref TEXT NOT NULL, learning_objective_id TEXT NOT NULL REFERENCES LearningObjective(learning_objective_id), event_type TEXT NOT NULL CHECK(event_type='observation'), performance_state TEXT NOT NULL, evidence_policy_version TEXT NOT NULL, captured_at TEXT NOT NULL);
-- Write-first capture layer.  These tables intentionally keep the incoming
-- payload and parse state loose so incomplete or not-yet-supported material
-- can still be reopened and enriched later.
CREATE TABLE IF NOT EXISTS SourceArtifact (
    source_artifact_id TEXT PRIMARY KEY,
    kind TEXT,
    source_name TEXT,
    stored_path TEXT,
    raw_text TEXT,
    raw_payload TEXT,
    subject_key TEXT CHECK(subject_key IN ('math','english','politics','professional') OR subject_key IS NULL),
    course_id TEXT REFERENCES Course(course_id),
    parse_state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS SourcePassage (
    source_passage_id TEXT PRIMARY KEY,
    source_artifact_id TEXT NOT NULL REFERENCES SourceArtifact(source_artifact_id),
    ordinal INTEGER NOT NULL,
    text TEXT,
    page_no INTEGER,
    bbox TEXT,
    locator_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_artifact_id, ordinal)
);
CREATE TABLE IF NOT EXISTS QuestionSourceLink (
    question_id TEXT NOT NULL REFERENCES Question(question_id),
    learning_objective_id TEXT REFERENCES LearningObjective(learning_objective_id),
    source_passage_id TEXT NOT NULL REFERENCES SourcePassage(source_passage_id),
    relation TEXT,
    origin TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(question_id, source_passage_id, relation)
);
-- SourcePassageFTS is an optional acceleration table. Store initializes it
-- when the local SQLite build supports FTS5/trigram; LIKE remains the fallback.
CREATE TABLE IF NOT EXISTS Answer (
    answer_id TEXT PRIMARY KEY,
    question_id TEXT REFERENCES Question(question_id),
    query TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    source_snapshot TEXT NOT NULL,
    model_provider TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Image-first intake foundation.  Files remain ordinary project files under
-- objects/; SQLite stores their durable identity and editable intake state.
CREATE TABLE IF NOT EXISTS CaptureBatch (
    batch_id TEXT PRIMARY KEY,
    subject_key TEXT CHECK(subject_key IN ('math','english','politics','professional') OR subject_key IS NULL),
    course_id TEXT REFERENCES Course(course_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS IntakeItem (
    intake_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE REFERENCES CaptureBatch(batch_id),
    state TEXT NOT NULL CHECK(state IN ('raw','saved','incomplete')),
    draft_fields TEXT NOT NULL DEFAULT '{}',
    failure_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ImageAsset (
    asset_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES CaptureBatch(batch_id),
    original_filename TEXT NOT NULL,
    mime TEXT,
    ordinal INTEGER NOT NULL,
    path TEXT,
    role TEXT CHECK(role IN ('question','my_process','reference','redo_process','mixed') OR role IS NULL),
    state TEXT NOT NULL DEFAULT 'saved' CHECK(state IN ('saved','incomplete')),
    failure_note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_intake_created ON IntakeItem(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_image_batch ON ImageAsset(batch_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_task_due ON ReviewTask(status,due_at);
CREATE INDEX IF NOT EXISTS idx_task_triplet ON ReviewTask(question_id,question_revision_id,review_prompt_revision_id);
CREATE INDEX IF NOT EXISTS idx_attempt_question ON Attempt(question_id,submitted_at);
CREATE INDEX IF NOT EXISTS idx_evidence_objective ON EvidenceEvent(learning_objective_id,captured_at);
CREATE VIEW IF NOT EXISTS ReviewItemProjection AS
WITH ranked_tasks AS (
  SELECT t.*, ROW_NUMBER() OVER (PARTITION BY t.question_id ORDER BY CASE t.reason_kind WHEN 'awaiting_assessment' THEN 0 WHEN 'assisted_retry' THEN 1 WHEN 'retry_after_fail' THEN 2 WHEN 'incomplete_attempt' THEN 3 WHEN 'initial_error' THEN 4 WHEN 'manual_declaration' THEN 5 ELSE 6 END,t.due_at,t.review_round,t.created_at,t.review_task_id) AS rn
  FROM ReviewTask t WHERE t.status='open'
), ranked_attempts AS (
  SELECT a.*, ROW_NUMBER() OVER (PARTITION BY a.question_id,a.question_revision_id ORDER BY a.submitted_at DESC, CASE a.origin_kind WHEN 'initial' THEN 0 WHEN 'practice' THEN 1 WHEN 'review' THEN 2 ELSE 3 END DESC, COALESCE(a.submit_event_ordinal,-1) DESC, a.attempt_id DESC) AS rn
  FROM Attempt a WHERE a.submission_state='submitted'
), latest_assessment AS (
  SELECT ass.*, ROW_NUMBER() OVER (PARTITION BY ass.attempt_id ORDER BY ass.created_at DESC,ass.assessment_id DESC) AS rn
  FROM Assessment ass WHERE ass.status='final'
)
SELECT q.question_id,rt.review_task_id,rt.question_revision_id,rt.review_prompt_revision_id,rt.reason_kind,rt.review_round,
       rt.due_at AS next_due_at,rt.due_at,rt.created_at,rt.status,
       CASE WHEN rt.review_task_id IS NOT NULL AND rt.reason_kind='spaced_confirmation' THEN 'passed_once'
            WHEN rt.review_task_id IS NOT NULL THEN 'cooling'
            WHEN la.attempt_id IS NOT NULL AND las.assessment_id IS NULL THEN 'awaiting_assessment'
            WHEN la.attempt_id IS NOT NULL AND (la.completion_claim<>'complete' OR las.result='incorrect') THEN 'needs_retry'
            ELSE 'needs_review' END AS state,
       la.attempt_id AS last_attempt_id,COALESCE(rt.schedule_policy_version,'schedule-v1') AS policy_version,q.lifecycle_state
FROM Question q
LEFT JOIN ranked_tasks rt ON rt.question_id=q.question_id AND rt.rn=1
LEFT JOIN ranked_attempts la ON la.question_id=q.question_id AND la.question_revision_id=q.current_question_revision_id AND la.rn=1
LEFT JOIN latest_assessment las ON las.attempt_id=la.attempt_id AND las.rn=1;
CREATE VIEW IF NOT EXISTS LearnerObjectiveProjection AS
WITH ev AS (
  SELECT e.*, ROW_NUMBER() OVER (PARTITION BY e.learning_objective_id ORDER BY e.captured_at DESC,e.evidence_event_id DESC) AS rn
  FROM EvidenceEvent e WHERE e.event_type='observation'
), ass AS (
  SELECT a.*,at.assistance_state,at.external_help_reported,at.completion_claim,at.submitted_at,
         ROW_NUMBER() OVER (PARTITION BY a.question_id ORDER BY COALESCE(a.finalized_at,a.created_at) DESC,a.assessment_id DESC) AS rn
  FROM Assessment a JOIN Attempt at ON at.attempt_id=a.attempt_id WHERE a.status='final'
)
SELECT lo.learning_objective_id,lo.name,
       CASE WHEN ev.learning_objective_id IS NULL AND ass.assessment_id IS NULL THEN 'no_evidence'
            WHEN ass.rn=1 AND ass.result='unassessed' AND EXISTS (SELECT 1 FROM ReviewTask t WHERE t.question_id=ass.question_id AND t.status='open') THEN 'awaiting_cold_review'
            WHEN ev.performance_state='failure' THEN 'recent_failure'
            WHEN ev.performance_state='success' AND (SELECT COUNT(*) FROM EvidenceEvent e2 WHERE e2.learning_objective_id=lo.learning_objective_id AND e2.performance_state='success' AND e2.event_type='observation') >= 2 THEN 'repeated_independent_pass'
            WHEN ev.performance_state='success' THEN 'independent_pass_once'
            ELSE 'needs_review' END AS state,
       ev.captured_at AS last_evidence_at,
       COALESCE((SELECT json_group_array(e3.evidence_event_id) FROM EvidenceEvent e3 WHERE e3.learning_objective_id=lo.learning_objective_id AND e3.event_type='observation'),'[]') AS supporting_evidence_event_ids,
       'evidence-v1' AS policy_version
FROM LearningObjective lo
LEFT JOIN ev ON ev.learning_objective_id=lo.learning_objective_id AND ev.rn=1
LEFT JOIN ass ON ass.rn=1 AND ass.question_id IN (SELECT q.question_id FROM Question q WHERE q.course_pack_release_id=lo.course_pack_release_id);

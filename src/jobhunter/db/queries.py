"""Named SQL queries used by the repository layer."""

# ── Jobs ──────────────────────────────────────────────────────────────────────

INSERT_JOB = """
INSERT INTO jobs (
    linkedin_job_id, title, company, location, employment_type,
    experience_level, salary_range, description, job_url, external_url,
    apply_type, company_domain, match_score, match_reasoning, search_query, status
) VALUES (
    :linkedin_job_id, :title, :company, :location, :employment_type,
    :experience_level, :salary_range, :description, :job_url, :external_url,
    :apply_type, :company_domain, :match_score, :match_reasoning, :search_query, :status
)
"""

GET_JOB_BY_ID = "SELECT * FROM jobs WHERE id = ?"

GET_JOB_BY_LINKEDIN_ID = "SELECT * FROM jobs WHERE linkedin_job_id = ?"

LIST_JOBS_BY_STATUS = "SELECT * FROM jobs WHERE status = ? ORDER BY match_score DESC, discovered_at DESC"

LIST_QUALIFIED_WITHOUT_APPLICATION = """
SELECT j.* FROM jobs j
LEFT JOIN applications a ON a.job_id = j.id
WHERE j.status = 'qualified'
  AND j.apply_type NOT IN ('interest_only', 'expired')
  AND (
    a.id IS NULL
    OR (
      -- Only the most-recent application matters; retry if it failed
      a.id = (SELECT id FROM applications WHERE job_id = j.id ORDER BY created_at DESC LIMIT 1)
      AND a.status = 'failed'
    )
  )
ORDER BY j.match_score DESC
"""

UPDATE_JOB_STATUS = """
UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE id = ?
"""

UPDATE_JOB_SCORE = """
UPDATE jobs
SET match_score = ?, match_reasoning = ?, status = ?, updated_at = datetime('now')
WHERE id = ?
"""

UPDATE_JOB_FULL = """
UPDATE jobs SET
    title = :title,
    company = :company,
    location = :location,
    employment_type = :employment_type,
    experience_level = :experience_level,
    salary_range = :salary_range,
    description = :description,
    external_url = :external_url,
    apply_type = :apply_type,
    company_domain = :company_domain,
    match_score = :match_score,
    match_reasoning = :match_reasoning,
    status = :status,
    updated_at = datetime('now')
WHERE id = :id
"""

EXISTS_JOB = "SELECT 1 FROM jobs WHERE linkedin_job_id = ?"

# ── Applications ──────────────────────────────────────────────────────────────

INSERT_APPLICATION = """
INSERT INTO applications (
    job_id, resume_path, cover_letter_path, resume_text,
    cover_letter_text, status, questions_json
) VALUES (
    :job_id, :resume_path, :cover_letter_path, :resume_text,
    :cover_letter_text, :status, :questions_json
)
"""

GET_APPLICATION_BY_ID = "SELECT * FROM applications WHERE id = ?"

GET_APPLICATION_BY_JOB = "SELECT * FROM applications WHERE job_id = ? ORDER BY id DESC LIMIT 1"

UPDATE_APPLICATION_STATUS = """
UPDATE applications
SET status = ?, error_message = ?, updated_at = datetime('now')
WHERE id = ?
"""

UPDATE_APPLICATION_SUBMITTED = """
UPDATE applications
SET status = 'submitted', submitted_at = datetime('now'),
    attempt_count = attempt_count + 1, updated_at = datetime('now')
WHERE id = ?
"""

INCREMENT_ATTEMPT = """
UPDATE applications
SET attempt_count = attempt_count + 1, updated_at = datetime('now')
WHERE id = ?
"""

APPS_SUBMITTED_TODAY = """
SELECT COUNT(*) FROM applications
WHERE status = 'submitted'
AND date(submitted_at) = date('now')
"""

# ── Credentials ───────────────────────────────────────────────────────────────

INSERT_CREDENTIAL = """
INSERT INTO credentials (domain, company, username, password, extra_data)
VALUES (:domain, :company, :username, :password, :extra_data)
ON CONFLICT(domain, username) DO UPDATE SET
    password = excluded.password,
    extra_data = excluded.extra_data,
    updated_at = datetime('now')
"""

GET_CREDENTIAL = "SELECT * FROM credentials WHERE domain = ? AND username = ?"

GET_CREDENTIALS_BY_DOMAIN = (
    "SELECT * FROM credentials WHERE domain = ? "
    "ORDER BY datetime(updated_at) DESC, id DESC"
)

DELETE_CREDENTIAL = "DELETE FROM credentials WHERE domain = ? AND username = ?"

# ── Email Log ─────────────────────────────────────────────────────────────────

INSERT_EMAIL = """
INSERT INTO email_log (
    gmail_message_id, thread_id, from_address, to_address,
    subject, body_preview, received_at, classification,
    confidence, linked_job_id, action_taken, action_details
) VALUES (
    :gmail_message_id, :thread_id, :from_address, :to_address,
    :subject, :body_preview, :received_at, :classification,
    :confidence, :linked_job_id, :action_taken, :action_details
)
"""

GET_EMAIL_BY_GMAIL_ID = "SELECT * FROM email_log WHERE gmail_message_id = ?"

LIST_EMAILS_BY_CLASSIFICATION = """
SELECT * FROM email_log WHERE classification = ?
ORDER BY received_at DESC
"""

EXISTS_EMAIL = "SELECT 1 FROM email_log WHERE gmail_message_id = ?"

# ── Agent Runs ────────────────────────────────────────────────────────────────

INSERT_AGENT_RUN = """
INSERT INTO agent_runs (agent_name, status)
VALUES (?, 'running')
"""

FINISH_AGENT_RUN = """
UPDATE agent_runs SET
    finished_at = datetime('now'),
    status = ?,
    jobs_found = ?,
    apps_submitted = ?,
    emails_processed = ?,
    error_message = ?,
    details_json = ?
WHERE id = ?
"""

LIST_RECENT_AGENT_RUNS = """
SELECT * FROM agent_runs
WHERE agent_name = ?
ORDER BY started_at DESC
LIMIT ?
"""

# ── LLM Usage ─────────────────────────────────────────────────────────────────

INSERT_LLM_USAGE = """
INSERT INTO llm_usage (agent_name, model, purpose, input_tokens, output_tokens, cost_usd, job_id)
VALUES (:agent_name, :model, :purpose, :input_tokens, :output_tokens, :cost_usd, :job_id)
"""

DAILY_COST = """
SELECT COALESCE(SUM(cost_usd), 0.0) FROM llm_usage
WHERE date(created_at) = date('now')
"""

COST_BY_AGENT_TODAY = """
SELECT agent_name, COALESCE(SUM(cost_usd), 0.0) as total_cost
FROM llm_usage
WHERE date(created_at) = date('now')
GROUP BY agent_name
"""

# ── QA Cache ───────────────────────────────────────────────────────────────────

GET_QA_CACHE = "SELECT * FROM qa_cache WHERE question_key=? AND options_hash=?"

UPSERT_QA_CACHE = """
INSERT INTO qa_cache (question_key, options_hash, field_type, answer, confidence, source, times_used)
VALUES (?, ?, ?, ?, ?, ?, 1)
ON CONFLICT(question_key, options_hash) DO UPDATE SET
    answer=excluded.answer, confidence=excluded.confidence, source=excluded.source,
    times_used=times_used+1, updated_at=datetime('now')
"""

# ── Workday tenant capabilities ──────────────────────────────────────────────

GET_WORKDAY_TENANT = "SELECT * FROM workday_tenants WHERE domain = ?"

UPSERT_WORKDAY_TENANT = """
INSERT INTO workday_tenants (domain, auth_mode, status, notes)
VALUES (?, ?, ?, ?)
ON CONFLICT(domain) DO UPDATE SET
    auth_mode=excluded.auth_mode,
    status=excluded.status,
    notes=excluded.notes,
    updated_at=datetime('now')
"""

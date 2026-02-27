-- JobHunter Database Schema

-- jobs table
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_job_id TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT,
    employment_type TEXT,
    experience_level TEXT,
    salary_range    TEXT,
    description     TEXT,
    job_url         TEXT NOT NULL,
    external_url    TEXT,
    apply_type      TEXT NOT NULL DEFAULT 'unknown',
    company_domain  TEXT,
    match_score     REAL,
    match_reasoning TEXT,
    search_query    TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    discovered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_apply_type ON jobs(apply_type);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

-- applications table
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    resume_path     TEXT,
    cover_letter_path TEXT,
    resume_text     TEXT,
    cover_letter_text TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    questions_json  TEXT,
    submitted_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

-- credentials table
CREATE TABLE IF NOT EXISTS credentials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    company         TEXT,
    username        TEXT NOT NULL,
    password        TEXT NOT NULL,
    extra_data      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(domain, username)
);
CREATE INDEX IF NOT EXISTS idx_credentials_domain ON credentials(domain);

-- email_log table
CREATE TABLE IF NOT EXISTS email_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT UNIQUE NOT NULL,
    thread_id       TEXT,
    from_address    TEXT NOT NULL,
    to_address      TEXT,
    subject         TEXT NOT NULL,
    body_preview    TEXT,
    received_at     TEXT NOT NULL,
    classification  TEXT,
    confidence      REAL,
    linked_job_id   INTEGER REFERENCES jobs(id),
    action_taken    TEXT,
    action_details  TEXT,
    processed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_email_classification ON email_log(classification);
CREATE INDEX IF NOT EXISTS idx_email_linked_job ON email_log(linked_job_id);
CREATE INDEX IF NOT EXISTS idx_email_received ON email_log(received_at);

-- agent_runs table
CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    jobs_found      INTEGER DEFAULT 0,
    apps_submitted  INTEGER DEFAULT 0,
    emails_processed INTEGER DEFAULT 0,
    error_message   TEXT,
    details_json    TEXT
);

-- llm_usage table
CREATE TABLE IF NOT EXISTS llm_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    model           TEXT NOT NULL,
    purpose         TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_usd        REAL,
    job_id          INTEGER REFERENCES jobs(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_usage(agent_name);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);

-- qa_cache table — stores answered application questions for cross-application reuse
CREATE TABLE IF NOT EXISTS qa_cache (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    question_key TEXT NOT NULL,        -- normalized question text
    options_hash TEXT NOT NULL DEFAULT '',  -- md5[:8] of sorted options; '' for text/textarea
    field_type   TEXT NOT NULL,
    answer       TEXT NOT NULL,
    confidence   REAL NOT NULL,
    source       TEXT NOT NULL,        -- 'claude' | 'strategic' | 'vision'
    times_used   INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(question_key, options_hash)
);
CREATE INDEX IF NOT EXISTS idx_qa_cache_key ON qa_cache(question_key);

-- workday_tenants table — per-tenant auth capability routing
CREATE TABLE IF NOT EXISTS workday_tenants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    domain     TEXT NOT NULL UNIQUE,
    auth_mode  TEXT NOT NULL DEFAULT 'auto',
    status     TEXT NOT NULL DEFAULT 'active',
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_workday_tenants_mode ON workday_tenants(auth_mode);

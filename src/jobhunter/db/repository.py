"""CRUD repository classes for all database tables."""

import json
import sqlite3
from typing import Optional

from .models import AgentRun, Application, Credential, EmailLog, Job, LlmUsage, QACache
from . import queries


def _row_to_job(row: sqlite3.Row) -> Job:
    d = dict(row)
    return Job(**d)


def _row_to_application(row: sqlite3.Row) -> Application:
    d = dict(row)
    return Application(**d)


def _row_to_credential(row: sqlite3.Row) -> Credential:
    d = dict(row)
    return Credential(**d)


def _row_to_email(row: sqlite3.Row) -> EmailLog:
    d = dict(row)
    return EmailLog(**d)


def _row_to_agent_run(row: sqlite3.Row) -> AgentRun:
    d = dict(row)
    return AgentRun(**d)


class JobRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def exists(self, linkedin_job_id: str) -> bool:
        row = self._conn.execute(queries.EXISTS_JOB, (linkedin_job_id,)).fetchone()
        return row is not None

    def insert(self, job: Job) -> int:
        """Insert a new job; return the new row id."""
        cur = self._conn.execute(queries.INSERT_JOB, {
            "linkedin_job_id": job.linkedin_job_id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "employment_type": job.employment_type,
            "experience_level": job.experience_level,
            "salary_range": job.salary_range,
            "description": job.description,
            "job_url": job.job_url,
            "external_url": job.external_url,
            "apply_type": job.apply_type,
            "company_domain": job.company_domain,
            "match_score": job.match_score,
            "match_reasoning": job.match_reasoning,
            "search_query": job.search_query,
            "status": job.status,
        })
        self._conn.commit()
        return cur.lastrowid

    def get_by_id(self, job_id: int) -> Optional[Job]:
        row = self._conn.execute(queries.GET_JOB_BY_ID, (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def get_by_linkedin_id(self, linkedin_job_id: str) -> Optional[Job]:
        row = self._conn.execute(queries.GET_JOB_BY_LINKEDIN_ID, (linkedin_job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def list_by_status(self, status: str) -> list[Job]:
        rows = self._conn.execute(queries.LIST_JOBS_BY_STATUS, (status,)).fetchall()
        return [_row_to_job(r) for r in rows]

    def list_qualified_without_application(self) -> list[Job]:
        rows = self._conn.execute(queries.LIST_QUALIFIED_WITHOUT_APPLICATION).fetchall()
        return [_row_to_job(r) for r in rows]

    def update_status(self, job_id: int, status: str) -> None:
        self._conn.execute(queries.UPDATE_JOB_STATUS, (status, job_id))
        self._conn.commit()

    def update_score(self, job_id: int, score: float, reasoning: str, status: str) -> None:
        self._conn.execute(queries.UPDATE_JOB_SCORE, (score, reasoning, status, job_id))
        self._conn.commit()

    def upsert(self, job: Job) -> int:
        """Insert or update by linkedin_job_id; return row id."""
        existing = self.get_by_linkedin_id(job.linkedin_job_id)
        if existing is None:
            return self.insert(job)
        job.id = existing.id
        self._conn.execute(queries.UPDATE_JOB_FULL, {
            "id": existing.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "employment_type": job.employment_type,
            "experience_level": job.experience_level,
            "salary_range": job.salary_range,
            "description": job.description,
            "external_url": job.external_url,
            "apply_type": job.apply_type,
            "company_domain": job.company_domain,
            "match_score": job.match_score,
            "match_reasoning": job.match_reasoning,
            "status": job.status,
        })
        self._conn.commit()
        return existing.id


class ApplicationRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, app: Application) -> int:
        cur = self._conn.execute(queries.INSERT_APPLICATION, {
            "job_id": app.job_id,
            "resume_path": app.resume_path,
            "cover_letter_path": app.cover_letter_path,
            "resume_text": app.resume_text,
            "cover_letter_text": app.cover_letter_text,
            "status": app.status,
            "questions_json": app.questions_json,
        })
        self._conn.commit()
        return cur.lastrowid

    def get_by_id(self, app_id: int) -> Optional[Application]:
        row = self._conn.execute(queries.GET_APPLICATION_BY_ID, (app_id,)).fetchone()
        return _row_to_application(row) if row else None

    def get_latest_for_job(self, job_id: int) -> Optional[Application]:
        row = self._conn.execute(queries.GET_APPLICATION_BY_JOB, (job_id,)).fetchone()
        return _row_to_application(row) if row else None

    def update_status(self, app_id: int, status: str, error: Optional[str] = None) -> None:
        self._conn.execute(queries.UPDATE_APPLICATION_STATUS, (status, error, app_id))
        self._conn.commit()

    def mark_submitted(self, app_id: int) -> None:
        self._conn.execute(queries.UPDATE_APPLICATION_SUBMITTED, (app_id,))
        self._conn.commit()

    def increment_attempt(self, app_id: int) -> None:
        self._conn.execute(queries.INCREMENT_ATTEMPT, (app_id,))
        self._conn.commit()

    def count_submitted_today(self) -> int:
        row = self._conn.execute(queries.APPS_SUBMITTED_TODAY).fetchone()
        return row[0] if row else 0


class CredentialRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, cred: Credential) -> None:
        """Insert or update credential (encrypted password should be pre-encrypted)."""
        self._conn.execute(queries.INSERT_CREDENTIAL, {
            "domain": cred.domain,
            "company": cred.company,
            "username": cred.username,
            "password": cred.password,
            "extra_data": cred.extra_data,
        })
        self._conn.commit()

    def get(self, domain: str, username: str) -> Optional[Credential]:
        row = self._conn.execute(queries.GET_CREDENTIAL, (domain, username)).fetchone()
        return _row_to_credential(row) if row else None

    def list_by_domain(self, domain: str) -> list[Credential]:
        rows = self._conn.execute(queries.GET_CREDENTIALS_BY_DOMAIN, (domain,)).fetchall()
        return [_row_to_credential(r) for r in rows]

    def delete(self, domain: str, username: str) -> None:
        self._conn.execute(queries.DELETE_CREDENTIAL, (domain, username))
        self._conn.commit()


class EmailRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def exists(self, gmail_message_id: str) -> bool:
        row = self._conn.execute(queries.EXISTS_EMAIL, (gmail_message_id,)).fetchone()
        return row is not None

    def insert(self, email: EmailLog) -> int:
        cur = self._conn.execute(queries.INSERT_EMAIL, {
            "gmail_message_id": email.gmail_message_id,
            "thread_id": email.thread_id,
            "from_address": email.from_address,
            "to_address": email.to_address,
            "subject": email.subject,
            "body_preview": email.body_preview,
            "received_at": email.received_at,
            "classification": email.classification,
            "confidence": email.confidence,
            "linked_job_id": email.linked_job_id,
            "action_taken": email.action_taken,
            "action_details": email.action_details,
        })
        self._conn.commit()
        return cur.lastrowid

    def get_by_gmail_id(self, gmail_message_id: str) -> Optional[EmailLog]:
        row = self._conn.execute(queries.GET_EMAIL_BY_GMAIL_ID, (gmail_message_id,)).fetchone()
        return _row_to_email(row) if row else None

    def list_by_classification(self, classification: str) -> list[EmailLog]:
        rows = self._conn.execute(
            queries.LIST_EMAILS_BY_CLASSIFICATION, (classification,)
        ).fetchall()
        return [_row_to_email(r) for r in rows]


class AgentRunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, agent_name: str) -> int:
        cur = self._conn.execute(queries.INSERT_AGENT_RUN, (agent_name,))
        self._conn.commit()
        return cur.lastrowid

    def finish(
        self,
        run_id: int,
        status: str,
        jobs_found: int = 0,
        apps_submitted: int = 0,
        emails_processed: int = 0,
        error_message: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> None:
        details_json = json.dumps(details) if details else None
        self._conn.execute(queries.FINISH_AGENT_RUN, (
            status, jobs_found, apps_submitted, emails_processed,
            error_message, details_json, run_id,
        ))
        self._conn.commit()

    def list_recent(self, agent_name: str, limit: int = 10) -> list[AgentRun]:
        rows = self._conn.execute(
            queries.LIST_RECENT_AGENT_RUNS, (agent_name, limit)
        ).fetchall()
        return [_row_to_agent_run(r) for r in rows]


class LlmUsageRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, usage: LlmUsage) -> int:
        cur = self._conn.execute(queries.INSERT_LLM_USAGE, {
            "agent_name": usage.agent_name,
            "model": usage.model,
            "purpose": usage.purpose,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "job_id": usage.job_id,
        })
        self._conn.commit()
        return cur.lastrowid

    def daily_cost(self) -> float:
        row = self._conn.execute(queries.DAILY_COST).fetchone()
        return float(row[0]) if row else 0.0

    def cost_by_agent_today(self) -> dict[str, float]:
        rows = self._conn.execute(queries.COST_BY_AGENT_TODAY).fetchall()
        return {r["agent_name"]: float(r["total_cost"]) for r in rows}


def _row_to_qa_cache(row: sqlite3.Row) -> QACache:
    d = dict(row)
    return QACache(**d)


class QACacheRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, question_key: str, options_hash: str) -> Optional[QACache]:
        row = self._conn.execute(queries.GET_QA_CACHE, (question_key, options_hash)).fetchone()
        return _row_to_qa_cache(row) if row else None

    def upsert(self, entry: QACache) -> None:
        self._conn.execute(queries.UPSERT_QA_CACHE, (
            entry.question_key, entry.options_hash, entry.field_type,
            entry.answer, entry.confidence, entry.source,
        ))
        self._conn.commit()

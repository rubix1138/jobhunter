"""Dataclass models for database rows."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Job:
    linkedin_job_id: str
    title: str
    company: str
    job_url: str
    id: Optional[int] = None
    location: Optional[str] = None
    employment_type: Optional[str] = None
    experience_level: Optional[str] = None
    salary_range: Optional[str] = None
    description: Optional[str] = None
    external_url: Optional[str] = None
    apply_type: str = "unknown"
    company_domain: Optional[str] = None
    match_score: Optional[float] = None
    match_reasoning: Optional[str] = None
    search_query: Optional[str] = None
    status: str = "new"
    discovered_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Application:
    job_id: int
    id: Optional[int] = None
    resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    resume_text: Optional[str] = None
    cover_letter_text: Optional[str] = None
    status: str = "pending"
    error_message: Optional[str] = None
    attempt_count: int = 0
    questions_json: Optional[str] = None
    submitted_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Credential:
    domain: str
    username: str
    password: str  # stored encrypted
    id: Optional[int] = None
    company: Optional[str] = None
    extra_data: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class EmailLog:
    gmail_message_id: str
    from_address: str
    subject: str
    received_at: str
    id: Optional[int] = None
    thread_id: Optional[str] = None
    to_address: Optional[str] = None
    body_preview: Optional[str] = None
    classification: Optional[str] = None
    confidence: Optional[float] = None
    linked_job_id: Optional[int] = None
    action_taken: Optional[str] = None
    action_details: Optional[str] = None
    processed_at: Optional[str] = None


@dataclass
class AgentRun:
    agent_name: str
    id: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    status: str = "running"
    jobs_found: int = 0
    apps_submitted: int = 0
    emails_processed: int = 0
    error_message: Optional[str] = None
    details_json: Optional[str] = None


@dataclass
class LlmUsage:
    agent_name: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cost_usd: Optional[float] = None
    job_id: Optional[int] = None
    id: Optional[int] = None
    created_at: Optional[str] = None


@dataclass
class QACache:
    question_key: str
    options_hash: str
    field_type: str
    answer: str
    confidence: float
    source: str
    times_used: int = 1
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class WorkdayTenant:
    domain: str
    auth_mode: str = "auto"  # auto|create_account|guest|signin_only|sso_only
    status: str = "active"   # active|blocked
    notes: Optional[str] = None
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

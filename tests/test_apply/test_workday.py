"""Tests for Workday applicator — credential management, URL parsing, section routing."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from jobhunter.applicators.workday import WorkdayApplicator, _extract_domain
from jobhunter.crypto.vault import CredentialVault
from jobhunter.db.engine import init_db
from jobhunter.db.models import Credential
from jobhunter.db.repository import CredentialRepo
from jobhunter.utils.profile_loader import UserProfile


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_profile() -> UserProfile:
    return UserProfile.model_validate({
        "personal": {
            "first_name": "Jane", "last_name": "Doe",
            "email": "jane@jobs.com", "personal_email": "jane@home.com",
            "phone": "555-0100", "location": "San Francisco, CA",
            "linkedin_url": "https://linkedin.com/in/jane",
        },
        "skills": {
            "programming_languages": [{"name": "Python", "years": 5, "proficiency": "expert"}],
            "frameworks_and_tools": ["FastAPI"],
        },
        "preferences": {"job_titles": ["Engineer"]},
        "application_answers": {
            "years_of_experience": 5, "desired_salary": "180000",
            "start_date": "2 weeks", "sponsorship_required": False,
            "has_disability": "prefer_not_to_answer", "veteran_status": "not_a_veteran",
            "gender": "prefer_not_to_answer", "ethnicity": "prefer_not_to_answer",
            "how_did_you_hear": "LinkedIn", "willing_to_travel": "10%",
        },
    })


@pytest.fixture
def vault():
    return CredentialVault(key=CredentialVault.generate_key())


@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


@pytest.fixture
def cred_repo(db_conn):
    return CredentialRepo(db_conn)


def make_applicator(vault, cred_repo):
    profile = make_profile()
    page = MagicMock()
    llm = MagicMock()
    from pathlib import Path
    return WorkdayApplicator(
        page=page,
        llm=llm,
        profile=profile,
        vault=vault,
        cred_repo=cred_repo,
        resume_path=Path("/tmp/resume.pdf"),
        vision=None,
    )


# ── Domain extraction ─────────────────────────────────────────────────────────

class TestExtractDomain:
    def test_myworkdayjobs(self):
        # Full hostname used so different Workday tenants get separate credential entries
        url = "https://acme.myworkdayjobs.com/en-US/External/job/12345"
        assert _extract_domain(url) == "acme.myworkdayjobs.com"

    def test_workday_site(self):
        url = "https://wd3.myworkdaysite.com/recruiting/company/careers"
        assert _extract_domain(url) == "wd3.myworkdaysite.com"

    def test_simple_domain(self):
        url = "https://jobs.example.com/apply"
        assert _extract_domain(url) == "jobs.example.com"

    def test_no_subdomain(self):
        url = "https://example.com/jobs"
        assert _extract_domain(url) == "example.com"

    def test_invalid_url(self):
        result = _extract_domain("not-a-url")
        assert isinstance(result, str)


# ── Credential storage ────────────────────────────────────────────────────────

class TestCredentialStorage:
    def test_encrypt_store_retrieve(self, vault, cred_repo):
        password = "MyStr0ng!Pass"
        encrypted = vault.encrypt(password)
        cred = Credential(domain="myworkdayjobs.com", username="jane@jobs.com", password=encrypted)
        cred_repo.upsert(cred)

        stored = cred_repo.get("myworkdayjobs.com", "jane@jobs.com")
        assert stored is not None
        assert vault.decrypt(stored.password) == password

    def test_upsert_updates_password(self, vault, cred_repo):
        cred = Credential(domain="wd.com", username="u@u.com", password=vault.encrypt("old"))
        cred_repo.upsert(cred)
        cred.password = vault.encrypt("new")
        cred_repo.upsert(cred)
        stored = cred_repo.get("wd.com", "u@u.com")
        assert vault.decrypt(stored.password) == "new"

    def test_missing_credential_returns_none(self, cred_repo):
        assert cred_repo.get("nonexistent.com", "nobody@nowhere.com") is None

    def test_generated_password_stored_encrypted(self, vault, cred_repo):
        pw = CredentialVault.generate_password(24)
        assert len(pw) == 24
        encrypted = vault.encrypt(pw)
        cred = Credential(domain="test.com", username="u@u.com", password=encrypted)
        cred_repo.upsert(cred)
        stored = cred_repo.get("test.com", "u@u.com")
        assert vault.decrypt(stored.password) == pw


# ── Email verification detection ──────────────────────────────────────────────

class TestEmailVerificationWall:
    @pytest.mark.asyncio
    async def test_detects_verify_email(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        applicator._page.content = AsyncMock(
            return_value="<html><body>Please verify your email address to continue.</body></html>"
        )
        assert await applicator._is_email_verification_wall() is True

    @pytest.mark.asyncio
    async def test_detects_check_email(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        applicator._page.content = AsyncMock(
            return_value="<html><body>Check your email for a verification link.</body></html>"
        )
        assert await applicator._is_email_verification_wall() is True

    @pytest.mark.asyncio
    async def test_clean_page_not_flagged(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        applicator._page.content = AsyncMock(
            return_value="<html><body><h1>My Information</h1><form>...</form></body></html>"
        )
        assert await applicator._is_email_verification_wall() is False


# ── Section routing ───────────────────────────────────────────────────────────

class TestSectionRouting:
    @pytest.mark.asyncio
    async def test_routes_personal_section(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        applicator._handle_personal_section = AsyncMock()
        applicator._handle_generic_section = AsyncMock()
        await applicator._handle_section("my information", "context")
        applicator._handle_personal_section.assert_called_once()
        applicator._handle_generic_section.assert_not_called()

    @pytest.mark.asyncio
    async def test_routes_document_section(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        applicator._handle_document_section = AsyncMock()
        applicator._handle_generic_section = AsyncMock()
        await applicator._handle_section("resume / cv", "context")
        applicator._handle_document_section.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_eeo_section(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        applicator._handle_eeo_section = AsyncMock()
        await applicator._handle_section("voluntary self-identification", "context")
        applicator._handle_eeo_section.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_unknown_to_generic(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        applicator._handle_generic_section = AsyncMock()
        await applicator._handle_section("something unexpected", "context")
        applicator._handle_generic_section.assert_called_once()

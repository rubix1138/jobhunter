"""Tests for FormFillingAgent — credential management, auth, domain extraction."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from jobhunter.applicators.form_filling import (
    FormFillingAgent,
    _extract_domain,
)
from jobhunter.crypto.vault import CredentialVault
from jobhunter.db.engine import init_db
from jobhunter.db.models import Credential, Job
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
    return FormFillingAgent(
        page=page,
        llm=llm,
        profile=profile,
        resume_path=Path("/tmp/resume.pdf"),
        vault=vault,
        cred_repo=cred_repo,
        vision=None,
    )


# ── Domain extraction ─────────────────────────────────────────────────────────

class TestExtractDomain:
    def test_myworkdayjobs(self):
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


# ── Apply with no URL ────────────────────────────────────────────────────────

class TestApplyNoUrl:
    @pytest.mark.asyncio
    async def test_returns_false_with_no_url(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        from jobhunter.db.models import Application
        job = Job(
            linkedin_job_id="j1",
            title="Engineer",
            company="Acme",
            job_url="https://linkedin.com/jobs/view/1",
            external_url=None,
            apply_type="external_workday",
        )
        app = Application(job_id=1)
        result = await applicator.apply(job, app)
        assert result is False


class TestAuthHandling:
    @pytest.mark.asyncio
    async def test_handle_auth_uses_guest_button_first(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        guest_btn = MagicMock()
        guest_btn.is_visible = AsyncMock(return_value=True)
        guest_btn.click = AsyncMock()

        def get_by_role_side_effect(role, **_kwargs):
            if role == "button":
                return MagicMock(first=guest_btn)
            hidden = MagicMock()
            hidden.is_visible = AsyncMock(return_value=False)
            hidden.click = AsyncMock()
            return MagicMock(first=hidden)

        applicator._page.get_by_role.side_effect = get_by_role_side_effect
        applicator._try_login = AsyncMock(return_value=False)
        applicator._try_create_account = AsyncMock(return_value=False)

        with patch("jobhunter.applicators.form_filling._GUEST_LABELS", ("Continue as Guest",)), patch(
            "jobhunter.applicators.form_filling.random_delay", new=AsyncMock()
        ):
            result = await applicator._handle_auth_if_needed("https://acme.myworkdayjobs.com/apply")

        assert result is True
        guest_btn.click.assert_awaited_once()
        applicator._try_login.assert_not_awaited()
        applicator._try_create_account.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_auth_uses_stored_credentials_for_login(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        cred_repo.upsert(
            Credential(
                domain="acme.myworkdayjobs.com",
                username="jane@jobs.com",
                password=vault.encrypt("SecretPass!23"),
            )
        )

        hidden = MagicMock()
        hidden.is_visible = AsyncMock(return_value=False)
        hidden.click = AsyncMock()
        applicator._page.get_by_role.return_value.first = hidden
        applicator._try_login = AsyncMock(return_value=True)
        applicator._try_create_account = AsyncMock(return_value=False)

        with patch("jobhunter.applicators.form_filling._GUEST_LABELS", ("Continue as Guest",)):
            result = await applicator._handle_auth_if_needed("https://acme.myworkdayjobs.com/apply")

        assert result is True
        applicator._try_login.assert_awaited_once_with("jane@jobs.com", "SecretPass!23")
        applicator._try_create_account.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_auth_falls_back_to_create_account(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        hidden = MagicMock()
        hidden.is_visible = AsyncMock(return_value=False)
        hidden.click = AsyncMock()
        applicator._page.get_by_role.return_value.first = hidden
        applicator._try_login = AsyncMock(return_value=False)
        applicator._try_create_account = AsyncMock(return_value=True)

        with patch("jobhunter.applicators.form_filling._GUEST_LABELS", ("Continue as Guest",)):
            result = await applicator._handle_auth_if_needed("https://acme.myworkdayjobs.com/apply")

        assert result is True
        applicator._try_create_account.assert_awaited_once_with(
            "acme.myworkdayjobs.com", "jane@jobs.com"
        )


class TestTryLogin:
    @pytest.mark.asyncio
    async def test_try_login_success_with_labeled_fields(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)

        clickable = MagicMock()
        clickable.is_visible = AsyncMock(return_value=True)
        clickable.click = AsyncMock()
        applicator._page.get_by_role.return_value.first = clickable

        email_field = MagicMock()
        email_field.is_visible = AsyncMock(return_value=True)
        email_field.fill = AsyncMock()
        password_field = MagicMock()
        password_field.is_visible = AsyncMock(return_value=True)
        password_field.fill = AsyncMock()

        def get_by_label_side_effect(label, **_kwargs):
            if "Email" in label or "Username" in label:
                return email_field
            return password_field

        applicator._page.get_by_label.side_effect = get_by_label_side_effect

        with patch("jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()), patch(
            "jobhunter.applicators.form_filling.random_delay", new=AsyncMock()
        ):
            result = await applicator._try_login("jane@jobs.com", "SecretPass!23")

        assert result is True
        email_field.fill.assert_awaited_once_with("jane@jobs.com")
        password_field.fill.assert_awaited_once_with("SecretPass!23")
        assert clickable.click.await_count >= 1

    @pytest.mark.asyncio
    async def test_try_login_returns_false_when_email_field_missing(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        hidden = MagicMock()
        hidden.is_visible = AsyncMock(return_value=False)
        hidden.click = AsyncMock()
        applicator._page.get_by_role.return_value.first = hidden
        applicator._page.get_by_label.side_effect = Exception("missing")

        email_input = MagicMock()
        email_input.is_visible = AsyncMock(return_value=False)
        applicator._page.locator.return_value.first = email_input

        result = await applicator._try_login("jane@jobs.com", "SecretPass!23")
        assert result is False


class TestTryCreateAccount:
    @pytest.mark.asyncio
    async def test_try_create_account_stores_encrypted_credentials(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)

        create_btn = MagicMock()
        create_btn.is_visible = AsyncMock(return_value=True)
        create_btn.click = AsyncMock()
        applicator._page.get_by_role.return_value.first = create_btn

        email_field = MagicMock()
        email_field.is_visible = AsyncMock(return_value=True)
        email_field.fill = AsyncMock()
        applicator._page.get_by_label.return_value = email_field

        pw_field_1 = MagicMock()
        pw_field_1.is_visible = AsyncMock(return_value=True)
        pw_field_1.fill = AsyncMock()
        pw_field_2 = MagicMock()
        pw_field_2.is_visible = AsyncMock(return_value=True)
        pw_field_2.fill = AsyncMock()
        pw_locator = MagicMock()
        pw_locator.all = AsyncMock(return_value=[pw_field_1, pw_field_2])
        applicator._page.locator.return_value = pw_locator

        with patch("jobhunter.applicators.form_filling._generate_password", return_value="StrongPass!1"), patch(
            "jobhunter.applicators.form_filling.micro_delay", new=AsyncMock()
        ), patch("jobhunter.applicators.form_filling.random_delay", new=AsyncMock()):
            result = await applicator._try_create_account("acme.myworkdayjobs.com", "jane@jobs.com")

        assert result is True
        email_field.fill.assert_awaited_once_with("jane+acme@jobs.com")
        stored = cred_repo.get("acme.myworkdayjobs.com", "jane+acme@jobs.com")
        assert stored is not None
        assert vault.decrypt(stored.password) == "StrongPass!1"

    @pytest.mark.asyncio
    async def test_try_create_account_returns_false_when_create_not_clickable(self, vault, cred_repo):
        applicator = make_applicator(vault, cred_repo)
        hidden = MagicMock()
        hidden.is_visible = AsyncMock(return_value=False)
        hidden.click = AsyncMock()
        applicator._page.get_by_role.return_value.first = hidden

        result = await applicator._try_create_account("acme.myworkdayjobs.com", "jane@jobs.com")
        assert result is False

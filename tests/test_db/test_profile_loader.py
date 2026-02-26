"""Tests for user profile loading and Pydantic validation."""

import textwrap
from pathlib import Path

import pytest
import yaml

from jobhunter.utils.profile_loader import (
    ApplicationAnswers,
    Education,
    Experience,
    PersonalInfo,
    Preferences,
    ProgrammingLanguage,
    Skills,
    UserProfile,
    load_profile,
)


MINIMAL_PROFILE = {
    "personal": {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@jobsearch.com",
        "personal_email": "jane@personal.com",
        "phone": "+1-555-0100",
        "location": "San Francisco, CA",
    },
    "summary": "Experienced software engineer.",
    "experience": [
        {
            "company": "Acme Corp",
            "title": "Software Engineer",
            "start_date": "2021-06",
            "end_date": "present",
        }
    ],
    "education": [
        {
            "institution": "State University",
            "degree": "BS Computer Science",
            "graduation_date": "2021-05",
        }
    ],
    "skills": {
        "programming_languages": [
            {"name": "Python", "years": 5, "proficiency": "expert"}
        ],
        "frameworks_and_tools": ["FastAPI", "Docker"],
    },
    "preferences": {
        "job_titles": ["Senior Software Engineer"],
    },
    "application_answers": {
        "years_of_experience": 5,
    },
}


@pytest.fixture
def profile_file(tmp_path):
    def _write(data):
        path = tmp_path / "user_profile.yaml"
        path.write_text(yaml.dump(data))
        return path
    return _write


class TestLoadProfile:
    def test_loads_minimal_profile(self, profile_file):
        path = profile_file(MINIMAL_PROFILE)
        profile = load_profile(path)
        assert profile.personal.first_name == "Jane"
        assert profile.personal.last_name == "Doe"

    def test_full_name(self, profile_file):
        path = profile_file(MINIMAL_PROFILE)
        profile = load_profile(path)
        assert profile.full_name() == "Jane Doe"

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_profile("/nonexistent/path/profile.yaml")

    def test_empty_file_raises(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_profile(path)

    def test_no_job_titles_raises(self, profile_file):
        data = dict(MINIMAL_PROFILE)
        data["preferences"] = {"job_titles": []}
        with pytest.raises(Exception):  # Pydantic validation error
            load_profile(profile_file(data))

    def test_warns_on_missing_name(self, profile_file):
        data = dict(MINIMAL_PROFILE)
        data["personal"] = dict(MINIMAL_PROFILE["personal"])
        data["personal"]["first_name"] = ""
        data["personal"]["last_name"] = ""
        profile = load_profile(profile_file(data))
        assert any("first_name" in w for w in profile.warnings)

    def test_warns_on_no_experience(self, profile_file):
        data = dict(MINIMAL_PROFILE)
        data["experience"] = []
        profile = load_profile(profile_file(data))
        assert any("experience" in w.lower() for w in profile.warnings)


class TestPersonalInfo:
    def test_valid(self):
        p = PersonalInfo(
            first_name="John",
            last_name="Smith",
            email="j@jobs.com",
            personal_email="j@personal.com",
            phone="555-1234",
            location="NYC",
        )
        assert p.first_name == "John"

    def test_defaults(self):
        p = PersonalInfo(
            first_name="A", last_name="B",
            email="a@b.com", personal_email="a@c.com",
            phone="", location="",
        )
        assert p.willing_to_relocate is False
        assert p.work_authorization == "US Citizen"


class TestExperience:
    def test_valid_start_date_yyyy_mm(self):
        exp = Experience(company="A", title="Eng", start_date="2021-06")
        assert exp.start_date == "2021-06"

    def test_valid_start_date_yyyy(self):
        exp = Experience(company="A", title="Eng", start_date="2021")
        assert exp.start_date == "2021"

    def test_invalid_start_date(self):
        with pytest.raises(Exception):
            Experience(company="A", title="Eng", start_date="January 2021")

    def test_defaults(self):
        exp = Experience(company="Corp", title="Dev", start_date="2020-01")
        assert exp.end_date == "present"
        assert exp.achievements == []
        assert exp.technologies == []


class TestProgrammingLanguage:
    def test_valid_proficiency(self):
        lang = ProgrammingLanguage(name="Python", proficiency="expert")
        assert lang.proficiency == "expert"

    def test_invalid_proficiency(self):
        with pytest.raises(Exception, match="proficiency"):
            ProgrammingLanguage(name="Python", proficiency="god-tier")


class TestPreferences:
    def test_valid_remote_preference(self):
        for val in ("remote_only", "remote_preferred", "hybrid_ok", "onsite_ok"):
            prefs = Preferences(job_titles=["Eng"], remote_preference=val)
            assert prefs.remote_preference == val

    def test_invalid_remote_preference(self):
        with pytest.raises(Exception, match="remote_preference"):
            Preferences(job_titles=["Eng"], remote_preference="anywhere")

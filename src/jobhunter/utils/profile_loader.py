"""YAML user profile parser with Pydantic validation."""

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class PersonalInfo(BaseModel):
    first_name: str
    last_name: str
    email: str
    personal_email: str
    phone: str
    location: str
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    willing_to_relocate: bool = False
    work_authorization: str = "US Citizen"


class Experience(BaseModel):
    company: str
    title: str
    start_date: str
    end_date: str = "present"
    location: str = ""
    description: str = ""
    achievements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)

    @field_validator("start_date")
    @classmethod
    def validate_start_date(cls, v: str) -> str:
        import re
        if v and v != "present":
            if not re.fullmatch(r"\d{4}(-\d{2})?", v):
                raise ValueError(f"start_date must be YYYY or YYYY-MM, got: {v!r}")
        return v


class Education(BaseModel):
    institution: str
    degree: str
    graduation_date: str = ""
    gpa: str = ""


class ProgrammingLanguage(BaseModel):
    name: str
    years: int = 0
    proficiency: str = "intermediate"

    @field_validator("proficiency")
    @classmethod
    def validate_proficiency(cls, v: str) -> str:
        allowed = {"expert", "advanced", "intermediate", "beginner"}
        if v not in allowed:
            raise ValueError(f"proficiency must be one of {allowed}, got: {v!r}")
        return v


class Certification(BaseModel):
    name: str
    issuer: str = ""
    date: str = ""


class SkillDomain(BaseModel):
    name: str
    details: str = ""
    years: int = 0
    proficiency: str = "advanced"


class Skills(BaseModel):
    domains: list[SkillDomain] = Field(default_factory=list)
    programming_languages: list[ProgrammingLanguage] = Field(default_factory=list)
    frameworks_and_tools: list[str] = Field(default_factory=list)
    security_products: list[str] = Field(default_factory=list)
    infrastructure_and_platforms: list[str] = Field(default_factory=list)
    other_tools: list[str] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)


class Preferences(BaseModel):
    job_titles: list[str]
    target_companies: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    min_salary: int = 0
    max_salary: int = 0
    preferred_salary: int = 0
    remote_preference: str = "remote_preferred"
    locations: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    company_size: list[str] = Field(default_factory=list)
    deal_breakers: list[str] = Field(default_factory=list)

    @field_validator("remote_preference")
    @classmethod
    def validate_remote_pref(cls, v: str) -> str:
        allowed = {"remote_only", "remote_preferred", "hybrid_ok", "onsite_ok"}
        if v not in allowed:
            raise ValueError(f"remote_preference must be one of {allowed}, got: {v!r}")
        return v


class ApplicationAnswers(BaseModel):
    years_of_experience: int = 0
    desired_salary: str = ""
    start_date: str = "2 weeks"
    sponsorship_required: bool = False
    has_disability: str = "prefer_not_to_answer"
    veteran_status: str = "not_a_veteran"
    gender: str = "prefer_not_to_answer"
    ethnicity: str = "prefer_not_to_answer"
    how_did_you_hear: str = "LinkedIn"
    willing_to_travel: str = ""
    custom_answers: dict[str, str] = Field(default_factory=dict)


class Publication(BaseModel):
    title: str
    publisher: str = ""
    year: int = 0


class SpeakingEngagement(BaseModel):
    title: str
    venue: str = ""
    year: str = ""

    @field_validator("year", mode="before")
    @classmethod
    def coerce_year_to_str(cls, v) -> str:
        return str(v) if v is not None else ""


class UserProfile(BaseModel):
    personal: PersonalInfo
    summary: str = ""
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    publications: list[Publication] = Field(default_factory=list)
    speaking_engagements: list[SpeakingEngagement] = Field(default_factory=list)
    preferences: Preferences
    application_answers: ApplicationAnswers = Field(default_factory=ApplicationAnswers)

    @model_validator(mode="after")
    def validate_profile_completeness(self) -> "UserProfile":
        warnings = []
        if not self.personal.first_name or not self.personal.last_name:
            warnings.append("personal.first_name and personal.last_name should be set")
        if not self.personal.email:
            warnings.append("personal.email (dedicated job-search inbox) should be set")
        if not self.personal.personal_email:
            warnings.append("personal.personal_email (forwarding address) should be set")
        if not self.experience:
            warnings.append("No work experience entries found")
        if not self.preferences.job_titles:
            raise ValueError("preferences.job_titles must have at least one entry")
        # Store warnings as a non-validated attribute
        object.__setattr__(self, "_warnings", warnings)
        return self

    @property
    def warnings(self) -> list[str]:
        return getattr(self, "_warnings", [])

    def full_name(self) -> str:
        return f"{self.personal.first_name} {self.personal.last_name}".strip()


def load_profile(path: str | Path = "profile/user_profile.yaml") -> UserProfile:
    """Load and validate the user profile from a YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"User profile not found at {path}. Run 'jobhunter init' first.")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"Profile file {path} is empty")

    return UserProfile.model_validate(raw)

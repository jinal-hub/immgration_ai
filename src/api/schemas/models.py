"""Pydantic request/response schemas for the API."""
from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Applicant
# ---------------------------------------------------------------------------
class VisaIntake(BaseModel):
    """The visa program an applicant is applying for."""
    program_id: str = Field(..., examples=["SKILLED_189", "SKILLED_190", "SKILLED_491"])


# The closed value vocabulary for the applicant's prior Australian visa
# history. Mirrors schema/applicant_intake.schema.json.
VisaHistory = Literal[
    "NONE", "PRIOR_GRANTED", "PRIOR_REFUSED", "PRIOR_CANCELLED", "PRIOR_REMOVED"
]


class ApplicantCreate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: datetime | None = None
    passport_nationality: str | None = None
    current_visa_subclass: str | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    english_level: str | None = None
    points: int | None = Field(default=None, ge=0, le=200)
    occupation: str | None = None
    skills_assessment_status: str | None = None
    health_status: str | None = None
    character_status: str | None = None
    has_regional_nomination: bool | None = None
    visa_subclass: str | None = None
    # First batch of newly modelled applicant concepts (Phase 4).
    visa_history: VisaHistory | None = None
    employer_sponsored: bool | None = None
    work_experience_years: int | None = Field(default=None, ge=0, le=80)
    in_australia_at_application: bool | None = None
    has_health_insurance: bool | None = None
    genuine_temporary_entrant: bool | None = None

    @field_validator("date_of_birth")
    @classmethod
    def reject_future_date_of_birth(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        if normalized > datetime.now(UTC):
            raise ValueError("date_of_birth cannot be in the future")
        return value


class ApplicantUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: datetime | None = None
    passport_nationality: str | None = None
    current_visa_subclass: str | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    english_level: str | None = None
    points: int | None = Field(default=None, ge=0, le=200)
    occupation: str | None = None
    skills_assessment_status: str | None = None
    health_status: str | None = None
    character_status: str | None = None
    has_regional_nomination: bool | None = None
    visa_subclass: str | None = None
    # First batch of newly modelled applicant concepts (Phase 4).
    visa_history: VisaHistory | None = None
    employer_sponsored: bool | None = None
    work_experience_years: int | None = Field(default=None, ge=0, le=80)
    in_australia_at_application: bool | None = None
    has_health_insurance: bool | None = None
    genuine_temporary_entrant: bool | None = None

    @field_validator("date_of_birth")
    @classmethod
    def reject_future_date_of_birth(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        if normalized > datetime.now(UTC):
            raise ValueError("date_of_birth cannot be in the future")
        return value


class ApplicantResponse(BaseModel):
    applicant_id: str
    user_id: str
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: datetime | None = None
    passport_nationality: str | None = None
    current_visa_subclass: str | None = None
    age: int | None = None
    english_level: str | None = None
    points: int | None = None
    occupation: str | None = None
    skills_assessment_status: str | None = None
    health_status: str | None = None
    character_status: str | None = None
    has_regional_nomination: bool | None = None
    visa_subclass: str | None = None
    # First batch of newly modelled applicant concepts (Phase 4).
    visa_history: str | None = None
    employer_sponsored: bool | None = None
    work_experience_years: int | None = None
    in_australia_at_application: bool | None = None
    has_health_insurance: bool | None = None
    genuine_temporary_entrant: bool | None = None
    created_at: datetime | None = None


class ApplicantSourceResponse(BaseModel):
    source_id: str
    authority: str
    url: str
    status: str
    fetch_status: str | None = None
    http_status: int | None = None
    last_checked_at: datetime | None = None
    last_successful_fetch_at: datetime | None = None


class ApplicantProgramResponse(BaseModel):
    program_id: str
    country_id: str
    visa_category: str
    subclass_code: str | None = None
    name: str
    status: str
    active_rule_count: int
    sources: list[ApplicantSourceResponse] = []


class ApplicantIntake(BaseModel):
    """The full intake payload for an eligibility assessment.

    Mirrors schema/applicant_intake.schema.json. Every fact except ``visa``
    is optional: an omitted fact stays *missing* (the rule engine answers
    UNKNOWN for it), which is deliberately different from a fact that was
    supplied and does not meet a threshold (FAIL).
    """
    visa: VisaIntake
    age: int | None = Field(default=None, ge=0, le=120)
    english_level: str | None = None
    points: int | None = Field(default=None, ge=0, le=200)
    occupation: str | None = None
    skills_assessment_status: str | None = None
    health_status: str | None = None
    character_status: str | None = None
    has_regional_nomination: bool | None = None
    visa_subclass: str | None = None
    visa_history: VisaHistory | None = None
    employer_sponsored: bool | None = None
    work_experience_years: int | None = Field(default=None, ge=0, le=80)
    in_australia_at_application: bool | None = None
    has_health_insurance: bool | None = None
    genuine_temporary_entrant: bool | None = None


# ---------------------------------------------------------------------------
# Visa Selection
# ---------------------------------------------------------------------------
class VisaSelectionCreate(BaseModel):
    program_id: str = Field(..., examples=["SKILLED_189", "SKILLED_190", "SKILLED_491"])


class VisaSelectionResponse(BaseModel):
    selection_id: str
    applicant_id: str
    program_id: str
    selected_at: datetime


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------
class AssessRequest(BaseModel):
    applicant_id: str
    program_id: str


class RuleResultResponse(BaseModel):
    result_id: str
    rule_id: str
    rule_version: int
    rule_version_id: int | None = None
    status: str
    field: str | None = None
    actual_value: str | None = None
    expected_value: str | None = None
    operator: str | None = None
    message: str | None = None
    reason: str | None = None
    evidence_text: str | None = None
    source_id: str | None = None
    source_url: str | None = None
    source_authority: str | None = None
    source_status: str | None = None
    source_fetch_status: str | None = None
    source_last_checked_at: datetime | None = None


class GapResponse(BaseModel):
    gap_id: str
    field: str
    priority: str
    message: str
    rule_id: str | None = None
    evidence_text: str | None = None
    answer: Any = None
    source_chunk_ids: list[int] = []


class AssessmentResponse(BaseModel):
    assessment_id: str
    applicant_id: str
    program_id: str
    business_status: str
    overall_status: str
    total_rules: int
    passed_count: int
    failed_count: int
    unknown_count: int
    not_applicable_count: int
    evaluated_at: datetime | None = None
    applicant_snapshot: dict[str, Any] | None = None
    ruleset_hash: str | None = None
    ruleset_id: str | None = None
    rule_results: list[RuleResultResponse] = []
    gaps: list[GapResponse] = []


class AssessmentListResponse(BaseModel):
    assessments: list[AssessmentResponse]
    total: int


# ---------------------------------------------------------------------------
# Evaluate (inline, no persistence)
# ---------------------------------------------------------------------------
class EvaluateRequest(BaseModel):
    applicant_id: str
    program_id: str


class EvaluateResponse(BaseModel):
    business_status: str
    overall_status: str
    total_rules: int
    passed_count: int
    failed_count: int
    unknown_count: int
    not_applicable_count: int
    rule_evaluations: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Gaps & Questions
# ---------------------------------------------------------------------------
class AnswerRequest(BaseModel):
    value: Any


class GapAnswerResponse(BaseModel):
    gap_id: str
    field: str
    answer: Any
    updated_at: datetime


class TenantCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    slug: str = Field(..., min_length=3, max_length=120, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class TenantResponse(BaseModel):
    tenant_id: str
    name: str
    slug: str
    role: str
    status: str


class TenantMemberCreateRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=256)
    role: Literal["CONSULTANT", "REVIEWER", "APPLICANT"]


class TenantMemberResponse(BaseModel):
    membership_id: str
    tenant_id: str
    user_id: str
    email: str
    full_name: str | None = None
    role: str
    status: str


class ApplicationCaseCreateRequest(BaseModel):
    applicant_id: str
    program_id: str
    title: str | None = Field(default=None, max_length=200)
    assessment_id: str | None = None


class ApplicationCaseStatusUpdate(BaseModel):
    status: Literal[
        "IN_PROGRESS", "NEEDS_INFO", "READY_FOR_REVIEW", "CHANGES_REQUESTED",
        "APPROVED", "PACKAGE_READY", "CLOSED",
    ]


class ApplicationCaseResponse(BaseModel):
    case_id: str
    tenant_id: str
    tenant_name: str | None = None
    applicant_id: str
    program_id: str
    created_by: str
    assigned_consultant_id: str | None = None
    assessment_id: str | None = None
    ruleset_id: str | None = None
    status: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class UnassignedCaseResponse(BaseModel):
    case_id: str
    tenant_id: str
    program_id: str
    status: str
    title: str | None = None
    created_at: datetime


class CaseDocumentResponse(BaseModel):
    document_id: str
    case_id: str
    tenant_id: str
    uploaded_by: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    review_status: Literal["PENDING_REVIEW", "ACCEPTED", "REJECTED"]
    rejection_reason: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class QuestionsResponse(BaseModel):
    questions: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


class RegisterResponse(BaseModel):
    user_id: str
    email: str
    full_name: str | None = None
    role: str
    access_token: str
    token_type: str = "bearer"
    expires_in: int


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PASSWORD_MAX_BYTES = 72  # bcrypt's effective input limit


def password_min_length() -> int:
    try:
        value = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))
    except ValueError:
        value = 8
    return max(1, min(value, _PASSWORD_MAX_BYTES))


def normalise_email(value: str) -> str:
    email = (value or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ValueError("email must be a valid address")
    return email


class LoginRequest(BaseModel):
    """Login deliberately has no password-strength rule.

    A wrong short password must reach authentication and produce the same 401 as
    any other wrong password; rejecting it at schema level would leak policy and
    make login behaviour inconsistent with existing accounts.
    """

    email: str
    password: str

    _normalise_login_email = field_validator("email")(normalise_email)


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str | None = None

    _normalise_register_email = field_validator("email")(normalise_email)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("password is required")
        if len(value.encode("utf-8")) > _PASSWORD_MAX_BYTES:
            raise ValueError(
                f"password must be at most {_PASSWORD_MAX_BYTES} bytes"
            )
        minimum = password_min_length()
        if len(value) < minimum:
            raise ValueError(f"password must be at least {minimum} characters")
        if value.strip() != value:
            raise ValueError("password must not have leading/trailing whitespace")
        return value

    @field_validator("full_name")
    @classmethod
    def _validate_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) > 128:
            raise ValueError("full_name must be at most 128 characters")
        return value or None


class UserResponse(BaseModel):
    user_id: str
    email: str
    full_name: str | None = None
    role: str


class PasswordResetRequest(BaseModel):
    email: str

    _normalise_email = field_validator("email")(normalise_email)


class PasswordResetConfirm(BaseModel):
    token: str = Field(..., min_length=16, max_length=256)
    password: str

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("password is required")
        if len(value.encode("utf-8")) > _PASSWORD_MAX_BYTES:
            raise ValueError(f"password must be at most {_PASSWORD_MAX_BYTES} bytes")
        minimum = password_min_length()
        if len(value) < minimum:
            raise ValueError(f"password must be at least {minimum} characters")
        if value.strip() != value:
            raise ValueError("password must not have leading/trailing whitespace")
        return value


class AcceptInvitationRequest(BaseModel):
    token: str = Field(..., min_length=16, max_length=256)


class TenantApplicantResponse(BaseModel):
    applicant_id: str
    user_id: str
    email: str
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class ConfirmFormAnswer(BaseModel):
    pass


class FormReviewRequest(BaseModel):
    status: Literal["REVIEWED", "APPROVED"]
    review_notes: str | None = None


class CaseDocumentDetailResponse(CaseDocumentResponse):
    version: int = 1
    scanner_status: str = "NOT_CONFIGURED"
    scanner_detail: str | None = None


class MessageResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Consultancy workflow (Phase 5-6 sprint)
# ---------------------------------------------------------------------------
class TenantInvitationCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=256)
    role: Literal["CONSULTANT", "REVIEWER", "APPLICANT"]

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return normalise_email(value)


class TenantInvitationResponse(BaseModel):
    invitation_id: str
    tenant_id: str
    email: str
    role: str
    invitation_token: str | None = None
    expires_at: datetime | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    status: str


class TenantMemberUpdate(BaseModel):
    role: Literal["CONSULTANT", "REVIEWER", "APPLICANT"]


class CaseChecklistItemCreate(BaseModel):
    requirement_key: str = Field(..., min_length=1, max_length=128)
    label: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    category: Literal["document", "information", "form"] = "document"
    required: bool = True
    position: int = Field(default=0, ge=0)


class CaseChecklistItemResponse(BaseModel):
    item_id: str
    case_id: str
    tenant_id: str
    requirement_key: str
    label: str
    description: str | None = None
    category: str
    required: bool
    status: str
    evidence_document_id: str | None = None
    rejection_reason: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    position: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CaseChecklistGenerateRequest(BaseModel):
    assessment_id: str | None = None


class ChecklistItemUpdate(BaseModel):
    status: Literal["PENDING", "SUBMITTED", "ACCEPTED", "REJECTED", "WAIVED"]
    rejection_reason: str | None = None


class CaseDocumentUploadResponse(BaseModel):
    document_id: str
    case_id: str
    tenant_id: str
    uploaded_by: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    review_status: str
    version: int
    scanner_status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CaseDocumentReviewRequest(BaseModel):
    review_status: Literal["ACCEPTED", "REJECTED"]
    rejection_reason: str | None = None


class CaseAssignRequest(BaseModel):
    consultant_id: str


class CaseActivityResponse(BaseModel):
    audit_id: str
    action: str
    entity_type: str
    entity_id: str | None = None
    before_json: dict | None = None
    after_json: dict | None = None
    user_id: str | None = None
    user_name: str | None = None
    created_at: datetime | None = None


class CaseDashboardResponse(BaseModel):
    case_count: int
    by_status: dict[str, int]
    consultant_workload: list[dict]


class FormTemplateCreate(BaseModel):
    form_key: str = Field(..., min_length=1, max_length=64)
    version: int = Field(default=1, ge=1)
    title: str = Field(..., min_length=1, max_length=200)
    program_id: str | None = None
    schema_json: dict[str, Any] = Field(default_factory=dict)


class FormTemplateResponse(BaseModel):
    template_id: str
    tenant_id: str | None = None
    form_key: str
    version: int
    title: str
    program_id: str | None = None
    schema_json: dict[str, Any]
    status: str
    created_by: str
    created_at: datetime | None = None


class FormAnswerCreate(BaseModel):
    answers_json: dict[str, Any] = Field(default_factory=dict)


class FormAnswerResponse(BaseModel):
    answer_id: str
    case_id: str
    tenant_id: str
    template_id: str
    answers_json: dict[str, Any]
    status: str
    submitted_at: datetime | None = None
    applicant_confirmed_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None
    missing_fields: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None

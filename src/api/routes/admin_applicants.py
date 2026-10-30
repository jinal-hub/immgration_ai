"""Admin-only, read-only applicant visibility.

Deliberately separate from applicants.py/assessments.py: those routes stay
scoped to the authenticated end-user's OWN applicant records (unchanged by
this file). This router gives an admin a cross-user view instead - it never
touches, loosens, or reuses the existing ownership check.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from api.helpers import parse_uuid_or_404
from api.routes.auth import require_admin
from db.connection import get_db_engine, get_session
from db.models import (
    ApiUser,
    ApplicantProfile,
    ApplicationCase,
    Assessment,
    ConsultancyTenant,
    TenantMembership,
)
from logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


class AdminApplicantTenantMembership(BaseModel):
    tenant_id: str
    tenant_name: str
    role: str
    status: str


class AdminApplicantCaseSummary(BaseModel):
    case_id: str
    tenant_id: str
    tenant_name: str
    program_id: str
    status: str
    assigned_consultant_id: str | None = None
    title: str | None = None
    created_at: str | None = None


class AdminApplicantResponse(BaseModel):
    applicant_id: str
    user_id: str
    owner_email: str
    profile_exists: bool = True
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: str | None = None
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
    created_at: str | None = None
    tenant_memberships: list[AdminApplicantTenantMembership] = Field(
        default_factory=list,
    )


class AdminApplicantListResponse(BaseModel):
    applicants: list[AdminApplicantResponse]
    total: int
    limit: int
    offset: int


class AdminApplicantAssessmentSummary(BaseModel):
    assessment_id: str
    program_id: str
    business_status: str
    overall_status: str
    ruleset_hash: str | None = None
    evaluated_at: str | None = None


class AdminApplicantDetailResponse(AdminApplicantResponse):
    assessments: list[AdminApplicantAssessmentSummary] = Field(default_factory=list)
    cases: list[AdminApplicantCaseSummary] = Field(default_factory=list)


def _tenant_memberships_by_user(
    session,
    user_ids: list[str],
) -> dict[str, list[AdminApplicantTenantMembership]]:
    if not user_ids:
        return {}
    rows = session.execute(
        select(TenantMembership, ConsultancyTenant.name)
        .join(
            ConsultancyTenant,
            ConsultancyTenant.tenant_id == TenantMembership.tenant_id,
        )
        .where(TenantMembership.user_id.in_(user_ids))
        .order_by(ConsultancyTenant.name)
    ).all()
    memberships: dict[str, list[AdminApplicantTenantMembership]] = {}
    for membership, tenant_name in rows:
        memberships.setdefault(membership.user_id, []).append(
            AdminApplicantTenantMembership(
                tenant_id=membership.tenant_id,
                tenant_name=tenant_name,
                role=membership.role,
                status=membership.status,
            )
        )
    return memberships


def _to_response(applicant: ApplicantProfile, owner_email: str) -> AdminApplicantResponse:
    return AdminApplicantResponse(
        applicant_id=applicant.applicant_id,
        user_id=applicant.user_id,
        owner_email=owner_email,
        profile_exists=True,
        first_name=applicant.first_name,
        last_name=applicant.last_name,
        date_of_birth=(
            applicant.date_of_birth.isoformat() if applicant.date_of_birth else None
        ),
        passport_nationality=applicant.passport_nationality,
        current_visa_subclass=applicant.current_visa_subclass,
        age=applicant.age,
        english_level=applicant.english_level,
        points=applicant.points,
        occupation=applicant.occupation,
        skills_assessment_status=applicant.skills_assessment_status,
        health_status=applicant.health_status,
        character_status=applicant.character_status,
        has_regional_nomination=applicant.has_regional_nomination,
        visa_subclass=applicant.visa_subclass,
        visa_history=applicant.visa_history,
        employer_sponsored=applicant.employer_sponsored,
        work_experience_years=applicant.work_experience_years,
        in_australia_at_application=applicant.in_australia_at_application,
        has_health_insurance=applicant.has_health_insurance,
        genuine_temporary_entrant=applicant.genuine_temporary_entrant,
        created_at=applicant.created_at.isoformat() if applicant.created_at else None,
    )


def _account_response(user: ApiUser) -> AdminApplicantResponse:
    name_parts = (user.full_name or "").strip().split(maxsplit=1)
    return AdminApplicantResponse(
        applicant_id=user.user_id,
        user_id=user.user_id,
        owner_email=user.email,
        profile_exists=False,
        first_name=name_parts[0] if name_parts else None,
        last_name=name_parts[1] if len(name_parts) > 1 else None,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


@router.get("", response_model=AdminApplicantListResponse)
def list_applicants(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(require_admin),
):
    """All applicant accounts and profiles, including accounts not yet filled in."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        account_filter = ApiUser.role == "applicant"
        total = session.execute(
            select(func.count())
            .select_from(ApiUser)
            .outerjoin(ApplicantProfile, ApiUser.user_id == ApplicantProfile.user_id)
            .where(account_filter)
        ).scalar() or 0
        rows = session.execute(
            select(ApiUser, ApplicantProfile)
            .outerjoin(ApplicantProfile, ApiUser.user_id == ApplicantProfile.user_id)
            .where(account_filter)
            .order_by(func.coalesce(ApplicantProfile.created_at, ApiUser.created_at).desc())
            .limit(limit)
            .offset(offset)
        ).all()
        memberships_by_user = _tenant_memberships_by_user(
            session,
            [user.user_id for user, _ in rows],
        )
        applicants = []
        for user, profile in rows:
            response = (
                _to_response(profile, user.email)
                if profile else _account_response(user)
            )
            applicants.append(response.model_copy(update={
                "tenant_memberships": memberships_by_user.get(user.user_id, []),
            }))
        return AdminApplicantListResponse(
            applicants=applicants,
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/{applicant_id}", response_model=AdminApplicantDetailResponse)
def get_applicant(
    applicant_id: str,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """One applicant's profile plus their assessment history (result, ruleset
    hash, date) - no owner check, this is the admin cross-user view."""
    parse_uuid_or_404(applicant_id)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        row = session.execute(
            select(ApplicantProfile, ApiUser.email)
            .join(ApiUser, ApiUser.user_id == ApplicantProfile.user_id)
            .where(ApplicantProfile.applicant_id == applicant_id)
        ).first()
        if row:
            applicant, owner_email = row
            assessments = session.execute(
                select(Assessment)
                .where(Assessment.applicant_id == applicant_id)
                .order_by(Assessment.evaluated_at.desc())
            ).scalars().all()
            base = _to_response(applicant, owner_email)
            cases = session.execute(
                select(ApplicationCase, ConsultancyTenant.name)
                .join(
                    ConsultancyTenant,
                    ConsultancyTenant.tenant_id == ApplicationCase.tenant_id,
                )
                .where(ApplicationCase.applicant_id == applicant_id)
                .order_by(ApplicationCase.created_at.desc())
            ).all()
            case_summaries = [
                AdminApplicantCaseSummary(
                    case_id=case.case_id,
                    tenant_id=case.tenant_id,
                    tenant_name=tenant_name,
                    program_id=case.program_id,
                    status=case.status,
                    assigned_consultant_id=case.assigned_consultant_id,
                    title=case.title,
                    created_at=case.created_at.isoformat() if case.created_at else None,
                )
                for case, tenant_name in cases
            ]
        else:
            user = session.execute(
                select(ApiUser).where(
                    ApiUser.user_id == applicant_id,
                    ApiUser.role == "applicant",
                )
            ).scalars().first()
            if not user:
                raise HTTPException(status_code=404, detail="Applicant not found")
            assessments = []
            base = _account_response(user)
            case_summaries = []

        return AdminApplicantDetailResponse(
            **base.model_copy(update={
                "tenant_memberships": _tenant_memberships_by_user(
                    session, [base.user_id],
                ).get(base.user_id, []),
            }).model_dump(),
            assessments=[
                AdminApplicantAssessmentSummary(
                    assessment_id=a.assessment_id,
                    program_id=a.program_id,
                    business_status=a.business_status,
                    overall_status=a.overall_status,
                    ruleset_hash=a.ruleset_hash,
                    evaluated_at=a.evaluated_at.isoformat() if a.evaluated_at else None,
                )
                for a in assessments
            ],
            cases=case_summaries,
        )

"""Applicant routes — CRUD for applicant profiles."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from api.helpers import parse_uuid_or_404
from api.routes.auth import get_current_user_id
from api.schemas.models import (
    ApplicantCreate,
    ApplicantProgramResponse,
    ApplicantResponse,
    ApplicantSourceResponse,
    ApplicantUpdate,
    ApplicationCaseResponse,
    VisaSelectionCreate,
    VisaSelectionResponse,
)
from db.connection import get_db_engine, get_session
from db.models import (
    ApplicantProfile,
    ApplicationCase,
    ConsultancyTenant,
    GovernmentSource,
    ImmigrationProgram,
    Rule,
    TenantMembership,
    VisaSelection,
)
from logging_config import get_logger
from services.applicant_service import ApplicantService
from services.tenant_service import case_payload

router = APIRouter()
logger = get_logger(__name__)


def _visa_selection_payload(selection: VisaSelection) -> dict[str, str | datetime]:
    return {
        "selection_id": selection.selection_id,
        "applicant_id": selection.applicant_id,
        "program_id": selection.program_id,
        "selected_at": selection.selected_at,
    }


@router.get("/programs", response_model=list[ApplicantProgramResponse])
def list_applicant_programs(
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """List active and reference-only visa programs with current rule availability."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        stmt = (
            select(
                ImmigrationProgram.program_id,
                ImmigrationProgram.country_id,
                ImmigrationProgram.visa_category,
                ImmigrationProgram.subclass_code,
                ImmigrationProgram.name,
                ImmigrationProgram.status,
                func.count(Rule.rule_id).label("active_rule_count"),
            )
            .outerjoin(
                Rule,
                (Rule.program_id == ImmigrationProgram.program_id)
                & (Rule.status == "ACTIVE"),
            )
            .where(ImmigrationProgram.status.in_(("ACTIVE", "REFERENCE_ONLY")))
            .group_by(
                ImmigrationProgram.program_id,
                ImmigrationProgram.country_id,
                ImmigrationProgram.visa_category,
                ImmigrationProgram.subclass_code,
                ImmigrationProgram.name,
                ImmigrationProgram.status,
            )
            .order_by(
                ImmigrationProgram.country_id,
                ImmigrationProgram.visa_category,
                ImmigrationProgram.subclass_code,
                ImmigrationProgram.name,
            )
        )
        programs = list(session.execute(stmt))
        program_ids = [row.program_id for row in programs]
        sources_by_program: dict[str, list[ApplicantSourceResponse]] = {
            program_id: [] for program_id in program_ids
        }
        if program_ids:
            sources = session.execute(
                select(GovernmentSource)
                .where(GovernmentSource.program_id.in_(program_ids))
                .order_by(GovernmentSource.program_id, GovernmentSource.authority)
            ).scalars()
            for source in sources:
                sources_by_program[source.program_id].append(
                    ApplicantSourceResponse(
                        source_id=source.source_id,
                        authority=source.authority,
                        url=source.url,
                        status=source.status,
                        fetch_status=source.fetch_status,
                        http_status=source.http_status,
                        last_checked_at=source.last_checked_at,
                        last_successful_fetch_at=source.last_successful_fetch_at,
                    )
                )
        return [
            {
                **row._mapping,
                "active_rule_count": int(row.active_rule_count),
                "sources": sources_by_program[row.program_id],
            }
            for row in programs
        ]


@router.get(
    "/{applicant_id}/visa-selections",
    response_model=list[VisaSelectionResponse],
)
def list_visa_selections(
    applicant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(applicant_id)
    with get_session(get_db_engine(request)) as session:
        profile = session.get(ApplicantProfile, applicant_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Applicant profile not found")
        if profile.user_id != user_id:
            raise HTTPException(status_code=403, detail="Applicant access denied")
        selections = session.execute(
            select(VisaSelection)
            .where(VisaSelection.applicant_id == applicant_id)
            .order_by(VisaSelection.selected_at.desc())
        ).scalars().all()
        return [_visa_selection_payload(selection) for selection in selections]


@router.post(
    "/{applicant_id}/visa-selections",
    response_model=VisaSelectionResponse,
    status_code=201,
)
def save_visa_selection(
    applicant_id: str,
    body: VisaSelectionCreate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(applicant_id)
    with get_session(get_db_engine(request)) as session:
        profile = session.get(ApplicantProfile, applicant_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Applicant profile not found")
        if profile.user_id != user_id:
            raise HTTPException(status_code=403, detail="Applicant access denied")
        program = session.get(ImmigrationProgram, body.program_id)
        if program is None or program.status not in {"ACTIVE", "REFERENCE_ONLY"}:
            raise HTTPException(status_code=404, detail="Visa program not found")
        selection = session.execute(
            select(VisaSelection).where(
                VisaSelection.applicant_id == applicant_id,
                VisaSelection.program_id == body.program_id,
            )
        ).scalar_one_or_none()
        if selection is None:
            selection = VisaSelection(
                applicant_id=applicant_id,
                program_id=body.program_id,
            )
            session.add(selection)
            session.flush()
        else:
            selection.selected_at = datetime.now(timezone.utc)
            session.flush()
        return _visa_selection_payload(selection)


@router.get(
    "/{applicant_id}/cases",
    response_model=list[ApplicationCaseResponse],
)
def list_applicant_cases(
    applicant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(applicant_id)
    with get_session(get_db_engine(request)) as session:
        profile = session.get(ApplicantProfile, applicant_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Applicant profile not found")
        if profile.user_id != user_id:
            raise HTTPException(status_code=403, detail="Applicant access denied")
        rows = session.execute(
            select(ApplicationCase, ConsultancyTenant.name)
            .join(
                TenantMembership,
                (TenantMembership.tenant_id == ApplicationCase.tenant_id)
                & (TenantMembership.user_id == user_id),
            )
            .join(
                ConsultancyTenant,
                ConsultancyTenant.tenant_id == ApplicationCase.tenant_id,
            )
            .where(
                ApplicationCase.applicant_id == applicant_id,
                TenantMembership.role == "APPLICANT",
                TenantMembership.status == "ACTIVE",
                ConsultancyTenant.status == "ACTIVE",
            )
            .order_by(ApplicationCase.created_at.desc())
        ).all()
        return [
            {**case_payload(case), "tenant_name": tenant_name}
            for case, tenant_name in rows
        ]


@router.post("", response_model=ApplicantResponse, status_code=201)
def create_applicant(
    body: ApplicantCreate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    engine = get_db_engine(request)
    with get_session(engine) as session:
        svc = ApplicantService(session)
        return svc.create(user_id, **body.model_dump(exclude_unset=True))


@router.get("", response_model=list[ApplicantResponse])
def list_applicants(request: Request, user_id: str = Depends(get_current_user_id)):
    engine = get_db_engine(request)
    with get_session(engine) as session:
        svc = ApplicantService(session)
        return svc.list_for_user(user_id)


@router.get("/{applicant_id}", response_model=ApplicantResponse)
def get_applicant(
    applicant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(applicant_id)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        svc = ApplicantService(session)
        result = svc.get(applicant_id)
        if not result:
            raise HTTPException(status_code=404, detail="Applicant not found")
        if result["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Applicant access denied")
        return result


@router.patch("/{applicant_id}", response_model=ApplicantResponse)
def update_applicant(
    applicant_id: str,
    body: ApplicantUpdate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(applicant_id)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        svc = ApplicantService(session)
        existing = svc.get(applicant_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Applicant not found")
        if existing["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Applicant access denied")
        result = svc.update(applicant_id, **body.model_dump(exclude_unset=True))
        if not result:
            raise HTTPException(status_code=404, detail="Applicant not found")
        return result

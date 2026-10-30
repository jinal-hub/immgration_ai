"""Assessment routes — run + retrieve eligibility assessments."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from api.helpers import parse_uuid_or_404
from api.routes.auth import get_current_user_id
from api.schemas.models import (
    AssessRequest,
    AssessmentResponse,
    AssessmentListResponse,
    AnswerRequest,
    EvaluateRequest,
    EvaluateResponse,
    GapAnswerResponse,
)
from db.connection import get_session, get_db_engine
from db.repositories import AssessmentRepo, GapRepo
from logging_config import get_logger
from services.applicant_service import ApplicantService
from services.assessment_service import AssessmentService
from assessment.applicant import APPLICANT_FACT_FIELDS
from rules.rule_engine import resolve_field_name

router = APIRouter()
logger = get_logger(__name__)


def _require_applicant_owner(session, applicant_id: str, user_id: str) -> None:
    applicant = ApplicantService(session).get(applicant_id)
    if not applicant:
        raise HTTPException(status_code=404, detail="Applicant not found")
    if applicant["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Applicant access denied")


def _validate_gap_answer(field: str, value):
    """Validate answers against modeled applicant facts, never client field names."""
    canonical_field = resolve_field_name(field)
    if canonical_field not in APPLICANT_FACT_FIELDS:
        raise HTTPException(
            status_code=422,
            detail="This question cannot be answered through the applicant profile.",
        )
    if value is None:
        raise HTTPException(status_code=422, detail="Answer cannot be empty.")

    boolean_fields = {
        "has_regional_nomination", "employer_sponsored",
        "in_australia_at_application", "has_health_insurance",
        "genuine_temporary_entrant",
    }
    integer_fields = {"age", "points", "work_experience_years"}
    allowed_values = {
        "english_level": {"none", "vocational", "competent", "proficient", "superior"},
        "skills_assessment_status": {
            "NOT_APPLICABLE", "PENDING", "VALID", "INVALID", "EXEMPT",
        },
        "health_status": {"UNKNOWN", "PASSED", "FAILED"},
        "character_status": {"UNKNOWN", "PASSED", "FAILED"},
        "visa_history": {
            "NONE", "PRIOR_GRANTED", "PRIOR_REFUSED",
            "PRIOR_CANCELLED", "PRIOR_REMOVED",
        },
    }

    if canonical_field in boolean_fields:
        if not isinstance(value, bool):
            raise HTTPException(status_code=422, detail="Answer must be true or false.")
        return canonical_field, value
    if canonical_field in integer_fields:
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(status_code=422, detail="Answer must be a whole number.")
        limits = {
            "age": (0, 120),
            "points": (0, 200),
            "work_experience_years": (0, 80),
        }
        minimum, maximum = limits[canonical_field]
        if not minimum <= value <= maximum:
            raise HTTPException(
                status_code=422,
                detail=f"Answer must be between {minimum} and {maximum}.",
            )
        return canonical_field, value
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise HTTPException(
            status_code=422,
            detail="Answer must be a non-empty string of at most 256 characters.",
        )
    normalized = value.strip()
    options = allowed_values.get(canonical_field)
    if options and normalized not in options:
        raise HTTPException(status_code=422, detail="Answer is not an allowed option.")
    return canonical_field, normalized


@router.post("", response_model=AssessmentResponse, status_code=201)
def create_assessment(
    body: AssessRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """Run eligibility assessment and persist result.

    Full pipeline: Load ACTIVE rules → build expression tree → Phase 3C
    assessment → gap engine → persist assessment + rule results + gaps.
    """
    engine = get_db_engine(request)
    with get_session(engine) as session:
        _require_applicant_owner(session, body.applicant_id, user_id)
        svc = AssessmentService(session)
        try:
            result = svc.run_assessment(
                applicant_id=body.applicant_id,
                program_id=body.program_id,
                request_id=request.headers.get("X-Request-ID"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return result


@router.get("", response_model=AssessmentListResponse)
def list_assessments(
    request: Request,
    applicant_id: str,
    limit: int = 20,
    user_id: str = Depends(get_current_user_id),
):
    engine = get_db_engine(request)
    with get_session(engine) as session:
        _require_applicant_owner(session, applicant_id, user_id)
        svc = AssessmentService(session)
        results = svc.list_for_applicant(applicant_id, limit=limit)
        return AssessmentListResponse(assessments=results, total=len(results))


@router.post(
    "/{assessment_id}/gaps/{gap_id}/answer",
    response_model=GapAnswerResponse,
)
def answer_assessment_gap(
    assessment_id: str,
    gap_id: str,
    body: AnswerRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """Persist an answer on its assessment and update the owned applicant profile."""
    parse_uuid_or_404(assessment_id)
    parse_uuid_or_404(gap_id)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        assessment = AssessmentRepo(session).get(assessment_id)
        if not assessment:
            raise HTTPException(status_code=404, detail="Assessment not found")
        _require_applicant_owner(session, assessment.applicant_id, user_id)

        gap = GapRepo(session).get(gap_id)
        if not gap or gap.assessment_id != assessment_id:
            raise HTTPException(status_code=404, detail="Question not found")
        canonical_field, answer = _validate_gap_answer(gap.field, body.value)

        updated_profile = ApplicantService(session).update(
            assessment.applicant_id,
            **{canonical_field: answer},
        )
        if updated_profile is None:
            raise HTTPException(status_code=404, detail="Applicant not found")

        gap.answer = json.dumps(answer, ensure_ascii=False)
        gap.updated_at = datetime.now(timezone.utc)
        session.flush()
        return GapAnswerResponse(
            gap_id=gap.gap_id,
            field=gap.field,
            answer=answer,
            updated_at=gap.updated_at,
        )


@router.get("/{assessment_id}", response_model=AssessmentResponse)
def get_assessment(
    assessment_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(assessment_id)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        svc = AssessmentService(session)
        result = svc.get(assessment_id)
        if not result:
            raise HTTPException(status_code=404, detail="Assessment not found")
        _require_applicant_owner(session, result["applicant_id"], user_id)
        return result


@router.post("/evaluate", response_model=EvaluateResponse)
def evaluate_inline(
    body: EvaluateRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    """In-memory eligibility check (no persistence).

    Runs the full pipeline — load ACTIVE rules, build expression tree,
    Phase 3C assessment — but does not save results to DB.
    """
    engine = get_db_engine(request)
    with get_session(engine) as session:
        _require_applicant_owner(session, body.applicant_id, user_id)
        svc = AssessmentService(session)
        try:
            return svc.evaluate_inline(
                applicant_id=body.applicant_id,
                program_id=body.program_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

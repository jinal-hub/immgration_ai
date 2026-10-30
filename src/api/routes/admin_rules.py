"""Admin rule review routes â€” approve, reject, validate, activate rules.

All mutations require admin role. The human approval flow:
    DRAFT â†’ VALIDATED (automated or admin) â†’ APPROVED (admin) â†’ ACTIVE (admin)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.routes.auth import require_admin
from assessment.applicant import APPLICANT_FACT_FIELDS, ApplicantProfile, VisaSelection
from db.connection import get_session, get_db_engine
from db.models import DocumentChunk, Rule as RuleORM, RuleVersion
from extraction.citations import citation_for_chunk
from logging_config import get_logger
from rules.rule_checker import check_rules
from rules.rule_engine import is_field_evaluable
from rules.rule_repository import EnhancedRuleRepo, orm_to_dataclass
from rules.rule_activation import can_transition

logger = get_logger(__name__)
router = APIRouter()


# The exact word an admin has to type to activate a rule that no applicant
# field can ever answer. Anything else (including a boolean flag) is
# refused, so the confirmation cannot be fired by accident or by a script
# that does not understand what it is confirming.
NOT_EVALUABLE_CONFIRMATION = "ACTIVATE"
SYNTHETIC_GOLD_FILE = (
    Path(__file__).resolve().parents[3] / "data" / "gold" / "requirements_gold.json"
)
CURATED_GOLD_SOURCES = {
    ("SRC-AU-189-MAIN", "DOC-GOLD-189-REAL"),
    ("SRC-AU-190-MAIN", "DOC-GOLD-190-REAL"),
}
REVIEWABLE_GOLD_STATUSES = {"DRAFT", "VALIDATED", "APPROVED"}


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------
class RuleActionRequest(BaseModel):
    notes: str | None = None
    # Typed confirmation, required only when the rule's field is not
    # something an applicant can supply. See NOT_EVALUABLE_CONFIRMATION.
    confirm_not_evaluable: str | None = None


class GoldRuleReviewRequest(BaseModel):
    decision: Literal["ACTIVATE", "REJECT"]
    confirm_not_evaluable: str | None = None


class RuleResponse(BaseModel):
    rule_id: str
    requirement_id: str | None = None
    program_id: str | None = None
    category: str | None = None
    field: str | None = None
    operator: str | None = None
    value: Any = None
    unit: str | None = None
    mandatory: bool = False
    critical: bool = False
    message: str | None = None
    status: str
    current_version: int = 1
    source_id: str | None = None
    document_id: str | None = None
    document_version: int | None = None
    chunk_id: int | None = None
    requirement_version_id: int | None = None
    evidence_text: str | None = None
    # Where in the source the rule's evidence came from: "p. 12" for a PDF
    # page, the heading path for an HTML section, None when the rule has no
    # chunk anchor at all.
    citation: str | None = None
    # Whether an applicant field can ever answer this rule. False means the
    # rule is real source material that no applicant profile can satisfy -
    # it will always evaluate as UNKNOWN.
    evaluable: bool = True
    created_at: str | None = None
    condition: Any = None
    exception: Any = None


class SyntheticGoldExamplesResponse(BaseModel):
    dataset_file: str
    program_id: str | None = None
    synthetic: bool
    warning: str
    items: list[dict[str, Any]]


class ReviewQueueResponse(BaseModel):
    drafts: list[dict]
    validated: list[dict]
    approved_pending: list[dict]
    active: list[dict]
    counts: dict


class BatchActionRequest(BaseModel):
    rule_ids: list[str]
    notes: str | None = None
    # Typed confirmation, required when ANY rule in the batch is not
    # evaluable. See NOT_EVALUABLE_CONFIRMATION.
    confirm_not_evaluable: str | None = None


class RuleCheckRequest(BaseModel):
    """A dry run of the rules against a hypothetical applicant."""
    program_id: str
    # The applicant's facts, using the intake field names.
    profile: dict[str, Any] = Field(default_factory=dict)
    # Which rules to check. Defaults to the program's ACTIVE rules; an admin
    # can also check a specific rule (any status) before activating it.
    rule_ids: list[str] | None = None


class RuleCheckResponse(BaseModel):
    program_id: str
    overall_status: str
    business_status: str
    total_rules: int
    passed: int
    failed: int
    unknown: int
    not_applicable: int
    ruleset_hash: str
    evaluated_at: str
    results: list[dict]
    gaps: list[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _actor_from(current_user: dict) -> str:
    """The audit-trail actor identity for a rule action: ALWAYS the
    authenticated admin, never the request body's own `actor` field (which
    is accepted for backward compatibility but ignored - a client could
    previously type any name there and have it recorded as who acted,
    which defeats the point of an audit trail)."""
    return current_user.get("email") or current_user.get("user_id") or "unknown-admin"


def _citation_for(session: Session, rule: RuleORM) -> str | None:
    """Resolve a rule's evidence anchor from the chunk it points at."""
    if rule.chunk_id is None:
        return None
    chunk = session.get(DocumentChunk, rule.chunk_id)
    return citation_for_chunk(chunk)


def _orm_to_response(obj: RuleORM, citation: str | None = None) -> RuleResponse:
    return RuleResponse(
        rule_id=obj.rule_id,
        requirement_id=obj.requirement_id,
        program_id=obj.program_id,
        category=obj.category,
        field=obj.field,
        operator=obj.operator,
        value=json.loads(obj.value_json) if obj.value_json else None,
        unit=obj.unit,
        mandatory=obj.mandatory or False,
        critical=obj.critical or False,
        message=obj.message,
        status=obj.status,
        current_version=obj.current_version or 1,
        source_id=obj.source_id,
        document_id=obj.document_id,
        document_version=obj.document_version,
        chunk_id=obj.chunk_id,
        requirement_version_id=obj.requirement_version_id,
        evidence_text=obj.evidence_text,
        citation=citation,
        evaluable=is_field_evaluable(obj.field),
        created_at=obj.created_at.isoformat() if obj.created_at else None,
        condition=json.loads(obj.condition_json) if obj.condition_json else None,
        exception=json.loads(obj.exception_json) if obj.exception_json else None,
    )


def _require_evaluable_confirmation(rule_field: str | None, confirmation: str | None) -> None:
    """Refuse to activate a rule no applicant field can answer, unless the
    admin explicitly typed the confirmation word.

    A rule like "you must hold a valid invitation at the time of
    application" is genuine source material, but nothing in the applicant
    profile can answer it, so it will evaluate as UNKNOWN for every applicant
    forever. That is a legitimate thing to activate - the requirement may
    still be worth recording - but it should never be a click that happens
    by accident, so it takes a typed confirmation.
    """
    if is_field_evaluable(rule_field):
        return
    if confirmation != NOT_EVALUABLE_CONFIRMATION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Rule field {rule_field!r} is not evaluable - no applicant field "
                f"supplies it, so this rule will always evaluate as UNKNOWN. "
                f"To activate it anyway, repeat the request with "
                f"confirm_not_evaluable=\"{NOT_EVALUABLE_CONFIRMATION}\"."
            ),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/queue", response_model=ReviewQueueResponse)
def review_queue(
    request: Request,
    program_id: str | None = None,
    current_user: dict = Depends(require_admin),
):
    """Get the review queue â€” all rules grouped by status."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        from orchestration.government_sync import get_review_queue
        return get_review_queue(session, program_id)


@router.get("", response_model=list[RuleResponse])
def list_rules(
    request: Request,
    status: str | None = None,
    program_id: str | None = None,
    current_user: dict = Depends(require_admin),
):
    """List rules, optionally filtered by status and program."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        if status:
            objs = repo.list_by_status(status, program_id)
        else:
            from db.models import Rule as RuleORM
            from sqlalchemy import select
            stmt = select(RuleORM)
            if program_id:
                stmt = stmt.where(RuleORM.program_id == program_id)
            objs = list(session.execute(stmt).scalars())
        return [_orm_to_response(o, _citation_for(session, o)) for o in objs]


# NOTE: /batch/* routes MUST be registered before any /{rule_id}/* route.
# Starlette matches in registration order, so a late /batch/validate would be
# swallowed by POST /{rule_id}/validate with rule_id="batch". Do not reorder.

@router.get("/gold-examples", response_model=SyntheticGoldExamplesResponse)
def synthetic_gold_examples(
    current_user: dict = Depends(require_admin),
):
    """Return synthetic examples for display only; they are never executable rules."""
    data = json.loads(SYNTHETIC_GOLD_FILE.read_text(encoding="utf-8"))
    return SyntheticGoldExamplesResponse(
        dataset_file=SYNTHETIC_GOLD_FILE.name,
        program_id=data.get("program_id"),
        synthetic=True,
        warning=(
            "Synthetic test data, not verbatim government evidence. "
            "Read-only; these examples cannot be activated."
        ),
        items=data.get("items", []),
    )


@router.post("/batch/validate", response_model=list[RuleResponse])
def batch_validate(
    body: BatchActionRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Batch DRAFT â†’ VALIDATED."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        results = []
        for rule_id in body.rule_ids:
            try:
                obj = repo.validate_rule(rule_id, actor=actor)
                repo.save_version_snapshot(rule_id, "VALIDATED", actor)
                results.append(_orm_to_response(obj, _citation_for(session, obj)))
            except ValueError as e:
                logger.warning("Batch validate failed for rule",
                               extra={"rule_id": rule_id, "error": str(e)})
        return results


@router.post("/batch/approve", response_model=list[RuleResponse])
def batch_approve(
    body: BatchActionRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Batch VALIDATED â†’ APPROVED."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        results = []
        for rule_id in body.rule_ids:
            try:
                obj = repo.approve_rule(rule_id, actor=actor)
                repo.save_version_snapshot(rule_id, "APPROVED", actor)
                results.append(_orm_to_response(obj, _citation_for(session, obj)))
            except ValueError as e:
                logger.warning("Batch approve failed for rule",
                               extra={"rule_id": rule_id, "error": str(e)})
        return results


@router.post("/batch/activate", response_model=list[RuleResponse])
def batch_activate(
    body: BatchActionRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Batch APPROVED → ACTIVE (each transactionally supersedes old version)."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)

        # The typed confirmation covers the whole batch: if ANY rule in it
        # is not evaluable, the admin has to confirm - a batch is exactly
        # where a guard that can be skipped one rule at a time would leak.
        for rule_id in body.rule_ids:
            obj = repo.get(rule_id)
            if obj is None:
                continue
            _require_evaluable_confirmation(obj.field, body.confirm_not_evaluable)

        results = []
        for rule_id in body.rule_ids:
            try:
                obj = repo.activate_rule(rule_id, actor=actor)
                results.append(_orm_to_response(obj, _citation_for(session, obj)))
            except ValueError as e:
                logger.warning("Batch activate failed for rule",
                               extra={"rule_id": rule_id, "error": str(e)})
        return results


@router.post("/evaluate", response_model=RuleCheckResponse)
def evaluate_rules(
    body: RuleCheckRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Dry-run the rules against a hypothetical applicant.

    Deterministic and side-effect free: no LLM call, no network request, no
    database write. The admin gets per-rule PASS/FAIL/UNKNOWN with a plain
    explanation, the overall verdict, the gaps to ask about, and a hash of
    exactly which rule versions produced the report.
    """
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)

        if body.rule_ids:
            rules = []
            for rule_id in body.rule_ids:
                obj = repo.get(rule_id)
                if obj is None:
                    raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")
                rules.append(orm_to_dataclass(obj))
        else:
            rules = [
                orm_to_dataclass(obj)
                for obj in repo.list_active(body.program_id)
            ]

        if not rules:
            raise HTTPException(
                status_code=404,
                detail=f"No rules to check for program {body.program_id}",
            )

        # Only facts the applicant profile actually carries are used; an
        # unknown key in the request is ignored rather than silently
        # becoming a rule input.
        facts = {
            name: body.profile[name]
            for name in APPLICANT_FACT_FIELDS
            if body.profile.get(name) is not None
        }
        applicant = ApplicantProfile(
            visa=VisaSelection(program_id=body.program_id),
            **facts,
        )

        report = check_rules(rules, applicant.to_rule_data(), body.program_id)
        return report.to_dict()


@router.get("/{rule_id}", response_model=RuleResponse)
def get_rule(
    rule_id: str,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Get a single rule by ID."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        obj = repo.get(rule_id)
        if not obj:
            raise HTTPException(status_code=404, detail="Rule not found")
        return _orm_to_response(obj, _citation_for(session, obj))


@router.post("/{rule_id}/validate", response_model=RuleResponse)
def validate_rule(
    rule_id: str,
    body: RuleActionRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """DRAFT â†’ VALIDATED (admin confirms automated validation passed)."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        try:
            obj = repo.validate_rule(rule_id, actor=actor)
            repo.save_version_snapshot(rule_id, "VALIDATED", actor)
            return _orm_to_response(obj, _citation_for(session, obj))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.post("/{rule_id}/approve", response_model=RuleResponse)
def approve_rule(
    rule_id: str,
    body: RuleActionRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """VALIDATED â†’ APPROVED (human review passes)."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        try:
            obj = repo.approve_rule(rule_id, actor=actor)
            repo.save_version_snapshot(rule_id, "APPROVED", actor)
            return _orm_to_response(obj, _citation_for(session, obj))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.post("/{rule_id}/reject", response_model=RuleResponse)
def reject_rule(
    rule_id: str,
    body: RuleActionRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Any pre-active status â†’ REJECTED."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        try:
            obj = repo.reject_rule(rule_id, actor=actor)
            repo.save_version_snapshot(rule_id, "REJECTED", actor)
            return _orm_to_response(obj, _citation_for(session, obj))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.post("/{rule_id}/activate", response_model=RuleResponse)
def activate_rule(
    rule_id: str,
    body: RuleActionRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """APPROVED → ACTIVE. Transactionally supersedes any existing active version."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        existing = repo.get(rule_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        _require_evaluable_confirmation(existing.field, body.confirm_not_evaluable)
        try:
            obj = repo.activate_rule(rule_id, actor=actor)
            return _orm_to_response(obj, _citation_for(session, obj))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.post("/{rule_id}/review", response_model=RuleResponse)
def review_gold_rule(
    rule_id: str,
    body: GoldRuleReviewRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Apply one explicit admin decision to a curated gold draft atomically."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        obj = repo.get(rule_id)
        if obj is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        if (
            not rule_id.startswith("RULE-GOLD-")
            or (obj.source_id, obj.document_id) not in CURATED_GOLD_SOURCES
        ):
            raise HTTPException(status_code=400, detail="Only curated gold rules can use this action")
        if obj.status not in REVIEWABLE_GOLD_STATUSES:
            raise HTTPException(
                status_code=400,
                detail="Only pre-active gold rules can be reviewed",
            )

        try:
            if body.decision == "REJECT":
                obj = repo.reject_rule(rule_id, actor=actor)
                repo.save_version_snapshot(rule_id, "REJECTED", actor)
            else:
                _require_evaluable_confirmation(obj.field, body.confirm_not_evaluable)
                if obj.status == "DRAFT":
                    obj = repo.validate_rule(rule_id, actor=actor)
                    repo.save_version_snapshot(rule_id, "VALIDATED", actor)
                if obj.status == "VALIDATED":
                    obj = repo.approve_rule(rule_id, actor=actor)
                    repo.save_version_snapshot(rule_id, "APPROVED", actor)
                obj = repo.activate_rule(rule_id, actor=actor)
            return _orm_to_response(obj, _citation_for(session, obj))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.post("/{rule_id}/retire", response_model=RuleResponse)
def retire_rule(
    rule_id: str,
    body: RuleActionRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """SUPERSEDED/ACTIVE â†’ RETIRED."""
    actor = _actor_from(current_user)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = EnhancedRuleRepo(session)
        try:
            obj = repo.retire_rule(rule_id, actor=actor)
            repo.save_version_snapshot(rule_id, "RETIRED", actor)
            return _orm_to_response(obj, _citation_for(session, obj))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

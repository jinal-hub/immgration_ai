"""Admin source management routes — create, update, list, disable government sources,
plus government source AUTO-DISCOVERY (onboarding/suggestion tool).

All mutations require admin role.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from api.routes.auth import require_admin
from db.connection import get_session, get_db_engine
from db.models import DiscoveredSource, GovernmentSource
from db.repositories import DiscoveredSourceRepo, SourceRepo
from fetch.politeness import host_of
from fetch.source_validation import (
    SourceValidationError,
    validate_http_url,
    validate_source_fields,
)
from logging_config import get_logger
from orchestration.host_cooldown import blocked_until, blocked_until_map
from source_discovery import discover_sources

logger = get_logger(__name__)
router = APIRouter()


# How a page is structured, which decides how it is chunked. Separate from
# source_type (HTML/PDF), which decides how the bytes are fetched.
_CONTENT_KINDS = {"PAGE", "NEWS_LIST"}
_CONTENT_KIND_DESCRIPTION = (
    "PAGE (default) chunks prose by heading. NEWS_LIST chunks a dated "
    "'latest updates' listing into one chunk per announcement, each with its "
    "own date and link, so a change to one item is traceable to that item "
    "instead of looking like a whole-page rewrite."
)


def _normalise_content_kind(value: str) -> str:
    """Accept any casing, reject anything not a known kind with a clear 422."""
    kind = (value or "").strip().upper()
    if kind not in _CONTENT_KINDS:
        raise ValueError(
            f"content_kind must be one of {sorted(_CONTENT_KINDS)}, got {value!r}"
        )
    return kind


def _validated_or_422(fields: dict) -> dict:
    try:
        return validate_source_fields(fields)
    except SourceValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"field": exc.field, "message": str(exc)},
        ) from exc


class SourceCreateRequest(BaseModel):
    source_id: str = Field(..., description="Unique source identifier (e.g. SRC-AU-189-MAIN)")
    country_id: str = Field(..., description="Country code (e.g. AU)")
    program_id: str = Field(..., description="Program ID (e.g. SKILLED_189)")
    visa_category: str = Field(..., description="Visa category (e.g. SKILLED)")
    authority: str = Field(..., description="Issuing authority")
    url: str = Field(..., description="Source URL")
    source_type: str = Field("HTML", description="Source type (HTML/PDF/API)")
    source_subtype: str | None = Field(None, description="Source subtype")
    priority: str = Field("PRIMARY", description="PRIMARY or SECONDARY")
    monitoring_method: str = Field("WEB", description="Monitoring method")
    crawl_frequency: str = Field("DAILY", description="Crawl frequency (DAILY/WEEKLY/MONTHLY)")
    notes: str | None = None
    extract_requirements: bool = Field(
        True,
        description="Whether a sync of this source runs requirement extraction. False keeps "
                     "the source fetched/hashed/diffed/stored as evidence (document, version, "
                     "chunks) without ever calling the LLM - for a reference-only source whose "
                     "content is out of product scope.",
    )
    content_kind: str = Field("PAGE", description=_CONTENT_KIND_DESCRIPTION)

    @field_validator("content_kind")
    @classmethod
    def _validate_content_kind(cls, v: str) -> str:
        return _normalise_content_kind(v)


# Statuses an admin may explicitly set via PATCH. Other government_sources.status
# values (ACCESS_BLOCKED, NETWORK_RESTRICTED, REQUIRES_BROWSER, REQUIRES_MANUAL_DOWNLOAD,
# BROKEN) are system-detected fetch outcomes set automatically by the sync pipeline
# (src/utils/fetch_document.py) — an admin imposing one of those by hand would
# misrepresent what the fetcher actually observed, so PATCH does not allow them.
_ADMIN_SETTABLE_SOURCE_STATUSES = {"ACTIVE", "DISABLED"}


class SourceUpdateRequest(BaseModel):
    status: str | None = Field(
        None,
        description=f"One of {sorted(_ADMIN_SETTABLE_SOURCE_STATUSES)}. Other statuses are "
                     "set automatically by the fetch pipeline, not by admin PATCH.",
    )
    priority: str | None = Field(None, description="PRIMARY or SECONDARY")
    crawl_frequency: str | None = None
    notes: str | None = None
    url: str | None = None
    extract_requirements: bool | None = Field(
        None,
        description="Toggle requirement extraction for this source. See SourceCreateRequest.",
    )
    content_kind: str | None = Field(None, description=_CONTENT_KIND_DESCRIPTION)

    @field_validator("content_kind")
    @classmethod
    def _validate_content_kind(cls, v: str | None) -> str | None:
        return None if v is None else _normalise_content_kind(v)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in _ADMIN_SETTABLE_SOURCE_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_ADMIN_SETTABLE_SOURCE_STATUSES)}, got {v!r}"
            )
        return v


class SourceResponse(BaseModel):
    source_id: str
    country_id: str
    program_id: str
    visa_category: str
    authority: str
    url: str
    source_type: str
    source_subtype: str | None = None
    priority: str
    monitoring_method: str
    crawl_frequency: str
    status: str
    extract_requirements: bool
    content_kind: str
    last_checked_at: str | None = None
    last_successful_fetch_at: str | None = None
    last_hash: str | None = None
    notes: str | None = None
    fallback_source_id: str | None = None
    fetch_status: str | None = None
    http_status: int | None = None
    last_error: str | None = None
    last_error_type: str | None = None
    consecutive_failures: int | None = None
    created_at: str | None = None
    # When the source's HOST will talk to us again after refusing a fetch
    # (HTTP 403/429). Null while the host is reachable. Sources on such a host
    # are skipped by sync, and "Run now" answers 409 until this passes.
    blocked_until: str | None = None


def _source_to_response(
    obj: GovernmentSource, host_cooldown_until=None
) -> SourceResponse:
    return SourceResponse(
        source_id=obj.source_id,
        country_id=obj.country_id,
        program_id=obj.program_id,
        visa_category=obj.visa_category,
        authority=obj.authority,
        url=obj.url,
        source_type=obj.source_type,
        source_subtype=obj.source_subtype,
        priority=obj.priority,
        monitoring_method=obj.monitoring_method,
        crawl_frequency=obj.crawl_frequency,
        status=obj.status,
        extract_requirements=obj.extract_requirements,
        content_kind=obj.content_kind or "PAGE",
        last_checked_at=obj.last_checked_at.isoformat() if obj.last_checked_at else None,
        last_successful_fetch_at=obj.last_successful_fetch_at.isoformat() if obj.last_successful_fetch_at else None,
        last_hash=obj.last_hash,
        notes=obj.notes,
        fallback_source_id=obj.fallback_source_id,
        fetch_status=obj.fetch_status,
        http_status=obj.http_status,
        last_error=obj.last_error,
        last_error_type=obj.last_error_type,
        consecutive_failures=obj.consecutive_failures,
        created_at=obj.created_at.isoformat() if obj.created_at else None,
        blocked_until=(
            host_cooldown_until.isoformat() if host_cooldown_until else None
        ),
    )


def _responses_for(session: Session, sources: list[GovernmentSource]) -> list[SourceResponse]:
    """Serialize a page of sources with ONE cooldown query, not one per row."""
    untils = blocked_until_map(session, [s.url for s in sources])
    return [_source_to_response(s, untils.get(s.url)) for s in sources]


# ===========================================================================
# AUTO-DISCOVERY endpoints. IMPORTANT: these MUST be registered before any
# `/{source_id}`-style route below, otherwise `GET /discovered` would be captured
# by `GET /{source_id}` and return 404. FastAPI matches routes in registration
# order.
# ===========================================================================

class DiscoveryRequest(BaseModel):
    seed_url: str = Field(..., description="Seed URL to discover same-domain sub-pages from")
    max_depth: int = Field(2, ge=0, le=5, description="Max crawl depth (default 2)")
    suggested_category: str | None = Field(
        None, description="Optional category bias: SKILLED/WORK/STUDENT/VISITOR"
    )


class DiscoveredSourceResponse(BaseModel):
    discovered_source_id: str
    seed_url: str
    candidate_url: str
    page_title: str | None = None
    matched_keywords: list[str] = []
    confidence_score: int
    suggested_visa_category: str | None = None
    suggested_country_id: str
    status: str
    discovered_at: str | None = None
    discovery_run_id: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    rejection_reason: str | None = None


def _discovered_to_response(obj: DiscoveredSource) -> DiscoveredSourceResponse:
    return DiscoveredSourceResponse(
        discovered_source_id=obj.discovered_source_id,
        seed_url=obj.seed_url,
        candidate_url=obj.candidate_url,
        page_title=obj.page_title,
        matched_keywords=obj.matched_keywords or [],
        confidence_score=obj.confidence_score,
        suggested_visa_category=obj.suggested_visa_category,
        suggested_country_id=obj.suggested_country_id,
        status=obj.status,
        discovered_at=obj.discovered_at.isoformat() if obj.discovered_at else None,
        discovery_run_id=obj.discovery_run_id,
        reviewed_by=obj.reviewed_by,
        reviewed_at=obj.reviewed_at.isoformat() if obj.reviewed_at else None,
        rejection_reason=obj.rejection_reason,
    )


class DiscoveryApproveRequest(BaseModel):
    """Fields an admin must supply to promote a candidate into a real source. The
    candidate_url (and country) come from the candidate; URL cannot be changed here."""
    source_id: str = Field(..., description="New government_sources source_id, e.g. SRC-AU-WORK-482")
    program_id: str = Field(..., description="Program ID, e.g. WORK_482")
    visa_category: str = Field(..., description="SKILLED/WORK/STUDENT/VISITOR")
    authority: str = Field(..., description="Issuing authority")
    priority: str = Field("PRIMARY", description="PRIMARY or SECONDARY")
    source_type: str = Field("HTML", description="HTML/PDF/API")
    source_subtype: str | None = None
    monitoring_method: str = Field("WEB")
    crawl_frequency: str = Field("DAILY")
    notes: str | None = None
    content_kind: str = Field("PAGE", description=_CONTENT_KIND_DESCRIPTION)

    @field_validator("content_kind")
    @classmethod
    def _validate_content_kind(cls, v: str) -> str:
        return _normalise_content_kind(v)


class DiscoveryRejectRequest(BaseModel):
    reason: str = Field(..., description="Why the candidate was rejected")


@router.post("/discover")
def discover(
    body: DiscoveryRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Run source auto-discovery from a seed URL and return newly created candidates
    (status PENDING_REVIEW). Never activates anything — approval is a separate action."""
    engine = get_db_engine(request)
    try:
        seed_url = validate_http_url(body.seed_url)
    except SourceValidationError as exc:
        raise HTTPException(
            status_code=422, detail={"field": "seed_url", "message": str(exc)}
        ) from exc
    with get_session(engine) as session:
        _, report = discover_sources(
            seed_url=seed_url,
            max_depth=body.max_depth,
            suggested_category=body.suggested_category,
            session=session,
        )
        if report.get("robots_blocked_seed"):
            raise HTTPException(status_code=400, detail=report["errors"][0])
        repo = DiscoveredSourceRepo(session)
        created = repo.list_by_status("PENDING_REVIEW")
        return [_discovered_to_response(c) for c in created]


@router.get("/discovered", response_model=list[DiscoveredSourceResponse])
def list_discovered(
    request: Request,
    status: str | None = None,
    suggested_visa_category: str | None = None,
    current_user: dict = Depends(require_admin),
):
    """List discovered candidates, filterable by status / suggested category."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = DiscoveredSourceRepo(session)
        rows = repo.list_by_status(status, suggested_visa_category)
        return [_discovered_to_response(r) for r in rows]


@router.post("/discovered/{discovered_source_id}/approve", response_model=SourceResponse)
def approve_discovered(
    discovered_source_id: str,
    body: DiscoveryApproveRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Promote a candidate into a real government_sources row (admin action). The
    candidate_url is fixed to the discovered page; the admin fills the remaining fields."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = DiscoveredSourceRepo(session)
        cand = repo.get(discovered_source_id)
        if not cand:
            raise HTTPException(status_code=404, detail="Discovered source not found")
        if cand.status != "PENDING_REVIEW":
            raise HTTPException(status_code=409, detail=f"Candidate already {cand.status}")
        source_repo = SourceRepo(session)
        if source_repo.get(body.source_id):
            raise HTTPException(status_code=409, detail=f"Source {body.source_id} already exists")
        source_fields = _validated_or_422({
            "source_id": body.source_id,
            "country_id": cand.suggested_country_id or "AU",
            "program_id": body.program_id,
            "visa_category": body.visa_category,
            "authority": body.authority,
            "url": cand.candidate_url,
            "source_type": body.source_type,
            "source_subtype": body.source_subtype,
            "priority": body.priority,
            "monitoring_method": body.monitoring_method,
            "crawl_frequency": body.crawl_frequency,
            "status": "ACTIVE",
            "notes": body.notes,
            "content_kind": body.content_kind,
        })
        cand, source = repo.approve(
            discovered_source_id,
            reviewed_by=current_user["user_id"],
            source_fields=source_fields,
        )
        session.commit()
        return _source_to_response(source, blocked_until(session, source.url))


@router.post("/discovered/{discovered_source_id}/reject")
def reject_discovered(
    discovered_source_id: str,
    body: DiscoveryRejectRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Reject a discovered candidate with a reason."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = DiscoveredSourceRepo(session)
        cand = repo.get(discovered_source_id)
        if not cand:
            raise HTTPException(status_code=404, detail="Discovered source not found")
        if cand.status != "PENDING_REVIEW":
            raise HTTPException(status_code=409, detail=f"Candidate already {cand.status}")
        obj = repo.reject(discovered_source_id, reviewed_by=current_user["user_id"],
                          rejection_reason=body.reason)
        session.commit()
        return _discovered_to_response(obj)


# ===========================================================================
# CRUD on real government_sources.
# ===========================================================================

@router.get("", response_model=list[SourceResponse])
def list_sources(
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """List all government sources."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        from sqlalchemy import select
        stmt = select(GovernmentSource)
        sources = list(session.execute(stmt).scalars())
        return _responses_for(session, sources)


@router.get("/{source_id}", response_model=SourceResponse)
def get_source(
    source_id: str,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Get a single government source by ID."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = SourceRepo(session)
        obj = repo.get(source_id)
        if not obj:
            raise HTTPException(status_code=404, detail="Source not found")
        return _source_to_response(obj, blocked_until(session, obj.url))


@router.post("", response_model=SourceResponse, status_code=201)
def create_source(
    body: SourceCreateRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Create a new government source."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = SourceRepo(session)
        existing = repo.get(body.source_id)
        if existing:
            raise HTTPException(status_code=409, detail=f"Source {body.source_id} already exists")
        fields = _validated_or_422(body.model_dump())
        obj = repo.create(
            source_id=fields["source_id"],
            country_id=fields["country_id"],
            program_id=fields["program_id"],
            visa_category=fields["visa_category"],
            authority=fields["authority"],
            url=fields["url"],
            source_type=fields["source_type"],
            source_subtype=fields.get("source_subtype"),
            priority=fields["priority"],
            monitoring_method=fields["monitoring_method"],
            crawl_frequency=fields["crawl_frequency"],
            status="ACTIVE",
            notes=fields.get("notes"),
            extract_requirements=fields["extract_requirements"],
            content_kind=fields["content_kind"],
        )
        session.commit()
        return _source_to_response(obj, blocked_until(session, obj.url))


@router.post("/{source_id}/sync", status_code=202)
def sync_one_source(
    source_id: str,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Run exactly one government source's sync in the background (admin only).

    Reuses the same process-wide sync lock as the global "sync all sources"
    trigger (POST /api/v1/admin/sync) and the scheduled job, so this returns
    409 if ANY sync - global or another single source - is already running.

    Also returns 409 when the source's HOST is in a refusal cooldown: the host
    answered our last request with 403/429 and every source on it is paused
    until ``FETCH_COOLDOWN_HOURS`` elapse. Deliberately no override flag — a
    forced request is still a request the host asked us not to make.

    Returns immediately with {"run_id", "status": "STARTED"}; progress is
    readable via the existing GET /api/v1/admin/runs/{run_id}.
    """
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = SourceRepo(session)
        source = repo.get(source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Source not found")
        until = blocked_until(session, source.url)
        if until is not None:
            # Structured detail (unlike the plain-string "busy" 409) so the
            # client can tell a paused host from an in-flight sync without
            # guessing at prose.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "host_cooldown",
                    "message": (
                        f"Host {host_of(source.url)} refused our last request and is "
                        f"paused until {until.isoformat()}. "
                        f"Sources on that host are skipped by sync until then."
                    ),
                    "host": host_of(source.url),
                    "blocked_until": until.isoformat(),
                },
            )

    from scheduler import start_single_source_sync_background
    result = start_single_source_sync_background(engine, source_id, trigger_type="MANUAL")
    if result.get("status") == "busy":
        raise HTTPException(
            status_code=409, detail=result.get("detail", "A sync is already in progress")
        )
    return result


@router.patch("/{source_id}", response_model=SourceResponse)
def update_source(
    source_id: str,
    body: SourceUpdateRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Update a government source (status, priority, crawl_frequency, notes, url,
    extract_requirements)."""
    engine = get_db_engine(request)
    with get_session(engine) as session:
        repo = SourceRepo(session)
        obj = repo.get(source_id)
        if not obj:
            raise HTTPException(status_code=404, detail="Source not found")
        update_fields = body.model_dump(exclude_unset=True)
        # government_sources.status is NOT NULL — an explicit "status": null in the
        # request body would otherwise reach setattr() and fail as a raw 500
        # IntegrityError at commit time instead of a clean 422. Omitting the field
        # entirely is fine (exclude_unset drops it, status is simply left unchanged).
        if "status" in update_fields and update_fields["status"] is None:
            raise HTTPException(status_code=422, detail="status must not be null")
        update_fields = _validated_or_422(update_fields)
        for k, v in update_fields.items():
            setattr(obj, k, v)
        session.commit()
        return _source_to_response(obj, blocked_until(session, obj.url))

"""Pipeline run routes — query sync execution history.

Admin role required. Provides audit trail for government source sync
operations. Only GET /{run_id} is exposed (status polling for Run
now/Run all) - the frontend has no pipeline-runs list/summary/per-source
history page, so those routes were removed (see DESIGN.md).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from api.helpers import parse_uuid_or_404
from api.routes.auth import require_admin
from db.connection import get_session, get_db_engine
from db.models import Rule
from db.repositories import PipelineRunRepo, SyncJobRepo
from logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _compute_rule_counts(session, source_id: str) -> dict[str, int]:
    """Count rules by status for the given source."""
    stmt = (
        select(func.count(), Rule.status)
        .where(Rule.source_id == source_id)
        .group_by(Rule.status)
    )
    rows = list(session.execute(stmt).all())
    counts = {row[1]: row[0] for row in rows}
    return {
        "rules_validated": counts.get("VALIDATED", 0),
        "rules_approved": counts.get("APPROVED", 0),
        "rules_activated": counts.get("ACTIVE", 0),
    }


def _run_to_dict(run, session=None) -> dict[str, Any]:
    base = {
        "run_id": run.run_id,
        "source_id": run.source_id,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_ms": run.duration_ms,
        "old_hash": run.old_hash,
        "new_hash": run.new_hash,
        # requirements_extracted counts attempts, not stored rows (item 6) -
        # stored_new is the real "new knowledge" figure; the UI should show
        # that, not requirements_extracted, as the headline count.
        "requirements_extracted": run.requirements_extracted,
        "stored_new": run.stored_new,
        "requirements_approved": run.requirements_approved,
        "rules_created": run.rules_created,
        "errors": run.errors or [],
        "metadata": run.metadata_json or {},
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
    if session and run.source_id:
        counts = _compute_rule_counts(session, run.source_id)
        base.update(counts)
    else:
        base["rules_validated"] = run.rules_validated
        base["rules_approved"] = run.rules_approved
        base["rules_activated"] = run.rules_activated
    return base




def _sync_job_to_dict(job, session) -> dict[str, Any]:
    """Serialize a durable SyncJob into the shape the run monitor expects.

    The in-memory background job uses ``result`` as a nested summary; keep that
    contract for compatibility, but also expose the durable counters at the top
    level so a client can show progress without knowing about the fallback.
    Source statuses are lower-cased (and HOST_COOLDOWN becomes ``cooldown``) to
    match the per-source SyncResult vocabulary used by the existing UI.
    """
    source_rows = sorted(job.sources, key=lambda row: row.source_id)
    result_rows = []
    for row in source_rows:
        status = (row.status or "").lower()
        if row.status == "SKIPPED" and row.reason == "HOST_COOLDOWN":
            status = "cooldown"
        result_rows.append({
            "source_id": row.source_id,
            "status": status,
            "raw_status": row.status,
            "reason": row.reason,
            "fetch_status": row.fetch_status,
            "http_status": row.http_status,
            "error": row.error,
            "errors": [row.error] if row.error else [],
            "pipeline_run_id": row.pipeline_run_id,
            "attempt": row.attempt,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        })

    duration_ms = None
    if job.started_at and job.completed_at:
        duration_ms = int((job.completed_at - job.started_at).total_seconds() * 1000)

    summary = {
        "total_sources": job.total_sources,
        "processed_sources": job.processed_sources,
        "changed": job.changed_sources,
        "unchanged": job.unchanged_sources,
        "errors": job.error_sources,
        "cooldowns": sum(
            1 for row in source_rows
            if row.status == "SKIPPED" and row.reason == "HOST_COOLDOWN"
        ),
        "skipped": job.skipped_sources,
        "results": result_rows,
    }
    return {
        "run_id": job.job_id,
        "job_id": job.job_id,
        "source_id": None,
        "trigger_type": job.trigger_type,
        "status": job.status,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "duration_ms": duration_ms,
        "total_sources": job.total_sources,
        "processed_sources": job.processed_sources,
        "changed_sources": job.changed_sources,
        "unchanged_sources": job.unchanged_sources,
        "error_sources": job.error_sources,
        "skipped_sources": job.skipped_sources,
        "errors": [job.last_error] if job.last_error else [],
        "metadata": job.metadata_json or {},
        "result": summary,
    }

@router.get("/{run_id}")
def get_run(
    run_id: str,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Get a single pipeline run by ID, or a background sync job's progress.

    A background sync started via POST /api/v1/admin/sync has its own run_id
    distinct from any per-source PipelineRun (sync_all_sources creates one
    PipelineRun per source, not one for the whole batch) — checked first here
    since it's a cheap in-memory lookup, falling back to the PipelineRun table.
    """
    parse_uuid_or_404(run_id)

    from scheduler import get_sync_job
    job = get_sync_job(run_id)
    if job is not None:
        return job

    engine = get_db_engine(request)
    with get_session(engine) as session:
        durable = SyncJobRepo(session).get(run_id)
        if durable is not None:
            return _sync_job_to_dict(durable, session)
        repo = PipelineRunRepo(session)
        run = repo.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Pipeline run not found")
        return _run_to_dict(run, session)


@router.post("/{run_id}/resume", status_code=202)
def resume_run(
    run_id: str,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    """Resume an interrupted/partial all-sources sync (admin only).

    Already CHANGED/UNCHANGED sources are not fetched again. A run that is still
    in flight is rejected with 409 rather than duplicated; a completed run has
    nothing unfinished to resume.
    """
    parse_uuid_or_404(run_id)

    engine = get_db_engine(request)
    with get_session(engine) as session:
        job = SyncJobRepo(session).get(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Sync job not found")
        if job.status in ("PENDING", "RUNNING"):
            raise HTTPException(
                status_code=409,
                detail="Sync job is already in progress",
            )
        if job.status == "COMPLETED":
            raise HTTPException(
                status_code=409,
                detail="Sync job already completed",
            )

    from scheduler import start_resume_sync_background
    result = start_resume_sync_background(engine, run_id)
    if result.get("status") == "busy":
        raise HTTPException(
            status_code=409,
            detail=result.get("detail", "A sync is already in progress"),
        )
    return result

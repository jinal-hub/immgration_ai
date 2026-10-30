"""Government sync orchestrator — end-to-end pipeline from source to ACTIVE rules.

Pipeline:
    1. Real fetch every due source (no stored-hash short-circuit)
    2. Classify change: freshly fetched hash vs previously known hash
       (source.last_hash captured BEFORE the fetch)
    3. INITIAL/CHANGED → compile approved requirements → DRAFT rules
    4. source.last_hash / last_checked_at updated on every successful fetch

Every sync execution is tracked via PipelineRun for audit trail.

Failure-safe invariant:
    If extraction/compilation fails → old ACTIVE rules stay ACTIVE,
    new rules are NOT activated, PipelineRun = FAILED | PARTIAL.

Multi-level change detection (for reviewer visibility):
    Level 1 — RAW:       source last_hash vs freshly fetched content hash
    Level 2 — DOCUMENT:  old document version vs new document version
    Level 3 — REQUIREMENT: old requirement fields vs new requirement fields
    Level 4 — RULE:      old rule data vs new rule data
    Stored in PipelineRun.metadata_json["change_detection"].

crawl_frequency gating:
    Scheduled syncs (trigger_type="SCHEDULED") skip ACTIVE sources that were
    checked more recently than their crawl frequency. Manual/ON_DEMAND syncs
    always re-fetch. Sources with no stored document are always due so a failed
    or never-bootstrapped source keeps getting retried.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from db.models import (
    GovernmentSource, GovernmentDocument,
    DocumentChunk, Requirement, PipelineRun,
)
from change_detection.change_detector import ChangeType, classify_change
from db.repositories import PipelineRunRepo, SyncJobRepo
from rules.requirement_to_rule import compile_approved_requirements
from rules.rule_engine import is_field_evaluable
from rules.rule_repository import EnhancedRuleRepo
from logging_config import get_logger
from fetch.backend import new_session as new_fetch_session
from fetch.politeness import host_of
from fetch.timing import SourceTiming, format_timing_table
from orchestration.host_cooldown import blocked_until
from utils.fetch_document import fetch_document_and_store

logger = get_logger(__name__)

_CRAWL_INTERVALS: dict[str, timedelta] = {
    "DAILY": timedelta(hours=24),
    "WEEKLY": timedelta(days=7),
    "MONTHLY": timedelta(days=30),
}
_DEFAULT_CRAWL_INTERVAL = _CRAWL_INTERVALS["DAILY"]


@dataclass
class SyncResult:
    """Result of syncing a single government source."""
    source_id: str
    status: str  # "unchanged" | "changed" | "error" | "no_document" | "cooldown"
    document_id: str | None = None
    document_version: int | None = None
    old_hash: str | None = None
    new_hash: str | None = None
    # requirements_extracted counts attempts, not stored rows (item 6) - use
    # stored_new for "how much new knowledge did this sync add".
    requirements_extracted: int = 0
    stored_new: int = 0
    requirements_approved: int = 0
    rules_created: int = 0
    rules_validated: int = 0
    rules_approved: int = 0
    rules_activated: int = 0
    rules_drafted: int = 0
    errors: list[str] = field(default_factory=list)
    timing: SourceTiming | None = None
    # The per-source PipelineRun this result was recorded against (None when
    # the source was skipped before any run was opened, e.g. host cooldown).
    run_id: str | None = None


def detect_change(session: Session, source_id: str) -> tuple[bool, str | None, str | None]:
    """Check if the government source has changed since last check.

    Returns: (changed, old_hash, new_hash)
    """
    source = session.get(GovernmentSource, source_id)
    if not source:
        raise ValueError(f"Source {source_id} not found")

    doc = (
        session.query(GovernmentDocument)
        .filter(GovernmentDocument.source_id == source_id)
        .order_by(GovernmentDocument.retrieved_at.desc())
        .first()
    )
    if not doc:
        return False, None, None

    old_hash = source.last_hash
    new_hash = doc.content_hash

    if old_hash is None:
        return True, None, new_hash

    return old_hash != new_hash, old_hash, new_hash


def _latest_document(session: Session, source_id: str) -> GovernmentDocument | None:
    return (
        session.query(GovernmentDocument)
        .filter(GovernmentDocument.source_id == source_id)
        .order_by(GovernmentDocument.retrieved_at.desc())
        .first()
    )


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalize a possibly-naive UTC datetime (e.g. reloaded from SQLite) to aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


_EMPTY_CONTENT_WARNINGS = {
    "SCANNED_PDF": (
        "Fetch succeeded and the PDF changed, but it has no text layer - it is "
        "a scanned/image-only document. OCR is out of scope, so nothing could "
        "be extracted; the document version and its raw snapshot are stored as "
        "evidence. A human has to read this one."
    ),
    "NO_STATIC_CONTENT": (
        "Fetch succeeded and content changed, but extraction produced zero "
        "document chunks (e.g. a page that renders no structural content) - no "
        "extraction was attempted. Content review recommended."
    ),
}


def _empty_content_warning(reason: str | None) -> str:
    """Say which kind of "nothing to extract" this was; they need different
    human actions, so the warning must not collapse them."""
    return _EMPTY_CONTENT_WARNINGS.get(
        reason or "",
        "Fetch succeeded and content changed, but extraction produced zero "
        "document chunks - no extraction was attempted. Content review "
        "recommended.",
    )


def _record_run_failure(session: Session, run: PipelineRun, error: str) -> PipelineRun:
    """Persist a FAILED PipelineRun on a healthy transaction.

    A database error raised mid-sync aborts the surrounding transaction in
    PostgreSQL: any further statement on it fails with ``InFailedSqlTransaction``.
    Roll back to a clean state first so the FAILED status can always be recorded
    and the remaining sources in the batch can keep running. If the run's row was
    already durable (committed during an earlier step such as the fetch), it is
    re-fetched in the fresh transaction and flipped to FAILED; otherwise it is
    re-inserted with the same run_id.
    """
    try:
        return PipelineRunRepo(session).fail(run.run_id, error)
    except Exception:
        # The current transaction was aborted — start a fresh one.
        session.rollback()
        run_repo = PipelineRunRepo(session)
        existing = session.get(PipelineRun, run.run_id)
        if existing is not None:
            return run_repo.fail(run.run_id, error)
        now = datetime.now(timezone.utc)
        fresh = PipelineRun(
            run_id=run.run_id,
            source_id=run.source_id,
            trigger_type=run.trigger_type,
            status="FAILED",
            started_at=_as_utc(run.started_at) or now,
            completed_at=now,
            errors=[error],
        )
        session.add(fresh)
        session.flush()
        return fresh


def _crawl_frequency_value(frequency: str | None) -> timedelta:
    """Map a source's ``crawl_frequency`` to a politeness interval (default DAILY)."""
    return _CRAWL_INTERVALS.get((frequency or "DAILY").upper(), _DEFAULT_CRAWL_INTERVAL)


def _source_is_due(session: Session, source: GovernmentSource, now: datetime) -> bool:
    """Whether a source should be fetched on a scheduled sync.

    Never checked before → due. No stored document → due (so bootstrap/failed
    sources keep being retried). Otherwise due once the crawl-frequency interval
    has elapsed since ``last_checked_at``.
    """
    if source.last_checked_at is None:
        return True
    if _latest_document(session, source.source_id) is None:
        return True
    last_checked = _as_utc(source.last_checked_at)
    return (now - last_checked) >= _crawl_frequency_value(source.crawl_frequency)


def get_source_documents(session: Session, source_id: str) -> list[dict]:
    """Get all document versions for a source, newest first."""
    docs = (
        session.query(GovernmentDocument)
        .filter(GovernmentDocument.source_id == source_id)
        .order_by(GovernmentDocument.retrieved_at.desc())
        .all()
    )
    return [
        {
            "document_id": d.document_id,
            "title": d.title,
            "content_hash": d.content_hash,
            "current_version": d.current_version,
            "retrieved_at": d.retrieved_at.isoformat() if d.retrieved_at else None,
        }
        for d in docs
    ]


def get_document_chunks(session: Session, document_id: str) -> list[dict]:
    """Get all chunks for a document."""
    chunks = (
        session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.order_index)
        .all()
    )
    return [
        {
            "chunk_id": c.chunk_id,
            "chunk_type": c.chunk_type,
            "heading_path": c.heading_path,
            "order_index": c.order_index,
            "raw_text": c.raw_text,
            "page_number": c.page_number,
        }
        for c in chunks
    ]


def compute_requirement_diff(old_reqs: list[dict], new_reqs: list[dict]) -> dict:
    """Compare old and new requirement snapshots.

    Returns a structured diff with added, removed, and changed requirements.
    """
    old_by_key = {r["semantic_key"]: r for r in old_reqs}
    new_by_key = {r["semantic_key"]: r for r in new_reqs}

    added = [new_by_key[k] for k in new_by_key if k not in old_by_key]
    removed = [old_by_key[k] for k in old_by_key if k not in new_by_key]

    changed = []
    for key in old_by_key:
        if key in new_by_key:
            old_r = old_by_key[key]
            new_r = new_by_key[key]
            diffs = {}
            for field_name in ("operator", "value_json", "mandatory", "criticality",
                               "field", "category", "description"):
                old_val = old_r.get(field_name)
                new_val = new_r.get(field_name)
                if old_val != new_val:
                    diffs[field_name] = {"old": old_val, "new": new_val}
            if diffs:
                changed.append({
                    "semantic_key": key,
                    "requirement_id": new_r.get("requirement_id"),
                    "fields_changed": diffs,
                })

    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "added": [{"semantic_key": r["semantic_key"]} for r in added],
        "removed": [{"semantic_key": r["semantic_key"]} for r in removed],
        "changed": changed,
    }


def compute_rule_diff(old_rules: list[dict], new_rules: list[dict]) -> dict:
    """Compare old and new rule snapshots.

    Returns a structured diff with added, removed, and changed rules.
    """
    old_by_key = {(r["program_id"], r["category"], r["field"]): r for r in old_rules}
    new_by_key = {(r["program_id"], r["category"], r["field"]): r for r in new_rules}

    added = [new_by_key[k] for k in new_by_key if k not in old_by_key]
    removed = [old_by_key[k] for k in old_by_key if k not in new_by_key]

    changed = []
    for key in old_by_key:
        if key in new_by_key:
            old_r = old_by_key[key]
            new_r = new_by_key[key]
            diffs = {}
            for field_name in ("operator", "value_json", "mandatory", "critical",
                               "message"):
                old_val = old_r.get(field_name)
                new_val = new_r.get(field_name)
                if old_val != new_val:
                    diffs[field_name] = {"old": old_val, "new": new_val}
            if diffs:
                changed.append({
                    "program_id": key[0],
                    "category": key[1],
                    "field": key[2],
                    "fields_changed": diffs,
                })

    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "added": [{"program_id": r["program_id"], "field": r["field"]} for r in added],
        "removed": [{"program_id": r["program_id"], "field": r["field"]} for r in removed],
        "changed": changed,
    }


def compute_change_detection(
    session: Session,
    source_id: str,
    old_hash: str | None,
    new_hash: str | None,
    program_id: str,
) -> dict:
    """Compute multi-level change detection for a sync run.

    Levels:
        1. RAW: hash comparison
        2. REQUIREMENT: requirement field diffs
        3. RULE: rule field diffs (before vs after compilation)

    Returns a dict suitable for PipelineRun.metadata_json["change_detection"].
    """
    change: dict[str, Any] = {
        "raw": {
            "old_hash": old_hash,
            "new_hash": new_hash,
            "changed": old_hash != new_hash if old_hash and new_hash else True,
        },
    }

    # Level 2: Requirement diff
    old_reqs = [
        {
            "semantic_key": r.semantic_key,
            "requirement_id": r.requirement_id,
            "operator": r.operator,
            "value_json": r.value_json,
            "mandatory": r.mandatory,
            "criticality": r.criticality,
            "field": r.field,
            "category": r.category,
            "description": r.description,
        }
        for r in session.query(Requirement).filter(
            Requirement.program_id == program_id,
            Requirement.status == "APPROVED",
        ).all()
    ]
    # After compilation, requirements remain APPROVED — the diff is "before sync" snapshot
    change["requirement"] = {
        "snapshot_count": len(old_reqs),
        "snapshot": old_reqs,
    }

    # Level 3: Rule diff — load current active rules before compilation
    rule_repo = EnhancedRuleRepo(session)
    old_active = rule_repo.list_active(program_id)
    old_rule_snapshots = [
        {
            "rule_id": r.rule_id,
            "program_id": r.program_id,
            "category": r.category,
            "field": r.field,
            "operator": r.operator,
            "value_json": r.value_json,
            "mandatory": r.mandatory,
            "critical": r.critical,
            "message": r.message,
            "status": r.status,
            "version": r.current_version,
        }
        for r in old_active
    ]
    change["rule"] = {
        "active_before_count": len(old_rule_snapshots),
        "active_before": old_rule_snapshots,
    }

    return change


def sync_source(session: Session, source_id: str,
                dry_run: bool = False, trigger_type: str = "ON_DEMAND",
                browser_session=None) -> SyncResult:
    """Run the full sync pipeline for one government source.

    ``browser_session``: an optional shared retrieval session,
    passed through to ``fetch_document_and_store`` so a batch of sources in
    one sync run can reuse a single already-running browser. The standalone/
    single-source path (no session passed) is unaffected.

    Creates a PipelineRun to track execution. Non-dry-run:
        1. Capture previous_hash = source.last_hash (BEFORE the fetch)
        2. Always re-fetch via fetch_document_and_store (browser escalation on
           blocks; source.last_hash/last_checked_at updated on every success)
        3. Classify classify_change(previous_hash, fresh_hash):
             UNCHANGED   → complete early (no change detection, no new version)
             INITIAL/CHANGED → compile approved requirements → DRAFT rules
        4. Fetch failure → run FAILED (source demoted when browser-blocked)

    dry_run performs a non-network, stored-state preview via detect_change().

    Failure-safe: if any step fails, old ACTIVE rules remain untouched.
    PipelineRun is marked FAILED or PARTIAL with error details.

    Refuses to run while the source's host is in a refusal cooldown: the run is
    rejected *before* a PipelineRun is opened, because no request was made and
    none should be. sync_all_sources() skips these sources up front; this guard
    covers the single-source path and anything racing in between.
    """
    if not dry_run:
        # Look up first rather than raising here: a missing source must keep
        # reporting through the existing "not found" error path below.
        _pending = session.get(GovernmentSource, source_id)
        if _pending is not None:
            until = blocked_until(session, _pending.url)
            if until is not None:
                message = (
                    f"Host {host_of(_pending.url)} refused our last request; "
                    f"not trying again before {until.isoformat()}"
                )
                logger.warning(
                    "Source sync refused — host is in a refusal cooldown",
                    extra={
                        "source_id": source_id,
                        "host": host_of(_pending.url),
                        "cooldown_until": until.isoformat(),
                    },
                )
                return SyncResult(source_id=source_id, status="cooldown",
                                  errors=[message])

    run_repo = PipelineRunRepo(session)
    rule_repo = EnhancedRuleRepo(session)
    run = run_repo.create(source_id=source_id, trigger_type=trigger_type)
    result = SyncResult(source_id=source_id, status="error", run_id=run.run_id)

    try:
        if dry_run:
            # 1. Detect change — dry-run preview over stored state (no network)
            changed, old_hash, new_hash = detect_change(session, source_id)
            run.old_hash = old_hash
            run.new_hash = new_hash

            if not changed:
                result.status = "unchanged"
                run_repo.complete(run.run_id)
                logger.info("Source unchanged (dry-run)", extra={"source_id": source_id})
                return result

            result.status = "changed"
            return _finalize_change(
                session, run_repo, rule_repo, run, result,
                source_id, old_hash, new_hash,
                dry_run=True, requirements_extracted=0,
            )

        # ---- Real path: always fetch, classify freshly fetched hash ----
        source = session.get(GovernmentSource, source_id)
        if source is None:
            raise ValueError(f"Source {source_id} not found")

        previous_hash = source.last_hash
        result.old_hash = previous_hash

        outcome = fetch_document_and_store(session, source, browser_session=browser_session)
        fresh_hash = outcome.content_hash
        run.old_hash = previous_hash
        run.new_hash = fresh_hash
        result.new_hash = fresh_hash
        result.timing = outcome.timing

        if outcome.document_id is None:
            # Fetch failed; diagnostics/demotion already handled by the fetcher.
            error_msg = outcome.error or outcome.status or "fetch failed"
            result.status = "error"
            result.errors.append(error_msg)
            _record_run_failure(session, run, error_msg)
            logger.warning(
                "Source fetch failed during sync",
                extra={
                    "source_id": source_id,
                    "fetch_status": outcome.status,
                    "error": error_msg,
                    "run_id": run.run_id,
                },
            )
            return result

        change = classify_change(previous_hash, fresh_hash)

        if change == ChangeType.UNCHANGED:
            # Re-fetched, but content identical to the last known hash → no
            # new version, no change detection, no re-extraction.
            result.status = "unchanged"
            result.document_id = outcome.document_id
            result.document_version = outcome.document_version
            run_repo.complete(run.run_id)
            logger.info(
                "Source unchanged (re-fetched, content identical)",
                extra={"source_id": source_id, "run_id": run.run_id},
            )
            return result

        result.status = "changed"
        result.requirements_extracted = outcome.requirements_extracted
        result.stored_new = outcome.stored_new
        empty_content_warning = (
            _empty_content_warning(outcome.empty_content_reason)
            if outcome.empty_content else None
        )
        return _finalize_change(
            session, run_repo, rule_repo, run, result,
            source_id, previous_hash, fresh_hash,
            dry_run=False, requirements_extracted=outcome.requirements_extracted,
            stored_new=outcome.stored_new,
            empty_content_warning=empty_content_warning,
        )

    except Exception as e:
        result.status = "error"
        result.errors.append(str(e))
        # Failure-safe: if anything fails, old ACTIVE rules are untouched
        # PipelineRun is FAILED, new rules never activated.
        # The recorder rolls back any aborted transaction first so a PostgreSQL
        # DB error mid-run can never poison the failure handler.
        _record_run_failure(session, run, str(e))
        logger.error("Source sync failed",
                      extra={"source_id": source_id, "error": str(e),
                             "run_id": run.run_id})

    return result


def _finalize_change(
    session: Session,
    run_repo: PipelineRunRepo,
    rule_repo: EnhancedRuleRepo,
    run,
    result: SyncResult,
    source_id: str,
    old_hash: str | None,
    new_hash: str | None,
    dry_run: bool,
    requirements_extracted: int = 0,
    stored_new: int = 0,
    empty_content_warning: str | None = None,
) -> SyncResult:
    """Run the shared changed-path body: change detection → compile → run record.

    Used by both the dry-run preview and the real always-fetch path. The real
    path passes the number of requirements extracted by the fetch step so the
    PipelineRun audit metrics reflect bootstrap/extraction (as the legacy
    zero-document path did). ``requirements_extracted`` counts attempts, not
    stored rows (item 6) - ``stored_new`` is the real "new knowledge" figure.

    ``empty_content_warning`` (real path only): the fetch succeeded and content
    genuinely changed, but extraction found zero document chunks. Seeded into
    the run's errors so it lands as a visible PARTIAL, never a silent COMPLETED.
    """
    result.requirements_extracted = requirements_extracted
    result.stored_new = stored_new

    doc = _latest_document(session, source_id)
    if not doc:
        result.status = "no_document"
        run_repo.fail(run.run_id, "No document found for source")
        return result

    result.document_id = doc.document_id
    result.document_version = doc.current_version

    # 1. Compute multi-level change detection (before compilation)
    change_detection = compute_change_detection(
        session, source_id, old_hash, new_hash, doc.program_id,
    )

    # 2. Count existing approved requirements
    approved_count = (
        session.query(Requirement)
        .filter(Requirement.program_id == doc.program_id)
        .filter(Requirement.status == "APPROVED")
        .count()
    )
    result.requirements_approved = approved_count

    # 3. Compile approved requirements → DRAFT rules
    #    Failure-safe: if compilation fails, old rules stay ACTIVE
    errors: list[str] = [empty_content_warning] if empty_content_warning else []
    if approved_count > 0 and not dry_run:
        try:
            compilation_results = compile_approved_requirements(
                session, program_id=doc.program_id, dry_run=dry_run
            )
            result.rules_created = sum(
                1 for r in compilation_results if r["status"] == "created"
            )
            result.rules_drafted = result.rules_created

            # Track per-rule errors
            for r in compilation_results:
                if r["status"] == "error" and r.get("error"):
                    errors.append(f"{r['requirement_id']}: {r['error']}")
        except Exception as comp_err:
            errors.append(f"Compilation failed: {comp_err}")
            logger.error("Requirement compilation failed",
                         extra={"source_id": source_id, "error": str(comp_err),
                                "run_id": run.run_id})

    # 4. Compute rule diff after compilation
    new_active = rule_repo.list_active(doc.program_id)
    change_detection["rule"]["active_after_count"] = len(new_active)
    change_detection["rule"]["active_after"] = [
        {
            "rule_id": r.rule_id,
            "program_id": r.program_id,
            "category": r.category,
            "field": r.field,
            "operator": r.operator,
            "value_json": r.value_json,
            "mandatory": r.mandatory,
            "critical": r.critical,
            "status": r.status,
            "version": r.current_version,
        }
        for r in new_active
    ]
    change_detection["rule"]["diff"] = compute_rule_diff(
        change_detection["rule"]["active_before"],
        change_detection["rule"]["active_after"],
    )

    # 5. Update PipelineRun via state machine.
    #    source.last_hash / last_checked_at were already updated by
    #    fetch_document_and_store after the fresh fetch (real path only).
    run_kwargs = dict(
        requirements_extracted=result.requirements_extracted,
        stored_new=result.stored_new,
        requirements_approved=result.requirements_approved,
        rules_created=result.rules_created,
        rules_validated=result.rules_validated,
        rules_approved=result.rules_approved,
        rules_activated=result.rules_activated,
    )
    if errors:
        run.metadata_json = {"change_detection": change_detection}
        try:
            run_repo.partial(
                run.run_id, "; ".join(errors), **run_kwargs,
            )
        except Exception as write_err:
            # The failing compile statement aborted the transaction (PostgreSQL
            # InFailedSqlTransaction), so even the PARTIAL write fails. Surface the
            # actionable root cause — not the generic "transaction is aborted" —
            # so the FAILED record that follows keeps the real error.
            raise RuntimeError(
                f"Recording partial results failed: {'; '.join(errors)}"
            ) from write_err
        logger.warning("Source sync partial",
                       extra={"source_id": source_id, "errors": errors,
                              "run_id": run.run_id})
    else:
        run.metadata_json = {"change_detection": change_detection}
        run_repo.complete(run.run_id, **run_kwargs)
        logger.info("Source sync completed",
                    extra={"source_id": source_id, "status": result.status,
                           "rules_created": result.rules_created,
                           "run_id": run.run_id})

    return result


def _job_source_status(result: SyncResult) -> str:
    """Map a SyncResult.status onto the durable SyncJobSource vocabulary."""
    status = (result.status or "").lower()
    if status == "changed":
        return "CHANGED"
    if status == "unchanged":
        return "UNCHANGED"
    if status in ("cooldown", "no_document"):
        return "SKIPPED"
    return "ERROR"


def _fetch_snapshot(session: Session, source_id: str) -> tuple[str | None, int | None]:
    """Current persisted fetch diagnostics for a source, after a sync attempt."""
    source = session.get(GovernmentSource, source_id)
    if source is None:
        return None, None
    return source.fetch_status, source.http_status


def sync_all_sources(session: Session, dry_run: bool = False,
                     trigger_type: str = "ON_DEMAND",
                     sync_job_id: str | None = None) -> list[SyncResult]:
    """Sync all active government sources.

    Scheduled syncs (trigger_type="SCHEDULED", non-dry-run) skip ACTIVE sources
    that were checked more recently than their ``crawl_frequency``. Manual and
    on-demand syncs always re-fetch, so an admin can force a refresh.

    Every non-dry-run source gets a durable SyncJobSource row *before* the
    first fetch, and progress is committed per source. That is what makes a
    multi-hour run survivable: a restart leaves an honest INTERRUPTED job with
    the already-finished sources recorded, and ``sync_job_id`` can be passed
    again to resume exactly the unfinished ones. ``sync_job_id`` also lets the
    in-memory background-sync handle use the same id as the durable job.

    Every source in this run shares ONE browser (fetch.backend.new_session()),
    with a fresh context per source so cookies and storage never leak between
    sources. It is lazy - nothing launches until the first actual fetch - and
    always closed at the end of the run, success or failure.
    """
    now = datetime.now(timezone.utc)
    sources = (
        session.query(GovernmentSource)
        .filter(GovernmentSource.status == "ACTIVE")
        .all()
    )
    source_ids = [src.source_id for src in sources]

    job_id: str | None = None
    if not dry_run:
        job_id = sync_job_id or str(uuid.uuid4())
        job_repo = SyncJobRepo(session)
        if job_repo.get(job_id) is None:
            job_repo.create(job_id, trigger_type, source_ids)
        else:
            job_repo.prepare_resume(job_id, source_ids)
        # Durable before any network work: a killed process must still leave a
        # resumable record instead of an empty table.
        session.commit()
        logger.info(
            "Sync job started",
            extra={
                "sync_job_id": job_id,
                "trigger_type": trigger_type,
                "total_sources": len(source_ids),
            },
        )

    results = []
    browser_session = new_fetch_session() if not dry_run else None
    try:
        results = _sync_sources(
            session, sources, dry_run, trigger_type, now, browser_session,
            sync_job_id=job_id,
        )
    finally:
        if browser_session is not None:
            browser_session.close()

    timings = [r.timing for r in results if r.timing is not None]
    if timings:
        logger.info(
            "Sync timing summary\n%s", format_timing_table(timings),
            extra={"sources_timed": len(timings)},
        )
    return results


def _sync_sources(
    session: Session, sources: list[GovernmentSource], dry_run: bool,
    trigger_type: str, now: datetime, browser_session,
    sync_job_id: str | None = None,
) -> list[SyncResult]:
    results = []
    job_repo = SyncJobRepo(session) if sync_job_id else None

    try:
        for src in sources:
            source_id = src.source_id
            row = (
                job_repo.get_source(sync_job_id, source_id)
                if job_repo is not None else None
            )
            # A resume never re-fetches a source that already genuinely
            # finished in the original run.
            if row is not None and row.status in ("CHANGED", "UNCHANGED"):
                results.append(SyncResult(
                    source_id=source_id, status=row.status.lower(),
                ))
                continue

            if job_repo is not None:
                job_repo.mark_running(sync_job_id, source_id)
                session.commit()

            if not dry_run:
                # A host that refused us is paused. Every source on it is skipped
                # for the rest of the run - the refusal is about the host, not the
                # one URL we happened to ask for.
                until = blocked_until(session, src.url, now)
                if until is not None:
                    message = (
                        f"Host {host_of(src.url)} refused an earlier request; "
                        f"this source was skipped until {until.isoformat()}."
                    )
                    logger.info(
                        "Source skipped - host is in a refusal cooldown",
                        extra={
                            "source_id": source_id,
                            "host": host_of(src.url),
                            "cooldown_until": until.isoformat(),
                        },
                    )
                    result = SyncResult(
                        source_id=source_id,
                        status="cooldown",
                        errors=[message],
                    )
                    if job_repo is not None:
                        fetch_status, http_status = _fetch_snapshot(session, source_id)
                        job_repo.mark_source(
                            sync_job_id, source_id, "SKIPPED",
                            reason="HOST_COOLDOWN", error=message,
                            fetch_status=fetch_status, http_status=http_status,
                        )
                        session.commit()
                    results.append(result)
                    continue
            if (
                trigger_type == "SCHEDULED"
                and not dry_run
                and not _source_is_due(session, src, now)
            ):
                logger.info(
                    "Source skipped - not due per crawl_frequency",
                    extra={"source_id": source_id, "crawl_frequency": src.crawl_frequency},
                )
                if job_repo is not None:
                    job_repo.mark_source(
                        sync_job_id, source_id, "SKIPPED", reason="NOT_DUE",
                    )
                    session.commit()
                continue
            try:
                result = sync_source(session, source_id, dry_run=dry_run,
                                     trigger_type=trigger_type,
                                     browser_session=browser_session)
            except Exception as e:  # very last line of defence - never poison the batch
                # A failed source must not terminate the sync: roll back its partial
                # writes, record a FAILED PipelineRun on the fresh transaction, and
                # continue with the remaining sources.
                session.rollback()
                failure_now = datetime.now(timezone.utc)
                run = PipelineRun(
                    source_id=source_id, trigger_type=trigger_type,
                    status="FAILED",
                    started_at=failure_now,
                    completed_at=failure_now,
                    errors=[f"Unhandled sync failure: {e}"],
                )
                session.add(run)
                session.flush()
                logger.error(
                    "Source sync crashed unhandled - batch continues",
                    extra={"source_id": source_id, "error": str(e),
                           "run_id": run.run_id},
                )
                result = SyncResult(source_id=source_id, status="error",
                                    errors=[str(e)], run_id=run.run_id)

            if job_repo is not None:
                fetch_status, http_status = _fetch_snapshot(session, source_id)
                reason = None
                if result.status == "cooldown":
                    reason = "HOST_COOLDOWN"
                elif result.status == "no_document":
                    reason = "NO_DOCUMENT"
                job_repo.mark_source(
                    sync_job_id, source_id, _job_source_status(result),
                    reason=reason,
                    error="; ".join(result.errors) if result.errors else None,
                    fetch_status=fetch_status, http_status=http_status,
                    pipeline_run_id=result.run_id,
                )
                # Per-source commit is the whole point: everything up to and
                # including this source is durable before the next fetch starts.
                session.commit()
            results.append(result)

    except BaseException:
        if job_repo is not None:
            try:
                session.rollback()
                job_repo.interrupt(sync_job_id)
                session.commit()
            except Exception:
                logger.exception(
                    "Failed to mark sync job INTERRUPTED",
                    extra={"sync_job_id": sync_job_id},
                )
        raise

    if job_repo is not None:
        try:
            job_repo.finish(sync_job_id)
            session.commit()
            job = job_repo.get(sync_job_id)
            logger.info(
                "Sync job finished",
                extra={
                    "sync_job_id": sync_job_id,
                    "status": job.status if job else None,
                    "total_sources": job.total_sources if job else None,
                    "changed": job.changed_sources if job else None,
                    "unchanged": job.unchanged_sources if job else None,
                    "errors": job.error_sources if job else None,
                    "skipped": job.skipped_sources if job else None,
                },
            )
        except Exception:
            # Source work is already durable; a failure to write the aggregate
            # must not turn a successful batch into an exception. The job stays
            # RUNNING and can be resumed/recovered.
            session.rollback()
            logger.exception(
                "Failed to finalize sync job", extra={"sync_job_id": sync_job_id},
            )
    return results


def get_review_queue(session: Session, program_id: str | None = None) -> dict[str, Any]:
    """Get a summary of rules awaiting human review/activation."""
    rule_repo = EnhancedRuleRepo(session)
    drafts = rule_repo.list_drafts(program_id)
    validated = rule_repo.list_validated(program_id)
    approved = rule_repo.list_approved_pending_activation(program_id)
    active = rule_repo.list_active(program_id)

    return {
        "drafts": [
            {"rule_id": r.rule_id, "program_id": r.program_id,
             "field": r.field, "category": r.category,
             "evaluable": is_field_evaluable(r.field),
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in drafts
        ],
        "validated": [
            {"rule_id": r.rule_id, "program_id": r.program_id,
             "field": r.field, "category": r.category,
             "evaluable": is_field_evaluable(r.field)}
            for r in validated
        ],
        "approved_pending": [
            {"rule_id": r.rule_id, "program_id": r.program_id,
             "field": r.field, "category": r.category,
             "evaluable": is_field_evaluable(r.field)}
            for r in approved
        ],
        "active": [
            {"rule_id": r.rule_id, "program_id": r.program_id,
             "field": r.field, "category": r.category,
             "evaluable": is_field_evaluable(r.field),
             "version": r.current_version}
            for r in active
        ],
        "counts": {
            "drafts": len(drafts),
            "validated": len(validated),
            "approved_pending": len(approved),
            "active": len(active),
        },
    }

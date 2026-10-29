"""Fetch → store → extract for the ORM sync path.

This is the "real network" path that bootstraps a government source with no stored document:

    fetch (plain HTTP) ─┬─ OK ─────────────────────► extract_html / extract_pdf ──► store
                        │                               document / version / chunks
                        │                             + persist immutable raw snapshot
                        ├─ ACCESS_BLOCKED ─────────┐
                        │   / REQUIRES_BROWSER      ├─► browser escalation (genuine session)
                        └─ other failure ──────────┴─► FetchLog + source diagnostics

After a successful fetch + store it runs the Phase-2 requirement extraction contract
(``run_requirement_extraction``) with the real extraction model.  The model is resolved
via ``EXTRACTION_MODEL`` (defaulting to ``command-a-03-2025``); the matching provider API key
(COHERE_API_KEY for the default Cohere model) must be set — there is no silent fallback to a mock
or to any other provider.

Everything here is ORM-driven on the session's transaction; Phase-2 persistence runs through
``db.conn.SqliteConn`` (a sqlite3-shaped shim over the same session) so legacy module code is
reused unchanged.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dotenv
import fetch.backend as fetch_backend
import fetch.fetcher as fetcher
from fetch.timing import SourceTiming

# In TESTS, dotenv loading is suppressed so a developer's .env cannot leak
# keys into the hermetic suite (confest strips them at module load).
if not os.environ.get("TESTING"):
    dotenv.load_dotenv()
from db.conn import SqliteConn
from db.current_session import current_session
from db.models import (
    DocumentChunk,
    DocumentVersion,
    ExtractionRun,
    FetchLog,
    GovernmentDocument,
    GovernmentSource,
)
from fetch.extractor_html import extract_html as _extract_html
from fetch.extractor_html import extract_news_list as _extract_news_list
from fetch.extractor_pdf import extract_pdf as _extract_pdf
from change_detection.hashing import sha256_bytes
from logging_config import get_logger
from orchestration.host_cooldown import clear_cooldown, is_refusal, start_cooldown
from sqlalchemy.orm import Session

# Retired retrieval path, reachable only via FETCH_BACKEND=legacy and deleted
# in Phase 1d. Imported so the legacy switch keeps working until then; nothing
# in the Playwright path touches it.
import fetch.browser_fetcher as browser_fetcher  # noqa: F401

logger = get_logger(__name__)

# There is no escalation set any more. Retrieval is Playwright-only (see
# fetch/backend.py and fetch/playwright_client.py): every source is fetched by
# a real browser the first time, so there is no second mechanism to fall back
# to and no way for a refusal to change how we fetch. The old set lived here
# and once wrongly included ACCESS_BLOCKED, which burned a full browser launch
# on every refused source for no possible benefit.


# ---------------------------------------------------------------------------
# Immutable raw snapshots.
#
# The raw fetched bytes are the evidentiary root of the whole system: every
# extracted requirement must be verifiable against the literal original
# government document, so government_documents.raw_path and
# document_versions.raw_path must be real, dereferenceable filesystem paths.
#
# The layout mirrors the legacy CLI (src/pipeline.py):
#     raw/{country_id}/{visa_category}/{source_id}/content.v{version_number}.{ext}
# The legacy CLI keys the leaf by calendar date; this path keys it by
# version_number instead, because a source that changes twice in one day would
# otherwise overwrite the earlier snapshot while its document_versions row still
# referenced it — silently destroying a version the system still claims is
# immutable.
#
# RAW_SNAPSHOT_DIR overrides the root (hermetic tests, or a deployment that
# mounts a durable volume for snapshots).
# ---------------------------------------------------------------------------
def _raw_root() -> Path:
    override = os.getenv("RAW_SNAPSHOT_DIR")
    if override:
        return Path(override)
    from db import ROOT

    return ROOT / "raw"


def _raw_snapshot_relpath(source: GovernmentSource, version_number: int) -> Path:
    source_type = (source.source_type or "HTML").upper()
    ext = "pdf" if source_type == "PDF" else "html"
    return (
        Path(str(source.country_id or "ZZ"))
        / (source.visa_category or "general").lower()
        / str(source.source_id)
        / f"content.v{version_number}.{ext}"
    )


def _manifest_path(path: Path) -> Path:
    """Sidecar manifest for a raw snapshot: ``content.v1.html`` -> ``content.v1.html.meta.json``."""
    return path.parent / f"{path.name}.meta.json"


class RawSnapshotIntegrityError(RuntimeError):
    """An immutable snapshot exists but its bytes or manifest do not agree."""


def _write_new_file(path: Path, data: bytes) -> None:
    """Atomically publish ``data`` at ``path`` without ever replacing a file.

    The bytes go to a temp file in the same directory (rename/link is only
    atomic within a filesystem) and are then hard-linked into place with
    ``O_EXCL`` semantics. If the target already exists, ``FileExistsError`` is
    raised and the caller decides whether the existing bytes are acceptable.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise
        except OSError:
            # Some filesystems/Windows configurations do not support hard
            # links. O_EXCL still guarantees we never clobber an existing
            # snapshot; a crash can leave a partial file, which the manifest
            # check on the next run will refuse.
            with open(path, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _manifest_bytes(metadata: dict[str, Any]) -> bytes:
    return (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _verify_existing_snapshot(
    path: Path, raw_bytes: bytes, content_hash: str, metadata: dict[str, Any],
) -> None:
    existing = path.read_bytes()
    if existing != raw_bytes:
        raise RawSnapshotIntegrityError(
            f"immutable snapshot {path} already exists with different bytes "
            f"(stored sha256={sha256_bytes(existing)}, incoming={content_hash})"
        )
    manifest_path = _manifest_path(path)
    if not manifest_path.is_file():
        raise RawSnapshotIntegrityError(
            f"immutable snapshot {path} has no retrieval manifest"
        )
    try:
        stored_meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RawSnapshotIntegrityError(
            f"retrieval manifest {manifest_path} is unreadable: {exc}"
        ) from exc
    if stored_meta.get("content_hash") != content_hash:
        raise RawSnapshotIntegrityError(
            f"retrieval manifest {manifest_path} does not match stored bytes"
        )
    if stored_meta.get("content_length") != len(raw_bytes):
        raise RawSnapshotIntegrityError(
            f"retrieval manifest {manifest_path} records a different length"
        )
    # The first manifest wins: identical bytes on the same version keep their
    # original retrieval time/duration rather than being rewritten to look like
    # a fresh acquisition.
    for key in ("source_id", "document_id", "version_number"):
        if key in metadata and stored_meta.get(key) != metadata[key]:
            raise RawSnapshotIntegrityError(
                f"retrieval manifest {manifest_path} disagrees on {key}"
            )


def _status_value(status: Any) -> str | None:
    value = getattr(status, "value", status)
    return str(value) if value is not None else None


def build_raw_metadata(
    source: GovernmentSource,
    document_id: str,
    version_number: int,
    content_hash: str,
    retrieved_at: datetime,
    raw_bytes: bytes,
    fetch_result: Any = None,
    acquisition: str = "http",
) -> dict[str, Any]:
    """Retrieval metadata for one immutable raw snapshot.

    The raw bytes alone are not sufficient evidence: reproducing what the
    extractor received requires knowing *which* document and version was
    fetched, *from where*, *when*, *how* it was obtained, and *whether the
    acquisition succeeded*.  This dict is written next to the bytes so the
    artifact is self-describing rather than depending on live DB rows that may
    later change.
    """
    return {
        "schema_version": 1,
        "acquisition": acquisition,
        "source_id": source.source_id,
        "country_id": source.country_id,
        "visa_category": source.visa_category,
        "source_type": (source.source_type or "HTML").upper(),
        "document_id": document_id,
        "version_number": version_number,
        "requested_url": source.url,
        "final_url": getattr(fetch_result, "url", None) or source.url,
        "content_type": getattr(fetch_result, "content_type", None),
        "http_status": getattr(fetch_result, "http_status", None),
        "fetch_status": _status_value(getattr(fetch_result, "status", None)),
        "error_type": getattr(fetch_result, "error_type", None),
        "duration_ms": getattr(fetch_result, "duration_ms", None),
        "retrieved_at": retrieved_at.isoformat(),
        "content_hash": content_hash,
        "content_length": len(raw_bytes),
    }


def persist_raw_snapshot(
    source: GovernmentSource,
    version_number: int,
    raw_bytes: bytes,
    document_id: str | None = None,
    content_hash: str | None = None,
    retrieved_at: datetime | None = None,
    fetch_result: Any = None,
    acquisition: str = "http",
) -> Path:
    """Write the immutable raw snapshot (and its metadata manifest) and return
    the path actually written.

    Idempotent: ``version_number`` only advances when the content hash changes,
    so a given (document_id, version_number) always maps to the same bytes.

    A sidecar ``<snapshot>.meta.json`` manifest records the retrieval metadata
    (source/document identity, version, URL, content type, HTTP/browser status
    and timing, content hash) so the stored artifact is sufficient on its own to
    reproduce what the extractor received.  It is written with sorted keys and a
    fixed indent so identical retrievals produce byte-identical manifests.
    """
    path = _raw_root() / _raw_snapshot_relpath(source, version_number)
    path.parent.mkdir(parents=True, exist_ok=True)

    actual_hash = sha256_bytes(raw_bytes)
    if content_hash is not None and content_hash != actual_hash:
        raise RawSnapshotIntegrityError(
            f"content_hash {content_hash} does not match the bytes to store "
            f"({actual_hash})"
        )
    metadata = build_raw_metadata(
        source=source,
        document_id=document_id or f"DOC-{str(source.source_id).replace('SRC-', '')}",
        version_number=version_number,
        content_hash=actual_hash,
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        raw_bytes=raw_bytes,
        fetch_result=fetch_result,
        acquisition=acquisition,
    )

    if path.exists():
        _verify_existing_snapshot(path, raw_bytes, actual_hash, metadata)
        return path

    manifest_path = _manifest_path(path)
    if manifest_path.exists():
        raise RawSnapshotIntegrityError(
            f"orphan retrieval manifest {manifest_path} exists without its snapshot"
        )

    # Publish the manifest first, then the bytes. The DB row is only written
    # after this function returns. If the process dies between the two links,
    # the next run sees an orphan manifest and refuses rather than silently
    # reusing unverifiable evidence. Both links are no-clobber.
    _write_new_file(manifest_path, _manifest_bytes(metadata))
    try:
        _write_new_file(path, raw_bytes)
    except FileExistsError:
        # Lost a race with another writer: verify what they published.
        _verify_existing_snapshot(path, raw_bytes, actual_hash, metadata)
    except Exception:
        try:
            manifest_path.unlink()
        except OSError:
            pass
        raise
    return path


def read_raw_metadata(path: str | Path) -> dict[str, Any]:
    """Read back the retrieval manifest written alongside a raw snapshot."""
    return json.loads(_manifest_path(Path(path)).read_text(encoding="utf-8"))



def _extraction_model() -> str:
    """Resolve the extraction model.

    Honors EXTRACTION_MODEL from the environment and falls back to the default
    Cohere model when unset. A
    missing API key for the resolved provider will fail fast when the client is
    constructed — there is no silent fallback to a mock or to any other provider.
    """
    from llm_client import DEFAULT_EXTRACTION_MODEL
    return os.getenv("EXTRACTION_MODEL", DEFAULT_EXTRACTION_MODEL)

# Extraction modules rely on flat imports against src/extraction.
_EXTRACTION_DIR = Path(__file__).resolve().parent.parent / "extraction"
if str(_EXTRACTION_DIR) not in sys.path:
    sys.path.insert(0, str(_EXTRACTION_DIR))
from llm_client import CachingLLMClient, PROMPT_VERSION, get_client  # noqa: E402
from llm_governance import get_shared_governance  # noqa: E402
from requirement_extractor import extract_from_document  # noqa: E402
from requirement_repository import (  # noqa: E402
    complete_extraction_run,
    new_run_id,
    persist_requirement,
)


# A fetch that worked and a parse that found nothing is not a failure and not
# an ordinary success - calling it "OK" hides a source that is quietly
# contributing no knowledge. It gets its own honest value in
# government_sources.fetch_status, which the sources list and the API already
# surface, together with a machine-readable reason.
OK_EMPTY_CONTENT = "OK_EMPTY_CONTENT"
EMPTY_SCANNED_PDF = "SCANNED_PDF"
EMPTY_NO_STATIC_CONTENT = "NO_STATIC_CONTENT"


@dataclass
class FetchStoreOutcome:
    source_id: str
    status: str  # "OK" | "UNCHANGED" | a FetchStatus value when fetch failed
    document_id: str | None = None
    document_version: int | None = None
    content_hash: str | None = None
    # requirements_extracted counts attempts, not stored rows (item 6) - use
    # stored_new for "how much new knowledge did this fetch add".
    requirements_extracted: int = 0
    stored_new: int = 0
    error: str | None = None
    error_type: str | None = None  # machine-readable failure class
    demoted: str | None = None  # GovernmentSource.status set when the source was demoted
    # True when the fetch succeeded (real 200 OK, real content_hash) and the
    # content genuinely changed (INITIAL/CHANGED), but extraction produced zero
    # document chunks — e.g. a JS-rendered SPA shell with no static content.
    # Must never be indistinguishable from a real "nothing changed" success;
    # the caller (government_sync.sync_source) turns this into a visible
    # PARTIAL PipelineRun warning rather than a silent COMPLETED.
    empty_content: bool = False
    # Why nothing was extracted, when empty_content is True: SCANNED_PDF (a
    # PDF with no text layer - OCR is out of scope, so there is nothing to
    # read) or NO_STATIC_CONTENT (a page that rendered no structural content).
    # The two need different human actions, so they are not collapsed.
    empty_content_reason: str | None = None
    timing: SourceTiming | None = None


def _resolve_session(session_or_engine) -> Session:
    if isinstance(session_or_engine, Session):
        return session_or_engine
    return current_session(session_or_engine)


def _load_chunk_dicts(
    session: Session, document_id: str, version_id: int | None = None
) -> list[dict[str, Any]]:
    """Load a document's chunks for extraction.

    ``version_id`` scopes the load to exactly the document version being extracted.
    Scoping matters on versioned documents: evidence must link a chunk to the
    version that chunk actually belongs to (``fk_evidence_chunk_lineage``), so
    re-extraction after a content change must only ever pair new-version chunks
    with the new version.
    """
    q = session.query(DocumentChunk).filter(DocumentChunk.document_id == document_id)
    if version_id is not None:
        q = q.filter(DocumentChunk.version_id == version_id)
    rows = q.order_by(DocumentChunk.order_index).all()
    return [
        {
            "chunk_id": c.chunk_id,
            "chunk_type": c.chunk_type,
            "heading_path": c.heading_path,
            "order_index": c.order_index,
            "raw_text": c.raw_text,
            "page_number": c.page_number,
        }
        for c in rows
    ]


def _build_timing(
    source_id: str, acquisition: str, fetch_result, total_start: float,
    extract_ms: int | None = None, llm_calls: int = 0, llm_wait_ms: int = 0,
) -> SourceTiming:
    """Assemble one source's timing row from whatever the fetch path measured.

    ``fetch_result``'s stage fields are None for a stage that never ran (e.g.
    browser_launch_ms on a plain-HTTP result); those pass through as None here
    too rather than being coerced to 0.
    """
    return SourceTiming(
        source_id=source_id,
        method=acquisition,
        limiter_wait_ms=getattr(fetch_result, "limiter_wait_ms", None) or 0,
        http_ms=getattr(fetch_result, "http_ms", None),
        browser_launch_ms=getattr(fetch_result, "browser_launch_ms", None),
        goto_ms=getattr(fetch_result, "goto_ms", None),
        selector_wait_ms=getattr(fetch_result, "selector_wait_ms", None),
        selector_timed_out=getattr(fetch_result, "selector_timed_out", None),
        extract_ms=extract_ms,
        llm_calls=llm_calls,
        llm_wait_ms=llm_wait_ms,
        total_ms=int((time.monotonic() - total_start) * 1000),
    )


def _retrieval_event(fetch_result) -> dict[str, Any]:
    """FetchLog columns describing one acquisition attempt.

    A fresh retrieval id is generated per attempt; timings stay ``None`` when
    the stage did not run rather than being coerced to zero.
    """
    return {
        "retrieval_id": str(uuid.uuid4()),
        "acquisition": getattr(fetch_result, "acquisition", None),
        "final_url": getattr(fetch_result, "url", None),
        "content_type": getattr(fetch_result, "content_type", None),
        "limiter_wait_ms": getattr(fetch_result, "limiter_wait_ms", None),
        "browser_launch_ms": getattr(fetch_result, "browser_launch_ms", None),
        "goto_ms": getattr(fetch_result, "goto_ms", None),
        "selector_wait_ms": getattr(fetch_result, "selector_wait_ms", None),
        "selector_timed_out": getattr(fetch_result, "selector_timed_out", None),
    }


def _record_fetch_failure(
    session: Session, source: GovernmentSource, fetch_result, now: datetime,
    timing: SourceTiming | None = None,
) -> FetchStoreOutcome:
    """Record a failed retrieval without touching the source's registry status.

    A failed fetch must never remove a source from the sync set.  Demoting it
    to NEEDS_MANUAL_CAPTURE used to look like an escalation path, but
    sync_all_sources() selects on status == 'ACTIVE', so the demotion silently
    and permanently excluded the source from every future run — a dead end that
    required a manual capture that no production operator performs, and that
    wrote a non-dereferenceable ``manual:{document_id}`` raw_path.

    The precise failure classification already lives on the source's
    ``fetch_status``/``last_error``/``error_type`` columns and in the FetchLog
    row below, so the registry status stays ACTIVE and the source keeps being
    retried on its normal schedule.  Previously stored documents, versions and
    activated rules are left untouched — a failure adds no new knowledge, and
    removes none.
    """
    demoted: str | None = None
    source.last_checked_at = now
    source.fetch_status = fetch_result.status.value
    source.http_status = fetch_result.http_status
    source.last_error = fetch_result.error
    source.last_error_type = fetch_result.error_type
    source.consecutive_failures = (source.consecutive_failures or 0) + 1

    # A refusal (403/429) pauses the whole host, not just this source: the next
    # request would be refused too. Timeouts, DNS failures and 5xx are faults,
    # not refusals, and never pause anything. The pause is the *only* reaction —
    # we keep the same IP, the same User-Agent and the same fetch method.
    if is_refusal(fetch_result.http_status):
        start_cooldown(
            session, source.url, fetch_result.http_status,
            source_id=source.source_id, now=now,
        )

    session.add(
        FetchLog(
            source_id=source.source_id,
            fetched_at=now,
            http_status=fetch_result.http_status,
            status=fetch_result.status.value,
            content_hash=None,
            changed=0,
            duration_ms=fetch_result.duration_ms,
            error=fetch_result.error,
            error_type=fetch_result.error_type,
            **_retrieval_event(fetch_result),
        )
    )
    session.flush()
    logger.warning(
        "Source fetch failed",
        extra={
            "source_id": source.source_id,
            "fetch_status": fetch_result.status.value,
            "error_type": fetch_result.error_type,
            "http_status": fetch_result.http_status,
            "error": fetch_result.error,
        },
    )
    if timing is not None:
        logger.info("Source fetch timing", extra=timing.as_log_fields())
    return FetchStoreOutcome(
        source_id=source.source_id,
        status=fetch_result.status.value,
        error=fetch_result.error,
        error_type=fetch_result.error_type,
        demoted=demoted,
        timing=timing,
    )


def run_requirement_extraction(
    session_or_engine, source: GovernmentSource, document_id: str, version_number: int
) -> dict[str, Any]:
    """Run the Phase-2 requirement-extraction contract for a freshly stored document.

    Creates one ``extraction_runs`` row for the configured model (via
    ``EXTRACTION_MODEL``, defaulting to ``command-a-03-2025``), executes the requirement
    contract over the document's extracted chunks, and persists results through the legacy
    Phase-2 repository. A missing API key for the resolved provider will cause a hard
    failure; there is no silent fallback to a
    mock or to any other provider.
    Returns the counters dict (same shape as ``run_extraction.py``).
    """
    session = _resolve_session(session_or_engine)
    session.flush()
    doc = session.get(GovernmentDocument, document_id)
    if doc is None:
        return {"document_id": document_id, "status": "DOCUMENT_NOT_FOUND"}

    dv = (
        session.query(DocumentVersion)
        .filter(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == version_number,
        )
        .order_by(DocumentVersion.version_id.desc())
        .first()
    )
    if dv is None:
        return {"document_id": document_id, "status": "NO_VERSION"}

    chunks = _load_chunk_dicts(session, document_id, version_id=dv.version_id)
    if not chunks:
        return {"document_id": document_id, "status": "NO_CHUNKS"}

    conn = SqliteConn(session)

    # Record the extraction run via the ORM so counters defaults apply on every engine; the
    # legacy extraction repository is then reused unchanged for requirement persistence. The model
    # is configurable via EXTRACTION_MODEL (defaults to the Cohere command model).
    dv = (
        session.query(DocumentVersion)
        .filter(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == version_number,
        )
        .order_by(DocumentVersion.version_id.desc())
        .first()
    )
    if dv is None:
        return {"document_id": document_id, "status": "NO_VERSION"}

    model_name = _extraction_model()

    # Reconcile runs abandoned by a hard-killed process before starting another.
    # Observed live: a killed extraction left RUNNING rows that nothing ever
    # revisited, so the audit trail claimed extraction was still in flight for a
    # version that had long finished failing. A new run supersedes them.
    stale_runs = (
        session.query(ExtractionRun)
        .filter(
            ExtractionRun.document_id == document_id,
            ExtractionRun.status == "RUNNING",
        )
        .all()
    )
    for stale in stale_runs:
        logger.warning(
            "Marking stale RUNNING extraction run as ABANDONED",
            extra={"run_id": stale.run_id, "document_id": document_id},
        )
        stale.status = "FAILED"
        stale.error = "abandoned: superseded by a newer extraction run"
    if stale_runs:
        conn.commit()

    run = ExtractionRun(
        run_id=new_run_id(),
        document_id=document_id,
        document_version=version_number,
        document_version_id=dv.version_id,
        model=model_name,
        prompt_version=PROMPT_VERSION,
        status="RUNNING",
        dry_run=int(False),
    )
    session.add(run)
    session.flush()
    run_id = run.run_id
    conn.commit()
    source_ctx = {
        "source_id": source.source_id,
        "authority": source.authority,
        "program_id": doc.program_id,
        "visa_category": source.visa_category,
    }
    document_ctx = {
        "document_id": document_id,
        "version": version_number,
        "title": doc.title,
    }

    counters: dict[str, int] = {
        "chunks_seen": len(chunks),
        "candidate_chunks": 0,
        "llm_calls": 0,
        # requirements_extracted counts ATTEMPTS (every accepted/needs-review
        # candidate persist_requirement() was called for) - not stored rows.
        # stored_new/merged_into_existing (item 6) split that into what
        # actually happened: a brand new requirements row, or evidence merged
        # into one that already existed. Use stored_new for "how much new
        # knowledge did this run add", never requirements_extracted.
        "requirements_extracted": 0,
        "stored_new": 0,
        "merged_into_existing": 0,
        "validated": 0,
        "needs_review": 0,
        "rejected": 0,
        "evidence_failures": 0,
        "schema_failures": 0,
        "deferred_chunks": 0,
    }
    _NEW_ROW_ACTIONS = {"CREATED", "CONFLICT_RECORDED"}
    _MERGED_ACTIONS = {"EVIDENCE_ADDED", "COMPLEMENTARY_EVIDENCE_ADDED"}
    # Wall-clock spent per candidate between one on_outcome callback and the
    # next (each candidate is persisted as it completes - see _stream below -
    # so the gap between callbacks is dominated by that candidate's own LLM
    # call). Approximate, not instrumented inside the LLM client itself, but
    # honest: it is real elapsed time, not a guess.
    llm_timing = {"wait_ms": 0, "marker": time.monotonic()}
    persisted: list[dict[str, Any]] = []

    def persist_outcome(outcome) -> None:
        """Persist one candidate's requirements immediately.

        Invoked as each candidate completes (not after the whole loop) so a
        provider failure mid-document cannot discard completed work.
        """
        counters["candidate_chunks"] += 1
        if getattr(outcome, "deferred", False):
            # Provider quota exhausted on this one chunk - no LLM call
            # actually completed, nothing to persist. Counted separately so
            # it is never mistaken for "the model looked and found nothing".
            counters["deferred_chunks"] += 1
            return
        counters["llm_calls"] += 1
        if getattr(outcome, "schema_errors", None):
            counters["schema_failures"] += 1
            return
        for req in outcome.accepted_requirements:
            counters["requirements_extracted"] += 1
            counters["validated"] += 1
            result = persist_requirement(
                conn,
                program_id=doc.program_id,
                country_id=doc.country_id,
                requirement=req,
                document_id=document_id,
                document_version=version_number,
                extraction_run_id=run_id,
                dry_run=False,
            )
            if result.get("action") in _NEW_ROW_ACTIONS:
                counters["stored_new"] += 1
            elif result.get("action") in _MERGED_ACTIONS:
                counters["merged_into_existing"] += 1
            persisted.append(result)
        for item in outcome.needs_review_requirements:
            counters["requirements_extracted"] += 1
            counters["needs_review"] += 1
            req = dict(item["requirement"])
            result = persist_requirement(
                conn,
                program_id=doc.program_id,
                country_id=doc.country_id,
                requirement=req,
                document_id=document_id,
                document_version=version_number,
                extraction_run_id=run_id,
                dry_run=False,
            )
            if result.get("requirement_id") and result["action"] == "CREATED":
                conn.execute(
                    "UPDATE requirements SET status = 'NEEDS_REVIEW' WHERE requirement_id = ?",
                    (result["requirement_id"],),
                )
                conn.commit()
            if result.get("action") in _NEW_ROW_ACTIONS:
                counters["stored_new"] += 1
            elif result.get("action") in _MERGED_ACTIONS:
                counters["merged_into_existing"] += 1
            persisted.append({**result, "reasons": item["reasons"]})
        for item in outcome.rejected_requirements:
            counters["rejected"] += 1
            if any("evidence" in r.lower() or "fabricat" in r.lower() for r in item["reasons"]):
                counters["evidence_failures"] += 1

    # Persist each candidate's requirements as soon as it is produced, so a
    # provider failure part-way through a long document keeps everything already
    # extracted instead of discarding the whole run.
    def _stream(outcome) -> None:
        now = time.monotonic()
        llm_timing["wait_ms"] += int((now - llm_timing["marker"]) * 1000)
        llm_timing["marker"] = now
        persist_outcome(outcome)

    try:
        client = get_client(
            model_name, visa_category=source.visa_category,
            governance=get_shared_governance(),
        )
        # Item 5: a persistent, chunk-content-hash-keyed cache - an unchanged
        # chunk in a changed document never reaches the LLM again. Keyed by
        # (chunk_hash, prompt_version, model), never by chunk_id (a new
        # DocumentVersion always gets fresh chunk rows, even for
        # byte-identical text).
        client = CachingLLMClient(client, session, PROMPT_VERSION)
        extract_from_document(
            source=source_ctx,
            document=document_ctx,
            all_chunks=chunks,
            llm_client=client,
            context_window=1,
            on_outcome=_stream,
        )
    except Exception as exc:
        # Requirements extracted before the failure are already durable; record
        # them on the FAILED run so partial progress is visible rather than the
        # run looking as though it produced nothing.
        try:
            complete_extraction_run(conn, run_id, counters, status="FAILED", error=str(exc))
        except Exception:
            pass
        raise

    # PARTIAL (not COMPLETED) when any chunk was deferred by provider quota
    # exhaustion - a signal that this document needs a retry, not that the
    # model looked at every candidate chunk and found nothing.
    final_status = "PARTIAL" if counters["deferred_chunks"] else "COMPLETED"
    complete_extraction_run(conn, run_id, counters, status=final_status)
    counters["llm_wait_ms"] = llm_timing["wait_ms"]
    return {
        "run_id": run_id,
        "document_id": document_id,
        "document_version": version_number,
        "model": model_name,
        "status": final_status,
        **counters,
        "persisted": persisted,
    }


def fetch_document_and_store(
    session_or_engine, source: GovernmentSource, browser_session=None,
) -> FetchStoreOutcome:
    """Bootstrap a source: fetch (with browser escalation) → store document/version/chunks →
    run requirement extraction. Returns a ``FetchStoreOutcome`` describing what happened.

    On fetch/parse failure the source diagnostics are updated and a ``FetchLog`` row records
    the outcome; a source is never demoted out of the scheduled sync set.

    ``browser_session``: an optional retrieval session shared across a whole
    sync run (fetch.backend.new_session()), so one browser serves every source
    instead of each launching and tearing down its own. Each source still gets
    its own fresh context. Standalone callers (no session) get a private one
    for the single fetch.
    """
    session = _resolve_session(session_or_engine)
    now = datetime.now(timezone.utc)
    source_type = (source.source_type or "HTML").upper()
    total_start = time.monotonic()

    # One retrieval path (fetch/backend.py -> Playwright). There is no
    # "escalate after a refusal" step any more: a 403/429 records the host
    # cooldown and stops, and with a single mechanism there is structurally
    # nothing to escalate to.
    fetch_result = fetch_backend.fetch(
        source.url, source_type=source_type,
        etag=source.etag, last_modified=source.last_modified,
        session=browser_session,
    )
    acquisition = fetch_result.acquisition or "unknown"
    if not fetch_result.ok:
        timing = _build_timing(source.source_id, acquisition, fetch_result, total_start)
        return _record_fetch_failure(session, source, fetch_result, now, timing=timing)

    # A real response proves the host is reachable again, so drop any pause it
    # (or a sibling source on the same host) left behind. Self-healing: a pause
    # that has already expired is removed rather than left to linger until some
    # later success happens to touch the same host.
    clear_cooldown(session, source.url)

    if fetch_result.not_modified:
        # Item 5: the origin's 304 already told us nothing changed - no body
        # was ever downloaded, so there is nothing to hash, re-store or
        # re-extract. Only diagnostics and the validators refresh.
        source.last_checked_at = now
        source.last_successful_fetch_at = now
        source.status = "ACTIVE"
        source.fetch_status = "OK"
        source.http_status = fetch_result.http_status
        source.last_error = None
        source.last_error_type = None
        source.consecutive_failures = 0
        if fetch_result.etag:
            source.etag = fetch_result.etag
        if fetch_result.last_modified:
            source.last_modified = fetch_result.last_modified

        existing_doc = (
            session.query(GovernmentDocument)
            .filter(GovernmentDocument.source_id == source.source_id)
            .order_by(GovernmentDocument.retrieved_at.desc())
            .first()
        )
        session.add(
            FetchLog(
                source_id=source.source_id, fetched_at=now,
                http_status=fetch_result.http_status, status=fetcher.FetchStatus.OK.value,
                content_hash=source.last_hash, changed=0,
                duration_ms=fetch_result.duration_ms, error=None,
                **_retrieval_event(fetch_result),
            )
        )
        session.flush()
        timing = _build_timing(source.source_id, acquisition, fetch_result, total_start)
        logger.info("Source fetch timing", extra=timing.as_log_fields())
        return FetchStoreOutcome(
            source_id=source.source_id,
            status="UNCHANGED",
            document_id=existing_doc.document_id if existing_doc else None,
            document_version=existing_doc.current_version if existing_doc else None,
            content_hash=source.last_hash,
            timing=timing,
        )

    raw_bytes = fetch_result.raw_bytes
    content_hash = sha256_bytes(raw_bytes)

    needs_ocr = False
    extract_start = time.monotonic()
    if source_type == "PDF":
        parsed = _extract_pdf(raw_bytes)
        chunk_rows = parsed.chunks
        needs_ocr = bool(getattr(parsed, "needs_ocr", False))
        title = parsed.title or source.url
    elif (source.content_kind or "PAGE").upper() == "NEWS_LIST":
        # A dated listing: one chunk per announcement, so a change to one item
        # is traceable to that item instead of looking like a whole-page
        # rewrite. Falls back to the ordinary walk when no items are found.
        parsed = _extract_news_list(raw_bytes, source.url)
        chunk_rows = parsed.chunks
        title = parsed.title or source.url
    else:
        parsed = _extract_html(raw_bytes, source.url)
        chunk_rows = parsed.chunks
        title = parsed.title or source.url
    extract_ms = int((time.monotonic() - extract_start) * 1000)

    # Nothing extractable: honest about which of the two very different
    # reasons it was, because they need different human actions - a scanned
    # PDF needs OCR (out of scope) while a page with no static content needs
    # a look at the page itself.
    empty_content_reason = None
    if not chunk_rows:
        empty_content_reason = (
            EMPTY_SCANNED_PDF if needs_ocr else EMPTY_NO_STATIC_CONTENT
        )

    document_id = f"DOC-{source.source_id.replace('SRC-', '')}"
    existing = session.get(GovernmentDocument, document_id)

    if existing is None:
        change_type = "INITIAL"
        version_number = 1
    elif existing.content_hash == content_hash:
        change_type = "UNCHANGED"
        version_number = existing.current_version or 1
    else:
        change_type = "CHANGED"
        version_number = (existing.current_version or 0) + 1

    # Persist the real fetched bytes before any row references them, so every
    # raw_path written below dereferences to the actual original document.
    raw_path_str = str(
        persist_raw_snapshot(
            source,
            version_number,
            raw_bytes,
            document_id=document_id,
            content_hash=content_hash,
            retrieved_at=now,
            fetch_result=fetch_result,
            acquisition=acquisition,
        )
    )

    doc = existing
    if doc is None:
        doc = GovernmentDocument(
            document_id=document_id,
            source_id=source.source_id,
            country_id=source.country_id,
            program_id=source.program_id,
            visa_category=source.visa_category,
            document_type=source.source_subtype or "GENERAL",
            url=fetch_result.url,
            title=title,
            retrieved_at=now,
            content_hash=content_hash,
            current_version=version_number,
            language="en",
            raw_path=raw_path_str,
            status="NEEDS_OCR" if needs_ocr else "CURRENT",
        )
        session.add(doc)
    else:
        doc.content_hash = content_hash
        doc.retrieved_at = now
        doc.current_version = version_number
        doc.url = fetch_result.url
        doc.status = "NEEDS_OCR" if needs_ocr else "CURRENT"
        # Keep the document row pointing at the current version's snapshot.
        doc.raw_path = raw_path_str
        if doc.title is None:
            doc.title = title

    source.last_hash = content_hash
    source.last_checked_at = now
    source.last_successful_fetch_at = now
    source.status = "ACTIVE"
    source.fetch_status = OK_EMPTY_CONTENT if empty_content_reason else "OK"
    source.http_status = fetch_result.http_status
    source.last_error = None
    source.last_error_type = None
    source.consecutive_failures = 0
    # Store this fetch's validators so the next one can ask for a conditional
    # GET. Both Playwright mechanisms carry them honestly - a page navigation
    # reports the origin's ETag/Last-Modified, and the request API sends the
    # conditional headers itself - so there is no acquisition path left that
    # has to be excluded here. (This used to be gated on the retired plain-HTTP
    # path, which silently disabled conditional GET once fetching became
    # browser-only.)
    source.etag = fetch_result.etag
    source.last_modified = fetch_result.last_modified

    if change_type != "UNCHANGED":
        session.flush()  # ensure the document row is visible before the version FK is checked
        dv = DocumentVersion(
            document_id=document_id,
            version_number=version_number,
            content_hash=content_hash,
            retrieved_at=now,
            raw_path=raw_path_str,
            change_type=change_type,
        )
        session.add(dv)
        session.flush()
        for c in chunk_rows:
            session.add(
                DocumentChunk(
                    document_id=document_id,
                    version_id=dv.version_id,
                    chunk_type=c.chunk_type,
                    heading_path=c.heading_path,
                    order_index=c.order_index,
                    raw_text=c.raw_text,
                    page_number=getattr(c, "page_number", None),
                )
            )

    session.add(
        FetchLog(
            source_id=source.source_id,
            fetched_at=now,
            http_status=fetch_result.http_status,
            status=fetcher.FetchStatus.OK.value,
            content_hash=content_hash,
            changed=1 if change_type != "UNCHANGED" else 0,
            duration_ms=fetch_result.duration_ms,
            error=None,
            **_retrieval_event(fetch_result),
        )
    )
    session.flush()

    requirements_extracted = 0
    stored_new = 0
    llm_calls = 0
    llm_wait_ms = 0
    if change_type != "UNCHANGED" and chunk_rows and source.extract_requirements:
        report = run_requirement_extraction(session, source, document_id, version_number)
        requirements_extracted = report.get("requirements_extracted", 0)
        stored_new = report.get("stored_new", 0)
        llm_calls = report.get("llm_calls", 0)
        llm_wait_ms = report.get("llm_wait_ms", 0)
        logger.info(
            "Fetch + extraction complete",
            extra={
                "source_id": source.source_id,
                "document_id": document_id,
                "version": version_number,
                "requirements_extracted": requirements_extracted,
                "run_id": report.get("run_id"),
            },
        )
    elif change_type != "UNCHANGED" and chunk_rows and not source.extract_requirements:
        # Item 3: a reference-only source (e.g. SRC-AU-MIGRATION-REGS-1994 -
        # the whole Migration Regulations, mostly out of product scope) is
        # still fetched, hashed, diffed and stored as evidence above; only the
        # LLM extraction step - the expensive, scope-specific one - is
        # skipped. No rows are deleted; the document/version/chunks persist
        # exactly as they would for an extracting source.
        logger.info(
            "Fetch complete; requirement extraction skipped (extract_requirements=false)",
            extra={
                "source_id": source.source_id, "document_id": document_id,
                "version": version_number,
            },
        )

    timing = _build_timing(
        source.source_id, acquisition, fetch_result, total_start,
        extract_ms=extract_ms, llm_calls=llm_calls, llm_wait_ms=llm_wait_ms,
    )
    logger.info("Source fetch timing", extra=timing.as_log_fields())

    return FetchStoreOutcome(
        source_id=source.source_id,
        status="OK" if change_type != "UNCHANGED" else "UNCHANGED",
        document_id=document_id,
        document_version=version_number,
        content_hash=content_hash,
        requirements_extracted=requirements_extracted,
        stored_new=stored_new,
        empty_content=(change_type != "UNCHANGED" and not chunk_rows),
        empty_content_reason=empty_content_reason,
        timing=timing,
    )
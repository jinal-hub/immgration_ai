"""Phase 2 CLI entry point.

Usage:
    python src/extraction/run_extraction.py --document-id DOC-AU-189-MAIN
    python src/extraction/run_extraction.py --source-id SRC-AU-189-MAIN
    python src/extraction/run_extraction.py --document-id DOC-AU-189-MAIN --dry-run
    python src/extraction/run_extraction.py --document-id DOC-AU-189-MAIN --model gpt-4o --limit 5
    python src/extraction/run_extraction.py --review --status NEEDS_REVIEW
    python src/extraction/run_extraction.py --review --approve REQ-SKILLED_189-abcd1234 --reviewer alice
    python src/extraction/run_extraction.py --review --reject REQ-SKILLED_189-abcd1234 --reviewer alice --notes "..."

Without --model, resolves EXTRACTION_MODEL from the environment, falling back to
command-a-03-2025. Missing COHERE_API_KEY fails fast for the default model — there is no silent fallback to a mock
and fake requirement output is never manufactured.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import ROOT, init_schema  # noqa: E402
from llm_client import PROMPT_VERSION, DEFAULT_EXTRACTION_MODEL, get_client  # noqa: E402
from llm_governance import get_shared_governance  # noqa: E402
from requirement_extractor import extract_from_document  # noqa: E402
from requirement_repository import complete_extraction_run, create_extraction_run, persist_requirement  # noqa: E402
import review as review_mod  # noqa: E402

VALIDATION_ROOT = ROOT / "validation"


def _load_chunks_for_document(conn, document_id: str) -> tuple[dict | None, int, list[dict]]:
    doc = conn.execute("SELECT * FROM government_documents WHERE document_id = ?", (document_id,)).fetchone()
    if doc is None:
        return None, 0, []
    version_row = conn.execute(
        "SELECT version_id FROM document_versions WHERE document_id = ? AND version_number = ?",
        (document_id, doc["current_version"]),
    ).fetchone()
    if version_row is None:
        return dict(doc), doc["current_version"], []
    chunk_rows = conn.execute(
        "SELECT * FROM document_chunks WHERE document_id = ? AND version_id = ? ORDER BY order_index",
        (document_id, version_row["version_id"]),
    ).fetchall()
    chunks = [dict(c) for c in chunk_rows]
    return dict(doc), doc["current_version"], chunks


def run_for_document(conn, document_id: str, model: str, dry_run: bool, limit: int | None,
                       context_window: int = 1) -> dict:
    doc, version, chunks = _load_chunks_for_document(conn, document_id)
    if doc is None:
        return {"document_id": document_id, "status": "DOCUMENT_NOT_FOUND"}
    if not chunks:
        return {"document_id": document_id, "status": "NO_CHUNKS", "note": "run Phase 1 pipeline first"}

    source_row = conn.execute(
        "SELECT * FROM government_sources WHERE source_id = ?", (doc["source_id"],)
    ).fetchone()

    run_id = create_extraction_run(conn, document_id, version, model, PROMPT_VERSION, dry_run)

    # Deterministic prompt selection: program_id → visa_category → category-specific prompt.
    # The client loads prompts/<visa_category>/<PROMPT_VERSION>.md and falls back to the
    # generic prompt if the category prompt is unavailable. This guarantees a Student source
    # never uses the Skilled extraction contract.
    visa_category = source_row["visa_category"] if source_row else doc.get("visa_category")
    source_ctx = {"source_id": source_row["source_id"], "authority": source_row["authority"],
                  "program_id": doc["program_id"], "visa_category": visa_category}
    document_ctx = {"document_id": document_id, "version": version, "title": doc["title"]}

    try:
        client = get_client(model, visa_category=visa_category, governance=get_shared_governance())
        outcomes = extract_from_document(
            source=source_ctx, document=document_ctx, all_chunks=chunks, llm_client=client,
            context_window=context_window,
        )
    except Exception as exc:
        # Fail-safe: a real extraction that raises must never be left as a
        # dangling RUNNING run or silently downgraded to a mock/placeholder.
        try:
            complete_extraction_run(conn, run_id, {}, status="FAILED", error=str(exc))
        except Exception:
            pass
        raise
    if limit:
        outcomes = outcomes[:limit]

    deferred_count = sum(1 for o in outcomes if getattr(o, "deferred", False))
    counters = {
        "chunks_seen": len(chunks), "candidate_chunks": len(outcomes),
        "llm_calls": len(outcomes) - deferred_count,
        "requirements_extracted": 0, "validated": 0, "needs_review": 0, "rejected": 0,
        "evidence_failures": 0, "schema_failures": 0, "deferred_chunks": deferred_count,
    }
    persisted = []

    for outcome in outcomes:
        if getattr(outcome, "deferred", False):
            continue
        if outcome.schema_errors:
            counters["schema_failures"] += 1
            continue
        for req in outcome.accepted_requirements:
            counters["requirements_extracted"] += 1
            counters["validated"] += 1
            result = persist_requirement(
                conn, program_id=doc["program_id"], country_id=doc["country_id"], requirement=req,
                document_id=document_id, document_version=version, extraction_run_id=run_id,
                dry_run=dry_run,
            )
            persisted.append(result)
        for item in outcome.needs_review_requirements:
            counters["requirements_extracted"] += 1
            counters["needs_review"] += 1
            req = dict(item["requirement"])
            result = persist_requirement(
                conn, program_id=doc["program_id"], country_id=doc["country_id"], requirement=req,
                document_id=document_id, document_version=version, extraction_run_id=run_id,
                dry_run=dry_run,
            )
            if not dry_run and result.get("requirement_id") and result["action"] == "CREATED":
                conn.execute(
                    "UPDATE requirements SET status = 'NEEDS_REVIEW' WHERE requirement_id = ?",
                    (result["requirement_id"],),
                )
                conn.commit()
            persisted.append({**result, "reasons": item["reasons"]})
        for item in outcome.rejected_requirements:
            counters["rejected"] += 1
            if any("evidence" in r.lower() or "fabricat" in r.lower() for r in item["reasons"]):
                counters["evidence_failures"] += 1

    final_status = "DRY_RUN" if dry_run else ("PARTIAL" if deferred_count else "COMPLETED")
    complete_extraction_run(conn, run_id, counters, status=final_status)

    return {
        "document_id": document_id, "document_version": version, "run_id": run_id,
        "model": model, "dry_run": dry_run, "status": final_status, **counters,
        "persisted": persisted,
    }


def run_for_source(conn, source_id: str, **kwargs) -> dict:
    doc_row = conn.execute(
        "SELECT document_id FROM government_documents WHERE source_id = ?", (source_id,)
    ).fetchone()
    if doc_row is None:
        return {"source_id": source_id, "status": "NO_DOCUMENT",
                "note": "no government_documents row for this source - run Phase 1 pipeline first"}
    return run_for_document(conn, doc_row["document_id"], **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 requirement extraction CLI.")
    parser.add_argument("--document-id")
    parser.add_argument("--source-id")
    parser.add_argument("--model", default=None,
                        help="Extraction model (default: EXTRACTION_MODEL or command-a-03-2025).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--context-window", type=int, default=1)

    parser.add_argument("--review", action="store_true", help="Enter review mode instead of extraction.")
    parser.add_argument("--status", default="NEEDS_REVIEW", help="Review mode: list requirements with this status.")
    parser.add_argument("--approve")
    parser.add_argument("--reject")
    parser.add_argument("--mark-needs-review", dest="mark_needs_review")
    parser.add_argument("--reviewer", default="cli-user")
    parser.add_argument("--notes")

    args = parser.parse_args()
    conn = init_schema()

    if args.review:
        if args.approve:
            review_mod.approve(conn, args.approve, args.reviewer, args.notes)
            print(f"APPROVED {args.approve} by {args.reviewer}")
        elif args.reject:
            review_mod.reject(conn, args.reject, args.reviewer, args.notes)
            print(f"REJECTED {args.reject} by {args.reviewer}")
        elif args.mark_needs_review:
            review_mod.mark_needs_review(conn, args.mark_needs_review, args.reviewer, args.notes)
            print(f"MARKED NEEDS_REVIEW {args.mark_needs_review} by {args.reviewer}")
        else:
            items = review_mod.list_for_review(conn, status=args.status)
            print(json.dumps(items, indent=2, default=str))
        conn.close()
        return

    if not args.document_id and not args.source_id:
        parser.error("provide --document-id or --source-id (or --review)")

    model = args.model or os.getenv("EXTRACTION_MODEL", DEFAULT_EXTRACTION_MODEL)
    from llm_client import MockLLMClient
    if model == MockLLMClient.model_name:
        parser.error(
            "mock-llm-v1 is a test-only double and must not be used in production extraction. "
            "Set EXTRACTION_MODEL to a supported model (e.g. command-a-03-2025) or pass --model."
        )

    kwargs = dict(model=model, dry_run=args.dry_run, limit=args.limit,
                   context_window=args.context_window)
    if args.document_id:
        report = run_for_document(conn, args.document_id, **kwargs)
    else:
        report = run_for_source(conn, args.source_id, **kwargs)

    VALIDATION_ROOT.mkdir(parents=True, exist_ok=True)
    report_path = VALIDATION_ROOT / "phase2_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "persisted"}, indent=2, default=str))
    print(f"[extraction] report written to {report_path}")
    conn.close()


if __name__ == "__main__":
    main()

"""Import curated gold annotations as reviewable DRAFT rules.

Gold examples are evaluation data, not live approvals. This command preserves
their own captured chunk snapshot and exact evidence lineage, excludes negative
examples, and never activates a rule.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from db.connection import get_engine, get_session
from db.models import (
    DocumentChunk,
    DocumentVersion,
    ExtractionRun,
    GovernmentDocument,
    GovernmentSource,
    Requirement,
    RequirementEvidence,
    RequirementVersion,
)
from rules.rule_compiler import compile_requirement_to_rule
from rules.rule_repository import EnhancedRuleRepo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLD_FILE = ROOT / "data" / "gold" / "skilled" / "skilled_189_real.json"
DEFAULT_CHUNKS_FILE = (
    ROOT / "data" / "real_validation" / "skilled_189_real_chunks.json"
)

GOLD_DATASETS = {
    "SKILLED_189": {
        "product_code": "189-63",
        "source_id": "SRC-AU-189-MAIN",
        "document_id": "DOC-GOLD-189-REAL",
        "extraction_run_id": "GOLD189-CURATED",
        "snapshot_name": "skilled_189_real",
        "subclass": "189",
    },
    "SKILLED_190": {
        "product_code": "190",
        "source_id": "SRC-AU-190-MAIN",
        "document_id": "DOC-GOLD-190-REAL",
        "extraction_run_id": "GOLD190-CURATED",
        "snapshot_name": "skilled_190_real",
        "subclass": "190",
    },
}
GOLD_CREATOR = "gold-curation-import"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
    ).encode("utf-8")


def _validate_dataset(
    gold: dict[str, Any], chunk_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], dict[str, str]]:
    program_id = gold.get("program_id")
    dataset = GOLD_DATASETS.get(program_id)
    if dataset is None:
        raise ValueError(f"Unsupported gold dataset program: {program_id!r}")
    if chunk_data.get("product_code") != dataset["product_code"]:
        raise ValueError(
            f"Chunk snapshot is not the expected subclass "
            f"{dataset['subclass']} capture"
        )

    chunks = {int(chunk["chunk_id"]): chunk for chunk in chunk_data.get("chunks", [])}
    positive = [
        item for item in gold.get("items", [])
        if item.get("expected") is not None
    ]
    if not positive:
        raise ValueError("Gold dataset contains no positive rule annotations")

    seen_ids: set[str] = set()
    for item in positive:
        gold_id = item.get("gold_id")
        if not gold_id or gold_id in seen_ids:
            raise ValueError(f"Missing or duplicate gold_id: {gold_id!r}")
        seen_ids.add(gold_id)
        expected = item["expected"]
        if not expected.get("field") or not expected.get("operator"):
            raise ValueError(f"{gold_id} is missing its expected field/operator")
        chunk_id = item.get("chunk_id")
        chunk = chunks.get(int(chunk_id)) if chunk_id is not None else None
        evidence = item.get("evidence_text", "").strip()
        if chunk is None or not evidence or evidence not in chunk.get("raw_text", ""):
            raise ValueError(
                f"{gold_id} evidence is not an exact substring of its declared "
                f"gold chunk ({chunk_id!r})"
            )
    return positive, chunks, dataset


def import_gold_rules(
    session: Session,
    gold: dict[str, Any],
    chunk_data: dict[str, Any],
    raw_root: Path,
) -> dict[str, int]:
    """Persist positive gold annotations as DRAFT rules, idempotently."""
    positive, chunks, dataset = _validate_dataset(gold, chunk_data)
    source_id = dataset["source_id"]
    document_id = dataset["document_id"]
    extraction_run_id = dataset["extraction_run_id"]
    source = session.get(GovernmentSource, source_id)
    if source is None:
        raise ValueError(f"Required source {source_id} is not registered")
    if source.program_id != gold["program_id"] or source.country_id != gold["country_id"]:
        raise ValueError("Gold dataset program/country does not match the source")

    chunk_bytes = _json_bytes(chunk_data)
    gold_bytes = _json_bytes(gold)
    content_hash = hashlib.sha256(chunk_bytes).hexdigest()
    retrieved_at = datetime.fromisoformat(
        chunk_data.get("extracted_at", gold["created_at"])
    ).replace(tzinfo=timezone.utc)

    artifact_dir = raw_root / "gold" / dataset["snapshot_name"]
    artifact_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = artifact_dir / "captured_chunks.json"
    gold_path = artifact_dir / "curated_annotations.json"
    if not chunk_path.exists():
        chunk_path.write_bytes(chunk_bytes)
    elif hashlib.sha256(chunk_path.read_bytes()).hexdigest() != content_hash:
        raise ValueError(f"Existing gold snapshot differs: {chunk_path}")
    if not gold_path.exists():
        gold_path.write_bytes(gold_bytes)
    elif gold_path.read_bytes() != gold_bytes:
        raise ValueError(f"Existing gold annotations differ: {gold_path}")

    document = session.get(GovernmentDocument, document_id)
    if document is None:
        document = GovernmentDocument(
            document_id=document_id,
            source_id=source_id,
            country_id=source.country_id,
            program_id=source.program_id,
            visa_category=source.visa_category,
            document_type="GOLD_CURATED",
            url=source.url,
            title=f"Curated gold snapshot — subclass {dataset['subclass']}",
            retrieved_at=retrieved_at,
            content_hash=content_hash,
            current_version=1,
            raw_path=str(chunk_path),
            extracted_path=str(gold_path),
            status="CURRENT",
        )
        session.add(document)
        session.flush()
    elif (
        document.source_id != source_id
        or document.program_id != gold["program_id"]
        or document.content_hash != content_hash
    ):
        raise ValueError(f"Existing {document_id} does not match this gold snapshot")

    version = (
        session.query(DocumentVersion)
        .filter_by(document_id=document_id, version_number=1)
        .one_or_none()
    )
    if version is None:
        version = DocumentVersion(
            document_id=document_id,
            version_number=1,
            content_hash=content_hash,
            retrieved_at=retrieved_at,
            raw_path=str(chunk_path),
            extracted_path=str(gold_path),
            change_type="MANUAL_CAPTURE",
            diff_summary="Curated evaluation snapshot; not a current live fetch.",
        )
        session.add(version)
        session.flush()
    elif version.content_hash != content_hash:
        raise ValueError("Existing gold document version has a different content hash")

    database_chunks = (
        session.query(DocumentChunk)
        .filter_by(document_id=document_id, version_id=version.version_id)
        .all()
    )
    by_order = {chunk.order_index: chunk for chunk in database_chunks}
    chunk_id_map: dict[int, int] = {}
    for gold_chunk_id, chunk in chunks.items():
        order_index = int(chunk.get("order_index", gold_chunk_id))
        stored = by_order.get(order_index)
        if stored is None:
            stored = DocumentChunk(
                document_id=document_id,
                version_id=version.version_id,
                chunk_type=chunk.get("chunk_type", "PARAGRAPH"),
                heading_path=chunk.get("heading_path"),
                order_index=order_index,
                raw_text=chunk["raw_text"],
                page_number=chunk.get("page_number"),
            )
            session.add(stored)
            session.flush()
            by_order[order_index] = stored
        elif stored.raw_text != chunk["raw_text"]:
            raise ValueError(f"Stored gold chunk {order_index} does not match snapshot")
        chunk_id_map[gold_chunk_id] = stored.chunk_id

    run = session.get(ExtractionRun, extraction_run_id)
    if run is None:
        run = ExtractionRun(
            run_id=extraction_run_id,
            document_id=document_id,
            document_version=1,
            document_version_id=version.version_id,
            model="manual-gold-curation",
            prompt_version="gold-json-import-v1",
            chunks_seen=len(chunks),
            candidate_chunks=len(positive),
            llm_calls=0,
            requirements_extracted=len(positive),
            stored_new=len(positive),
            needs_review=len(positive),
            status="COMPLETED",
            dry_run=0,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(run)
        session.flush()
    elif (
        run.document_id != document_id
        or run.document_version_id != version.version_id
        or run.model != "manual-gold-curation"
    ):
        raise ValueError("Existing gold extraction record has unexpected lineage")

    created_requirements = 0
    created_rules = 0
    rule_repo = EnhancedRuleRepo(session)

    for item in positive:
        gold_id = item["gold_id"]
        requirement_id = f"GOLD-{gold_id}"
        rule_id = f"RULE-GOLD-{gold_id}"
        expected = item["expected"]
        db_chunk_id = chunk_id_map[int(item["chunk_id"])]
        requirement = session.get(Requirement, requirement_id)

        if requirement is None:
            requirement = Requirement(
                requirement_id=requirement_id,
                semantic_key=f"gold:{gold_id}",
                program_id=gold["program_id"],
                country_id=gold["country_id"],
                document_version_id=version.version_id,
                category=item["category"],
                name=item["requirement_text"][:256],
                description=item["requirement_text"],
                field=expected["field"],
                operator=expected["operator"],
                value_json=json.dumps(expected.get("value"), ensure_ascii=False),
                unit=expected.get("unit"),
                mandatory=int(bool(expected.get("mandatory", False))),
                content_kind=item["content_kind"],
                criticality=item.get("criticality", "NORMAL"),
                status="NEEDS_REVIEW",
                current_version=1,
                extraction_run_id=run.run_id,
            )
            session.add(requirement)
            session.flush()
            created_requirements += 1
        elif (
            requirement.extraction_run_id != run.run_id
            or requirement.document_version_id != version.version_id
        ):
            raise ValueError(f"Existing requirement {requirement_id} has unexpected lineage")

        req_version = (
            session.query(RequirementVersion)
            .filter_by(requirement_id=requirement_id, version_number=1)
            .one_or_none()
        )
        if req_version is None:
            snapshot = {
                "gold_id": gold_id,
                "content_kind": item["content_kind"],
                "category": item["category"],
                "requirement_text": item["requirement_text"],
                "expected": expected,
                "evidence_text": item["evidence_text"],
            }
            req_version = RequirementVersion(
                requirement_id=requirement_id,
                version_number=1,
                snapshot_json=json.dumps(snapshot, ensure_ascii=False),
                document_version=1,
                document_version_id=version.version_id,
                change_type="INITIAL",
                created_by=GOLD_CREATOR,
            )
            session.add(req_version)
            session.flush()

        evidence = (
            session.query(RequirementEvidence)
            .filter_by(requirement_id=requirement_id, chunk_id=db_chunk_id)
            .one_or_none()
        )
        if evidence is None:
            session.add(RequirementEvidence(
                requirement_id=requirement_id,
                document_id=document_id,
                document_version=1,
                document_version_id=version.version_id,
                chunk_id=db_chunk_id,
                evidence_text=item["evidence_text"],
                evidence_type="DIRECT_TEXT",
                verified=1,
                verification_note="Exact substring verified against curated gold chunk.",
                extraction_method="GOLD_CURATION",
                extracted_at=retrieved_at,
            ))
            session.flush()

        if rule_repo.get(rule_id) is None:
            compiled = compile_requirement_to_rule(
                {
                    "_requirement_id": requirement_id,
                    "_program_id": gold["program_id"],
                    "_requirement_version_id": req_version.version_id,
                    "_source_id": source_id,
                    "_document_id": document_id,
                    "_document_version": 1,
                    "category": item["category"],
                    "name": item["requirement_text"],
                    "description": item["requirement_text"],
                    "field": expected["field"],
                    "operator": expected["operator"],
                    "value": expected.get("value"),
                    "unit": expected.get("unit"),
                    "mandatory": expected.get("mandatory", False),
                    "criticality": item.get("criticality", "NORMAL"),
                    "evidence": [{
                        "chunk_id": db_chunk_id,
                        "evidence_text": item["evidence_text"],
                    }],
                },
                rule_id,
            )
            rule_repo.upsert_from_compiled(rule_id, compiled, requirement_id)
            rule_repo.save_version_snapshot(rule_id, "CREATED", GOLD_CREATOR)
            created_rules += 1

    return {
        "positive_gold_annotations": len(positive),
        "excluded_negative_examples": len(gold.get("items", [])) - len(positive),
        "requirements_created": created_requirements,
        "draft_rules_created": created_rules,
        "draft_rules_total": len(positive),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-file", type=Path, default=DEFAULT_GOLD_FILE)
    parser.add_argument("--chunks-file", type=Path, default=DEFAULT_CHUNKS_FILE)
    args = parser.parse_args()
    gold = json.loads(args.gold_file.read_text(encoding="utf-8"))
    chunk_data = json.loads(args.chunks_file.read_text(encoding="utf-8"))
    raw_root = Path(os.getenv("RAW_SNAPSHOT_DIR", ROOT / "raw"))
    engine = get_engine()
    with get_session(engine) as session:
        result = import_gold_rules(session, gold, chunk_data, raw_root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

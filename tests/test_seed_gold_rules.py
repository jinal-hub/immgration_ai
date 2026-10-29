"""Gold annotations import as reviewable rules with verified provenance."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db.models import (  # noqa: E402
    Base,
    Country,
    GovernmentSource,
    ImmigrationProgram,
    Requirement,
    RequirementEvidence,
    Rule,
)
from seed_gold_rules import import_gold_rules  # noqa: E402


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        db.add(Country(country_id="AU", name="Australia", iso_code="AUS"))
        db.add(ImmigrationProgram(
            program_id="SKILLED_189",
            country_id="AU",
            visa_category="SKILLED",
            subclass_code="189",
            name="Skilled Independent",
            status="ACTIVE",
        ))
        db.add(ImmigrationProgram(
            program_id="SKILLED_190",
            country_id="AU",
            visa_category="SKILLED",
            subclass_code="190",
            name="Skilled Nominated",
            status="ACTIVE",
        ))
        db.add(GovernmentSource(
            source_id="SRC-AU-189-MAIN",
            country_id="AU",
            program_id="SKILLED_189",
            visa_category="SKILLED",
            authority="Home Affairs",
            url="https://immi.homeaffairs.gov.au/visas/189",
            source_type="HTML",
            priority="PRIMARY",
            monitoring_method="WEB",
            crawl_frequency="DAILY",
            status="ACTIVE",
        ))
        db.add(GovernmentSource(
            source_id="SRC-AU-190-MAIN",
            country_id="AU",
            program_id="SKILLED_190",
            visa_category="SKILLED",
            authority="Home Affairs",
            url="https://immi.homeaffairs.gov.au/visas/190",
            source_type="HTML",
            priority="PRIMARY",
            monitoring_method="WEB",
            crawl_frequency="DAILY",
            status="ACTIVE",
        ))
        db.commit()
        yield db
    engine.dispose()


def _gold_data(subclass="189"):
    if subclass == "190":
        gold_path = ROOT / "data/gold/skilled/skilled_190_real.json"
        chunks_path = ROOT / "data/real_validation/skilled_190_real_chunks.json"
    else:
        gold_path = ROOT / "data/gold/skilled/skilled_189_real.json"
        chunks_path = ROOT / "data/real_validation/skilled_189_real_chunks.json"
    gold = json.loads(
        gold_path.read_text(encoding="utf-8")
    )
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    return gold, chunks


def test_imports_all_positive_gold_as_draft_rules_and_is_idempotent(session, tmp_path):
    gold, chunks = _gold_data()

    result = import_gold_rules(session, gold, chunks, tmp_path)

    assert result == {
        "positive_gold_annotations": 17,
        "excluded_negative_examples": 6,
        "requirements_created": 17,
        "draft_rules_created": 17,
        "draft_rules_total": 17,
    }
    rules = session.query(Rule).filter(Rule.rule_id.like("RULE-GOLD-%")).all()
    assert len(rules) == 17
    assert {rule.status for rule in rules} == {"DRAFT"}
    assert session.query(Requirement).filter(
        Requirement.requirement_id.like("GOLD-%")
    ).count() == 17
    assert session.query(RequirementEvidence).filter(
        RequirementEvidence.requirement_id.like("GOLD-%")
    ).count() == 17

    rules[0].status = "VALIDATED"
    session.flush()
    rerun = import_gold_rules(session, gold, chunks, tmp_path)
    assert rerun["requirements_created"] == 0
    assert rerun["draft_rules_created"] == 0
    assert session.get(Rule, rules[0].rule_id).status == "VALIDATED"


def test_import_rejects_non_verbatim_gold_evidence_before_writing(session, tmp_path):
    gold, chunks = _gold_data()
    positive = next(item for item in gold["items"] if item.get("expected") is not None)
    positive["evidence_text"] = "Text that is not in the gold source chunk"

    with pytest.raises(ValueError, match="not an exact substring"):
        import_gold_rules(session, gold, chunks, tmp_path)

    assert session.query(Rule).count() == 0
    assert session.query(Requirement).count() == 0
    assert not list(tmp_path.rglob("*.json"))


def test_imports_190_real_gold_as_draft_rules_and_is_idempotent(session, tmp_path):
    gold, chunks = _gold_data("190")

    result = import_gold_rules(session, gold, chunks, tmp_path)

    assert result == {
        "positive_gold_annotations": 5,
        "excluded_negative_examples": 1,
        "requirements_created": 5,
        "draft_rules_created": 5,
        "draft_rules_total": 5,
    }
    rules = session.query(Rule).filter(Rule.rule_id.like("RULE-GOLD-REAL-190-%")).all()
    assert len(rules) == 5
    assert {rule.program_id for rule in rules} == {"SKILLED_190"}
    assert {rule.status for rule in rules} == {"DRAFT"}

    rerun = import_gold_rules(session, gold, chunks, tmp_path)
    assert rerun["requirements_created"] == 0
    assert rerun["draft_rules_created"] == 0

"""Tests for Phase 3A rule engine."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rules.models import Rule, Provenance, EvaluationStatus
from rules.rule_engine import evaluate_predicate, evaluate_rule, evaluate_all_rules


def _make_rule(**overrides):
    prov = Provenance(
        source_id="SRC-AU-189-MAIN",
        document_id="DOC-AU-189-MAIN",
        document_version=1,
        chunk_id=3,
        requirement_id="REQ-189-AGE-001",
        evidence_text="You must be aged under 45",
    )
    defaults = dict(
        rule_id="RULE-189-AGE-001",
        requirement_id="REQ-189-AGE-001",
        program_id="SKILLED_189",
        category="AGE",
        field="applicant.age",
        operator="LT",
        value=45,
        unit="years",
        mandatory=True,
        critical=True,
        message="Applicant must be under 45",
        provenance=prov,
    )
    defaults.update(overrides)
    return Rule(**defaults)


# --- PASS tests ---

def test_pass_basic():
    rule = _make_rule()
    result = evaluate_rule(rule, {"applicant.age": 30})
    assert result.status == EvaluationStatus.PASS


def test_pass_boundary():
    rule = _make_rule()
    result = evaluate_rule(rule, {"applicant.age": 44})
    assert result.status == EvaluationStatus.PASS


def test_pass_gte():
    rule = _make_rule(operator="GTE", value=65, field="applicant.points_score")
    result = evaluate_rule(rule, {"applicant.points_score": 70})
    assert result.status == EvaluationStatus.PASS


def test_pass_in():
    rule = _make_rule(operator="IN", value=["substantive", "BVA"], field="applicant.visa_status")
    result = evaluate_rule(rule, {"applicant.visa_status": "substantive"})
    assert result.status == EvaluationStatus.PASS


# --- FAIL tests ---

def test_fail_basic():
    rule = _make_rule()
    result = evaluate_rule(rule, {"applicant.age": 50})
    assert result.status == EvaluationStatus.FAIL


def test_fail_boundary():
    rule = _make_rule()
    result = evaluate_rule(rule, {"applicant.age": 45})
    assert result.status == EvaluationStatus.FAIL


def test_fail_gte():
    rule = _make_rule(operator="GTE", value=65, field="applicant.points_score")
    result = evaluate_rule(rule, {"applicant.points_score": 60})
    assert result.status == EvaluationStatus.FAIL


def test_fail_in():
    rule = _make_rule(operator="IN", value=["substantive", "BVA"], field="applicant.visa_status")
    result = evaluate_rule(rule, {"applicant.visa_status": "tourist"})
    assert result.status == EvaluationStatus.FAIL


# --- UNKNOWN tests ---

def test_unknown_missing_field():
    rule = _make_rule()
    result = evaluate_rule(rule, {})
    assert result.status == EvaluationStatus.UNKNOWN


def test_unknown_none_value():
    rule = _make_rule()
    result = evaluate_rule(rule, {"applicant.age": None})
    assert result.status == EvaluationStatus.UNKNOWN


def test_unknown_reason():
    rule = _make_rule()
    result = evaluate_rule(rule, {})
    assert "not provided" in result.reason


def test_structured_condition_passes_before_base_rule():
    rule = _make_rule(
        condition={"field": "visa_subclass", "operator": "EQ", "value": "189"},
    )
    assert evaluate_rule(
        rule, {"age": 30, "visa_subclass": "189"},
    ).status == EvaluationStatus.PASS


def test_structured_condition_not_met_makes_rule_not_applicable():
    rule = _make_rule(
        condition={"if": "visa_subclass", "equals": "190"},
    )
    assert evaluate_rule(
        rule, {"age": 50, "visa_subclass": "189"},
    ).status == EvaluationStatus.NOT_APPLICABLE


def test_unknown_condition_does_not_execute_free_text():
    rule = _make_rule(condition={"description": "unless exceptional circumstances"})
    assert evaluate_rule(rule, {"age": 30}).status == EvaluationStatus.UNKNOWN


def test_predicate_composes_and_or_not_without_eval():
    data = {"visa_subclass": "189", "age": 30}
    predicate = {
        "and": [
            {"field": "visa_subclass", "operator": "IN", "value": ["189", "190"]},
            {"not": {"field": "age", "operator": "GTE", "value": 45}},
        ],
    }
    assert evaluate_predicate(predicate, data) == EvaluationStatus.PASS


def test_malformed_predicate_is_unknown_not_a_false_legal_finding():
    assert evaluate_predicate(
        {"field": "age", "operator": "IN", "value": 45},
        {"age": 30},
    ) == EvaluationStatus.UNKNOWN
    assert evaluate_predicate(
        {"field": "age", "operator": "EVAL", "value": "anything"},
        {"age": 30},
    ) == EvaluationStatus.UNKNOWN


def test_exception_waives_failed_base_rule():
    rule = _make_rule(
        exception={"field": "visa_subclass", "operator": "EQ", "value": "190"},
    )
    result = evaluate_rule(rule, {"age": 50, "visa_subclass": "190"})
    assert result.status == EvaluationStatus.NOT_APPLICABLE


def test_unknown_exception_keeps_failed_base_rule_incomplete():
    rule = _make_rule(exception={"field": "visa_subclass", "operator": "EQ", "value": "190"})
    result = evaluate_rule(rule, {"age": 50})
    assert result.status == EvaluationStatus.UNKNOWN


# --- EXISTS / NOT_EXISTS ---

def test_exists_pass():
    rule = _make_rule(operator="EXISTS", value=None)
    result = evaluate_rule(rule, {"applicant.age": 30})
    assert result.status == EvaluationStatus.PASS


def test_exists_fail():
    rule = _make_rule(operator="EXISTS", value=None)
    result = evaluate_rule(rule, {})
    assert result.status == EvaluationStatus.FAIL


def test_not_exists_pass():
    rule = _make_rule(operator="NOT_EXISTS", value=None)
    result = evaluate_rule(rule, {})
    assert result.status == EvaluationStatus.PASS


def test_not_exists_fail():
    rule = _make_rule(operator="NOT_EXISTS", value=None)
    result = evaluate_rule(rule, {"applicant.age": 30})
    assert result.status == EvaluationStatus.FAIL


# --- evaluate_all_rules ---

def test_all_pass():
    rules = [
        _make_rule(rule_id="R1", field="f1", operator="LT", value=45),
        _make_rule(rule_id="R2", field="f2", operator="GTE", value=65),
    ]
    data = {"f1": 30, "f2": 70}
    result = evaluate_all_rules(rules, data, "SKILLED_189")
    assert result.overall_status == EvaluationStatus.PASS
    assert result.passed == 2
    assert result.failed == 0
    assert result.unknown == 0


def test_one_fail():
    rules = [
        _make_rule(rule_id="R1", field="f1", operator="LT", value=45),
        _make_rule(rule_id="R2", field="f2", operator="GTE", value=65),
    ]
    data = {"f1": 50, "f2": 70}
    result = evaluate_all_rules(rules, data, "SKILLED_189")
    assert result.overall_status == EvaluationStatus.FAIL
    assert result.failed == 1


def test_unknown_no_fail():
    rules = [
        _make_rule(rule_id="R1", field="f1", operator="LT", value=45),
        _make_rule(rule_id="R2", field="f2", operator="GTE", value=65),
    ]
    data = {"f1": 30}
    result = evaluate_all_rules(rules, data, "SKILLED_189")
    assert result.overall_status == EvaluationStatus.UNKNOWN
    assert result.unknown == 1
    assert result.passed == 1


def test_fail_takes_precedence_over_unknown():
    rules = [
        _make_rule(rule_id="R1", field="f1", operator="LT", value=45),
        _make_rule(rule_id="R2", field="f2", operator="GTE", value=65),
    ]
    data = {"f1": 50}
    result = evaluate_all_rules(rules, data, "SKILLED_189")
    assert result.overall_status == EvaluationStatus.FAIL
    assert result.failed == 1
    assert result.unknown == 1


# ---------------------------------------------------------------------------
# Phase 2 extraction contract -> Phase 3C ApplicantProfile.to_rule_data()
# integration seam. Requirement fields come from prompts/requirement_extraction_v2.md
# (dotted "applicant.xxx" names); real applicant data comes from
# ApplicantProfile.to_rule_data() (flat canonical names). Found via the
# tests/e2e/test_full_knowledge_to_assessment.py PostgreSQL E2E run, which failed
# with every rule UNKNOWN despite matching applicant data — every one of these
# would previously have returned UNKNOWN regardless of what the applicant provided.
# ---------------------------------------------------------------------------

def test_resolves_applicant_prefixed_field_against_bare_profile_key():
    """rule.field="applicant.age" must resolve against to_rule_data()'s bare "age"."""
    rule = _make_rule(field="applicant.age", operator="LT", value=45)
    result = evaluate_rule(rule, {"age": 30})  # ApplicantProfile.to_rule_data() shape
    assert result.status == EvaluationStatus.PASS
    assert result.actual_value == 30


def test_resolves_points_score_alias_to_points():
    rule = _make_rule(field="applicant.points_score", operator="GTE", value=65)
    result = evaluate_rule(rule, {"points": 80})
    assert result.status == EvaluationStatus.PASS


def test_resolves_required_points_alias_to_points():
    rule = _make_rule(field="applicant.required_points", operator="GTE", value=65)
    result = evaluate_rule(rule, {"points": 70})
    assert result.status == EvaluationStatus.PASS


def test_resolves_skills_assessment_alias():
    # REQUIRED is translated to EQ/True by compile_requirement_to_rule() at compile
    # time (tests/rules/test_rule_compiler.py) — a Rule never carries "REQUIRED" here.
    rule = _make_rule(field="applicant.skills_assessment", operator="EQ", value=True)
    result = evaluate_rule(rule, {"skills_assessment_status": True})
    assert result.status == EvaluationStatus.PASS


def test_resolves_health_check_alias():
    rule = _make_rule(field="applicant.health_check", operator="EQ", value=True)
    result = evaluate_rule(rule, {"health_status": True})
    assert result.status == EvaluationStatus.PASS


def test_resolves_character_check_alias():
    rule = _make_rule(field="applicant.character_check", operator="EQ", value=True)
    result = evaluate_rule(rule, {"character_status": True})
    assert result.status == EvaluationStatus.PASS


def test_unmapped_field_still_correctly_unknown_not_fabricated():
    """A field with no modeled applicant fact (e.g. a program-level fact) must
    stay UNKNOWN, not be silently matched to something unrelated."""
    rule = _make_rule(field="applicant.au_govt_debt", operator="EQ", value=False)
    result = evaluate_rule(rule, {"age": 30, "points": 80})
    assert result.status == EvaluationStatus.UNKNOWN


def test_exact_dotted_match_still_wins_over_alias():
    """Existing rules/tests that use matching dotted keys on both sides (the
    pre-fix convention used throughout this file) must keep working unchanged."""
    rule = _make_rule(field="applicant.points_score", operator="GTE", value=65)
    result = evaluate_rule(rule, {"applicant.points_score": 70, "points": 1})
    assert result.status == EvaluationStatus.PASS
    assert result.actual_value == 70

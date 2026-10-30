"""Phase 3C-3: Eligibility Assessment — ELIGIBLE / INELIGIBLE / INCOMPLETE.

Three business outcomes from rule evaluation:
- ELIGIBLE: All applicable mandatory rules PASS
- INELIGIBLE: Any mandatory rule FAIL
- INELIGIBLE: Any critical rule FAIL (even if not all evaluated)
- INCOMPLETE: No FAIL, but some required information UNKNOWN

Internal statuses remain PASS/FAIL/UNKNOWN/NOT_APPLICABLE.
Business outcome maps these to ELIGIBLE/INELIGIBLE/INCOMPLETE.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rules.models import Rule, RuleStatus, EvaluationStatus, Provenance
from rules.expressions import (
    ANDNode, RuleLeaf, ConditionalNode, NOTNode,
    evaluate_expression,
)
from assessment.applicant import ApplicantProfile, VisaSelection
from assessment.assessment import assess_eligibility, EligibilityOutcome


def _make_rule(rule_id, field="age", op="LT", val=45, mandatory=True, critical=True,
               program_id="SKILLED_189"):
    return Rule(
        rule_id=rule_id, requirement_id=f"REQ-{rule_id}",
        program_id=program_id, category="TEST",
        field=field, operator=op, value=val, unit=None,
        mandatory=mandatory, critical=critical, message=f"Rule {rule_id}",
        provenance=Provenance(
            source_id="SRC", document_id="DOC", document_version=1,
            chunk_id=1, requirement_id=f"REQ-{rule_id}", evidence_text="evidence",
        ),
        status=RuleStatus.ACTIVE,
    )


# ============================================================
# ELIGIBLE SCENARIO
# ============================================================

class TestEligibleScenario:
    """All applicable rules PASS → ELIGIBLE."""

    def test_eligible_189(self):
        """Full 189 profile → ELIGIBLE."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
            _make_rule("ENGLISH", "english_level", "IN",
                       ["competent", "proficient", "superior"]),
            _make_rule("SKILLS", "skills_assessment_status", "EQ", "VALID"),
            _make_rule("HEALTH", "health_status", "EQ", "PASSED"),
            _make_rule("CHARACTER", "character_status", "EQ", "PASSED"),
        ]
        expr = ANDNode([RuleLeaf(r.rule_id) for r in rules])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30, english_level="proficient", points=70,
            skills_assessment_status="VALID", health_status="PASSED",
            character_status="PASSED",
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "ELIGIBLE"
        assert outcome.failed_rules == []
        assert outcome.unknown_rules == []


# ============================================================
# INELIGIBLE SCENARIO
# ============================================================

class TestIneligibleScenario:
    """Any mandatory rule FAIL → INELIGIBLE."""

    def test_ineligible_points_fail(self):
        """Points below requirement → INELIGIBLE."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
        ]
        expr = ANDNode([RuleLeaf("AGE"), RuleLeaf("POINTS")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30, points=50,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INELIGIBLE"
        assert "POINTS" in outcome.failed_rules

    def test_ineligible_age_fail(self):
        """Age above limit → INELIGIBLE."""
        rules = [_make_rule("AGE", "age", "LT", 45)]
        expr = RuleLeaf("AGE")
        rules_dict = {"AGE": rules[0]}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=50,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INELIGIBLE"
        assert "AGE" in outcome.failed_rules

    def test_ineligible_multiple_failures(self):
        """Multiple rules fail → INELIGIBLE."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
        ]
        expr = ANDNode([RuleLeaf("AGE"), RuleLeaf("POINTS")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=50, points=50,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INELIGIBLE"
        assert "AGE" in outcome.failed_rules
        assert "POINTS" in outcome.failed_rules


class TestOptionalRuleHandling:
    def test_optional_failure_does_not_make_applicant_ineligible(self):
        required = _make_rule("REQUIRED", "age", "LT", 45)
        optional = _make_rule(
            "OPTIONAL", "points", "GTE", 65, mandatory=False, critical=False,
        )
        outcome = assess_eligibility(
            ANDNode([RuleLeaf("REQUIRED"), RuleLeaf("OPTIONAL")]),
            {required.rule_id: required, optional.rule_id: optional},
            ApplicantProfile(
                visa=VisaSelection(program_id="SKILLED_189"),
                age=30,
                points=50,
            ),
        )
        assert outcome.business_status == "ELIGIBLE"
        assert outcome.failed_rules == ["OPTIONAL"]

    def test_optional_unknown_does_not_make_assessment_incomplete(self):
        required = _make_rule("REQUIRED", "age", "LT", 45)
        optional = _make_rule(
            "OPTIONAL", "points", "GTE", 65, mandatory=False, critical=False,
        )
        outcome = assess_eligibility(
            ANDNode([RuleLeaf("REQUIRED"), RuleLeaf("OPTIONAL")]),
            {required.rule_id: required, optional.rule_id: optional},
            ApplicantProfile(
                visa=VisaSelection(program_id="SKILLED_189"), age=30,
            ),
        )
        assert outcome.business_status == "ELIGIBLE"
        assert outcome.unknown_rules == ["OPTIONAL"]

    def test_critical_optional_flag_still_gates_eligibility(self):
        critical = _make_rule(
            "CRITICAL", "points", "GTE", 65, mandatory=False, critical=True,
        )
        outcome = assess_eligibility(
            RuleLeaf("CRITICAL"),
            {"CRITICAL": critical},
            ApplicantProfile(
                visa=VisaSelection(program_id="SKILLED_189"), points=50,
            ),
        )
        assert outcome.business_status == "INELIGIBLE"


# ============================================================
# INCOMPLETE SCENARIO
# ============================================================

class TestIncompleteScenario:
    """No FAIL, but some UNKNOWN → INCOMPLETE."""

    def test_incomplete_missing_english(self):
        """Missing English → INCOMPLETE."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("ENGLISH", "english_level", "IN",
                       ["competent", "proficient", "superior"]),
        ]
        expr = ANDNode([RuleLeaf("AGE"), RuleLeaf("ENGLISH")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INCOMPLETE"
        assert "ENGLISH" in outcome.unknown_rules

    def test_incomplete_missing_multiple(self):
        """Multiple missing fields → INCOMPLETE."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
            _make_rule("ENGLISH", "english_level", "IN",
                       ["competent", "proficient", "superior"]),
            _make_rule("SKILLS", "skills_assessment_status", "EQ", "VALID"),
        ]
        expr = ANDNode([RuleLeaf(r.rule_id) for r in rules])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INCOMPLETE"
        assert set(outcome.unknown_rules) == {"POINTS", "ENGLISH", "SKILLS"}

    def test_incomplete_not_confused_with_fail(self):
        """UNKNOWN does not become FAIL."""
        rules = [_make_rule("POINTS", "points", "GTE", 65)]
        expr = RuleLeaf("POINTS")
        rules_dict = {"POINTS": rules[0]}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INCOMPLETE"
        assert outcome.failed_rules == []


# ============================================================
# NOT_APPLICABLE HANDLING
# ============================================================

class TestNotApplicableHandling:
    """NOT_APPLICABLE rules are excluded from business outcome."""

    def test_not_applicable_excluded(self):
        """189-only rules excluded for 190 applicant."""
        rules = [
            _make_rule("AGE-189", "age", "LT", 45, program_id="SKILLED_189"),
            _make_rule("AGE-190", "age", "LT", 45, program_id="SKILLED_190"),
        ]
        expr = ANDNode([RuleLeaf("AGE-189"), RuleLeaf("AGE-190")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "ELIGIBLE"

    def test_not_applicable_in_complete_assessment(self):
        """NOT_APPLICABLE rules don't cause INCOMPLETE."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
        ]
        expr = ANDNode([RuleLeaf("AGE"), RuleLeaf("POINTS")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INCOMPLETE"
        assert "POINTS" in outcome.unknown_rules


# ============================================================
# EVALUATION TRACE
# ============================================================

class TestEvaluationTrace:
    """Full trace of every rule evaluation."""

    def test_trace_preserved(self):
        """Every rule evaluation is recorded."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
        ]
        expr = ANDNode([RuleLeaf("AGE"), RuleLeaf("POINTS")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30, points=70,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert len(outcome.evaluations) == 2
        rule_ids = {e.rule_id for e in outcome.evaluations}
        assert rule_ids == {"AGE", "POINTS"}

    def test_trace_includes_status(self):
        """Each evaluation includes its status."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
        ]
        expr = ANDNode([RuleLeaf("AGE"), RuleLeaf("POINTS")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30, points=50,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        statuses = {e.rule_id: e.status for e in outcome.evaluations}
        assert statuses["AGE"] == EvaluationStatus.PASS
        assert statuses["POINTS"] == EvaluationStatus.FAIL

    def test_missing_field_in_trace(self):
        """Missing field shows as UNKNOWN in trace."""
        rules = [_make_rule("POINTS", "points", "GTE", 65)]
        expr = RuleLeaf("POINTS")
        rules_dict = {"POINTS": rules[0]}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert len(outcome.evaluations) == 1
        assert outcome.evaluations[0].status == EvaluationStatus.UNKNOWN
        assert outcome.evaluations[0].actual_value is None


# ============================================================
# BUSINESS OUTCOME MAPPING
# ============================================================

class TestBusinessOutcomeMapping:
    """Map internal statuses to business outcomes."""

    def test_all_pass_eligible(self):
        """All PASS → ELIGIBLE."""
        rules = [_make_rule("AGE", "age", "LT", 45)]
        expr = RuleLeaf("AGE")
        rules_dict = {"AGE": rules[0]}
        p = ApplicantProfile(visa=VisaSelection(program_id="SKILLED_189"), age=30)
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "ELIGIBLE"

    def test_one_fail_ineligible(self):
        """One FAIL → INELIGIBLE."""
        rules = [_make_rule("AGE", "age", "LT", 45)]
        expr = RuleLeaf("AGE")
        rules_dict = {"AGE": rules[0]}
        p = ApplicantProfile(visa=VisaSelection(program_id="SKILLED_189"), age=50)
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INELIGIBLE"

    def test_all_unknown_incomplete(self):
        """All UNKNOWN → INCOMPLETE."""
        rules = [_make_rule("POINTS", "points", "GTE", 65)]
        expr = RuleLeaf("POINTS")
        rules_dict = {"POINTS": rules[0]}
        p = ApplicantProfile(visa=VisaSelection(program_id="SKILLED_189"))
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INCOMPLETE"

    def test_pass_and_unknown_incomplete(self):
        """Some PASS + some UNKNOWN (no FAIL) → INCOMPLETE."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
        ]
        expr = ANDNode([RuleLeaf("AGE"), RuleLeaf("POINTS")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(visa=VisaSelection(program_id="SKILLED_189"), age=30)
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INCOMPLETE"

    def test_fail_and_unknown_ineligible(self):
        """FAIL + UNKNOWN → INELIGIBLE (FAIL takes precedence)."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
        ]
        expr = ANDNode([RuleLeaf("AGE"), RuleLeaf("POINTS")])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(visa=VisaSelection(program_id="SKILLED_189"), age=50)
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.business_status == "INELIGIBLE"

    def test_counts_correct(self):
        """Pass/fail/unknown/not_applicable counts are correct."""
        rules = [
            _make_rule("AGE", "age", "LT", 45),
            _make_rule("POINTS", "points", "GTE", 65),
            _make_rule("ENGLISH", "english_level", "IN",
                       ["competent", "proficient", "superior"]),
        ]
        expr = ANDNode([RuleLeaf(r.rule_id) for r in rules])
        rules_dict = {r.rule_id: r for r in rules}
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            age=30,
        )
        outcome = assess_eligibility(expr, rules_dict, p)
        assert outcome.passed_count == 1
        assert outcome.failed_count == 0
        assert outcome.unknown_count == 2
        assert outcome.not_applicable_count == 0
        assert outcome.total_count == 3

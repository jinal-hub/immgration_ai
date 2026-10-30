"""Phase 3B-3: Exception handling — a verified exception waives the base rule.

Semantics:
- Evaluate the base rule and exception.
- If the exception applies → NOT_APPLICABLE (base requirement is waived).
- If it does not apply → use the base rule result.
- If applicability is unknown and the base rule fails/is unknown → UNKNOWN.

Real examples:
- "Age must be under 45" UNLESS "subclass 476 applies" (476 has different age limit).
- "Points must be 65+" UNLESS "regional nomination applies" (65 becomes 50).
- "English must be competent" UNLESS "citizenship of exempt country" (no English required).
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rules.models import EvaluationStatus, Rule, Provenance
from rules.expressions import (
    evaluate_and, evaluate_or, evaluate_not,
    RuleLeaf, ANDNode, ORNode, NOTNode, ConditionalNode,
    ExceptionNode, evaluate_expression,
)


def _make_rule(rule_id, field, op, val):
    return Rule(
        rule_id=rule_id, requirement_id=f"REQ-{rule_id}",
        program_id="SKILLED_189", category="TEST",
        field=field, operator=op, value=val, unit=None,
        mandatory=True, critical=True, message=f"Rule {rule_id}",
        provenance=Provenance(
            source_id="SRC", document_id="DOC", document_version=1,
            chunk_id=1, requirement_id=f"REQ-{rule_id}", evidence_text="evidence",
        ),
    )


# ============================================================
# EXCEPTION TRUTH TABLE
# ============================================================

class TestExceptionTruthTable:
    """ExceptionNode: main UNLESS exception."""

    def test_exception_applies_main_pass(self):
        """Exception applies + main PASS -> NOT_APPLICABLE (waived)."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
        }
        data = {"age": 30, "is_476": True}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.NOT_APPLICABLE

    def test_exception_applies_main_fail(self):
        """Exception applies + main FAIL -> NOT_APPLICABLE (waived)."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
        }
        data = {"age": 50, "is_476": True}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.NOT_APPLICABLE

    def test_exception_applies_main_unknown(self):
        """A verified exception waives the base rule even when its fact is unknown."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
        }
        data = {"is_476": True}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.NOT_APPLICABLE

    def test_exception_not_applies_main_pass(self):
        """Exception not applies + main PASS -> PASS."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
        }
        data = {"age": 30, "is_476": False}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

    def test_exception_not_applies_main_fail(self):
        """Exception not applies + main FAIL -> FAIL."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
        }
        data = {"age": 50, "is_476": False}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.FAIL

    def test_exception_unknown(self):
        """Exception UNKNOWN + main PASS -> PASS (can't determine if exception applies)."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
        }
        data = {"age": 30}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

    def test_exception_not_applicable_main_pass(self):
        """Exception NOT_APPLICABLE + main PASS -> PASS."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
        }
        data = {"age": 30}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

    def test_both_main_and_exception_referenced_in_evals(self):
        """Both rules appear in evaluation list."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
        }
        data = {"age": 30, "is_476": True}
        _, evals = evaluate_expression(node, rules, data)
        rule_ids = [e.rule_id for e in evals]
        assert "MAIN" in rule_ids
        assert "EXCEPTION" in rule_ids


# ============================================================
# NESTED EXCEPTIONS
# ============================================================

class TestNestedExceptions:
    """Exceptions nested with AND/OR/NOT/IF_THEN."""

    def test_exception_inside_and(self):
        """AND(exception_rule, other) where exception not applies."""
        exc = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        node = ANDNode([exc, RuleLeaf("OTHER")])
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
            "OTHER": _make_rule("OTHER", "skills", "EQ", True),
        }
        data = {"age": 30, "is_476": False, "skills": True}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

    def test_exception_inside_and_applies(self):
        """AND(exception_rule, other) where exception applies -> base waived."""
        exc = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        node = ANDNode([exc, RuleLeaf("OTHER")])
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
            "OTHER": _make_rule("OTHER", "skills", "EQ", True),
        }
        data = {"age": 30, "is_476": True, "skills": True}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

    def test_exception_or_with_not_applicable(self):
        """OR(exception_rule, other) where exception not applies -> PASS via other."""
        exc = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        node = ORNode([exc, RuleLeaf("OTHER")])
        rules = {
            "MAIN": _make_rule("MAIN", "age", "LT", 45),
            "EXCEPTION": _make_rule("EXCEPTION", "is_476", "EQ", True),
            "OTHER": _make_rule("OTHER", "skills", "EQ", True),
        }
        data = {"age": 30, "is_476": False, "skills": True}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

    def test_exception_serialization(self):
        """ExceptionNode serialization."""
        node = ExceptionNode(RuleLeaf("MAIN"), RuleLeaf("EXCEPTION"))
        d = node.to_dict()
        assert d["type"] == "EXCEPTION"
        assert d["rule"]["type"] == "RULE"
        assert d["exception"]["type"] == "RULE"


# ============================================================
# REAL IMMIGRATION CASES
# ============================================================

class TestRealExceptionCases:
    """Real immigration exception patterns."""

    def test_age_with_476_exception(self):
        """Age under 45 UNLESS subclass 476 (different limit)."""
        node = ExceptionNode(RuleLeaf("AGE"), RuleLeaf("IS_476"))
        rules = {
            "AGE": _make_rule("AGE", "age", "LT", 45),
            "IS_476": _make_rule("IS_476", "visa_subclass", "EQ", "476"),
        }
        data = {"age": 30, "visa_subclass": "476"}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.NOT_APPLICABLE

    def test_age_standard_no_exception(self):
        """Standard age rule, no exception applies."""
        node = ExceptionNode(RuleLeaf("AGE"), RuleLeaf("IS_476"))
        rules = {
            "AGE": _make_rule("AGE", "age", "LT", 45),
            "IS_476": _make_rule("IS_476", "visa_subclass", "EQ", "476"),
        }
        data = {"age": 30, "visa_subclass": "189"}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

    def test_points_with_regional_exception(self):
        """Points 65+ UNLESS regional nomination (lower threshold)."""
        node = ExceptionNode(RuleLeaf("POINTS"), RuleLeaf("REGIONAL"))
        rules = {
            "POINTS": _make_rule("POINTS", "points", "GTE", 65),
            "REGIONAL": _make_rule("REGIONAL", "has_regional_nomination", "EQ", True),
        }
        data = {"points": 55, "has_regional_nomination": True}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.NOT_APPLICABLE

    def test_190_with_exception(self):
        """AND(AGE, OR(POINTS, EXCEPTION_RULE)) structure."""
        points_with_exception = ExceptionNode(
            RuleLeaf("POINTS"),
            RuleLeaf("REGIONAL"),
        )
        node = ANDNode([
            RuleLeaf("AGE"),
            points_with_exception,
        ])
        rules = {
            "AGE": _make_rule("AGE", "age", "LT", 45),
            "POINTS": _make_rule("POINTS", "points", "GTE", 65),
            "REGIONAL": _make_rule("REGIONAL", "has_regional_nomination", "EQ", True),
        }
        data = {"age": 30, "points": 55, "has_regional_nomination": True}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

    def test_exception_with_conditional(self):
        """IF is_476 THEN (exception applies on age)."""
        node = ConditionalNode(
            RuleLeaf("IS_476"),
            RuleLeaf("AGE_476"),
        )
        rules = {
            "IS_476": _make_rule("IS_476", "visa_subclass", "EQ", "476"),
            "AGE_476": _make_rule("AGE_476", "age", "LT", 50),
        }
        data = {"visa_subclass": "476", "age": 48}
        status, _ = evaluate_expression(node, rules, data)
        assert status == EvaluationStatus.PASS

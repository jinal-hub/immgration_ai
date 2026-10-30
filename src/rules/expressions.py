"""Phase 3B: Rule expression tree — AND / OR / NOT / IF-THEN / EXCEPTION.

Evaluates combinations of rule results.
Generic engine — no immigration-specific logic.

Four-state semantics:
- PASS: Rule applies and condition passed.
- FAIL: Rule applies and condition failed.
- UNKNOWN: Rule applies, but required information is missing.
- NOT_APPLICABLE: Rule does not apply to this applicant/pathway.

Composition rules:
- AND: Filter out NOT_APPLICABLE, then FAIL > UNKNOWN > PASS.
- OR: Filter out NOT_APPLICABLE, then PASS > UNKNOWN > FAIL.
- NOT: Inverts PASS/FAIL, UNKNOWN and NOT_APPLICABLE stay unchanged.
- IF/THEN: IF FAIL -> NOT_APPLICABLE, IF UNKNOWN -> UNKNOWN, IF PASS -> evaluate THEN.
- EXCEPTION: rule UNLESS exception. Exception applies + main PASS -> FAIL.
  Exception does NOT apply -> main result unchanged.
  Exception UNKNOWN or NOT_APPLICABLE -> main result unchanged.
"""
from __future__ import annotations

from typing import Any

from rules.models import EvaluationStatus, Rule, RuleEvaluation
from rules.rule_engine import evaluate_rule


def _filter_applicable(statuses: list[EvaluationStatus]) -> list[EvaluationStatus]:
    """Remove NOT_APPLICABLE from status list.

    NOT_APPLICABLE means 'this rule doesn't apply to this applicant'.
    It should not influence the logical evaluation of remaining rules.
    """
    return [s for s in statuses if s != EvaluationStatus.NOT_APPLICABLE]


def evaluate_and(statuses: list[EvaluationStatus]) -> EvaluationStatus:
    """Evaluate AND over a list of statuses.

    NOT_APPLICABLE is filtered out before evaluation.
    If all are NOT_APPLICABLE (empty after filter), result is NOT_APPLICABLE.

    Semantics after filtering:
        PASS + PASS -> PASS
        FAIL + anything -> FAIL  (FAIL takes precedence over UNKNOWN)
        UNKNOWN + PASS -> UNKNOWN
        UNKNOWN + UNKNOWN -> UNKNOWN
    """
    applicable = _filter_applicable(statuses)
    if not applicable:
        return EvaluationStatus.NOT_APPLICABLE

    has_fail = False
    has_unknown = False

    for s in applicable:
        if s == EvaluationStatus.FAIL:
            has_fail = True
        elif s == EvaluationStatus.UNKNOWN:
            has_unknown = True

    if has_fail:
        return EvaluationStatus.FAIL
    if has_unknown:
        return EvaluationStatus.UNKNOWN
    return EvaluationStatus.PASS


def evaluate_or(statuses: list[EvaluationStatus]) -> EvaluationStatus:
    """Evaluate OR over a list of statuses.

    NOT_APPLICABLE is filtered out before evaluation.
    If all are NOT_APPLICABLE (empty after filter), result is NOT_APPLICABLE.

    Semantics after filtering:
        PASS + anything -> PASS  (PASS takes precedence)
        FAIL + FAIL -> FAIL
        FAIL + UNKNOWN -> UNKNOWN  (can't decide without UNKNOWN resolving)
        UNKNOWN + UNKNOWN -> UNKNOWN
    """
    applicable = _filter_applicable(statuses)
    if not applicable:
        return EvaluationStatus.NOT_APPLICABLE

    has_pass = False
    has_unknown = False

    for s in applicable:
        if s == EvaluationStatus.PASS:
            has_pass = True
        elif s == EvaluationStatus.UNKNOWN:
            has_unknown = True

    if has_pass:
        return EvaluationStatus.PASS
    if has_unknown:
        return EvaluationStatus.UNKNOWN
    return EvaluationStatus.FAIL


def evaluate_not(status: EvaluationStatus) -> EvaluationStatus:
    """Evaluate NOT — inverts PASS/FAIL.

    UNKNOWN stays UNKNOWN (can't negate unknown).
    NOT_APPLICABLE stays NOT_APPLICABLE (can't negate inapplicable).
    """
    if status == EvaluationStatus.PASS:
        return EvaluationStatus.FAIL
    if status == EvaluationStatus.FAIL:
        return EvaluationStatus.PASS
    return status


# ============================================================
# Expression tree model
# ============================================================


class ExpressionNode:
    """Base class for expression tree nodes."""
    pass


class RuleLeaf(ExpressionNode):
    """A leaf node referencing a single rule."""

    def __init__(self, rule_id: str):
        self.rule_id = rule_id

    def to_dict(self) -> dict:
        return {"type": "RULE", "rule_id": self.rule_id}


class ANDNode(ExpressionNode):
    """AND — all applicable children must PASS."""

    def __init__(self, children: list[ExpressionNode]):
        self.children = children

    def to_dict(self) -> dict:
        return {"type": "AND", "children": [c.to_dict() for c in self.children]}


class ORNode(ExpressionNode):
    """OR — at least one applicable child must PASS."""

    def __init__(self, children: list[ExpressionNode]):
        self.children = children

    def to_dict(self) -> dict:
        return {"type": "OR", "children": [c.to_dict() for c in self.children]}


class NOTNode(ExpressionNode):
    """NOT — inverts child result."""

    def __init__(self, child: ExpressionNode):
        self.child = child

    def to_dict(self) -> dict:
        return {"type": "NOT", "child": self.child.to_dict()}


class ConditionalNode(ExpressionNode):
    """IF/THEN — evaluate consequence only if condition passes.

    Semantics:
        IF PASS    -> evaluate THEN
        IF FAIL    -> NOT_APPLICABLE
        IF UNKNOWN -> UNKNOWN
        IF NOT_APPLICABLE -> NOT_APPLICABLE
    """

    def __init__(self, condition: ExpressionNode, consequence: ExpressionNode):
        self.condition = condition
        self.consequence = consequence

    def to_dict(self) -> dict:
        return {
            "type": "IF_THEN",
            "condition": self.condition.to_dict(),
            "consequence": self.consequence.to_dict(),
        }


class ExceptionNode(ExpressionNode):
    """EXCEPTION — rule UNLESS exception.

    Semantics:
        Exception applies -> NOT_APPLICABLE (the base rule is waived)
        Exception does NOT apply -> main result unchanged
        Exception UNKNOWN and main FAIL/UNKNOWN -> UNKNOWN
        Exception UNKNOWN and main PASS -> PASS
        Exception NOT_APPLICABLE -> main result unchanged
    """

    def __init__(self, rule: ExpressionNode, exception: ExpressionNode):
        self.rule = rule
        self.exception = exception

    def to_dict(self) -> dict:
        return {
            "type": "EXCEPTION",
            "rule": self.rule.to_dict(),
            "exception": self.exception.to_dict(),
        }


class DependencyNode(ExpressionNode):
    """DEPENDENCY — rule requires prerequisite rules.

    Semantics:
        Any dependency FAIL -> NOT_APPLICABLE
        Any dependency UNKNOWN -> UNKNOWN
        Any dependency NOT_APPLICABLE -> NOT_APPLICABLE
        All dependencies PASS -> evaluate rule normally
    """

    def __init__(self, rule: ExpressionNode, dependencies: list[ExpressionNode]):
        self.rule = rule
        self.dependencies = dependencies

    def to_dict(self) -> dict:
        return {
            "type": "DEPENDENCY",
            "rule": self.rule.to_dict(),
            "dependencies": [d.to_dict() for d in self.dependencies],
        }


# ============================================================
# Expression evaluator
# ============================================================


def evaluate_expression(
    node: ExpressionNode,
    rules_by_id: dict[str, Rule],
    applicant_data: dict[str, Any],
) -> tuple[EvaluationStatus, list[RuleEvaluation]]:
    """Recursively evaluate an expression tree against applicant data.

    Returns:
        (overall_status, all_individual_evaluations)
    """
    if isinstance(node, RuleLeaf):
        rule = rules_by_id.get(node.rule_id)
        if rule is None:
            ev = RuleEvaluation(
                rule_id=node.rule_id,
                status=EvaluationStatus.ERROR,
                field="UNKNOWN",
                actual_value=None,
                expected_value=None,
                operator="UNKNOWN",
                message=f"Rule {node.rule_id} not found",
                reason=f"Rule {node.rule_id} not found in rules registry",
            )
            return ev.status, [ev]

        ev = evaluate_rule(rule, applicant_data)
        return ev.status, [ev]

    if isinstance(node, NOTNode):
        child_status, child_evals = evaluate_expression(node.child, rules_by_id, applicant_data)
        result = evaluate_not(child_status)
        return result, child_evals

    if isinstance(node, ANDNode):
        all_statuses = []
        all_evals = []
        for child in node.children:
            status, evals = evaluate_expression(child, rules_by_id, applicant_data)
            all_statuses.append(status)
            all_evals.extend(evals)
        return evaluate_and(all_statuses), all_evals

    if isinstance(node, ORNode):
        all_statuses = []
        all_evals = []
        for child in node.children:
            status, evals = evaluate_expression(child, rules_by_id, applicant_data)
            all_statuses.append(status)
            all_evals.extend(evals)
        return evaluate_or(all_statuses), all_evals

    if isinstance(node, ConditionalNode):
        cond_status, cond_evals = evaluate_expression(node.condition, rules_by_id, applicant_data)

        if cond_status == EvaluationStatus.PASS:
            cons_status, cons_evals = evaluate_expression(node.consequence, rules_by_id, applicant_data)
            return cons_status, cond_evals + cons_evals

        if cond_status == EvaluationStatus.FAIL:
            return EvaluationStatus.NOT_APPLICABLE, cond_evals

        return cond_status, cond_evals

    if isinstance(node, ExceptionNode):
        main_status, main_evals = evaluate_expression(node.rule, rules_by_id, applicant_data)
        exc_status, exc_evals = evaluate_expression(node.exception, rules_by_id, applicant_data)

        all_evals = main_evals + exc_evals

        if exc_status == EvaluationStatus.PASS:
            return EvaluationStatus.NOT_APPLICABLE, all_evals

        if (
            exc_status == EvaluationStatus.UNKNOWN
            and main_status in (EvaluationStatus.FAIL, EvaluationStatus.UNKNOWN)
        ):
            return EvaluationStatus.UNKNOWN, all_evals

        # A nonmatching or inapplicable exception leaves the base rule unchanged.
        return main_status, all_evals

    if isinstance(node, DependencyNode):
        all_evals = []

        # Evaluate all dependencies first
        for dep in node.dependencies:
            dep_status, dep_evals = evaluate_expression(dep, rules_by_id, applicant_data)
            all_evals.extend(dep_evals)

            # FAIL in dependency -> NOT_APPLICABLE (rule doesn't apply if prereqs fail)
            if dep_status == EvaluationStatus.FAIL:
                return EvaluationStatus.NOT_APPLICABLE, all_evals
            # NOT_APPLICABLE stays NOT_APPLICABLE
            if dep_status == EvaluationStatus.NOT_APPLICABLE:
                return EvaluationStatus.NOT_APPLICABLE, all_evals
            # UNKNOWN propagates
            if dep_status == EvaluationStatus.UNKNOWN:
                return EvaluationStatus.UNKNOWN, all_evals

        # All dependencies PASS -> evaluate the rule
        rule_status, rule_evals = evaluate_expression(node.rule, rules_by_id, applicant_data)
        return rule_status, all_evals + rule_evals

    raise ValueError(f"Unknown node type: {type(node)}")

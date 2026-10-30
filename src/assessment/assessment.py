"""Phase 3C-3: Eligibility Assessment — ELIGIBLE / INELIGIBLE / INCOMPLETE.

Three business outcomes:
- ELIGIBLE: All applicable mandatory rules PASS
- INELIGIBLE: Any mandatory/critical rule FAIL
- INCOMPLETE: No FAIL, but some required information UNKNOWN

Internal statuses remain PASS/FAIL/UNKNOWN/NOT_APPLICABLE.
Business outcome maps these to ELIGIBLE/INELIGIBLE/INCOMPLETE.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rules.models import Rule, EvaluationStatus, RuleEvaluation
from rules.expressions import (
    ANDNode, ConditionalNode, DependencyNode, ExceptionNode, ExpressionNode,
    NOTNode, ORNode, RuleLeaf, evaluate_expression,
)
from assessment.applicant import ApplicantProfile


@dataclass
class EligibilityOutcome:
    """Result of an eligibility assessment."""
    business_status: str  # ELIGIBLE | INELIGIBLE | INCOMPLETE
    overall_status: EvaluationStatus  # raw engine status
    evaluations: list[RuleEvaluation]
    passed_rules: list[str]
    failed_rules: list[str]
    unknown_rules: list[str]
    not_applicable_rules: list[str]
    passed_count: int
    failed_count: int
    unknown_count: int
    not_applicable_count: int
    total_count: int


def assess_eligibility(
    expression: ExpressionNode,
    rules_by_id: dict[str, Rule],
    applicant: ApplicantProfile,
) -> EligibilityOutcome:
    """Evaluate an expression tree against an applicant profile.

    1. Convert profile to rule data (None fields excluded → UNKNOWN)
    2. Evaluate expression tree
    3. Map raw statuses to business outcome
    """
    rule_data = applicant.to_rule_data()
    _, evaluations = evaluate_expression(expression, rules_by_id, rule_data)
    required_expression = _required_expression(expression, rules_by_id)
    if required_expression is None:
        overall_status = EvaluationStatus.NOT_APPLICABLE
    else:
        overall_status, _ = evaluate_expression(
            required_expression, rules_by_id, rule_data,
        )

    passed = [e.rule_id for e in evaluations if e.status == EvaluationStatus.PASS]
    failed = [e.rule_id for e in evaluations if e.status == EvaluationStatus.FAIL]
    unknown = [e.rule_id for e in evaluations if e.status == EvaluationStatus.UNKNOWN]
    not_app = [e.rule_id for e in evaluations if e.status == EvaluationStatus.NOT_APPLICABLE]

    business = _map_to_business_status(overall_status)

    return EligibilityOutcome(
        business_status=business,
        overall_status=overall_status,
        evaluations=evaluations,
        passed_rules=passed,
        failed_rules=failed,
        unknown_rules=unknown,
        not_applicable_rules=not_app,
        passed_count=len(passed),
        failed_count=len(failed),
        unknown_count=len(unknown),
        not_applicable_count=len(not_app),
        total_count=len(evaluations),
    )


def _required_expression(
    node: ExpressionNode,
    rules_by_id: dict[str, Rule],
) -> ExpressionNode | None:
    """Remove optional-only leaves while preserving policy composition."""
    if isinstance(node, RuleLeaf):
        rule = rules_by_id.get(node.rule_id)
        return node if rule is not None and (rule.mandatory or rule.critical) else None
    if isinstance(node, ANDNode):
        children = [
            child for child in
            (_required_expression(item, rules_by_id) for item in node.children)
            if child is not None
        ]
        return ANDNode(children) if children else None
    if isinstance(node, ORNode):
        children = [
            child for child in
            (_required_expression(item, rules_by_id) for item in node.children)
            if child is not None
        ]
        return ORNode(children) if children else None
    if isinstance(node, NOTNode):
        child = _required_expression(node.child, rules_by_id)
        return NOTNode(child) if child is not None else None
    if isinstance(node, ConditionalNode):
        consequence = _required_expression(node.consequence, rules_by_id)
        return (
            ConditionalNode(node.condition, consequence)
            if consequence is not None else None
        )
    if isinstance(node, ExceptionNode):
        rule = _required_expression(node.rule, rules_by_id)
        return ExceptionNode(rule, node.exception) if rule is not None else None
    if isinstance(node, DependencyNode):
        rule = _required_expression(node.rule, rules_by_id)
        return (
            DependencyNode(rule, node.dependencies)
            if rule is not None else None
        )
    raise ValueError(f"Unknown expression node: {type(node)}")


def _map_to_business_status(overall: EvaluationStatus) -> str:
    """Only required/critical rules gate eligibility.

    If no requirements apply, the result stays incomplete rather than implying
    the applicant meets criteria that were never evaluated.
    """
    if overall == EvaluationStatus.FAIL:
        return "INELIGIBLE"
    if overall in (
        EvaluationStatus.UNKNOWN,
        EvaluationStatus.ERROR,
        EvaluationStatus.NOT_APPLICABLE,
    ):
        return "INCOMPLETE"
    return "ELIGIBLE"

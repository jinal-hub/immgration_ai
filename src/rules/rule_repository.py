"""Enhanced rule repository — bridges DB Rule model ↔ in-memory Rule dataclass.

Provides status transitions, transactional activation (supersede old, activate new),
and loading rules back into the in-memory Rule dataclass for the rule engine.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db.models import Rule as RuleORM, RuleVersion, Requirement
from rules.models import Rule, RuleStatus, Provenance
from rules.rule_activation import (
    validate_transition, now_iso, RuleActivationError,
)
from logging_config import get_logger

logger = get_logger(__name__)


def orm_to_dataclass(orm: RuleORM) -> Rule:
    """Convert a DB Rule ORM object into an in-memory Rule dataclass."""
    prov = Provenance(
        source_id=orm.source_id or "",
        document_id=orm.document_id or "",
        document_version=orm.document_version or 0,
        chunk_id=orm.chunk_id or 0,
        requirement_id=orm.requirement_id or "",
        evidence_text=orm.evidence_text or "",
        requirement_version_id=orm.requirement_version_id or 0,
    )
    return Rule(
        rule_id=orm.rule_id,
        requirement_id=orm.requirement_id or "",
        program_id=orm.program_id or "",
        category=orm.category or "",
        field=orm.field or "",
        operator=orm.operator or "",
        value=json.loads(orm.value_json) if orm.value_json else None,
        unit=orm.unit,
        mandatory=orm.mandatory or False,
        critical=orm.critical or False,
        message=orm.message or "",
        provenance=prov,
        status=RuleStatus(orm.status or "DRAFT"),
        created_at=orm.created_at.isoformat() if orm.created_at else now_iso(),
        version=orm.current_version or 1,
        condition=json.loads(orm.condition_json) if orm.condition_json else None,
        exception=json.loads(orm.exception_json) if orm.exception_json else None,
    )


class EnhancedRuleRepo:
    """Repository for rule lifecycle management with proper state machine."""

    def __init__(self, session: Session):
        self._s = session

    # ------------------------------------------------------------------
    # Basic CRUD
    # ------------------------------------------------------------------
    def get(self, rule_id: str) -> RuleORM | None:
        return self._s.get(RuleORM, rule_id)

    def get_as_dataclass(self, rule_id: str) -> Rule | None:
        orm = self.get(rule_id)
        return orm_to_dataclass(orm) if orm else None

    def create(self, rule_id: str, **kwargs) -> RuleORM:
        obj = RuleORM(rule_id=rule_id, **kwargs)
        self._s.add(obj)
        self._s.flush()
        return obj

    def count(self) -> int:
        return self._s.execute(select(func.count()).select_from(RuleORM)).scalar() or 0

    def list_by_status(self, status: str, program_id: str | None = None) -> list[RuleORM]:
        stmt = select(RuleORM).where(RuleORM.status == status)
        if program_id:
            stmt = stmt.where(RuleORM.program_id == program_id)
        return list(self._s.execute(stmt).scalars())

    def list_active(self, program_id: str | None = None) -> list[RuleORM]:
        return self.list_by_status("ACTIVE", program_id)

    def list_active_as_dataclasses(self, program_id: str | None = None) -> list[Rule]:
        return [orm_to_dataclass(r) for r in self.list_active(program_id)]

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------
    def transition_status(self, rule_id: str, new_status: str,
                          actor: str = "system") -> RuleORM:
        """Transition a rule to a new status. Validates the transition."""
        obj = self.get(rule_id)
        if not obj:
            raise ValueError(f"Rule {rule_id} not found")
        validate_transition(obj.status, new_status)
        old_status = obj.status
        obj.status = new_status
        self._s.flush()
        logger.info("Rule status transitioned",
                     extra={"rule_id": rule_id, "from": old_status,
                            "to": new_status, "actor": actor})
        return obj

    def validate_rule(self, rule_id: str, actor: str = "system") -> RuleORM:
        """DRAFT → VALIDATED (passed automated validation)."""
        return self.transition_status(rule_id, "VALIDATED", actor)

    def approve_rule(self, rule_id: str, actor: str) -> RuleORM:
        """VALIDATED → APPROVED (human reviewed)."""
        return self.transition_status(rule_id, "APPROVED", actor)

    def reject_rule(self, rule_id: str, actor: str) -> RuleORM:
        """Any pre-active status → REJECTED."""
        return self.transition_status(rule_id, "REJECTED", actor)

    def activate_rule(self, rule_id: str, actor: str = "system") -> RuleORM:
        """APPROVED → ACTIVE. Transactionally supersedes any existing active version
        of the same logical rule (same program_id + category + field)."""
        obj = self.get(rule_id)
        if not obj:
            raise ValueError(f"Rule {rule_id} not found")
        validate_transition(obj.status, "ACTIVE")

        # Find and supersede any currently ACTIVE rule for same logical key
        existing_active = self._find_active_for_logical_key(
            obj.program_id, obj.category, obj.field, exclude_rule_id=rule_id
        )
        for old in existing_active:
            old.status = "SUPERSEDED"
            old.updated_at = datetime.now(timezone.utc)
            self._s.flush()
            logger.info("Superseded old rule version",
                         extra={"old_rule_id": old.rule_id, "new_rule_id": rule_id,
                                "program_id": old.program_id})

        obj.status = "ACTIVE"
        obj.updated_at = datetime.now(timezone.utc)
        self._s.flush()

        # Record activation version
        self._save_version(rule_id, obj.current_version or 1, "ACTIVATED", actor)

        logger.info("Rule activated",
                     extra={"rule_id": rule_id, "program_id": obj.program_id,
                            "actor": actor})
        return obj

    def retire_rule(self, rule_id: str, actor: str = "system") -> RuleORM:
        """SUPERSEDED or ACTIVE → RETIRED."""
        return self.transition_status(rule_id, "RETIRED", actor)

    # ------------------------------------------------------------------
    # Version tracking
    # ------------------------------------------------------------------
    def _save_version(self, rule_id: str, version_number: int,
                      change_type: str, created_by: str) -> RuleVersion:
        obj = self.get(rule_id)
        # Auto-increment: use max existing + 1 to avoid UNIQUE collisions
        from sqlalchemy import func as safunc
        max_ver = self._s.query(safunc.max(RuleVersion.version_number)).filter(
            RuleVersion.rule_id == rule_id
        ).scalar()
        actual_version = (max_ver + 1) if max_ver is not None else version_number
        snapshot = {
            "rule_id": rule_id,
            "status": obj.status,
            "program_id": obj.program_id,
            "category": obj.category,
            "field": obj.field,
            "operator": obj.operator,
            "value": json.loads(obj.value_json) if obj.value_json else None,
            "unit": obj.unit,
            "mandatory": obj.mandatory,
            "critical": obj.critical,
            "message": obj.message,
            "source_id": obj.source_id,
            "document_id": obj.document_id,
            "document_version": obj.document_version,
            "chunk_id": obj.chunk_id,
            "evidence_text": obj.evidence_text,
        }
        rv = RuleVersion(
            rule_id=rule_id,
            version_number=actual_version,
            snapshot_json=snapshot,
            change_type=change_type,
            created_by=created_by,
            # keep version snapshots anchored to the same requirement lineage as the rule
            requirement_version_id=obj.requirement_version_id,
        )
        self._s.add(rv)
        self._s.flush()
        return rv

    def save_version_snapshot(self, rule_id: str, change_type: str,
                              created_by: str = "system") -> RuleVersion:
        """Save a version snapshot of the current rule state."""
        obj = self.get(rule_id)
        if not obj:
            raise ValueError(f"Rule {rule_id} not found")
        return self._save_version(rule_id, obj.current_version or 1,
                                  change_type, created_by)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------
    def _find_active_for_logical_key(self, program_id: str, category: str,
                                     field: str,
                                     exclude_rule_id: str = "") -> list[RuleORM]:
        """Find ACTIVE rules with same logical key (program + category + field)."""
        stmt = (
            select(RuleORM)
            .where(RuleORM.status == "ACTIVE")
            .where(RuleORM.program_id == program_id)
            .where(RuleORM.category == category)
            .where(RuleORM.field == field)
        )
        if exclude_rule_id:
            stmt = stmt.where(RuleORM.rule_id != exclude_rule_id)
        return list(self._s.execute(stmt).scalars())

    def get_active_version_for_field(self, program_id: str,
                                     category: str, field: str) -> RuleORM | None:
        """Get the single ACTIVE rule for a specific field in a program."""
        results = self._find_active_for_logical_key(program_id, category, field)
        return results[0] if results else None

    def get_active_rules_as_dataclasses(self, program_id: str) -> list[Rule]:
        """Load all ACTIVE rules for a program into in-memory Rule dataclasses."""
        return self.list_active_as_dataclasses(program_id)

    def list_drafts(self, program_id: str | None = None) -> list[RuleORM]:
        """List all DRAFT rules awaiting review."""
        return self.list_by_status("DRAFT", program_id)

    def list_validated(self, program_id: str | None = None) -> list[RuleORM]:
        """List all VALIDATED rules awaiting approval."""
        return self.list_by_status("VALIDATED", program_id)

    def list_approved_pending_activation(self, program_id: str | None = None) -> list[RuleORM]:
        """List APPROVED rules ready for activation."""
        return self.list_by_status("APPROVED", program_id)

    # ------------------------------------------------------------------
    # Bulk operations for the orchestrator
    # ------------------------------------------------------------------
    def _compiled_to_data(self, compiled_rule: Rule, requirement_id: str) -> dict[str, Any]:
        """Shared field mapping used by both upsert_from_compiled (persists) and
        would_change_from_compiled (dry-run preview, persists nothing)."""
        prov = compiled_rule.provenance
        return dict(
            requirement_id=requirement_id,
            requirement_version_id=prov.requirement_version_id or None,
            program_id=compiled_rule.program_id,
            category=compiled_rule.category,
            field=compiled_rule.field,
            operator=compiled_rule.operator,
            value_json=json.dumps(compiled_rule.value),
            unit=compiled_rule.unit,
            mandatory=compiled_rule.mandatory,
            critical=compiled_rule.critical,
            message=compiled_rule.message,
            source_id=prov.source_id,
            document_id=prov.document_id,
            document_version=prov.document_version,
            chunk_id=prov.chunk_id,
            evidence_text=prov.evidence_text,
            condition_json=json.dumps(compiled_rule.condition) if compiled_rule.condition else None,
            exception_json=json.dumps(compiled_rule.exception) if compiled_rule.exception else None,
            status="DRAFT",
            current_version=1,
        )

    def upsert_from_compiled(self, rule_id: str, compiled_rule: Rule,
                             requirement_id: str) -> tuple[RuleORM, bool]:
        """Create or update a rule from a compiled Phase 3 Rule dataclass.
        If the rule already exists, update its fields.
        Rules in ACTIVE/SUPERSEDED/RETIRED status are NEVER downgraded to DRAFT.
        This is the failure-safe invariant: recompilation never destroys active rules.

        Returns (rule, changed) - `changed` is True only when a new row was
        inserted or an existing row's fields actually differ from what's being
        set; False for a genuine no-op (recompiling a requirement that
        produces byte-for-byte the same rule as what's already stored).
        Callers (compile_approved_requirements -> government_sync's
        rules_created metric, and the version-snapshot audit trail) both rely
        on this to avoid reporting/recording "created" for a sync that
        touched nothing - a real bug found live: 3 rules with created_at from
        a previous day were counted as freshly "created" in a run that
        re-processed the same already-ACTIVE, unchanged rules.
        """
        existing = self.get(rule_id)
        data = self._compiled_to_data(compiled_rule, requirement_id)
        if existing:
            protected_statuses = {"ACTIVE", "SUPERSEDED", "RETIRED"}
            if existing.status in protected_statuses:
                fields = {k: v for k, v in data.items() if k != "status"}
                changed = any(getattr(existing, k) != v for k, v in fields.items())
                for k, v in fields.items():
                    setattr(existing, k, v)
                self._s.flush()
                return existing, changed
            changed = any(getattr(existing, k) != v for k, v in data.items())
            for k, v in data.items():
                setattr(existing, k, v)
            self._s.flush()
            return existing, changed
        return self.create(rule_id=rule_id, **data), True

    def would_change_from_compiled(self, rule_id: str, compiled_rule: Rule,
                                   requirement_id: str) -> bool:
        """Preview whether upsert_from_compiled would report `changed=True`,
        without persisting anything. Used by compile_approved_requirements'
        dry_run mode so a preview doesn't just assume everything is new."""
        existing = self.get(rule_id)
        if existing is None:
            return True
        data = self._compiled_to_data(compiled_rule, requirement_id)
        protected_statuses = {"ACTIVE", "SUPERSEDED", "RETIRED"}
        fields = {k: v for k, v in data.items() if k != "status"} \
            if existing.status in protected_statuses else data
        return any(getattr(existing, k) != v for k, v in fields.items())

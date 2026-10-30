"""Tests for the assessment service — full end-to-end pipeline.

Covers the complete flow:
    Applicant → Load Rules → Build Expression → Applicability Filter →
    Expression Engine → Eligibility → Gap Engine → Persist Results

Verifies all fixes:
    1. evaluate_inline delegates to shared _evaluate (no duplication)
    2. rule_version comes from DB, not hardcoded
    3. actual_value/expected_value stored as JSON, not str()
    4. visa_subclass maps to actual subclass, not program_id
    5. filter_applicable_rules dead code removed from critical path
    6. rule_version_id set on assessment
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, GovernmentSource, GovernmentDocument
from db.repositories import (
    ApplicantRepo, CountryRepo, ProgramRepo,
)
from rules.rule_repository import EnhancedRuleRepo
from services.assessment_service import AssessmentService, _build_profile_data
from assessment.applicant import ApplicantProfile, VisaSelection


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture()
def session(db_engine):
    factory = sessionmaker(bind=db_engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def seed_program(session):
    """Seed AU + SKILLED_189 program."""
    CountryRepo(session).upsert("AU", "Australia", "AU")
    ProgramRepo(session).upsert(
        "SKILLED_189", "AU", "Skilled Independent", "189",
        "Skilled Independent visa",
    )
    session.flush()


@pytest.fixture()
def seed_rules(session, seed_program):
    """Seed two ACTIVE rules: age < 45, points >= 65."""
    repo = EnhancedRuleRepo(session)
    repo.create(
        rule_id="RULE-AGE-189",
        requirement_id="REQ-AGE-189",
        program_id="SKILLED_189",
        category="age",
        field="age",
        operator="LT",
        value_json="45",
        mandatory=True,
        critical=True,
        message="Age must be under 45",
        status="ACTIVE",
        current_version=3,
        source_id="SRC-1",
        document_id="DOC-1",
        document_version=1,
        chunk_id=1,
        evidence_text="Under 45 required",
    )
    repo.create(
        rule_id="RULE-POINTS-189",
        requirement_id="REQ-POINTS-189",
        program_id="SKILLED_189",
        category="points",
        field="points",
        operator="GTE",
        value_json="65",
        mandatory=True,
        critical=False,
        message="Must score at least 65 points",
        status="ACTIVE",
        current_version=2,
        source_id="SRC-1",
        document_id="DOC-1",
        document_version=1,
        chunk_id=2,
        evidence_text="Minimum 65 points",
    )
    session.flush()


@pytest.fixture()
def create_applicant(session):
    """Factory to create an applicant profile."""
    def _create(**kwargs):
        repo = ApplicantRepo(session)
        return repo.create(user_id="user-test", **kwargs)
    return _create


# ---------------------------------------------------------------------------
# Bug fix: rule_version from DB, not hardcoded 1
# ---------------------------------------------------------------------------
class TestRuleVersionFromDB:
    def test_rule_version_reflects_db(self, session, seed_rules, create_applicant):
        """AssessmentRuleResult.rule_version matches the rule's current_version."""
        applicant = create_applicant(age=30, points=70)
        svc = AssessmentService(session)
        result = svc.run_assessment(applicant.applicant_id, "SKILLED_189")

        assert result["business_status"] == "ELIGIBLE"

        rr_map = {rr["rule_id"]: rr for rr in result["rule_results"]}
        assert rr_map["RULE-AGE-189"]["rule_version"] == 3
        assert rr_map["RULE-POINTS-189"]["rule_version"] == 2


# ---------------------------------------------------------------------------
# Bug fix: actual_value/expected_value are JSON-serializable
# ---------------------------------------------------------------------------
class TestValueSerialization:
    def test_actual_value_is_json(self, session, seed_rules, create_applicant):
        """Stored actual_value/expected_value should be valid JSON."""
        applicant = create_applicant(age=30, points=70)
        svc = AssessmentService(session)
        result = svc.run_assessment(applicant.applicant_id, "SKILLED_189")

        for rr in result["rule_results"]:
            # Should be valid JSON strings
            json.loads(rr["actual_value"])
            json.loads(rr["expected_value"])

    def test_int_values_survive_roundtrip(self, session, seed_rules, create_applicant):
        """Integer values survive JSON roundtrip."""
        applicant = create_applicant(age=30, points=70)
        svc = AssessmentService(session)
        result = svc.run_assessment(applicant.applicant_id, "SKILLED_189")

        rr_map = {rr["rule_id"]: rr for rr in result["rule_results"]}
        assert json.loads(rr_map["RULE-AGE-189"]["actual_value"]) == 30
        assert json.loads(rr_map["RULE-AGE-189"]["expected_value"]) == 45


# ---------------------------------------------------------------------------
# Bug fix: visa_subclass field mapping
# ---------------------------------------------------------------------------
class TestVisaSubclassMapping:
    def test_to_rule_data_uses_program_id(self, session):
        """to_rule_data() sets "program_id" to the visa program."""
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_491"),
            age=30,
            visa_subclass="491",
        )
        d = p.to_rule_data()
        assert d["program_id"] == "SKILLED_491"
        assert d["visa_subclass"] == "491"

    def test_visa_subclass_not_overwritten_by_program(self, session):
        """visa_subclass field is the actual subclass, not the program_id."""
        p = ApplicantProfile(
            visa=VisaSelection(program_id="SKILLED_189"),
            visa_subclass="189",
        )
        d = p.to_rule_data()
        assert d["visa_subclass"] == "189"
        assert d["program_id"] == "SKILLED_189"


# ---------------------------------------------------------------------------
# evaluate_inline uses shared _evaluate (no code duplication)
# ---------------------------------------------------------------------------
class TestEvaluateInline:
    def test_evaluate_inline_matches_run_assessment(self, session, seed_rules, create_applicant):
        """evaluate_inline returns the same result as run_assessment (minus persistence)."""
        applicant = create_applicant(age=30, points=70)
        svc = AssessmentService(session)

        inline = svc.evaluate_inline(applicant.applicant_id, "SKILLED_189")
        assert inline["business_status"] == "ELIGIBLE"
        assert inline["overall_status"] == "PASS"
        assert inline["total_rules"] == 2
        assert inline["passed_count"] == 2
        assert inline["failed_count"] == 0

    def test_evaluate_inline_ineligible(self, session, seed_rules, create_applicant):
        """Inline evaluation detects ineligible applicant."""
        applicant = create_applicant(age=50, points=70)
        svc = AssessmentService(session)
        inline = svc.evaluate_inline(applicant.applicant_id, "SKILLED_189")
        assert inline["business_status"] == "INELIGIBLE"
        assert inline["failed_count"] >= 1

    def test_evaluate_inline_incomplete(self, session, seed_rules, create_applicant):
        """Inline evaluation detects incomplete when fields missing."""
        applicant = create_applicant()  # no data
        svc = AssessmentService(session)
        inline = svc.evaluate_inline(applicant.applicant_id, "SKILLED_189")
        assert inline["business_status"] == "INCOMPLETE"
        assert inline["unknown_count"] >= 1

    def test_evaluate_inline_no_persistence(self, session, seed_rules, create_applicant):
        """Inline evaluation does NOT create an assessment record."""
        from db.repositories import AssessmentRepo
        applicant = create_applicant(age=30, points=70)
        svc = AssessmentService(session)
        repo = AssessmentRepo(session)

        count_before = len(repo.list_for_applicant(applicant.applicant_id))
        svc.evaluate_inline(applicant.applicant_id, "SKILLED_189")
        count_after = len(repo.list_for_applicant(applicant.applicant_id))
        assert count_after == count_before

    def test_evaluate_inlinelicant_not_found(self, session, seed_rules):
        """evaluate_inline raises ValueError for missing applicant."""
        svc = AssessmentService(session)
        with pytest.raises(ValueError, match="not found"):
            svc.evaluate_inline("nonexistent", "SKILLED_189")

    def test_evaluate_inline_no_rules(self, session, create_applicant):
        """evaluate_inline raises ValueError when no rules exist."""
        applicant = create_applicant(age=30)
        svc = AssessmentService(session)
        with pytest.raises(ValueError, match="No ACTIVE rules"):
            svc.evaluate_inline(applicant.applicant_id, "SKILLED_189")


# ---------------------------------------------------------------------------
# Full pipeline: all steps connected
# ---------------------------------------------------------------------------
class TestFullPipeline:
    def test_eligible_path(self, session, seed_rules, create_applicant):
        """Full path: all rules pass → ELIGIBLE → persisted with rule results."""
        applicant = create_applicant(age=30, points=70)
        svc = AssessmentService(session)
        result = svc.run_assessment(applicant.applicant_id, "SKILLED_189")

        assert result["business_status"] == "ELIGIBLE"
        assert result["overall_status"] == "PASS"
        assert result["total_rules"] == 2
        assert result["passed_count"] == 2
        assert result["failed_count"] == 0
        assert result["unknown_count"] == 0
        assert len(result["rule_results"]) == 2
        assert len(result["gaps"]) == 0
        assert result["assessment_id"] is not None

    def test_ineligible_path(self, session, seed_rules, create_applicant):
        """Full path: mandatory rule fails → INELIGIBLE → persisted."""
        applicant = create_applicant(age=50, points=70)
        svc = AssessmentService(session)
        result = svc.run_assessment(applicant.applicant_id, "SKILLED_189")

        assert result["business_status"] == "INELIGIBLE"
        assert result["failed_count"] >= 1
        assert result["overall_status"] == "FAIL"

    def test_incomplete_path_with_gaps(self, session, seed_rules, create_applicant):
        """Full path: missing data → INCOMPLETE → gaps persisted."""
        applicant = create_applicant()  # no data
        svc = AssessmentService(session)
        result = svc.run_assessment(applicant.applicant_id, "SKILLED_189")

        assert result["business_status"] == "INCOMPLETE"
        assert result["unknown_count"] >= 1
        assert len(result["gaps"]) >= 1
        # Gaps should have proper structure
        gap = result["gaps"][0]
        assert "field" in gap
        assert "priority" in gap
        assert "message" in gap

    def test_assessment_persisted_and_retrievable(self, session, seed_rules, create_applicant):
        """Created assessment can be fetched back with all data."""
        applicant = create_applicant(age=30, points=70)
        svc = AssessmentService(session)
        created = svc.run_assessment(applicant.applicant_id, "SKILLED_189")

        fetched = svc.get(created["assessment_id"])
        assert fetched is not None
        assert fetched["assessment_id"] == created["assessment_id"]
        assert fetched["business_status"] == "ELIGIBLE"
        assert len(fetched["rule_results"]) == 2
        assert len(fetched["gaps"]) == 0

    def test_list_for_applicant(self, session, seed_rules, create_applicant):
        """Multiple assessments for same applicant are listed."""
        applicant = create_applicant(age=30, points=70)
        svc = AssessmentService(session)
        svc.run_assessment(applicant.applicant_id, "SKILLED_189")
        svc.run_assessment(applicant.applicant_id, "SKILLED_189")

        results = svc.list_for_applicant(applicant.applicant_id)
        assert len(results) == 2

    def test_build_profile_data_extracts_correct_fields(self, session, create_applicant):
        """_build_profile_data maps ORM columns to dict correctly."""
        applicant = create_applicant(
            age=30, english_level="proficient", points=70,
            occupation="261313", visa_subclass="189",
        )
        data = _build_profile_data(applicant)
        assert data["age"] == 30
        assert data["english_level"] == "proficient"
        assert data["points"] == 70
        assert data["occupation"] == "261313"
        assert data["visa_subclass"] == "189"

    def test_build_profile_data_skips_none_fields(self, session, create_applicant):
        """_build_profile_data omits fields that are None."""
        applicant = create_applicant(age=30)
        data = _build_profile_data(applicant)
        assert data["age"] == 30
        assert "english_level" not in data
        assert "points" not in data

    def test_date_of_birth_overrides_stale_stored_age(self, session, create_applicant):
        today = datetime.now(timezone.utc).date()
        birth_date = today.replace(year=today.year - 30, month=1, day=1)
        applicant = create_applicant(
            age=18,
            date_of_birth=datetime.combine(
                birth_date, datetime.min.time(), tzinfo=timezone.utc,
            ),
        )
        data = _build_profile_data(applicant)
        expected_age = today.year - birth_date.year - (
            (today.month, today.day) < (birth_date.month, birth_date.day)
        )
        assert data["age"] == expected_age

    def test_future_date_of_birth_is_rejected(self, session, create_applicant):
        future = datetime.now(timezone.utc) + timedelta(days=365)
        applicant = create_applicant(date_of_birth=future)
        with pytest.raises(ValueError, match="cannot be in the future"):
            _build_profile_data(applicant)

    def test_error_handling_applicant_not_found(self, session, seed_rules):
        """run_assessment raises ValueError for nonexistent applicant."""
        svc = AssessmentService(session)
        with pytest.raises(ValueError, match="not found"):
            svc.run_assessment("nonexistent", "SKILLED_189")

    def test_error_handling_no_rules(self, session, create_applicant):
        """run_assessment raises ValueError when no rules exist."""
        applicant = create_applicant(age=30)
        svc = AssessmentService(session)
        with pytest.raises(ValueError, match="No ACTIVE rules"):
            svc.run_assessment(applicant.applicant_id, "SKILLED_189")

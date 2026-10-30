"""Applicant service — business logic for applicant CRUD."""
from __future__ import annotations

from db.repositories import ApplicantRepo, VisaSelectionRepo
from logging_config import get_logger

logger = get_logger(__name__)


# The applicant facts the service returns. Kept as one list so the single
# and the list views cannot drift apart.
_PROFILE_FIELDS = (
    "first_name",
    "last_name",
    "date_of_birth",
    "passport_nationality",
    "current_visa_subclass",
    "age",
    "english_level",
    "points",
    "occupation",
    "skills_assessment_status",
    "health_status",
    "character_status",
    "has_regional_nomination",
    "visa_subclass",
    # First batch of newly modelled applicant concepts (Phase 4).
    "visa_history",
    "employer_sponsored",
    "work_experience_years",
    "in_australia_at_application",
    "has_health_insurance",
    "genuine_temporary_entrant",
)


def _profile_payload(profile) -> dict:
    payload = {
        "applicant_id": profile.applicant_id,
        "user_id": profile.user_id,
    }
    for name in _PROFILE_FIELDS:
        payload[name] = getattr(profile, name, None)
    payload["created_at"] = profile.created_at
    return payload


class ApplicantService:
    def __init__(self, session):
        self._repo = ApplicantRepo(session)
        self._visa_repo = VisaSelectionRepo(session)

    def create(self, user_id: str, **kwargs) -> dict:
        profile = self._repo.create(user_id, **kwargs)
        logger.info("Applicant created", extra={"applicant_id": profile.applicant_id})
        return self.get(profile.applicant_id)

    def get(self, applicant_id: str) -> dict | None:
        profile = self._repo.get(applicant_id)
        if not profile:
            return None
        return _profile_payload(profile)

    def list_for_user(self, user_id: str) -> list[dict]:
        profiles = self._repo.list_for_user(user_id)
        return [_profile_payload(p) for p in profiles]

    def update(self, applicant_id: str, **kwargs) -> dict | None:
        profile = self._repo.update(applicant_id, **kwargs)
        if not profile:
            return None
        logger.info("Applicant updated", extra={"applicant_id": applicant_id})
        return self.get(applicant_id)

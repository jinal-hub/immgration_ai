"""Consultancy tenant membership and application-case access rules."""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db.models import (
    ApiUser,
    ApplicationCase,
    Assessment,
    AuditLog,
    CaseChecklistItem,
    ConsultancyTenant,
    FormAnswer,
    FormTemplate,
    Gap,
    ImmigrationProgram,
    TenantInvitation,
    TenantMembership,
)
from db.models import (
    ApplicantProfile as ApplicantProfileModel,
)

_INVITATION_TTL_HOURS = 72


class TenantService:
    def __init__(self, session: Session):
        self._session = session

    def membership(self, tenant_id: str, user_id: str) -> TenantMembership | None:
        return self._session.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.user_id == user_id,
                TenantMembership.status == "ACTIVE",
            )
        ).scalars().first()

    def create_tenant(self, user_id: str, name: str, slug: str) -> ConsultancyTenant:
        tenant = ConsultancyTenant(
            name=name.strip(),
            slug=slug,
            created_by=user_id,
            status="ACTIVE",
        )
        self._session.add(tenant)
        self._session.flush()
        self._session.add(
            TenantMembership(
                tenant_id=tenant.tenant_id,
                user_id=user_id,
                role="OWNER",
                status="ACTIVE",
            )
        )
        self._session.flush()
        self._audit(
            user_id,
            "TENANT_CREATED",
            "consultancy_tenant",
            str(tenant.tenant_id),
            None,
            {"name": tenant.name, "slug": tenant.slug},
        )
        return tenant

    def list_for_user(self, user_id: str) -> list[dict]:
        rows = self._session.execute(
            select(ConsultancyTenant, TenantMembership.role)
            .join(
                TenantMembership,
                TenantMembership.tenant_id == ConsultancyTenant.tenant_id,
            )
            .where(
                TenantMembership.user_id == user_id,
                TenantMembership.status == "ACTIVE",
                ConsultancyTenant.status == "ACTIVE",
            )
            .order_by(ConsultancyTenant.name)
        )
        return [
            {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "slug": tenant.slug,
                "role": role,
                "status": tenant.status,
            }
            for tenant, role in rows
        ]

    def add_member(
        self, tenant_id: str, email: str, role: str,
    ) -> TenantMembership | None:
        tenant = self._session.get(ConsultancyTenant, tenant_id)
        if tenant is None or tenant.status != "ACTIVE":
            return None
        user = self._session.execute(
            select(ApiUser).where(ApiUser.email == email.strip().lower())
        ).scalars().first()
        if user is None or not user.is_active:
            raise LookupError("Active user account not found")
        existing = self._session.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.user_id == user.user_id,
            )
        ).scalars().first()
        if existing is not None:
            raise ValueError("User already has a membership in this tenant")

        membership = TenantMembership(
            tenant_id=tenant_id,
            user_id=user.user_id,
            role=role,
            status="ACTIVE",
        )
        self._session.add(membership)
        self._session.flush()
        return membership

    def list_members(self, tenant_id: str) -> list[dict]:
        rows = self._session.execute(
            select(TenantMembership, ApiUser)
            .join(ApiUser, ApiUser.user_id == TenantMembership.user_id)
            .where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.status == "ACTIVE",
            )
            .order_by(TenantMembership.created_at)
        )
        return [
            {
                "membership_id": member.membership_id,
                "tenant_id": member.tenant_id,
                "user_id": member.user_id,
                "email": user.email,
                "full_name": user.full_name,
                "role": member.role,
                "status": member.status,
            }
            for member, user in rows
        ]

    def create_case(
        self,
        tenant_id: str,
        user_id: str,
        applicant_id: str,
        program_id: str,
        title: str | None,
        assessment_id: str | None = None,
    ) -> ApplicationCase:
        tenant = self._session.get(ConsultancyTenant, tenant_id)
        if tenant is None or tenant.status != "ACTIVE":
            raise LookupError("Tenant not found")
        membership = self.membership(tenant_id, user_id)
        if membership is None:
            raise PermissionError("Tenant membership required")
        applicant = self._session.get(ApplicantProfileModel, applicant_id)
        if applicant is None:
            raise LookupError("Applicant profile not found")

        if membership.role == "APPLICANT":
            if applicant.user_id != user_id:
                raise PermissionError("Applicants may open cases only for their own profile")
        elif membership.role in ("OWNER", "CONSULTANT"):
            applicant_membership = self.membership(tenant_id, applicant.user_id)
            if applicant_membership is None or applicant_membership.role != "APPLICANT":
                raise PermissionError("Applicant must belong to this tenant")
        else:
            raise PermissionError("This tenant role cannot create application cases")

        program = self._session.get(ImmigrationProgram, program_id)
        if program is None or program.status != "ACTIVE" or program.subclass_code is None:
            raise LookupError("Active applicant visa program not found")
        assessment = None
        if assessment_id is not None:
            assessment = self._session.get(Assessment, assessment_id)
            if (
                assessment is None
                or assessment.applicant_id != applicant_id
                or assessment.program_id != program_id
            ):
                raise LookupError("Assessment not found for this applicant and program")

        case = ApplicationCase(
            tenant_id=tenant_id,
            applicant_id=applicant_id,
            program_id=program_id,
            created_by=user_id,
            assigned_consultant_id=(
                user_id if membership.role == "CONSULTANT" else None
            ),
            assessment_id=assessment.assessment_id if assessment else None,
            ruleset_id=assessment.ruleset_id if assessment else None,
            status="DRAFT",
            title=title,
        )
        self._session.add(case)
        self._session.flush()
        self._audit(
            user_id,
            "CASE_CREATED",
            "application_case",
            str(case.case_id),
            None,
            {
                "tenant_id": tenant_id,
                "applicant_id": applicant_id,
                "program_id": program_id,
                "status": case.status,
            },
        )
        return case

    def list_applicants_for_tenant(
        self, tenant_id: str, user_id: str
    ) -> list[dict] | None:
        membership = self.membership(tenant_id, user_id)
        if membership is None:
            return None
        stmt = (
            select(
                ApplicantProfileModel,
                ApiUser.email,
                ApiUser.full_name,
            )
            .join(
                TenantMembership,
                (TenantMembership.user_id == ApplicantProfileModel.user_id)
                & (TenantMembership.tenant_id == tenant_id)
                & (TenantMembership.role == "APPLICANT")
                & (TenantMembership.status == "ACTIVE"),
            )
            .join(ApiUser, ApiUser.user_id == ApplicantProfileModel.user_id)
        )
        if membership.role == "APPLICANT":
            stmt = stmt.where(ApplicantProfileModel.user_id == user_id)
        elif membership.role == "CONSULTANT":
            stmt = stmt.join(
                ApplicationCase,
                ApplicationCase.applicant_id == ApplicantProfileModel.applicant_id,
            ).where(
                ApplicationCase.tenant_id == tenant_id,
                ApplicationCase.assigned_consultant_id == user_id,
            )
        elif membership.role not in {"OWNER", "REVIEWER"}:
            return []
        rows = self._session.execute(
            stmt.distinct().order_by(
                ApplicantProfileModel.last_name,
                ApplicantProfileModel.first_name,
            )
        )
        return [
            {
                "applicant_id": profile.applicant_id,
                "user_id": profile.user_id,
                "email": email,
                "full_name": full_name,
                "first_name": profile.first_name,
                "last_name": profile.last_name,
            }
            for profile, email, full_name in rows
        ]

    def list_cases(self, tenant_id: str, user_id: str) -> list[ApplicationCase]:
        membership = self.membership(tenant_id, user_id)
        if membership is None:
            raise PermissionError("Tenant membership required")
        stmt = select(ApplicationCase).where(
            ApplicationCase.tenant_id == tenant_id,
        )
        if membership.role == "APPLICANT":
            stmt = stmt.join(
                ApplicantProfileModel,
                ApplicantProfileModel.applicant_id == ApplicationCase.applicant_id,
            ).where(ApplicantProfileModel.user_id == user_id)
        elif membership.role == "CONSULTANT":
            stmt = stmt.where(ApplicationCase.assigned_consultant_id == user_id)
        elif membership.role not in ("OWNER", "REVIEWER"):
            return []
        return list(
            self._session.execute(
                stmt.order_by(ApplicationCase.created_at.desc())
            ).scalars()
        )

    def list_unassigned_cases(
        self, tenant_id: str, user_id: str
    ) -> list[ApplicationCase]:
        membership = self.membership(tenant_id, user_id)
        if membership is None or membership.role != "CONSULTANT":
            raise PermissionError("Consultant membership required")
        return list(
            self._session.execute(
                select(ApplicationCase)
                .where(
                    ApplicationCase.tenant_id == tenant_id,
                    ApplicationCase.assigned_consultant_id.is_(None),
                )
                .order_by(ApplicationCase.created_at.asc())
            ).scalars()
        )

    def get_case(self, case_id: str, user_id: str) -> ApplicationCase | None:
        case = self._session.get(ApplicationCase, case_id)
        if case is None:
            return None
        membership = self.membership(case.tenant_id, user_id)
        if membership is None:
            return None
        if membership.role == "APPLICANT":
            applicant = self._session.get(ApplicantProfileModel, case.applicant_id)
            return case if applicant and applicant.user_id == user_id else None
        if membership.role == "CONSULTANT":
            return case if case.assigned_consultant_id == user_id else None
        if membership.role in ("OWNER", "REVIEWER"):
            return case
        return None

    def update_case_status(
        self,
        case_id: str,
        user_id: str,
        next_status: str,
    ) -> ApplicationCase:
        case = self.get_case(case_id, user_id)
        if case is None:
            raise LookupError("Application case not found")
        membership = self.membership(case.tenant_id, user_id)
        if membership is None:
            raise PermissionError("Tenant membership required")

        transitions = {
            "DRAFT": {"IN_PROGRESS"},
            "IN_PROGRESS": {"NEEDS_INFO", "READY_FOR_REVIEW", "CLOSED"},
            "NEEDS_INFO": {"IN_PROGRESS", "CLOSED"},
            "READY_FOR_REVIEW": {
                "NEEDS_INFO", "CHANGES_REQUESTED", "APPROVED",
            },
            "CHANGES_REQUESTED": {"IN_PROGRESS", "READY_FOR_REVIEW"},
            "APPROVED": {"PACKAGE_READY", "CLOSED"},
            "PACKAGE_READY": {"CLOSED"},
            "CLOSED": set(),
        }
        applicant_transitions = {
            ("DRAFT", "IN_PROGRESS"),
            ("IN_PROGRESS", "READY_FOR_REVIEW"),
            ("NEEDS_INFO", "IN_PROGRESS"),
            ("CHANGES_REQUESTED", "IN_PROGRESS"),
            ("CHANGES_REQUESTED", "READY_FOR_REVIEW"),
        }
        if membership.role == "APPLICANT":
            allowed = (case.status, next_status) in applicant_transitions
        elif membership.role in {"OWNER", "CONSULTANT", "REVIEWER"}:
            allowed = next_status in transitions.get(case.status, set())
        else:
            allowed = False
        if not allowed:
            raise ValueError(
                f"Transition from {case.status} to {next_status} is not allowed."
            )

        before = {"status": case.status}
        case.status = next_status
        self._session.flush()
        self._audit(
            user_id,
            "CASE_STATUS_CHANGED",
            "application_case",
            str(case.case_id),
            before,
            {"status": next_status},
        )
        return case

    def _user_by_email(self, email: str) -> ApiUser | None:
        return self._session.execute(
            select(ApiUser).where(ApiUser.email == email)
        ).scalars().first()

    @staticmethod
    def _invitation_status(invitation: TenantInvitation) -> str:
        now = datetime.now(UTC)
        if invitation.accepted_at is not None:
            return "ACCEPTED"
        if invitation.revoked_at is not None:
            return "REVOKED"
        expires_at = invitation.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at < now:
                return "EXPIRED"
        return "PENDING"

    def _audit(
        self,
        user_id: str,
        action: str,
        entity_type: str,
        entity_id: str | None,
        before: dict | None,
        after: dict | None,
    ) -> None:
        self._session.add(
            AuditLog(
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                before_json=before,
                after_json=after,
            )
        )

    def invite_member(
        self,
        tenant_id: str,
        email: str,
        role: str,
        invited_by: str,
    ) -> tuple[TenantInvitation, str]:
        tenant = self._session.get(ConsultancyTenant, tenant_id)
        if tenant is None or tenant.status != "ACTIVE":
            raise LookupError("Tenant not found")
        normalised = email.strip().lower()
        actor_user = self._user_by_email(normalised)
        if actor_user is not None:
            existing = self._session.execute(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant_id,
                    TenantMembership.user_id == actor_user.user_id,
                    TenantMembership.status == "ACTIVE",
                )
            ).scalars().first()
            if existing is not None:
                raise ValueError("User already has an active membership in this tenant")
        token = secrets.token_urlsafe(32)
        invitation = TenantInvitation(
            tenant_id=tenant_id,
            email=normalised,
            role=role,
            invited_by=invited_by,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=_INVITATION_TTL_HOURS),
        )
        self._session.add(invitation)
        self._session.flush()
        self._audit(
            invited_by,
            "TENANT_INVITATION_CREATED",
            "tenant_invitation",
            str(invitation.invitation_id),
            None,
            {"email": normalised, "role": role},
        )
        return invitation, token

    def list_invitations(self, tenant_id: str) -> list[dict]:
        rows = self._session.execute(
            select(TenantInvitation, ApiUser)
            .outerjoin(ApiUser, ApiUser.user_id == TenantInvitation.invited_by)
            .where(TenantInvitation.tenant_id == tenant_id)
            .order_by(TenantInvitation.created_at.desc())
        ).all()
        return [
            {
                "invitation_id": inv.invitation_id,
                "email": inv.email,
                "role": inv.role,
                "status": self._invitation_status(inv),
                "invited_by": inv.invited_by,
                "invited_by_name": inviter.full_name if inviter else None,
                "expires_at": inv.expires_at,
                "accepted_at": inv.accepted_at,
                "revoked_at": inv.revoked_at,
                "created_at": inv.created_at,
            }
            for inv, inviter in rows
        ]

    def revoke_invitation(self, tenant_id: str, invitation_id: str, user_id: str) -> None:
        invitation = self._session.execute(
            select(TenantInvitation).where(
                TenantInvitation.invitation_id == invitation_id,
                TenantInvitation.tenant_id == tenant_id,
            )
        ).scalars().first()
        if invitation is None:
            raise LookupError("Invitation not found")
        if invitation.accepted_at is not None or invitation.revoked_at is not None:
            raise ValueError("Invitation cannot be revoked")
        membership = self.membership(tenant_id, user_id)
        if membership is None or membership.role != "OWNER":
            raise PermissionError("Tenant owner role required")
        invitation.revoked_at = datetime.now(UTC)
        self._session.flush()
        self._audit(
            user_id,
            "TENANT_INVITATION_REVOKED",
            "tenant_invitation",
            str(invitation_id),
            None,
            {"email": invitation.email},
        )

    def accept_invitation(self, token: str, user_id: str) -> TenantMembership:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        invitation = self._session.execute(
            select(TenantInvitation).where(TenantInvitation.token_hash == token_hash)
        ).scalars().first()
        if invitation is None:
            raise LookupError("Invitation not found")
        if invitation.accepted_at is not None:
            raise ValueError("Invitation already accepted")
        if invitation.revoked_at is not None:
            raise ValueError("Invitation revoked")
        expires_at = invitation.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at < datetime.now(UTC):
                raise ValueError("Invitation expired")
        user = self._session.get(ApiUser, user_id)
        if user is None or not user.is_active:
            raise LookupError("Active user account not found")
        if user.email.strip().lower() != invitation.email:
            raise PermissionError("Invitation email does not match your account")
        tenant = self._session.get(ConsultancyTenant, invitation.tenant_id)
        if tenant is None or tenant.status != "ACTIVE":
            raise LookupError("Invitation tenant is not active")
        existing = self._session.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == invitation.tenant_id,
                TenantMembership.user_id == user.user_id,
            )
        ).scalars().first()
        if existing is not None:
            if existing.status == "ACTIVE":
                raise ValueError("User already has an active membership in this tenant")
            existing.status = "ACTIVE"
            existing.role = invitation.role
            membership = existing
        else:
            membership = TenantMembership(
                tenant_id=invitation.tenant_id,
                user_id=user.user_id,
                role=invitation.role,
                status="ACTIVE",
            )
            self._session.add(membership)
        invitation.accepted_at = datetime.now(UTC)
        self._session.flush()
        self._audit(
            user_id,
            "TENANT_INVITATION_ACCEPTED",
            "tenant_invitation",
            str(invitation.invitation_id),
            None,
            {"role": invitation.role},
        )
        return membership

    def update_member_role(
        self, tenant_id: str, membership_id: str, role: str, user_id: str
    ) -> TenantMembership:
        actor = self.membership(tenant_id, user_id)
        if actor is None or actor.role != "OWNER":
            raise PermissionError("Tenant owner role required")
        membership = self._session.execute(
            select(TenantMembership).where(
                TenantMembership.membership_id == membership_id,
                TenantMembership.tenant_id == tenant_id,
            )
        ).scalars().first()
        if membership is None:
            raise LookupError("Membership not found")
        if membership.role == "OWNER":
            raise PermissionError("Owner role cannot be changed")
        if role == "OWNER":
            raise PermissionError("Cannot assign owner role")
        before = {"role": membership.role, "status": membership.status}
        membership.role = role
        self._session.flush()
        self._audit(
            user_id,
            "TENANT_MEMBER_ROLE_CHANGED",
            "tenant_membership",
            membership_id,
            before,
            {"role": role, "status": membership.status},
        )
        return membership

    def revoke_member(self, tenant_id: str, membership_id: str, user_id: str) -> None:
        actor = self.membership(tenant_id, user_id)
        if actor is None or actor.role != "OWNER":
            raise PermissionError("Tenant owner role required")
        membership = self._session.execute(
            select(TenantMembership).where(
                TenantMembership.membership_id == membership_id,
                TenantMembership.tenant_id == tenant_id,
            )
        ).scalars().first()
        if membership is None:
            raise LookupError("Membership not found")
        if membership.role == "OWNER":
            raise PermissionError("Owner membership cannot be revoked")
        active_owners = self._session.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.role == "OWNER",
                TenantMembership.status == "ACTIVE",
            )
        ).scalars().all()
        if membership.status == "ACTIVE" and len(active_owners) <= 1:
            raise ValueError("Cannot revoke the last active owner")
        before = {"role": membership.role, "status": membership.status}
        membership.status = "REVOKED"
        self._session.flush()
        self._audit(
            user_id,
            "TENANT_MEMBER_REVOKED",
            "tenant_membership",
            membership_id,
            before,
            {"role": membership.role, "status": membership.status},
        )

    def assign_case(
        self,
        case_id: str,
        consultant_id: str,
        user_id: str,
    ) -> ApplicationCase:
        case = self._session.get(ApplicationCase, case_id)
        if case is None:
            raise LookupError("Application case not found")
        actor = self.membership(case.tenant_id, user_id)
        if actor is None:
            raise PermissionError("Tenant membership required")
        consultant = self._session.get(ApiUser, consultant_id)
        if consultant is None or not consultant.is_active:
            raise LookupError("Consultant not found")
        consultant_membership = self.membership(case.tenant_id, consultant_id)
        if (
            consultant_membership is None
            or consultant_membership.role not in {"CONSULTANT", "REVIEWER", "OWNER"}
        ):
            raise PermissionError("Assignee must be an active tenant consultant, reviewer, or owner")
        if actor.role == "CONSULTANT":
            if case.assigned_consultant_id is not None:
                raise PermissionError("Case is already assigned")
            if consultant_id != user_id:
                raise PermissionError("Consultants can only assign unassigned cases to themselves")
        elif actor.role not in {"OWNER", "REVIEWER"}:
            raise PermissionError("This tenant role cannot assign cases")
        before = {"assigned_consultant_id": case.assigned_consultant_id}
        case.assigned_consultant_id = consultant_id
        self._session.flush()
        self._audit(
            user_id,
            "CASE_ASSIGNED",
            "application_case",
            str(case.case_id),
            before,
            {"assigned_consultant_id": consultant_id},
        )
        return case

    def generate_checklist(
        self, case_id: str, user_id: str, assessment_id: str | None = None
    ) -> list[CaseChecklistItem]:
        case = self.get_case(case_id, user_id)
        if case is None:
            raise LookupError("Application case not found")
        actor = self.membership(case.tenant_id, user_id)
        if actor is None or actor.role not in {"OWNER", "CONSULTANT", "REVIEWER"}:
            raise PermissionError("Tenant staff role required")
        assessment_id = assessment_id or case.assessment_id

        existing = self._session.execute(
            select(CaseChecklistItem).where(CaseChecklistItem.case_id == case_id)
        ).scalars().all()
        if existing:
            raise ValueError("Checklist already generated for this case")

        base_items = [
            (
                "passport_copy",
                "Passport copy",
                "Workflow prompt: provide the identity page of the current passport.",
                "document",
                True,
                0,
            ),
            (
                "identity_documents",
                "Identity supporting documents",
                "Additional identity documents requested for this case.",
                "document",
                False,
                1,
            ),
            (
                "health_examination",
                "Health documents (if requested)",
                "Upload health documents only if requested by the relevant authority.",
                "document",
                False,
                2,
            ),
        ]
        program = self._session.get(ImmigrationProgram, case.program_id)
        category_prompts = {
            "SKILLED": [
                ("skills_assessment", "Skills assessment evidence", "Upload relevant skills assessment evidence if applicable."),
                ("employment_evidence", "Employment evidence", "Upload employment evidence requested for this pathway."),
            ],
            "WORK": [
                ("sponsor_evidence", "Sponsorship or employment evidence", "Upload employer or sponsorship evidence requested for this pathway."),
            ],
            "STUDENT": [
                ("enrolment_evidence", "Enrolment evidence", "Upload enrolment evidence requested for this pathway."),
                ("insurance_evidence", "Health insurance evidence", "Upload insurance evidence if required for this pathway."),
            ],
            "VISITOR": [
                ("travel_evidence", "Travel and financial evidence", "Upload itinerary or financial evidence requested for this pathway."),
            ],
        }
        category = program.visa_category if program else ""
        for key, label, description in category_prompts.get(category, []):
            base_items.append(
                (key, label, description, "document", False, len(base_items))
            )

        items: list[CaseChecklistItem] = []
        for requirement_key, label, description, category, required, position in base_items:
            items.append(
                CaseChecklistItem(
                    tenant_id=case.tenant_id,
                    case_id=case_id,
                    requirement_key=requirement_key,
                    label=label,
                    description=description,
                    category=category,
                    required=required,
                    status="PENDING",
                    position=position,
                )
            )

        if assessment_id is not None:
            assessment = self._session.get(Assessment, assessment_id)
            if (
                assessment is None
                or assessment.applicant_id != case.applicant_id
                or assessment.program_id != case.program_id
            ):
                raise LookupError("Assessment not found for this case")
            case.assessment_id = assessment_id
            case.ruleset_id = assessment.ruleset_id
            assessment_gaps = self._session.execute(
                select(Gap).where(Gap.assessment_id == assessment_id)
            ).scalars()
            gap_position = len(base_items)
            for index, gap in enumerate(assessment_gaps):
                field = gap.field or gap.message or "additional information"
                items.append(
                    CaseChecklistItem(
                        tenant_id=case.tenant_id,
                        case_id=case_id,
                        requirement_key=f"assessment_gap_{index}",
                        label=f"Provide: {field}",
                        description=gap.message or "Answer requested by eligibility assessment.",
                        category="information",
                        required=True,
                        status="PENDING",
                        position=gap_position + index,
                    )
                )

        self._session.add_all(items)
        self._session.flush()
        self._audit(
            user_id,
            "CHECKLIST_GENERATED",
            "application_case",
            str(case.case_id),
            None,
            {"item_count": len(items), "assessment_id": assessment_id},
        )
        return items

    def list_checklist(
        self, case_id: str, user_id: str
    ) -> list[CaseChecklistItem] | None:
        case = self.get_case(case_id, user_id)
        if case is None:
            return None
        return list(
            self._session.execute(
                select(CaseChecklistItem)
                .where(CaseChecklistItem.case_id == case_id)
                .order_by(CaseChecklistItem.position, CaseChecklistItem.created_at)
            ).scalars()
        )

    def update_checklist_item(
        self,
        case_id: str,
        item_id: str,
        user_id: str,
        status: str,
        rejection_reason: str | None = None,
    ) -> CaseChecklistItem:
        case = self.get_case(case_id, user_id)
        if case is None:
            raise LookupError("Application case not found")
        item = self._session.execute(
            select(CaseChecklistItem).where(
                CaseChecklistItem.item_id == item_id,
                CaseChecklistItem.case_id == case_id,
                CaseChecklistItem.tenant_id == case.tenant_id,
            )
        ).scalars().first()
        if item is None:
            raise LookupError("Checklist item not found")
        membership = self.membership(item.tenant_id, user_id)
        if membership is None or membership.role not in {"OWNER", "CONSULTANT", "REVIEWER"}:
            raise PermissionError("Tenant staff role required")
        if status not in {"PENDING", "SUBMITTED", "ACCEPTED", "REJECTED", "WAIVED"}:
            raise ValueError("Invalid checklist status")
        before = {"status": item.status, "rejection_reason": item.rejection_reason}
        item.status = status
        item.rejection_reason = rejection_reason if status == "REJECTED" else None
        if status in {"ACCEPTED", "REJECTED", "WAIVED"}:
            item.reviewed_by = user_id
            item.reviewed_at = datetime.now(UTC)
        self._session.flush()
        self._audit(
            user_id,
            "CHECKLIST_ITEM_UPDATED",
            "case_checklist_item",
            str(item_id),
            before,
            {"status": item.status, "rejection_reason": item.rejection_reason},
        )
        return item

    def list_form_templates(
        self,
        program_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[FormTemplate]:
        stmt = select(FormTemplate).where(FormTemplate.status == "PUBLISHED")
        if tenant_id is not None:
            stmt = stmt.where(
                or_(
                    FormTemplate.tenant_id.is_(None),
                    FormTemplate.tenant_id == tenant_id,
                )
            )
        if program_id:
            stmt = stmt.where(FormTemplate.program_id == program_id)
        return list(
            self._session.execute(stmt.order_by(FormTemplate.created_at.desc())).scalars()
        )

    def create_form_template(
        self,
        body: dict,
        user_id: str,
        tenant_id: str | None = None,
    ) -> FormTemplate:
        schema = body.get("schema_json", {})
        questions = schema.get("questions", []) if isinstance(schema, dict) else None
        if not isinstance(questions, list):
            raise TypeError("Form schema questions must be a list.")
        seen_keys: set[str] = set()
        for question in questions:
            if not isinstance(question, dict):
                raise TypeError("Each form question must be an object.")
            key = question.get("key")
            label = question.get("label")
            question_type = question.get("type", "text")
            if not isinstance(key, str) or not key.strip() or len(key) > 128:
                raise ValueError("Each form question needs a valid key.")
            if key in seen_keys:
                raise ValueError(f"Duplicate form question key: {key}")
            seen_keys.add(key)
            if not isinstance(label, str) or not label.strip() or len(label) > 200:
                raise ValueError(f"Form question {key} needs a valid label.")
            if question_type not in {"text", "number", "boolean", "date", "select"}:
                raise ValueError(f"Unsupported form question type for {key}.")
            if "required" in question and not isinstance(question["required"], bool):
                raise ValueError(f"Question {key} required must be a boolean.")
            if question_type == "select" and (
                not isinstance(question.get("options"), list)
                or not question["options"]
                or not all(isinstance(option, str) for option in question["options"])
            ):
                raise ValueError(f"Select question {key} needs string options.")
        template = FormTemplate(
            tenant_id=tenant_id,
            form_key=body["form_key"],
            version=body.get("version", 1),
            title=body["title"],
            program_id=body.get("program_id"),
            schema_json=body.get("schema_json", {}),
            status="PUBLISHED",
            created_by=user_id,
        )
        self._session.add(template)
        self._session.flush()
        self._audit(
            user_id,
            "FORM_TEMPLATE_CREATED",
            "form_template",
            str(template.template_id),
            None,
            {
                "form_key": template.form_key,
                "version": template.version,
                "program_id": template.program_id,
                "tenant_id": template.tenant_id,
            },
        )
        return template

    def list_form_answers(self, case_id: str, user_id: str) -> list[FormAnswer] | None:
        case = self.get_case(case_id, user_id)
        if case is None:
            return None
        return list(
            self._session.execute(
                select(FormAnswer).where(FormAnswer.case_id == case_id).order_by(FormAnswer.created_at.desc())
            ).scalars()
        )

    def upsert_form_answer(
        self,
        case_id: str,
        template_id: str,
        answers_json: dict,
        user_id: str,
        submit: bool = False,
    ) -> FormAnswer:
        case = self.get_case(case_id, user_id)
        if case is None:
            raise LookupError("Application case not found")
        membership = self.membership(case.tenant_id, user_id)
        if membership is None:
            raise PermissionError("Tenant membership required")
        if membership.role not in {"APPLICANT", "OWNER", "CONSULTANT", "REVIEWER"}:
            raise PermissionError("This tenant role cannot edit application forms")
        template = self._session.get(FormTemplate, template_id)
        if (
            template is None
            or template.status != "PUBLISHED"
            or template.program_id not in (None, case.program_id)
            or template.tenant_id not in (None, case.tenant_id)
        ):
            raise LookupError("Form template not found")
        if not isinstance(answers_json, dict):
            raise TypeError("Form answers must be an object.")
        questions = (template.schema_json or {}).get("questions", [])
        question_by_key = {question["key"]: question for question in questions}
        unexpected = set(answers_json) - set(question_by_key)
        if unexpected:
            raise ValueError(
                f"Unknown form answer fields: {', '.join(sorted(unexpected))}"
            )
        for key, value in answers_json.items():
            question = question_by_key[key]
            question_type = question.get("type", "text")
            valid = (
                (question_type == "text" and isinstance(value, str))
                or (
                    question_type == "number"
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                )
                or (question_type == "boolean" and isinstance(value, bool))
                or (question_type == "date" and isinstance(value, str))
                or (
                    question_type == "select"
                    and isinstance(value, str)
                    and value in question.get("options", [])
                )
            )
            if not valid:
                raise ValueError(f"Answer for {key} has the wrong type or value.")
        if submit:
            if membership.role != "APPLICANT":
                raise PermissionError("Only the applicant can submit this form.")
            missing = [
                question["key"]
                for question in questions
                if question.get("required")
                and answers_json.get(question["key"]) in (None, "", [], {})
            ]
            if missing:
                raise ValueError(
                    f"Required answers are missing: {', '.join(missing)}"
                )
        answer = self._session.execute(
            select(FormAnswer).where(
                FormAnswer.case_id == case_id,
                FormAnswer.template_id == template_id,
            )
        ).scalars().first()
        now = datetime.now(UTC)
        if answer is None:
            answer = FormAnswer(
                tenant_id=case.tenant_id,
                case_id=case_id,
                template_id=template_id,
                answers_json=answers_json,
                status="SUBMITTED" if submit else "DRAFT",
                submitted_at=now if submit else None,
                applicant_confirmed_at=None,
            )
            self._session.add(answer)
        else:
            if answer.status != "DRAFT":
                raise ValueError("Submitted or reviewed forms cannot be edited.")
            answer.answers_json = answers_json
            if submit:
                answer.status = "SUBMITTED"
                answer.submitted_at = now
        self._session.flush()
        self._audit(
            user_id,
            "FORM_ANSWER_SUBMITTED" if submit else "FORM_ANSWER_SAVED",
            "application_case",
            str(case_id),
            None,
            {"template_id": template_id, "status": answer.status},
        )
        return answer

    def submit_form_answer(self, case_id: str, template_id: str, user_id: str) -> FormAnswer:
        return self.upsert_form_answer(
            case_id, template_id, {}, user_id, submit=True
        )

    def confirm_form_answer(self, case_id: str, template_id: str, user_id: str) -> FormAnswer:
        case = self.get_case(case_id, user_id)
        if case is None:
            raise LookupError("Application case not found")
        answer = self._session.execute(
            select(FormAnswer).where(
                FormAnswer.case_id == case_id,
                FormAnswer.template_id == template_id,
            )
        ).scalars().first()
        if answer is None:
            raise LookupError("Form answer not found")
        membership = self.membership(case.tenant_id, user_id)
        if membership is None or membership.role != "APPLICANT":
            raise PermissionError("Applicant role required")
        if answer.status != "SUBMITTED":
            raise ValueError("Form answer must be submitted before confirmation")
        missing = self._missing_form_answer_fields(answer)
        if missing:
            raise ValueError(
                f"Required answers are missing: {', '.join(missing)}"
            )
        answer.applicant_confirmed_at = datetime.now(UTC)
        self._session.flush()
        self._audit(
            user_id,
            "FORM_ANSWER_CONFIRMED",
            "application_case",
            str(case_id),
            None,
            {"template_id": template_id},
        )
        return answer

    def review_form_answer(
        self,
        case_id: str,
        template_id: str,
        user_id: str,
        status: str,
        review_notes: str | None = None,
    ) -> FormAnswer:
        case = self.get_case(case_id, user_id)
        if case is None:
            raise LookupError("Application case not found")
        answer = self._session.execute(
            select(FormAnswer).where(
                FormAnswer.case_id == case_id,
                FormAnswer.template_id == template_id,
            )
        ).scalars().first()
        if answer is None:
            raise LookupError("Form answer not found")
        membership = self.membership(case.tenant_id, user_id)
        if membership is None or membership.role not in {"OWNER", "CONSULTANT", "REVIEWER"}:
            raise PermissionError("Tenant staff role required")
        if status not in {"REVIEWED", "APPROVED"}:
            raise ValueError("Invalid review status")
        if status == "APPROVED" and answer.applicant_confirmed_at is None:
            raise ValueError("Applicant confirmation is required before approval.")
        before = {"status": answer.status}
        answer.status = status
        answer.reviewed_by = user_id
        answer.reviewed_at = datetime.now(UTC)
        answer.review_notes = review_notes
        self._session.flush()
        self._audit(
            user_id,
            "FORM_ANSWER_REVIEWED",
            "application_case",
            str(case_id),
            before,
            {"template_id": template_id, "status": status},
        )
        return answer

    def list_case_activities(self, case_id: str, user_id: str) -> list[dict]:
        case = self.get_case(case_id, user_id)
        if case is None:
            raise LookupError("Application case not found")
        rows = self._session.execute(
            select(AuditLog, ApiUser)
            .outerjoin(ApiUser, ApiUser.user_id == AuditLog.user_id)
            .where(
                AuditLog.entity_type == "application_case",
                AuditLog.entity_id == str(case_id),
            )
            .order_by(AuditLog.created_at.asc())
        ).all()
        return [
            {
                "audit_id": log.audit_id,
                "action": log.action,
                "entity_type": log.entity_type,
                "entity_id": log.entity_id,
                "before_json": log.before_json,
                "after_json": log.after_json,
                "user_id": log.user_id,
                "user_name": user.full_name if user else None,
                "created_at": log.created_at,
            }
            for log, user in rows
        ]

    def record_case_activity(
        self,
        case_id: str,
        user_id: str,
        action: str,
        before: dict | None,
        after: dict | None,
    ) -> None:
        case = self.get_case(case_id, user_id)
        if case is None:
            raise LookupError("Application case not found")
        self._audit(
            user_id,
            action,
            "application_case",
            str(case.case_id),
            before,
            after,
        )

    def _missing_form_answer_fields(self, answer: FormAnswer) -> list[str]:
        template = self._session.get(FormTemplate, answer.template_id)
        if template is None:
            return []
        questions = (template.schema_json or {}).get("questions", [])
        answers = answer.answers_json or {}
        missing: list[str] = []
        for question in questions:
            key = question.get("key")
            if not key:
                continue
            if question.get("required") and (
                key not in answers
                or answers[key] in (None, "", [], {})
            ):
                missing.append(key)
        return missing

    def dashboard_summary(self, tenant_id: str, user_id: str) -> dict:
        membership = self.membership(tenant_id, user_id)
        if membership is None or membership.role not in {"OWNER", "CONSULTANT", "REVIEWER"}:
            raise PermissionError("Tenant staff role required")
        stmt = select(ApplicationCase).where(ApplicationCase.tenant_id == tenant_id)
        if membership.role == "CONSULTANT":
            stmt = stmt.where(ApplicationCase.assigned_consultant_id == user_id)
        cases = list(self._session.execute(stmt).scalars())
        by_status: dict[str, int] = {}
        workload: dict[str, int] = {}
        for case in cases:
            by_status[case.status] = by_status.get(case.status, 0) + 1
            if case.assigned_consultant_id:
                workload[str(case.assigned_consultant_id)] = (
                    workload.get(str(case.assigned_consultant_id), 0) + 1
                )
        return {
            "case_count": len(cases),
            "by_status": by_status,
            "consultant_workload": [
                {"consultant_id": cid, "case_count": count}
                for cid, count in sorted(workload.items(), key=lambda item: -item[1])
            ],
        }


def case_payload(case: ApplicationCase) -> dict:
    return {
        "case_id": case.case_id,
        "tenant_id": case.tenant_id,
        "applicant_id": case.applicant_id,
        "program_id": case.program_id,
        "created_by": case.created_by,
        "assigned_consultant_id": case.assigned_consultant_id,
        "assessment_id": case.assessment_id,
        "ruleset_id": case.ruleset_id,
        "status": case.status,
        "title": case.title,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
    }

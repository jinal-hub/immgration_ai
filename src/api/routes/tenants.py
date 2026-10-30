"""Consultancy tenant management and tenant-scoped application cases."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError

from api.helpers import parse_uuid_or_404
from api.routes.auth import get_current_user_id
from api.schemas.models import (
    AcceptInvitationRequest,
    ApplicationCaseCreateRequest,
    ApplicationCaseResponse,
    CaseActivityResponse,
    CaseAssignRequest,
    CaseDashboardResponse,
    FormTemplateCreate,
    FormTemplateResponse,
    TenantApplicantResponse,
    TenantCreateRequest,
    TenantInvitationCreate,
    TenantInvitationResponse,
    TenantMemberCreateRequest,
    TenantMemberResponse,
    TenantMemberUpdate,
    TenantResponse,
    UnassignedCaseResponse,
)
from db.connection import get_db_engine, get_session
from db.models import ApplicationCase
from services.tenant_service import TenantService, case_payload

router = APIRouter()


def _require_membership(service: TenantService, tenant_id: str, user_id: str):
    membership = service.membership(tenant_id, user_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return membership


@router.get("", response_model=list[TenantResponse])
def list_tenants(
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    with get_session(get_db_engine(request)) as session:
        return TenantService(session).list_for_user(user_id)


@router.post("", response_model=TenantResponse, status_code=201)
def create_tenant(
    body: TenantCreateRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    try:
        with get_session(get_db_engine(request)) as session:
            tenant = TenantService(session).create_tenant(
                user_id, body.name, body.slug,
            )
            return {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "slug": tenant.slug,
                "role": "OWNER",
                "status": tenant.status,
            }
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A consultancy with this slug already exists.",
        ) from exc


@router.get("/{tenant_id}/members", response_model=list[TenantMemberResponse])
def list_tenant_members(
    tenant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        membership = _require_membership(service, tenant_id, user_id)
        if membership.role == "APPLICANT":
            raise HTTPException(
                status_code=403,
                detail="Tenant staff role required to view memberships",
            )
        return service.list_members(tenant_id)


@router.post(
    "/{tenant_id}/members",
    response_model=TenantMemberResponse,
    status_code=201,
)
def add_tenant_member(
    tenant_id: str,
    body: TenantMemberCreateRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        membership = _require_membership(service, tenant_id, user_id)
        if membership.role != "OWNER":
            raise HTTPException(status_code=403, detail="Tenant owner role required")
        try:
            added = service.add_member(tenant_id, body.email, body.role)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if added is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        response = next(
            item for item in service.list_members(tenant_id)
            if item["membership_id"] == added.membership_id
        )
        return response


@router.get(
    "/{tenant_id}/cases",
    response_model=list[ApplicationCaseResponse],
)
def list_tenant_cases(
    tenant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        try:
            cases = TenantService(session).list_cases(tenant_id, user_id)
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail="Tenant not found") from exc
        return [case_payload(case) for case in cases]


@router.get(
    "/{tenant_id}/unassigned-cases",
    response_model=list[UnassignedCaseResponse],
)
def list_unassigned_tenant_cases(
    tenant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        try:
            cases = TenantService(session).list_unassigned_cases(tenant_id, user_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return [
            {
                "case_id": case.case_id,
                "tenant_id": case.tenant_id,
                "program_id": case.program_id,
                "status": case.status,
                "title": case.title,
                "created_at": case.created_at,
            }
            for case in cases
        ]


@router.post(
    "/{tenant_id}/cases",
    response_model=ApplicationCaseResponse,
    status_code=201,
)
def create_tenant_case(
    tenant_id: str,
    body: ApplicationCaseCreateRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    parse_uuid_or_404(body.applicant_id)
    with get_session(get_db_engine(request)) as session:
        try:
            case = TenantService(session).create_case(
                tenant_id=tenant_id,
                user_id=user_id,
                applicant_id=body.applicant_id,
                program_id=body.program_id,
                title=body.title,
                assessment_id=body.assessment_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return case_payload(case)


@router.get(
    "/{tenant_id}/dashboard",
    response_model=CaseDashboardResponse,
)
def get_tenant_dashboard(
    tenant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        membership = _require_membership(service, tenant_id, user_id)
        if membership.role == "APPLICANT":
            raise HTTPException(status_code=403, detail="Tenant staff role required")
        try:
            return service.dashboard_summary(tenant_id, user_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get(
    "/{tenant_id}/applicants",
    response_model=list[TenantApplicantResponse],
)
def list_tenant_applicants(
    tenant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        applicants = TenantService(session).list_applicants_for_tenant(
            tenant_id, user_id
        )
        if applicants is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return applicants


@router.get(
    "/{tenant_id}/invitations",
    response_model=list[TenantInvitationResponse],
)
def list_tenant_invitations(
    tenant_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        membership = _require_membership(service, tenant_id, user_id)
        if membership.role != "OWNER":
            raise HTTPException(
                status_code=403, detail="Tenant owner role required"
            )
        return service.list_invitations(tenant_id)


@router.post(
    "/{tenant_id}/invitations",
    response_model=TenantInvitationResponse,
    status_code=201,
)
def create_tenant_invitation(
    tenant_id: str,
    body: TenantInvitationCreate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        membership = _require_membership(service, tenant_id, user_id)
        if membership.role != "OWNER":
            raise HTTPException(
                status_code=403, detail="Tenant owner role required"
            )
        try:
            invitation, raw_token = service.invite_member(
                tenant_id, body.email, body.role, user_id
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "invitation_id": invitation.invitation_id,
            "tenant_id": invitation.tenant_id,
            "email": invitation.email,
            "role": invitation.role,
            "invitation_token": raw_token,
            "expires_at": invitation.expires_at,
            "accepted_at": invitation.accepted_at,
            "revoked_at": invitation.revoked_at,
            "status": service._invitation_status(invitation),
        }


@router.delete(
    "/{tenant_id}/invitations/{invitation_id}",
    status_code=204,
)
def revoke_tenant_invitation(
    tenant_id: str,
    invitation_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    parse_uuid_or_404(invitation_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        membership = _require_membership(service, tenant_id, user_id)
        if membership.role != "OWNER":
            raise HTTPException(
                status_code=403, detail="Tenant owner role required"
            )
        try:
            service.revoke_invitation(tenant_id, invitation_id, user_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)


@router.post("/invitations/accept", status_code=200)
def accept_tenant_invitation(
    body: AcceptInvitationRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        try:
            membership = service.accept_invitation(body.token, user_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {
            "membership_id": membership.membership_id,
            "tenant_id": membership.tenant_id,
            "role": membership.role,
            "status": membership.status,
        }


@router.patch(
    "/{tenant_id}/members/{membership_id}",
    response_model=TenantMemberResponse,
)
def update_tenant_member(
    tenant_id: str,
    membership_id: str,
    body: TenantMemberUpdate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    parse_uuid_or_404(membership_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        _require_membership(service, tenant_id, user_id)
        try:
            updated = service.update_member_role(
                tenant_id, membership_id, body.role, user_id
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        row = next(
            item
            for item in service.list_members(tenant_id)
            if item["membership_id"] == updated.membership_id
        )
        return row


@router.delete(
    "/{tenant_id}/members/{membership_id}",
    status_code=204,
)
def revoke_tenant_member(
    tenant_id: str,
    membership_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    parse_uuid_or_404(membership_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        _require_membership(service, tenant_id, user_id)
        try:
            service.revoke_member(tenant_id, membership_id, user_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)


@router.post(
    "/{tenant_id}/cases/{case_id}/assign",
    response_model=ApplicationCaseResponse,
)
def assign_case(
    tenant_id: str,
    case_id: str,
    body: CaseAssignRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(body.consultant_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        case = session.get(ApplicationCase, case_id)
        if case is None or case.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Application case not found")
        try:
            updated = service.assign_case(case_id, body.consultant_id, user_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return case_payload(updated)


@router.get(
    "/{tenant_id}/cases/{case_id}/activities",
    response_model=list[CaseActivityResponse],
)
def list_case_activities(
    tenant_id: str,
    case_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        case = service.get_case(case_id, user_id)
        if case is None or case.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Application case not found")
        return service.list_case_activities(case_id, user_id)


@router.get(
    "/{tenant_id}/form-templates",
    response_model=list[FormTemplateResponse],
)
def list_tenant_form_templates(
    tenant_id: str,
    request: Request,
    program_id: str | None = Query(default=None),
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        _require_membership(service, tenant_id, user_id)
        return [
            {
                "template_id": template.template_id,
                "tenant_id": template.tenant_id,
                "form_key": template.form_key,
                "version": template.version,
                "title": template.title,
                "program_id": template.program_id,
                "schema_json": template.schema_json,
                "status": template.status,
                "created_by": template.created_by,
                "created_at": template.created_at,
            }
            for template in service.list_form_templates(program_id, tenant_id)
        ]


@router.post(
    "/{tenant_id}/form-templates",
    response_model=FormTemplateResponse,
    status_code=201,
)
def create_tenant_form_template(
    tenant_id: str,
    body: FormTemplateCreate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(tenant_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        membership = _require_membership(service, tenant_id, user_id)
        if membership.role != "OWNER":
            raise HTTPException(
                status_code=403, detail="Tenant owner role required"
            )
        try:
            template = service.create_form_template(
                {
                    "form_key": body.form_key,
                    "version": body.version,
                    "title": body.title,
                    "program_id": body.program_id,
                    "schema_json": body.schema_json,
                },
                user_id,
                tenant_id,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="A form template with this key and version already exists.",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TypeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "template_id": template.template_id,
            "tenant_id": template.tenant_id,
            "form_key": template.form_key,
            "version": template.version,
            "title": template.title,
            "program_id": template.program_id,
            "schema_json": template.schema_json,
            "status": template.status,
            "created_by": template.created_by,
            "created_at": template.created_at,
        }

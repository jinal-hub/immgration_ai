"""Tenant-authorized application-case routes."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from api.helpers import parse_uuid_or_404
from api.routes.auth import get_current_user_id
from api.schemas.models import (
    ApplicationCaseResponse,
    ApplicationCaseStatusUpdate,
    CaseChecklistGenerateRequest,
    CaseChecklistItemResponse,
    CaseDocumentDetailResponse,
    CaseDocumentReviewRequest,
    ChecklistItemUpdate,
    FormAnswerCreate,
    FormAnswerResponse,
    FormReviewRequest,
)
from db.connection import get_db_engine, get_session
from db.models import (
    ApplicantProfile,
    Assessment,
    CaseChecklistItem,
    CaseDocument,
    ConsultancyTenant,
    FormAnswer,
    Gap,
)
from services.assessment_service import AssessmentService
from services.case_document_scanner import DocumentScanError, scan_document
from services.case_document_storage import CaseDocumentStorage, DocumentStorageError
from services.case_package_service import (
    build_application_package_bytes,
    build_package_bytes,
)
from services.tenant_service import TenantService, case_payload

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_PACKAGE_DOCUMENT_BYTES = 50 * 1024 * 1024
_ALLOWED_DOCUMENTS = {
    ".pdf": ("application/pdf", lambda value: value.startswith(b"%PDF-")),
    ".png": ("image/png", lambda value: value.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image/jpeg", lambda value: value.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda value: value.startswith(b"\xff\xd8\xff")),
}


def _document_payload(document: CaseDocument) -> dict:
    return {
        "document_id": document.document_id,
        "case_id": document.case_id,
        "tenant_id": document.tenant_id,
        "uploaded_by": document.uploaded_by,
        "filename": document.filename,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
        "sha256": document.sha256,
        "review_status": document.review_status,
        "rejection_reason": document.rejection_reason,
        "reviewed_by": document.reviewed_by,
        "reviewed_at": document.reviewed_at,
        "version": document.version,
        "scanner_status": document.scanner_status,
        "scanner_detail": document.scanner_detail,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


def _storage() -> CaseDocumentStorage:
    try:
        return CaseDocumentStorage()
    except RuntimeError as exc:
        logger.error("Applicant document storage is misconfigured: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Applicant document storage is not configured.",
        ) from exc


def _validated_upload(
    original_filename: str,
    declared_content_type: str | None,
    content: bytes,
) -> tuple[str, str]:
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded document is empty.")
    if len(content) > _MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="Document exceeds the 10 MiB limit.")

    raw_filename = original_filename.replace("\\", "/")
    filename = PurePosixPath(raw_filename).name
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename).strip()
    if not filename or len(filename) > 256:
        raise HTTPException(status_code=422, detail="Invalid document filename.")

    extension = PurePosixPath(filename).suffix.lower()
    file_type = _ALLOWED_DOCUMENTS.get(extension)
    if file_type is None or not file_type[1](content):
        raise HTTPException(
            status_code=415,
            detail="Only valid PDF, PNG, and JPEG documents are accepted.",
        )
    content_type = file_type[0]
    if declared_content_type not in (
        None, "", "application/octet-stream", content_type,
    ):
        raise HTTPException(
            status_code=415,
            detail="Declared content type does not match the document.",
        )
    return filename, content_type


@router.get("/{case_id}", response_model=ApplicationCaseResponse)
def get_case(
    case_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        case = TenantService(session).get_case(case_id, user_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        return case_payload(case)


@router.patch("/{case_id}/status", response_model=ApplicationCaseResponse)
def update_case_status(
    case_id: str,
    body: ApplicationCaseStatusUpdate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        try:
            case = TenantService(session).update_case_status(
                case_id, user_id, body.status,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return case_payload(case)


@router.get("/{case_id}/assessment")
def get_case_assessment(
    case_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        case = TenantService(session).get_case(case_id, user_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        if case.assessment_id is None:
            raise HTTPException(
                status_code=404,
                detail="No eligibility assessment is linked to this case.",
            )
        assessment = AssessmentService(session).get(case.assessment_id)
        if (
            assessment is None
            or assessment["applicant_id"] != case.applicant_id
            or assessment["program_id"] != case.program_id
        ):
            raise HTTPException(
                status_code=404,
                detail="Linked eligibility assessment was not found.",
            )
        return assessment


@router.post(
    "/{case_id}/documents",
    response_model=CaseDocumentDetailResponse,
    status_code=201,
)
async def upload_case_document(
    case_id: str,
    request: Request,
    filename: str,
    checklist_item_id: str | None = Query(default=None),
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    if checklist_item_id is not None:
        parse_uuid_or_404(checklist_item_id)
    with get_session(get_db_engine(request)) as session:
        case = TenantService(session).get_case(case_id, user_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        tenant_id = case.tenant_id

    content_buffer = bytearray()
    async for chunk in request.stream():
        content_buffer.extend(chunk)
        if len(content_buffer) > _MAX_DOCUMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Document exceeds the 10 MiB limit.",
            )
    content = bytes(content_buffer)
    filename, content_type = _validated_upload(
        filename,
        request.headers.get("content-type"),
        content,
    )
    storage = _storage()
    version = 1
    replaces_document_id = None
    if checklist_item_id is not None:
        with get_session(get_db_engine(request)) as session:
            item = session.execute(
                select(CaseChecklistItem).where(
                    CaseChecklistItem.item_id == checklist_item_id,
                    CaseChecklistItem.case_id == case_id,
                )
            ).scalar_one_or_none()
            if item is None:
                raise HTTPException(
                    status_code=404, detail="Checklist item not found for this case"
                )
            current = session.execute(
                select(CaseDocument)
                .where(
                    CaseDocument.case_id == case_id,
                    CaseDocument.checklist_item_id == checklist_item_id,
                )
                .order_by(CaseDocument.version.desc())
            ).scalars().first()
            if current is not None:
                version = current.version + 1
                replaces_document_id = current.document_id

    try:
        scan_result = scan_document(
            getattr(request.app.state, "document_scanner", None),
            content,
        )
    except DocumentScanError as exc:
        logger.error("Applicant document malware scan failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Document could not be safely scanned. Try again later.",
        ) from exc
    if scan_result.status == "INFECTED":
        raise HTTPException(
            status_code=422,
            detail="The uploaded document failed malware scanning.",
        )

    try:
        storage_key = storage.store(tenant_id, content, content_type)
    except DocumentStorageError as exc:
        logger.exception("Could not store applicant case document")
        raise HTTPException(
            status_code=503,
            detail="Document storage is temporarily unavailable.",
        ) from exc

    try:
        with get_session(get_db_engine(request)) as session:
            service = TenantService(session)
            case = service.get_case(case_id, user_id)
            if case is None:
                raise HTTPException(
                    status_code=404, detail="Application case not found",
                )
            document = CaseDocument(
                tenant_id=case.tenant_id,
                case_id=case.case_id,
                uploaded_by=user_id,
                filename=filename,
                content_type=content_type,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                storage_key=storage_key,
                review_status="PENDING_REVIEW",
                checklist_item_id=checklist_item_id,
                version=version,
                replaces_document_id=replaces_document_id,
                scanner_status=scan_result.status,
                scanner_detail=scan_result.detail,
            )
            session.add(document)
            session.flush()
            if checklist_item_id is not None:
                item = session.get(CaseChecklistItem, checklist_item_id)
                if item is not None:
                    item.status = "SUBMITTED"
                    item.evidence_document_id = document.document_id
            service.record_case_activity(
                case_id,
                user_id,
                "CASE_DOCUMENT_UPLOADED",
                None,
                {
                    "document_id": document.document_id,
                    "filename": document.filename,
                    "version": document.version,
                    "scanner_status": document.scanner_status,
                },
            )
            payload = _document_payload(document)
            session.commit()
            return payload
    except (SQLAlchemyError, HTTPException) as exc:
        try:
            storage.delete(storage_key)
        except DocumentStorageError:
            logger.exception("Orphaned applicant case document needs cleanup")
        if isinstance(exc, HTTPException):
            raise
        logger.exception("Could not record uploaded applicant case document")
        raise HTTPException(
            status_code=503,
            detail="Could not record the uploaded document.",
        ) from exc


@router.get(
    "/{case_id}/documents",
    response_model=list[CaseDocumentDetailResponse],
)
def list_case_documents(
    case_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        case = TenantService(session).get_case(case_id, user_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        documents = session.execute(
            select(CaseDocument)
            .where(
                CaseDocument.case_id == case.case_id,
                CaseDocument.tenant_id == case.tenant_id,
            )
            .order_by(CaseDocument.created_at.desc())
        ).scalars()
        return [_document_payload(document) for document in documents]


@router.get("/{case_id}/documents/{document_id}")
def download_case_document(
    case_id: str,
    document_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(document_id)
    with get_session(get_db_engine(request)) as session:
        case = TenantService(session).get_case(case_id, user_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        document = session.execute(
            select(CaseDocument).where(
                CaseDocument.document_id == document_id,
                CaseDocument.case_id == case.case_id,
                CaseDocument.tenant_id == case.tenant_id,
            )
        ).scalar_one_or_none()
        if document is None:
            raise HTTPException(status_code=404, detail="Case document not found")
        storage_key = document.storage_key
        filename = document.filename
        content_type = document.content_type

    try:
        content = _storage().retrieve(storage_key)
    except DocumentStorageError as exc:
        logger.exception("Could not retrieve applicant case document")
        raise HTTPException(
            status_code=503,
            detail="Document storage is temporarily unavailable.",
        ) from exc
    return StreamingResponse(
        iter([content]),
        media_type=content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/{case_id}/documents/{document_id}", status_code=204)
def delete_case_document(
    case_id: str,
    document_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(document_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        case = service.get_case(case_id, user_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        document = session.execute(
            select(CaseDocument).where(
                CaseDocument.document_id == document_id,
                CaseDocument.case_id == case.case_id,
                CaseDocument.tenant_id == case.tenant_id,
            )
        ).scalar_one_or_none()
        if document is None:
            raise HTTPException(status_code=404, detail="Case document not found")
        membership = service.membership(case.tenant_id, user_id)
        if (
            document.uploaded_by != user_id
            and (membership is None or membership.role != "OWNER")
        ):
            raise HTTPException(
                status_code=403,
                detail="Only the uploader or tenant owner can delete this document.",
            )
        storage_key = document.storage_key
        session.delete(document)
        session.flush()
        session.commit()

    try:
        _storage().delete(storage_key)
    except DocumentStorageError as exc:
        logger.exception("Deleted document metadata but could not delete stored object")
        raise HTTPException(
            status_code=503,
            detail="Document record was deleted but stored data needs cleanup.",
        ) from exc
    return Response(status_code=204)


@router.patch(
    "/{case_id}/documents/{document_id}/review",
    response_model=CaseDocumentDetailResponse,
)
def review_case_document(
    case_id: str,
    document_id: str,
    body: CaseDocumentReviewRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(document_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        case = service.get_case(case_id, user_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        membership = service.membership(case.tenant_id, user_id)
        if membership is None or membership.role not in {
            "OWNER", "CONSULTANT", "REVIEWER",
        }:
            raise HTTPException(
                status_code=403,
                detail="Tenant staff role required to review documents.",
            )
        document = session.execute(
            select(CaseDocument).where(
                CaseDocument.document_id == document_id,
                CaseDocument.case_id == case.case_id,
                CaseDocument.tenant_id == case.tenant_id,
            )
        ).scalar_one_or_none()
        if document is None:
            raise HTTPException(status_code=404, detail="Case document not found")
        if body.review_status == "REJECTED" and not body.rejection_reason:
            raise HTTPException(
                status_code=422,
                detail="A rejection reason is required when rejecting a document.",
            )
        before = {
            "review_status": document.review_status,
            "rejection_reason": document.rejection_reason,
        }
        document.review_status = body.review_status
        document.reviewed_by = user_id
        document.reviewed_at = datetime.now(UTC)
        document.rejection_reason = (
            body.rejection_reason if body.review_status == "REJECTED" else None
        )
        if document.checklist_item_id is not None:
            item = session.get(CaseChecklistItem, document.checklist_item_id)
            if item is not None:
                item.status = (
                    "ACCEPTED" if body.review_status == "ACCEPTED" else "REJECTED"
                )
                item.rejection_reason = (
                    body.rejection_reason if body.review_status == "REJECTED" else None
                )
                item.reviewed_by = user_id
                item.reviewed_at = datetime.now(UTC)
        service.record_case_activity(
            case_id,
            user_id,
            "CASE_DOCUMENT_REVIEWED",
            before,
            {
                "document_id": document.document_id,
                "review_status": document.review_status,
                "rejection_reason": document.rejection_reason,
            },
        )
        session.flush()
        return _document_payload(document)


@router.get(
    "/{case_id}/checklist",
    response_model=list[CaseChecklistItemResponse],
)
def list_case_checklist(
    case_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        items = service.list_checklist(case_id, user_id)
        if items is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        return [_checklist_payload(item) for item in items]


@router.post(
    "/{case_id}/checklist/generate",
    response_model=list[CaseChecklistItemResponse],
)
def generate_case_checklist(
    case_id: str,
    body: CaseChecklistGenerateRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        try:
            items = service.generate_checklist(
                case_id, user_id, body.assessment_id
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return [_checklist_payload(item) for item in items]


@router.patch(
    "/{case_id}/checklist/{item_id}",
    response_model=CaseChecklistItemResponse,
)
def update_case_checklist_item(
    case_id: str,
    item_id: str,
    body: ChecklistItemUpdate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(item_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        try:
            item = service.update_checklist_item(
                case_id, item_id, user_id, body.status, body.rejection_reason
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _checklist_payload(item)


def _checklist_payload(item: CaseChecklistItem) -> dict:
    return {
        "item_id": item.item_id,
        "case_id": item.case_id,
        "tenant_id": item.tenant_id,
        "requirement_key": item.requirement_key,
        "label": item.label,
        "description": item.description,
        "category": item.category,
        "required": item.required,
        "status": item.status,
        "evidence_document_id": item.evidence_document_id,
        "rejection_reason": item.rejection_reason,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at,
        "position": item.position,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


@router.get(
    "/{case_id}/forms",
    response_model=list[FormAnswerResponse],
)
def list_case_forms(
    case_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        answers = service.list_form_answers(case_id, user_id)
        if answers is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        return [_form_answer_payload(service, answer) for answer in answers]


@router.post(
    "/{case_id}/forms/{template_id}",
    response_model=FormAnswerResponse,
)
def save_case_form(
    case_id: str,
    template_id: str,
    body: FormAnswerCreate,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(template_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        try:
            answer = service.upsert_form_answer(
                case_id, template_id, body.answers_json, user_id, submit=False
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TypeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _form_answer_payload(service, answer)


@router.post(
    "/{case_id}/forms/{template_id}/submit",
    response_model=FormAnswerResponse,
)
def submit_case_form(
    case_id: str,
    template_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(template_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        existing = service.list_form_answers(case_id, user_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        answer = next(
            (item for item in existing if item.template_id == template_id), None
        )
        answers_json = answer.answers_json if answer else {}
        try:
            updated = service.upsert_form_answer(
                case_id, template_id, answers_json, user_id, submit=True
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TypeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _form_answer_payload(service, updated)


@router.post(
    "/{case_id}/forms/{template_id}/confirm",
    response_model=FormAnswerResponse,
)
def confirm_case_form(
    case_id: str,
    template_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(template_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        try:
            updated = service.confirm_form_answer(case_id, template_id, user_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _form_answer_payload(service, updated)


@router.post(
    "/{case_id}/forms/{template_id}/review",
    response_model=FormAnswerResponse,
)
def review_case_form(
    case_id: str,
    template_id: str,
    body: FormReviewRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    parse_uuid_or_404(template_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        try:
            updated = service.review_form_answer(
                case_id, template_id, user_id, body.status, body.review_notes
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _form_answer_payload(service, updated)


def _form_answer_payload(
    service: TenantService, answer: FormAnswer
) -> dict:
    missing = service._missing_form_answer_fields(answer) if hasattr(
        service, "_missing_form_answer_fields"
    ) else []
    return {
        "answer_id": answer.answer_id,
        "case_id": answer.case_id,
        "tenant_id": answer.tenant_id,
        "template_id": answer.template_id,
        "answers_json": answer.answers_json,
        "status": answer.status,
        "submitted_at": answer.submitted_at,
        "applicant_confirmed_at": answer.applicant_confirmed_at,
        "reviewed_by": answer.reviewed_by,
        "reviewed_at": answer.reviewed_at,
        "review_notes": answer.review_notes,
        "missing_fields": missing,
        "created_at": answer.created_at,
        "updated_at": answer.updated_at,
    }


@router.get("/{case_id}/package")
def download_case_package(
    case_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
):
    parse_uuid_or_404(case_id)
    with get_session(get_db_engine(request)) as session:
        service = TenantService(session)
        case = service.get_case(case_id, user_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Application case not found")
        tenant = session.get(ConsultancyTenant, case.tenant_id)
        applicant = session.get(ApplicantProfile, case.applicant_id)
        document_rows = list(
            session.execute(
                select(CaseDocument)
                .where(
                    CaseDocument.case_id == case.case_id,
                    CaseDocument.tenant_id == case.tenant_id,
                )
                .order_by(CaseDocument.created_at.desc())
            ).scalars()
        )
        documents = [_document_payload(document) for document in document_rows]
        accepted_document_metadata = [
            {
                "document_id": document.document_id,
                "filename": document.filename,
                "storage_key": document.storage_key,
                "size_bytes": document.size_bytes,
            }
            for document in document_rows
            if document.review_status == "ACCEPTED"
        ]
        document_manifest = [
            {
                "document_id": document.document_id,
                "filename": document.filename,
                "review_status": document.review_status,
                "version": document.version,
                "size_bytes": document.size_bytes,
                "sha256": document.sha256,
                "scanner_status": document.scanner_status,
                "included_in_package": document.review_status == "ACCEPTED",
            }
            for document in document_rows
        ]
        checklist = [
            _checklist_payload(item)
            for item in service.list_checklist(case_id, user_id) or []
        ]
        answers = service.list_form_answers(case_id, user_id) or []
        forms = [
            _form_answer_payload(service, answer) for answer in answers
        ]
        assessment = None
        if case.assessment_id:
            assessment_orm = session.get(Assessment, case.assessment_id)
            if assessment_orm is not None:
                gaps = [
                    {
                        "field": gap.field,
                        "message": gap.message,
                        "priority": gap.priority,
                    }
                    for gap in session.execute(
                        select(Gap).where(Gap.assessment_id == assessment_orm.assessment_id)
                    ).scalars()
                ]
                assessment = {
                    "business_status": assessment_orm.business_status,
                    "overall_status": assessment_orm.overall_status,
                    "passed_count": assessment_orm.passed_count,
                    "failed_count": assessment_orm.failed_count,
                    "unknown_count": assessment_orm.unknown_count,
                    "gaps": gaps,
                }
        summary_pdf = build_package_bytes(
            case,
            applicant,
            tenant.name if tenant else "Consultancy",
            documents,
            checklist,
            forms,
            assessment,
        )

    total_document_bytes = sum(
        document["size_bytes"] for document in accepted_document_metadata
    )
    if total_document_bytes > _MAX_PACKAGE_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Accepted case documents exceed the 50 MiB package limit.",
        )
    accepted_documents = []
    if accepted_document_metadata:
        storage = _storage()
        for document in accepted_document_metadata:
            try:
                content = storage.retrieve(document["storage_key"])
            except DocumentStorageError as exc:
                logger.exception("Could not retrieve a document for case package")
                raise HTTPException(
                    status_code=503,
                    detail="A case document is temporarily unavailable.",
                ) from exc
            accepted_documents.append(
                {
                    "document_id": document["document_id"],
                    "filename": document["filename"],
                    "content": content,
                }
            )
    package_bytes = build_application_package_bytes(
        summary_pdf,
        accepted_documents,
        document_manifest,
    )
    return StreamingResponse(
        iter([package_bytes]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="case-{case_id}-package.zip"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )

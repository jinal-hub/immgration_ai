"""Draft application package generation for consultancy cases.

Produces a simple PDF from stored case data. Generated packages are
preparation aids and are explicitly marked as draft material, not official
government forms.
"""
from __future__ import annotations

import io
import json
import re
import zipfile

from db.models import ApplicantProfile, ApplicationCase


def _escape_pdf_text(text: str) -> str:
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\\", "\\\\")
    text = text.replace("(", "\\(")
    text = text.replace(")", "\\)")
    return text


def _wrap(text: str, font_size: float, max_width: float) -> list[str]:
    if not text:
        return [""]
    chars_per_line = max(1, int(max_width / (font_size * 0.52)))
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        if len(trial) <= chars_per_line:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def build_package_bytes(
    case: ApplicationCase,
    applicant: ApplicantProfile | None,
    tenant_name: str,
    documents: list[dict],
    checklist: list[dict],
    forms: list[dict],
    assessment: dict | None,
) -> bytes:
    font_size = 10.5
    leading = 13
    margin_x = 50
    max_width = 612 - 2 * margin_x
    start_y = 740
    bottom_y = 60

    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)

    def section(title: str) -> None:
        emit("")
        emit(f"=== {title.upper()} ===")

    emit("IMMIGRATION KNOWLEDGE PATHWAY")
    emit("DRAFT APPLICATION PACKAGE")
    emit("")
    emit("This package is a draft preparation aid produced by the consultancy workspace.")
    emit("It is not an official Australian Government form and does not constitute a visa")
    emit("application or a migration decision.")
    emit("")
    emit(f"Case ID: {case.case_id}")
    emit(f"Consultancy/Tenant: {tenant_name}")
    emit(f"Program: {case.program_id}")
    emit(f"Status: {case.status}")
    emit(f"Title: {case.title or 'Not provided'}")
    emit(f"Assigned consultant: {case.assigned_consultant_id or 'Not assigned'}")
    if applicant:
        emit(f"Applicant: {applicant.first_name or ''} {applicant.last_name or ''}".strip())
        emit(f"Applicant ID: {applicant.applicant_id}")
        emit(f"Email: {applicant.user_id}")
    emit("")

    section("Assessment")
    if assessment:
        emit(f"Business status: {assessment.get('business_status', 'Not assessed')}")
        emit(f"Overall status: {assessment.get('overall_status', 'Not assessed')}")
        emit(
            f"Rules: {assessment.get('passed_count', 0)} passed, "
            f"{assessment.get('failed_count', 0)} not met, "
            f"{assessment.get('unknown_count', 0)} need answers"
        )
        gaps = assessment.get("gaps") or []
        for gap in gaps:
            emit(f"  - {gap.get('field', 'gap')}: {gap.get('message', '')}")
    else:
        emit("No eligibility assessment linked to this case.")
    emit("")

    section("Checklist")
    if checklist:
        for item in checklist:
            mark = "[x]" if item.get("status") == "ACCEPTED" else "[ ]"
            emit(
                f"{mark} {item.get('label', item.get('requirement_key', 'Item'))}"
            )
            if item.get("description"):
                emit(f"      {item['description']}")
            if item.get("status") == "REJECTED" and item.get("rejection_reason"):
                emit(f"      Rejected: {item['rejection_reason']}")
    else:
        emit("No checklist items.")
    emit("")

    section("Documents")
    if documents:
        for doc in documents:
            emit(
                f"- {doc.get('filename', 'document')} "
                f"({doc.get('content_type', 'unknown')}, {doc.get('size_bytes', 0)} bytes)"
            )
            emit(f"      Version {doc.get('version', 1)} - review: {doc.get('review_status', 'pending')}")
            if doc.get("scanner_status") and doc.get("scanner_status") != "NOT_CONFIGURED":
                emit(f"      Scanner: {doc['scanner_status']}")
    else:
        emit("No documents.")
    emit("")

    section("Forms")
    if forms:
        for form in forms:
            emit(f"- {form.get('template_title', form.get('template_id', 'form'))}")
            emit(f"      Status: {form.get('status', 'draft')}")
            if form.get("applicant_confirmed_at"):
                emit("      Applicant confirmed: yes")
            answers = form.get("answers_json") or {}
            if answers:
                for key, value in answers.items():
                    emit(f"      {key}: {value}")
    else:
        emit("No forms.")
    emit("")

    emit("")
    emit("End of draft package.")

    # Build PDF: one content stream per page, splitting lines at bottom_y.
    pages: list[list[str]] = []
    current_page: list[str] = []
    y = start_y
    for line in lines:
        wrapped = _wrap(line, font_size, max_width)
        for wrapped_line in wrapped:
            if y < bottom_y:
                pages.append(current_page)
                current_page = []
                y = start_y
            current_page.append(wrapped_line)
            y -= leading
    if current_page:
        pages.append(current_page)

    font_obj = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    content_objs: list[bytes] = []
    page_objs: list[bytes] = []
    for i, page_lines in enumerate(pages):
        stream = b"BT\n/F1 10.5 Tf\n"
        y = start_y
        for text in page_lines:
            stream += (
                f"{margin_x} {y} Td ({_escape_pdf_text(text)}) Tj\n".encode(
                    "latin-1", "replace"
                )
            )
            y -= leading
        stream += b"ET\n"
        content_objs.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream
            + b"\nendstream\nendobj"
        )
        page_objs.append(
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >>"
            b" /MediaBox [0 0 612 792] /Contents "
            + str(4 + i).encode()
            + b" 0 R >>\nendobj"
        )
    pages_obj = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(
            str(4 + len(content_objs) + i).encode() + b" 0 R"
            for i in range(len(page_objs))
        )
        + b"] /Count " + str(len(page_objs)).encode() + b" >>\nendobj"
    )
    catalog_obj = b"<< /Type /Catalog /Pages 2 0 R >>\nendobj"

    objects: list[bytes] = [
        catalog_obj,            # 1
        pages_obj,              # 2
        font_obj,               # 3
        *content_objs,          # 4..
        *page_objs,
    ]

    parts = [b"%PDF-1.4\n"]
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(b"".join(parts)))
        parts.append(f"{index} 0 obj\n".encode("ascii"))
        parts.append(obj)
        parts.append(b"\nendobj\n")

    body = b"".join(parts)
    xref_pos = len(body)
    parts.append(b"xref\n0 " + str(len(objects) + 1).encode() + b"\n")
    parts.append(b"0000000000 65535 f \n")
    for offset in offsets:
        parts.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    parts.append(b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n")
    parts.append(b"startxref\n" + str(xref_pos).encode("ascii") + b"\n%%EOF\n")
    return b"".join(parts)


def build_application_package_bytes(
    summary_pdf: bytes,
    documents: list[dict],
    document_manifest: list[dict],
) -> bytes:
    """Bundle the draft summary with reviewed documents and a status manifest."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("case-summary.pdf", summary_pdf)
        for document in documents:
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", document["filename"])
            archive.writestr(
                f"documents/{document['document_id']}-{safe_name}",
                document["content"],
            )
        archive.writestr(
            "manifest.json",
            json.dumps(
                {"documents": document_manifest},
                ensure_ascii=False,
                indent=2,
            ),
        )
    return output.getvalue()

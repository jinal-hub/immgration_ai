"""Malware-scanning contract for private applicant document uploads.

Deployments may inject a scanner implementing ``scan(content)`` on
``app.state.document_scanner``. No scanner is claimed to run by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DocumentScanError(RuntimeError):
    """The configured scanner could not provide a trustworthy result."""


@dataclass(frozen=True)
class DocumentScanResult:
    status: str
    detail: str | None = None


class DocumentScanner(Protocol):
    def scan(self, content: bytes) -> DocumentScanResult:
        """Return CLEAN or INFECTED; raise DocumentScanError on scanner failure."""


def scan_document(
    scanner: DocumentScanner | None,
    content: bytes,
) -> DocumentScanResult:
    if scanner is None:
        return DocumentScanResult("NOT_CONFIGURED")
    result = scanner.scan(content)
    if not isinstance(result, DocumentScanResult):
        raise DocumentScanError("Document scanner returned an invalid result.")
    if result.status not in {"CLEAN", "INFECTED"}:
        raise DocumentScanError("Document scanner returned an unsupported status.")
    return result

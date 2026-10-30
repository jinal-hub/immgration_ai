"""Private storage for applicant case documents."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


class DocumentStorageError(RuntimeError):
    """A private document could not be stored or retrieved."""


class CaseDocumentStorage:
    def __init__(self) -> None:
        self.backend = os.getenv("APPLICANT_DOCUMENT_STORAGE", "local").strip().lower()
        self.root = Path(
            os.getenv(
                "APPLICANT_DOCUMENT_DIR",
                str(Path.home() / ".immigration-knowledge" / "applicant_documents"),
            )
        )
        self.bucket = os.getenv("APPLICANT_DOCUMENT_S3_BUCKET", "").strip()
        self.region = os.getenv("APPLICANT_DOCUMENT_S3_REGION", "").strip() or None
        self.endpoint = os.getenv("APPLICANT_DOCUMENT_S3_ENDPOINT", "").strip() or None
        self.access_key = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        self.secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
        self.session_token = os.getenv("AWS_SESSION_TOKEN", "").strip() or None

        if self.backend not in {"local", "s3"}:
            raise RuntimeError("APPLICANT_DOCUMENT_STORAGE must be 'local' or 's3'.")
        if os.getenv("ENVIRONMENT", "").lower() == "production" and self.backend != "s3":
            raise RuntimeError("Production applicant documents must use private S3 storage.")
        if self.backend == "s3" and not self.bucket:
            raise RuntimeError("APPLICANT_DOCUMENT_S3_BUCKET is required for S3 storage.")
        if self.backend == "s3" and not self.region:
            raise RuntimeError("APPLICANT_DOCUMENT_S3_REGION is required for S3 storage.")

    def store(self, tenant_id: str, content: bytes, content_type: str) -> str:
        storage_key = f"{tenant_id}/{uuid.uuid4().hex}"
        if self.backend == "s3":
            self._s3_request(
                "PUT",
                storage_key,
                content=content,
                content_type=content_type,
                encrypt=True,
            )
            return storage_key

        destination = self._local_path(storage_key)
        try:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as target:
                target.write(content)
        except OSError as exc:
            raise DocumentStorageError("Private local document write failed.") from exc
        return storage_key

    def retrieve(self, storage_key: str) -> bytes:
        if self.backend == "s3":
            return self._s3_request("GET", storage_key)

        try:
            return self._local_path(storage_key).read_bytes()
        except OSError as exc:
            raise DocumentStorageError("Private local document read failed.") from exc

    def delete(self, storage_key: str) -> None:
        if self.backend == "s3":
            self._s3_request("DELETE", storage_key)
            return

        try:
            self._local_path(storage_key).unlink()
        except OSError as exc:
            raise DocumentStorageError("Private local document delete failed.") from exc

    def _local_path(self, storage_key: str) -> Path:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}/[0-9a-f]{32}", storage_key):
            raise DocumentStorageError("Invalid internal document storage key.")
        destination = (self.root / storage_key).resolve()
        if not destination.is_relative_to(self.root.resolve()):
            raise DocumentStorageError("Document storage key escaped its private root.")
        return destination

    def _s3_request(
        self,
        method: str,
        storage_key: str,
        content: bytes = b"",
        content_type: str | None = None,
        encrypt: bool = False,
    ) -> bytes:
        if not self.access_key or not self.secret_key:
            raise DocumentStorageError(
                "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required for S3 storage."
            )

        region = self.region
        base_url = self.endpoint or (
            "https://s3.amazonaws.com"
            if region == "us-east-1"
            else f"https://s3.{region}.amazonaws.com"
        )
        endpoint = urlsplit(base_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise DocumentStorageError("Invalid S3-compatible endpoint URL.")
        if endpoint.scheme != "https" and os.getenv("ENVIRONMENT", "").lower() == "production":
            raise DocumentStorageError("Production S3 endpoint must use HTTPS.")
        encoded_key = quote(storage_key, safe="/-_.~")
        canonical_uri = (
            f"{endpoint.path.rstrip('/')}/{quote(self.bucket, safe='-_.~')}/{encoded_key}"
        )
        url = f"{endpoint.scheme}://{endpoint.netloc}{canonical_uri}"
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(content).hexdigest()

        headers = {
            "host": endpoint.netloc,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            headers["content-type"] = content_type
        if encrypt:
            headers["x-amz-server-side-encryption"] = "AES256"
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            f"{name}:{' '.join(value.strip().split())}\n"
            for name, value in sorted(headers.items())
        )
        canonical_request = (
            f"{method}\n{canonical_uri}\n\n{canonical_headers}\n"
            f"{signed_headers}\n{payload_hash}"
        )
        scope = f"{date_stamp}/{region}/s3/aws4_request"
        string_to_sign = "\n".join((
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ))
        date_key = hmac.new(
            f"AWS4{self.secret_key}".encode(),
            date_stamp.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        signing_key = hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        try:
            response = requests.request(
                method,
                url,
                data=content if method == "PUT" else None,
                headers=headers,
                timeout=(5, 30),
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            raise DocumentStorageError(
                f"Private S3 {method.lower()} request failed."
            ) from exc

"""Auth routes — register, login, me."""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, update

from api.schemas.models import (
    LoginRequest,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    RegisterResponse,
    TokenResponse,
    UserCreate,
    UserResponse,
)
from db.connection import get_session, get_db_engine
from db.models import PasswordResetToken
from logging_config import get_logger
from services import login_throttle
from services.applicant_service import ApplicantService
from services.auth_service import AuthService, decode_access_token
from services.password_reset_email import (
    PasswordResetEmailError,
    password_reset_email_configuration_error,
    send_password_reset_email,
)

router = APIRouter()
logger = get_logger(__name__)


def get_current_user(request: Request) -> dict:
    """Extract and validate JWT from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth_header[7:]
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    engine = get_db_engine(request)
    with get_session(engine) as session:
        svc = AuthService(session)
        user = svc.get_current_user(token)
        if not user:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        return user


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Reusable dependency for admin-only endpoints.

    Composes on top of get_current_user (which enforces a valid Bearer JWT and an
    active user) and then rejects non-admin roles with 403. Use as:
    ``current_user: dict = Depends(require_admin)``.
    """
    role = (current_user or {}).get("role")
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return current_user


def get_current_user_id(request: Request) -> str:
    """Return the active authenticated user's ID for user-owned routes."""
    return get_current_user(request)["user_id"]


def _public_registration_allowed() -> bool:
    return os.getenv("ALLOW_PUBLIC_REGISTRATION", "false").strip().lower() == "true"


def _ensure_applicant_profile(session, user_id: str, full_name: str | None) -> None:
    applicant_service = ApplicantService(session)
    if applicant_service.list_for_user(user_id):
        return
    name_parts = (full_name or "").strip().split(maxsplit=1)
    applicant_service.create(
        user_id,
        first_name=name_parts[0] if name_parts else None,
        last_name=name_parts[1] if len(name_parts) > 1 else None,
    )


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(body: UserCreate, request: Request):
    """Self-service registration — gated behind ALLOW_PUBLIC_REGISTRATION
    (default false). Admin accounts are bootstrapped separately via
    ADMIN_EMAIL/ADMIN_PASSWORD (src/create_admin.py), never through this
    route - self-registration always yields role="applicant" regardless."""
    if not _public_registration_allowed():
        raise HTTPException(
            status_code=403,
            detail="Public registration is disabled. Set ALLOW_PUBLIC_REGISTRATION=true to enable it.",
        )
    engine = get_db_engine(request)
    with get_session(engine) as session:
        svc = AuthService(session)
        # Deterministic duplicate rejection: check first so a re-registration attempt
        # (including one targeting an existing admin account with role="applicant")
        # is answered with a clean 409 and never even reaches the DB constraint.
        if svc.email_registered(body.email):
            raise HTTPException(status_code=409, detail="Email already registered")
        try:
            result = svc.register(body.email, body.password, body.full_name)
            _ensure_applicant_profile(
                session,
                result["user_id"],
                result.get("full_name"),
            )
        except IntegrityError:
            # Race backstop (two concurrent requests) — keep the same clean 409.
            raise HTTPException(status_code=409, detail="Email already registered")
        return RegisterResponse(
            user_id=result["user_id"],
            email=result["email"],
            full_name=result["full_name"],
            role=result["role"],
            access_token=result["access_token"],
            token_type=result["token_type"],
            expires_in=result["expires_in"],
        )


@router.post("/password-reset/request", response_model=MessageResponse)
def request_password_reset(body: PasswordResetRequest, request: Request):
    """Send a one-use, short-lived password-reset link without exposing account existence."""
    configuration_error = password_reset_email_configuration_error()
    if configuration_error:
        raise HTTPException(
            status_code=503,
            detail=configuration_error,
        )
    client_host = request.client.host if request.client else "unknown"
    throttle_key = f"PASSWORD_RESET|{client_host}"
    wait = login_throttle.retry_after_seconds(throttle_key)
    if wait:
        raise HTTPException(
            status_code=429,
            detail="Too many password-reset requests. Try again later.",
            headers={"Retry-After": str(wait)},
        )

    engine = get_db_engine(request)
    with get_session(engine) as session:
        from db.repositories import UserRepo

        user = UserRepo(session).get_by_email(body.email)
        if user and user.is_active:
            now = datetime.now(timezone.utc)
            session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user.user_id,
                    PasswordResetToken.used_at.is_(None),
                )
                .values(used_at=now)
            )
            token = secrets.token_urlsafe(32)
            session.add(
                PasswordResetToken(
                    user_id=user.user_id,
                    token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    expires_at=now + timedelta(minutes=30),
                )
            )
            try:
                send_password_reset_email(user.email, token)
            except PasswordResetEmailError as exc:
                logger.error(
                    "Password reset email delivery failed",
                    extra={"user_id": str(user.user_id), "error": str(exc)},
                )
                raise HTTPException(status_code=503, detail=str(exc)) from exc

    login_throttle.record_failure(throttle_key)
    return MessageResponse(
        message="If an active account exists for that email, a password-reset link has been sent."
    )


@router.post("/password-reset/confirm", response_model=MessageResponse)
def confirm_password_reset(body: PasswordResetConfirm, request: Request):
    token_hash = hashlib.sha256(body.token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    engine = get_db_engine(request)
    with get_session(engine) as session:
        reset_token = session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == token_hash,
                PasswordResetToken.used_at.is_(None),
            ).with_for_update()
        ).scalars().first()
        if not reset_token:
            raise HTTPException(status_code=400, detail="This password-reset link is invalid or expired.")

        expires_at = reset_token.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            reset_token.used_at = now
            raise HTTPException(status_code=400, detail="This password-reset link is invalid or expired.")

        svc = AuthService(session)
        if not svc.update_password(str(reset_token.user_id), body.password):
            raise HTTPException(status_code=400, detail="This password-reset link is invalid or expired.")
        reset_token.used_at = now
        session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == reset_token.user_id,
                PasswordResetToken.reset_id != reset_token.reset_id,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=now)
        )

    return MessageResponse(message="Your password has been reset. You can now sign in.")


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request):
    client_host = request.client.host if request.client else "unknown"
    # Normalise email for keys and lookup
    email_norm = (body.email or "").strip().casefold()
    # Hard lock only on (IP, email) pair
    throttle_key_ip_email = f"{client_host}|{email_norm}"
    # Per-email counter only slows/logs; never rejects correct login
    throttle_key_email = f"EMAIL|{email_norm}"

    # Check only the hard lock on (IP,email) pair before verifying credentials
    wait_ip_email = login_throttle.retry_after_seconds(throttle_key_ip_email)
    if wait_ip_email:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(wait_ip_email)},
        )

    engine = get_db_engine(request)
    with get_session(engine) as session:
        svc = AuthService(session)
        # Verify credentials first
        result = svc.login(email_norm, body.password)
        if not result:
            # Record failures on both keys (email for alerting/progressive delay tracking)
            login_throttle.record_failure(throttle_key_email)
            locked_for_ip_email = login_throttle.record_failure(throttle_key_ip_email)
            if locked_for_ip_email:
                raise HTTPException(
                    status_code=429,
                    detail="Too many failed login attempts. Try again later.",
                    headers={"Retry-After": str(locked_for_ip_email)},
                )
            raise HTTPException(status_code=401, detail="Invalid email or password")
        if result["role"] == "applicant":
            user = svc.get_current_user(result["access_token"])
            _ensure_applicant_profile(
                session,
                result["user_id"],
                user.get("full_name") if user else None,
            )
        # Correct password always succeeds; reset counters
        login_throttle.reset(throttle_key_ip_email)
        login_throttle.reset(throttle_key_email)
        return TokenResponse(
            access_token=result["access_token"],
            token_type=result["token_type"],
            expires_in=result["expires_in"],
            role=result["role"],
        )


@router.get("/me", response_model=UserResponse)
def get_me(request: Request, current_user: dict = Depends(get_current_user)):
    return UserResponse(
        user_id=current_user["user_id"],
        email=current_user["email"],
        full_name=current_user.get("full_name"),
        role=current_user["role"],
    )

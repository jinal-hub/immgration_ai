"""Auth routes — register, login, me."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from api.schemas.models import (
    LoginRequest,
    RegisterResponse,
    TokenResponse,
    UserCreate,
    UserResponse,
)
from db.connection import get_session, get_db_engine
from logging_config import get_logger
from services import login_throttle
from services.auth_service import AuthService, decode_access_token

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

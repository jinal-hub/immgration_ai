"""SMTP delivery for applicant password-reset links."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from urllib.parse import quote, urlsplit


class PasswordResetEmailError(RuntimeError):
    """Raised when reset-email configuration or SMTP delivery fails."""


def _smtp_settings() -> tuple[str, int, str, str | None, str | None, bool]:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    if not host or not sender:
        raise PasswordResetEmailError(
            "Password reset email is not configured. Set SMTP_HOST and SMTP_FROM."
        )
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise PasswordResetEmailError("SMTP_PORT must be a valid port number.") from exc
    if not 1 <= port <= 65535:
        raise PasswordResetEmailError("SMTP_PORT must be between 1 and 65535.")
    username = os.getenv("SMTP_USERNAME", "").strip() or None
    password = os.getenv("SMTP_PASSWORD") or None
    if bool(username) != bool(password):
        raise PasswordResetEmailError(
            "Set both SMTP_USERNAME and SMTP_PASSWORD, or leave both blank."
        )
    use_starttls = os.getenv("SMTP_STARTTLS", "true").strip().lower() == "true"
    if username and not use_starttls:
        raise PasswordResetEmailError("SMTP_STARTTLS must be enabled when SMTP authentication is used.")
    public_url = os.getenv("APP_PUBLIC_URL", "http://localhost:5173").rstrip("/")
    parsed_url = urlsplit(public_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
    ):
        raise PasswordResetEmailError("APP_PUBLIC_URL must be an absolute HTTP(S) URL.")
    if (
        os.getenv("ENVIRONMENT", "development").strip().lower() == "production"
        and parsed_url.scheme != "https"
    ):
        raise PasswordResetEmailError("APP_PUBLIC_URL must use HTTPS in production.")
    return host, port, sender, username, password, use_starttls


def password_reset_email_configuration_error() -> str | None:
    try:
        _smtp_settings()
    except PasswordResetEmailError as exc:
        return str(exc)
    return None


def send_password_reset_email(email: str, token: str) -> None:
    host, port, sender, username, password, use_starttls = _smtp_settings()
    public_url = os.getenv("APP_PUBLIC_URL", "http://localhost:5173").rstrip("/")
    reset_url = f"{public_url}/portal/reset-password?token={quote(token)}"
    message = EmailMessage()
    message["Subject"] = "Reset your Pathway password"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "We received a request to reset your Pathway password.\n\n"
        f"Use this link within 30 minutes:\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email."
    )

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_starttls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise PasswordResetEmailError("Could not deliver the password-reset email.") from exc

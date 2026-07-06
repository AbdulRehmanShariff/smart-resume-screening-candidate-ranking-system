"""
services/email_service.py
--------------------------
Flask-Mail email sending service for the Smart Resume Screening System.

Provides high-level functions for every transactional email the platform sends.
All functions share the same contract:
  1. Render the Jinja2 HTML template.
  2. Build the Flask-Mail Message object.
  3. Attempt to send via SMTP.
  4. Write an EmailLog row regardless of success or failure.
  5. On failure: log the exception, update the EmailLog with the error, and
     raise ServiceUnavailableError so the caller can decide whether to surface
     it to the user.

Architecture decisions:
  - This module never commits db.session. The caller (auth_service) commits
    after calling the email function so that the email log, the user mutation,
    and the audit log all commit in a single atomic transaction.
  - Flask current_app is used to access FRONTEND_URL config at call time
    (not at import time) so the service works correctly under test overrides.
  - Templates are rendered with render_template() from Flask so Jinja2
    auto-escaping is active for the HTML body, preventing XSS via
    user-supplied values like first_name.
  - Both HTML and plaintext bodies are always sent (plaintext for mail clients
    that disable HTML).
  - MAIL_SUPPRESS_SEND=True in TestingConfig means no real emails are sent
    during test runs — Flask-Mail handles this transparently.

Email templates (relative to app/templates/email/):
  verification.html   — Email address verification
  password_reset.html — Password reset link
  welcome.html        — Post-verification welcome

Logging:
  Every send attempt is logged at INFO level. Failures are logged at ERROR.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from flask import current_app, render_template
from flask_mail import Message

from app.extensions import db, mail
from app.models.email_log import EmailLog, EmailStatus, EmailTemplate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _render_html(template_name: str, **context) -> str:
    """
    Render a Jinja2 HTML email template from app/templates/email/.

    Args:
        template_name : Template filename relative to app/templates/email/.
        **context     : Template variables.

    Returns:
        Rendered HTML string.
    """
    return render_template(f"email/{template_name}", **context)


def _render_plaintext_fallback(subject: str, body_lines: list[str]) -> str:
    """
    Build a plain-text fallback body for email clients that disable HTML.

    Args:
        subject    : Email subject line.
        body_lines : List of text lines to include in the body.

    Returns:
        Plain-text email body string.
    """
    separator = "-" * 60
    lines = [
        "Smart Resume Screening System",
        separator,
        subject,
        separator,
        "",
    ] + body_lines + [
        "",
        separator,
        "This is an automated message. Please do not reply.",
        "(c) 2026 Smart Resume Screening System",
    ]
    return "\n".join(lines)


def _create_email_log(
    *,
    user_id: Optional[object],
    recipient_email: str,
    template: str,
    subject: str,
    template_vars: Optional[dict] = None,
) -> EmailLog:
    """
    Create an EmailLog row in PENDING state and add it to the session.

    The caller must commit. This function deliberately does NOT commit
    so that the email log, user state, and audit log all land in one
    atomic transaction.

    Args:
        user_id        : UUID of the recipient user (or None).
        recipient_email: The email address being sent to.
        template       : EmailTemplate constant value string.
        subject        : The rendered email subject line.
        template_vars  : Dict of template variables for the metadata JSONB.

    Returns:
        The un-committed EmailLog instance.
    """
    log = EmailLog(
        user_id=user_id,
        recipient_email=recipient_email,
        template=template,
        subject=subject,
        status=EmailStatus.PENDING.value,
        retry_count=0,          # Explicit Python-side default; ORM default only
                                # fires on flush — we need it set now so that
                                # _send_message can safely do retry_count += 1
                                # even before the row reaches the database.
        email_metadata={"template_vars": template_vars or {}},
    )
    db.session.add(log)
    return log


def _send_message(
    log: EmailLog,
    msg: Message,
) -> None:
    """
    Send the message via Flask-Mail and update the EmailLog accordingly.

    On success: sets log.status = SENT and log.sent_at.
    On failure: sets log.status = FAILED, log.error_message, increments
                log.retry_count, then raises ServiceUnavailableError.

    The caller must commit after this returns.

    Args:
        log : The EmailLog row to update.
        msg : The Flask-Mail Message to send.

    Raises:
        ServiceUnavailableError: If the SMTP send fails.
    """
    from app.core.exceptions import ServiceUnavailableError

    try:
        mail.send(msg)
        log.status = EmailStatus.SENT.value
        log.sent_at = datetime.now(timezone.utc)
        logger.info(
            "Email sent | template=%s | to=%s", log.template, log.recipient_email
        )
    except Exception as exc:
        log.status = EmailStatus.FAILED.value
        log.error_message = str(exc)
        log.retry_count = (log.retry_count or 0) + 1
        logger.error(
            "Email send failed | template=%s | to=%s | error=%s",
            log.template,
            log.recipient_email,
            exc,
            exc_info=True,
        )
        raise ServiceUnavailableError(
            "The email could not be sent at this time. "
            "Your account has been created — please try the resend option shortly."
        ) from exc


# ---------------------------------------------------------------------------
# Public Email Functions
# ---------------------------------------------------------------------------


def send_verification_email(
    *,
    user_id: object,
    email: str,
    first_name: str,
    token: str,
) -> EmailLog:
    """
    Send an email address verification email.

    Builds a verification URL of the form:
      {FRONTEND_URL}/verify-email?token={token}

    The link uses the frontend URL so the React/Next.js app can handle
    the verification flow (displaying a loading screen, calling the
    /api/v1/auth/verify-email route, and redirecting appropriately).

    Args:
        user_id    : UUID of the user being verified.
        email      : Recipient email address.
        first_name : User's first name for personalisation.
        token      : The verification token from user.generate_verification_token().

    Returns:
        The EmailLog instance (not yet committed).

    Raises:
        ServiceUnavailableError: If SMTP delivery fails.
    """
    frontend_url: str = current_app.config.get("FRONTEND_URL", "http://localhost:3000")
    verification_url: str = f"{frontend_url}/verify-email?token={token}"
    subject: str = "Verify your email address — Smart Resume"

    html_body: str = _render_html(
        "verification.html",
        first_name=first_name,
        verification_url=verification_url,
    )
    text_body: str = _render_plaintext_fallback(
        subject,
        [
            f"Hi {first_name},",
            "",
            "Please verify your email address by visiting the link below:",
            "",
            verification_url,
            "",
            "This link expires in 24 hours.",
            "",
            "If you did not create an account, please ignore this email.",
        ],
    )

    log = _create_email_log(
        user_id=user_id,
        recipient_email=email,
        template=EmailTemplate.EMAIL_VERIFICATION.value,
        subject=subject,
        template_vars={
            "first_name": first_name,
            "verification_url": verification_url,
        },
    )

    msg = Message(
        subject=subject,
        recipients=[email],
        html=html_body,
        body=text_body,
    )
    _send_message(log, msg)
    return log


def send_password_reset_email(
    *,
    user_id: object,
    email: str,
    first_name: str,
    token: str,
    ip_address: Optional[str] = None,
) -> EmailLog:
    """
    Send a password reset email containing a time-limited reset link.

    Builds a reset URL of the form:
      {FRONTEND_URL}/reset-password?token={token}

    The link expires in 1 hour (enforced by user.is_reset_token_valid()).
    The IP address of the requester is shown in the email as a security
    disclosure so the user can verify the request originated from them.

    Args:
        user_id    : UUID of the user requesting the reset.
        email      : Recipient email address.
        first_name : User's first name for personalisation.
        token      : The reset token from user.generate_reset_token().
        ip_address : IP address of the HTTP request (for security disclosure).

    Returns:
        The EmailLog instance (not yet committed).

    Raises:
        ServiceUnavailableError: If SMTP delivery fails.
    """
    frontend_url: str = current_app.config.get("FRONTEND_URL", "http://localhost:3000")
    reset_url: str = f"{frontend_url}/reset-password?token={token}"
    subject: str = "Reset your password — Smart Resume"
    display_ip: str = ip_address or "Unknown"

    html_body: str = _render_html(
        "password_reset.html",
        first_name=first_name,
        email=email,
        reset_url=reset_url,
        ip_address=display_ip,
    )
    text_body: str = _render_plaintext_fallback(
        subject,
        [
            f"Hi {first_name},",
            "",
            "We received a request to reset the password for your account.",
            "",
            f"Account: {email}",
            f"Requested from: {display_ip}",
            "",
            "Click the link below to reset your password (expires in 1 hour):",
            "",
            reset_url,
            "",
            "If you did not request this, you can safely ignore this email.",
            "Your password will not change unless you visit the link above.",
        ],
    )

    log = _create_email_log(
        user_id=user_id,
        recipient_email=email,
        template=EmailTemplate.PASSWORD_RESET.value,
        subject=subject,
        template_vars={
            "first_name": first_name,
            "email": email,
            "reset_url": reset_url,
            "ip_address": display_ip,
        },
    )

    msg = Message(
        subject=subject,
        recipients=[email],
        html=html_body,
        body=text_body,
    )
    _send_message(log, msg)
    return log


def send_welcome_email(
    *,
    user_id: object,
    email: str,
    first_name: str,
    role: str,
) -> EmailLog:
    """
    Send a welcome email after the user successfully verifies their email.

    The email is role-aware: candidate accounts see a "how to get started"
    list oriented around job searching; recruiter accounts see a list
    oriented around posting jobs and reviewing candidates.

    Args:
        user_id    : UUID of the user.
        email      : Recipient email address.
        first_name : User's first name for personalisation.
        role       : Role name string ('candidate' or 'recruiter').

    Returns:
        The EmailLog instance (not yet committed).

    Note:
        Welcome email failures are NOT fatal — if this send fails, the
        verification has already succeeded. The caller catches
        ServiceUnavailableError from this function and logs it but does
        NOT re-raise it to the user.
    """
    frontend_url: str = current_app.config.get("FRONTEND_URL", "http://localhost:3000")
    dashboard_url: str = f"{frontend_url}/dashboard"
    subject: str = "Welcome to Smart Resume!"

    role_label: str = "Candidate" if role == "candidate" else "Recruiter"

    html_body: str = _render_html(
        "welcome.html",
        first_name=first_name,
        role=role,
        role_label=role_label,
        dashboard_url=dashboard_url,
    )
    text_body: str = _render_plaintext_fallback(
        subject,
        [
            f"Hi {first_name},",
            "",
            f"Welcome to Smart Resume! Your {role_label} account is now fully active.",
            "",
            f"Visit your dashboard here: {dashboard_url}",
        ],
    )

    log = _create_email_log(
        user_id=user_id,
        recipient_email=email,
        template=EmailTemplate.WELCOME.value,
        subject=subject,
        template_vars={
            "first_name": first_name,
            "role": role,
            "dashboard_url": dashboard_url,
        },
    )

    msg = Message(
        subject=subject,
        recipients=[email],
        html=html_body,
        body=text_body,
    )
    _send_message(log, msg)
    return log

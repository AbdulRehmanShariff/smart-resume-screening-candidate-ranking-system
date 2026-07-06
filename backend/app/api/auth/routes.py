"""
api/auth/routes.py
------------------
Authentication Blueprint — route handlers for the Smart Resume Screening System.

Thin route layer: validates input, delegates to auth_service, returns responses.
All business logic lives in app/services/auth_service.py.

Registered endpoints (Batch 2A):
  POST /api/v1/auth/register/candidate
  POST /api/v1/auth/register/recruiter
  POST /api/v1/auth/login

Registered endpoints (Batch 2B):
  GET  /api/v1/auth/verify-email         — Email address verification (token in query string)
  POST /api/v1/auth/resend-verification  — Resend verification email
  POST /api/v1/auth/forgot-password      — Initiate password reset
  POST /api/v1/auth/reset-password       — Complete password reset with token + new password
  GET  /api/v1/auth/me                   — Current user profile (JWT required)

Route handler contract:
  1. Parse JSON body (handle missing/malformed JSON gracefully).
  2. Validate with the appropriate Marshmallow schema.
  3. Call the service function with validated data + request context.
  4. Return the standard response envelope from app.core.responses.
  5. Never raise — all exceptions are caught by global handlers in
     the application factory (app/__init__.py).

Error handling chain:
  ValidationError (marshmallow) -> 422 Unprocessable Entity (caught here)
  AppException subclasses       -> handled by @app.errorhandler(AppException)
  Unhandled exceptions          -> 500 handled by @app.errorhandler(500)

Anti-enumeration design:
  forgot_password and resend_verification always return HTTP 200 regardless
  of whether the email is registered — to prevent user discovery via
  differential responses.
"""

import logging

from flask import Blueprint, request
from marshmallow import ValidationError

from app.core.decorators import get_current_user, jwt_required_user
from app.core.responses import created_response, success_response
from app.core.responses import validation_error_response
from app.schemas.auth import (
    CandidateRegistrationSchema,
    ForgotPasswordSchema,
    LoginSchema,
    RecruiterRegistrationSchema,
    ResendVerificationSchema,
    ResetPasswordSchema,
)
from app.services import auth_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

auth_bp = Blueprint("auth", __name__)

# ---------------------------------------------------------------------------
# Schema instances (module-level — schemas are thread-safe in Flask)
# ---------------------------------------------------------------------------

_candidate_schema = CandidateRegistrationSchema()
_recruiter_schema = RecruiterRegistrationSchema()
_login_schema = LoginSchema()
_forgot_schema = ForgotPasswordSchema()
_reset_schema = ResetPasswordSchema()
_resend_schema = ResendVerificationSchema()


# ---------------------------------------------------------------------------
# Request Helpers
# ---------------------------------------------------------------------------


def _get_json() -> dict:
    """
    Parse the request body as JSON.

    Returns an empty dict if the body is absent or not valid JSON,
    allowing the schema to produce field-level 'required' errors
    rather than a raw 400.
    """
    return request.get_json(silent=True) or {}


def _ctx() -> dict:
    """Return common request context kwargs for service calls."""
    return {
        "ip_address": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
    }


# ---------------------------------------------------------------------------
# Batch 2A Routes
# ---------------------------------------------------------------------------


@auth_bp.post("/register/candidate")
def register_candidate():
    """
    Register a new candidate account.

    Creates a User (role=candidate) + CandidateProfile. Sends a verification
    email. Login is blocked until the email is verified.

    Request body (JSON):
      email       : string, required
      password    : string, required — min 8 chars, at least 1 letter + 1 digit
      first_name  : string, required
      last_name   : string, required
      phone       : string, optional

    Responses:
      201 Created             — Account created; verification email sent
      409 Conflict            — Email already registered
      422 Unprocessable       — Validation errors
      503 Service Unavailable — Email service temporarily unavailable
    """
    raw: dict = _get_json()
    try:
        data: dict = _candidate_schema.load(raw)
    except ValidationError as exc:
        logger.debug("Candidate registration validation failed | errors=%s", exc.messages)
        return validation_error_response(exc.messages)

    user_data: dict = auth_service.register_candidate(data, **_ctx())
    return created_response(
        message=(
            "Account created successfully. "
            "Please check your email and verify your address before logging in."
        ),
        data=user_data,
    )


@auth_bp.post("/register/recruiter")
def register_recruiter():
    """
    Register a new recruiter account.

    Creates a User (role=recruiter) + RecruiterProfile. Sends a verification
    email. Login is blocked until the email is verified.

    Request body (JSON):
      email        : string, required
      password     : string, required
      first_name   : string, required
      last_name    : string, required
      company_name : string, required
      phone        : string, optional
      company_size : string, optional — startup|small|medium|large|enterprise
      industry     : string, optional
      designation  : string, optional

    Responses:
      201 Created             — Account created; verification email sent
      409 Conflict            — Email already registered
      422 Unprocessable       — Validation errors
      503 Service Unavailable — Email service temporarily unavailable
    """
    raw: dict = _get_json()
    try:
        data: dict = _recruiter_schema.load(raw)
    except ValidationError as exc:
        logger.debug("Recruiter registration validation failed | errors=%s", exc.messages)
        return validation_error_response(exc.messages)

    user_data: dict = auth_service.register_recruiter(data, **_ctx())
    return created_response(
        message=(
            "Account created successfully. "
            "Please check your email and verify your address before logging in."
        ),
        data=user_data,
    )


@auth_bp.post("/login")
def login():
    """
    Authenticate a user and return JWT access + refresh tokens.

    Request body (JSON):
      email    : string, required
      password : string, required

    Responses:
      200 OK           — Login successful; tokens returned
      401 Unauthorized — Invalid credentials or account not eligible
      422 Unprocessable — Validation errors

    Response data (200):
      access_token  : JWT Bearer token (short-lived)
      refresh_token : JWT for token rotation (long-lived)
      token_type    : "Bearer"
      user          : Public user object
    """
    raw: dict = _get_json()
    try:
        data: dict = _login_schema.load(raw)
    except ValidationError as exc:
        logger.debug("Login validation failed | errors=%s", exc.messages)
        return validation_error_response(exc.messages)

    result: dict = auth_service.login(data, **_ctx())
    return success_response(message="Login successful.", data=result)


# ---------------------------------------------------------------------------
# Batch 2B Routes
# ---------------------------------------------------------------------------


@auth_bp.get("/verify-email")
def verify_email():
    """
    Verify a user's email address using the token from the verification email.

    The frontend calls this route after the user clicks the verification link.
    Token is passed as a query parameter (GET) so the browser navigates
    directly to this URL from the email client without needing JavaScript.

    Query parameters:
      token : string, required — the verification token from the email

    Responses:
      200 OK           — Email verified successfully
      400 Bad Request  — Token is invalid, expired, or already used
      422 Unprocessable — Token parameter missing

    Response data (200):
      user                : Public user object (now with is_verified=true)
      welcome_email_sent  : boolean
    """
    token: str = request.args.get("token", "").strip()

    if not token:
        return validation_error_response({"token": ["Verification token is required."]})

    result: dict = auth_service.verify_email(token, **_ctx())
    return success_response(
        message="Email verified successfully. Welcome to Smart Resume!",
        data=result,
    )


@auth_bp.post("/resend-verification")
def resend_verification():
    """
    Resend the email verification link.

    Always returns 200 regardless of whether the email is registered or
    already verified, to prevent user enumeration.

    Request body (JSON):
      email : string, required

    Responses:
      200 OK            — Always (anti-enumeration)
      422 Unprocessable — Validation errors
    """
    raw: dict = _get_json()
    try:
        data: dict = _resend_schema.load(raw)
    except ValidationError as exc:
        logger.debug("Resend verification validation failed | errors=%s", exc.messages)
        return validation_error_response(exc.messages)

    auth_service.resend_verification(data["email"], **_ctx())
    return success_response(
        message=(
            "If your email address is registered and not yet verified, "
            "a new verification link has been sent."
        ),
        data=None,
    )


@auth_bp.post("/forgot-password")
def forgot_password():
    """
    Initiate the password reset flow.

    Sends a password reset email if the address is registered.
    Always returns 200 regardless of outcome, to prevent user enumeration.

    Request body (JSON):
      email : string, required

    Responses:
      200 OK            — Always (anti-enumeration)
      422 Unprocessable — Validation errors
    """
    raw: dict = _get_json()
    try:
        data: dict = _forgot_schema.load(raw)
    except ValidationError as exc:
        logger.debug("Forgot password validation failed | errors=%s", exc.messages)
        return validation_error_response(exc.messages)

    auth_service.forgot_password(data["email"], **_ctx())
    return success_response(
        message=(
            "If an account exists for that email address, "
            "a password reset link has been sent."
        ),
        data=None,
    )


@auth_bp.post("/reset-password")
def reset_password():
    """
    Complete the password reset flow with the token from the reset email.

    Request body (JSON):
      token        : string, required — token from the reset email link
      new_password : string, required — min 8 chars, at least 1 letter + 1 digit

    Responses:
      200 OK           — Password reset successfully
      400 Bad Request  — Token is invalid or expired
      422 Unprocessable — Validation errors
    """
    raw: dict = _get_json()
    try:
        data: dict = _reset_schema.load(raw)
    except ValidationError as exc:
        logger.debug("Reset password validation failed | errors=%s", exc.messages)
        return validation_error_response(exc.messages)

    auth_service.reset_password(
        token=data["token"],
        new_password=data["new_password"],
        **_ctx(),
    )
    return success_response(
        message=(
            "Password reset successfully. "
            "You can now log in with your new password."
        ),
        data=None,
    )


@auth_bp.get("/me")
@jwt_required_user
def me():
    """
    Return the authenticated user's public profile.

    Requires a valid JWT Bearer token in the Authorization header.

    Responses:
      200 OK           — User profile returned
      401 Unauthorized — Missing or invalid token

    Response data (200):
      User public dict (id, email, first_name, last_name, role, ...)
    """
    user = get_current_user()
    return success_response(
        message="User profile retrieved successfully.",
        data=user.to_public_dict(),
    )

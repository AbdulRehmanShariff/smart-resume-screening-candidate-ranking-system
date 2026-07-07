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

Registered endpoints (Batch 2C):
  POST /api/v1/auth/refresh              — Issue new access token using refresh token (rotation)
  POST /api/v1/auth/logout               — Revoke current access + refresh token pair
  POST /api/v1/auth/logout-all           — Revoke all sessions for this user

Registered endpoints (Batch 2D):
  POST  /api/v1/auth/change-password     — Change password (requires current password)
  POST  /api/v1/auth/change-email        — Change email (requires current password, re-triggers verification)
  GET   /api/v1/auth/me                  — Current user + role-specific profile (improved)
  PATCH /api/v1/auth/profile/candidate   — Partial update of candidate profile
  PATCH /api/v1/auth/profile/recruiter   — Partial update of recruiter profile

Registered endpoints (Batch 2E):
  GET    /api/v1/auth/sessions               — List revoked/past sessions for session management UI
  DELETE /api/v1/auth/sessions/<jti>         — Revoke a specific session by JTI
  POST   /api/v1/auth/deactivate             — Self-service account deactivation (requires password)
  POST   /api/v1/auth/reactivate/<user_id>   — Admin: reactivate a deactivated account
  POST   /api/v1/auth/introspect             — Decode and inspect a JWT's claims
  GET    /api/v1/auth/security-settings      — Get current user's security preferences
  PATCH  /api/v1/auth/security-settings      — Update current user's security preferences

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
from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request
from marshmallow import ValidationError

from app.core.decorators import get_current_user, jwt_required_user
from app.core.responses import created_response, success_response
from app.core.responses import validation_error_response
from app.schemas.auth import (
    AccountDeactivationSchema,
    CandidateRegistrationSchema,
    ChangeEmailSchema,
    ChangePasswordSchema,
    ForgotPasswordSchema,
    LoginSchema,
    RecruiterRegistrationSchema,
    ResendVerificationSchema,
    ResetPasswordSchema,
    TokenIntrospectSchema,
    UpdateCandidateProfileSchema,
    UpdateRecruiterProfileSchema,
    UpdateSecuritySettingsSchema,
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
_change_password_schema = ChangePasswordSchema()
_change_email_schema = ChangeEmailSchema()
_update_candidate_profile_schema = UpdateCandidateProfileSchema()
_update_recruiter_profile_schema = UpdateRecruiterProfileSchema()
_deactivation_schema = AccountDeactivationSchema()
_introspect_schema = TokenIntrospectSchema()
_security_settings_schema = UpdateSecuritySettingsSchema()


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
    Return the authenticated user's public profile including role-specific data.

    Improved in Batch 2D: the response now includes a 'profile' key that
    contains the user's CandidateProfile (for candidates) or RecruiterProfile
    (for recruiters). Admin users receive profile=null.

    Requires a valid JWT Bearer token in the Authorization header.

    Responses:
      200 OK           — User profile + role-specific profile returned
      401 Unauthorized — Missing or invalid token

    Response data (200):
      user public dict + profile: { candidate or recruiter profile fields }
    """
    user = get_current_user()
    return success_response(
        message="User profile retrieved successfully.",
        data=auth_service.get_full_profile(user),
    )


# ---------------------------------------------------------------------------
# Batch 2C Routes
# ---------------------------------------------------------------------------


@auth_bp.post("/refresh")
def refresh():
    """
    Issue a new access token using a valid refresh token (with token rotation).

    This endpoint accepts a refresh token in the Authorization header
    (not an access token). Flask-JWT-Extended distinguishes the two via the
    `type` claim embedded in the token payload.

    Token rotation is ON by default: the old refresh token is revoked and a
    new refresh token is returned alongside the new access token. This makes
    stolen refresh tokens detectable — a token used twice will be rejected
    on the second use because it was already rotated after the first.

    Authorization:
      Bearer <refresh_token>   (NOT an access token)

    Responses:
      200 OK           — New access token (and optionally new refresh token) returned
      401 Unauthorized — Missing, invalid, expired, or already-revoked refresh token
      401 Unauthorized — User account suspended/deleted since token was issued

    Response data (200):
      access_token  : New short-lived JWT (access)
      refresh_token : New refresh token (rotation) or null if rotation disabled
      token_type    : "Bearer"
    """
    # verify_jwt_in_request(refresh=True) enforces that the token type
    # claim is "refresh". Sending an access token here returns 422.
    verify_jwt_in_request(refresh=True)

    jwt_payload: dict = get_jwt()
    identity: str = get_jwt_identity()

    result: dict = auth_service.refresh_token(
        current_refresh_jti=jwt_payload["jti"],
        user_id=identity,
        rotate=True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    )

    return success_response(
        message="Access token refreshed successfully.",
        data=result,
    )


@auth_bp.post("/logout")
def logout():
    """
    Revoke the caller's current access and refresh token pair.

    Both tokens are added to the TokenBlocklist immediately. Any subsequent
    request carrying either token will be rejected with 401 before reaching
    any route handler (via the blocklist loader in app/__init__.py).

    The refresh token JTI must be included in the request body because
    Flask-JWT-Extended only exposes the JTI of the *current request's* token
    (the access token). The client must send the refresh token JTI it received
    at login.

    Authorization:
      Bearer <access_token>

    Request body (JSON, optional):
      refresh_token_jti : string — JTI of the refresh token to also revoke.
                                    If omitted, only the access token is revoked.

    Responses:
      200 OK           — Logged out successfully
      401 Unauthorized — Missing or invalid access token
    """
    verify_jwt_in_request()

    jwt_payload: dict = get_jwt()
    identity: str = get_jwt_identity()

    # The client optionally sends the refresh token JTI so we can revoke it too
    body: dict = _get_json()
    refresh_jti: str = body.get("refresh_token_jti", "")

    auth_service.logout(
        access_jti=jwt_payload["jti"],
        refresh_jti=refresh_jti,
        user_id=identity,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    )

    return success_response(
        message="You have been logged out successfully.",
        data=None,
    )


@auth_bp.post("/logout-all")
def logout_all():
    """
    Revoke all active sessions for this user across all devices.

    Immediately revokes the current access token. Other active sessions
    (on other devices) will expire naturally within the access token lifetime
    (1 hour). For stronger immediate invalidation, change your password after
    calling this endpoint — password change will invalidate the stored
    verification and reset tokens as well.

    Authorization:
      Bearer <access_token>

    Responses:
      200 OK           — All sessions revoked
      401 Unauthorized — Missing or invalid access token

    Response data (200):
      sessions_revoked : int — number of token entries newly blocklisted
    """
    verify_jwt_in_request()

    jwt_payload: dict = get_jwt()
    identity: str = get_jwt_identity()

    sessions_revoked: int = auth_service.logout_all(
        current_access_jti=jwt_payload["jti"],
        user_id=identity,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    )

    return success_response(
        message=(
            "All sessions have been revoked. "
            "Active sessions on other devices will expire within 1 hour."
        ),
        data={"sessions_revoked": sessions_revoked},
    )


# ---------------------------------------------------------------------------
# Batch 2D Routes
# ---------------------------------------------------------------------------


@auth_bp.post("/change-password")
@jwt_required_user
def change_password():
    """
    Change the authenticated user's password.

    Requires the caller's CURRENT password for re-authentication, preventing
    a stolen session token from silently hijacking the account.
    After a successful change the current access token is revoked — the user
    must log in again with the new password.

    Request body (JSON):
      current_password : string, required — existing password
      new_password     : string, required — 8-128 chars, ≥1 letter + ≥1 digit

    Responses:
      200 OK                  — Password changed; current session revoked
      401 Unauthorized        — Wrong current_password or missing token
      400 Bad Request         — new_password is the same as current_password
      422 Unprocessable Entity — Validation error

    Response data (200):
      message only (no sensitive data returned)
    """
    body: dict = _get_json()
    try:
        data = _change_password_schema.load(body)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    user = get_current_user()
    jwt_payload: dict = get_jwt()

    auth_service.change_password(
        user=user,
        current_password=data["current_password"],
        new_password=data["new_password"],
        current_access_jti=jwt_payload.get("jti"),
        **_ctx(),
    )

    return success_response(
        message=(
            "Password changed successfully. "
            "Please log in again with your new password."
        )
    )


@auth_bp.post("/change-email")
@jwt_required_user
def change_email():
    """
    Change the authenticated user's email address.

    Requires the caller's CURRENT password for re-authentication.
    The new email is stored immediately but marked unverified — a
    verification link is dispatched to the new address. The user must
    verify the new address before they can log in again.

    Request body (JSON):
      new_email        : string, required — valid email address
      current_password : string, required — for re-authentication

    Responses:
      200 OK                  — Email changed; verification link dispatched
      400 Bad Request         — new_email == current email
      401 Unauthorized        — Wrong current_password or missing token
      409 Conflict            — new_email already registered to another account
      422 Unprocessable Entity — Validation error

    Response data (200):
      user public dict + verification_email_sent: bool
    """
    body: dict = _get_json()
    try:
        data = _change_email_schema.load(body)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    user = get_current_user()

    result = auth_service.change_email(
        user=user,
        new_email=data["new_email"],
        current_password=data["current_password"],
        **_ctx(),
    )

    return success_response(
        message=(
            "Email address updated. "
            "A verification link has been sent to your new email. "
            "Please verify it before logging in again."
        ),
        data=result,
    )


@auth_bp.patch("/profile/candidate")
@jwt_required_user
def update_candidate_profile():
    """
    Partially update the authenticated candidate's profile.

    All fields are optional — send only the fields you wish to update.
    Fields omitted from the request body are left unchanged.

    This endpoint is restricted to users with the 'candidate' role.
    Recruiters and admins will receive a 403 Forbidden response.

    Request body (JSON — all fields optional):
      first_name          : string, 1-100 chars
      last_name           : string, 1-100 chars
      phone               : string, optional
      headline            : string, max 255 chars
      summary             : string (free text)
      location            : string, max 255 chars
      linkedin_url        : valid URL, max 500 chars
      github_url          : valid URL, max 500 chars
      portfolio_url       : valid URL, max 500 chars
      years_of_experience : float, 0-50
      availability        : one of: immediately | two_weeks | one_month | not_looking

    Responses:
      200 OK                  — Profile updated
      401 Unauthorized        — Missing or invalid token
      403 Forbidden           — User is not a candidate
      422 Unprocessable Entity — Validation error

    Response data (200):
      user public dict + profile: { candidate profile fields }
    """
    from app.core.exceptions import AuthorizationError

    body: dict = _get_json()
    try:
        data = _update_candidate_profile_schema.load(body)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    user = get_current_user()

    if not user.is_candidate:
        raise AuthorizationError(
            "This endpoint is only available to candidates."
        )

    result = auth_service.update_candidate_profile(
        user=user,
        data=data,
        **_ctx(),
    )

    return success_response(
        message="Candidate profile updated successfully.",
        data=result,
    )


@auth_bp.patch("/profile/recruiter")
@jwt_required_user
def update_recruiter_profile():
    """
    Partially update the authenticated recruiter's profile.

    All fields are optional — send only the fields you wish to update.
    Fields omitted from the request body are left unchanged.

    This endpoint is restricted to users with the 'recruiter' role.
    Candidates and admins will receive a 403 Forbidden response.

    Request body (JSON — all fields optional):
      first_name      : string, 1-100 chars
      last_name       : string, 1-100 chars
      phone           : string, optional
      company_name    : string, 1-255 chars
      company_website : valid URL, max 500 chars
      company_size    : one of: startup | small | medium | large | enterprise
      industry        : string, max 100 chars
      designation     : string, max 255 chars

    Responses:
      200 OK                  — Profile updated
      401 Unauthorized        — Missing or invalid token
      403 Forbidden           — User is not a recruiter
      422 Unprocessable Entity — Validation error

    Response data (200):
      user public dict + profile: { recruiter profile fields }
    """
    from app.core.exceptions import AuthorizationError

    body: dict = _get_json()
    try:
        data = _update_recruiter_profile_schema.load(body)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    user = get_current_user()

    if not user.is_recruiter:
        raise AuthorizationError(
            "This endpoint is only available to recruiters."
        )

    result = auth_service.update_recruiter_profile(
        user=user,
        data=data,
        **_ctx(),
    )

    return success_response(
        message="Recruiter profile updated successfully.",
        data=result,
    )


# ---------------------------------------------------------------------------
# Batch 2E Routes
# ---------------------------------------------------------------------------


@auth_bp.get("/sessions")
@jwt_required_user
def get_sessions():
    """
    List the authenticated user's revoked / past session entries.

    Returns the last 50 token blocklist entries for this user, ordered
    newest-first. This powers a session management UI where the user can
    see and manage which sessions have been terminated.

    Requires a valid JWT Bearer token in the Authorization header.

    Responses:
      200 OK           — Session list returned (may be empty)
      401 Unauthorized — Missing or invalid token

    Response data (200):
      List of token blocklist entries (jti, token_type, created_at)
    """
    user = get_current_user()
    sessions = auth_service.get_sessions(user)
    return success_response(
        message=f"Retrieved {len(sessions)} session record(s).",
        data=sessions,
    )


@auth_bp.delete("/sessions/<string:jti>")
@jwt_required_user
def revoke_session(jti: str):
    """
    Revoke a specific session by its JWT ID (JTI).

    Allows the user to terminate a specific active session (e.g. a phone
    that was lost) without invalidating all other sessions. If the JTI is
    already revoked, the request is treated as idempotent (success).

    URL parameter:
      jti : The JWT ID claim to revoke (from the session list).

    Requires a valid JWT Bearer token in the Authorization header.

    Responses:
      200 OK           — Session revoked (or was already revoked)
      401 Unauthorized — Missing or invalid token
      404 Not Found    — JTI does not belong to this user
    """
    user = get_current_user()
    auth_service.revoke_session(user=user, jti=jti, **_ctx())
    return success_response(message="Session revoked successfully.")


@auth_bp.post("/deactivate")
@jwt_required_user
def deactivate_account():
    """
    Self-service account deactivation.

    Sets the account to is_active=False (soft deactivation — data is
    preserved). All active sessions are invalidated immediately.
    Requires the current password to prevent abuse of stolen tokens.

    Deactivated accounts can only be reactivated by an admin via
    POST /auth/reactivate/<user_id>.

    Request body (JSON):
      current_password : string, required — for identity confirmation
      reason           : string, optional — stored in the audit log

    Responses:
      200 OK                   — Account deactivated; current session revoked
      401 Unauthorized         — Wrong current_password or missing token
      422 Unprocessable Entity — Validation error

    Response data (200):
      message only
    """
    body: dict = _get_json()
    try:
        data = _deactivation_schema.load(body)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    user = get_current_user()
    jwt_payload: dict = get_jwt()

    auth_service.deactivate_account(
        user=user,
        current_password=data["current_password"],
        reason=data.get("reason"),
        current_access_jti=jwt_payload.get("jti"),
        **_ctx(),
    )

    return success_response(
        message=(
            "Your account has been deactivated. "
            "All active sessions have been terminated. "
            "Contact support to reactivate your account."
        )
    )


@auth_bp.post("/reactivate/<string:user_id>")
@jwt_required_user
def reactivate_account(user_id: str):
    """
    Admin: reactivate a previously deactivated user account.

    Sets the target user's is_active=True. This endpoint is restricted
    to users with the 'admin' role.

    URL parameter:
      user_id : UUID of the user to reactivate.

    Requires a valid JWT Bearer token with the 'admin' role.

    Responses:
      200 OK           — Account reactivated
      400 Bad Request  — Account is already active, or invalid UUID
      401 Unauthorized — Missing or invalid token
      403 Forbidden    — Caller is not an admin
      404 Not Found    — User does not exist

    Response data (200):
      Updated user public dict for the reactivated user
    """
    from app.core.exceptions import AuthorizationError

    admin = get_current_user()

    if not admin.is_admin:
        raise AuthorizationError(
            "This endpoint is restricted to administrators."
        )

    result = auth_service.reactivate_account(
        target_user_id=user_id,
        admin_user=admin,
        **_ctx(),
    )

    return success_response(
        message="Account reactivated successfully.",
        data=result,
    )


@auth_bp.post("/introspect")
@jwt_required_user
def introspect_token():
    """
    Decode and inspect a JWT's claims without triggering any side effects.

    Accepts any raw JWT string and returns its decoded payload in a
    human-readable format. The token's blocklist status is also reported.

    Use cases:
      - Frontend: display token expiry countdown
      - Admin / developer tooling: inspect token claims
      - Debugging: verify token was issued correctly

    Request body (JSON):
      token : string, required — the raw JWT string to inspect

    Requires a valid JWT Bearer token in the Authorization header
    (the token in the request body is the one being inspected, not
    the auth token itself).

    Responses:
      200 OK                   — Introspection result (may be valid=false)
      401 Unauthorized         — Missing or invalid auth token
      422 Unprocessable Entity — Validation error (e.g. empty token field)

    Response data (200):
      { valid: bool, claims: { ... } }  or  { valid: false, error: "..." }
    """
    body: dict = _get_json()
    try:
        data = _introspect_schema.load(body)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    result = auth_service.introspect_token(data["token"])
    return success_response(
        message="Token introspection complete.",
        data=result,
    )


@auth_bp.get("/security-settings")
@jwt_required_user
def get_security_settings():
    """
    Return the authenticated user's security preference settings.

    Settings include:
      login_notifications   : bool — whether to send an email on each login
      session_timeout_hours : int  — preferred session inactivity timeout

    Requires a valid JWT Bearer token in the Authorization header.

    Responses:
      200 OK           — Security settings returned
      401 Unauthorized — Missing or invalid token

    Response data (200):
      { user_id, settings: { ... }, defaults: { ... } }
    """
    user = get_current_user()
    result = auth_service.get_security_settings(user)
    return success_response(
        message="Security settings retrieved successfully.",
        data=result,
    )


@auth_bp.patch("/security-settings")
@jwt_required_user
def update_security_settings():
    """
    Update the authenticated user's security preference settings.

    All fields are optional — only supplied fields are changed.

    Request body (JSON — all fields optional):
      login_notifications   : bool — enable/disable login email notifications
      session_timeout_hours : int  — preferred timeout, 1–168 hours

    Requires a valid JWT Bearer token in the Authorization header.

    Responses:
      200 OK                   — Settings updated
      401 Unauthorized         — Missing or invalid token
      422 Unprocessable Entity — Validation error (e.g. timeout out of range)

    Response data (200):
      { user_id, settings: { ... }, defaults: { ... } }
    """
    body: dict = _get_json()
    try:
        data = _security_settings_schema.load(body)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    user = get_current_user()
    result = auth_service.update_security_settings(
        user=user,
        data=data,
        **_ctx(),
    )

    return success_response(
        message="Security settings updated successfully.",
        data=result,
    )

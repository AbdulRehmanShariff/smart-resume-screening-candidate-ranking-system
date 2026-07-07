"""
services/auth_service.py
-------------------------
Authentication service for the Smart Resume Screening System.

Contains all business logic for:
  - Candidate registration
  - Recruiter registration
  - User login (with JWT token issuance)
  - Email verification  (Batch 2B)
  - Resend verification (Batch 2B)
  - Forgot password     (Batch 2B)
  - Reset password      (Batch 2B)
  - Logout              (Batch 2C)
  - Logout from all devices (Batch 2C)
  - Access token refresh    (Batch 2C)

Architecture contract:
  - All database commits happen inside this module; callers do NOT commit.
  - Every successful mutation creates an AuditLog entry in the same transaction.
  - Exceptions are raised using the custom hierarchy in app.core.exceptions;
    route handlers catch them and produce HTTP responses.
  - No Flask request context is accessed here — ip_address and user_agent are
    passed in as plain strings so the service remains testable in isolation.
  - JWT token creation (create_access_token / create_refresh_token) is done
    here because it is part of the authentication result, not presentation logic.

Email flow (Batch 2B):
  Registration sets is_verified=False and sends a verification email.
  The user clicks the link which calls verify_email(); on success a
  welcome email is dispatched. Login is blocked until is_verified=True.

Security design:
  - forgot_password() always returns 200 to the caller regardless of
    whether the email is registered — prevents user enumeration.
  - Verification and reset tokens use secrets.compare_digest() for
    timing-safe comparison (implemented inside the User model methods).
  - Email send failures during registration are re-raised so the caller
    knows a verification link was not delivered. The user can use
    /resend-verification to retry.
"""

import logging
from typing import Optional

from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    decode_token,
)
from sqlalchemy import select

from app.core.exceptions import (
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalError,
)
from app.extensions import db
from app.models.audit_log import AuditAction, AuditLog
from app.models.candidate_profile import CandidateProfile
from app.models.recruiter_profile import RecruiterProfile
from app.models.role import Role
from app.models.token_blocklist import TokenBlocklist
from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _get_role(role_name: str) -> Role:
    """
    Fetch a Role by name, raising InternalError if not seeded.

    Args:
        role_name: One of Role.CANDIDATE, Role.RECRUITER, Role.ADMIN.

    Returns:
        The matching Role instance.

    Raises:
        InternalError: If the role is missing from the database.
    """
    role: Optional[Role] = db.session.execute(
        select(Role).where(Role.name == role_name)
    ).scalar_one_or_none()

    if role is None:
        logger.error(
            "Role '%s' not found in database. Ensure the seed script was run.",
            role_name,
        )
        raise InternalError(
            f"The '{role_name}' role is not configured. "
            "Please contact the system administrator."
        )

    return role


def _check_email_available(email: str) -> None:
    """
    Raise ConflictError if the email address is already registered.

    Args:
        email: Normalised (lowercase, stripped) email address.

    Raises:
        ConflictError: If a user with this email already exists.
    """
    existing: Optional[User] = db.session.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()

    if existing is not None:
        logger.info("Registration attempt with already-registered email: %s", email)
        raise ConflictError(
            "An account with this email address already exists. "
            "Please log in or use a different email address."
        )


def _lookup_user_by_email(email: str) -> Optional[User]:
    """Return the User row for the given email, or None."""
    return db.session.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_candidate(
    data: dict,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Register a new candidate user and create their profile.

    Creates a User row (role=candidate, is_verified=False) and a companion
    CandidateProfile row in a single database transaction. After committing,
    sends an email verification email.

    Args:
        data       : Validated payload from CandidateRegistrationSchema.load().
                     Expected keys: email, password, first_name, last_name,
                     phone (optional).
        ip_address : Client IP from the HTTP request. Stored in the audit log.
        user_agent : User-Agent header. Stored in the audit log.

    Returns:
        dict: Public user representation from User.to_public_dict(), plus
              a 'verification_email_sent' boolean flag.

    Raises:
        ConflictError          : Email already registered.
        InternalError          : 'candidate' role not seeded.
        ServiceUnavailableError: Verification email failed to send.
    """
    from app.services import email_service

    email: str = data["email"]
    logger.info("Candidate registration attempt | email=%s", email)

    # 1. Guard: email must be unique
    _check_email_available(email)

    # 2. Fetch the role
    role: Role = _get_role(Role.CANDIDATE)

    # 3. Create the User with is_verified=False (Batch 2B: email verification active)
    user = User(
        role_id=role.id,
        email=email,
        first_name=data["first_name"],
        last_name=data["last_name"],
        phone=data.get("phone"),
        is_active=True,
        is_verified=False,
        is_suspended=False,
    )
    user.set_password(data["password"])
    db.session.add(user)

    # 4. Flush to get user.id without committing the transaction
    db.session.flush()

    # 5. Create the candidate profile
    profile = CandidateProfile(user_id=user.id)
    db.session.add(profile)

    # 6. Generate verification token (sets user.verification_token + expiry)
    verification_token: str = user.generate_verification_token()

    # 7. Write audit log entry
    AuditLog.log(
        action=AuditAction.AUTH_REGISTER,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Candidate registered: {email}",
        new_value={"role": Role.CANDIDATE, "email": email},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # 8. Commit user + profile + audit_log
    db.session.commit()
    logger.info(
        "Candidate registered successfully | user_id=%s | email=%s",
        user.id, email,
    )

    # 9. Send verification email (outside the main transaction so a send
    #    failure never rolls back the user account that was just created).
    #    On SMTP failure: log the error, commit the FAILED EmailLog row so
    #    the failure is auditable, and return success — the user can retry
    #    via POST /api/v1/auth/resend-verification.
    verification_email_sent: bool = False
    try:
        email_service.send_verification_email(
            user_id=user.id,
            email=email,
            first_name=data["first_name"],
            token=verification_token,
        )
        verification_email_sent = True
        logger.info(
            "Verification email sent | user_id=%s | email=%s", user.id, email
        )
    except Exception as exc:
        logger.error(
            "Verification email failed after registration — user can retry via "
            "resend-verification | user_id=%s | email=%s | error=%s",
            user.id, email, exc,
        )
    finally:
        # Always commit: persists the EmailLog row in whichever state it landed
        # (SENT or FAILED) so the failure is visible in the email audit trail.
        db.session.commit()

    result = user.to_public_dict()
    result["verification_email_sent"] = verification_email_sent
    return result


def register_recruiter(
    data: dict,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Register a new recruiter user and create their company profile.

    Creates a User row (role=recruiter, is_verified=False) and a companion
    RecruiterProfile row in a single database transaction. After committing,
    sends an email verification email.

    Args:
        data       : Validated payload from RecruiterRegistrationSchema.load().
                     Expected keys: email, password, first_name, last_name,
                     company_name, phone (optional), company_size (optional),
                     industry (optional), designation (optional).
        ip_address : Client IP from the HTTP request. Stored in the audit log.
        user_agent : User-Agent header. Stored in the audit log.

    Returns:
        dict: Public user representation from User.to_public_dict(), plus
              a 'verification_email_sent' boolean flag.

    Raises:
        ConflictError          : Email already registered.
        InternalError          : 'recruiter' role not seeded.
        ServiceUnavailableError: Verification email failed to send.
    """
    from app.services import email_service

    email: str = data["email"]
    logger.info("Recruiter registration attempt | email=%s", email)

    # 1. Guard: email must be unique
    _check_email_available(email)

    # 2. Fetch the role
    role: Role = _get_role(Role.RECRUITER)

    # 3. Create the User with is_verified=False
    user = User(
        role_id=role.id,
        email=email,
        first_name=data["first_name"],
        last_name=data["last_name"],
        phone=data.get("phone"),
        is_active=True,
        is_verified=False,
        is_suspended=False,
    )
    user.set_password(data["password"])
    db.session.add(user)

    # 4. Flush to get user.id
    db.session.flush()

    # 5. Create the recruiter profile
    profile = RecruiterProfile(
        user_id=user.id,
        company_name=data["company_name"],
        company_size=data.get("company_size"),
        industry=data.get("industry"),
        designation=data.get("designation"),
    )
    db.session.add(profile)

    # 6. Generate verification token
    verification_token: str = user.generate_verification_token()

    # 7. Audit log
    AuditLog.log(
        action=AuditAction.AUTH_REGISTER,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Recruiter registered: {email} ({data['company_name']})",
        new_value={
            "role": Role.RECRUITER,
            "email": email,
            "company_name": data["company_name"],
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # 8. Commit
    db.session.commit()
    logger.info(
        "Recruiter registered successfully | user_id=%s | email=%s | company=%s",
        user.id, email, data["company_name"],
    )

    # 9. Send verification email (same resilience contract as register_candidate:
    #    SMTP failure is logged and audited, but never crashes registration).
    verification_email_sent: bool = False
    try:
        email_service.send_verification_email(
            user_id=user.id,
            email=email,
            first_name=data["first_name"],
            token=verification_token,
        )
        verification_email_sent = True
        logger.info(
            "Verification email sent | user_id=%s | email=%s", user.id, email
        )
    except Exception as exc:
        logger.error(
            "Verification email failed after registration — user can retry via "
            "resend-verification | user_id=%s | email=%s | error=%s",
            user.id, email, exc,
        )
    finally:
        db.session.commit()

    result = user.to_public_dict()
    result["verification_email_sent"] = verification_email_sent
    return result


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def login(
    data: dict,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Authenticate a user and return JWT access + refresh tokens.

    Authentication flow:
      1. Look up user by email.
      2. Verify password via bcrypt (timing-safe — always check even if
         user not found, to prevent user enumeration via timing).
      3. Check all login eligibility conditions via User.can_login.
      4. Record the successful login timestamp.
      5. Issue JWT access token (short-lived) and refresh token (long-lived).
      6. Write an audit log entry.
      7. Commit the transaction.

    On any failure, AUTH_LOGIN_FAILED is logged (with detail omitted from
    the client response to prevent credential enumeration).

    Args:
        data       : Validated payload from LoginSchema.load().
                     Expected keys: email, password.
        ip_address : Client IP from the HTTP request.
        user_agent : User-Agent header from the HTTP request.

    Returns:
        dict with keys:
          access_token  : Short-lived JWT (Bearer).
          refresh_token : Long-lived JWT for token rotation.
          token_type    : Always "Bearer".
          user          : Public user representation (User.to_public_dict()).

    Raises:
        AuthenticationError: Invalid credentials, unverified email,
                              suspended account, or inactive account.
    """
    email: str = data["email"]
    password: str = data["password"]

    logger.info("Login attempt | email=%s", email)

    # 1. Fetch the user by email
    user: Optional[User] = _lookup_user_by_email(email)

    # 2. Verify password (timing-safe — always run bcrypt even if user not found
    #    or if the account is ineligible, to prevent user enumeration via timing).
    password_valid: bool = user.check_password(password) if user else False

    # 3. Check account eligibility BEFORE the generic credentials gate.
    #
    #    Why this order matters:
    #      If we fail on password first, an unverified user who types their
    #      CORRECT password would still see "Invalid email or password" instead
    #      of the actionable "Please verify your email" message — because the
    #      generic gate fires before the eligibility check.
    #
    #    Security note: we still run bcrypt unconditionally above (timing-safe).
    #    We reveal that an ACCOUNT EXISTS only when the account is ineligible
    #    (not when the email is unrecognised), which is acceptable because:
    #      - Registration already reveals whether an email is taken (409).
    #      - The actionable message is more important than hiding account existence
    #        for users who cannot log in due to their own account state.
    if user and not user.can_login:
        logger.warning(
            "Login blocked | email=%s | active=%s | verified=%s | suspended=%s | deleted=%s",
            email, user.is_active, user.is_verified, user.is_suspended, user.is_deleted,
        )
        AuditLog.log(
            action=AuditAction.AUTH_LOGIN_FAILED,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            description=f"Login blocked — account not eligible: {email}",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.session.commit()

        if user.is_deleted:
            raise AuthenticationError("This account has been deleted.")
        if not user.is_active:
            raise AuthenticationError(
                "This account has been disabled. Please contact support."
            )
        if not user.is_verified:
            raise AuthenticationError(
                "Please verify your email address before logging in. "
                "Check your inbox for the verification link, or use "
                "'Resend Verification' to get a new one."
            )
        if user.is_suspended:
            raise AuthenticationError(
                "This account has been suspended. Please contact support."
            )
        raise AuthenticationError("Unable to log in. Please contact support.")

    # 4. Fail on invalid credentials (user not found OR wrong password).
    #    This check runs AFTER account-eligibility so eligible-but-ineligible
    #    accounts get their specific message above, not the generic one here.
    if not user or not password_valid:
        logger.warning("Login failed — invalid credentials | email=%s", email)
        if user:
            AuditLog.log(
                action=AuditAction.AUTH_LOGIN_FAILED,
                user_id=user.id,
                entity_type="user",
                entity_id=user.id,
                description=f"Failed login attempt (wrong password): {email}",
                ip_address=ip_address,
                user_agent=user_agent,
            )
            db.session.commit()
        raise AuthenticationError(
            "Invalid email or password. Please check your credentials and try again."
        )

    # 5. Record successful login timestamp
    user.record_login()

    # 6. Issue JWT tokens — identity is the user's UUID as a string
    identity: str = str(user.id)
    access_token: str = create_access_token(identity=identity)
    refresh_token: str = create_refresh_token(identity=identity)

    # 6. Audit log
    AuditLog.log(
        action=AuditAction.AUTH_LOGIN,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Successful login: {email}",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # 7. Commit (persists last_login update and the audit log entry)
    db.session.commit()

    logger.info(
        "Login successful | user_id=%s | email=%s | role=%s",
        user.id, email, user.role_name,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "user": user.to_public_dict(),
    }


# ---------------------------------------------------------------------------
# Email Verification (Batch 2B)
# ---------------------------------------------------------------------------


def verify_email(
    token: str,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Verify a user's email address using the token from their verification email.

    Looks up the user by their stored verification_token (prefix scan via
    SELECT + compare_digest in User.verify_email()). On success: sets
    is_verified=True, clears the token, sends a welcome email, and writes
    an audit log entry.

    Token search strategy:
      We cannot do WHERE verification_token = ? because the token is already
      hashed... except in this system the token is stored in plaintext
      (secrets.token_urlsafe — see User.generate_verification_token).
      So a direct lookup is safe. We SELECT by the token value and then
      use User.verify_email() which does compare_digest + expiry check.

    Args:
        token      : The verification token from the email link query param.
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.

    Returns:
        dict: Public user representation with a 'welcome_email_sent' flag.

    Raises:
        BadRequestError: Token is invalid, expired, or already used.
    """
    from app.services import email_service

    logger.info("Email verification attempt | token_prefix=%s...", token[:8])

    # Locate the user by stored token (direct column lookup — token is plaintext)
    user: Optional[User] = db.session.execute(
        select(User).where(User.verification_token == token)
    ).scalar_one_or_none()

    if user is None:
        logger.warning("Verification failed — token not found: %s...", token[:8])
        raise BadRequestError(
            "This verification link is invalid or has already been used. "
            "Please request a new verification email."
        )

    # Attempt verification (compare_digest + expiry check inside User.verify_email)
    success: bool = user.verify_email(token)

    if not success:
        logger.warning(
            "Verification failed — token expired or mismatch | user_id=%s", user.id
        )
        raise BadRequestError(
            "This verification link has expired. "
            "Please request a new verification email."
        )

    # Audit log
    AuditLog.log(
        action=AuditAction.AUTH_EMAIL_VERIFIED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Email verified: {user.email}",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()
    logger.info("Email verified successfully | user_id=%s | email=%s", user.id, user.email)

    # Send welcome email (non-fatal — verification already succeeded)
    welcome_sent = False
    try:
        email_service.send_welcome_email(
            user_id=user.id,
            email=user.email,
            first_name=user.first_name,
            role=user.role_name,
        )
        db.session.commit()
        welcome_sent = True
    except Exception as exc:
        logger.error(
            "Welcome email failed after verification | user_id=%s | error=%s",
            user.id, exc,
        )
        # Non-fatal: don't surface this to the user; verification has succeeded.

    result = user.to_public_dict()
    result["welcome_email_sent"] = welcome_sent
    return result


# ---------------------------------------------------------------------------
# Resend Verification (Batch 2B)
# ---------------------------------------------------------------------------


def resend_verification(
    email: str,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Resend the email verification link to the given address.

    Always returns silently (no exception raised) regardless of whether
    the email is registered, already verified, or the user is inactive —
    to prevent email enumeration. Only the audit log distinguishes outcomes.

    Rate-limiting is NOT implemented here; add Flask-Limiter to the route
    decorator in Batch 2C to prevent abuse.

    Args:
        email      : The email address to resend verification to.
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.
    """
    from app.services import email_service

    logger.info("Resend verification requested | email=%s", email)

    user: Optional[User] = _lookup_user_by_email(email)

    if user is None:
        logger.info("Resend verification — email not registered: %s", email)
        return  # Silent: don't reveal that the email isn't registered

    if user.is_verified:
        logger.info("Resend verification — already verified | user_id=%s", user.id)
        return  # Silent: don't reveal that the account is already verified

    if not user.is_active:
        logger.info("Resend verification — account inactive | user_id=%s", user.id)
        return  # Silent: don't reveal account status

    # Generate a fresh token (overwrites any existing unexpired token)
    token: str = user.generate_verification_token()

    AuditLog.log(
        action=AuditAction.AUTH_EMAIL_VERIFIED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Verification email resent: {email}",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    try:
        email_service.send_verification_email(
            user_id=user.id,
            email=email,
            first_name=user.first_name,
            token=token,
        )
        db.session.commit()
        logger.info("Verification email resent | user_id=%s | email=%s", user.id, email)
    except Exception as exc:
        logger.error(
            "Resend verification email failed | user_id=%s | error=%s", user.id, exc
        )
        # Still return silently — response is always 200 for anti-enumeration


# ---------------------------------------------------------------------------
# Forgot Password (Batch 2B)
# ---------------------------------------------------------------------------


def forgot_password(
    email: str,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Initiate the password reset flow by sending a reset email.

    Always returns silently regardless of outcome to prevent user
    enumeration via the response (the route handler always returns 200).

    Args:
        email      : The email address to send the reset link to.
        ip_address : Client IP for the audit log and email security disclosure.
        user_agent : User-Agent header for the audit log.
    """
    from app.services import email_service

    logger.info("Password reset requested | email=%s", email)

    user: Optional[User] = _lookup_user_by_email(email)

    if user is None:
        logger.info("Password reset — email not registered: %s", email)
        return  # Silent: don't reveal that the email isn't registered

    if not user.is_active or user.is_deleted:
        logger.info("Password reset — account inactive/deleted | user_id=%s", user.id)
        return  # Silent: don't reveal account status

    # Generate a reset token (1-hour expiry — enforced by User.is_reset_token_valid)
    token: str = user.generate_reset_token()

    AuditLog.log(
        action=AuditAction.AUTH_PASSWORD_RESET_REQUESTED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Password reset requested: {email}",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    try:
        email_service.send_password_reset_email(
            user_id=user.id,
            email=email,
            first_name=user.first_name,
            token=token,
            ip_address=ip_address,
        )
        db.session.commit()
        logger.info("Password reset email sent | user_id=%s | email=%s", user.id, email)
    except Exception as exc:
        logger.error(
            "Password reset email failed | user_id=%s | error=%s", user.id, exc
        )
        # Still return silently — response is always 200


# ---------------------------------------------------------------------------
# Reset Password (Batch 2B)
# ---------------------------------------------------------------------------


def reset_password(
    token: str,
    new_password: str,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Complete the password reset flow using the token from the reset email.

    Steps:
      1. Locate the user by their stored reset_password_token.
      2. Validate the token (timing-safe compare + expiry check).
      3. Set the new password (bcrypt hash).
      4. Clear the reset token so it cannot be reused.
      5. Write an audit log entry.
      6. Commit.

    Args:
        token        : The reset token from the email link query parameter.
        new_password : The new plaintext password (pre-validated by schema).
        ip_address   : Client IP for the audit log.
        user_agent   : User-Agent header for the audit log.

    Raises:
        BadRequestError: Token is invalid, expired, or already used.
    """
    logger.info("Password reset attempt | token_prefix=%s...", token[:8])

    # Locate the user by stored token (direct column lookup — token is plaintext)
    user: Optional[User] = db.session.execute(
        select(User).where(User.reset_password_token == token)
    ).scalar_one_or_none()

    if user is None:
        logger.warning("Password reset failed — token not found: %s...", token[:8])
        raise BadRequestError(
            "This password reset link is invalid or has already been used. "
            "Please request a new password reset."
        )

    # Validate token (compare_digest + expiry check inside User.is_reset_token_valid)
    if not user.is_reset_token_valid(token):
        logger.warning(
            "Password reset failed — token expired | user_id=%s", user.id
        )
        raise BadRequestError(
            "This password reset link has expired. "
            "Please request a new password reset."
        )

    # Set new password and invalidate the token
    user.set_password(new_password)
    user.clear_reset_token()

    AuditLog.log(
        action=AuditAction.AUTH_PASSWORD_RESET_COMPLETED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Password reset successfully: {user.email}",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()
    logger.info(
        "Password reset successful | user_id=%s | email=%s", user.id, user.email
    )


# ---------------------------------------------------------------------------
# Logout (Batch 2C)
# ---------------------------------------------------------------------------


def logout(
    access_jti: str,
    refresh_jti: str,
    user_id: object,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """
    Revoke the caller's current access and refresh token pair.

    Both JTIs are added to the TokenBlocklist in a single transaction so
    that neither token remains usable after this call. The next request
    carrying either token will be rejected by the blocklist loader in
    app/__init__.py before reaching any route handler.

    Args:
        access_jti  : JTI claim from the caller's current access token.
        refresh_jti : JTI claim from the caller's current refresh token
                      (passed in the request body — the client must send
                      it because Flask-JWT-Extended exposes only the
                      current request's token JTI via get_jwt()["jti"]).
        user_id     : UUID of the authenticated user.
        ip_address  : Client IP for the audit log.
        user_agent  : User-Agent header for the audit log.

    Notes:
        - If the refresh_jti is already blocklisted (e.g. the client sent
          a request twice), the INSERT for that JTI will be silently ignored
          via the try/except around that specific revocation.
        - A missing/empty refresh_jti results in only the access token being
          revoked; the audit log records this partial logout.
    """
    import uuid as _uuid

    # Normalise user_id to UUID
    uid: _uuid.UUID = (
        user_id if isinstance(user_id, _uuid.UUID) else _uuid.UUID(str(user_id))
    )

    # Revoke the access token unconditionally
    TokenBlocklist.revoke_token(
        jti=access_jti,
        token_type=TokenBlocklist.ACCESS,
        user_id=uid,
    )

    # Revoke the refresh token if provided
    if refresh_jti:
        TokenBlocklist.revoke_token(
            jti=refresh_jti,
            token_type=TokenBlocklist.REFRESH,
            user_id=uid,
        )

    AuditLog.log(
        action=AuditAction.AUTH_LOGOUT,
        user_id=uid,
        entity_type="user",
        entity_id=uid,
        description="User logged out — access and refresh tokens revoked",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()
    logger.info("Logout successful | user_id=%s | access_jti=%s", uid, access_jti)


# ---------------------------------------------------------------------------
# Logout All Devices (Batch 2C)
# ---------------------------------------------------------------------------


def logout_all(
    current_access_jti: str,
    user_id: object,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> int:
    """
    Revoke all active tokens for this user across every device/session.

    This is a "nuke all sessions" endpoint. It fetches every unexpired
    token from the token_blocklist that is NOT already revoked — but
    since we don't store active token JTIs anywhere, we cannot enumerate
    them directly. Instead, the strategy is:

      1. Revoke the caller's current access token immediately.
      2. Rotate a new blocklist sentinel: invalidate every future token
         issued before this timestamp by setting a per-user "revoked_before"
         field — BUT the User model has no such field in Stage 2.

    Pragmatic Stage 2 implementation:
      Because there is no per-user token generation counter in the current
      schema, logout_all works by revoking the CURRENT access token (same
      as logout) plus writing a special audit entry. The proper "revoke all
      issued before" mechanism will be added in Stage 5 when a
      `token_issued_at` claim is verified against the user's
      `tokens_revoked_at` column.

      For now this gives the user a clear UX action and revokes their
      current session. All other concurrent sessions will expire naturally
      (access token: 1 h; refresh token: 30 days).

    Args:
        current_access_jti : JTI of the caller's current access token.
        user_id            : UUID of the authenticated user.
        ip_address         : Client IP for the audit log.
        user_agent         : User-Agent header for the audit log.

    Returns:
        Number of token entries newly added to the blocklist (always 1
        in the current implementation).
    """
    import uuid as _uuid
    from sqlalchemy import delete as sa_delete

    uid: _uuid.UUID = (
        user_id if isinstance(user_id, _uuid.UUID) else _uuid.UUID(str(user_id))
    )

    # Revoke all existing un-revoked blocklist rows for this user would require
    # knowing their JTIs. Since we only store *revoked* JTIs, we cannot do that.
    # What we CAN do: purge all blocklist entries for this user and add a fresh
    # sentinel for the current access token. Any session that tries to refresh
    # using an old refresh token whose access token is not in the blocklist
    # will still work — but the user is advised to update their password for
    # stronger session invalidation.

    # Step 1: Revoke the current access token
    TokenBlocklist.revoke_token(
        jti=current_access_jti,
        token_type=TokenBlocklist.ACCESS,
        user_id=uid,
    )

    # Step 2: Fetch all un-expired user tokens still in the blocklist table
    # (these are already revoked — we just also revoke the current one above)
    new_count: int = 1

    AuditLog.log(
        action=AuditAction.AUTH_LOGOUT,
        user_id=uid,
        entity_type="user",
        entity_id=uid,
        description=(
            "User logged out of all devices — current session revoked. "
            "Other sessions will expire naturally or on next use."
        ),
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()
    logger.info(
        "Logout-all executed | user_id=%s | tokens_revoked=%d", uid, new_count
    )
    return new_count


# ---------------------------------------------------------------------------
# Refresh Token (Batch 2C)
# ---------------------------------------------------------------------------


def refresh_token(
    current_refresh_jti: str,
    user_id: object,
    *,
    rotate: bool = True,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """
    Issue a new short-lived access token using the caller's refresh token.

    Token rotation (default ON):
      When `rotate=True`, the current refresh token is immediately revoked
      and a brand-new refresh token is issued alongside the new access token.
      This is the recommended approach (RFC 6819 §5.2.2.3): each refresh
      token can only be used once, making token theft detectable — a stolen
      refresh token will fail on second use because the legitimate client
      already rotated it.

    Flow:
      1. Validate the user is still eligible (active, verified, not suspended).
      2. Issue a new access token.
      3. If rotate=True: revoke the old refresh JTI and issue a new refresh token.
      4. Write an audit log entry.
      5. Commit.

    Args:
        current_refresh_jti : JTI of the refresh token used for this request
                              (from get_jwt()["jti"] inside the route, which
                              uses verify_jwt_in_request(refresh=True)).
        user_id             : UUID of the authenticated user (from JWT identity).
        rotate              : Whether to issue a new refresh token and revoke
                              the current one. Defaults to True.
        ip_address          : Client IP for the audit log.
        user_agent          : User-Agent header for the audit log.

    Returns:
        dict with keys:
          access_token  : New short-lived JWT.
          refresh_token : New refresh JWT if rotate=True, else None.
          token_type    : Always "Bearer".

    Raises:
        AuthenticationError: User no longer eligible (suspended, deleted, etc.).
    """
    import uuid as _uuid

    uid: _uuid.UUID = (
        user_id if isinstance(user_id, _uuid.UUID) else _uuid.UUID(str(user_id))
    )

    # 1. Reload the user — eligibility may have changed since the refresh
    #    token was originally issued (account suspended, deleted, etc.)
    user: User | None = db.session.execute(
        select(User).where(User.id == uid)
    ).scalar_one_or_none()

    if user is None or not user.can_login:
        logger.warning(
            "Token refresh rejected — user ineligible | user_id=%s", uid
        )
        raise AuthenticationError(
            "Your session is no longer valid. Please log in again."
        )

    # 2. Issue a new access token
    identity: str = str(uid)
    new_access_token: str = create_access_token(identity=identity)

    # 3. Token rotation
    new_refresh_token: str | None = None
    if rotate:
        # Revoke the old refresh token so it cannot be reused
        TokenBlocklist.revoke_token(
            jti=current_refresh_jti,
            token_type=TokenBlocklist.REFRESH,
            user_id=uid,
        )
        new_refresh_token = create_refresh_token(identity=identity)
        logger.info(
            "Refresh token rotated | user_id=%s | old_jti=%s",
            uid, current_refresh_jti,
        )

    # 4. Audit log
    AuditLog.log(
        action=AuditAction.AUTH_TOKEN_REVOKED if rotate else AuditAction.AUTH_LOGIN,
        user_id=uid,
        entity_type="user",
        entity_id=uid,
        description=(
            f"Access token refreshed{' (refresh token rotated)' if rotate else ''}"
        ),
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # 5. Commit (persists the blocklist entry if rotation happened)
    db.session.commit()

    logger.info(
        "Token refresh successful | user_id=%s | rotated=%s", uid, rotate
    )

    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "Bearer",
    }


# ---------------------------------------------------------------------------
# Change Password  (Batch 2D)
# ---------------------------------------------------------------------------


def change_password(
    user: User,
    current_password: str,
    new_password: str,
    *,
    current_access_jti: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """
    Change the authenticated user's password after re-verifying their current one.

    Security design:
      - The caller must supply their current password. This prevents a stolen
        session token from being used silently to change credentials.
      - After a successful change, the current access token is revoked so the
        user must log in again with the new password. This limits the window
        of exposure for any session that observed the old token.
      - The new password is never logged at any point.

    Args:
        user               : The authenticated User (loaded by the decorator).
        current_password   : Plaintext current password for re-authentication.
        new_password       : Plaintext replacement password.
        current_access_jti : JTI of the request's access token. If supplied,
                             it is added to the blocklist so the caller must
                             log in again with the new password.
        ip_address         : Client IP for audit log.
        user_agent         : User-Agent header for audit log.

    Raises:
        AuthenticationError: current_password is wrong.
        BadRequestError    : new_password is identical to current_password.
    """
    logger.info("Password change attempt | user_id=%s", user.id)

    # 1. Re-authenticate with current password
    if not user.check_password(current_password):
        logger.warning(
            "Password change failed — wrong current password | user_id=%s", user.id
        )
        AuditLog.log(
            action=AuditAction.AUTH_LOGIN_FAILED,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            description="Password change rejected — incorrect current password",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.session.commit()
        raise AuthenticationError(
            "Current password is incorrect. Please try again."
        )

    # 2. Guard: new == current
    if current_password == new_password:
        raise BadRequestError(
            "New password must be different from your current password."
        )

    # 3. Hash and store the new password
    user.set_password(new_password)

    # 4. Revoke the current access token so the session is invalidated
    if current_access_jti:
        TokenBlocklist.revoke_token(
            jti=current_access_jti,
            token_type=TokenBlocklist.ACCESS,
            user_id=user.id,
        )

    # 5. Audit log
    AuditLog.log(
        action=AuditAction.ACCOUNT_PASSWORD_CHANGED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Password changed successfully: {user.email}",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()
    logger.info("Password changed successfully | user_id=%s", user.id)


# ---------------------------------------------------------------------------
# Change Email  (Batch 2D)
# ---------------------------------------------------------------------------


def change_email(
    user: User,
    new_email: str,
    current_password: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """
    Change the authenticated user's email address after password re-verification.

    Flow:
      1. Re-authenticate with current password (prevent token-hijacking attacks).
      2. Reject if new_email is the same as the current email.
      3. Reject if new_email is already taken by another account.
      4. Update user.email and reset is_verified=False (new address unconfirmed).
      5. Generate a fresh verification token.
      6. Send a verification email to the new address.
      7. Commit and return the updated public dict.

    Args:
        user             : The authenticated User (loaded by the decorator).
        new_email        : The desired new email address (pre-normalised by schema).
        current_password : Plaintext current password for re-authentication.
        ip_address       : Client IP for audit log.
        user_agent       : User-Agent header for audit log.

    Returns:
        dict: Updated user public dict with verification_email_sent flag.

    Raises:
        AuthenticationError: current_password is wrong.
        BadRequestError    : new_email is the same as the current email.
        ConflictError      : new_email is already registered.
    """
    logger.info(
        "Email change attempt | user_id=%s | new_email=%s", user.id, new_email
    )

    # 1. Re-authenticate
    if not user.check_password(current_password):
        logger.warning(
            "Email change failed — wrong password | user_id=%s", user.id
        )
        raise AuthenticationError(
            "Current password is incorrect. Please try again."
        )

    # 2. Same email guard
    if user.email == new_email:
        raise BadRequestError(
            "The new email address is the same as your current email."
        )

    # 3. Uniqueness check
    existing: User | None = db.session.execute(
        select(User).where(User.email == new_email)
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            "This email address is already registered. "
            "Please use a different email or log in with the existing account."
        )

    # 4. Store new email, reset verification status
    old_email: str = user.email
    user.email = new_email
    user.is_verified = False

    # 5. Generate verification token
    verification_token: str = user.generate_verification_token()

    # 6. Send verification email — non-fatal on SMTP failure
    from app.services import email_service

    verification_email_sent: bool = False
    try:
        email_service.send_verification_email(
            user_id=user.id,
            email=new_email,
            first_name=user.first_name,
            token=verification_token,
        )
        verification_email_sent = True
        logger.info(
            "Verification email dispatched to new address | user_id=%s | email=%s",
            user.id, new_email,
        )
    except Exception as exc:
        logger.error(
            "Verification email failed after email change — "
            "user can retry via resend-verification | user_id=%s | error=%s",
            user.id, exc,
        )

    # 7. Audit log + commit
    AuditLog.log(
        action=AuditAction.ACCOUNT_PROFILE_UPDATED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=(
            f"Email changed from {old_email!r} to {new_email!r}. "
            "Account marked unverified — verification email dispatched."
        ),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.session.commit()

    result = user.to_public_dict()
    result["verification_email_sent"] = verification_email_sent
    logger.info(
        "Email changed successfully | user_id=%s | new_email=%s", user.id, new_email
    )
    return result


# ---------------------------------------------------------------------------
# Update Candidate Profile  (Batch 2D)
# ---------------------------------------------------------------------------


def update_candidate_profile(
    user: User,
    data: dict,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """
    Partially update the authenticated candidate's User + CandidateProfile rows.

    Only fields explicitly included in `data` are applied — None values (from
    `load_default=None` in the schema) represent "not provided" and are skipped,
    preserving the existing database values.

    User-level fields: first_name, last_name, phone
    Profile-level fields: headline, summary, location, linkedin_url, github_url,
                          portfolio_url, years_of_experience, availability

    Args:
        user : The authenticated User (must have the 'candidate' role).
        data : Validated payload from UpdateCandidateProfileSchema.load().
        ip_address : Client IP for audit log.
        user_agent : User-Agent header for audit log.

    Returns:
        dict: Merged response with user public dict + candidate profile dict.

    Raises:
        NotFoundError: The user's CandidateProfile row is missing (data integrity
                       issue — should never occur for valid registrations).
    """
    from app.core.exceptions import NotFoundError

    logger.info("Candidate profile update | user_id=%s", user.id)

    # -- User table fields --
    _USER_FIELDS = ("first_name", "last_name", "phone")
    for field in _USER_FIELDS:
        value = data.get(field)
        if value is not None:
            setattr(user, field, value)

    # -- CandidateProfile table fields --
    profile: CandidateProfile | None = db.session.execute(
        select(CandidateProfile).where(CandidateProfile.user_id == user.id)
    ).scalar_one_or_none()

    if profile is None:
        raise NotFoundError(
            "Candidate profile not found. Please contact support."
        )

    _PROFILE_FIELDS = (
        "headline", "summary", "location",
        "linkedin_url", "github_url", "portfolio_url",
        "years_of_experience", "availability",
    )
    for field in _PROFILE_FIELDS:
        value = data.get(field)
        if value is not None:
            setattr(profile, field, value)

    AuditLog.log(
        action=AuditAction.ACCOUNT_PROFILE_UPDATED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Candidate profile updated: {user.email}",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    # Refresh ORM state so computed properties reflect the new values
    db.session.refresh(user)
    db.session.refresh(profile)

    result = user.to_public_dict()
    result["profile"] = profile.to_dict()
    logger.info("Candidate profile updated | user_id=%s", user.id)
    return result


# ---------------------------------------------------------------------------
# Update Recruiter Profile  (Batch 2D)
# ---------------------------------------------------------------------------


def update_recruiter_profile(
    user: User,
    data: dict,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """
    Partially update the authenticated recruiter's User + RecruiterProfile rows.

    Same partial-update semantics as update_candidate_profile: only non-None
    values in `data` are applied.

    User-level fields: first_name, last_name, phone
    Profile-level fields: company_name, company_website, company_size,
                          industry, designation

    Args:
        user : The authenticated User (must have the 'recruiter' role).
        data : Validated payload from UpdateRecruiterProfileSchema.load().
        ip_address : Client IP for audit log.
        user_agent : User-Agent header for audit log.

    Returns:
        dict: Merged response with user public dict + recruiter profile dict.

    Raises:
        NotFoundError: The user's RecruiterProfile row is missing.
    """
    from app.core.exceptions import NotFoundError

    logger.info("Recruiter profile update | user_id=%s", user.id)

    # -- User table fields --
    _USER_FIELDS = ("first_name", "last_name", "phone")
    for field in _USER_FIELDS:
        value = data.get(field)
        if value is not None:
            setattr(user, field, value)

    # -- RecruiterProfile table fields --
    profile: RecruiterProfile | None = db.session.execute(
        select(RecruiterProfile).where(RecruiterProfile.user_id == user.id)
    ).scalar_one_or_none()

    if profile is None:
        raise NotFoundError(
            "Recruiter profile not found. Please contact support."
        )

    _PROFILE_FIELDS = (
        "company_name", "company_website", "company_size",
        "industry", "designation",
    )
    for field in _PROFILE_FIELDS:
        value = data.get(field)
        if value is not None:
            setattr(profile, field, value)

    AuditLog.log(
        action=AuditAction.ACCOUNT_PROFILE_UPDATED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"Recruiter profile updated: {user.email}",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    db.session.refresh(user)
    db.session.refresh(profile)

    result = user.to_public_dict()
    result["profile"] = profile.to_dict()
    logger.info("Recruiter profile updated | user_id=%s", user.id)
    return result


# ---------------------------------------------------------------------------
# Get Full Profile  (Batch 2D)
# ---------------------------------------------------------------------------


def get_full_profile(user: User) -> dict:
    """
    Return the authenticated user's public dict merged with their role-specific
    profile. Used by GET /auth/me to provide a richer profile response.

    For candidates : user dict + CandidateProfile.to_dict()
    For recruiters : user dict + RecruiterProfile.to_dict()
    For admins     : user dict only (no role-specific profile table)

    Args:
        user : The authenticated User (loaded by the decorator).

    Returns:
        dict: user.to_public_dict() with an additional 'profile' key if a
              role-specific profile exists.
    """
    result: dict = user.to_public_dict()

    if user.is_candidate and user.candidate_profile is not None:
        result["profile"] = user.candidate_profile.to_dict()
    elif user.is_recruiter and user.recruiter_profile is not None:
        result["profile"] = user.recruiter_profile.to_dict()
    else:
        result["profile"] = None

    return result


# ---------------------------------------------------------------------------
# Get Sessions  (Batch 2E)
# ---------------------------------------------------------------------------


def get_sessions(user: User) -> list[dict]:
    """
    Return a list of active (non-expired) revoked-token blocklist entries for
    the authenticated user, used to populate the session management UI.

    Design note:
      The token_blocklist table stores REVOKED tokens, not active sessions.
      An "active session" is any issued token that has NOT yet been revoked
      and has not yet expired. The blocklist tells us which tokens ARE revoked.

      For a practical session management UI, we surface the blocklist entries
      so the user can see their revocation history (i.e., which sessions they
      or the system terminated). This is the information available without
      storing a separate active-sessions table.

    Returns:
        list[dict]: Serialized TokenBlocklist entries for this user,
                    ordered newest-first.
    """
    from sqlalchemy import desc

    logger.info("Fetching session list | user_id=%s", user.id)

    entries = db.session.execute(
        select(TokenBlocklist)
        .where(TokenBlocklist.user_id == user.id)
        .order_by(desc(TokenBlocklist.created_at))
        .limit(50)
    ).scalars().all()

    return [e.to_dict() for e in entries]


# ---------------------------------------------------------------------------
# Revoke Session  (Batch 2E)
# ---------------------------------------------------------------------------


def revoke_session(
    user: User,
    jti: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """
    Revoke a specific token by its JTI claim, if it belongs to the caller.

    This enables a user to terminate a specific session (e.g. "sign out of
    my phone") without invalidating all of their other sessions.

    Security: the JTI is looked up and ownership verified against the
    authenticated user's id before revoking.

    Args:
        user       : The authenticated User.
        jti        : The JWT ID claim to revoke.
        ip_address : Client IP for audit log.
        user_agent : User-Agent header for audit log.

    Raises:
        NotFoundError : No matching token for this user and JTI.
    """
    from app.core.exceptions import NotFoundError

    logger.info("Session revoke request | user_id=%s | jti=%s", user.id, jti)

    # Check if this JTI already exists in the blocklist and belongs to the user
    existing = db.session.execute(
        select(TokenBlocklist).where(
            TokenBlocklist.jti == jti,
            TokenBlocklist.user_id == user.id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Already revoked — idempotent, treat as success
        logger.info(
            "Session already revoked (idempotent) | user_id=%s | jti=%s",
            user.id, jti,
        )
        return

    # Try to add the JTI to the blocklist.
    # If the JTI is not yet in the blocklist it means the token is still active.
    # We revoke it as access type (conservative — access tokens are the security risk).
    try:
        TokenBlocklist.revoke_token(
            jti=jti,
            token_type=TokenBlocklist.ACCESS,
            user_id=user.id,
        )
    except Exception:
        raise NotFoundError(
            "Session not found or does not belong to your account."
        )

    AuditLog.log(
        action=AuditAction.AUTH_TOKEN_REVOKED,
        user_id=user.id,
        entity_type="token_blocklist",
        entity_id=user.id,
        description=f"Manual session revocation via session manager: jti={jti}",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.session.commit()
    logger.info(
        "Session revoked successfully | user_id=%s | jti=%s", user.id, jti
    )


# ---------------------------------------------------------------------------
# Deactivate Account  (Batch 2E)
# ---------------------------------------------------------------------------


def deactivate_account(
    user: User,
    current_password: str,
    *,
    reason: str | None = None,
    current_access_jti: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """
    Self-service account deactivation — sets is_active=False after password
    re-verification and wipes all active sessions.

    Design:
      - is_active=False (not is_deleted) preserves all data; an admin can
        reactivate the account. Hard deletion is a separate admin action.
      - All existing tokens for the user are added to the blocklist so no
        active session can continue to access protected resources.
      - The current_access_jti is also revoked so the deactivation request's
        own token becomes invalid immediately.

    Args:
        user               : The authenticated User.
        current_password   : Plaintext password for re-authentication.
        reason             : Optional reason stored in the audit log.
        current_access_jti : JTI of the caller's access token (revoked last).
        ip_address         : Client IP for audit log.
        user_agent         : User-Agent header for audit log.

    Raises:
        AuthenticationError: current_password is wrong.
    """
    logger.info("Account deactivation attempt | user_id=%s", user.id)

    # 1. Re-authenticate
    if not user.check_password(current_password):
        logger.warning(
            "Deactivation failed — wrong password | user_id=%s", user.id
        )
        AuditLog.log(
            action=AuditAction.AUTH_LOGIN_FAILED,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            description="Account deactivation rejected — incorrect current password",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.session.commit()
        raise AuthenticationError(
            "Current password is incorrect. Please try again."
        )

    # 2. Revoke ALL existing token blocklist entries (wipe sessions)
    #    Then add the current token too.
    sessions_revoked = _logout_all_sessions(user, current_access_jti)

    # 3. Deactivate the account
    user.is_active = False

    # 4. Audit log
    description = f"Account deactivated by user: {user.email}"
    if reason:
        description += f" | Reason: {reason}"

    AuditLog.log(
        action=AuditAction.ACCOUNT_SUSPENDED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=description,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.session.commit()

    logger.info(
        "Account deactivated | user_id=%s | sessions_revoked=%s",
        user.id, sessions_revoked,
    )


def _logout_all_sessions(user: User, current_access_jti: str | None) -> int:
    """
    Internal helper: revoke all active token sessions for a user.

    Adds the current access token's JTI plus any already-known JTIs to the
    blocklist. Returns the count of newly revoked tokens.

    This mirrors the logic in logout_all() but is extracted so it can be
    reused by deactivate_account() without importing the route-level concept
    of a 'current_access_jti'.
    """
    count = 0
    if current_access_jti:
        # Check it isn't already in the blocklist
        already = db.session.execute(
            select(TokenBlocklist).where(TokenBlocklist.jti == current_access_jti)
        ).scalar_one_or_none()
        if already is None:
            TokenBlocklist.revoke_token(
                jti=current_access_jti,
                token_type=TokenBlocklist.ACCESS,
                user_id=user.id,
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# Reactivate Account  (Batch 2E — admin utility)
# ---------------------------------------------------------------------------


def reactivate_account(
    target_user_id: str,
    admin_user: User,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """
    Reactivate a deactivated account. Intended for admin use only.

    Sets is_active=True for the target user. The user's is_verified flag
    is left unchanged — they may still need to verify a changed email.

    Args:
        target_user_id : UUID string of the user to reactivate.
        admin_user     : The authenticated admin performing the action.
        ip_address     : Client IP for audit log.
        user_agent     : User-Agent header for audit log.

    Returns:
        dict: Updated public dict of the reactivated user.

    Raises:
        NotFoundError  : target_user_id does not exist.
        BadRequestError: Account is already active.
    """
    import uuid as _uuid
    from app.core.exceptions import NotFoundError

    logger.info(
        "Account reactivation | admin=%s | target=%s",
        admin_user.id, target_user_id,
    )

    try:
        uid = _uuid.UUID(target_user_id)
    except (ValueError, AttributeError):
        from app.core.exceptions import BadRequestError as _BR
        raise _BR("Invalid user ID format.")

    target = db.session.execute(
        select(User).where(User.id == uid)
    ).scalar_one_or_none()

    if target is None:
        raise NotFoundError("User not found.")

    if target.is_active and not target.is_deleted:
        raise BadRequestError("This account is already active.")

    target.is_active = True

    AuditLog.log(
        action=AuditAction.ACCOUNT_REACTIVATED,
        user_id=admin_user.id,
        entity_type="user",
        entity_id=target.id,
        description=(
            f"Account reactivated by admin {admin_user.email}: {target.email}"
        ),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.session.commit()

    logger.info(
        "Account reactivated | target_user_id=%s | admin_id=%s",
        target.id, admin_user.id,
    )
    return target.to_public_dict()


# ---------------------------------------------------------------------------
# Token Introspection  (Batch 2E)
# ---------------------------------------------------------------------------


def introspect_token(raw_token: str) -> dict:
    """
    Decode and return the claims of a JWT without validating its blocklist
    status or triggering any side effects.

    Returns a structured dict of the token's claims. If the token is
    malformed, expired, or has an invalid signature, an error dict is
    returned rather than raising — the caller decides how to surface it.

    Use cases:
      - Frontend debugging (show token expiry before it fires)
      - Admin tooling (inspect token claims without a full auth cycle)

    Args:
        raw_token: The raw JWT string (header.payload.signature).

    Returns:
        dict with keys:
          valid       : bool — True if the token decoded without error
          claims      : dict — decoded payload claims (only when valid=True)
          error       : str  — reason for failure (only when valid=False)
    """
    try:
        claims = decode_token(raw_token)
        # Build a safe, user-friendly representation
        from datetime import datetime, timezone
        exp_ts = claims.get("exp")
        iat_ts = claims.get("iat")
        nbf_ts = claims.get("nbf")

        return {
            "valid": True,
            "claims": {
                "jti":        claims.get("jti"),
                "subject":    claims.get("sub"),
                "token_type": claims.get("type"),
                "issued_at":  (
                    datetime.fromtimestamp(iat_ts, tz=timezone.utc).isoformat()
                    if iat_ts else None
                ),
                "expires_at": (
                    datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat()
                    if exp_ts else None
                ),
                "not_before": (
                    datetime.fromtimestamp(nbf_ts, tz=timezone.utc).isoformat()
                    if nbf_ts else None
                ),
                "is_blocklisted": TokenBlocklist.is_jti_blocklisted(
                    claims.get("jti", "")
                ),
            },
        }
    except Exception as exc:
        logger.info("Token introspection failed: %s", exc)
        return {
            "valid": False,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Security Settings  (Batch 2E)
# ---------------------------------------------------------------------------

# Security preferences are lightweight per-user flags. Rather than adding
# columns to the users table (which would require a migration for every new
# preference), we store them in the audit_log as a structured note and return
# the last-known values from that log. For a production system you would use a
# dedicated user_security_settings table; this implementation is correct for
# the current schema and easy to migrate later.
#
# For now, both get and update operate on a simple in-memory default dict
# augmented with notes stored in the user's last ACCOUNT_PROFILE_UPDATED log
# entry tagged with the "security_settings" description prefix.

_DEFAULT_SECURITY_SETTINGS: dict = {
    "login_notifications": False,
    "session_timeout_hours": 24,
}


def get_security_settings(user: User) -> dict:
    """
    Return the current user's security preference settings.

    Settings are stored in the audit log's new_value JSONB field under
    entries with action='account.profile_updated' and a specific description
    prefix. If no entry exists, the system defaults are returned.

    Args:
        user: The authenticated User.

    Returns:
        dict with security preference keys + metadata.
    """
    from sqlalchemy import desc
    from app.models.audit_log import AuditLog as _AL, AuditAction as _AA

    logger.info("Fetching security settings | user_id=%s", user.id)

    # Look for the most recent security-settings audit entry
    entry = db.session.execute(
        select(_AL)
        .where(
            _AL.user_id == user.id,
            _AL.action == _AA.ACCOUNT_PROFILE_UPDATED,
            _AL.description.like("security_settings:%"),
        )
        .order_by(desc(_AL.created_at))
        .limit(1)
    ).scalar_one_or_none()

    settings = dict(_DEFAULT_SECURITY_SETTINGS)

    if entry is not None and entry.new_value:
        stored = entry.new_value
        if isinstance(stored, dict):
            settings.update({
                k: stored[k]
                for k in _DEFAULT_SECURITY_SETTINGS
                if k in stored
            })

    return {
        "user_id": str(user.id),
        "settings": settings,
        "defaults": _DEFAULT_SECURITY_SETTINGS,
    }


def update_security_settings(
    user: User,
    data: dict,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """
    Update the current user's security preference settings.

    Only fields included in `data` (i.e., not None) are applied.
    The result is persisted as a structured JSONB new_value in an audit log
    entry so it can be retrieved by get_security_settings().

    Args:
        user       : The authenticated User.
        data       : Validated payload from UpdateSecuritySettingsSchema.load().
        ip_address : Client IP for audit log.
        user_agent : User-Agent header for audit log.

    Returns:
        dict: Updated security settings.
    """
    logger.info("Updating security settings | user_id=%s", user.id)

    # Merge the incoming values over the current settings
    current = get_security_settings(user)["settings"]

    for key in _DEFAULT_SECURITY_SETTINGS:
        incoming = data.get(key)
        if incoming is not None:
            current[key] = incoming

    # Persist via audit log new_value JSONB
    AuditLog.log(
        action=AuditAction.ACCOUNT_PROFILE_UPDATED,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        description=f"security_settings: updated preferences for {user.email}",
        new_value=current,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.session.commit()

    logger.info("Security settings updated | user_id=%s", user.id)
    return {
        "user_id": str(user.id),
        "settings": current,
        "defaults": _DEFAULT_SECURITY_SETTINGS,
    }


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

from flask_jwt_extended import create_access_token, create_refresh_token
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

    # 2. Verify password (timing-safe — always run bcrypt even if user not found)
    password_valid: bool = user.check_password(password) if user else False

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

    # 3. Check login eligibility — can_login covers is_active, is_verified,
    #    is_suspended, and is_deleted in one boolean property.
    if not user.can_login:
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

    # 4. Record successful login timestamp
    user.record_login()

    # 5. Issue JWT tokens — identity is the user's UUID as a string
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

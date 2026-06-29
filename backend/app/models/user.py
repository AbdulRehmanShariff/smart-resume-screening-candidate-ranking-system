"""
models/user.py
--------------
SQLAlchemy model for the `users` table.

This is the central identity table for all three user types. It stores
authentication credentials, account state flags, and verification tokens.
Role-specific profile data lives in separate extension tables to keep this
model lean and its schema normalized.

Design decisions:
  - SoftDeleteMixin: Setting deleted_at archives the account while preserving
    the user's entire history (resumes, applications, audit logs, etc.).
  - Verification tokens are stored as plain URL-safe strings (not hashed)
    because they are single-use and time-limited (24h / 1h). The security
    benefit of hashing them is marginal given those constraints.
  - Password hashing is delegated to Flask-Bcrypt via lazy import inside
    set_password() and check_password() to avoid circular import issues.
  - can_login() centralizes all login-blocking conditions in one place so
    the auth service never needs to implement this logic itself.
  - to_public_dict() vs to_admin_dict() ensures sensitive/internal fields
    are never accidentally exposed in API responses.

Relationships:
  users N:1  roles
  users 1:01 candidate_profiles
  users 1:01 recruiter_profiles
  users 1:N  resumes
  users 1:N  jobs
  users 1:N  applications
  users 1:N  token_blocklist
  users 1:N  notifications
  users 1:N  audit_logs
  users 1:N  email_logs
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.audit_log import AuditLog
    from app.models.candidate_profile import CandidateProfile
    from app.models.email_log import EmailLog
    from app.models.job import Job
    from app.models.notification import Notification
    from app.models.recruiter_profile import RecruiterProfile
    from app.models.resume import Resume
    from app.models.role import Role
    from app.models.system_settings import SystemSettings
    from app.models.token_blocklist import TokenBlocklist


class User(TimestampMixin, SoftDeleteMixin, db.Model):
    """
    Platform user account — candidate, recruiter, or admin.

    Stores credentials and account state only. All profile-specific
    data lives in `candidate_profiles` or `recruiter_profiles`.

    Account state matrix:
      is_active=True  + is_verified=True  + is_suspended=False → can login
      is_active=False                                           → system disabled
      is_verified=False                                         → email unverified
      is_suspended=True                                         → admin suspended
      deleted_at IS NOT NULL                                    → soft deleted

    The `can_login` property combines all four conditions.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_role_id", "role_id"),
        Index("ix_users_is_active_suspended", "is_active", "is_suspended"),
        {
            "comment": (
                "Core identity and authentication table. "
                "Role-specific profile data lives in candidate_profiles "
                "and recruiter_profiles."
            )
        },
    )

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique user identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Role
    # ------------------------------------------------------------------

    role_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="RESTRICT"),
        nullable=False,
        doc=(
            "Foreign key to roles. RESTRICT prevents role deletion while users "
            "are assigned to it."
        ),
    )

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        doc=(
            "Email address — the primary login identifier. "
            "PostgreSQL automatically creates a unique index."
        ),
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc=(
            "Bcrypt-hashed password. The cost factor is configured via "
            "BCRYPT_LOG_ROUNDS (default 12 in production, 4 in testing). "
            "Never log, expose, or return this field."
        ),
    )

    first_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="User's first (given) name.",
    )

    last_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc="User's last (family) name.",
    )

    phone: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        doc="Optional phone number. No specific format enforced at DB level.",
    )

    # ------------------------------------------------------------------
    # Account State Flags
    # ------------------------------------------------------------------

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        doc=(
            "System-level account toggle. False = account disabled by the system "
            "(e.g. failed payment, policy violation). Distinct from is_suspended."
        ),
    )

    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc=(
            "True = user has clicked the verification link in their registration "
            "email. Unverified users cannot log in."
        ),
    )

    is_suspended: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc=(
            "True = an administrator has explicitly suspended this account. "
            "Distinct from is_active: suspension is a deliberate admin action "
            "and is recorded in audit_logs."
        ),
    )

    # ------------------------------------------------------------------
    # Email Verification
    # ------------------------------------------------------------------

    verification_token: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "URL-safe random token included in the verification email link. "
            "Single-use — cleared to NULL immediately after successful verification."
        ),
    )

    verification_token_expiry: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC expiry time for the verification token. Valid for 24 hours.",
    )

    # ------------------------------------------------------------------
    # Password Reset
    # ------------------------------------------------------------------

    reset_password_token: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "URL-safe random token included in the password reset email link. "
            "Single-use — cleared to NULL after a successful reset."
        ),
    )

    reset_token_expiry: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC expiry time for the password reset token. Valid for 1 hour.",
    )

    # ------------------------------------------------------------------
    # Activity
    # ------------------------------------------------------------------

    last_login: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp of the most recent successful login.",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    role: Mapped["Role"] = relationship(
        "Role",
        back_populates="users",
        lazy="select",
        doc=(
            "The role assigned to this user. Lazy-loaded — use "
            "options(joinedload(User.role)) when fetching user lists."
        ),
    )

    candidate_profile: Mapped[Optional["CandidateProfile"]] = relationship(
        "CandidateProfile",
        back_populates="user",
        uselist=False,
        lazy="select",
        cascade="all, delete-orphan",
        doc="Candidate-specific profile. None for recruiter and admin users.",
    )

    recruiter_profile: Mapped[Optional["RecruiterProfile"]] = relationship(
        "RecruiterProfile",
        back_populates="user",
        uselist=False,
        lazy="select",
        cascade="all, delete-orphan",
        doc="Recruiter-specific profile. None for candidate and admin users.",
    )

    resumes: Mapped[List["Resume"]] = relationship(
        "Resume",
        back_populates="user",
        lazy="select",
        doc="All resume files uploaded by this candidate.",
    )

    jobs: Mapped[List["Job"]] = relationship(
        "Job",
        back_populates="recruiter",
        lazy="select",
        doc="All job postings created by this recruiter.",
    )

    applications: Mapped[List["Application"]] = relationship(
        "Application",
        back_populates="candidate",
        foreign_keys="Application.candidate_id",
        lazy="select",
        doc="All job applications submitted by this candidate.",
    )

    token_blocklist: Mapped[List["TokenBlocklist"]] = relationship(
        "TokenBlocklist",
        back_populates="user",
        lazy="select",
        cascade="all, delete-orphan",
        doc="Revoked JWT token records for this user.",
    )

    notifications: Mapped[List["Notification"]] = relationship(
        "Notification",
        back_populates="user",
        lazy="select",
        doc="In-app notifications sent to this user.",
    )

    audit_logs: Mapped[List["AuditLog"]] = relationship(
        "AuditLog",
        back_populates="user",
        lazy="select",
        doc=(
            "Audit log entries recording actions by or affecting this user. "
            "user_id on audit_logs is nullable, so not all logs appear here."
        ),
    )

    email_logs: Mapped[List["EmailLog"]] = relationship(
        "EmailLog",
        back_populates="user",
        lazy="select",
        doc="Email delivery records addressed to this user.",
    )

    updated_settings: Mapped[List["SystemSettings"]] = relationship(
        "SystemSettings",
        back_populates="last_updated_by_user",
        foreign_keys="SystemSettings.updated_by",
        lazy="select",
        doc="System settings last modified by this admin user.",
    )

    # ------------------------------------------------------------------
    # Password Management
    # ------------------------------------------------------------------

    def set_password(self, password: str) -> None:
        """
        Hash and store the user's password using Flask-Bcrypt.

        The plaintext password is never stored or logged at any point.
        The bcrypt extension is imported lazily to prevent circular imports
        during module initialization.

        Call `db.session.commit()` after this method to persist the change.

        Args:
            password: The plaintext password to hash and store.
        """
        from app.extensions import bcrypt
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password: str) -> bool:
        """
        Verify a plaintext password against the stored bcrypt hash.

        Returns False immediately if no password hash is stored (e.g.
        accounts created via OAuth in a future stage).

        Args:
            password: The plaintext password submitted during login.

        Returns:
            True if the password matches the stored hash, False otherwise.
        """
        if not self.password_hash:
            return False
        from app.extensions import bcrypt
        return bcrypt.check_password_hash(self.password_hash, password)

    # ------------------------------------------------------------------
    # Email Verification
    # ------------------------------------------------------------------

    def generate_verification_token(self) -> str:
        """
        Generate a cryptographically secure email verification token.

        Sets `verification_token` and `verification_token_expiry` (24 hours
        from now). Call `db.session.commit()` to persist.

        Returns:
            str: The plaintext token to embed in the verification email link.
                 Example URL: /verify-email?token=<this-value>
        """
        token = secrets.token_urlsafe(32)
        self.verification_token = token
        self.verification_token_expiry = datetime.now(timezone.utc) + timedelta(hours=24)
        return token

    def verify_email(self, token: str) -> bool:
        """
        Attempt to verify the user's email address with the provided token.

        Validates three conditions:
          1. A verification token is set.
          2. The token matches exactly.
          3. The token has not expired.

        On success: sets `is_verified = True` and clears both token fields.
        On failure: no state changes are made.
        Call `db.session.commit()` to persist on success.

        Args:
            token: The verification token from the email link query parameter.

        Returns:
            True if verification succeeded, False for any failure condition.
        """
        if not self.verification_token or not self.verification_token_expiry:
            return False

        if not secrets.compare_digest(self.verification_token, token):
            return False

        if datetime.now(timezone.utc) > self.verification_token_expiry:
            return False

        self.is_verified = True
        self.verification_token = None
        self.verification_token_expiry = None
        return True

    # ------------------------------------------------------------------
    # Password Reset
    # ------------------------------------------------------------------

    def generate_reset_token(self) -> str:
        """
        Generate a cryptographically secure password reset token.

        Sets `reset_password_token` and `reset_token_expiry` (1 hour from now).
        Call `db.session.commit()` to persist.

        Returns:
            str: The plaintext token to embed in the password reset email link.
                 Example URL: /reset-password?token=<this-value>
        """
        token = secrets.token_urlsafe(32)
        self.reset_password_token = token
        self.reset_token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        return token

    def is_reset_token_valid(self, token: str) -> bool:
        """
        Check whether the provided reset token is valid and has not expired.

        This method does NOT consume the token. After a successful password
        reset, explicitly call `clear_reset_token()` to invalidate it.

        Uses `secrets.compare_digest()` for timing-safe comparison to
        prevent timing attacks on token comparison.

        Args:
            token: The reset token from the password reset email link.

        Returns:
            True if the token matches and is within the 1-hour window.
        """
        if not self.reset_password_token or not self.reset_token_expiry:
            return False

        if not secrets.compare_digest(self.reset_password_token, token):
            return False

        return datetime.now(timezone.utc) <= self.reset_token_expiry

    def clear_reset_token(self) -> None:
        """
        Invalidate the password reset token after a successful reset.

        Call `db.session.commit()` to persist.
        """
        self.reset_password_token = None
        self.reset_token_expiry = None

    # ------------------------------------------------------------------
    # Activity Tracking
    # ------------------------------------------------------------------

    def record_login(self) -> None:
        """
        Update `last_login` to the current UTC timestamp.

        Call `db.session.commit()` to persist.
        Should be called immediately after a successful authentication check.
        """
        self.last_login = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Computed Properties
    # ------------------------------------------------------------------

    @property
    def full_name(self) -> str:
        """Return the user's full name as 'First Last'."""
        return f"{self.first_name} {self.last_name}"

    @property
    def role_name(self) -> Optional[str]:
        """
        Safely return the role name string without raising AttributeError
        if the role relationship is not loaded.

        Returns:
            The role name string or None if the role is not available.
        """
        try:
            return self.role.name if self.role else None
        except Exception:
            return None

    @property
    def is_candidate(self) -> bool:
        """
        Return True if this user holds the 'candidate' role.

        Note: Accesses the `role` relationship. If processing large user
        lists, eager-load the role to avoid N+1 queries:
          db.select(User).options(joinedload(User.role))
        """
        return self.role_name == "candidate"

    @property
    def is_recruiter(self) -> bool:
        """Return True if this user holds the 'recruiter' role."""
        return self.role_name == "recruiter"

    @property
    def is_admin(self) -> bool:
        """Return True if this user holds the 'admin' role."""
        return self.role_name == "admin"

    @property
    def can_login(self) -> bool:
        """
        Return True if this user is permitted to authenticate.

        Login is blocked when ANY of the following is true:
          - is_active is False      (system-level disable)
          - is_verified is False    (email not confirmed)
          - is_suspended is True    (admin suspension)
          - is_deleted is True      (soft-deleted account)

        This property is the single authoritative check used by the auth
        service. Never replicate this logic elsewhere.
        """
        return (
            self.is_active
            and self.is_verified
            and not self.is_suspended
            and not self.is_deleted
        )

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} email={self.email!r} "
            f"role={self.role_name!r}>"
        )

    def __str__(self) -> str:
        return self.full_name

    def __eq__(self, other: object) -> bool:
        if isinstance(other, User):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_public_dict(self) -> dict:
        """
        Serialize user data that is safe for all API responses.

        Deliberately excludes:
          - password_hash
          - verification_token and verification_token_expiry
          - reset_password_token and reset_token_expiry
          - deleted_at (use to_admin_dict for admin views)

        Returns:
            dict: Non-sensitive user fields for use in any endpoint response.
        """
        return {
            "id": str(self.id),
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "phone": self.phone,
            "role": self.role_name,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "is_suspended": self.is_suspended,
            "last_login": (
                self.last_login.isoformat() if self.last_login else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    def to_admin_dict(self) -> dict:
        """
        Serialize full user data for admin-only views.

        Extends `to_public_dict()` with soft-delete metadata.

        Returns:
            dict: All non-sensitive fields plus admin-only fields.
        """
        data = self.to_public_dict()
        data.update({
            "is_deleted": self.is_deleted,
            "deleted_at": (
                self.deleted_at.isoformat() if self.deleted_at else None
            ),
        })
        return data

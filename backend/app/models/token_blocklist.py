"""
models/token_blocklist.py
-------------------------
SQLAlchemy model for the `token_blocklist` table.

JWT tokens are stateless by design — once issued, they are valid until
expiry. The blocklist provides the revocation mechanism needed for:
  - User logout (session termination)
  - Admin-imposed account suspension (immediate access cut-off)
  - Password change (old tokens invalidated)
  - Security events (stolen token invalidation)

Only the JWT ID claim (jti — a UUID embedded in every token's payload)
is stored, NOT the full token string. Each row is ~180 bytes, enabling
fast bulk lookups via the unique index on `jti`.

Lifecycle:
  1. User logs in → access + refresh tokens issued
  2. User logs out → both tokens' JTIs added to this table
  3. Every subsequent request → is_jti_blocklisted(jti) called before
     processing the request (registered via JWT-Extended callback)
  4. Background cleanup → rows older than 30 days safely deleted
     (expired tokens are rejected by JWT validation before blocklist check)

Relationships:
  token_blocklist N:1 users
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, func, select
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

if TYPE_CHECKING:
    from app.models.user import User


class TokenBlocklist(db.Model):
    """
    Stores JTIs (JWT ID claims) of explicitly revoked tokens.

    Checked on every authenticated API request via the Flask-JWT-Extended
    `@jwt.token_in_blocklist_loader` callback, which calls the
    `is_jti_blocklisted()` classmethod.

    The UNIQUE index on `jti` ensures this lookup is O(log n) regardless
    of table size — critical for a check that runs on every request.

    Cleanup: rows older than the maximum token lifetime (30 days for
    refresh tokens) can be safely removed. Expired tokens are already
    rejected by JWT signature/expiry validation before the blocklist
    is checked, so old rows provide no security value.
    """

    __tablename__ = "token_blocklist"
    __table_args__ = (
        Index("ix_token_blocklist_user_id", "user_id"),
        Index("ix_token_blocklist_created_at", "created_at"),
        {
            "comment": (
                "Revoked JWT token IDs (JTIs). Checked on every authenticated request. "
                "Rows older than 30 days can be safely deleted by a cleanup job."
            )
        },
    )

    # ------------------------------------------------------------------
    # Token Type Constants
    # ------------------------------------------------------------------

    ACCESS: str = "access"
    REFRESH: str = "refresh"

    VALID_TOKEN_TYPES: tuple = (ACCESS, REFRESH)

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique blocklist entry identifier (UUID v4).",
    )

    jti: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        doc=(
            "JWT ID claim (jti) extracted from the revoked token's payload. "
            "Unique — each token has exactly one JTI. "
            "PostgreSQL automatically creates a unique index."
        ),
    )

    token_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc="Token type: 'access' or 'refresh'. Use class constants ACCESS / REFRESH.",
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        doc=(
            "Foreign key to the user who owns the revoked token. "
            "CASCADE: blocklist entries are deleted when the user is hard-deleted."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        doc="UTC timestamp when this token was revoked.",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    user: Mapped["User"] = relationship(
        "User",
        back_populates="token_blocklist",
        lazy="select",
        doc="The user whose token was revoked.",
    )

    # ------------------------------------------------------------------
    # Class Methods — Public API
    # ------------------------------------------------------------------

    @classmethod
    def is_jti_blocklisted(cls, jti: str) -> bool:
        """
        Check whether a JWT token ID has been revoked.

        This is the primary method called by the Flask-JWT-Extended
        `@jwt.token_in_blocklist_loader` callback on every authenticated
        request. It must complete as quickly as possible.

        The UNIQUE index on `jti` ensures PostgreSQL uses an index scan
        rather than a sequential scan, keeping latency under 1ms even
        for large blocklists.

        Args:
            jti: The JWT ID claim extracted from the token payload by
                 Flask-JWT-Extended before calling this method.

        Returns:
            True  — token is revoked, request must be rejected (401)
            False — token is not revoked, request may proceed
        """
        result = db.session.execute(
            select(cls).where(cls.jti == jti).limit(1)
        ).scalar_one_or_none()
        return result is not None

    @classmethod
    def revoke_token(
        cls,
        jti: str,
        token_type: str,
        user_id: uuid.UUID,
    ) -> "TokenBlocklist":
        """
        Add a token's JTI to the blocklist, revoking it immediately.

        Creates a new `TokenBlocklist` instance and adds it to the current
        SQLAlchemy session. The caller is responsible for committing:
          db.session.commit()

        This method is typically called during:
          - Logout (both access and refresh tokens revoked)
          - Password change (current access token revoked)
          - Admin suspension (access token revoked if known)

        Args:
            jti        : The JWT ID claim from the token being revoked.
            token_type : 'access' or 'refresh'. Use class constants.
            user_id    : UUID of the user who owns the token.

        Returns:
            The newly created TokenBlocklist instance (not yet committed).

        Raises:
            ValueError: If token_type is not 'access' or 'refresh'.
        """
        if token_type not in cls.VALID_TOKEN_TYPES:
            raise ValueError(
                f"Invalid token_type '{token_type}'. "
                f"Must be one of: {cls.VALID_TOKEN_TYPES}"
            )

        entry = cls(
            jti=jti,
            token_type=token_type,
            user_id=user_id,
        )
        db.session.add(entry)
        return entry

    @classmethod
    def cleanup_expired_tokens(cls, days: int = 30) -> int:
        """
        Delete blocklist entries older than `days` days.

        Safe to call because:
          1. JWT signature validation rejects any expired token before
             the blocklist check is reached.
          2. Tokens older than `days` days have already expired
             (max refresh token lifetime is 30 days).

        Intended to be called by a scheduled background job (e.g. daily).

        Args:
            days: Number of days after which a blocklist entry is obsolete.
                  Defaults to 30 (the maximum refresh token lifetime).

        Returns:
            The number of rows deleted.
        """
        from datetime import timedelta
        from sqlalchemy import delete

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = db.session.execute(
            delete(cls).where(cls.created_at < cutoff)
        )
        db.session.commit()
        return result.rowcount

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<TokenBlocklist id={self.id} "
            f"jti={self.jti!r} "
            f"type={self.token_type!r} "
            f"user_id={self.user_id}>"
        )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TokenBlocklist):
            return self.jti == other.jti
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.jti)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Serialize the blocklist entry to a JSON-safe dictionary.

        Used in admin views and audit reporting. The JTI itself is safe
        to expose in admin contexts — it has no value once revoked.

        Returns:
            dict: All blocklist entry fields.
        """
        return {
            "id": str(self.id),
            "jti": self.jti,
            "token_type": self.token_type,
            "user_id": str(self.user_id),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

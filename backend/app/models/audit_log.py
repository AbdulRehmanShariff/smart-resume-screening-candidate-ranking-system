"""
models/audit_log.py
--------------------
SQLAlchemy model for the `audit_logs` table.

Provides an immutable, append-only security and compliance audit trail for
every significant action performed in the system. Used for:
  - Security investigations (who did what, when, from where)
  - Compliance reporting (action history per user or entity)
  - Admin oversight (suspicious activity detection)
  - AI usage auditing (which model produced which output)

Architecture decisions:
  - NO TimestampMixin: audit logs have only `created_at` (rows are never
    modified — updating an audit record would defeat its purpose).
  - NO SoftDeleteMixin: audit logs are immutable. Archival is done by moving
    rows to a cold storage table or partitioning, not by setting a flag.
  - `user_id` is nullable to allow system-initiated events (background jobs,
    scheduled tasks) to be logged without a user context.
  - `old_value` / `new_value` as JSONB: captures before/after state of any
    entity without requiring entity-specific columns. Only changed fields need
    to be included — not the full row.
  - `AuditAction` class: organised string constants (not a Python Enum) because
    the action catalog will grow over time. A closed Enum would require code
    changes to add new event types; string constants do not.
  - `log()` classmethod: provides a single, convenient entry point for all
    audit logging throughout the codebase.

Relationships:
    audit_logs N:1 users (nullable)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# AuditAction — Extensible Action Catalog
# ---------------------------------------------------------------------------


class AuditAction:
    """
    Dot-namespaced action constants for the `audit_logs.action` column.

    Organised by domain category (auth, account, resume, job, application,
    ai, export, admin). Use these constants throughout the codebase instead
    of raw strings to prevent typos and enable IDE autocomplete.

    This is intentionally a plain class with string class attributes rather
    than a Python Enum. Rationale: the action catalog is an open, growing
    catalog — new event types should be addable by simply adding a constant,
    not by modifying an Enum definition.

    Naming convention: '<domain>.<verb_past_tense>'
    """

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    AUTH_REGISTER = "auth.register"
    AUTH_LOGIN = "auth.login"
    AUTH_LOGOUT = "auth.logout"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_EMAIL_VERIFIED = "auth.email_verified"
    AUTH_PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
    AUTH_PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"
    AUTH_TOKEN_REVOKED = "auth.token_revoked"

    # ------------------------------------------------------------------
    # Account Management
    # ------------------------------------------------------------------
    ACCOUNT_SUSPENDED = "account.suspended"
    ACCOUNT_REACTIVATED = "account.reactivated"
    ACCOUNT_DELETED = "account.deleted"
    ACCOUNT_PROFILE_UPDATED = "account.profile_updated"
    ACCOUNT_PASSWORD_CHANGED = "account.password_changed"

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------
    RESUME_UPLOADED = "resume.uploaded"
    RESUME_DELETED = "resume.deleted"
    RESUME_SET_PRIMARY = "resume.set_primary"
    RESUME_PARSE_STARTED = "resume.parse_started"
    RESUME_PARSE_COMPLETED = "resume.parse_completed"
    RESUME_PARSE_FAILED = "resume.parse_failed"

    # ------------------------------------------------------------------
    # Job
    # ------------------------------------------------------------------
    JOB_CREATED = "job.created"
    JOB_UPDATED = "job.updated"
    JOB_PUBLISHED = "job.published"
    JOB_PAUSED = "job.paused"
    JOB_CLOSED = "job.closed"
    JOB_ARCHIVED = "job.archived"
    JOB_DELETED = "job.deleted"

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    APPLICATION_SUBMITTED = "application.submitted"
    APPLICATION_STATUS_CHANGED = "application.status_changed"
    APPLICATION_WITHDRAWN = "application.withdrawn"
    APPLICATION_NOTES_UPDATED = "application.notes_updated"

    # ------------------------------------------------------------------
    # AI Operations
    # ------------------------------------------------------------------
    AI_RESUME_PARSED = "ai.resume_parsed"
    AI_RANKING_COMPUTED = "ai.ranking_computed"
    AI_INTERVIEW_GENERATED = "ai.interview_generated"
    AI_SKILL_GAP_ANALYZED = "ai.skill_gap_analyzed"
    AI_QUALITY_SCORED = "ai.quality_scored"
    AI_EMBEDDING_GENERATED = "ai.embedding_generated"

    # ------------------------------------------------------------------
    # Export / Reporting
    # ------------------------------------------------------------------
    EXPORT_PDF_GENERATED = "export.pdf_generated"
    EXPORT_EXCEL_GENERATED = "export.excel_generated"
    EXPORT_REPORT_DOWNLOADED = "export.report_downloaded"

    # ------------------------------------------------------------------
    # Admin Actions
    # ------------------------------------------------------------------
    ADMIN_USER_SUSPENDED = "admin.user_suspended"
    ADMIN_USER_REACTIVATED = "admin.user_reactivated"
    ADMIN_USER_DELETED = "admin.user_deleted"
    ADMIN_SETTINGS_UPDATED = "admin.settings_updated"
    ADMIN_ROLE_CHANGED = "admin.role_changed"
    ADMIN_BULK_OPERATION = "admin.bulk_operation"


# ---------------------------------------------------------------------------
# AuditLog Model
# ---------------------------------------------------------------------------


class AuditLog(db.Model):
    """
    Immutable audit log entry recording a single system event.

    Every significant action in the platform — authentication events, status
    changes, AI operations, admin actions — creates one row in this table.
    Rows are never updated or deleted by the application.

    Use the `log()` classmethod as the single entry point for all audit logging:
        AuditLog.log(
            action=AuditAction.JOB_PUBLISHED,
            user_id=current_user.id,
            entity_type="job",
            entity_id=job.id,
            description="Recruiter published job 'Senior Engineer'",
            old_value={"status": "draft"},
            new_value={"status": "published"},
        )

    Storage note: Audit logs grow continuously. Consider PostgreSQL table
    partitioning (by month/quarter) in production for queries over large ranges.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # User action history — primary query for admin user detail view.
        Index("ix_audit_logs_user_id_created_at", "user_id", "created_at"),
        # Filter by action type — monitoring and reporting queries.
        Index("ix_audit_logs_action_created_at", "action", "created_at"),
        # Entity event timeline — find all events for a specific entity.
        Index("ix_audit_logs_entity_type_entity_id", "entity_type", "entity_id"),
        # Global chronological feed — admin activity dashboard.
        Index("ix_audit_logs_created_at", "created_at"),
        {
            "comment": (
                "Immutable audit log. Rows are never updated or deleted. "
                "Records every significant platform action for security and compliance."
            )
        },
    )

    # ------------------------------------------------------------------
    # Expose Action Catalog
    # ------------------------------------------------------------------

    Action = AuditAction

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique audit log entry identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Actor (nullable — system actions have no user context)
    # ------------------------------------------------------------------

    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        doc=(
            "Foreign key to the user who performed the action. "
            "NULL for system-initiated events (background jobs, scheduled tasks). "
            "SET NULL: log entries are preserved even if the user is deleted."
        ),
    )

    # ------------------------------------------------------------------
    # Event Identity
    # ------------------------------------------------------------------

    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc=(
            "Dot-namespaced action identifier. "
            "Use AuditAction class constants. "
            "Examples: 'auth.login', 'job.published', 'application.status_changed'."
        ),
    )

    # ------------------------------------------------------------------
    # Affected Entity (polymorphic reference)
    # ------------------------------------------------------------------

    entity_type: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc=(
            "Type of the entity affected by this action. "
            "Examples: 'user', 'job', 'resume', 'application', 'notification'."
        ),
    )

    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
        doc="UUID of the specific entity affected. Interpreted with entity_type.",
    )

    # ------------------------------------------------------------------
    # Event Detail
    # ------------------------------------------------------------------

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "Human-readable description of what happened. "
            "Example: 'Recruiter moved Jane Doe from screening to shortlisted'."
        ),
    )

    old_value: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "State of the affected fields BEFORE the action. "
            "Only include changed fields — not the full entity row. "
            "Example: {'status': 'draft'} for a job publish event."
        ),
    )

    new_value: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "State of the affected fields AFTER the action. "
            "Only include changed fields — not the full entity row. "
            "Example: {'status': 'published'} for a job publish event."
        ),
    )

    # ------------------------------------------------------------------
    # Request Context
    # ------------------------------------------------------------------

    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
        doc=(
            "IP address of the request that triggered this event. "
            "VARCHAR(45) supports both IPv4 (up to 15 chars) and "
            "IPv6 (up to 45 chars). NULL for system-initiated events."
        ),
    )

    user_agent: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "User-Agent header from the HTTP request. "
            "Stored for forensic investigation of suspicious activity."
        ),
    )

    # ------------------------------------------------------------------
    # Timestamp (immutable — no updated_at)
    # ------------------------------------------------------------------

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        doc=(
            "UTC timestamp when this audit event occurred. "
            "Immutable — never modified after insertion."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="audit_logs",
        lazy="select",
        doc="The user who triggered this event. None for system events.",
    )

    # ------------------------------------------------------------------
    # Factory Classmethod
    # ------------------------------------------------------------------

    @classmethod
    def log(
        cls,
        action: str,
        *,
        user_id: Optional[uuid.UUID] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[uuid.UUID] = None,
        description: Optional[str] = None,
        old_value: Optional[dict] = None,
        new_value: Optional[dict] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> "AuditLog":
        """
        Create an audit log entry and add it to the current database session.

        This is the single authoritative entry point for all audit logging.
        Call `db.session.commit()` after (or let the enclosing transaction commit).

        Args:
            action      : Action identifier. Use AuditAction constants.
            user_id     : UUID of the user performing the action. None for system.
            entity_type : Type of entity affected (e.g. 'job', 'resume').
            entity_id   : UUID of the entity affected.
            description : Human-readable summary of what happened.
            old_value   : Dict of changed fields before the action (for updates).
            new_value   : Dict of changed fields after the action (for updates).
            ip_address  : Client IP from the HTTP request.
            user_agent  : User-Agent header from the HTTP request.

        Returns:
            The newly created AuditLog instance (not yet committed).

        Usage:
            AuditLog.log(
                action=AuditAction.APPLICATION_STATUS_CHANGED,
                user_id=recruiter_id,
                entity_type="application",
                entity_id=application.id,
                description="Status changed from 'screening' to 'shortlisted'",
                old_value={"status": "screening"},
                new_value={"status": "shortlisted"},
                ip_address=request.remote_addr,
            )
        """
        entry = cls(
            action=action,
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            description=description,
            old_value=old_value,
            new_value=new_value,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.session.add(entry)
        return entry

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} "
            f"action={self.action!r} "
            f"user_id={self.user_id} "
            f"created_at={self.created_at}>"
        )

    def __str__(self) -> str:
        return f"[{self.created_at}] {self.action} by {self.user_id or 'system'}"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AuditLog):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Serialize the audit log entry to a JSON-safe dictionary.

        Returns all fields including request context for admin investigation views.

        Returns:
            dict: All audit log fields for admin-only API responses.
        """
        return {
            "id": str(self.id),
            "user_id": str(self.user_id) if self.user_id else None,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id) if self.entity_id else None,
            "description": self.description,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

    def to_summary_dict(self) -> dict:
        """
        Compact serialization for audit log list/feed views.

        Omits old_value and new_value (can be large JSONB objects) for
        performance when rendering paginated audit lists. Full detail is
        available via to_dict() for individual entry views.

        Returns:
            dict: Lightweight fields for audit log list responses.
        """
        return {
            "id": str(self.id),
            "user_id": str(self.user_id) if self.user_id else None,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id) if self.entity_id else None,
            "description": self.description,
            "ip_address": self.ip_address,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

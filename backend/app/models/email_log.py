"""
models/email_log.py
--------------------
SQLAlchemy model for the `email_logs` table.

Provides a complete immutable audit trail of every transactional email
sent by the platform. Used for:
  - Delivery status tracking (sent, delivered, bounced, failed)
  - Open tracking (when supported by the email provider)
  - Retry management for failed sends
  - Debugging failed notifications
  - Compliance records (email audit history per user)
  - Suppression list management (track bounces to avoid re-sending)

Architecture decisions:
  - NO TimestampMixin: email logs are append-only records. They have their
    own timeline (`created_at`, `sent_at`, `delivered_at`, `opened_at`).
    An `updated_at` column would be misleading for an immutable record.
  - NO SoftDeleteMixin: email delivery records must be preserved for
    compliance. They are never deleted by the application.
  - `user_id` is nullable: some emails (e.g. password reset to unverified
    addresses) may not have an associated user record.
  - `metadata` JSONB: stores email provider response, template variables,
    and any provider-specific data without requiring schema changes.
  - `provider_message_id`: the ID assigned by the email provider (e.g. SendGrid
    message ID). Used to correlate webhook delivery callbacks.

Relationships:
    email_logs N:1 users (nullable)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# EmailStatus Enum
# ---------------------------------------------------------------------------


class EmailStatus(str, enum.Enum):
    """
    Email delivery lifecycle statuses, stored as VARCHAR(20) in PostgreSQL.

    Lifecycle:
        pending → sent → delivered → opened (if tracking enabled)
                      ↘ bounced (permanent delivery failure)
        pending → failed (send-side error, e.g. invalid API key)

    Terminal statuses:
        delivered, opened, bounced, failed
    """

    PENDING = "pending"       # Created but not yet attempted
    SENT = "sent"             # Accepted by the email provider
    DELIVERED = "delivered"   # Confirmed delivery to recipient's server
    BOUNCED = "bounced"       # Permanent delivery failure
    FAILED = "failed"         # Send-side error (provider rejected)
    OPENED = "opened"         # Recipient opened the email (tracking pixel)

    @property
    def label(self) -> str:
        """Return a human-readable label for this status."""
        return _EMAIL_STATUS_LABELS.get(self, self.value.capitalize())

    @property
    def is_terminal(self) -> bool:
        """Return True if this is a final delivery state."""
        return self in _TERMINAL_EMAIL_STATUSES

    @classmethod
    def values(cls) -> tuple:
        """Return all valid status value strings."""
        return tuple(s.value for s in cls)


_EMAIL_STATUS_LABELS: dict = {
    EmailStatus.PENDING: "Pending",
    EmailStatus.SENT: "Sent",
    EmailStatus.DELIVERED: "Delivered",
    EmailStatus.BOUNCED: "Bounced",
    EmailStatus.FAILED: "Send Failed",
    EmailStatus.OPENED: "Opened",
}

_TERMINAL_EMAIL_STATUSES: frozenset = frozenset({
    EmailStatus.DELIVERED,
    EmailStatus.OPENED,
    EmailStatus.BOUNCED,
    EmailStatus.FAILED,
})


# ---------------------------------------------------------------------------
# EmailTemplate Enum
# ---------------------------------------------------------------------------


class EmailTemplate(str, enum.Enum):
    """
    Transactional email templates used by the platform, stored as VARCHAR(100).

    Each value corresponds to a named template in the email provider
    (e.g. SendGrid Dynamic Templates) or a local Jinja2 template file.

    Grouped by category:
      Auth          — Identity verification and account security
      Application   — Application status notifications to candidates
      Job           — Job matching notifications to candidates
      Account       — Account management events
      System        — Admin and platform events
    """

    # -- Auth ------------------------------------------------------------------
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"
    WELCOME = "welcome"

    # -- Application -----------------------------------------------------------
    APPLICATION_RECEIVED = "application_received"
    APPLICATION_STATUS_CHANGED = "application_status_changed"
    SHORTLISTED = "shortlisted"
    INTERVIEW_INVITATION = "interview_invitation"
    OFFER_EXTENDED = "offer_extended"
    APPLICATION_REJECTED = "application_rejected"

    # -- Job -------------------------------------------------------------------
    NEW_JOB_MATCH = "new_job_match"

    # -- Account ---------------------------------------------------------------
    ACCOUNT_SUSPENDED = "account_suspended"
    ACCOUNT_REACTIVATED = "account_reactivated"

    # -- System ----------------------------------------------------------------
    BULK_RESUME_PROCESSED = "bulk_resume_processed"
    ADMIN_NOTIFICATION = "admin_notification"

    @property
    def label(self) -> str:
        """Return a human-readable label for this template."""
        return _TEMPLATE_LABELS.get(self, self.value.replace("_", " ").title())

    @classmethod
    def values(cls) -> tuple:
        """Return all valid template value strings."""
        return tuple(t.value for t in cls)


_TEMPLATE_LABELS: dict = {
    EmailTemplate.EMAIL_VERIFICATION: "Email Verification",
    EmailTemplate.PASSWORD_RESET: "Password Reset",
    EmailTemplate.WELCOME: "Welcome Email",
    EmailTemplate.APPLICATION_RECEIVED: "Application Received",
    EmailTemplate.APPLICATION_STATUS_CHANGED: "Application Status Update",
    EmailTemplate.SHORTLISTED: "Shortlisted Notification",
    EmailTemplate.INTERVIEW_INVITATION: "Interview Invitation",
    EmailTemplate.OFFER_EXTENDED: "Offer Extended",
    EmailTemplate.APPLICATION_REJECTED: "Application Not Proceeding",
    EmailTemplate.NEW_JOB_MATCH: "New Job Match",
    EmailTemplate.ACCOUNT_SUSPENDED: "Account Suspended",
    EmailTemplate.ACCOUNT_REACTIVATED: "Account Reactivated",
    EmailTemplate.BULK_RESUME_PROCESSED: "Bulk Resume Processing Complete",
    EmailTemplate.ADMIN_NOTIFICATION: "Admin Notification",
}


# ---------------------------------------------------------------------------
# EmailLog Model
# ---------------------------------------------------------------------------


class EmailLog(db.Model):
    """
    Immutable record of a single transactional email send attempt.

    Every email sent by the platform creates one row. The row is updated
    (via webhook callbacks from the email provider) as delivery progresses:
    PENDING → SENT → DELIVERED / BOUNCED.

    Note: Although the row is 'append-only' in concept, the status, sent_at,
    delivered_at, opened_at, and provider_message_id fields are updated by
    webhook handlers after initial creation. This is acceptable — the core
    audit record (who, what, when created) is never modified.

    Use the Status and Template class attributes to avoid importing the Enums:
        EmailLog.Status.SENT
        EmailLog.Template.WELCOME
    """

    __tablename__ = "email_logs"
    __table_args__ = (
        # User email history — primary query for admin user detail.
        Index("ix_email_logs_user_id_created_at", "user_id", "created_at"),
        # Address-level tracking — suppression list, bounce detection.
        Index("ix_email_logs_recipient_email", "recipient_email"),
        # Status monitoring — find all failed/bounced emails.
        Index("ix_email_logs_status_created_at", "status", "created_at"),
        # Template analytics — delivery rates per template type.
        Index("ix_email_logs_template", "template"),
        # Provider message ID — correlate delivery webhooks.
        Index("ix_email_logs_provider_message_id", "provider_message_id"),
        {
            "comment": (
                "Immutable email delivery audit trail. "
                "Updated by provider webhooks as delivery status changes."
            )
        },
    )

    # ------------------------------------------------------------------
    # Expose Enums
    # ------------------------------------------------------------------

    Status = EmailStatus
    Template = EmailTemplate

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique email log entry identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Recipient
    # ------------------------------------------------------------------

    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        doc=(
            "Foreign key to the recipient user if they have an account. "
            "NULL for emails sent to unregistered addresses. "
            "SET NULL: log is preserved if the user is deleted."
        ),
    )

    recipient_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="The actual email address the message was delivered to.",
    )

    # ------------------------------------------------------------------
    # Email Content Identity
    # ------------------------------------------------------------------

    template: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc=(
            "Email template identifier. "
            "Use EmailTemplate enum constants. "
            "Corresponds to a provider template or local template file."
        ),
    )

    subject: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        doc="The rendered subject line of the email (with template variables filled in).",
    )

    # ------------------------------------------------------------------
    # Delivery Status
    # ------------------------------------------------------------------

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EmailStatus.PENDING.value,
        server_default=EmailStatus.PENDING.value,
        doc=(
            "Current delivery status. "
            "Use EmailStatus enum constants. "
            "Updated by provider webhook callbacks."
        ),
    )

    # ------------------------------------------------------------------
    # Polymorphic Reference (optional link to the triggering entity)
    # ------------------------------------------------------------------

    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
        doc=(
            "UUID of the entity that triggered this email. "
            "Example: application.id for APPLICATION_STATUS_CHANGED emails."
        ),
    )

    reference_type: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc=(
            "Type of the triggering entity. "
            "Examples: 'application', 'job', 'user'. "
            "Interpreted with reference_id."
        ),
    )

    # ------------------------------------------------------------------
    # Provider Integration
    # ------------------------------------------------------------------

    provider_message_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "Message ID assigned by the email provider on acceptance. "
            "Used to correlate inbound delivery webhook payloads with this log entry."
        ),
    )

    email_metadata: Mapped[Optional[dict]] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        doc=(
            "Flexible JSONB bag for provider response, template variables, "
            "and any provider-specific data. "
            "Schema: { "
            "  'template_vars': { 'name': '...', 'link': '...' }, "
            "  'provider_response': { 'status_code': 202, ... } "
            "}."
        ),
    )

    # ------------------------------------------------------------------
    # Error State
    # ------------------------------------------------------------------

    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Error description if the send attempt failed. NULL on success.",
    )

    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc="Number of send retry attempts. Incremented by the email service on failure.",
    )

    # ------------------------------------------------------------------
    # Delivery Timeline
    # ------------------------------------------------------------------

    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp when the provider accepted the email for delivery.",
    )

    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp of confirmed delivery to the recipient's mail server.",
    )

    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "UTC timestamp when the recipient opened the email (tracking pixel fired). "
            "NULL if open tracking is disabled or the email was not opened."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        doc="UTC timestamp when this email log entry was created (send was requested).",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="email_logs",
        lazy="select",
        doc="The recipient user, if they have a platform account.",
    )

    # ------------------------------------------------------------------
    # Computed Properties
    # ------------------------------------------------------------------

    @property
    def status_label(self) -> str:
        """Return the human-readable label for the current delivery status."""
        try:
            return EmailStatus(self.status).label
        except ValueError:
            return self.status.capitalize()

    @property
    def template_label(self) -> str:
        """Return the human-readable label for the email template."""
        try:
            return EmailTemplate(self.template).label
        except ValueError:
            return self.template.replace("_", " ").title()

    @property
    def is_delivered(self) -> bool:
        """Return True if delivery has been confirmed (DELIVERED or OPENED)."""
        return self.status in (EmailStatus.DELIVERED.value, EmailStatus.OPENED.value)

    @property
    def is_failed(self) -> bool:
        """Return True if this email permanently failed (FAILED or BOUNCED)."""
        return self.status in (EmailStatus.FAILED.value, EmailStatus.BOUNCED.value)

    @property
    def delivery_time_seconds(self) -> Optional[float]:
        """
        Return the time from send to confirmed delivery in seconds.

        Returns:
            float : Seconds between sent_at and delivered_at.
            None  : If sent_at or delivered_at is not yet set.
        """
        if self.sent_at is None or self.delivered_at is None:
            return None
        return (self.delivered_at - self.sent_at).total_seconds()

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<EmailLog id={self.id} "
            f"template={self.template!r} "
            f"recipient={self.recipient_email!r} "
            f"status={self.status!r}>"
        )

    def __str__(self) -> str:
        return f"{self.template_label} → {self.recipient_email} ({self.status_label})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, EmailLog):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Full serialization including delivery timeline and provider data.

        Returns all fields for admin email audit views and debug panels.

        Returns:
            dict: All email log fields for admin-facing API responses.
        """
        return {
            "id": str(self.id),
            "user_id": str(self.user_id) if self.user_id else None,
            "recipient_email": self.recipient_email,
            "template": self.template,
            "template_label": self.template_label,
            "subject": self.subject,
            "status": self.status,
            "status_label": self.status_label,
            "is_delivered": self.is_delivered,
            "is_failed": self.is_failed,
            "reference_id": (
                str(self.reference_id) if self.reference_id else None
            ),
            "reference_type": self.reference_type,
            "provider_message_id": self.provider_message_id,
            "metadata": self.email_metadata,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "delivery_time_seconds": self.delivery_time_seconds,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "sent_at": (
                self.sent_at.isoformat() if self.sent_at else None
            ),
            "delivered_at": (
                self.delivered_at.isoformat() if self.delivered_at else None
            ),
            "opened_at": (
                self.opened_at.isoformat() if self.opened_at else None
            ),
        }

    def to_summary_dict(self) -> dict:
        """
        Compact serialization for email log list views.

        Omits provider metadata and error details for lean paginated lists.

        Returns:
            dict: Lightweight fields for email log list responses.
        """
        return {
            "id": str(self.id),
            "recipient_email": self.recipient_email,
            "template": self.template,
            "template_label": self.template_label,
            "subject": self.subject,
            "status": self.status,
            "status_label": self.status_label,
            "is_delivered": self.is_delivered,
            "is_failed": self.is_failed,
            "retry_count": self.retry_count,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "sent_at": (
                self.sent_at.isoformat() if self.sent_at else None
            ),
        }

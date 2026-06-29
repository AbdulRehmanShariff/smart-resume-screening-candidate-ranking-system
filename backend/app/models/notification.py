"""
models/notification.py
-----------------------
SQLAlchemy model for the `notifications` table.

Provides an in-app notification feed for all three user roles. Notifications
are created by the system in response to platform events (application status
changes, new job matches, interview invitations, system alerts, etc.).

Architecture decisions:
  - Three Python Enums (NotificationPriority, NotificationType, NotificationCategory)
    stored as VARCHAR — type-safe in Python, migration-friendly in PostgreSQL.
  - Dual read tracking: `is_read` BOOLEAN + `read_at` TIMESTAMP.
      is_read  : Used in indexes for fast unread-count and feed queries.
      read_at  : Records the precise timestamp the notification was opened.
      mark_as_read() sets both atomically — never set one without the other.
  - Polymorphic reference pattern: `reference_id` + `reference_type` allow a
    notification to point to any entity (job, application, resume, user)
    without requiring separate FK columns or join tables.
  - `action_url`: Deep link for frontend navigation when a notification is clicked.
  - SoftDeleteMixin: Users can 'dismiss' notifications (soft delete) without
    permanently removing them. Dismissed notifications are excluded from the feed
    but preserved for audit history.
  - Priority levels: LOW, NORMAL, HIGH, CRITICAL — used for visual emphasis
    (badge colours, sound alerts) and filter capabilities.

Relationships:
    notifications N:1 users
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# NotificationPriority Enum
# ---------------------------------------------------------------------------


class NotificationPriority(str, enum.Enum):
    """
    Notification urgency levels, stored as VARCHAR(20) in PostgreSQL.

    Priority affects:
      - Visual styling (badge colour, icon weight)
      - Sort order within the notification feed
      - Whether a push/email alert is triggered

    Levels:
      LOW      — Background information, no immediate action needed.
      NORMAL   — Standard platform events (status updates, job matches).
      HIGH     — Time-sensitive events (interview scheduled, offer received).
      CRITICAL — Requires immediate attention (account suspended, security alert).
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def label(self) -> str:
        """Return the human-readable label for this priority level."""
        return _PRIORITY_LABELS.get(self, self.value.capitalize())

    @property
    def sort_order(self) -> int:
        """
        Return a sort weight for ordering by priority (lower = higher priority).
        CRITICAL=1, HIGH=2, NORMAL=3, LOW=4.
        """
        return _PRIORITY_SORT_ORDER.get(self, 99)

    @classmethod
    def values(cls) -> tuple:
        """Return all valid priority value strings."""
        return tuple(p.value for p in cls)


_PRIORITY_LABELS: dict = {
    NotificationPriority.LOW: "Low",
    NotificationPriority.NORMAL: "Normal",
    NotificationPriority.HIGH: "High",
    NotificationPriority.CRITICAL: "Critical",
}

_PRIORITY_SORT_ORDER: dict = {
    NotificationPriority.CRITICAL: 1,
    NotificationPriority.HIGH: 2,
    NotificationPriority.NORMAL: 3,
    NotificationPriority.LOW: 4,
}


# ---------------------------------------------------------------------------
# NotificationType Enum
# ---------------------------------------------------------------------------


class NotificationType(str, enum.Enum):
    """
    Visual presentation type for the notification, stored as VARCHAR(20).

    Maps to frontend styling:
      INFO    — Blue / informational icon
      SUCCESS — Green / checkmark icon
      WARNING — Amber / exclamation icon
      ALERT   — Red / urgent icon
    """

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ALERT = "alert"

    @property
    def label(self) -> str:
        """Return the human-readable label for this notification type."""
        return self.value.capitalize()

    @classmethod
    def values(cls) -> tuple:
        """Return all valid type value strings."""
        return tuple(t.value for t in cls)


# ---------------------------------------------------------------------------
# NotificationCategory Enum
# ---------------------------------------------------------------------------


class NotificationCategory(str, enum.Enum):
    """
    Functional category of the notification, stored as VARCHAR(50).

    Used to filter and group notifications in the frontend:
      APPLICATION_UPDATE — Candidate's application moved to a new status.
      JOB_MATCH          — AI found a new job matching the candidate's profile.
      INTERVIEW          — Interview has been scheduled or confirmed.
      OFFER              — An offer has been extended or responded to.
      SYSTEM             — Platform-level announcements or maintenance alerts.
      ACCOUNT            — Account status changes (verified, suspended, etc.).
    """

    APPLICATION_UPDATE = "application_update"
    JOB_MATCH = "job_match"
    INTERVIEW = "interview"
    OFFER = "offer"
    SYSTEM = "system"
    ACCOUNT = "account"

    @property
    def label(self) -> str:
        """Return the human-readable label for this category."""
        return _CATEGORY_LABELS.get(self, self.value.replace("_", " ").title())

    @classmethod
    def values(cls) -> tuple:
        """Return all valid category value strings."""
        return tuple(c.value for c in cls)


_CATEGORY_LABELS: dict = {
    NotificationCategory.APPLICATION_UPDATE: "Application Update",
    NotificationCategory.JOB_MATCH: "Job Match",
    NotificationCategory.INTERVIEW: "Interview",
    NotificationCategory.OFFER: "Offer",
    NotificationCategory.SYSTEM: "System",
    NotificationCategory.ACCOUNT: "Account",
}

# Priority levels considered 'high urgency' — used by is_high_priority property.
_HIGH_URGENCY_PRIORITIES: frozenset = frozenset({
    NotificationPriority.HIGH,
    NotificationPriority.CRITICAL,
})


# ---------------------------------------------------------------------------
# Notification Model
# ---------------------------------------------------------------------------


class Notification(TimestampMixin, SoftDeleteMixin, db.Model):
    """
    An in-app notification delivered to a platform user.

    Notifications are system-generated in response to platform events.
    Users can:
      - View all unread notifications in a priority-ordered feed.
      - Mark individual notifications as read (mark_as_read()).
      - Mark all notifications as read (bulk operation in service layer).
      - Dismiss notifications (soft delete via SoftDeleteMixin).

    Read state is tracked by two complementary fields:
      is_read : BOOLEAN — used in indexes for fast badge count and feed queries.
      read_at : TIMESTAMP — records the exact moment the notification was opened.

    Always use mark_as_read() to update both fields atomically.
    Never set is_read or read_at individually.

    Priority-to-type convention:
      CRITICAL → ALERT
      HIGH     → WARNING or SUCCESS
      NORMAL   → INFO or SUCCESS
      LOW      → INFO
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # Primary feed query: unread notifications for a user, newest first.
        Index("ix_notifications_user_id_is_read", "user_id", "is_read"),
        # Full feed query: all notifications for a user, newest first.
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
        # Priority feed: filter by urgency within a user's unread notifications.
        Index(
            "ix_notifications_user_priority_read",
            "user_id",
            "priority",
            "is_read",
        ),
        # Polymorphic reference lookup: find all notifications for a specific entity.
        Index(
            "ix_notifications_reference",
            "reference_type",
            "reference_id",
        ),
        {
            "comment": (
                "In-app notification feed. "
                "Soft-deletable (user dismissal). "
                "Polymorphic reference allows linking to any platform entity."
            )
        },
    )

    # ------------------------------------------------------------------
    # Expose Enums to consumers without separate imports
    # ------------------------------------------------------------------

    Priority = NotificationPriority
    Type = NotificationType
    Category = NotificationCategory

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique notification identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Recipient
    # ------------------------------------------------------------------

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        doc=(
            "Foreign key to the recipient user. "
            "CASCADE: notifications are removed when the user is hard-deleted."
        ),
    )

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc=(
            "Short notification title shown in the notification list. "
            'Example: "Your application was shortlisted!"'
        ),
    )

    message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc=(
            "Full notification message. "
            "May contain up to a few sentences of context. "
            "Displayed in the notification detail view."
        ),
    )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=NotificationType.INFO.value,
        server_default=NotificationType.INFO.value,
        doc=(
            "Visual presentation type. "
            "Use NotificationType enum: INFO, SUCCESS, WARNING, ALERT. "
            "Determines the frontend icon and colour scheme."
        ),
    )

    priority: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=NotificationPriority.NORMAL.value,
        server_default=NotificationPriority.NORMAL.value,
        doc=(
            "Urgency level. "
            "Use NotificationPriority enum: LOW, NORMAL, HIGH, CRITICAL. "
            "Affects sort order, visual emphasis, and whether push/email alerts fire."
        ),
    )

    category: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc=(
            "Functional category for filtering and grouping. "
            "Use NotificationCategory enum: APPLICATION_UPDATE, JOB_MATCH, "
            "INTERVIEW, OFFER, SYSTEM, ACCOUNT."
        ),
    )

    # ------------------------------------------------------------------
    # Polymorphic Reference (optional link to the triggering entity)
    # ------------------------------------------------------------------

    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=True,
        doc=(
            "UUID of the related entity that triggered this notification. "
            "Interpreted in conjunction with reference_type. "
            "Example: the application UUID when notifying of a status change."
        ),
    )

    reference_type: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc=(
            "Type of the referenced entity. "
            "Examples: 'application', 'job', 'resume', 'user'. "
            "Used with reference_id to construct deep-link navigation."
        ),
    )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    action_url: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        doc=(
            "Frontend deep-link URL to navigate to when the notification is clicked. "
            "Examples: '/applications/uuid', '/jobs/uuid', '/profile'. "
            "Optional — some notifications may not require navigation."
        ),
    )

    # ------------------------------------------------------------------
    # Read State (dual tracking: fast queries + precise timestamp)
    # ------------------------------------------------------------------

    is_read: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc=(
            "True = the user has read this notification. "
            "Indexed for fast unread-count badge queries. "
            "Always set via mark_as_read() — never set directly."
        ),
    )

    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "UTC timestamp when the user opened/read this notification. "
            "NULL if is_read is False. "
            "Provides exact timing for analytics (e.g. median time-to-read). "
            "Always set via mark_as_read() — never set directly."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    user: Mapped["User"] = relationship(
        "User",
        back_populates="notifications",
        lazy="select",
        doc="The user who received this notification.",
    )

    # ------------------------------------------------------------------
    # Read State Management
    # ------------------------------------------------------------------

    def mark_as_read(self) -> None:
        """
        Mark this notification as read, recording the exact read timestamp.

        Sets both `is_read = True` and `read_at = datetime.now(UTC)` atomically.
        Calling this on an already-read notification is idempotent — the
        existing `read_at` timestamp is preserved.

        Call `db.session.commit()` after to persist.
        """
        if not self.is_read:
            self.is_read = True
            self.read_at = datetime.now(timezone.utc)

    def mark_as_unread(self) -> None:
        """
        Revert a notification to unread state.

        Clears both `is_read` and `read_at`. Use sparingly — the primary
        use case is correcting accidental bulk-read-all operations.
        Call `db.session.commit()` after to persist.
        """
        self.is_read = False
        self.read_at = None

    # ------------------------------------------------------------------
    # Computed Properties
    # ------------------------------------------------------------------

    @property
    def priority_label(self) -> str:
        """Return the human-readable label for the notification priority."""
        try:
            return NotificationPriority(self.priority).label
        except ValueError:
            return self.priority.capitalize()

    @property
    def type_label(self) -> str:
        """Return the human-readable label for the notification type."""
        try:
            return NotificationType(self.type).label
        except ValueError:
            return self.type.capitalize()

    @property
    def category_label(self) -> Optional[str]:
        """Return the human-readable label for the notification category."""
        if self.category is None:
            return None
        try:
            return NotificationCategory(self.category).label
        except ValueError:
            return self.category.replace("_", " ").title()

    @property
    def is_high_priority(self) -> bool:
        """Return True if this notification is HIGH or CRITICAL priority."""
        return self.priority in _HIGH_URGENCY_PRIORITIES

    @property
    def is_dismissed(self) -> bool:
        """
        Return True if this notification has been soft-deleted (dismissed).

        Dismissed notifications are hidden from the user's feed but
        preserved in the database for audit purposes.
        """
        return self.is_deleted

    @property
    def priority_sort_order(self) -> int:
        """
        Return the numeric sort weight for this notification's priority.

        Lower value = shown first in priority-ordered feeds.
        CRITICAL=1, HIGH=2, NORMAL=3, LOW=4.
        """
        try:
            return NotificationPriority(self.priority).sort_order
        except ValueError:
            return 99

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<Notification id={self.id} "
            f"user_id={self.user_id} "
            f"priority={self.priority!r} "
            f"is_read={self.is_read} "
            f"title={self.title!r}>"
        )

    def __str__(self) -> str:
        return self.title

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Notification):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Serialize the notification to a JSON-safe dictionary.

        Includes all fields needed for rendering the notification feed,
        detail view, and admin management panels.

        Returns:
            dict: All notification fields safe for API response inclusion.
        """
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "title": self.title,
            "message": self.message,
            "type": self.type,
            "type_label": self.type_label,
            "priority": self.priority,
            "priority_label": self.priority_label,
            "priority_sort_order": self.priority_sort_order,
            "is_high_priority": self.is_high_priority,
            "category": self.category,
            "category_label": self.category_label,
            "reference_id": (
                str(self.reference_id) if self.reference_id else None
            ),
            "reference_type": self.reference_type,
            "action_url": self.action_url,
            "is_read": self.is_read,
            "read_at": (
                self.read_at.isoformat() if self.read_at else None
            ),
            "is_dismissed": self.is_dismissed,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
            "deleted_at": (
                self.deleted_at.isoformat() if self.deleted_at else None
            ),
        }

    def to_feed_dict(self) -> dict:
        """
        Compact serialization for the notification feed list.

        Omits verbose fields (user_id, updated_at, deleted_at) not needed
        for rendering individual notification cards in a feed.
        Includes all fields required for visual rendering and navigation.

        Returns:
            dict: Lightweight fields for the notification feed API response.
        """
        return {
            "id": str(self.id),
            "title": self.title,
            "message": self.message,
            "type": self.type,
            "priority": self.priority,
            "priority_label": self.priority_label,
            "is_high_priority": self.is_high_priority,
            "category": self.category,
            "category_label": self.category_label,
            "reference_id": (
                str(self.reference_id) if self.reference_id else None
            ),
            "reference_type": self.reference_type,
            "action_url": self.action_url,
            "is_read": self.is_read,
            "read_at": (
                self.read_at.isoformat() if self.read_at else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

"""
models/base.py
--------------
Shared SQLAlchemy mixins for the Smart Resume Screening System.

Provides two reusable mixins that are composed into every model:

  TimestampMixin
    Adds `created_at` and `updated_at` columns.
    Both are set automatically — no manual management required.

  SoftDeleteMixin
    Adds `deleted_at` for safe archival instead of hard deletion.
    Records are never physically removed from the database.
    Provides `soft_delete()`, `restore()`, and `is_deleted` helpers.

Applicable tables for SoftDeleteMixin:
  users, jobs, resumes, notifications

NOT applicable to:
  roles, profiles, applications (use status), token_blocklist
  (time-based cleanup), audit_logs, email_logs (immutable audit trails),
  ai_processing_jobs (use cancelled status)

Usage:
  from app.models.base import TimestampMixin, SoftDeleteMixin

  class MyModel(TimestampMixin, SoftDeleteMixin, db.Model):
      __tablename__ = "my_table"
      ...
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


# ---------------------------------------------------------------------------
# TimestampMixin
# ---------------------------------------------------------------------------


class TimestampMixin:
    """
    Adds `created_at` and `updated_at` timestamp columns to any model.

    Both columns are timezone-aware (UTC). `created_at` is immutable after
    insert. `updated_at` is refreshed automatically on every UPDATE via the
    SQLAlchemy `onupdate` hook.

    The `server_default` values ensure the database also sets these correctly
    when rows are inserted outside of the ORM (e.g. raw SQL, seed scripts).
    The Python-side `default` and `onupdate` callables keep the ORM-managed
    values consistent.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        doc="UTC timestamp when the record was first created. Never modified.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        doc="UTC timestamp of the most recent update. Refreshed on every write.",
    )


# ---------------------------------------------------------------------------
# SoftDeleteMixin
# ---------------------------------------------------------------------------


class SoftDeleteMixin:
    """
    Adds soft-delete support via a `deleted_at` timestamp column.

    Instead of issuing DELETE statements, application code calls
    `instance.soft_delete()` which sets `deleted_at` to the current UTC
    time. The record remains in the database and can be fully restored.

    IMPORTANT — Query Responsibility:
      This mixin does NOT automatically filter soft-deleted rows from queries.
      Every query that should exclude deleted records must explicitly add:
        .where(Model.deleted_at.is_(None))
      or use a service-layer helper. This is intentional — some queries
      (admin views, audit reports) legitimately need to see deleted records.

    Tables using this mixin:
      users, jobs, resumes, notifications
    """

    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        index=True,
        doc=(
            "UTC timestamp when this record was soft-deleted. "
            "NULL means the record is active."
        ),
    )

    # ------------------------------------------------------------------
    # Instance Methods
    # ------------------------------------------------------------------

    def soft_delete(self) -> None:
        """
        Mark this record as deleted by setting `deleted_at` to now (UTC).

        This method only updates the Python object. You must call
        `db.session.commit()` to persist the change to the database.

        Calling this on an already-deleted record is idempotent —
        the existing `deleted_at` timestamp is preserved.
        """
        if self.deleted_at is None:
            self.deleted_at = datetime.now(timezone.utc)

    def restore(self) -> None:
        """
        Restore a soft-deleted record by clearing `deleted_at`.

        This method only updates the Python object. You must call
        `db.session.commit()` to persist the change to the database.
        """
        self.deleted_at = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_deleted(self) -> bool:
        """
        Return True if this record has been soft-deleted.

        Usage:
            if user.is_deleted:
                raise NotFoundError("User account no longer exists")
        """
        return self.deleted_at is not None

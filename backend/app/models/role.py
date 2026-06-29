"""
models/role.py
--------------
SQLAlchemy model for the `roles` table.

Roles define the three user types in the system. This table is seeded once
at database initialization and is never modified at runtime by the application.

Design decisions:
  - Stored as a proper lookup table (not a PostgreSQL ENUM or string column
    on users) so that new roles can be added without a schema migration.
  - Class-level constants (CANDIDATE, RECRUITER, ADMIN) should be used
    throughout the codebase instead of raw string literals to prevent typos.
  - `created_at` is tracked for audit completeness. No `updated_at` because
    roles are immutable once seeded.

Relationships:
  roles 1:N users
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List

from sqlalchemy import Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class Role(TimestampMixin, db.Model):
    """
    Represents a user role in the Smart Resume Screening platform.

    Seeded values:
      candidate — Job seekers who upload resumes and apply to job postings.
      recruiter — HR professionals who post jobs and manage candidates.
      admin     — Platform administrators with full management access.

    Prefer the class constants over raw strings:
      Role.CANDIDATE  instead of  "candidate"
      Role.RECRUITER  instead of  "recruiter"
      Role.ADMIN      instead of  "admin"
    """

    __tablename__ = "roles"
    __table_args__ = (
        {
            "comment": (
                "User role definitions. Seeded at database initialization. "
                "Never modified at runtime."
            )
        },
    )

    # ------------------------------------------------------------------
    # Role Name Constants
    # ------------------------------------------------------------------

    CANDIDATE: str = "candidate"
    RECRUITER: str = "recruiter"
    ADMIN: str = "admin"

    ALL_ROLES: tuple = (CANDIDATE, RECRUITER, ADMIN)

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique role identifier (UUID v4).",
    )

    name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        doc=(
            "Role name. One of: 'candidate', 'recruiter', 'admin'. "
            "PostgreSQL automatically creates a unique index for this column."
        ),
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Human-readable description of the role and its platform capabilities.",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    users: Mapped[List["User"]] = relationship(
        "User",
        back_populates="role",
        lazy="select",
        doc="All users assigned to this role.",
    )

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<Role id={self.id} name={self.name!r}>"

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Role):
            return self.id == other.id
        if isinstance(other, str):
            return self.name == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Serialize the Role to a JSON-safe dictionary.

        Returns:
            dict: Role fields safe for inclusion in any API response.
        """
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

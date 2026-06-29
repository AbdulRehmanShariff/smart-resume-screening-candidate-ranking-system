"""
models/recruiter_profile.py
----------------------------
SQLAlchemy model for the `recruiter_profiles` table.

Stores company and recruiter-specific information for users with the
'recruiter' role. This table is a strict one-to-one extension of `users`
and is created during recruiter onboarding.

The recruiter profile serves two purposes:
  1. Provides company branding on job postings (company name, size, industry).
  2. Identifies the recruiter's designation for candidate-facing communication.

Soft delete: NOT applied — profile visibility is governed by the associated
user's state. See candidate_profile.py for the full rationale.

Cascade: ON DELETE CASCADE from users — hard-deleting a user removes
their profile automatically at the database level.

Relationships:
    recruiter_profiles N:1 users
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class RecruiterProfile(TimestampMixin, db.Model):
    """
    Recruiter-specific profile — a one-to-one extension of the `users` table.

    Created during recruiter onboarding. `company_name` is the only required
    field; all other fields are optional to minimise onboarding friction.

    Company size values use class-level constants:
        RecruiterProfile.STARTUP     — 1–10 employees
        RecruiterProfile.SMALL       — 11–50 employees
        RecruiterProfile.MEDIUM      — 51–200 employees
        RecruiterProfile.LARGE       — 201–1,000 employees
        RecruiterProfile.ENTERPRISE  — 1,000+ employees
    """

    __tablename__ = "recruiter_profiles"
    __table_args__ = (
        Index("ix_recruiter_profiles_company_name", "company_name"),
        Index("ix_recruiter_profiles_industry", "industry"),
        Index("ix_recruiter_profiles_company_size", "company_size"),
        {
            "comment": (
                "Recruiter and company profile data. "
                "One-to-one extension of users for the recruiter role."
            )
        },
    )

    # ------------------------------------------------------------------
    # Company Size Constants
    # ------------------------------------------------------------------

    STARTUP: str = "startup"        # 1–10 employees
    SMALL: str = "small"            # 11–50 employees
    MEDIUM: str = "medium"          # 51–200 employees
    LARGE: str = "large"            # 201–1,000 employees
    ENTERPRISE: str = "enterprise"  # 1,000+ employees

    ALL_SIZES: tuple = (STARTUP, SMALL, MEDIUM, LARGE, ENTERPRISE)

    SIZE_LABELS: dict = {
        STARTUP: "Startup (1–10 employees)",
        SMALL: "Small Company (11–50 employees)",
        MEDIUM: "Medium Company (51–200 employees)",
        LARGE: "Large Company (201–1,000 employees)",
        ENTERPRISE: "Enterprise (1,000+ employees)",
    }

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique recruiter profile identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Foreign Key
    # ------------------------------------------------------------------

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        doc=(
            "Foreign key to users. "
            "UNIQUE enforces one profile per recruiter. "
            "CASCADE removes the profile when the user is hard-deleted."
        ),
    )

    # ------------------------------------------------------------------
    # Company Information
    # ------------------------------------------------------------------

    company_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc=(
            "Legal or trading name of the company. "
            "Displayed on all job postings created by this recruiter. "
            "Required — the only mandatory field in this profile."
        ),
    )

    company_website: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        doc=(
            "Company website URL. Displayed on job postings. "
            "Format validation is handled at the API/schema layer."
        ),
    )

    company_size: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc=(
            "Approximate number of employees. "
            "Use class constants: STARTUP, SMALL, MEDIUM, LARGE, ENTERPRISE."
        ),
    )

    industry: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        doc=(
            "Industry or sector the company operates in. "
            'Free-form text. Examples: "Technology", "Finance", "Healthcare", '
            '"E-commerce", "Education".'
        ),
    )

    # ------------------------------------------------------------------
    # Recruiter Information
    # ------------------------------------------------------------------

    designation: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "Recruiter's job title within the company. "
            'Examples: "HR Manager", "Talent Acquisition Lead", "Technical Recruiter".'
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    user: Mapped["User"] = relationship(
        "User",
        back_populates="recruiter_profile",
        lazy="select",
        doc="The recruiter user who owns this profile.",
    )

    # ------------------------------------------------------------------
    # Computed Properties
    # ------------------------------------------------------------------

    @property
    def company_size_label(self) -> Optional[str]:
        """
        Return the human-readable label for the current company size value.

        Returns:
            str  : A display-friendly label (e.g. 'Startup (1–10 employees)').
            None : If company_size has not been set.
        """
        if self.company_size is None:
            return None
        return self.SIZE_LABELS.get(self.company_size, self.company_size)

    @property
    def profile_completeness(self) -> int:
        """
        Calculate a profile completeness percentage (0–100).

        Tracks how many optional fields have been filled in. Used to
        prompt recruiters to complete their profile and to add company
        credibility signals on job postings.

        Weighted fields:
            company_name    — 30 points (required, always present)
            company_website — 20 points
            company_size    — 15 points
            industry        — 20 points
            designation     — 15 points

        Returns:
            int: Completeness score from 0 to 100.
        """
        score = 0
        if self.company_name:   # Always True — required field
            score += 30
        if self.company_website:
            score += 20
        if self.company_size:
            score += 15
        if self.industry:
            score += 20
        if self.designation:
            score += 15
        return min(score, 100)

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<RecruiterProfile id={self.id} "
            f"user_id={self.user_id} "
            f"company={self.company_name!r}>"
        )

    def __str__(self) -> str:
        return self.company_name or f"Recruiter Profile ({self.user_id})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RecruiterProfile):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Serialize the recruiter profile to a JSON-safe dictionary.

        Includes the computed `company_size_label` for frontend display
        without requiring an additional lookup on the client side.

        Returns:
            dict: All profile fields safe for API response inclusion.
        """
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "company_name": self.company_name,
            "company_website": self.company_website,
            "company_size": self.company_size,
            "company_size_label": self.company_size_label,
            "industry": self.industry,
            "designation": self.designation,
            "profile_completeness": self.profile_completeness,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    def to_public_dict(self) -> dict:
        """
        Serialize company-facing information safe for public job posting display.

        Excludes internal fields (user_id, profile_completeness, timestamps)
        that are not relevant to candidates viewing a job posting.

        Returns:
            dict: Company fields for display on job postings and search results.
        """
        return {
            "company_name": self.company_name,
            "company_website": self.company_website,
            "company_size": self.company_size,
            "company_size_label": self.company_size_label,
            "industry": self.industry,
        }

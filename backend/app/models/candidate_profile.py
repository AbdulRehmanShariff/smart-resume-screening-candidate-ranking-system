"""
models/candidate_profile.py
----------------------------
SQLAlchemy model for the `candidate_profiles` table.

Stores manually-maintained, high-level profile information for candidates.
This table is a strict one-to-one extension of `users` and exists only for
users with the 'candidate' role.

Separation of concerns:
  - This table  : Manually entered profile data (headline, location, links)
  - resumes.parsed_data (JSONB) : AI-extracted structured content from uploaded
    resume files (skills, education, work experience, projects, certifications)

This separation prevents data duplication and keeps profile data (manually
curated) distinct from AI-parsed resume data (automatically extracted).

Soft delete: NOT applied. Profile visibility is determined by the associated
user's state. When a user is soft-deleted (user.deleted_at is set), the
profile becomes inaccessible through the user relationship. A redundant
soft-delete on the profile itself adds complexity without benefit.

Cascade: ON DELETE CASCADE from users — hard-deleting a user removes
their profile automatically at the database level.

Relationships:
    candidate_profiles N:1 users
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class CandidateProfile(TimestampMixin, db.Model):
    """
    Candidate-specific profile — a one-to-one extension of the `users` table.

    Created automatically during candidate onboarding. All fields are optional
    to allow progressive profile completion. The `profile_completeness` property
    can be used to prompt candidates to fill in missing information.

    Availability values use class-level constants:
        CandidateProfile.IMMEDIATELY    — ready to start now
        CandidateProfile.TWO_WEEKS      — available after two weeks notice
        CandidateProfile.ONE_MONTH      — available after one month notice
        CandidateProfile.NOT_LOOKING    — passive / not job hunting
    """

    __tablename__ = "candidate_profiles"
    __table_args__ = (
        Index("ix_candidate_profiles_availability", "availability"),
        Index("ix_candidate_profiles_location", "location"),
        CheckConstraint(
            "years_of_experience >= 0",
            name="ck_candidate_profiles_years_of_experience_non_negative",
        ),
        {
            "comment": (
                "Candidate-specific profile data. "
                "One-to-one extension of users for the candidate role. "
                "AI-extracted resume content is stored separately in resumes.parsed_data."
            )
        },
    )

    # ------------------------------------------------------------------
    # Availability Constants
    # ------------------------------------------------------------------

    IMMEDIATELY: str = "immediately"
    TWO_WEEKS: str = "two_weeks"
    ONE_MONTH: str = "one_month"
    NOT_LOOKING: str = "not_looking"

    ALL_AVAILABILITY: tuple = (IMMEDIATELY, TWO_WEEKS, ONE_MONTH, NOT_LOOKING)

    AVAILABILITY_LABELS: dict = {
        IMMEDIATELY: "Immediately Available",
        TWO_WEEKS: "Available in 2 Weeks",
        ONE_MONTH: "Available in 1 Month",
        NOT_LOOKING: "Not Currently Looking",
    }

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique candidate profile identifier (UUID v4).",
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
            "UNIQUE enforces one profile per candidate. "
            "CASCADE removes the profile when the user is hard-deleted."
        ),
    )

    # ------------------------------------------------------------------
    # Profile Fields
    # ------------------------------------------------------------------

    headline: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "Professional headline displayed at the top of the candidate profile. "
            'Example: "Senior Python Developer | 5 Years Experience | Open to Remote".'
        ),
    )

    summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "Manually written professional summary. "
            "Distinct from resumes.ai_summary, which is AI-generated from the resume file. "
            "This field reflects what the candidate chooses to highlight."
        ),
    )

    location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            'City and/or country. Free-form text. '
            'Examples: "San Francisco, CA", "London, UK", "Remote".'
        ),
    )

    # ------------------------------------------------------------------
    # External Links
    # ------------------------------------------------------------------

    linkedin_url: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        doc=(
            "Full LinkedIn profile URL. "
            "Format validation is handled at the API/schema layer, not enforced here."
        ),
    )

    github_url: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        doc="Full GitHub profile URL. Format validation at the API layer.",
    )

    portfolio_url: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        doc="Personal portfolio, blog, or website URL.",
    )

    # ------------------------------------------------------------------
    # Experience & Availability
    # ------------------------------------------------------------------

    years_of_experience: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(4, 1),
        nullable=True,
        doc=(
            "Total years of relevant professional experience. "
            "Non-negative (enforced by CHECK constraint). "
            "One decimal place — e.g. 3.5, 7.0, 0.5."
        ),
    )

    availability: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc=(
            "Current job search availability status. "
            "Use class constants: IMMEDIATELY, TWO_WEEKS, ONE_MONTH, NOT_LOOKING."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    user: Mapped["User"] = relationship(
        "User",
        back_populates="candidate_profile",
        lazy="select",
        doc="The candidate user who owns this profile.",
    )

    # ------------------------------------------------------------------
    # Computed Properties
    # ------------------------------------------------------------------

    @property
    def availability_label(self) -> Optional[str]:
        """
        Return the human-readable label for the current availability value.

        Returns:
            str  : A display-friendly label (e.g. 'Immediately Available').
            None : If availability has not been set.
        """
        if self.availability is None:
            return None
        return self.AVAILABILITY_LABELS.get(self.availability, self.availability)

    @property
    def profile_completeness(self) -> int:
        """
        Calculate a profile completeness percentage (0–100).

        Counts how many of the key optional fields have been filled in.
        This is used to prompt candidates to complete their profiles and
        to weight search results in favour of more complete profiles.

        Weighted fields:
            headline            — 15 points
            summary             — 20 points
            location            — 10 points
            years_of_experience — 10 points
            availability        — 10 points
            linkedin_url        — 10 points
            github_url          — 10 points
            portfolio_url       —  5 points
            (resume uploaded is tracked separately in the resumes table)

        Returns:
            int: Completeness score from 0 to 100.
        """
        score = 0
        if self.headline:
            score += 15
        if self.summary:
            score += 20
        if self.location:
            score += 10
        if self.years_of_experience is not None:
            score += 10
        if self.availability:
            score += 10
        if self.linkedin_url:
            score += 10
        if self.github_url:
            score += 10
        if self.portfolio_url:
            score += 5
        return min(score, 80)  # Max 80 here; resume contributes the remaining 20

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<CandidateProfile id={self.id} "
            f"user_id={self.user_id} "
            f"headline={self.headline!r}>"
        )

    def __str__(self) -> str:
        return self.headline or f"Candidate Profile ({self.user_id})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CandidateProfile):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Serialize the candidate profile to a JSON-safe dictionary.

        Converts `years_of_experience` from Decimal to float for JSON
        compatibility. Includes the computed `availability_label` for
        frontend display and `profile_completeness` for the dashboard.

        Returns:
            dict: All profile fields safe for API response inclusion.
        """
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "headline": self.headline,
            "summary": self.summary,
            "location": self.location,
            "linkedin_url": self.linkedin_url,
            "github_url": self.github_url,
            "portfolio_url": self.portfolio_url,
            "years_of_experience": (
                float(self.years_of_experience)
                if self.years_of_experience is not None
                else None
            ),
            "availability": self.availability,
            "availability_label": self.availability_label,
            "profile_completeness": self.profile_completeness,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

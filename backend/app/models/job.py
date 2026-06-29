"""
models/job.py
-------------
SQLAlchemy model for the `jobs` table.

Stores job postings created by recruiters. This is the central domain entity
that the AI ranking pipeline evaluates resumes against. Candidates apply to
jobs, and recruiters view ranked candidate lists per job.

Enhancements in this version:
  - 5-state status lifecycle: draft → published → paused → closed → archived
  - 6-level experience_level: intern, fresher, junior, mid, senior, lead
  - Separated required from preferred skills: skills_required + nice_to_have_skills
    (both JSONB with GIN indexes for efficient containment queries)
  - Soft delete via SoftDeleteMixin

Design decisions:
  - `skills_required` and `nice_to_have_skills` are separate JSONB arrays rather
    than a single object to allow independent GIN indexing and simpler API design.
  - `views_count` and `applications_count` are denormalized counters updated
    atomically. This avoids COUNT(*) joins on every job listing render.
  - `salary_min` / `salary_max` use DECIMAL(12,2) for financial precision.
  - Status is VARCHAR, not a PostgreSQL ENUM. ENUMs are hard to extend without
    table rewrites; VARCHAR with application-level validation is more maintainable.

Relationships:
    jobs N:1 users (recruiter)
    jobs 1:N applications
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.user import User


class Job(TimestampMixin, SoftDeleteMixin, db.Model):
    """
    A job posting created by a recruiter.

    Lifecycle:
        draft → published → paused → closed → archived

    The `status` field controls candidate visibility and application acceptance.
    Only `published` jobs appear in candidate job searches and accept applications.
    Use class-level status constants to avoid magic strings throughout the codebase.

    Skills are separated into two lists:
        skills_required     — Must-have skills (used for hard filtering and ranking)
        nice_to_have_skills — Preferred but not mandatory (used for ranking bonus points)
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # Query indexes
        Index("ix_jobs_recruiter_id", "recruiter_id"),
        Index("ix_jobs_status_created_at", "status", "created_at"),
        Index("ix_jobs_experience_level", "experience_level"),
        Index("ix_jobs_job_type", "job_type"),
        Index("ix_jobs_is_remote", "is_remote"),
        # GIN indexes for JSONB skill containment queries (@> operator)
        Index(
            "ix_jobs_skills_required_gin",
            "skills_required",
            postgresql_using="gin",
        ),
        Index(
            "ix_jobs_nice_to_have_skills_gin",
            "nice_to_have_skills",
            postgresql_using="gin",
        ),
        # Check constraints
        CheckConstraint(
            "salary_max IS NULL OR salary_min IS NULL OR salary_max >= salary_min",
            name="ck_jobs_salary_max_gte_min",
        ),
        CheckConstraint(
            "views_count >= 0",
            name="ck_jobs_views_count_non_negative",
        ),
        CheckConstraint(
            "applications_count >= 0",
            name="ck_jobs_applications_count_non_negative",
        ),
        {
            "comment": (
                "Job postings created by recruiters. "
                "The central entity for the AI candidate ranking pipeline."
            )
        },
    )

    # ------------------------------------------------------------------
    # Status Constants — 5-State Lifecycle
    # ------------------------------------------------------------------

    DRAFT: str = "draft"
    PUBLISHED: str = "published"
    PAUSED: str = "paused"
    CLOSED: str = "closed"
    ARCHIVED: str = "archived"

    ALL_STATUSES: tuple = (DRAFT, PUBLISHED, PAUSED, CLOSED, ARCHIVED)

    STATUS_LABELS: dict = {
        DRAFT: "Draft",
        PUBLISHED: "Published",
        PAUSED: "Paused",
        CLOSED: "Closed",
        ARCHIVED: "Archived",
    }

    # States where new applications are accepted
    ACCEPTING_APPLICATION_STATUSES: tuple = (PUBLISHED,)

    # States visible to candidates in job search
    VISIBLE_STATUSES: tuple = (PUBLISHED,)

    # ------------------------------------------------------------------
    # Job Type Constants
    # ------------------------------------------------------------------

    FULL_TIME: str = "full_time"
    PART_TIME: str = "part_time"
    CONTRACT: str = "contract"
    INTERNSHIP: str = "internship"
    FREELANCE: str = "freelance"

    ALL_JOB_TYPES: tuple = (FULL_TIME, PART_TIME, CONTRACT, INTERNSHIP, FREELANCE)

    JOB_TYPE_LABELS: dict = {
        FULL_TIME: "Full Time",
        PART_TIME: "Part Time",
        CONTRACT: "Contract",
        INTERNSHIP: "Internship",
        FREELANCE: "Freelance",
    }

    # ------------------------------------------------------------------
    # Experience Level Constants — 6 Levels
    # ------------------------------------------------------------------

    INTERN: str = "intern"
    FRESHER: str = "fresher"
    JUNIOR: str = "junior"
    MID: str = "mid"
    SENIOR: str = "senior"
    LEAD: str = "lead"

    ALL_EXPERIENCE_LEVELS: tuple = (INTERN, FRESHER, JUNIOR, MID, SENIOR, LEAD)

    EXPERIENCE_LEVEL_LABELS: dict = {
        INTERN: "Intern",
        FRESHER: "Fresher / Entry Level (0–1 year)",
        JUNIOR: "Junior (1–3 years)",
        MID: "Mid-Level (3–5 years)",
        SENIOR: "Senior (5–8 years)",
        LEAD: "Lead / Principal (8+ years)",
    }

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique job identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Foreign Key
    # ------------------------------------------------------------------

    recruiter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        doc=(
            "Foreign key to the recruiter who created this posting. "
            "CASCADE: jobs are removed if the recruiter account is hard-deleted."
        ),
    )

    # ------------------------------------------------------------------
    # Core Job Details
    # ------------------------------------------------------------------

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc='Job title shown to candidates. Example: "Senior Backend Engineer".',
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc=(
            "Full job description used for AI semantic matching. "
            "This is the primary text the AI pipeline analyses against resumes."
        ),
    )

    requirements: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Technical, educational, or certification requirements for the role.",
    )

    responsibilities: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Day-to-day duties and responsibilities of the role.",
    )

    # ------------------------------------------------------------------
    # Location & Work Mode
    # ------------------------------------------------------------------

    location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc='Physical office location. Example: "New York, NY" or "London, UK".',
    )

    is_remote: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc="True = remote work is fully or partially supported.",
    )

    # ------------------------------------------------------------------
    # Job Classification
    # ------------------------------------------------------------------

    job_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=FULL_TIME,
        doc=(
            "Employment type. "
            "Use constants: FULL_TIME, PART_TIME, CONTRACT, INTERNSHIP, FREELANCE."
        ),
    )

    experience_level: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc=(
            "Required experience level. "
            "Use constants: INTERN, FRESHER, JUNIOR, MID, SENIOR, LEAD."
        ),
    )

    # ------------------------------------------------------------------
    # Compensation
    # ------------------------------------------------------------------

    salary_min: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2),
        nullable=True,
        doc=(
            "Minimum salary in salary_currency. NULL = not disclosed. "
            "Must be <= salary_max if both are provided (enforced by CHECK constraint)."
        ),
    )

    salary_max: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2),
        nullable=True,
        doc=(
            "Maximum salary in salary_currency. NULL = not disclosed. "
            "Must be >= salary_min if both are provided (enforced by CHECK constraint)."
        ),
    )

    salary_currency: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="USD",
        server_default="USD",
        doc=(
            "ISO 4217 currency code for salary values. "
            'Examples: "USD", "EUR", "GBP", "INR", "CAD".'
        ),
    )

    # ------------------------------------------------------------------
    # Skills — Required vs. Preferred (separated for independent GIN indexing)
    # ------------------------------------------------------------------

    skills_required: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        doc=(
            "Must-have skills for this role. "
            "Used for hard skill matching and AI ranking score calculation. "
            'Example: ["Python", "Flask", "PostgreSQL", "Docker"]. '
            "GIN index enables efficient @> containment queries."
        ),
    )

    nice_to_have_skills: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        doc=(
            "Preferred but not mandatory skills. "
            "Used for ranking bonus points in AI scoring — "
            "candidates with these skills rank higher but are not excluded without them. "
            'Example: ["Kubernetes", "Terraform", "GraphQL"]. '
            "GIN index enables efficient @> containment queries."
        ),
    )

    # ------------------------------------------------------------------
    # Lifecycle & Status
    # ------------------------------------------------------------------

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DRAFT,
        server_default="draft",
        doc=(
            "Current lifecycle status. "
            "Use constants: DRAFT, PUBLISHED, PAUSED, CLOSED, ARCHIVED. "
            "Only PUBLISHED jobs are visible to candidates and accept applications."
        ),
    )

    application_deadline: Mapped[Optional[datetime]] = mapped_column(
        db.DateTime(timezone=True),
        nullable=True,
        doc=(
            "UTC deadline after which new applications are no longer accepted. "
            "NULL = no deadline (applications accepted indefinitely while published)."
        ),
    )

    jd_file_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        doc=(
            "Storage path to an uploaded job description file (PDF or TXT). "
            "Optional — job description can be entered as text in the `description` field."
        ),
    )

    # ------------------------------------------------------------------
    # Denormalized Counters (updated atomically to avoid COUNT(*) joins)
    # ------------------------------------------------------------------

    views_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc=(
            "Number of times this job listing has been viewed by candidates. "
            "Incremented atomically on each view. Avoids expensive COUNT joins."
        ),
    )

    applications_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc=(
            "Number of applications received for this job. "
            "Incremented when a candidate submits an application. "
            "Decremented when a candidate withdraws."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    recruiter: Mapped["User"] = relationship(
        "User",
        back_populates="jobs",
        lazy="select",
        doc="The recruiter who created this job posting.",
    )

    applications: Mapped[List["Application"]] = relationship(
        "Application",
        back_populates="job",
        lazy="select",
        doc="All applications submitted for this job.",
    )

    # ------------------------------------------------------------------
    # Computed Properties
    # ------------------------------------------------------------------

    @property
    def status_label(self) -> str:
        """Return the human-readable label for the current status."""
        return self.STATUS_LABELS.get(self.status, self.status)

    @property
    def job_type_label(self) -> Optional[str]:
        """Return the human-readable label for the job type."""
        return self.JOB_TYPE_LABELS.get(self.job_type, self.job_type)

    @property
    def experience_level_label(self) -> Optional[str]:
        """Return the human-readable label for the experience level."""
        if self.experience_level is None:
            return None
        return self.EXPERIENCE_LEVEL_LABELS.get(
            self.experience_level, self.experience_level
        )

    @property
    def is_published(self) -> bool:
        """Return True if the job is live and publicly visible."""
        return self.status == self.PUBLISHED and not self.is_deleted

    @property
    def is_deadline_passed(self) -> bool:
        """
        Return True if the application deadline has passed.

        Returns False if no deadline is set (no-deadline jobs accept
        applications indefinitely while published).
        """
        if self.application_deadline is None:
            return False
        return datetime.now(timezone.utc) > self.application_deadline

    @property
    def is_accepting_applications(self) -> bool:
        """
        Return True if this job currently accepts new applications.

        Conditions for accepting applications:
          1. Status is PUBLISHED.
          2. The job has not been soft-deleted.
          3. The application deadline has not passed (or there is no deadline).

        This is the authoritative check used by the application service
        before allowing a candidate to submit an application.
        """
        return (
            self.status == self.PUBLISHED
            and not self.is_deleted
            and not self.is_deadline_passed
        )

    @property
    def salary_range_display(self) -> Optional[str]:
        """
        Return a formatted salary range string for display.

        Returns:
            str  : Formatted range, e.g. "USD 60,000 – 80,000".
            None : If neither salary_min nor salary_max is set.
        """
        if self.salary_min is None and self.salary_max is None:
            return None

        currency = self.salary_currency or "USD"

        if self.salary_min is not None and self.salary_max is not None:
            min_val = f"{float(self.salary_min):,.0f}"
            max_val = f"{float(self.salary_max):,.0f}"
            return f"{currency} {min_val} – {max_val}"

        if self.salary_min is not None:
            return f"{currency} {float(self.salary_min):,.0f}+"

        return f"Up to {currency} {float(self.salary_max):,.0f}"

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<Job id={self.id} "
            f"title={self.title!r} "
            f"status={self.status!r}>"
        )

    def __str__(self) -> str:
        return self.title

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Job):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Full serialization for recruiter-facing views.

        Includes all fields: internal counters, status metadata, soft-delete
        state, and recruiter-only fields (jd_file_path).

        Returns:
            dict: All job fields for recruiter dashboard and management views.
        """
        return {
            "id": str(self.id),
            "recruiter_id": str(self.recruiter_id),
            "title": self.title,
            "description": self.description,
            "requirements": self.requirements,
            "responsibilities": self.responsibilities,
            "location": self.location,
            "is_remote": self.is_remote,
            "job_type": self.job_type,
            "job_type_label": self.job_type_label,
            "experience_level": self.experience_level,
            "experience_level_label": self.experience_level_label,
            "salary_min": (
                float(self.salary_min) if self.salary_min is not None else None
            ),
            "salary_max": (
                float(self.salary_max) if self.salary_max is not None else None
            ),
            "salary_currency": self.salary_currency,
            "salary_range_display": self.salary_range_display,
            "skills_required": self.skills_required or [],
            "nice_to_have_skills": self.nice_to_have_skills or [],
            "status": self.status,
            "status_label": self.status_label,
            "application_deadline": (
                self.application_deadline.isoformat()
                if self.application_deadline
                else None
            ),
            "is_deadline_passed": self.is_deadline_passed,
            "jd_file_path": self.jd_file_path,
            "views_count": self.views_count,
            "applications_count": self.applications_count,
            "is_accepting_applications": self.is_accepting_applications,
            "is_deleted": self.is_deleted,
            "deleted_at": (
                self.deleted_at.isoformat() if self.deleted_at else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    def to_public_dict(self) -> dict:
        """
        Serialization for candidate-facing job detail pages.

        Excludes internal recruiter fields: recruiter_id, jd_file_path,
        soft-delete metadata. Includes all fields candidates need to
        evaluate a job and decide whether to apply.

        Returns:
            dict: Public job fields for candidate-facing API responses.
        """
        return {
            "id": str(self.id),
            "title": self.title,
            "description": self.description,
            "requirements": self.requirements,
            "responsibilities": self.responsibilities,
            "location": self.location,
            "is_remote": self.is_remote,
            "job_type": self.job_type,
            "job_type_label": self.job_type_label,
            "experience_level": self.experience_level,
            "experience_level_label": self.experience_level_label,
            "salary_min": (
                float(self.salary_min) if self.salary_min is not None else None
            ),
            "salary_max": (
                float(self.salary_max) if self.salary_max is not None else None
            ),
            "salary_currency": self.salary_currency,
            "salary_range_display": self.salary_range_display,
            "skills_required": self.skills_required or [],
            "nice_to_have_skills": self.nice_to_have_skills or [],
            "application_deadline": (
                self.application_deadline.isoformat()
                if self.application_deadline
                else None
            ),
            "is_deadline_passed": self.is_deadline_passed,
            "is_accepting_applications": self.is_accepting_applications,
            "views_count": self.views_count,
            "applications_count": self.applications_count,
            "posted_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

    def to_list_dict(self) -> dict:
        """
        Compact serialization for job listing cards and search results.

        Contains only the fields needed to render a job card in a list view.
        Omits long text fields (description, requirements, responsibilities)
        to keep API responses fast when returning many jobs at once.

        Returns:
            dict: Compact job fields for paginated job listing responses.
        """
        return {
            "id": str(self.id),
            "title": self.title,
            "location": self.location,
            "is_remote": self.is_remote,
            "job_type": self.job_type,
            "job_type_label": self.job_type_label,
            "experience_level": self.experience_level,
            "experience_level_label": self.experience_level_label,
            "salary_range_display": self.salary_range_display,
            "salary_currency": self.salary_currency,
            "skills_required": self.skills_required or [],
            "status": self.status,
            "status_label": self.status_label,
            "is_accepting_applications": self.is_accepting_applications,
            "application_deadline": (
                self.application_deadline.isoformat()
                if self.application_deadline
                else None
            ),
            "applications_count": self.applications_count,
            "posted_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

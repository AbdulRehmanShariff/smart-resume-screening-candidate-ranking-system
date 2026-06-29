"""
models/application.py
----------------------
SQLAlchemy model for the `applications` table.

The application is the central transactional entity connecting a candidate,
a job posting, and a resume. It stores the entire application lifecycle,
AI scoring results, explainable ranking data, and recruiter interactions.

Architecture:
  - Python Enum (ApplicationStatus) for type-safe status management, stored
    as VARCHAR in PostgreSQL for migration flexibility.
  - Four separate JSONB fields for AI output, each with a distinct purpose:
      score_breakdown    : Numeric scores per evaluation category.
      ranking_reason     : Human-readable explanations (explainable AI).
      skill_gap          : Structured required/preferred skill gap analysis.
      interview_questions: AI-generated interview questions with rationale.
  - ai_metadata         : Model traceability — records which AI model version
    produced each piece of output.
  - last_ai_processed_at: Single timestamp tracking the most recent AI run,
    used for cache invalidation and staleness checks.

No SoftDeleteMixin: applications use the WITHDRAWN status for candidate-side
cancellation and REJECTED for recruiter-side dismissal. Physical deletion is
never required — the full history must be preserved for audit and analytics.

UNIQUE constraint on (job_id, candidate_id) prevents duplicate applications.

Relationships:
    applications N:1 jobs
    applications N:1 users (as candidate)
    applications N:1 resumes
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.resume import Resume
    from app.models.user import User


# ---------------------------------------------------------------------------
# ApplicationStatus Enum
# ---------------------------------------------------------------------------


class ApplicationStatus(str, enum.Enum):
    """
    13-state application lifecycle, stored as VARCHAR(30) in PostgreSQL.

    Inherits from `str` so that:
        application.status == "applied"               # True
        application.status == ApplicationStatus.APPLIED  # True
        str(ApplicationStatus.APPLIED)                # "applied"

    Using VARCHAR instead of a native PostgreSQL ENUM type allows adding
    new statuses without requiring a schema migration (ALTER TYPE).

    Lifecycle summary:
        applied → screening → (assessment cycle) → shortlisted
                → (interview cycle) → offer_sent → offer_accepted / offer_declined
        Any active state → rejected (recruiter decision)
        Any active state → withdrawn (candidate decision)
    """

    # -- Initial state ---------------------------------------------------------
    APPLIED = "applied"

    # -- Screening -------------------------------------------------------------
    SCREENING = "screening"

    # -- Assessment cycle ------------------------------------------------------
    ASSESSMENT_PENDING = "assessment_pending"
    ASSESSMENT_COMPLETED = "assessment_completed"

    # -- Shortlist -------------------------------------------------------------
    SHORTLISTED = "shortlisted"

    # -- Interview stages ------------------------------------------------------
    TECHNICAL_INTERVIEW = "technical_interview"
    HR_INTERVIEW = "hr_interview"
    FINAL_INTERVIEW = "final_interview"

    # -- Offer stages ----------------------------------------------------------
    OFFER_SENT = "offer_sent"
    OFFER_ACCEPTED = "offer_accepted"   # Terminal — successful hire
    OFFER_DECLINED = "offer_declined"   # Terminal — candidate refused

    # -- Closed states ---------------------------------------------------------
    REJECTED = "rejected"               # Terminal — recruiter dismissed
    WITHDRAWN = "withdrawn"             # Terminal — candidate withdrew

    # -- Label map -------------------------------------------------------------
    @property
    def label(self) -> str:
        """Return a human-readable display label for this status."""
        return _APPLICATION_STATUS_LABELS.get(self, self.value)

    @classmethod
    def values(cls) -> tuple:
        """Return all valid status value strings."""
        return tuple(s.value for s in cls)


# Human-readable labels — defined after the Enum to avoid forward-reference issues.
_APPLICATION_STATUS_LABELS: dict = {
    ApplicationStatus.APPLIED: "Applied",
    ApplicationStatus.SCREENING: "Under Review",
    ApplicationStatus.ASSESSMENT_PENDING: "Assessment Pending",
    ApplicationStatus.ASSESSMENT_COMPLETED: "Assessment Completed",
    ApplicationStatus.SHORTLISTED: "Shortlisted",
    ApplicationStatus.TECHNICAL_INTERVIEW: "Technical Interview",
    ApplicationStatus.HR_INTERVIEW: "HR Interview",
    ApplicationStatus.FINAL_INTERVIEW: "Final Interview",
    ApplicationStatus.OFFER_SENT: "Offer Extended",
    ApplicationStatus.OFFER_ACCEPTED: "Offer Accepted",
    ApplicationStatus.OFFER_DECLINED: "Offer Declined",
    ApplicationStatus.REJECTED: "Not Proceeding",
    ApplicationStatus.WITHDRAWN: "Withdrawn by Candidate",
}

# Frozen sets for O(1) membership checks in properties.
_TERMINAL_STATUSES: frozenset = frozenset({
    ApplicationStatus.OFFER_ACCEPTED,
    ApplicationStatus.OFFER_DECLINED,
    ApplicationStatus.REJECTED,
    ApplicationStatus.WITHDRAWN,
})

_INTERVIEW_STATUSES: frozenset = frozenset({
    ApplicationStatus.TECHNICAL_INTERVIEW,
    ApplicationStatus.HR_INTERVIEW,
    ApplicationStatus.FINAL_INTERVIEW,
})

_OFFER_STATUSES: frozenset = frozenset({
    ApplicationStatus.OFFER_SENT,
    ApplicationStatus.OFFER_ACCEPTED,
    ApplicationStatus.OFFER_DECLINED,
})

_ACTIVE_STATUSES: frozenset = frozenset(
    s for s in ApplicationStatus if s not in _TERMINAL_STATUSES
)


# ---------------------------------------------------------------------------
# Application Model
# ---------------------------------------------------------------------------


class Application(TimestampMixin, db.Model):
    """
    A candidate's application to a specific job using a specific resume.

    Tracks the full lifecycle from submission through hiring decision,
    including all AI-generated scoring, ranking, and interview content.

    Status transitions are managed by the application service layer.
    Use `update_status()` to change status — it always updates
    `status_updated_at` atomically.

    AI output fields are progressively populated as the AI pipeline runs:
      1. match_score + score_breakdown + ranking_reason — after ranking
      2. skill_gap                                      — after gap analysis
      3. interview_questions                            — after question gen
      4. ai_metadata + last_ai_processed_at            — after any AI step
    """

    __tablename__ = "applications"
    __table_args__ = (
        # Core business constraint: one application per candidate per job.
        UniqueConstraint(
            "job_id",
            "candidate_id",
            name="uq_applications_job_candidate",
        ),
        # Ranking query: retrieve ordered candidate list for a job.
        Index("ix_applications_job_score", "job_id", "match_score"),
        # Candidate history: all applications by a candidate, newest first.
        Index("ix_applications_candidate_created", "candidate_id", "created_at"),
        # Status filtering within a job's applicant pool.
        Index("ix_applications_job_status", "job_id", "status"),
        # Resume FK index.
        Index("ix_applications_resume_id", "resume_id"),
        # Status change monitoring.
        Index("ix_applications_status_updated_at", "status_updated_at"),
        # AI processing staleness checks.
        Index("ix_applications_last_ai_processed_at", "last_ai_processed_at"),
        # Check constraints.
        CheckConstraint(
            "match_score IS NULL OR (match_score >= 0 AND match_score <= 100)",
            name="ck_applications_match_score_range",
        ),
        CheckConstraint(
            "rank IS NULL OR rank >= 1",
            name="ck_applications_rank_positive",
        ),
        {
            "comment": (
                "Candidate applications. "
                "Connects a candidate, a job, and a resume. "
                "Stores the complete AI scoring and application lifecycle."
            )
        },
    )

    # ------------------------------------------------------------------
    # Expose Enum to consumers without a separate import
    # ------------------------------------------------------------------

    Status = ApplicationStatus

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique application identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Foreign Keys
    # ------------------------------------------------------------------

    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        doc=(
            "Foreign key to the job being applied for. "
            "CASCADE: applications are removed when the job is hard-deleted."
        ),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        doc=(
            "Foreign key to the candidate who submitted this application. "
            "CASCADE: applications are removed when the user is hard-deleted."
        ),
    )

    resume_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("resumes.id", ondelete="RESTRICT"),
        nullable=False,
        doc=(
            "Foreign key to the resume submitted with this application. "
            "RESTRICT: prevents resume deletion while an application references it."
        ),
    )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=ApplicationStatus.APPLIED.value,
        server_default=ApplicationStatus.APPLIED.value,
        doc=(
            "Current application status. "
            "Use ApplicationStatus enum values. "
            "Stored as VARCHAR for PostgreSQL migration flexibility. "
            "Always update via update_status() to keep status_updated_at in sync."
        ),
    )

    status_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "UTC timestamp when the status was last changed. "
            "NULL only for the initial state (set immediately on first status change). "
            "Always updated atomically with status via update_status()."
        ),
    )

    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        doc=(
            "UTC timestamp when the candidate submitted this application. "
            "Semantically distinct from created_at (TimestampMixin) — "
            "applied_at explicitly marks the submission event."
        ),
    )

    # ------------------------------------------------------------------
    # AI Scoring — Numeric
    # ------------------------------------------------------------------

    match_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(5, 2),
        nullable=True,
        doc=(
            "AI-computed overall match score from 0.00 to 100.00. "
            "Weighted aggregate of all score_breakdown categories. "
            "Used as the primary ranking signal. "
            "NULL until the AI ranking pipeline completes for this application."
        ),
    )

    rank: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc=(
            "Candidate's rank within the applicant pool for this job. "
            "1 = highest ranked. Updated after each bulk re-ranking run. "
            "NULL until ranking is complete."
        ),
    )

    # ------------------------------------------------------------------
    # AI Output — JSONB Fields
    # ------------------------------------------------------------------

    score_breakdown: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Numeric per-category AI scores that compose the overall match_score. "
            "Schema: { "
            "  'overall': 87.5, "
            "  'skills_match': 90.0, "
            "  'experience_match': 85.0, "
            "  'education_match': 75.0, "
            "  'keyword_overlap': 92.0, "
            "  'semantic_similarity': 88.0 "
            "}. "
            "Used to render score breakdowns in the recruiter dashboard."
        ),
    )

    ranking_reason: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Explainable AI narrative justifying the match_score and rank. "
            "Distinct from score_breakdown (which is numeric) — "
            "ranking_reason provides human-readable context. "
            "Schema: { "
            "  'summary': 'Strong candidate matching 8/10 required skills...', "
            "  'strengths': ['5+ years Python experience', 'Docker/K8s coverage'], "
            "  'areas_for_development': ['No Kubernetes mentioned', 'Leadership not documented'], "
            "  'recommendation': 'Recommended for technical interview' "
            "}. "
            "Displayed to recruiters as the AI's reasoning, not just a score."
        ),
    )

    skill_gap: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Structured skill gap analysis comparing the resume against job requirements. "
            "Schema: { "
            "  'matched_required': ['Python', 'Flask', 'PostgreSQL'], "
            "  'missing_required': ['Docker'], "
            "  'matched_preferred': ['Redis'], "
            "  'missing_preferred': ['Kubernetes', 'Terraform'] "
            "}. "
            "Used to highlight gaps to both recruiters and candidates."
        ),
    )

    interview_questions: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "AI-generated interview questions tailored to this candidate and job. "
            "Schema: { "
            "  'technical': [{ 'question': '...', 'rationale': '...', 'difficulty': 'medium' }], "
            "  'behavioral': [{ 'question': '...', 'rationale': '...', 'difficulty': 'easy' }], "
            "  'culture_fit': [{ 'question': '...', 'rationale': '...' }] "
            "}. "
            "Shown exclusively to recruiters — never to the candidate."
        ),
    )

    ai_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "AI model traceability, keyed by operation name. "
            "Each operation records: model_name, model_version, processed_at, "
            "processing_time_ms. "
            "Operations: 'ranking', 'skill_gap_analysis', 'interview_questions'. "
            "Example: { 'ranking': { 'model_name': 'gemini-1.5-pro', "
            "'model_version': '001', 'processed_at': '...', "
            "'processing_time_ms': 3120 } }."
        ),
    )

    # ------------------------------------------------------------------
    # AI Processing Timestamp
    # ------------------------------------------------------------------

    last_ai_processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "UTC timestamp of the most recent AI processing run for this application. "
            "Updated after any AI operation completes (ranking, skill gap, "
            "interview generation). "
            "Used to: (1) display 'Last analysed X minutes ago' in the recruiter UI, "
            "(2) detect stale scores when the job or resume is updated, "
            "(3) trigger re-analysis queuing when the timestamp is too old."
        ),
    )

    # ------------------------------------------------------------------
    # Recruiter Notes (private)
    # ------------------------------------------------------------------

    recruiter_notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "Private notes from the recruiter. "
            "Never exposed to the candidate in any API response. "
            "Stored as free-form text — no structure enforced."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    job: Mapped["Job"] = relationship(
        "Job",
        back_populates="applications",
        lazy="select",
        doc="The job posting this application was submitted for.",
    )

    candidate: Mapped["User"] = relationship(
        "User",
        back_populates="applications",
        foreign_keys=[candidate_id],
        lazy="select",
        doc="The candidate who submitted this application.",
    )

    resume: Mapped["Resume"] = relationship(
        "Resume",
        back_populates="applications",
        lazy="select",
        doc="The resume submitted with this application.",
    )

    # ------------------------------------------------------------------
    # Status Management
    # ------------------------------------------------------------------

    def update_status(self, new_status: ApplicationStatus | str) -> None:
        """
        Update the application status and record the change timestamp.

        Always use this method instead of setting `status` directly —
        it atomically updates `status_updated_at` to maintain audit integrity.
        Call `db.session.commit()` after to persist.

        Args:
            new_status: The new ApplicationStatus enum value or a valid
                        status string (e.g. 'shortlisted').
        """
        self.status = (
            new_status.value
            if isinstance(new_status, ApplicationStatus)
            else new_status
        )
        self.status_updated_at = datetime.now(timezone.utc)

    def record_ai_processing(self) -> None:
        """
        Update `last_ai_processed_at` to the current UTC time.

        Call this method at the end of any AI operation that modifies
        score_breakdown, ranking_reason, skill_gap, interview_questions,
        or ai_metadata. Call `db.session.commit()` after to persist.
        """
        self.last_ai_processed_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Computed Properties — Status
    # ------------------------------------------------------------------

    @property
    def status_label(self) -> str:
        """Return the human-readable label for the current status."""
        try:
            return ApplicationStatus(self.status).label
        except ValueError:
            return self.status

    @property
    def is_terminal(self) -> bool:
        """
        Return True if the application has reached a terminal state.

        Terminal states: OFFER_ACCEPTED, OFFER_DECLINED, REJECTED, WITHDRAWN.
        No further status transitions are allowed from a terminal state.
        """
        return self.status in _TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """
        Return True if the application is still in progress.

        An application is active when it has not reached any terminal state.
        """
        return self.status in _ACTIVE_STATUSES

    @property
    def is_interview_stage(self) -> bool:
        """Return True if the application is in any interview stage."""
        return self.status in _INTERVIEW_STATUSES

    @property
    def is_offer_stage(self) -> bool:
        """Return True if an offer has been extended or resolved."""
        return self.status in _OFFER_STATUSES

    @property
    def is_successful_hire(self) -> bool:
        """Return True if the candidate accepted the offer."""
        return self.status == ApplicationStatus.OFFER_ACCEPTED.value

    # ------------------------------------------------------------------
    # Computed Properties — AI State
    # ------------------------------------------------------------------

    @property
    def has_ai_score(self) -> bool:
        """Return True if AI ranking has been completed for this application."""
        return self.match_score is not None

    @property
    def has_ai_explanation(self) -> bool:
        """Return True if explainable AI ranking_reason is available."""
        return self.ranking_reason is not None

    @property
    def has_skill_gap(self) -> bool:
        """Return True if skill gap analysis has been completed."""
        return self.skill_gap is not None

    @property
    def has_interview_questions(self) -> bool:
        """Return True if AI interview questions have been generated."""
        return self.interview_questions is not None

    @property
    def is_fully_analysed(self) -> bool:
        """
        Return True if all AI analysis steps are complete for this application.

        Requires: match_score, ranking_reason, skill_gap, and interview_questions
        to all be populated.
        """
        return (
            self.has_ai_score
            and self.has_ai_explanation
            and self.has_skill_gap
            and self.has_interview_questions
        )

    @property
    def days_since_applied(self) -> Optional[int]:
        """
        Return the number of whole days elapsed since the application was submitted.

        Returns:
            int  : Days elapsed since applied_at.
            None : If applied_at is not set.
        """
        if self.applied_at is None:
            return None
        delta = datetime.now(timezone.utc) - self.applied_at
        return delta.days

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<Application id={self.id} "
            f"job_id={self.job_id} "
            f"candidate_id={self.candidate_id} "
            f"status={self.status!r} "
            f"score={self.match_score}>"
        )

    def __str__(self) -> str:
        return f"Application({self.candidate_id} → {self.job_id}, {self.status})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Application):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Full serialization including all AI output and recruiter-private fields.

        Use for internal admin views or when the full data set is required.
        Includes recruiter_notes — do NOT return this to candidates.

        Returns:
            dict: All application fields.
        """
        return {
            "id": str(self.id),
            "job_id": str(self.job_id),
            "candidate_id": str(self.candidate_id),
            "resume_id": str(self.resume_id),
            "status": self.status,
            "status_label": self.status_label,
            "is_terminal": self.is_terminal,
            "is_active": self.is_active,
            "is_interview_stage": self.is_interview_stage,
            "is_offer_stage": self.is_offer_stage,
            "match_score": (
                float(self.match_score) if self.match_score is not None else None
            ),
            "rank": self.rank,
            "score_breakdown": self.score_breakdown,
            "ranking_reason": self.ranking_reason,
            "skill_gap": self.skill_gap,
            "interview_questions": self.interview_questions,
            "ai_metadata": self.ai_metadata,
            "has_ai_score": self.has_ai_score,
            "has_ai_explanation": self.has_ai_explanation,
            "has_skill_gap": self.has_skill_gap,
            "has_interview_questions": self.has_interview_questions,
            "is_fully_analysed": self.is_fully_analysed,
            "last_ai_processed_at": (
                self.last_ai_processed_at.isoformat()
                if self.last_ai_processed_at
                else None
            ),
            "recruiter_notes": self.recruiter_notes,
            "days_since_applied": self.days_since_applied,
            "applied_at": (
                self.applied_at.isoformat() if self.applied_at else None
            ),
            "status_updated_at": (
                self.status_updated_at.isoformat()
                if self.status_updated_at
                else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    def to_recruiter_dict(self) -> dict:
        """
        Serialization for the recruiter's applicant review panel.

        Includes: AI scores, ranking, explainable AI, skill gap, interview
        questions, and recruiter notes. All recruiter-relevant intelligence.

        Returns:
            dict: Recruiter-facing application data.
        """
        return {
            "id": str(self.id),
            "candidate_id": str(self.candidate_id),
            "resume_id": str(self.resume_id),
            "status": self.status,
            "status_label": self.status_label,
            "is_terminal": self.is_terminal,
            "match_score": (
                float(self.match_score) if self.match_score is not None else None
            ),
            "rank": self.rank,
            "score_breakdown": self.score_breakdown,
            "ranking_reason": self.ranking_reason,
            "skill_gap": self.skill_gap,
            "interview_questions": self.interview_questions,
            "has_ai_score": self.has_ai_score,
            "has_ai_explanation": self.has_ai_explanation,
            "is_fully_analysed": self.is_fully_analysed,
            "last_ai_processed_at": (
                self.last_ai_processed_at.isoformat()
                if self.last_ai_processed_at
                else None
            ),
            "recruiter_notes": self.recruiter_notes,
            "days_since_applied": self.days_since_applied,
            "applied_at": (
                self.applied_at.isoformat() if self.applied_at else None
            ),
            "status_updated_at": (
                self.status_updated_at.isoformat()
                if self.status_updated_at
                else None
            ),
        }

    def to_candidate_dict(self) -> dict:
        """
        Serialization safe for candidate-facing API responses.

        Deliberately excludes recruiter-private fields:
          - recruiter_notes    (confidential)
          - interview_questions (shown only to recruiters)
          - ranking_reason     (internal AI reasoning)
          - score_breakdown    (internal scores)
          - ai_metadata        (internal traceability)
          - rank               (rank among other candidates)

        Returns:
            dict: Fields the candidate is permitted to see about their application.
        """
        return {
            "id": str(self.id),
            "job_id": str(self.job_id),
            "resume_id": str(self.resume_id),
            "status": self.status,
            "status_label": self.status_label,
            "is_terminal": self.is_terminal,
            "skill_gap": self.skill_gap,   # Candidates can see their own skill gaps
            "days_since_applied": self.days_since_applied,
            "applied_at": (
                self.applied_at.isoformat() if self.applied_at else None
            ),
            "status_updated_at": (
                self.status_updated_at.isoformat()
                if self.status_updated_at
                else None
            ),
        }

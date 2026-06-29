"""
models/ai_processing_job.py
----------------------------
SQLAlchemy model for the `ai_processing_jobs` table.

Implements a persistent job queue for all asynchronous AI operations.
When the AI pipeline cannot process a resume synchronously (e.g. during
bulk upload or when the AI provider is rate-limited), a job is created
here and processed by a background worker.

Architecture decisions:
  - NO TimestampMixin: uses semantically precise timestamps
    (`queued_at`, `started_at`, `completed_at`) instead of generic
    `created_at` / `updated_at`.
  - NO SoftDeleteMixin: use the CANCELLED status for cancellation.
  - Priority is an integer (1–10) rather than an enum: allows fine-grained
    ordering within priority bands. Class constants provide named bands
    (PRIORITY_CRITICAL=1, PRIORITY_HIGH=2, PRIORITY_NORMAL=5, etc.).
  - `model_name` / `model_version` on the job itself: provides per-job
    AI model configuration overrides (e.g. run this specific job with
    a newer model version for A/B testing).
  - `mark_failed()` implements exponential backoff retry scheduling:
    if retry_count < max_retries, the job is re-queued with next_retry_at
    set to 60s × 2^(retry_count-1). Only after exhausting all retries
    does the status become FAILED (terminal).
  - `input_data` / `result_data` as JSONB: flexible enough to accommodate
    any job type's payload without schema changes.

Worker query pattern (implemented in the worker service, not here):
    SELECT * FROM ai_processing_jobs
    WHERE status = 'queued'
      AND (next_retry_at IS NULL OR next_retry_at <= NOW())
    ORDER BY priority ASC, queued_at ASC
    LIMIT 10;

Relationships:
    ai_processing_jobs — no FK relationships (uses entity_type/entity_id pattern)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


# ---------------------------------------------------------------------------
# AIJobStatus Enum
# ---------------------------------------------------------------------------


class AIJobStatus(str, enum.Enum):
    """
    AI processing job lifecycle statuses, stored as VARCHAR(20) in PostgreSQL.

    Lifecycle:
        queued → processing → completed
                           ↘ failed (after max_retries exhausted)
        Any state → cancelled (admin or application request)

    Note: A failed job that still has retries remaining is re-queued
    (status returns to QUEUED with next_retry_at set) rather than
    entering the FAILED terminal state.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"         # Terminal — all retries exhausted
    CANCELLED = "cancelled"   # Terminal — explicitly cancelled

    @property
    def label(self) -> str:
        """Return a human-readable label for this status."""
        return _JOB_STATUS_LABELS.get(self, self.value.capitalize())

    @classmethod
    def values(cls) -> tuple:
        """Return all valid status value strings."""
        return tuple(s.value for s in cls)


_JOB_STATUS_LABELS: dict = {
    AIJobStatus.QUEUED: "Queued",
    AIJobStatus.PROCESSING: "Processing",
    AIJobStatus.COMPLETED: "Completed",
    AIJobStatus.FAILED: "Failed",
    AIJobStatus.CANCELLED: "Cancelled",
}

_TERMINAL_JOB_STATUSES: frozenset = frozenset({
    AIJobStatus.COMPLETED,
    AIJobStatus.FAILED,
    AIJobStatus.CANCELLED,
})


# ---------------------------------------------------------------------------
# AIJobType Enum
# ---------------------------------------------------------------------------


class AIJobType(str, enum.Enum):
    """
    AI operation types handled by the background worker, stored as VARCHAR(50).

    Each job type corresponds to a specific AI pipeline step:
      RESUME_PARSE         — Extract structured content from a resume file.
      RESUME_EMBED         — Generate and store a vector embedding in FAISS.
      RESUME_QUALITY_SCORE — Score resume quality (0–100) with category breakdown.
      RESUME_AI_SUMMARY    — Generate a one-paragraph AI candidate summary.
      APPLICATION_RANK     — Rank a candidate against a specific job.
      SKILL_GAP_ANALYZE    — Identify required/preferred skill gaps.
      INTERVIEW_GENERATE   — Generate tailored interview questions.
      BULK_RERANK          — Re-rank all candidates for a job after update.
    """

    RESUME_PARSE = "resume_parse"
    RESUME_EMBED = "resume_embed"
    RESUME_QUALITY_SCORE = "resume_quality_score"
    RESUME_AI_SUMMARY = "resume_ai_summary"
    APPLICATION_RANK = "application_rank"
    SKILL_GAP_ANALYZE = "skill_gap_analyze"
    INTERVIEW_GENERATE = "interview_generate"
    BULK_RERANK = "bulk_rerank"

    @property
    def label(self) -> str:
        """Return a human-readable label for this job type."""
        return _JOB_TYPE_LABELS.get(self, self.value.replace("_", " ").title())

    @classmethod
    def values(cls) -> tuple:
        """Return all valid job type value strings."""
        return tuple(t.value for t in cls)


_JOB_TYPE_LABELS: dict = {
    AIJobType.RESUME_PARSE: "Resume Parsing",
    AIJobType.RESUME_EMBED: "Embedding Generation",
    AIJobType.RESUME_QUALITY_SCORE: "Quality Scoring",
    AIJobType.RESUME_AI_SUMMARY: "AI Summary Generation",
    AIJobType.APPLICATION_RANK: "Candidate Ranking",
    AIJobType.SKILL_GAP_ANALYZE: "Skill Gap Analysis",
    AIJobType.INTERVIEW_GENERATE: "Interview Question Generation",
    AIJobType.BULK_RERANK: "Bulk Re-ranking",
}


# ---------------------------------------------------------------------------
# AIProcessingJob Model
# ---------------------------------------------------------------------------


class AIProcessingJob(db.Model):
    """
    A queued AI processing task awaiting execution by a background worker.

    Jobs are created by API endpoints or services when AI processing must
    happen asynchronously. Background workers poll this table, claim jobs
    by calling mark_started(), execute the AI operation, then call
    mark_completed() or mark_failed().

    Retry logic (implemented in mark_failed()):
        - On failure, retry_count is incremented.
        - If retry_count < max_retries: job is re-queued (status=QUEUED)
          with next_retry_at set using exponential backoff.
        - If retry_count >= max_retries: status becomes FAILED (terminal).

    Priority levels (use class constants):
        PRIORITY_CRITICAL = 1  — Immediate user-facing operation
        PRIORITY_HIGH     = 2  — User waiting for result
        PRIORITY_NORMAL   = 5  — Standard async processing
        PRIORITY_LOW      = 8  — Non-urgent background enrichment
        PRIORITY_BACKGROUND = 10 — Scheduled maintenance jobs
    """

    __tablename__ = "ai_processing_jobs"
    __table_args__ = (
        # Worker job pickup: fetch eligible jobs by priority, then queue order.
        # Partial index on queued/retrying jobs only — terminal jobs excluded.
        Index(
            "ix_ai_jobs_worker_pickup",
            "status",
            "priority",
            "queued_at",
            postgresql_where=text("status = 'queued'"),
        ),
        # Entity lookup: find all AI jobs for a specific resume/application.
        Index("ix_ai_jobs_entity_type_entity_id", "entity_type", "entity_id"),
        # Type + status monitoring for admin dashboards.
        Index("ix_ai_jobs_job_type_status", "job_type", "status"),
        # Retry scheduler: find jobs due for retry.
        Index(
            "ix_ai_jobs_next_retry_at",
            "next_retry_at",
            postgresql_where=text("status = 'queued' AND next_retry_at IS NOT NULL"),
        ),
        # Check constraints.
        CheckConstraint(
            "priority BETWEEN 1 AND 10",
            name="ck_ai_jobs_priority_range",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="ck_ai_jobs_retry_count_non_negative",
        ),
        CheckConstraint(
            "max_retries >= 0",
            name="ck_ai_jobs_max_retries_non_negative",
        ),
        CheckConstraint(
            "processing_time_ms IS NULL OR processing_time_ms >= 0",
            name="ck_ai_jobs_processing_time_non_negative",
        ),
        {
            "comment": (
                "Persistent async job queue for AI pipeline operations. "
                "Background workers poll this table for jobs to process."
            )
        },
    )

    # ------------------------------------------------------------------
    # Priority Constants
    # ------------------------------------------------------------------

    PRIORITY_CRITICAL: int = 1    # User blocking — must process immediately
    PRIORITY_HIGH: int = 2        # User waiting — process within seconds
    PRIORITY_NORMAL: int = 5      # Standard async — process within minutes
    PRIORITY_LOW: int = 8         # Non-urgent — process when workers are free
    PRIORITY_BACKGROUND: int = 10 # Scheduled — process during off-peak hours

    # ------------------------------------------------------------------
    # Expose Enums
    # ------------------------------------------------------------------

    Status = AIJobStatus
    JobType = AIJobType

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique AI job identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Job Identity
    # ------------------------------------------------------------------

    job_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc=(
            "The AI operation to perform. "
            "Use AIJobType enum constants. "
            "Determines which AI pipeline the worker will invoke."
        ),
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AIJobStatus.QUEUED.value,
        server_default=AIJobStatus.QUEUED.value,
        doc=(
            "Current job status. "
            "Use AIJobStatus enum constants. "
            "Always update via mark_started(), mark_completed(), mark_failed()."
        ),
    )

    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=PRIORITY_NORMAL,
        server_default=str(PRIORITY_NORMAL),
        doc=(
            "Processing priority (1–10, lower = higher priority). "
            "Use class constants: PRIORITY_CRITICAL=1, PRIORITY_HIGH=2, "
            "PRIORITY_NORMAL=5, PRIORITY_LOW=8, PRIORITY_BACKGROUND=10."
        ),
    )

    # ------------------------------------------------------------------
    # Target Entity (polymorphic reference)
    # ------------------------------------------------------------------

    entity_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc=(
            "Type of the entity this job operates on. "
            "Examples: 'resume' (for parsing/embedding), "
            "'application' (for ranking/skill_gap), 'job' (for bulk_rerank)."
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=False,
        doc="UUID of the specific entity this job will process.",
    )

    # ------------------------------------------------------------------
    # Payload
    # ------------------------------------------------------------------

    input_data: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Job-specific input parameters passed to the AI worker. "
            "Schema varies by job_type. "
            "Example for APPLICATION_RANK: { 'job_id': '...', 'resume_id': '...' }."
        ),
    )

    result_data: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Structured result returned by the AI worker on success. "
            "Schema varies by job_type. "
            "Example for RESUME_PARSE: { 'skills': [...], 'experience': [...] }. "
            "NULL until the job reaches COMPLETED status."
        ),
    )

    # ------------------------------------------------------------------
    # Error State
    # ------------------------------------------------------------------

    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "Human-readable error message from the last failed attempt. "
            "NULL while the job is queued or processing."
        ),
    )

    error_traceback: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "Full Python traceback from the last failed attempt. "
            "Stored for debugging. Excluded from non-admin API responses."
        ),
    )

    # ------------------------------------------------------------------
    # Retry State
    # ------------------------------------------------------------------

    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc=(
            "Number of failed attempts so far. "
            "Incremented by mark_failed(). "
            "When retry_count >= max_retries, status becomes FAILED (terminal)."
        ),
    )

    max_retries: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        server_default="3",
        doc=(
            "Maximum number of allowed attempts before the job is permanently failed. "
            "Default is 3 (initial attempt + 2 retries). "
            "Set to 0 for jobs that should not be retried."
        ),
    )

    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "UTC timestamp after which the job is eligible for retry. "
            "Set by mark_failed() using exponential backoff: 60s × 2^(retry_count-1). "
            "NULL for fresh jobs (not yet attempted) or terminal jobs."
        ),
    )

    # ------------------------------------------------------------------
    # AI Model Configuration
    # ------------------------------------------------------------------

    model_name: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        doc=(
            "AI model to use for this specific job. "
            "Overrides the system default when set. "
            "Example: 'gemini-1.5-pro'. "
            "Enables A/B testing of different model versions."
        ),
    )

    model_version: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc=(
            "Version of the AI model. "
            "Used with model_name for precise model selection and traceability."
        ),
    )

    # ------------------------------------------------------------------
    # Worker Tracking
    # ------------------------------------------------------------------

    worker_id: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        doc=(
            "Identifier of the worker process/thread that claimed this job. "
            "Format: '{hostname}:{pid}:{thread_id}'. "
            "NULL until a worker picks up the job."
        ),
    )

    processing_time_ms: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc=(
            "Total time taken by the AI operation in milliseconds. "
            "Measured from when the AI call started to when it returned. "
            "NULL until the job reaches COMPLETED status."
        ),
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------

    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        doc="UTC timestamp when this job was added to the queue.",
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="UTC timestamp when a worker claimed and started processing this job.",
    )

    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "UTC timestamp when this job reached a terminal state "
            "(COMPLETED, FAILED, or CANCELLED)."
        ),
    )

    # ------------------------------------------------------------------
    # Worker Lifecycle Methods
    # ------------------------------------------------------------------

    def mark_started(self, worker_id: str) -> None:
        """
        Claim this job for processing by a worker.

        Sets status to PROCESSING, records the worker ID and start time.
        Call `db.session.commit()` after to persist.

        Args:
            worker_id: Unique identifier of the claiming worker.
                       Recommended format: '{hostname}:{pid}'.
        """
        self.status = AIJobStatus.PROCESSING.value
        self.worker_id = worker_id
        self.started_at = datetime.now(timezone.utc)

    def mark_completed(
        self,
        result_data: dict,
        processing_time_ms: int,
        model_name: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> None:
        """
        Mark this job as successfully completed.

        Sets status to COMPLETED, stores the result, and records timing.
        Call `db.session.commit()` after to persist.

        Args:
            result_data       : Structured result from the AI operation.
            processing_time_ms: Time taken by the AI call in milliseconds.
            model_name        : Name of the model used (if different from job config).
            model_version     : Version of the model used.
        """
        self.status = AIJobStatus.COMPLETED.value
        self.result_data = result_data
        self.processing_time_ms = processing_time_ms
        self.completed_at = datetime.now(timezone.utc)
        if model_name is not None:
            self.model_name = model_name
        if model_version is not None:
            self.model_version = model_version

    def mark_failed(
        self,
        error_message: str,
        error_traceback: Optional[str] = None,
    ) -> None:
        """
        Record a failure and either re-queue for retry or terminate.

        Implements exponential backoff:
            retry 1: next_retry_at = now + 60s
            retry 2: next_retry_at = now + 120s
            retry 3: next_retry_at = now + 240s

        If retry_count < max_retries after incrementing:
            → status returns to QUEUED with next_retry_at set.
        If retry_count >= max_retries:
            → status becomes FAILED (terminal). completed_at is set.

        Call `db.session.commit()` after to persist.

        Args:
            error_message  : Human-readable description of the failure.
            error_traceback: Full Python traceback string (optional).
        """
        self.error_message = error_message
        self.error_traceback = error_traceback
        self.retry_count += 1

        if self.retry_count >= self.max_retries:
            # Terminal failure — all retries exhausted.
            self.status = AIJobStatus.FAILED.value
            self.next_retry_at = None
            self.completed_at = datetime.now(timezone.utc)
        else:
            # Eligible for retry — re-queue with exponential backoff.
            delay_seconds = 60 * (2 ** (self.retry_count - 1))
            self.next_retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
            )
            self.status = AIJobStatus.QUEUED.value

    def cancel(self) -> None:
        """
        Cancel this job, preventing it from being processed.

        Only valid for QUEUED jobs. A PROCESSING job should be cancelled
        by the worker service after the current operation finishes.
        Call `db.session.commit()` after to persist.
        """
        self.status = AIJobStatus.CANCELLED.value
        self.completed_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Computed Properties
    # ------------------------------------------------------------------

    @property
    def status_label(self) -> str:
        """Return the human-readable label for the current status."""
        try:
            return AIJobStatus(self.status).label
        except ValueError:
            return self.status.capitalize()

    @property
    def job_type_label(self) -> str:
        """Return the human-readable label for the job type."""
        try:
            return AIJobType(self.job_type).label
        except ValueError:
            return self.job_type.replace("_", " ").title()

    @property
    def is_terminal(self) -> bool:
        """Return True if this job has reached a non-recoverable final state."""
        return self.status in _TERMINAL_JOB_STATUSES

    @property
    def is_retrying(self) -> bool:
        """
        Return True if this job is re-queued after a failure (retry in progress).

        A retrying job has status QUEUED, retry_count > 0, and next_retry_at set.
        """
        return (
            self.status == AIJobStatus.QUEUED.value
            and self.retry_count > 0
            and self.next_retry_at is not None
        )

    @property
    def wait_time_ms(self) -> Optional[int]:
        """
        Return the time this job spent waiting in the queue (ms).

        Calculated as started_at - queued_at. None if not yet started.
        """
        if self.started_at is None or self.queued_at is None:
            return None
        delta = self.started_at - self.queued_at
        return int(delta.total_seconds() * 1000)

    @property
    def elapsed_ms(self) -> Optional[int]:
        """
        Return elapsed processing time in milliseconds.

        For completed jobs: returns stored processing_time_ms.
        For in-progress jobs: returns time elapsed since started_at.
        For queued/cancelled/failed: returns None.
        """
        if self.processing_time_ms is not None:
            return self.processing_time_ms
        if self.started_at is not None and self.status == AIJobStatus.PROCESSING.value:
            delta = datetime.now(timezone.utc) - self.started_at
            return int(delta.total_seconds() * 1000)
        return None

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<AIProcessingJob id={self.id} "
            f"type={self.job_type!r} "
            f"status={self.status!r} "
            f"priority={self.priority}>"
        )

    def __str__(self) -> str:
        return f"{self.job_type_label} ({self.status_label})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AIProcessingJob):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Full serialization including payload, result, and error detail.

        Use for admin job monitoring panels or detailed status queries.
        May return large JSONB fields (input_data, result_data, error_traceback).

        Returns:
            dict: All AI job fields for admin-facing API responses.
        """
        return {
            "id": str(self.id),
            "job_type": self.job_type,
            "job_type_label": self.job_type_label,
            "status": self.status,
            "status_label": self.status_label,
            "priority": self.priority,
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id),
            "input_data": self.input_data,
            "result_data": self.result_data,
            "error_message": self.error_message,
            "error_traceback": self.error_traceback,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "is_retrying": self.is_retrying,
            "next_retry_at": (
                self.next_retry_at.isoformat() if self.next_retry_at else None
            ),
            "model_name": self.model_name,
            "model_version": self.model_version,
            "worker_id": self.worker_id,
            "processing_time_ms": self.processing_time_ms,
            "elapsed_ms": self.elapsed_ms,
            "wait_time_ms": self.wait_time_ms,
            "is_terminal": self.is_terminal,
            "queued_at": (
                self.queued_at.isoformat() if self.queued_at else None
            ),
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }

    def to_status_dict(self) -> dict:
        """
        Lightweight status serialization for progress polling.

        Omits heavy payload fields (input_data, result_data, error_traceback)
        for use in periodic status-check API responses where the client
        only needs to know whether the job is done.

        Returns:
            dict: Status-essential fields for progress-tracking responses.
        """
        return {
            "id": str(self.id),
            "job_type": self.job_type,
            "status": self.status,
            "status_label": self.status_label,
            "is_terminal": self.is_terminal,
            "is_retrying": self.is_retrying,
            "retry_count": self.retry_count,
            "error_message": self.error_message,
            "processing_time_ms": self.processing_time_ms,
            "elapsed_ms": self.elapsed_ms,
            "queued_at": (
                self.queued_at.isoformat() if self.queued_at else None
            ),
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }

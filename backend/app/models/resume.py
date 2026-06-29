"""
models/resume.py
----------------
SQLAlchemy model for the `resumes` table.

Stores uploaded resume file metadata and the full AI-processed output
for every resume. This is the primary input to the AI analysis pipeline.

Enhancements in this version:
  - Resume versioning: `version` (INTEGER) + `previous_resume_id` (self-referential FK)
    Allows candidates to track upload history without losing previous versions.
  - SHA-256 deduplication: `sha256_hash` with a per-user unique index prevents
    a candidate from uploading the exact same file twice.
  - MIME type tracking: `mime_type` VARCHAR(100) for validation and storage decisions.
  - Explicit file size: `file_size_bytes` INTEGER with a positivity CHECK constraint.
  - Expanded 8-state parse_status lifecycle:
    uploaded → queued → parsing → parsed → embedding_generated → ranked → completed → failed
  - Soft delete via SoftDeleteMixin.

Design decisions:
  - `parsed_data` (JSONB): Full structured content extracted by the AI parser
    (skills, education, work experience, projects, certifications). Schema-flexible
    so the AI parser can evolve without migrations.
  - `quality_report` (JSONB): Per-category quality breakdown from the AI quality
    scorer. Stored separately from `quality_score` to preserve the detail behind
    the aggregate score.
  - `ai_metadata` (JSONB): Per-operation model traceability. Each AI operation
    (parsing, embedding, quality scoring) writes its model name, version, and
    processing time into this field for auditability.
  - Partial unique index on `(user_id) WHERE is_primary = TRUE AND deleted_at IS NULL`:
    Enforces one active primary resume per candidate at the database level.
  - `sha256_hash` is stored as a 64-character hex string (not binary) for
    readability in queries and simpler API exposure.

Relationships:
    resumes N:1 users
    resumes 1:N applications
    resumes 0:1 resumes (self-referential: previous version)
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.user import User


class Resume(TimestampMixin, SoftDeleteMixin, db.Model):
    """
    An uploaded resume file with its full AI-processing lifecycle.

    Processing pipeline:
        uploaded → queued → parsing → parsed → embedding_generated → ranked → completed
                                                                             ↘ failed

    Versioning:
        Each new upload by a candidate creates a new row with `version` incremented
        by one. `previous_resume_id` points to the immediately preceding version,
        forming a linked chain of upload history.

    Deduplication:
        The unique index on `(user_id, sha256_hash)` prevents a candidate from
        uploading the same file twice. The API layer should check this before
        creating a new row and return a meaningful error if a duplicate is detected.
    """

    __tablename__ = "resumes"
    __table_args__ = (
        # Deduplication: one unique file content per user
        Index(
            "uix_resumes_user_sha256",
            "user_id",
            "sha256_hash",
            unique=True,
        ),
        # Partial unique index: enforce exactly one active primary resume per candidate.
        # Uses postgresql_where for a conditional index — only one row per user_id
        # can have is_primary=TRUE when it is not soft-deleted.
        Index(
            "uix_resumes_one_primary_per_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary = TRUE AND deleted_at IS NULL"),
        ),
        # Query indexes
        Index("ix_resumes_user_id", "user_id"),
        Index("ix_resumes_parse_status", "parse_status"),
        Index("ix_resumes_is_primary", "is_primary"),
        Index("ix_resumes_previous_resume_id", "previous_resume_id"),
        # Check constraints
        CheckConstraint(
            "file_size_bytes > 0",
            name="ck_resumes_file_size_bytes_positive",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_resumes_version_at_least_one",
        ),
        CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 100)",
            name="ck_resumes_quality_score_range",
        ),
        {
            "comment": (
                "Uploaded resume files and their AI-processing state. "
                "parsed_data, quality_report, and ai_metadata are JSONB — "
                "populated progressively as the AI pipeline processes the file."
            )
        },
    )

    # ------------------------------------------------------------------
    # Parse Status Constants — 8-State Lifecycle
    # ------------------------------------------------------------------

    UPLOADED: str = "uploaded"
    QUEUED: str = "queued"
    PARSING: str = "parsing"
    PARSED: str = "parsed"
    EMBEDDING_GENERATED: str = "embedding_generated"
    RANKED: str = "ranked"
    COMPLETED: str = "completed"
    FAILED: str = "failed"

    ALL_PARSE_STATUSES: tuple = (
        UPLOADED,
        QUEUED,
        PARSING,
        PARSED,
        EMBEDDING_GENERATED,
        RANKED,
        COMPLETED,
        FAILED,
    )

    PARSE_STATUS_LABELS: dict = {
        UPLOADED: "Uploaded",
        QUEUED: "Queued for Processing",
        PARSING: "Parsing Resume Content",
        PARSED: "Content Extracted",
        EMBEDDING_GENERATED: "Embedding Generated",
        RANKED: "Ranked Against Job",
        COMPLETED: "Processing Complete",
        FAILED: "Processing Failed",
    }

    # Statuses considered 'in-progress' (AI pipeline is working)
    IN_PROGRESS_STATUSES: tuple = (QUEUED, PARSING)

    # Statuses where parsed_data is available
    PARSEABLE_STATUSES: tuple = (
        PARSED,
        EMBEDDING_GENERATED,
        RANKED,
        COMPLETED,
    )

    # ------------------------------------------------------------------
    # File Type Constants
    # ------------------------------------------------------------------

    TYPE_PDF: str = "pdf"
    TYPE_DOCX: str = "docx"
    TYPE_TXT: str = "txt"
    TYPE_IMAGE: str = "image"

    ALL_FILE_TYPES: tuple = (TYPE_PDF, TYPE_DOCX, TYPE_TXT, TYPE_IMAGE)

    # ------------------------------------------------------------------
    # MIME Type Constants (reference values for API validation)
    # ------------------------------------------------------------------

    MIME_PDF: str = "application/pdf"
    MIME_DOCX: str = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    MIME_DOC: str = "application/msword"
    MIME_TXT: str = "text/plain"
    MIME_PNG: str = "image/png"
    MIME_JPG: str = "image/jpeg"
    MIME_WEBP: str = "image/webp"

    ALLOWED_MIME_TYPES: tuple = (
        MIME_PDF,
        MIME_DOCX,
        MIME_DOC,
        MIME_TXT,
        MIME_PNG,
        MIME_JPG,
        MIME_WEBP,
    )

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique resume identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Foreign Keys
    # ------------------------------------------------------------------

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        doc=(
            "Foreign key to the candidate who uploaded this resume. "
            "CASCADE: resumes are removed when the user is hard-deleted."
        ),
    )

    previous_resume_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("resumes.id", ondelete="SET NULL"),
        nullable=True,
        doc=(
            "Self-referential FK to the immediately preceding version of this resume. "
            "NULL for the first upload (version = 1). "
            "SET NULL: if the previous version is deleted, this link is cleared gracefully."
        ),
    )

    # ------------------------------------------------------------------
    # File Identity & Versioning
    # ------------------------------------------------------------------

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        doc=(
            "Version number of this resume upload. "
            "Starts at 1 for the first upload. "
            "Incremented by the service layer on each subsequent upload. "
            "Enforced >= 1 by CHECK constraint."
        ),
    )

    file_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Original filename as submitted by the candidate. Example: 'john_smith_cv.pdf'.",
    )

    file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        doc=(
            "Storage path or cloud URL for the uploaded file. "
            "In local storage: a relative path under UPLOAD_FOLDER. "
            "In cloud storage (future): a full object URL."
        ),
    )

    file_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc=(
            "Normalised file type category. "
            "Use constants: TYPE_PDF, TYPE_DOCX, TYPE_TXT, TYPE_IMAGE."
        ),
    )

    mime_type: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        doc=(
            "Detected MIME type of the uploaded file. "
            "Determined server-side using python-magic, not from the file extension. "
            "Use MIME_* class constants as reference values for validation. "
            'Example: "application/pdf", "image/png".'
        ),
    )

    file_size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc=(
            "File size in bytes. Enforced > 0 by CHECK constraint. "
            "Used for storage quota tracking and upload limit validation."
        ),
    )

    sha256_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        doc=(
            "SHA-256 hash of the file contents, encoded as a 64-character hex string. "
            "Combined with user_id in a UNIQUE index to detect duplicate uploads "
            "from the same candidate. Computed server-side before writing to DB."
        ),
    )

    # ------------------------------------------------------------------
    # Primary Resume Flag
    # ------------------------------------------------------------------

    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc=(
            "True = this is the candidate's active/default resume. "
            "Enforced at the DB level: only one active primary resume per candidate "
            "via the partial unique index uix_resumes_one_primary_per_active_user."
        ),
    )

    # ------------------------------------------------------------------
    # AI Processing State
    # ------------------------------------------------------------------

    parse_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=UPLOADED,
        server_default="uploaded",
        doc=(
            "Current position in the AI processing pipeline. "
            "Use constants: UPLOADED, QUEUED, PARSING, PARSED, "
            "EMBEDDING_GENERATED, RANKED, COMPLETED, FAILED."
        ),
    )

    parsed_data: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Structured content extracted from the resume by the AI parsing pipeline. "
            "Schema: { contact, summary, skills, experience, education, certifications, "
            "projects, languages }. "
            "NULL until parse_status reaches PARSED."
        ),
    )

    embedding_stored: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc=(
            "True = the resume's vector embedding has been computed and stored in FAISS. "
            "Provides an explicit flag for embedding state independent of parse_status, "
            "allowing embeddings to be invalidated and regenerated without changing status."
        ),
    )

    # ------------------------------------------------------------------
    # Quality Scoring
    # ------------------------------------------------------------------

    quality_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(5, 2),
        nullable=True,
        doc=(
            "AI-computed overall quality score from 0.00 to 100.00. "
            "Enforced in range [0, 100] by CHECK constraint. "
            "NULL until the quality scoring pipeline completes."
        ),
    )

    quality_report: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Per-category quality breakdown from the AI quality scorer. "
            "Schema: { formatting, contact_info, experience_detail, skills_clarity, "
            "quantifiable_achievements, grammar, overall_feedback }. "
            "NULL until quality scoring completes."
        ),
    )

    # ------------------------------------------------------------------
    # AI-Generated Content
    # ------------------------------------------------------------------

    ai_summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "AI-generated one-paragraph summary of the candidate's professional profile. "
            "Written to be shown to recruiters as a quick overview. "
            "Distinct from candidate_profiles.summary (manually written by the candidate)."
        ),
    )

    ai_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "AI model traceability data, keyed by operation name. "
            "Each key records model_name, model_version, processed_at, and "
            "processing_time_ms for the corresponding AI operation. "
            "Operations: parsing, quality_scoring, embedding. "
            "Example: { 'parsing': { 'model_name': 'gemini-1.5-pro', "
            "'model_version': '001', 'processed_at': '...', "
            "'processing_time_ms': 2341, 'token_count': 1842 } }"
        ),
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------

    parsed_at: Mapped[Optional[object]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "UTC timestamp when the AI parsing pipeline completed for this resume. "
            "NULL if parsing has not yet completed. "
            "Note: created_at (from TimestampMixin) serves as the upload timestamp."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    user: Mapped["User"] = relationship(
        "User",
        back_populates="resumes",
        lazy="select",
        doc="The candidate who uploaded this resume.",
    )

    applications: Mapped[List["Application"]] = relationship(
        "Application",
        back_populates="resume",
        lazy="select",
        doc="All job applications that used this resume.",
    )

    # Self-referential relationship: this resume → its previous version.
    # `foreign()` annotation in primaryjoin marks `previous_resume_id` as
    # the FK side, and `Resume.id` as the referenced PK side.
    # `uselist=False` makes this a many-to-one (not one-to-many).
    previous_version: Mapped[Optional["Resume"]] = relationship(
        "Resume",
        primaryjoin="foreign(Resume.previous_resume_id) == Resume.id",
        uselist=False,
        lazy="select",
        doc=(
            "The immediately preceding version of this resume. "
            "None if this is version 1 (the first upload). "
            "Forms a linked chain: v3 → v2 → v1."
        ),
    )

    # ------------------------------------------------------------------
    # Computed Properties — Processing State
    # ------------------------------------------------------------------

    @property
    def parse_status_label(self) -> str:
        """Return the human-readable label for the current parse_status."""
        return self.PARSE_STATUS_LABELS.get(self.parse_status, self.parse_status)

    @property
    def is_processing(self) -> bool:
        """
        Return True if the AI pipeline is actively working on this resume
        (status is QUEUED or PARSING).

        Used by the frontend to show a processing spinner.
        """
        return self.parse_status in self.IN_PROGRESS_STATUSES

    @property
    def is_parseable(self) -> bool:
        """
        Return True if `parsed_data` is available (parsing has completed).

        A resume is parseable when it has reached at least the PARSED state.
        This does not require the full pipeline to be complete — the
        ranking and embedding steps can still be pending.
        """
        return self.parse_status in self.PARSEABLE_STATUSES

    @property
    def is_fully_processed(self) -> bool:
        """
        Return True if the complete AI pipeline has finished successfully
        (status is COMPLETED).
        """
        return self.parse_status == self.COMPLETED

    @property
    def is_failed(self) -> bool:
        """Return True if any step in the AI pipeline has failed."""
        return self.parse_status == self.FAILED

    # ------------------------------------------------------------------
    # Computed Properties — File Information
    # ------------------------------------------------------------------

    @property
    def file_size_readable(self) -> str:
        """
        Return the file size as a human-readable string.

        Examples: '512.0 B', '2.4 KB', '1.8 MB'.

        Returns:
            str: Human-readable file size, or 'Unknown' if file_size_bytes is unset.
        """
        if self.file_size_bytes is None:
            return "Unknown"

        size = float(self.file_size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    @property
    def is_image_resume(self) -> bool:
        """Return True if this resume was uploaded as an image file."""
        return self.file_type == self.TYPE_IMAGE

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<Resume id={self.id} "
            f"user_id={self.user_id} "
            f"version={self.version} "
            f"status={self.parse_status!r}>"
        )

    def __str__(self) -> str:
        return f"{self.file_name} (v{self.version})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Resume):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Full serialization including all AI-processed content.

        Includes parsed_data, quality_report, and ai_metadata — large
        JSONB fields only needed for the full resume detail view.

        Returns:
            dict: All resume fields for recruiter analysis and candidate detail views.
        """
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "version": self.version,
            "previous_resume_id": (
                str(self.previous_resume_id) if self.previous_resume_id else None
            ),
            "file_name": self.file_name,
            "file_type": self.file_type,
            "mime_type": self.mime_type,
            "file_size_bytes": self.file_size_bytes,
            "file_size_readable": self.file_size_readable,
            "sha256_hash": self.sha256_hash,
            "is_primary": self.is_primary,
            "is_image_resume": self.is_image_resume,
            "parse_status": self.parse_status,
            "parse_status_label": self.parse_status_label,
            "is_processing": self.is_processing,
            "is_parseable": self.is_parseable,
            "is_fully_processed": self.is_fully_processed,
            "is_failed": self.is_failed,
            "embedding_stored": self.embedding_stored,
            "quality_score": (
                float(self.quality_score)
                if self.quality_score is not None
                else None
            ),
            "quality_report": self.quality_report,
            "ai_summary": self.ai_summary,
            "ai_metadata": self.ai_metadata,
            "parsed_data": self.parsed_data,
            "is_deleted": self.is_deleted,
            "uploaded_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "parsed_at": (
                self.parsed_at.isoformat() if self.parsed_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
            "deleted_at": (
                self.deleted_at.isoformat() if self.deleted_at else None
            ),
        }

    def to_list_dict(self) -> dict:
        """
        Compact serialization for resume list views.

        Omits heavy JSONB fields (parsed_data, quality_report, ai_metadata)
        to keep paginated list responses lean. Suitable for a candidate's
        'My Resumes' page or a recruiter's applicant list.

        Returns:
            dict: Lightweight resume fields for list and summary views.
        """
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "version": self.version,
            "previous_resume_id": (
                str(self.previous_resume_id) if self.previous_resume_id else None
            ),
            "file_name": self.file_name,
            "file_type": self.file_type,
            "mime_type": self.mime_type,
            "file_size_bytes": self.file_size_bytes,
            "file_size_readable": self.file_size_readable,
            "is_primary": self.is_primary,
            "parse_status": self.parse_status,
            "parse_status_label": self.parse_status_label,
            "is_processing": self.is_processing,
            "is_fully_processed": self.is_fully_processed,
            "is_failed": self.is_failed,
            "quality_score": (
                float(self.quality_score)
                if self.quality_score is not None
                else None
            ),
            "ai_summary": self.ai_summary,
            "is_deleted": self.is_deleted,
            "uploaded_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }

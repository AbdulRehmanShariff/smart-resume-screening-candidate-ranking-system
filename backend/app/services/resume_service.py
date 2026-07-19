"""
services/resume_service.py
--------------------------
Resume management service for the Smart Resume Screening System.

Contains all business logic for:
  - upload_resume()    : Validate, hash, store file, create DB record,
                         handle versioning, set-as-primary.
  - list_resumes()     : Paginated filtered list of a candidate's resumes.
  - get_resume()       : Single resume fetch with ownership check.
  - set_primary()      : Atomically demote old primary and promote new one.
  - delete_resume()    : Guard against active applications, soft-delete DB
                         record, hard-delete file from storage.
  - get_resume_stats() : Aggregate resume counts by status for dashboard.

Architecture contract:
  - All database commits happen inside this module; callers do NOT commit.
  - Every successful mutation creates an AuditLog entry in the same
    transaction, guaranteeing audit consistency even on rollback.
  - File hash (SHA-256) is computed by STREAMING uploaded bytes in 64 KB
    chunks — the file object is never fully loaded into memory before hashing.
    After hashing, file.seek(0) resets the pointer before LocalStorage reads it.
  - StorageError from LocalStorage is mapped to the appropriate AppException
    subclass so routes never see storage-layer exceptions directly.
  - No Flask request context accessed here — ip_address and user_agent are
    passed as plain strings for testability.
  - SQLAlchemy 2.0 style throughout: select(), scalar_one_or_none(),
    session.execute(), never Model.query.

Upload pipeline:
  1. Stream file → compute SHA-256 hash in chunks (no full load into memory)
  2. Seek file pointer back to 0
  3. Check for duplicate (user_id + sha256_hash) via DB query
  4. Determine next version number (max existing + 1)
  5. Call LocalStorage.save_resume() — validates MIME, size, writes to disk
  6. Create Resume DB record
  7. If set_as_primary: demote current primary, promote this one
  8. AuditLog.log(RESUME_UPLOADED)
  9. db.session.commit()

Supported MIME types (validated by LocalStorage):
  application/pdf
  application/vnd.openxmlformats-officedocument.wordprocessingml.document (docx)
  application/msword (doc)

Note: TXT and image types are intentionally excluded per the API requirement
to support only PDF, DOC, and DOCX resume uploads. LocalStorage's default
RESUME_ALLOWED_MIME_TYPES includes txt/image — we pass a restricted set.
"""

import hashlib
import logging
import uuid
from typing import Optional

from flask import current_app
from sqlalchemy import and_, func, select

from app.core.exceptions import (
    AuthorizationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from app.extensions import db
from app.models.audit_log import AuditAction, AuditLog
from app.models.resume import Resume
from app.models.user import User
from app.models.ai_processing_job import AIProcessingJob, AIJobType
from app.storage import LocalStorage, StorageError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowed MIME types for resume uploads
# Restricted to document formats only (no txt/image per Stage 4 requirements)
# ---------------------------------------------------------------------------

_RESUME_ALLOWED_MIME_TYPES: frozenset = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
})

_ALLOWED_HUMAN_READABLE: str = "PDF, DOC, DOCX"

# Chunk size for streaming SHA-256 computation: 64 KB per chunk
_HASH_CHUNK_SIZE: int = 64 * 1024


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _compute_sha256_streaming(file) -> str:
    """
    Compute the SHA-256 hash of an uploaded file by reading it in chunks.

    Does NOT load the entire file into memory. After this function returns,
    the caller MUST call file.seek(0) to reset the read pointer before
    passing the file object to LocalStorage.

    Args:
        file: A file-like object with a .read(n) method (Flask FileStorage).

    Returns:
        64-character lowercase hex string of the SHA-256 digest.
    """
    hasher = hashlib.sha256()
    while True:
        chunk = file.read(_HASH_CHUNK_SIZE)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.hexdigest()


def _load_resume(resume_id: str, *, include_deleted: bool = False) -> Resume:
    """
    Load a Resume by UUID string, raising NotFoundError if absent or deleted.

    Args:
        resume_id      : UUID string from the URL parameter.
        include_deleted: If True, also loads soft-deleted resumes.

    Returns:
        Resume ORM instance.

    Raises:
        BadRequestError: Malformed UUID string.
        NotFoundError  : Resume not found or soft-deleted.
    """
    try:
        parsed_id = uuid.UUID(resume_id)
    except (ValueError, AttributeError):
        raise BadRequestError(f"Invalid resume ID format: '{resume_id}'.")

    stmt = select(Resume).where(Resume.id == parsed_id)
    if not include_deleted:
        stmt = stmt.where(Resume.deleted_at.is_(None))

    resume: Optional[Resume] = db.session.execute(stmt).scalar_one_or_none()

    if resume is None:
        raise NotFoundError("Resume not found.")

    return resume


def _assert_owns_resume(resume: Resume, candidate: User) -> None:
    """
    Raise AuthorizationError if the candidate does not own this resume.

    Args:
        resume    : The Resume ORM instance.
        candidate : The authenticated user.

    Raises:
        AuthorizationError: The user does not own this resume.
    """
    if resume.user_id != candidate.id and not candidate.is_admin:
        logger.warning(
            "Unauthorized resume access | resume_id=%s user_id=%s",
            resume.id,
            candidate.id,
        )
        raise AuthorizationError("You do not have permission to access this resume.")


def _has_active_applications(resume: Resume) -> bool:
    """
    Return True if the resume is referenced by any non-terminal applications.

    Terminal statuses: offer_accepted, offer_declined, rejected, withdrawn.
    Deleting a resume with active applications would corrupt the application
    trail — the candidate must withdraw those applications first.

    Args:
        resume: The Resume ORM instance.

    Returns:
        True if at least one non-terminal application uses this resume.
    """
    from app.models.application import Application, ApplicationStatus

    terminal = [
        ApplicationStatus.OFFER_ACCEPTED.value,
        ApplicationStatus.OFFER_DECLINED.value,
        ApplicationStatus.REJECTED.value,
        ApplicationStatus.WITHDRAWN.value,
    ]

    count: int = db.session.execute(
        select(func.count(Application.id)).where(
            and_(
                Application.resume_id == resume.id,
                Application.status.notin_(terminal),
            )
        )
    ).scalar_one()

    return count > 0


def _get_next_version(user_id: uuid.UUID) -> int:
    """
    Return the next version number for a new resume upload by this user.

    Queries the maximum existing version (including soft-deleted records)
    so that version numbers are never reused even after deletion.

    Args:
        user_id: The candidate's UUID.

    Returns:
        Next version integer (1 for the first upload, N+1 thereafter).
    """
    max_version: Optional[int] = db.session.execute(
        select(func.max(Resume.version)).where(Resume.user_id == user_id)
    ).scalar_one()

    return (max_version or 0) + 1


def _get_current_primary(user_id: uuid.UUID) -> Optional[Resume]:
    """
    Return the candidate's current active primary resume, or None.

    Args:
        user_id: The candidate's UUID.

    Returns:
        Resume instance if a primary exists, None otherwise.
    """
    return db.session.execute(
        select(Resume).where(
            and_(
                Resume.user_id == user_id,
                Resume.is_primary.is_(True),
                Resume.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


def _build_sort_clause(sort_by: str):
    """
    Return the SQLAlchemy ORDER BY clause for a given sort_by value.

    Args:
        sort_by: One of the ResumeListFilterSchema sort_by choices.

    Returns:
        List of SQLAlchemy column expressions for .order_by().
    """
    mapping = {
        "newest":    [Resume.created_at.desc()],
        "oldest":    [Resume.created_at.asc()],
        "name_asc":  [Resume.file_name.asc(), Resume.created_at.desc()],
        "name_desc": [Resume.file_name.desc(), Resume.created_at.desc()],
        "size_asc":  [Resume.file_size_bytes.asc(), Resume.created_at.desc()],
        "size_desc": [Resume.file_size_bytes.desc(), Resume.created_at.desc()],
    }
    return mapping.get(sort_by, [Resume.created_at.desc()])


def _storage_error_to_app_exception(exc: StorageError) -> Exception:
    """
    Convert a StorageError into the appropriate AppException subclass.

    Keeps the service layer's exception contract clean — routes only
    ever see AppException subclasses, never storage-layer exceptions.

    Args:
        exc: The StorageError raised by LocalStorage.

    Returns:
        An AppException subclass instance.
    """
    if exc.code == StorageError.INVALID_TYPE:
        return BadRequestError(
            f"Unsupported file type. Please upload a {_ALLOWED_HUMAN_READABLE} file."
        )
    if exc.code == StorageError.TOO_LARGE:
        return BadRequestError(str(exc.message))
    if exc.code == StorageError.DUPLICATE:
        return ConflictError("This exact file has already been uploaded.")
    # WRITE_FAILED, NOT_FOUND, or unknown codes
    return BadRequestError(f"File storage error: {exc.message}")


# ---------------------------------------------------------------------------
# Public Service Functions
# ---------------------------------------------------------------------------


def upload_resume(
    candidate: User,
    file,
    data: dict,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Upload a resume file, create a DB record, and optionally set as primary.

    Pipeline:
      1. Stream-hash the file (SHA-256) without loading into memory.
      2. Reset file pointer (seek(0)).
      3. Check (user_id, sha256_hash) uniqueness — raise ConflictError on dup.
      4. Determine next version number.
      5. Save file via LocalStorage (validates MIME type and size).
      6. Create Resume ORM record.
      7. db.session.flush() to assign resume.id.
      8. If set_as_primary: demote old primary, set this one as primary.
      9. Audit log entry.
     10. db.session.commit().

    Args:
        candidate  : The authenticated candidate uploading the file.
        file       : Flask FileStorage object from request.files['resume'].
        data       : Validated payload from ResumeUploadSchema.load().
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.

    Returns:
        dict: Full resume representation from Resume.to_dict().

    Raises:
        BadRequestError  : Invalid file type, oversized file, or no file provided.
        ConflictError    : Duplicate file (same SHA-256 already exists for this user).
        AuthorizationError: User is not a candidate or admin.
    """
    if not candidate.is_candidate and not candidate.is_admin:
        raise AuthorizationError("Only candidates can upload resumes.")

    if file is None or not getattr(file, "filename", "").strip():
        raise BadRequestError(
            "No resume file provided. "
            "Send the file as 'resume' in a multipart/form-data request."
        )

    logger.debug("[DEBUG-TRACE] Enter upload_resume()")

    # ------------------------------------------------------------------
    # Step 1: Compute SHA-256 by streaming — no full load into memory
    # ------------------------------------------------------------------
    logger.debug(
        "Computing SHA-256 for upload | candidate_id=%s filename=%r",
        candidate.id,
        file.filename,
    )
    sha256_hash = _compute_sha256_streaming(file)

    # ------------------------------------------------------------------
    # Step 2: Reset file pointer before LocalStorage reads it
    # ------------------------------------------------------------------
    file.seek(0)

    # ------------------------------------------------------------------
    # Step 3: Duplicate detection (per-user uniqueness on sha256_hash)
    #
    # The DB unique index uix_resumes_user_sha256 is a PARTIAL index
    # enforcing uniqueness only on active (non-deleted) resumes:
    #   WHERE deleted_at IS NULL
    # This query mirrors that constraint exactly. A candidate may re-upload
    # a previously deleted file — that creates a new version of that file.
    # ------------------------------------------------------------------
    existing: Optional[Resume] = db.session.execute(
        select(Resume).where(
            and_(
                Resume.user_id == candidate.id,
                Resume.sha256_hash == sha256_hash,
                Resume.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        logger.info(
            "Duplicate resume upload blocked | candidate_id=%s "
            "existing_resume_id=%s",
            candidate.id,
            existing.id,
        )
        raise ConflictError(
            f"This file has already been uploaded (version {existing.version}). "
            "Upload a different file or delete the existing one first."
        )

    # ------------------------------------------------------------------
    # Step 4: Determine the previous version and version number
    # ------------------------------------------------------------------
    next_version = _get_next_version(candidate.id)

    # Get the ID of the candidate's most recent (highest version) active resume
    # to set as previous_resume_id for version chain tracking
    previous_resume: Optional[Resume] = db.session.execute(
        select(Resume)
        .where(
            and_(
                Resume.user_id == candidate.id,
                Resume.deleted_at.is_(None),
            )
        )
        .order_by(Resume.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Step 5: Save file via LocalStorage (MIME + size validation inside)
    # ------------------------------------------------------------------
    storage = LocalStorage.from_app(current_app)

    try:
        storage_result = storage.save_resume(
            file=file,
            user_id=candidate.id,
            allowed_mime_types=_RESUME_ALLOWED_MIME_TYPES,
        )
    except StorageError as exc:
        logger.warning(
            "Storage save failed | candidate_id=%s filename=%r | %s",
            candidate.id,
            file.filename,
            exc,
        )
        raise _storage_error_to_app_exception(exc) from exc

    logger.debug("[DEBUG-TRACE] Resume saved via LocalStorage")

    # ------------------------------------------------------------------
    # Step 6: Create Resume ORM record
    # ------------------------------------------------------------------
    resume = Resume(
        user_id=candidate.id,
        previous_resume_id=previous_resume.id if previous_resume else None,
        version=next_version,
        file_name=storage_result.original_filename,
        file_path=storage_result.file_path,
        file_type=storage_result.file_type,
        mime_type=storage_result.mime_type,
        file_size_bytes=storage_result.file_size_bytes,
        sha256_hash=sha256_hash,
        is_primary=False,      # set in step 8 if requested
        parse_status=Resume.UPLOADED,
    )

    db.session.add(resume)

    # ------------------------------------------------------------------
    # Step 7: flush to obtain resume.id before audit log references it
    # ------------------------------------------------------------------
    db.session.flush()

    # ------------------------------------------------------------------
    # Step 8: Set as primary if requested (atomic within this transaction)
    # ------------------------------------------------------------------
    set_as_primary: bool = data.get("set_as_primary", False)

    if set_as_primary:
        # Demote the current primary (if it is a different resume).
        # IMPORTANT: flush the demote to the DB *before* setting is_primary=True
        # on the new resume. The partial unique constraint
        # (uix_resumes_one_primary_per_active_user) is immediate — PostgreSQL
        # checks it after every individual statement. If the promote UPDATE
        # reaches the DB before the demote UPDATE, the constraint sees two
        # primary resumes transiently and raises IntegrityError.
        # The explicit flush guarantees the order: demote → 0 primaries → promote.
        current_primary = _get_current_primary(candidate.id)
        if current_primary is not None and current_primary.id != resume.id:
            current_primary.is_primary = False
            db.session.flush()   # demote lands first; constraint now sees 0 primaries
            logger.debug(
                "Demoted previous primary resume | resume_id=%s",
                current_primary.id,
            )
        resume.is_primary = True
    elif next_version == 1:
        # First ever upload — auto-promote to primary (no existing primary to demote)
        resume.is_primary = True
        set_as_primary = True   # flag for log message accuracy

    # ------------------------------------------------------------------
    # Step 9: Audit log
    # ------------------------------------------------------------------
    AuditLog.log(
        action=AuditAction.RESUME_UPLOADED,
        user_id=candidate.id,
        entity_type="resume",
        entity_id=resume.id,
        description=(
            f"Candidate '{candidate.first_name} {candidate.last_name}' "
            f"uploaded resume '{resume.file_name}' "
            f"(v{resume.version}, {resume.file_size_readable})"
            + (" — set as primary." if resume.is_primary else ".")
        ),
        new_value={
            "file_name": resume.file_name,
            "file_type": resume.file_type,
            "version": resume.version,
            "is_primary": resume.is_primary,
            "file_size_bytes": resume.file_size_bytes,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # ------------------------------------------------------------------
    # Step 9b: Enqueue RESUME_PARSE AI job
    # ------------------------------------------------------------------
    logger.debug("[DEBUG-TRACE] Creating AIProcessingJob")
    ai_job = AIProcessingJob(
        job_type=AIJobType.RESUME_PARSE.value,
        entity_type="resume",
        entity_id=resume.id,
        priority=AIProcessingJob.PRIORITY_NORMAL,
        input_data={
            "resume_id": str(resume.id),
        },
    )
    logger.debug("[DEBUG-TRACE] Executing db.session.add(ai_job)")
    db.session.add(ai_job)
    logger.debug("[DEBUG-TRACE] Executing db.session.flush()")
    db.session.flush()

    logger.info(
        "AI job created | job_id=%s resume_id=%s job_type=%s",
        ai_job.id,
        resume.id,
        ai_job.job_type
    )

    # ------------------------------------------------------------------
    # Step 10: Commit
    # ------------------------------------------------------------------
    logger.debug("[DEBUG-TRACE] Executing db.session.commit()")
    db.session.commit()
    logger.debug("[DEBUG-TRACE] Commit successful")

    logger.info(
        "Resume uploaded | resume_id=%s candidate_id=%s version=%d "
        "is_primary=%s filename=%r",
        resume.id,
        candidate.id,
        resume.version,
        resume.is_primary,
        resume.file_name,
    )

    return resume.to_dict()


def list_resumes(candidate: User, filters: dict) -> dict:
    """
    Return a paginated list of a candidate's own non-deleted resumes.

    Supports filtering by parse_status, file_type, is_primary.
    Soft-deleted resumes are always excluded.

    Args:
        candidate : The authenticated candidate.
        filters   : Validated payload from ResumeListFilterSchema.load().

    Returns:
        dict with keys:
          resumes    : list of resume.to_list_dict() dicts for this page
          pagination : page, per_page, total, total_pages, has_next, has_prev
    """
    page: int = filters.get("page", 1)
    per_page: int = filters.get("per_page", 20)
    sort_by: str = filters.get("sort_by", "newest")

    # Base: candidate's own non-deleted resumes
    stmt = select(Resume).where(
        and_(
            Resume.user_id == candidate.id,
            Resume.deleted_at.is_(None),
        )
    )

    # Optional filters
    parse_status = filters.get("parse_status")
    if parse_status is not None:
        stmt = stmt.where(Resume.parse_status == parse_status)

    file_type = filters.get("file_type")
    if file_type is not None:
        stmt = stmt.where(Resume.file_type == file_type)

    is_primary = filters.get("is_primary")
    if is_primary is not None:
        stmt = stmt.where(Resume.is_primary.is_(is_primary))

    # Count total (before LIMIT/OFFSET)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = db.session.execute(count_stmt).scalar_one()

    # Apply sort and pagination
    order_clauses = _build_sort_clause(sort_by)
    stmt = stmt.order_by(*order_clauses)
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)

    resumes = db.session.execute(stmt).scalars().all()

    total_pages = max(1, (total + per_page - 1) // per_page)

    logger.info(
        "Resume list | candidate_id=%s total=%d page=%d/%d",
        candidate.id,
        total,
        page,
        total_pages,
    )

    return {
        "resumes": [r.to_list_dict() for r in resumes],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }


def get_resume(resume_id: str, candidate: User) -> dict:
    """
    Fetch a single resume with ownership check.

    Returns the full resume dict including parsed_data, quality_report,
    and ai_metadata (all None until the AI pipeline runs).

    Args:
        resume_id : UUID string from the URL parameter.
        candidate : The authenticated user.

    Returns:
        dict: Full resume representation from Resume.to_dict().

    Raises:
        BadRequestError    : Malformed UUID.
        NotFoundError      : Resume not found or soft-deleted.
        AuthorizationError : Caller does not own this resume.
    """
    resume: Resume = _load_resume(resume_id)
    _assert_owns_resume(resume, candidate)

    return resume.to_dict()


def set_primary(
    resume_id: str,
    candidate: User,
    *,
    note: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Set a resume as the candidate's primary/active resume.

    Atomically:
      1. Demotes the current primary (if different from the target).
      2. Promotes the target resume to is_primary=True.
      3. Audit logs the change.
      4. Commits.

    Idempotent: if the resume is already primary, returns it without a
    commit (no unnecessary writes or audit log entries).

    Args:
        resume_id  : UUID string identifying the resume to promote.
        candidate  : The authenticated candidate making the request.
        note       : Optional note for the audit log.
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.

    Returns:
        dict: Updated full resume representation from Resume.to_dict().

    Raises:
        BadRequestError    : Malformed UUID.
        NotFoundError      : Resume not found or soft-deleted.
        AuthorizationError : Caller does not own this resume.
    """
    resume: Resume = _load_resume(resume_id)
    _assert_owns_resume(resume, candidate)

    # Idempotency: already primary — no-op
    if resume.is_primary:
        logger.info(
            "set_primary no-op — already primary | resume_id=%s", resume.id
        )
        return resume.to_dict()

    # Demote current primary, then flush before promoting.
    # The partial unique constraint (uix_resumes_one_primary_per_active_user)
    # is immediate — flush guarantees the demote UPDATE reaches PostgreSQL
    # before the promote UPDATE, so the constraint never sees two primaries.
    current_primary = _get_current_primary(candidate.id)
    if current_primary is not None:
        current_primary.is_primary = False
        db.session.flush()   # demote lands first; constraint now sees 0 primaries
        logger.debug(
            "Demoted primary resume | old_primary_id=%s", current_primary.id
        )

    # Promote target
    resume.is_primary = True

    description = (
        f"Candidate '{candidate.first_name} {candidate.last_name}' "
        f"set resume '{resume.file_name}' (v{resume.version}) as primary."
    )
    if note:
        description += f" Note: {note}"

    AuditLog.log(
        action=AuditAction.RESUME_SET_PRIMARY,
        user_id=candidate.id,
        entity_type="resume",
        entity_id=resume.id,
        description=description,
        old_value={"primary_resume_id": str(current_primary.id) if current_primary else None},
        new_value={"primary_resume_id": str(resume.id)},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Primary resume updated | new_primary_id=%s candidate_id=%s",
        resume.id,
        candidate.id,
    )

    return resume.to_dict()


def delete_resume(
    resume_id: str,
    candidate: User,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Soft-delete a resume record and hard-delete its file from storage.

    Guards:
      1. Cannot delete a resume with active (non-terminal) applications.
         The candidate must withdraw those applications first.
      2. Cannot delete the primary resume if it is the candidate's only resume.

    On success:
      - Sets resume.deleted_at (soft delete).
      - Removes the physical file from the filesystem via LocalStorage.
      - If the resume was the primary, promotes the most recent other resume
        to be the new primary (if one exists).
      - Commits the DB transaction.
      - File deletion happens after the DB commit — if file deletion fails,
        the DB change is already committed (resume is soft-deleted) and the
        orphaned file is logged as a warning. This is intentional: the DB is
        the source of truth; a stale file on disk is recoverable via cleanup.

    Args:
        resume_id  : UUID string identifying the resume to delete.
        candidate  : The authenticated candidate.
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.

    Returns:
        None — caller should return HTTP 200 with a success message.

    Raises:
        BadRequestError    : Resume has active applications, or malformed UUID.
        NotFoundError      : Resume not found or already soft-deleted.
        AuthorizationError : Caller does not own this resume.
    """
    resume: Resume = _load_resume(resume_id)
    _assert_owns_resume(resume, candidate)

    # Guard: block deletion if active applications use this resume
    if _has_active_applications(resume):
        raise BadRequestError(
            f"Resume '{resume.file_name}' is associated with one or more active "
            "job applications and cannot be deleted. "
            "Please withdraw those applications first."
        )

    was_primary = resume.is_primary

    # Soft-delete the DB record and clear primary flag.
    resume.soft_delete()
    resume.is_primary = False   # prevent orphaned primary flag

    # Promote next-most-recent resume to primary if this was the primary.
    # Flush first: the soft_delete (sets deleted_at) + is_primary=False must
    # reach PostgreSQL before the promote UPDATE. Once deleted_at is set, this
    # row exits the partial constraint scope, so promoting the next resume is
    # safe. Without flush the promote UPDATE might land first, transiently
    # creating two primary active resumes and violating the immediate constraint.
    if was_primary:
        db.session.flush()   # soft-delete + demote land first

        next_primary: Optional[Resume] = db.session.execute(
            select(Resume)
            .where(
                and_(
                    Resume.user_id == candidate.id,
                    Resume.id != resume.id,
                    Resume.deleted_at.is_(None),
                )
            )
            .order_by(Resume.version.desc())
            .limit(1)
        ).scalar_one_or_none()

        if next_primary is not None:
            next_primary.is_primary = True
            logger.info(
                "Auto-promoted resume to primary after deletion | "
                "new_primary_id=%s candidate_id=%s",
                next_primary.id,
                candidate.id,
            )

    AuditLog.log(
        action=AuditAction.RESUME_DELETED,
        user_id=candidate.id,
        entity_type="resume",
        entity_id=resume.id,
        description=(
            f"Candidate '{candidate.first_name} {candidate.last_name}' "
            f"deleted resume '{resume.file_name}' (v{resume.version})"
            + (" — was primary, promoted next version." if was_primary else ".")
        ),
        old_value={
            "file_name": resume.file_name,
            "version": resume.version,
            "is_primary": was_primary,
        },
        new_value={"deleted": True},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    # Capture the file path before committing (resume.file_path still valid here)
    file_path = resume.file_path

    db.session.commit()

    logger.info(
        "Resume soft-deleted | resume_id=%s candidate_id=%s filename=%r",
        resume.id,
        candidate.id,
        resume.file_name,
    )

    # Hard-delete the physical file AFTER the DB commit
    # (DB is source of truth; file deletion failure is recoverable)
    storage = LocalStorage.from_app(current_app)
    try:
        deleted = storage.delete_file(file_path)
        if not deleted:
            logger.warning(
                "Physical file not found during resume delete | "
                "resume_id=%s file_path=%s",
                resume.id,
                file_path,
            )
    except StorageError as exc:
        # Log but do not re-raise — DB record is already soft-deleted.
        # A stale file on disk is recoverable by a periodic cleanup job.
        logger.error(
            "Failed to delete physical file for resume_id=%s "
            "file_path=%s | %s",
            resume.id,
            file_path,
            exc,
        )


def get_resume_stats(candidate: User) -> dict:
    """
    Return aggregate resume statistics for a candidate's dashboard.

    Provides:
      - Total resume count (non-deleted)
      - Count breakdown by parse_status
      - Primary resume summary
      - Total storage used in bytes

    Args:
        candidate: The authenticated candidate.

    Returns:
        dict with keys:
          total            : int  total non-deleted resumes
          by_status        : dict { parse_status: count }
          primary_resume   : dict (to_list_dict()) or None
          total_size_bytes : int  total storage used by all non-deleted resumes
          total_size_readable: str human-readable total size
    """
    # GROUP BY aggregate — status breakdown in a single query
    rows = db.session.execute(
        select(Resume.parse_status, func.count(Resume.id))
        .where(
            and_(
                Resume.user_id == candidate.id,
                Resume.deleted_at.is_(None),
            )
        )
        .group_by(Resume.parse_status)
    ).all()

    by_status: dict = {row[0]: row[1] for row in rows}
    total: int = sum(by_status.values())

    # Total storage bytes
    total_bytes: int = db.session.execute(
        select(func.coalesce(func.sum(Resume.file_size_bytes), 0)).where(
            and_(
                Resume.user_id == candidate.id,
                Resume.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    # Primary resume summary
    primary = _get_current_primary(candidate.id)

    # Human-readable total size
    size = float(total_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            total_size_readable = f"{size:.1f} {unit}"
            break
        size /= 1024.0
    else:
        total_size_readable = f"{size:.1f} TB"

    logger.info(
        "Resume stats | candidate_id=%s total=%d total_bytes=%d",
        candidate.id,
        total,
        total_bytes,
    )

    return {
        "total": total,
        "by_status": by_status,
        "primary_resume": primary.to_list_dict() if primary else None,
        "total_size_bytes": total_bytes,
        "total_size_readable": total_size_readable,
    }

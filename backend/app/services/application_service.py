"""
services/application_service.py
---------------------------------
Application management service for the Smart Resume Screening System.

Contains all business logic for:
  - apply_for_job()             : Submit a new application and enqueue AI jobs.
  - get_application()           : Fetch a single application with role-aware serialization.
  - list_candidate_applications(): Paginated list of a candidate's own applications.
  - list_job_applicants()       : Paginated list of all applicants for a recruiter's job.
  - update_status()             : Advance an application through the 13-state lifecycle.
  - update_recruiter_notes()    : Save private recruiter notes.
  - withdraw_application()      : Candidate-initiated withdrawal.

Architecture contract (consistent with job_service.py and resume_service.py):
  - All database commits happen inside this module; callers do NOT commit.
  - Every successful mutation creates an AuditLog entry in the same transaction.
  - No Flask request context is accessed here — ip_address and user_agent are
    passed as plain strings for testability.
  - SQLAlchemy 2.0 style: select(), scalar_one_or_none(), session.execute().
  - AppException subclasses only — never raw SQLAlchemy or storage exceptions.

AI integration boundary:
  - apply_for_job() creates an AIProcessingJob record (APPLICATION_RANK) in the
    same database transaction as the Application record. This is the ONLY place
    in Stage 5 where the AI pipeline is touched.
  - The AIProcessingJob is committed atomically with the Application — either
    both exist or neither does (transaction safety).
  - The AI worker (future stage) polls ai_processing_jobs and writes back to the
    Application's JSONB fields. This service does not interact with the AI worker.
  - The APPLICATION_RANK job is created at PRIORITY_NORMAL (5). Recruiters who
    publish a job with many existing applicants may trigger BULK_RERANK — that
    is handled in job_service.py, not here.

Status lifecycle (13 states — enforced via _VALID_TRANSITIONS):
  applied → screening → assessment_pending → assessment_completed → shortlisted
          → technical_interview → hr_interview → final_interview
          → offer_sent → offer_accepted (terminal)
                       → offer_declined (terminal)
  Any active state → rejected  (terminal, recruiter-initiated)
  Any active state → withdrawn (terminal, candidate-initiated — use withdraw_application())
"""

import logging
import uuid
from typing import Optional

from sqlalchemy import and_, func, select

from app.core.exceptions import (
    AuthorizationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from app.extensions import db
from app.models.ai_processing_job import AIJobType, AIProcessingJob
from app.models.application import Application, ApplicationStatus
from app.models.audit_log import AuditAction, AuditLog
from app.models.job import Job
from app.models.resume import Resume
from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status Lifecycle — Allowed Transitions (recruiter-initiated)
# ---------------------------------------------------------------------------

# Map: current_status → frozenset of valid target statuses.
# "withdrawn" is intentionally absent — candidates use withdraw_application().
_VALID_TRANSITIONS: dict[str, frozenset] = {
    ApplicationStatus.APPLIED.value: frozenset({
        ApplicationStatus.SCREENING.value,
        ApplicationStatus.REJECTED.value,
    }),
    ApplicationStatus.SCREENING.value: frozenset({
        ApplicationStatus.ASSESSMENT_PENDING.value,
        ApplicationStatus.SHORTLISTED.value,
        ApplicationStatus.REJECTED.value,
    }),
    ApplicationStatus.ASSESSMENT_PENDING.value: frozenset({
        ApplicationStatus.ASSESSMENT_COMPLETED.value,
        ApplicationStatus.REJECTED.value,
    }),
    ApplicationStatus.ASSESSMENT_COMPLETED.value: frozenset({
        ApplicationStatus.SHORTLISTED.value,
        ApplicationStatus.REJECTED.value,
    }),
    ApplicationStatus.SHORTLISTED.value: frozenset({
        ApplicationStatus.TECHNICAL_INTERVIEW.value,
        ApplicationStatus.HR_INTERVIEW.value,
        ApplicationStatus.OFFER_SENT.value,
        ApplicationStatus.REJECTED.value,
    }),
    ApplicationStatus.TECHNICAL_INTERVIEW.value: frozenset({
        ApplicationStatus.HR_INTERVIEW.value,
        ApplicationStatus.FINAL_INTERVIEW.value,
        ApplicationStatus.OFFER_SENT.value,
        ApplicationStatus.REJECTED.value,
    }),
    ApplicationStatus.HR_INTERVIEW.value: frozenset({
        ApplicationStatus.FINAL_INTERVIEW.value,
        ApplicationStatus.OFFER_SENT.value,
        ApplicationStatus.REJECTED.value,
    }),
    ApplicationStatus.FINAL_INTERVIEW.value: frozenset({
        ApplicationStatus.OFFER_SENT.value,
        ApplicationStatus.REJECTED.value,
    }),
    ApplicationStatus.OFFER_SENT.value: frozenset({
        ApplicationStatus.OFFER_ACCEPTED.value,
        ApplicationStatus.OFFER_DECLINED.value,
        ApplicationStatus.REJECTED.value,
    }),
    # Terminal states — no further transitions
    ApplicationStatus.OFFER_ACCEPTED.value:  frozenset(),
    ApplicationStatus.OFFER_DECLINED.value:  frozenset(),
    ApplicationStatus.REJECTED.value:        frozenset(),
    ApplicationStatus.WITHDRAWN.value:       frozenset(),
}


# ---------------------------------------------------------------------------
# Sort clauses for list queries
# ---------------------------------------------------------------------------

def _build_sort_clause(sort_by: str) -> list:
    """
    Return SQLAlchemy ORDER BY clauses for application list queries.

    Args:
        sort_by: One of the ApplicationListFilterSchema sort_by choices.

    Returns:
        List of SQLAlchemy column expressions for .order_by().
    """
    mapping = {
        "newest":     [Application.applied_at.desc()],
        "oldest":     [Application.applied_at.asc()],
        "score_desc": [Application.match_score.desc().nullslast(), Application.applied_at.desc()],
        "score_asc":  [Application.match_score.asc().nullslast(), Application.applied_at.desc()],
        "status":     [Application.status.asc(), Application.applied_at.desc()],
    }
    return mapping.get(sort_by, [Application.applied_at.desc()])


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _load_application(application_id: str) -> Application:
    """
    Load an Application by UUID string.

    Args:
        application_id: UUID string from the URL parameter.

    Returns:
        Application ORM instance.

    Raises:
        BadRequestError: Malformed UUID string.
        NotFoundError  : Application not found.
    """
    try:
        parsed_id = uuid.UUID(application_id)
    except (ValueError, AttributeError):
        raise BadRequestError(f"Invalid application ID format: '{application_id}'.")

    application: Optional[Application] = db.session.execute(
        select(Application).where(Application.id == parsed_id)
    ).scalar_one_or_none()

    if application is None:
        raise NotFoundError("Application not found.")

    return application


def _assert_candidate_owns(application: Application, candidate: User) -> None:
    """
    Raise AuthorizationError if the candidate did not submit this application.

    Args:
        application: The Application ORM instance.
        candidate  : The authenticated user.

    Raises:
        AuthorizationError: The user is not the applicant.
    """
    if application.candidate_id != candidate.id and not candidate.is_admin:
        logger.warning(
            "Unauthorized application access | application_id=%s user_id=%s",
            application.id,
            candidate.id,
        )
        raise AuthorizationError(
            "You do not have permission to access this application."
        )


def _assert_recruiter_owns_job(application: Application, recruiter: User) -> None:
    """
    Raise AuthorizationError if the recruiter does not own the job this
    application belongs to.

    Args:
        application: The Application ORM instance.
        recruiter  : The authenticated recruiter.

    Raises:
        AuthorizationError: Recruiter does not own the job.
        NotFoundError     : The referenced job no longer exists.
    """
    job: Optional[Job] = db.session.execute(
        select(Job).where(Job.id == application.job_id)
    ).scalar_one_or_none()

    if job is None:
        raise NotFoundError("The job associated with this application was not found.")

    if job.recruiter_id != recruiter.id and not recruiter.is_admin:
        logger.warning(
            "Unauthorized applicant access | application_id=%s recruiter_id=%s",
            application.id,
            recruiter.id,
        )
        raise AuthorizationError(
            "You do not have permission to manage this application."
        )


def _enqueue_ranking_job(application: Application) -> None:
    """
    Create an AIProcessingJob record to rank this new application.

    Called inside apply_for_job() within the same open transaction, so the
    AI job is committed atomically with the Application record. If the
    transaction rolls back, both are cancelled together.

    The background worker (future stage) will pick this up, compute the
    match_score and score_breakdown, and write back to the Application.

    Args:
        application: The newly-created Application (must be flushed so
                     application.id is available).
    """
    ai_job = AIProcessingJob(
        job_type=AIJobType.APPLICATION_RANK.value,
        entity_type="application",
        entity_id=application.id,
        priority=AIProcessingJob.PRIORITY_NORMAL,
        input_data={
            "application_id": str(application.id),
            "job_id": str(application.job_id),
            "resume_id": str(application.resume_id),
        },
    )
    db.session.add(ai_job)
    logger.debug(
        "Enqueued APPLICATION_RANK job | application_id=%s", application.id
    )


# ---------------------------------------------------------------------------
# Public Service Functions
# ---------------------------------------------------------------------------


def apply_for_job(
    candidate: User,
    data: dict,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Submit a new job application and enqueue an AI ranking job.

    Guards:
      1. Job must exist and be in PUBLISHED status.
      2. Resume must be owned by this candidate and not soft-deleted.
      3. No duplicate application exists for this (job, candidate) pair.

    On success:
      - Creates the Application record (status = APPLIED).
      - Increments job.applications_count atomically.
      - Enqueues an AIProcessingJob (APPLICATION_RANK) in the same transaction.
      - Creates an AuditLog entry.
      - Commits everything atomically.

    The API returns 201 immediately. AI scoring happens asynchronously.

    Args:
        candidate  : The authenticated candidate submitting the application.
        data       : Validated payload from ApplicationCreateSchema.load().
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.

    Returns:
        dict: Candidate-facing application representation (to_candidate_dict()).

    Raises:
        AuthorizationError: User is not a candidate.
        NotFoundError     : Job or resume not found.
        BadRequestError   : Job is not published, or resume not owned by candidate.
        ConflictError     : Duplicate application for this (job, candidate) pair.
    """
    if not candidate.is_candidate and not candidate.is_admin:
        raise AuthorizationError("Only candidates can submit job applications.")

    job_id: uuid.UUID = data["job_id"]
    resume_id: uuid.UUID = data["resume_id"]

    # ------------------------------------------------------------------
    # Guard 1: Job must exist and be PUBLISHED
    # ------------------------------------------------------------------
    job: Optional[Job] = db.session.execute(
        select(Job).where(
            and_(
                Job.id == job_id,
                Job.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if job is None:
        raise NotFoundError("Job posting not found.")

    if job.status != Job.PUBLISHED:
        raise BadRequestError(
            "This job is not currently accepting applications. "
            "Only published jobs can be applied to."
        )

    # ------------------------------------------------------------------
    # Guard 2: Resume must exist, belong to this candidate, not deleted
    # ------------------------------------------------------------------
    resume: Optional[Resume] = db.session.execute(
        select(Resume).where(
            and_(
                Resume.id == resume_id,
                Resume.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if resume is None:
        raise NotFoundError("Resume not found.")

    if resume.user_id != candidate.id:
        raise AuthorizationError(
            "You can only apply with your own resumes."
        )

    # ------------------------------------------------------------------
    # Guard 3: No duplicate application for this (job, candidate) pair
    # ------------------------------------------------------------------
    duplicate: Optional[Application] = db.session.execute(
        select(Application).where(
            and_(
                Application.job_id == job_id,
                Application.candidate_id == candidate.id,
            )
        )
    ).scalar_one_or_none()

    if duplicate is not None:
        raise ConflictError(
            f"You have already applied for this position "
            f"(application status: {duplicate.status_label})."
        )

    # ------------------------------------------------------------------
    # Create Application record
    # ------------------------------------------------------------------
    application = Application(
        job_id=job_id,
        candidate_id=candidate.id,
        resume_id=resume_id,
        status=ApplicationStatus.APPLIED.value,
    )
    db.session.add(application)
    db.session.flush()   # assign application.id before audit log / AI job

    # ------------------------------------------------------------------
    # Increment job.applications_count (denormalized counter)
    # ------------------------------------------------------------------
    job.applications_count = (job.applications_count or 0) + 1

    # ------------------------------------------------------------------
    # Enqueue AI ranking job (atomic with Application commit)
    # ------------------------------------------------------------------
    _enqueue_ranking_job(application)

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    AuditLog.log(
        action=AuditAction.APPLICATION_SUBMITTED,
        user_id=candidate.id,
        entity_type="application",
        entity_id=application.id,
        description=(
            f"Candidate '{candidate.first_name} {candidate.last_name}' "
            f"applied for '{job.title}'."
        ),
        new_value={
            "job_id": str(job_id),
            "resume_id": str(resume_id),
            "status": application.status,
        },
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Application submitted | application_id=%s candidate_id=%s job_id=%s",
        application.id,
        candidate.id,
        job_id,
    )

    return application.to_candidate_dict()


def get_application(
    application_id: str,
    viewer: User,
) -> dict:
    """
    Fetch a single application with role-aware serialization.

    Serialization rules:
      - Candidate (owner): to_candidate_dict() — excludes recruiter-private fields.
      - Recruiter (job owner): to_recruiter_dict() — includes AI scores and notes.
      - Admin: to_dict() — full serialization.

    Args:
        application_id: UUID string from the URL parameter.
        viewer        : The authenticated user making the request.

    Returns:
        dict: Role-appropriate application representation.

    Raises:
        BadRequestError   : Malformed UUID.
        NotFoundError     : Application not found.
        AuthorizationError: Viewer is neither the candidate nor the job's recruiter.
    """
    application: Application = _load_application(application_id)

    if viewer.is_admin:
        return application.to_dict()

    if viewer.is_candidate:
        _assert_candidate_owns(application, viewer)
        return application.to_candidate_dict()

    if viewer.is_recruiter:
        _assert_recruiter_owns_job(application, viewer)
        return application.to_recruiter_dict()

    raise AuthorizationError("You do not have permission to view this application.")


def list_candidate_applications(
    candidate: User,
    filters: dict,
) -> dict:
    """
    Return a paginated list of a candidate's own applications.

    Supports filtering by status. Returns candidate-safe serialization
    (to_candidate_dict()) — never exposes recruiter-private fields.

    Args:
        candidate : The authenticated candidate.
        filters   : Validated payload from ApplicationListFilterSchema.load().

    Returns:
        dict with keys:
          applications : list of to_candidate_dict() for this page
          pagination   : page, per_page, total, total_pages, has_next, has_prev
    """
    page: int = filters.get("page", 1)
    per_page: int = filters.get("per_page", 20)
    sort_by: str = filters.get("sort_by", "newest")

    stmt = select(Application).where(
        Application.candidate_id == candidate.id
    )

    status_filter = filters.get("status")
    if status_filter:
        stmt = stmt.where(Application.status == status_filter)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = db.session.execute(count_stmt).scalar_one()

    stmt = stmt.order_by(*_build_sort_clause(sort_by))
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)

    applications = db.session.execute(stmt).scalars().all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    logger.info(
        "Candidate applications listed | candidate_id=%s total=%d page=%d/%d",
        candidate.id, total, page, total_pages,
    )

    return {
        "applications": [a.to_candidate_dict() for a in applications],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }


def list_job_applicants(
    job_id: str,
    recruiter: User,
    filters: dict,
) -> dict:
    """
    Return a paginated list of all applicants for a specific job.

    Only the job's owner recruiter (or an admin) may access this.
    Returns recruiter-facing serialization (to_recruiter_dict()) which
    includes AI scores, ranking, skill gaps, and recruiter notes.

    Args:
        job_id    : UUID string of the job posting.
        recruiter : The authenticated recruiter.
        filters   : Validated payload from ApplicationListFilterSchema.load().

    Returns:
        dict with keys:
          applications : list of to_recruiter_dict() for this page
          pagination   : page, per_page, total, total_pages, has_next, has_prev

    Raises:
        BadRequestError   : Malformed job UUID.
        NotFoundError     : Job not found.
        AuthorizationError: Recruiter does not own this job.
    """
    try:
        parsed_job_id = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        raise BadRequestError(f"Invalid job ID format: '{job_id}'.")

    job: Optional[Job] = db.session.execute(
        select(Job).where(
            and_(Job.id == parsed_job_id, Job.deleted_at.is_(None))
        )
    ).scalar_one_or_none()

    if job is None:
        raise NotFoundError("Job posting not found.")

    if job.recruiter_id != recruiter.id and not recruiter.is_admin:
        raise AuthorizationError(
            "You do not have permission to view applicants for this job."
        )

    page: int = filters.get("page", 1)
    per_page: int = filters.get("per_page", 20)
    sort_by: str = filters.get("sort_by", "newest")

    stmt = select(Application).where(Application.job_id == parsed_job_id)

    status_filter = filters.get("status")
    if status_filter:
        stmt = stmt.where(Application.status == status_filter)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = db.session.execute(count_stmt).scalar_one()

    stmt = stmt.order_by(*_build_sort_clause(sort_by))
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)

    applications = db.session.execute(stmt).scalars().all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    logger.info(
        "Job applicants listed | job_id=%s recruiter_id=%s total=%d page=%d/%d",
        parsed_job_id, recruiter.id, total, page, total_pages,
    )

    return {
        "applications": [a.to_recruiter_dict() for a in applications],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }


def update_status(
    application_id: str,
    target_status: str,
    recruiter: User,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Advance an application to a new status (recruiter-initiated).

    Enforces the 13-state lifecycle graph via _VALID_TRANSITIONS.
    Idempotent: if the application is already in the target status, returns
    it unchanged without writing to the DB.

    Args:
        application_id : UUID string of the application.
        target_status  : The desired new status (validated by schema).
        recruiter      : The authenticated recruiter making the transition.
        ip_address     : Client IP for the audit log.
        user_agent     : User-Agent header for the audit log.

    Returns:
        dict: Recruiter-facing application representation.

    Raises:
        BadRequestError   : Transition not permitted from the current state.
        NotFoundError     : Application not found.
        AuthorizationError: Recruiter does not own the job this application belongs to.
    """
    application: Application = _load_application(application_id)
    _assert_recruiter_owns_job(application, recruiter)

    current = application.status

    # Idempotency
    if current == target_status:
        logger.info(
            "Status update no-op | application_id=%s status=%s",
            application.id, current,
        )
        return application.to_recruiter_dict()

    # Lifecycle graph enforcement
    allowed = _VALID_TRANSITIONS.get(current, frozenset())
    if target_status not in allowed:
        allowed_str = ", ".join(sorted(allowed)) if allowed else "none"
        raise BadRequestError(
            f"Cannot transition application from '{current}' to '{target_status}'. "
            f"Allowed transitions from '{current}': {allowed_str}."
        )

    old_status = application.status
    application.update_status(target_status)   # updates status + status_updated_at

    AuditLog.log(
        action=AuditAction.APPLICATION_STATUS_CHANGED,
        user_id=recruiter.id,
        entity_type="application",
        entity_id=application.id,
        description=(
            f"Recruiter '{recruiter.first_name} {recruiter.last_name}' "
            f"moved application from '{old_status}' to '{target_status}'."
        ),
        old_value={"status": old_status},
        new_value={"status": target_status},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Application status updated | application_id=%s %s → %s recruiter_id=%s",
        application.id, old_status, target_status, recruiter.id,
    )

    return application.to_recruiter_dict()


def update_recruiter_notes(
    application_id: str,
    notes: Optional[str],
    recruiter: User,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Save private recruiter notes on an application.

    Notes are never exposed to the candidate in any API response.
    Pass None (or an empty string) to clear existing notes.

    Args:
        application_id : UUID string of the application.
        notes          : The new note text, or None to clear.
        recruiter      : The authenticated recruiter.
        ip_address     : Client IP for the audit log.
        user_agent     : User-Agent header for the audit log.

    Returns:
        dict: Recruiter-facing application representation.

    Raises:
        NotFoundError     : Application not found.
        AuthorizationError: Recruiter does not own the job.
    """
    application: Application = _load_application(application_id)
    _assert_recruiter_owns_job(application, recruiter)

    old_notes = application.recruiter_notes
    application.recruiter_notes = notes or None   # normalize empty string → None

    AuditLog.log(
        action=AuditAction.APPLICATION_NOTES_UPDATED,
        user_id=recruiter.id,
        entity_type="application",
        entity_id=application.id,
        description=(
            f"Recruiter '{recruiter.first_name} {recruiter.last_name}' "
            f"updated notes on application {application.id}."
        ),
        old_value={"recruiter_notes": old_notes},
        new_value={"recruiter_notes": application.recruiter_notes},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Recruiter notes updated | application_id=%s recruiter_id=%s",
        application.id, recruiter.id,
    )

    return application.to_recruiter_dict()


def withdraw_application(
    application_id: str,
    candidate: User,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Candidate-initiated withdrawal of an active application.

    Cannot withdraw an already-terminal application.

    Args:
        application_id : UUID string of the application.
        candidate      : The authenticated candidate withdrawing.
        ip_address     : Client IP for the audit log.
        user_agent     : User-Agent header for the audit log.

    Returns:
        dict: Candidate-facing application representation (status=withdrawn).

    Raises:
        NotFoundError     : Application not found.
        AuthorizationError: Candidate did not submit this application.
        BadRequestError   : Application is already in a terminal state.
    """
    application: Application = _load_application(application_id)
    _assert_candidate_owns(application, candidate)

    if application.is_terminal:
        raise BadRequestError(
            f"This application is already in a terminal state "
            f"('{application.status_label}') and cannot be withdrawn."
        )

    old_status = application.status
    application.update_status(ApplicationStatus.WITHDRAWN)

    AuditLog.log(
        action=AuditAction.APPLICATION_WITHDRAWN,
        user_id=candidate.id,
        entity_type="application",
        entity_id=application.id,
        description=(
            f"Candidate '{candidate.first_name} {candidate.last_name}' "
            f"withdrew their application (was '{old_status}')."
        ),
        old_value={"status": old_status},
        new_value={"status": ApplicationStatus.WITHDRAWN.value},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Application withdrawn | application_id=%s candidate_id=%s",
        application.id, candidate.id,
    )

    return application.to_candidate_dict()

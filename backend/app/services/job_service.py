"""
services/job_service.py
-----------------------
Job management service for the Smart Resume Screening System.

Contains all business logic for:
  - create_job()           : Create a new draft job posting
  - get_job()              : Fetch a single job with ownership/visibility checks
  - update_job()           : Partial update of a job's editable fields
  - transition_status()    : Enforce the 5-state lifecycle (draft → published …)
  - delete_job()           : Soft-delete with open-application guard
  - list_recruiter_jobs()  : Paginated list of a recruiter's own jobs
  - search_jobs()          : Candidate-facing paginated public job search
  - increment_view()       : Atomic views_count increment (no full ORM load)
  - get_job_stats()        : Application count breakdown by status for a job

Architecture contract:
  - All database commits happen inside this module; callers do NOT commit.
  - Every successful mutation creates an AuditLog entry in the same
    transaction, guaranteeing audit consistency even on rollback.
  - Exceptions use the custom hierarchy in app.core.exceptions; the global
    handler in the application factory converts them to JSON responses.
  - No Flask request context is accessed here — ip_address and user_agent are
    passed as plain strings for testability in isolation.
  - SQLAlchemy 2.0 style is used throughout: `select()`, `scalar_one_or_none()`,
    `session.execute()`, never `Model.query`.

Status lifecycle (5 states):
  draft  ──► published ──► paused ──► published  (re-open)
    └──────────────────────────────► closed ──► archived

Valid transitions:
  draft      → published
  published  → paused
  published  → closed
  paused     → published
  paused     → closed
  closed     → archived

All other transitions are rejected with BadRequestError.
"""

import logging
import uuid
from typing import Optional

from sqlalchemy import and_, func, or_, select, text, update

from app.core.exceptions import (
    AuthorizationError,
    BadRequestError,
    NotFoundError,
)
from app.extensions import db
from app.models.audit_log import AuditAction, AuditLog
from app.models.job import Job
from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status Lifecycle — Allowed Transitions
# ---------------------------------------------------------------------------

# Map: current_status → set of valid target statuses
_VALID_TRANSITIONS: dict[str, frozenset] = {
    Job.DRAFT:      frozenset({Job.PUBLISHED}),
    Job.PUBLISHED:  frozenset({Job.PAUSED, Job.CLOSED}),
    Job.PAUSED:     frozenset({Job.PUBLISHED, Job.CLOSED}),
    Job.CLOSED:     frozenset({Job.ARCHIVED}),
    Job.ARCHIVED:   frozenset(),   # terminal — no further transitions
}

# Map: target_status → AuditAction constant
_TRANSITION_AUDIT_ACTION: dict[str, str] = {
    Job.PUBLISHED: AuditAction.JOB_PUBLISHED,
    Job.PAUSED:    AuditAction.JOB_PAUSED,
    Job.CLOSED:    AuditAction.JOB_CLOSED,
    Job.ARCHIVED:  AuditAction.JOB_ARCHIVED,
}

# Editable fields for update_job() — maps payload key → model attribute name.
# status, recruiter_id, views_count, applications_count are intentionally absent.
_UPDATABLE_FIELDS: tuple = (
    "title",
    "description",
    "requirements",
    "responsibilities",
    "location",
    "is_remote",
    "job_type",
    "experience_level",
    "salary_min",
    "salary_max",
    "salary_currency",
    "skills_required",
    "nice_to_have_skills",
    "application_deadline",
)


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _load_job(job_id: str, *, include_deleted: bool = False) -> Job:
    """
    Load a Job by ID string, raising NotFoundError if absent or soft-deleted.

    Args:
        job_id         : UUID string from the URL parameter.
        include_deleted: If True, also returns soft-deleted jobs (admin use).

    Returns:
        The matching Job ORM instance.

    Raises:
        NotFoundError : Job not found, or soft-deleted (unless include_deleted).
        BadRequestError: Malformed UUID string.
    """
    try:
        parsed_id = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        raise BadRequestError(f"Invalid job ID format: '{job_id}'.")

    stmt = select(Job).where(Job.id == parsed_id)
    if not include_deleted:
        stmt = stmt.where(Job.deleted_at.is_(None))

    job: Optional[Job] = db.session.execute(stmt).scalar_one_or_none()

    if job is None:
        raise NotFoundError("Job posting not found.")

    return job


def _assert_owns_job(job: Job, recruiter: User) -> None:
    """
    Raise AuthorizationError if the recruiter does not own this job.

    Only the job's creator (recruiter_id == recruiter.id) or an admin may
    modify the job. This check is applied before any mutation.

    Args:
        job       : The Job ORM instance.
        recruiter : The authenticated user making the request.

    Raises:
        AuthorizationError: The user does not own this job.
    """
    if job.recruiter_id != recruiter.id and not recruiter.is_admin:
        logger.warning(
            "Unauthorized job modification attempt | job_id=%s user_id=%s",
            job.id,
            recruiter.id,
        )
        raise AuthorizationError(
            "You do not have permission to modify this job posting."
        )


def _has_open_applications(job: Job) -> bool:
    """
    Return True if the job has any non-terminal applications.

    Used as a guard before allowing job deletion: deleting a job with active
    applications would abandon candidates mid-process. The recruiter must close
    or reject those applications first.

    Args:
        job: The Job ORM instance.

    Returns:
        True if any active (non-terminal) applications exist for this job.
    """
    from app.models.application import Application, ApplicationStatus

    # Terminal statuses — applications in these states are no longer active
    terminal = [
        ApplicationStatus.OFFER_ACCEPTED.value,
        ApplicationStatus.OFFER_DECLINED.value,
        ApplicationStatus.REJECTED.value,
        ApplicationStatus.WITHDRAWN.value,
    ]

    count: int = db.session.execute(
        select(func.count(Application.id)).where(
            and_(
                Application.job_id == job.id,
                Application.status.notin_(terminal),
            )
        )
    ).scalar_one()

    return count > 0


def _build_sort_clause(sort_by: str, Job: type):
    """
    Return the SQLAlchemy ORDER BY clause for a given sort_by value.

    Args:
        sort_by : One of the JobFilterSchema sort_by choices.
        Job     : The Job model class (passed to avoid circular import at call site).

    Returns:
        A list of SQLAlchemy column expressions to pass to .order_by().
    """
    mapping = {
        "newest":          [Job.created_at.desc()],
        "oldest":          [Job.created_at.asc()],
        "salary_asc":      [Job.salary_min.asc().nullslast(), Job.created_at.desc()],
        "salary_desc":     [Job.salary_max.desc().nullslast(), Job.created_at.desc()],
        "applications_asc":  [Job.applications_count.asc(), Job.created_at.desc()],
        "applications_desc": [Job.applications_count.desc(), Job.created_at.desc()],
    }
    return mapping.get(sort_by, [Job.created_at.desc()])


# ---------------------------------------------------------------------------
# Public Service Functions
# ---------------------------------------------------------------------------


def create_job(
    recruiter: User,
    data: dict,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Create a new job posting in draft status.

    New jobs are always created as DRAFT regardless of any status field
    in the payload (the schema does not accept a status field; this is a
    belt-and-suspenders guard at the service layer).

    Args:
        recruiter  : The authenticated recruiter creating the job.
        data       : Validated payload from CreateJobSchema.load().
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.

    Returns:
        dict: Full job representation from Job.to_dict() for the
              recruiter-facing response.

    Raises:
        AuthorizationError: If the user is not a recruiter or admin.
    """
    if not recruiter.is_recruiter and not recruiter.is_admin:
        raise AuthorizationError("Only recruiters can create job postings.")

    logger.info(
        "Job creation | recruiter_id=%s title=%r",
        recruiter.id,
        data.get("title"),
    )

    job = Job(
        recruiter_id=recruiter.id,
        status=Job.DRAFT,    # always draft on creation
        title=data["title"],
        description=data["description"],
        job_type=data["job_type"],
        requirements=data.get("requirements"),
        responsibilities=data.get("responsibilities"),
        location=data.get("location"),
        is_remote=data.get("is_remote", False),
        experience_level=data.get("experience_level"),
        salary_min=data.get("salary_min"),
        salary_max=data.get("salary_max"),
        salary_currency=data.get("salary_currency", "USD"),
        skills_required=data.get("skills_required") or [],
        nice_to_have_skills=data.get("nice_to_have_skills") or [],
        application_deadline=data.get("application_deadline"),
    )

    db.session.add(job)
    db.session.flush()   # assign job.id before audit log references it

    AuditLog.log(
        action=AuditAction.JOB_CREATED,
        user_id=recruiter.id,
        entity_type="job",
        entity_id=job.id,
        description=(
            f"Recruiter '{recruiter.first_name} {recruiter.last_name}' "
            f"created job '{job.title}' as draft."
        ),
        new_value={"title": job.title, "status": job.status},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Job created | job_id=%s recruiter_id=%s title=%r",
        job.id,
        recruiter.id,
        job.title,
    )

    return job.to_dict()


def get_job(
    job_id: str,
    viewer: Optional[User] = None,
) -> dict:
    """
    Fetch a single job posting with visibility-based serialization.

    Visibility rules:
      - Candidate / unauthenticated: only published, non-deleted jobs.
        Returns job.to_public_dict() (excludes recruiter-only fields).
      - Recruiter (owner): any non-deleted job they own (all statuses).
        Returns job.to_dict() (full recruiter view).
      - Admin: any non-deleted job of any recruiter.
        Returns job.to_dict() (full admin view).

    Args:
        job_id : UUID string from the URL parameter.
        viewer : The authenticated user, or None for unauthenticated access.

    Returns:
        dict: Serialized job (scope depends on viewer role).

    Raises:
        NotFoundError     : Job not found, soft-deleted, or not visible.
        AuthorizationError: Recruiter trying to view another recruiter's job.
        BadRequestError   : Malformed UUID.
    """
    job: Job = _load_job(job_id)

    is_admin = viewer is not None and viewer.is_admin
    is_owner = viewer is not None and job.recruiter_id == viewer.id
    is_recruiter = viewer is not None and viewer.is_recruiter

    # Candidate / unauthenticated — only published jobs
    if not is_admin and not is_owner:
        if not is_recruiter or not is_owner:
            # Non-owner recruiters cannot view other recruiters' jobs
            # via this endpoint; their own jobs they can see at any status.
            if job.status != Job.PUBLISHED:
                raise NotFoundError("Job posting not found.")
            # Track the view asynchronously (atomic increment without ORM load)
            # Note: increment_view() is called by the route, not here, to allow
            # the route to decide whether to count admin views.
            return job.to_public_dict()

    # Owner recruiter or admin — full dict, any status
    return job.to_dict()


def update_job(
    job_id: str,
    recruiter: User,
    data: dict,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Apply a partial update to a job posting.

    Only the fields present and non-None in `data` are applied. Status is
    intentionally not updatable via this function (use transition_status()).
    The job must be in DRAFT or PUBLISHED status to be editable — PAUSED,
    CLOSED, and ARCHIVED jobs cannot be modified to prevent data corruption
    after the AI pipeline has processed them.

    Args:
        job_id     : UUID string identifying the job.
        recruiter  : The authenticated recruiter making the request.
        data       : Validated payload from UpdateJobSchema.load().
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.

    Returns:
        dict: Updated full job representation from Job.to_dict().

    Raises:
        NotFoundError     : Job not found.
        AuthorizationError: Caller does not own this job.
        BadRequestError   : Job is in a non-editable status.
    """
    job: Job = _load_job(job_id)
    _assert_owns_job(job, recruiter)

    # Guard: only allow edits on draft and published jobs
    editable_statuses = (Job.DRAFT, Job.PUBLISHED)
    if job.status not in editable_statuses:
        raise BadRequestError(
            f"Job in '{job.status}' status cannot be edited. "
            "Only draft and published jobs can be updated. "
            "Archive or restore the job to make changes."
        )

    # Capture old values for audit log (only changed fields)
    old_values: dict = {}
    new_values: dict = {}

    for field in _UPDATABLE_FIELDS:
        if field in data and data[field] is not None:
            old_val = getattr(job, field, None)
            new_val = data[field]
            # Only record fields that actually changed
            if old_val != new_val:
                old_values[field] = old_val
                new_values[field] = new_val
            setattr(job, field, new_val)

    if not new_values:
        # Data validated fine but nothing actually changed — return as-is
        logger.info(
            "Job update with no effective changes | job_id=%s", job.id
        )
        return job.to_dict()

    AuditLog.log(
        action=AuditAction.JOB_UPDATED,
        user_id=recruiter.id,
        entity_type="job",
        entity_id=job.id,
        description=(
            f"Recruiter '{recruiter.first_name} {recruiter.last_name}' "
            f"updated job '{job.title}'."
        ),
        old_value=old_values,
        new_value=new_values,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Job updated | job_id=%s recruiter_id=%s fields=%s",
        job.id,
        recruiter.id,
        list(new_values.keys()),
    )

    return job.to_dict()


def transition_status(
    job_id: str,
    target_status: str,
    recruiter: User,
    *,
    note: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Transition a job posting to a new lifecycle status.

    Enforces the 5-state lifecycle graph. Any transition not in
    _VALID_TRANSITIONS raises BadRequestError. When publishing a draft job,
    a minimum viability check is applied: title and description must be
    non-empty (always true since they are required on creation, but acts
    as a safety net if the job was created via direct DB insert).

    Valid transitions:
      draft      → published
      published  → paused | closed
      paused     → published | closed
      closed     → archived
      archived   → (none — terminal state)

    Args:
        job_id        : UUID string identifying the job.
        target_status : The desired new status string (e.g. 'published').
        recruiter     : The authenticated recruiter making the request.
        note          : Optional recruiter note stored in the audit log.
        ip_address    : Client IP for the audit log.
        user_agent    : User-Agent header for the audit log.

    Returns:
        dict: Updated full job representation from Job.to_dict().

    Raises:
        NotFoundError     : Job not found.
        AuthorizationError: Caller does not own this job.
        BadRequestError   : Transition is not permitted from the current status,
                            or the job is already in the target status.
    """
    job: Job = _load_job(job_id)
    _assert_owns_job(job, recruiter)

    current = job.status

    # Idempotency guard
    if current == target_status:
        logger.info(
            "Job transition no-op | job_id=%s status=%s", job.id, current
        )
        return job.to_dict()

    # Lifecycle graph check
    allowed = _VALID_TRANSITIONS.get(current, frozenset())
    if target_status not in allowed:
        allowed_str = ", ".join(sorted(allowed)) if allowed else "none"
        raise BadRequestError(
            f"Cannot transition job from '{current}' to '{target_status}'. "
            f"Allowed transitions from '{current}': {allowed_str}."
        )

    # Minimum viability check when publishing
    if target_status == Job.PUBLISHED:
        if not job.title or not job.title.strip():
            raise BadRequestError(
                "Cannot publish a job with an empty title."
            )
        if not job.description or not job.description.strip():
            raise BadRequestError(
                "Cannot publish a job with an empty description."
            )

    # Apply the transition
    old_status = job.status
    job.status = target_status

    audit_action = _TRANSITION_AUDIT_ACTION.get(
        target_status, AuditAction.JOB_UPDATED
    )

    description = (
        f"Recruiter '{recruiter.first_name} {recruiter.last_name}' "
        f"transitioned job '{job.title}' from '{old_status}' to '{target_status}'."
    )
    if note:
        description += f" Note: {note}"

    AuditLog.log(
        action=audit_action,
        user_id=recruiter.id,
        entity_type="job",
        entity_id=job.id,
        description=description,
        old_value={"status": old_status},
        new_value={"status": target_status, "note": note},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Job status transition | job_id=%s %s → %s recruiter_id=%s",
        job.id,
        old_status,
        target_status,
        recruiter.id,
    )

    return job.to_dict()


def delete_job(
    job_id: str,
    recruiter: User,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Soft-delete a job posting.

    Sets deleted_at to the current UTC time. Soft-deleted jobs are excluded
    from all normal queries but their data is preserved for audit purposes.

    Guard: A job with active (non-terminal) applications cannot be deleted.
    The recruiter must first close or reject those applications. This prevents
    abandoning candidates who are mid-process.

    Jobs in PUBLISHED status are auto-transitioned to CLOSED before deletion
    to prevent candidates from seeing the job after it is soft-deleted
    (the is_deleted check in Job.is_published handles this, but CLOSED is
    set for data integrity / human readability of the audit trail).

    Args:
        job_id     : UUID string identifying the job.
        recruiter  : The authenticated recruiter making the request.
        ip_address : Client IP for the audit log.
        user_agent : User-Agent header for the audit log.

    Returns:
        None — caller should return HTTP 200 with a success message.

    Raises:
        NotFoundError     : Job not found.
        AuthorizationError: Caller does not own this job.
        BadRequestError   : Job has open applications and cannot be deleted.
    """
    job: Job = _load_job(job_id)
    _assert_owns_job(job, recruiter)

    # Guard: block deletion if active applications exist
    if _has_open_applications(job):
        raise BadRequestError(
            f"Job '{job.title}' has active applications and cannot be deleted. "
            "Please close or reject all pending applications first."
        )

    # Auto-close published/paused jobs before deletion for data integrity
    auto_closed = False
    if job.status in (Job.PUBLISHED, Job.PAUSED):
        job.status = Job.CLOSED
        auto_closed = True

    job.soft_delete()

    AuditLog.log(
        action=AuditAction.JOB_DELETED,
        user_id=recruiter.id,
        entity_type="job",
        entity_id=job.id,
        description=(
            f"Recruiter '{recruiter.first_name} {recruiter.last_name}' "
            f"deleted job '{job.title}'"
            + (" (auto-closed before deletion)." if auto_closed else ".")
        ),
        old_value={"status": job.status, "title": job.title},
        new_value={"deleted": True},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    db.session.commit()

    logger.info(
        "Job soft-deleted | job_id=%s recruiter_id=%s title=%r",
        job.id,
        recruiter.id,
        job.title,
    )


def list_recruiter_jobs(
    recruiter: User,
    filters: dict,
) -> dict:
    """
    Return a paginated list of a recruiter's own job postings.

    Returns jobs at any status (draft, published, paused, closed, archived)
    that belong to this recruiter. Soft-deleted jobs are excluded.
    Uses the validated filter dict from JobFilterSchema.

    Args:
        recruiter : The authenticated recruiter.
        filters   : Validated payload from JobFilterSchema.load().

    Returns:
        dict with keys:
          jobs        : list of job.to_dict() dicts for this page
          pagination  : page, per_page, total, total_pages, has_next, has_prev
    """
    page: int = filters.get("page", 1)
    per_page: int = filters.get("per_page", 20)
    sort_by: str = filters.get("sort_by", "newest")
    status_filter: Optional[str] = filters.get("status")   # None = all statuses

    # Base query: recruiter's own jobs, not deleted
    stmt = select(Job).where(
        and_(
            Job.recruiter_id == recruiter.id,
            Job.deleted_at.is_(None),
        )
    )

    # Optional status filter (recruiter dashboard can filter by status)
    # When status == 'published' (the schema default), we still return all statuses
    # because the recruiter's /my endpoint should show everything. Only apply the
    # filter if it is not the default 'published' value, unless explicitly set.
    # The route is responsible for passing the raw filter dict; the schema default
    # of 'published' is appropriate for candidate search but not for recruiter list.
    # The route handler for /my will pass status=None to get all statuses.
    if status_filter and status_filter != "all":
        stmt = stmt.where(Job.status == status_filter)

    # Apply job_type filter if provided
    job_type = filters.get("job_type")
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)

    # Apply experience_level filter if provided
    exp_level = filters.get("experience_level")
    if exp_level:
        stmt = stmt.where(Job.experience_level == exp_level)

    # Apply is_remote filter
    is_remote = filters.get("is_remote")
    if is_remote is not None:
        stmt = stmt.where(Job.is_remote == is_remote)

    # Apply free-text search on title (ILIKE)
    search = filters.get("search")
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Job.title.ilike(pattern),
                Job.description.ilike(pattern),
            )
        )

    # Count total for pagination (before LIMIT/OFFSET)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = db.session.execute(count_stmt).scalar_one()

    # Apply sort and pagination
    order_clauses = _build_sort_clause(sort_by, Job)
    stmt = stmt.order_by(*order_clauses)
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)

    jobs = db.session.execute(stmt).scalars().all()

    total_pages = max(1, (total + per_page - 1) // per_page)

    logger.info(
        "Recruiter jobs listed | recruiter_id=%s total=%d page=%d/%d",
        recruiter.id,
        total,
        page,
        total_pages,
    )

    return {
        "jobs": [j.to_dict() for j in jobs],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }


def search_jobs(filters: dict) -> dict:
    """
    Candidate-facing paginated public job search.

    Returns only PUBLISHED, non-deleted jobs. Supports filtering by job type,
    experience level, remote flag, location (partial ILIKE), required skills
    (ALL skills must be present in skills_required), salary range, and
    free-text search against title + description.

    Args:
        filters : Validated payload from JobFilterSchema.load(). The `status`
                  field from the filter is ignored here — only published jobs
                  are ever returned to candidates.

    Returns:
        dict with keys:
          jobs        : list of job.to_public_dict() dicts for this page
          pagination  : page, per_page, total, total_pages, has_next, has_prev
    """
    page: int = filters.get("page", 1)
    per_page: int = filters.get("per_page", 20)
    sort_by: str = filters.get("sort_by", "newest")

    # Base: only published, non-deleted jobs (candidate search — never draft/paused)
    stmt = select(Job).where(
        and_(
            Job.status == Job.PUBLISHED,
            Job.deleted_at.is_(None),
        )
    )

    # Job type filter
    job_type = filters.get("job_type")
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)

    # Experience level filter
    exp_level = filters.get("experience_level")
    if exp_level:
        stmt = stmt.where(Job.experience_level == exp_level)

    # Remote filter
    is_remote = filters.get("is_remote")
    if is_remote is not None:
        stmt = stmt.where(Job.is_remote == is_remote)

    # Location partial match (case-insensitive)
    location = filters.get("location")
    if location:
        stmt = stmt.where(Job.location.ilike(f"%{location}%"))

    # Skills filter — ALL listed skills must appear in skills_required
    # Uses PostgreSQL JSONB @> (contains) operator for efficient GIN index usage.
    skills = filters.get("skills")
    if skills:
        import json as _json
        stmt = stmt.where(
            Job.skills_required.op("@>")(
                func.cast(_json.dumps(skills), db.Text)
            )
        )

    # Salary range filter — at least one salary bound overlaps the filter range
    salary_min_filter = filters.get("salary_min")
    salary_max_filter = filters.get("salary_max")
    if salary_min_filter is not None:
        # Job's max salary must be >= the requested minimum (or undisclosed)
        stmt = stmt.where(
            or_(
                Job.salary_max >= salary_min_filter,
                Job.salary_max.is_(None),
            )
        )
    if salary_max_filter is not None:
        # Job's min salary must be <= the requested maximum (or undisclosed)
        stmt = stmt.where(
            or_(
                Job.salary_min <= salary_max_filter,
                Job.salary_min.is_(None),
            )
        )

    # Currency filter (only meaningful when salary range is also specified)
    salary_currency = filters.get("salary_currency")
    if salary_currency:
        stmt = stmt.where(Job.salary_currency == salary_currency)

    # Free-text search — title and description (ILIKE, case-insensitive)
    search = filters.get("search")
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Job.title.ilike(pattern),
                Job.description.ilike(pattern),
            )
        )

    # Deadline filter: exclude jobs whose deadline has already passed
    # (A published job past its deadline should not appear in search results)
    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc)
    stmt = stmt.where(
        or_(
            Job.application_deadline.is_(None),
            Job.application_deadline > now,
        )
    )

    # Count total
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = db.session.execute(count_stmt).scalar_one()

    # Sort and paginate
    order_clauses = _build_sort_clause(sort_by, Job)
    stmt = stmt.order_by(*order_clauses)
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)

    jobs = db.session.execute(stmt).scalars().all()

    total_pages = max(1, (total + per_page - 1) // per_page)

    logger.info(
        "Job search | total=%d page=%d/%d filters=%s",
        total,
        page,
        total_pages,
        {k: v for k, v in filters.items() if v is not None and k not in ("page", "per_page")},
    )

    return {
        "jobs": [j.to_public_dict() for j in jobs],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }


def increment_view(job_id: str) -> None:
    """
    Atomically increment the views_count for a job posting.

    Uses a direct SQL UPDATE (not an ORM load) to avoid a SELECT + UPDATE
    round-trip and to eliminate the possibility of a race condition between
    two concurrent requests both reading views_count=5 and both writing 6.

    This function is intentionally silent on errors — if the job no longer
    exists (race between view and delete), the UPDATE affects 0 rows and
    no exception is raised. The view count is a best-effort metric.

    The update is committed immediately and independently of any surrounding
    transaction so that a page view is recorded even if the surrounding
    request subsequently fails.

    Args:
        job_id : UUID string identifying the job. Silently no-ops for
                 invalid or non-existent IDs.
    """
    try:
        parsed_id = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return  # invalid ID — silently ignore

    db.session.execute(
        update(Job)
        .where(
            and_(
                Job.id == parsed_id,
                Job.deleted_at.is_(None),
            )
        )
        .values(views_count=Job.views_count + 1)
    )
    db.session.commit()


def get_job_stats(job_id: str, recruiter: User) -> dict:
    """
    Return application count breakdown by status for a recruiter's job.

    Provides a lightweight stats object for the recruiter dashboard card —
    how many applicants are at each stage. Does not load all applications;
    uses a GROUP BY aggregate query for efficiency.

    Args:
        job_id    : UUID string identifying the job.
        recruiter : The authenticated recruiter requesting the stats.

    Returns:
        dict with keys:
          job_id              : str UUID
          title               : str job title
          status              : str current job status
          total_applications  : int  total application count
          by_status           : dict mapping status → count
          views_count         : int  total view count
          applications_count  : int  denormalised counter (for quick display)

    Raises:
        NotFoundError     : Job not found.
        AuthorizationError: Caller does not own this job.
        BadRequestError   : Malformed UUID.
    """
    from app.models.application import Application

    job: Job = _load_job(job_id)
    _assert_owns_job(job, recruiter)

    # GROUP BY aggregate — single query for all status counts
    rows = db.session.execute(
        select(Application.status, func.count(Application.id))
        .where(Application.job_id == job.id)
        .group_by(Application.status)
    ).all()

    by_status: dict = {row[0]: row[1] for row in rows}
    total: int = sum(by_status.values())

    logger.info(
        "Job stats | job_id=%s total_applications=%d",
        job.id,
        total,
    )

    return {
        "job_id": str(job.id),
        "title": job.title,
        "status": job.status,
        "status_label": job.status_label,
        "total_applications": total,
        "by_status": by_status,
        "views_count": job.views_count,
        "applications_count": job.applications_count,
        "is_accepting_applications": job.is_accepting_applications,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }

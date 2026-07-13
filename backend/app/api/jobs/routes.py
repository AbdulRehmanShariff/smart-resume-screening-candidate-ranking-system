"""
api/jobs/routes.py
------------------
Jobs Blueprint — route handlers for the Smart Resume Screening System.

Thin route layer: parse input → validate schema → call job_service → return response.
All business logic lives in app/services/job_service.py.

Registered endpoints:

  Candidate-facing (authentication optional):
    GET  /api/v1/jobs                  — Public job search (published only)
    GET  /api/v1/jobs/<job_id>         — Fetch a single job (visibility-aware)

  Recruiter-only (recruiter_required):
    POST   /api/v1/jobs                     — Create a new draft job posting
    GET    /api/v1/jobs/my                  — Recruiter's own jobs (all statuses)
    PATCH  /api/v1/jobs/<job_id>           — Partial update of a job
    DELETE /api/v1/jobs/<job_id>           — Soft-delete a job
    POST   /api/v1/jobs/<job_id>/publish    — Transition → published
    POST   /api/v1/jobs/<job_id>/pause      — Transition → paused
    POST   /api/v1/jobs/<job_id>/close      — Transition → closed
    POST   /api/v1/jobs/<job_id>/archive    — Transition → archived

  Recruiter-only stats:
    GET    /api/v1/jobs/<job_id>/stats      — Application breakdown by status

Route handler contract:
  1. Parse JSON body or query-string using the appropriate helper.
  2. Validate with the matching Marshmallow schema.
  3. Call the service function with validated data + request context.
  4. Return the standard response envelope from app.core.responses.
  5. Never raise — all AppException subclasses are caught by the global
     error handler in app/__init__.py.

Visibility design for GET /jobs and GET /jobs/<job_id>:
  - Unauthenticated requests and candidates only see PUBLISHED jobs.
  - Recruiters see their own jobs at any status via /jobs/my.
  - GET /jobs/<job_id> performs an optional JWT decode — if no valid
    token is present the request is treated as unauthenticated (public)
    so that job detail pages work without login.

View counting design:
  - increment_view() is called inside GET /jobs/<job_id> after the
    service returns the job data.
  - It is NOT called for recruiter/admin self-views to avoid polluting
    the metric, and NOT called for the /my list endpoint.
  - It commits its own transaction independently so a view is recorded
    even if subsequent code fails.
"""

import logging

from flask import Blueprint, request
from flask_jwt_extended import verify_jwt_in_request
from marshmallow import ValidationError

from app.core.decorators import get_current_user, recruiter_required
from app.core.responses import (
    created_response,
    success_response,
    validation_error_response,
)
from app.schemas.job import (
    CreateJobSchema,
    JobFilterSchema,
    JobStatusTransitionSchema,
    UpdateJobSchema,
)
from app.services import job_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

jobs_bp = Blueprint("jobs", __name__)

# ---------------------------------------------------------------------------
# Schema instances (module-level — Marshmallow schemas are thread-safe)
# ---------------------------------------------------------------------------

_create_schema = CreateJobSchema()
_update_schema = UpdateJobSchema()
_filter_schema = JobFilterSchema()
_transition_schema = JobStatusTransitionSchema()


# ---------------------------------------------------------------------------
# Request Helpers
# ---------------------------------------------------------------------------


def _get_json() -> dict:
    """
    Parse the request body as JSON.

    Returns an empty dict if the body is absent or not valid JSON,
    allowing the schema to surface field-level 'required' errors
    rather than a raw 400.
    """
    return request.get_json(silent=True) or {}


def _get_query_args() -> dict:
    """
    Convert Flask's ImmutableMultiDict query args to a plain dict.

    For multi-value keys (e.g. ?skills=Python&skills=Docker), preserves
    the full list so that JobFilterSchema.skills can receive a list.
    Single-value keys are kept as scalars (not wrapped in a list) to match
    what Marshmallow's non-List fields expect.
    """
    multi_keys = {"skills"}   # fields that accept multiple values
    result = {}
    for key in request.args:
        if key in multi_keys:
            result[key] = request.args.getlist(key)
        else:
            result[key] = request.args.get(key)
    return result


def _ctx() -> dict:
    """Return common request-context kwargs for service calls."""
    return {
        "ip_address": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
    }


def _optional_current_user():
    """
    Attempt to load the current user from a JWT without requiring one.

    Used by routes where authentication is optional (e.g. public job search,
    single-job view). Returns the User instance if a valid JWT is present,
    or None for unauthenticated requests.

    This intentionally swallows all JWT errors — an invalid or absent token
    on a public endpoint is treated the same as unauthenticated access.
    """
    try:
        verify_jwt_in_request(optional=True)
        from app.core.decorators import _load_user_from_jwt  # noqa: PLC0415
        return _load_user_from_jwt()
    except Exception:  # noqa: BLE001 — catch all JWT/runtime errors
        return None


# ---------------------------------------------------------------------------
# Batch 3C Routes
# ---------------------------------------------------------------------------


@jobs_bp.post("/")
@recruiter_required
def create_job():
    """
    Create a new job posting in DRAFT status.

    Only recruiters may create job postings. The job is always created as
    DRAFT — use the /publish endpoint to make it visible to candidates.

    Request body (JSON):
      title               : string, required — 1–255 chars
      description         : string, required — 1–50,000 chars
      job_type            : string, required — full_time | part_time | contract
                                               | internship | freelance
      requirements        : string, optional
      responsibilities    : string, optional
      location            : string, optional
      is_remote           : bool, optional — default false
      experience_level    : string, optional — intern|fresher|junior|mid|senior|lead
      salary_min          : float, optional
      salary_max          : float, optional — must be >= salary_min
      salary_currency     : string, optional — ISO 4217, default "USD"
      skills_required     : list[string], optional
      nice_to_have_skills : list[string], optional
      application_deadline: ISO 8601 datetime, optional — must be future

    Responses:
      201 Created           — Job created in draft status
      401 Unauthorized      — Missing or invalid JWT
      403 Forbidden         — User is not a recruiter
      422 Unprocessable     — Validation errors
    """
    recruiter = get_current_user()
    raw: dict = _get_json()

    try:
        data: dict = _create_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "Job creation validation failed | recruiter_id=%s | errors=%s",
            recruiter.id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    job_data: dict = job_service.create_job(recruiter, data, **_ctx())

    return created_response(
        message="Job posting created successfully in draft status.",
        data=job_data,
    )


@jobs_bp.get("/")
def search_jobs():
    """
    Candidate-facing public job search.

    Returns paginated PUBLISHED jobs only. Unauthenticated access is
    fully supported — no JWT required. Supports filtering by job type,
    experience level, remote flag, location, skills, salary range, and
    free-text search.

    Query parameters:
      job_type          : string — full_time|part_time|contract|internship|freelance
      experience_level  : string — intern|fresher|junior|mid|senior|lead
      is_remote         : bool
      location          : string — partial ILIKE match
      skills            : string (repeatable) — ?skills=Python&skills=Flask
      salary_min        : float
      salary_max        : float
      salary_currency   : string — ISO 4217
      search            : string — full-text search on title + description
      sort_by           : string — newest|oldest|salary_asc|salary_desc
                                   |applications_asc|applications_desc
      page              : int, default 1
      per_page          : int, default 20, max 100

    Responses:
      200 OK        — Paginated list of published jobs
      422 Unprocessable — Invalid filter parameters
    """
    raw: dict = _get_query_args()

    # Override status: public search always returns published jobs only.
    # Prevent a malicious caller from passing ?status=draft to probe drafts.
    raw["status"] = "published"

    try:
        filters: dict = _filter_schema.load(raw)
    except ValidationError as exc:
        logger.debug("Job search filter validation failed | errors=%s", exc.messages)
        return validation_error_response(exc.messages)

    result: dict = job_service.search_jobs(filters)

    return success_response(
        message="Jobs retrieved successfully.",
        data=result["jobs"],
        meta={"pagination": result["pagination"]},
    )


@jobs_bp.get("/my")
@recruiter_required
def list_my_jobs():
    """
    Return the authenticated recruiter's own job postings (all statuses).

    Supports optional filtering/sorting via query parameters. Unlike the
    public /jobs search, this endpoint shows draft, paused, closed, and
    archived jobs too — not just published ones.

    Query parameters:
      status            : string — filter by specific status (optional; omit for all)
      job_type          : string
      experience_level  : string
      is_remote         : bool
      search            : string — ILIKE on title + description
      sort_by           : string — newest|oldest|salary_asc|salary_desc
                                   |applications_asc|applications_desc
      page              : int, default 1
      per_page          : int, default 20, max 100

    Responses:
      200 OK           — Paginated list of recruiter's jobs
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not a recruiter
      422 Unprocessable — Invalid filter parameters
    """
    recruiter = get_current_user()
    raw: dict = _get_query_args()

    # For the recruiter's own list, remove the status default so the service
    # returns all statuses unless the recruiter explicitly filters.
    # The JobFilterSchema default of 'published' is appropriate for public search
    # but not here — a recruiter wants to see all their jobs by default.
    raw.pop("status", None)

    try:
        filters: dict = _filter_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "Recruiter job list filter validation failed | recruiter_id=%s | errors=%s",
            recruiter.id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    # Signal "all statuses" to the service by removing the schema-default status
    filters.pop("status", None)
    filters["status"] = None   # service treats None as "no status filter"

    result: dict = job_service.list_recruiter_jobs(recruiter, filters)

    return success_response(
        message="Your job postings retrieved successfully.",
        data=result["jobs"],
        meta={"pagination": result["pagination"]},
    )


@jobs_bp.get("/<job_id>")
def get_job(job_id: str):
    """
    Fetch a single job posting.

    Visibility rules (enforced in job_service.get_job):
      - Unauthenticated / candidate : only PUBLISHED jobs visible
      - Recruiter (owner)           : any status visible
      - Admin                       : any status visible

    For candidates and unauthenticated visitors, the view counter is
    incremented atomically after the job is retrieved. Recruiter
    self-views and admin views do NOT increment the counter.

    Path parameters:
      job_id : UUID of the job posting

    Responses:
      200 OK           — Job data returned
      400 Bad Request  — Malformed job_id UUID
      404 Not Found    — Job not found, soft-deleted, or not visible
    """
    viewer = _optional_current_user()

    job_data: dict = job_service.get_job(job_id, viewer)

    # Increment view counter for public (non-owner, non-admin) access
    is_owner = viewer is not None and str(viewer.id) == job_data.get("recruiter_id")
    is_admin = viewer is not None and viewer.is_admin
    if not is_owner and not is_admin:
        job_service.increment_view(job_id)

    return success_response(
        message="Job retrieved successfully.",
        data=job_data,
    )


@jobs_bp.patch("/<job_id>")
@recruiter_required
def update_job(job_id: str):
    """
    Partially update a job posting.

    Only the job owner (recruiter) may update a job. All fields are optional;
    only provided non-None values are applied. Status is not updatable via
    this endpoint — use the transition endpoints instead.

    Jobs in PAUSED, CLOSED, or ARCHIVED status cannot be updated.

    Path parameters:
      job_id : UUID of the job posting

    Request body (JSON — all optional):
      title, description, requirements, responsibilities, location,
      is_remote, job_type, experience_level, salary_min, salary_max,
      salary_currency, skills_required, nice_to_have_skills,
      application_deadline

    Responses:
      200 OK           — Job updated successfully
      400 Bad Request  — Job is in a non-editable status, or malformed UUID
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this job
      404 Not Found    — Job not found
      422 Unprocessable — Validation errors
    """
    recruiter = get_current_user()
    raw: dict = _get_json()

    try:
        data: dict = _update_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "Job update validation failed | job_id=%s | recruiter_id=%s | errors=%s",
            job_id,
            recruiter.id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    job_data: dict = job_service.update_job(job_id, recruiter, data, **_ctx())

    return success_response(
        message="Job posting updated successfully.",
        data=job_data,
    )


@jobs_bp.delete("/<job_id>")
@recruiter_required
def delete_job(job_id: str):
    """
    Soft-delete a job posting.

    Sets deleted_at on the job. Deleted jobs are excluded from all normal
    queries but their data is preserved for audit purposes. This action
    is irreversible via the API (no restore endpoint in this stage).

    Guard: Jobs with active (non-terminal) applications cannot be deleted.
    The recruiter must close or reject those applications first.

    Path parameters:
      job_id : UUID of the job posting

    Responses:
      200 OK           — Job deleted successfully
      400 Bad Request  — Job has open applications, or malformed UUID
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this job
      404 Not Found    — Job not found
    """
    recruiter = get_current_user()

    job_service.delete_job(job_id, recruiter, **_ctx())

    return success_response(
        message="Job posting deleted successfully.",
        data=None,
    )


# ---------------------------------------------------------------------------
# Status Transition Endpoints
# ---------------------------------------------------------------------------


@jobs_bp.post("/<job_id>/publish")
@recruiter_required
def publish_job(job_id: str):
    """
    Transition a job from DRAFT or PAUSED to PUBLISHED.

    Published jobs are visible to candidates in public job search.

    Valid from: draft, paused
    Invalid from: published (no-op guard), closed, archived

    Request body (JSON — all optional):
      note : string — optional recruiter note recorded in the audit log

    Responses:
      200 OK           — Job is now published
      400 Bad Request  — Transition not allowed from current status
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this job
      404 Not Found    — Job not found
      422 Unprocessable — Validation errors on note field
    """
    recruiter = get_current_user()
    raw: dict = _get_json()

    try:
        params: dict = _transition_schema.load(raw)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    job_data: dict = job_service.transition_status(
        job_id,
        target_status="published",
        recruiter=recruiter,
        note=params.get("note"),
        **_ctx(),
    )

    return success_response(
        message="Job posting is now published and visible to candidates.",
        data=job_data,
    )


@jobs_bp.post("/<job_id>/pause")
@recruiter_required
def pause_job(job_id: str):
    """
    Transition a job from PUBLISHED to PAUSED.

    Paused jobs are hidden from candidate search but can be re-published
    without losing their application history.

    Valid from: published
    Invalid from: draft, paused (no-op guard), closed, archived

    Request body (JSON — all optional):
      note : string — optional recruiter note recorded in the audit log

    Responses:
      200 OK           — Job is now paused
      400 Bad Request  — Transition not allowed from current status
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this job
      404 Not Found    — Job not found
      422 Unprocessable — Validation errors on note field
    """
    recruiter = get_current_user()
    raw: dict = _get_json()

    try:
        params: dict = _transition_schema.load(raw)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    job_data: dict = job_service.transition_status(
        job_id,
        target_status="paused",
        recruiter=recruiter,
        note=params.get("note"),
        **_ctx(),
    )

    return success_response(
        message="Job posting is now paused. It is no longer visible to candidates.",
        data=job_data,
    )


@jobs_bp.post("/<job_id>/close")
@recruiter_required
def close_job(job_id: str):
    """
    Transition a job from PUBLISHED or PAUSED to CLOSED.

    Closed jobs no longer accept applications. Existing applications are
    preserved for recruiter review. Closed jobs cannot be re-published
    (archive first if needed).

    Valid from: published, paused
    Invalid from: draft, closed (no-op guard), archived

    Request body (JSON — all optional):
      note : string — optional recruiter note recorded in the audit log

    Responses:
      200 OK           — Job is now closed
      400 Bad Request  — Transition not allowed from current status
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this job
      404 Not Found    — Job not found
      422 Unprocessable — Validation errors on note field
    """
    recruiter = get_current_user()
    raw: dict = _get_json()

    try:
        params: dict = _transition_schema.load(raw)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    job_data: dict = job_service.transition_status(
        job_id,
        target_status="closed",
        recruiter=recruiter,
        note=params.get("note"),
        **_ctx(),
    )

    return success_response(
        message="Job posting is now closed. No further applications will be accepted.",
        data=job_data,
    )


@jobs_bp.post("/<job_id>/archive")
@recruiter_required
def archive_job(job_id: str):
    """
    Transition a job from CLOSED to ARCHIVED.

    Archived jobs are fully retired and serve as a historical record.
    This is a terminal state — no further status transitions are allowed.

    Valid from: closed
    Invalid from: draft, published, paused, archived (no-op guard)

    Request body (JSON — all optional):
      note : string — optional recruiter note recorded in the audit log

    Responses:
      200 OK           — Job is now archived
      400 Bad Request  — Transition not allowed from current status
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this job
      404 Not Found    — Job not found
      422 Unprocessable — Validation errors on note field
    """
    recruiter = get_current_user()
    raw: dict = _get_json()

    try:
        params: dict = _transition_schema.load(raw)
    except ValidationError as exc:
        return validation_error_response(exc.messages)

    job_data: dict = job_service.transition_status(
        job_id,
        target_status="archived",
        recruiter=recruiter,
        note=params.get("note"),
        **_ctx(),
    )

    return success_response(
        message="Job posting has been archived.",
        data=job_data,
    )


@jobs_bp.get("/<job_id>/stats")
@recruiter_required
def get_job_stats(job_id: str):
    """
    Return application count breakdown by status for a recruiter's job.

    Provides a lightweight aggregated summary for the recruiter dashboard
    card — how many applicants are at each stage. Uses a single GROUP BY
    query rather than loading all applications.

    Path parameters:
      job_id : UUID of the job posting

    Responses:
      200 OK           — Stats returned successfully
      400 Bad Request  — Malformed UUID
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this job
      404 Not Found    — Job not found

    Response data:
      job_id             : str UUID
      title              : str
      status             : str current status
      status_label       : str human-readable status
      total_applications : int
      by_status          : dict { status_string: count }
      views_count        : int
      applications_count : int
      is_accepting_applications: bool
      created_at         : ISO 8601 datetime
    """
    recruiter = get_current_user()

    stats: dict = job_service.get_job_stats(job_id, recruiter)

    return success_response(
        message="Job statistics retrieved successfully.",
        data=stats,
    )

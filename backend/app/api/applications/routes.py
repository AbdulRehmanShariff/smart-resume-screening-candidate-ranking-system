"""
api/applications/routes.py
--------------------------
Applications Blueprint — route handlers for the Smart Resume Screening System.

Thin route layer: parse input → validate → call application_service → return response.
All business logic lives in app/services/application_service.py.

Registered endpoints:
  POST   /api/v1/applications/                           — Submit application (candidate)
  GET    /api/v1/applications/                           — List own applications (candidate)
  GET    /api/v1/applications/<application_id>           — Get single application
  POST   /api/v1/applications/<application_id>/withdraw  — Withdraw application (candidate)
  PATCH  /api/v1/applications/<application_id>/status    — Update status (recruiter)
  PATCH  /api/v1/applications/<application_id>/notes     — Update private notes (recruiter)

Route handler contract:
  1. Authenticate via decorator (@candidate_required / @recruiter_required / @jwt_required_user).
  2. Parse JSON body or query-string args.
  3. Validate with the matching Marshmallow schema.
  4. Call the service function with validated data + request context.
  5. Return the standard response envelope from app.core.responses.
  6. Never raise — AppException subclasses are caught by the global error handler.

Role-awareness:
  - Candidate endpoints use @candidate_required.
  - Recruiter endpoints use @recruiter_required.
  - GET /<application_id> uses @jwt_required_user (both roles valid — service
    applies role-aware serialization: to_candidate_dict() vs to_recruiter_dict()).
"""

import logging

from flask import Blueprint, request
from marshmallow import ValidationError

from app.core.decorators import (
    candidate_required,
    get_current_user,
    jwt_required_user,
    recruiter_required,
)
from app.core.responses import (
    created_response,
    success_response,
    validation_error_response,
)
from app.schemas.application import (
    ApplicationCreateSchema,
    ApplicationListFilterSchema,
    ApplicationStatusUpdateSchema,
    RecruiterNotesSchema,
)
from app.services import application_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

applications_bp = Blueprint("applications", __name__)


# ---------------------------------------------------------------------------
# Schema instances (module-level — Marshmallow schemas are thread-safe)
# ---------------------------------------------------------------------------

_create_schema = ApplicationCreateSchema()
_list_filter_schema = ApplicationListFilterSchema()
_status_schema = ApplicationStatusUpdateSchema()
_notes_schema = RecruiterNotesSchema()


# ---------------------------------------------------------------------------
# Request Helpers (same pattern as resumes/routes.py and auth/routes.py)
# ---------------------------------------------------------------------------


def _get_json() -> dict:
    """Parse request body as JSON. Returns {} on absent or invalid JSON."""
    return request.get_json(silent=True) or {}


def _get_query_args() -> dict:
    """Convert Flask query-string args to a plain dict."""
    return {key: request.args.get(key) for key in request.args}


def _ctx() -> dict:
    """Return ip_address and user_agent kwargs for service audit log calls."""
    return {
        "ip_address": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
    }


# ---------------------------------------------------------------------------
# Batch 5C Routes — Candidate
# ---------------------------------------------------------------------------


@applications_bp.post("/")
@candidate_required
def submit_application():
    """
    Submit a new job application.

    The candidate selects a published job and one of their own resumes.
    The service creates the Application record and enqueues an AI ranking
    job atomically. The API returns 201 immediately; scoring is async.

    Request body (JSON):
      job_id    : UUID, required — the published job to apply for
      resume_id : UUID, required — the candidate's own resume to submit

    Responses:
      201 Created       — Application submitted; AI ranking queued
      400 Bad Request   — Job not published, or resume not owned by candidate
      401 Unauthorized  — Missing or invalid JWT
      403 Forbidden     — User is not a candidate
      404 Not Found     — Job or resume not found
      409 Conflict      — Already applied for this job
      422 Unprocessable — Validation errors on request body
    """
    candidate = get_current_user()
    raw = _get_json()

    try:
        data = _create_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "Application create validation failed | candidate_id=%s | errors=%s",
            candidate.id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    application = application_service.apply_for_job(candidate, data, **_ctx())

    return created_response(
        message="Application submitted successfully. AI scoring has been queued.",
        data=application,
    )


@applications_bp.get("/")
@candidate_required
def list_my_applications():
    """
    Return the authenticated candidate's own application history.

    Paginated and filterable by status. Returns candidate-safe serialization
    (excludes recruiter-private fields: notes, AI scores, interview questions).

    Query parameters:
      status   : string — filter by application status
      sort_by  : string — newest|oldest|score_desc|score_asc|status (default: newest)
      page     : int    — default 1
      per_page : int    — default 20, max 100

    Responses:
      200 OK           — Paginated application list
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not a candidate
      422 Unprocessable — Invalid filter parameters
    """
    candidate = get_current_user()
    raw = _get_query_args()

    try:
        filters = _list_filter_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "Application list filter validation failed | candidate_id=%s | errors=%s",
            candidate.id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    result = application_service.list_candidate_applications(candidate, filters)

    return success_response(
        message="Applications retrieved successfully.",
        data=result["applications"],
        meta={"pagination": result["pagination"]},
    )


@applications_bp.get("/<application_id>")
@jwt_required_user
def get_application(application_id: str):
    """
    Fetch a single application by ID.

    Role-aware serialization:
      - Candidate (owner): candidate-safe dict (no AI scores or recruiter notes).
      - Recruiter (job owner): full recruiter dict (AI scores, skill gap, notes).
      - Admin: complete dict including all fields.

    Path parameters:
      application_id : UUID of the application

    Responses:
      200 OK           — Application data returned
      400 Bad Request  — Malformed UUID
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — Viewer is neither the applicant nor the job's recruiter
      404 Not Found    — Application not found
    """
    viewer = get_current_user()

    application = application_service.get_application(application_id, viewer)

    return success_response(
        message="Application retrieved successfully.",
        data=application,
    )


@applications_bp.post("/<application_id>/withdraw")
@candidate_required
def withdraw_application(application_id: str):
    """
    Withdraw an active application.

    Cannot withdraw an application that is already in a terminal state
    (offer_accepted, offer_declined, rejected, or already withdrawn).

    Path parameters:
      application_id : UUID of the application to withdraw

    Responses:
      200 OK           — Application withdrawn (status = withdrawn)
      400 Bad Request  — Application is already in a terminal state
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User did not submit this application
      404 Not Found    — Application not found
    """
    candidate = get_current_user()

    application = application_service.withdraw_application(
        application_id, candidate, **_ctx()
    )

    return success_response(
        message="Application withdrawn successfully.",
        data=application,
    )


# ---------------------------------------------------------------------------
# Batch 5C Routes — Recruiter
# ---------------------------------------------------------------------------


@applications_bp.patch("/<application_id>/status")
@recruiter_required
def update_application_status(application_id: str):
    """
    Advance an application through the hiring lifecycle.

    Enforces the 13-state lifecycle graph. Only the job's owner recruiter
    may transition application statuses. Idempotent: if the application is
    already in the requested status, returns 200 with no DB write.

    Path parameters:
      application_id : UUID of the application

    Request body (JSON):
      status : string, required — target status value

    Valid target statuses (recruiter-initiated):
      screening, assessment_pending, assessment_completed, shortlisted,
      technical_interview, hr_interview, final_interview,
      offer_sent, offer_accepted, offer_declined, rejected

    Responses:
      200 OK           — Status updated (or already in target status)
      400 Bad Request  — Transition not permitted from the current state
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — Recruiter does not own this job
      404 Not Found    — Application not found
      422 Unprocessable — Invalid or missing status value
    """
    recruiter = get_current_user()
    raw = _get_json()

    try:
        data = _status_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "Application status update validation failed | "
            "application_id=%s | errors=%s",
            application_id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    application = application_service.update_status(
        application_id,
        data["status"],
        recruiter,
        **_ctx(),
    )

    return success_response(
        message="Application status updated successfully.",
        data=application,
    )


@applications_bp.patch("/<application_id>/notes")
@recruiter_required
def update_recruiter_notes(application_id: str):
    """
    Save private notes on an application.

    Notes are stored in recruiter_notes and are never exposed to the candidate
    in any API response. Pass null or an empty string to clear existing notes.

    Path parameters:
      application_id : UUID of the application

    Request body (JSON):
      notes : string, required (pass null to clear) — private notes, max 5000 chars

    Responses:
      200 OK           — Notes saved
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — Recruiter does not own this job
      404 Not Found    — Application not found
      422 Unprocessable — notes field missing or exceeds 5000 characters
    """
    recruiter = get_current_user()
    raw = _get_json()

    try:
        data = _notes_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "Recruiter notes validation failed | application_id=%s | errors=%s",
            application_id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    application = application_service.update_recruiter_notes(
        application_id,
        data.get("notes"),
        recruiter,
        **_ctx(),
    )

    return success_response(
        message="Recruiter notes updated successfully.",
        data=application,
    )

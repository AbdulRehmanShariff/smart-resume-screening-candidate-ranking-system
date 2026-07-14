"""
api/resumes/routes.py
---------------------
Resumes Blueprint — route handlers for the Smart Resume Screening System.

Thin route layer: parse input → validate → call resume_service → return response.
All business logic lives in app/services/resume_service.py.

Registered endpoints:
  POST   /api/v1/resumes/                        — Upload resume (multipart/form-data)
  GET    /api/v1/resumes/                        — List own resumes (paginated)
  GET    /api/v1/resumes/stats                   — Aggregate resume statistics
  GET    /api/v1/resumes/<resume_id>             — Get single resume detail
  PATCH  /api/v1/resumes/<resume_id>/set-primary — Set as primary resume
  DELETE /api/v1/resumes/<resume_id>             — Soft-delete a resume

Route handler contract:
  1. Parse multipart form data or JSON body using the appropriate helper.
  2. Validate with the matching Marshmallow schema.
  3. Call the service function with validated data + request context.
  4. Return the standard response envelope from app.core.responses.
  5. Never raise — AppException subclasses are caught by the global
     error handler in app/__init__.py.

File upload design:
  - The 'resume' file field is read from request.files['resume'].
  - Only multipart/form-data requests are accepted for the upload endpoint.
  - If no file is present, the service raises BadRequestError with a clear message.
  - SHA-256 is computed by STREAMING the file in 64 KB chunks inside the service
    layer (no full load into memory in the route layer).

/stats route placement:
  - GET /resumes/stats is registered BEFORE GET /resumes/<resume_id> to prevent
    Flask from treating 'stats' as a resume_id parameter.
"""

import logging

from flask import Blueprint, request
from marshmallow import ValidationError

from app.core.decorators import candidate_required, get_current_user
from app.core.responses import (
    created_response,
    success_response,
    validation_error_response,
)
from app.schemas.resume import (
    ResumeListFilterSchema,
    ResumeUploadSchema,
    SetPrimarySchema,
)
from app.services import resume_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

resumes_bp = Blueprint("resumes", __name__)

# ---------------------------------------------------------------------------
# Schema instances (module-level — Marshmallow schemas are thread-safe)
# ---------------------------------------------------------------------------

_upload_schema = ResumeUploadSchema()
_list_filter_schema = ResumeListFilterSchema()
_set_primary_schema = SetPrimarySchema()


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
    """Convert Flask query-string args to a plain dict (single values only)."""
    return {key: request.args.get(key) for key in request.args}


def _ctx() -> dict:
    """Return common request-context kwargs for service calls."""
    return {
        "ip_address": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
    }


# ---------------------------------------------------------------------------
# Batch 4C Routes
# ---------------------------------------------------------------------------


@resumes_bp.post("/")
@candidate_required
def upload_resume():
    """
    Upload a new resume file.

    Accepts a multipart/form-data request. The file must be sent under the
    field name 'resume'. Optional form fields are validated by ResumeUploadSchema.

    Supported file types: PDF, DOC, DOCX (MIME-type validated server-side).
    Maximum file size: configured via MAX_RESUME_SIZE_MB (default 10 MB).

    Processing pipeline (inside service):
      1. Stream-hash the file (SHA-256) without loading into memory.
      2. Reset file pointer (seek(0)).
      3. Duplicate detection — reject if same file already uploaded.
      4. Determine version number.
      5. Save via LocalStorage (MIME + size validation).
      6. Create Resume DB record.
      7. Optionally set as primary.
      8. Audit log + commit.

    Form fields:
      resume         : file, required — the resume file
      set_as_primary : bool, optional — if true, set as primary resume after upload
      note           : string, optional — free-text label (max 500 chars)

    Responses:
      201 Created       — Resume uploaded and record created
      400 Bad Request   — Missing file, wrong file type, or file too large
      401 Unauthorized  — Missing or invalid JWT
      403 Forbidden     — User is not a candidate
      409 Conflict      — Exact same file already uploaded
      422 Unprocessable — Validation errors on form fields
    """
    candidate = get_current_user()

    # Read optional metadata from the multipart form fields
    form_data = request.form.to_dict()
    try:
        data: dict = _upload_schema.load(form_data)
    except ValidationError as exc:
        logger.debug(
            "Resume upload form validation failed | candidate_id=%s | errors=%s",
            candidate.id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    # Get the uploaded file (None if not provided — service handles the error)
    resume_file = request.files.get("resume")

    resume_data: dict = resume_service.upload_resume(
        candidate,
        resume_file,
        data,
        **_ctx(),
    )

    return created_response(
        message="Resume uploaded successfully.",
        data=resume_data,
    )


@resumes_bp.get("/")
@candidate_required
def list_resumes():
    """
    Return the authenticated candidate's own resume list.

    Paginated and filterable. Only non-deleted resumes are returned.
    All resumes at any parse_status are included unless filtered.

    Query parameters:
      parse_status : string — filter by AI processing status
                              (uploaded|queued|parsing|parsed|embedding_generated
                               |ranked|completed|failed)
      file_type    : string — filter by category (pdf|docx|txt|image)
      is_primary   : bool   — if true, return only the primary resume
      sort_by      : string — newest|oldest|name_asc|name_desc|size_asc|size_desc
      page         : int, default 1
      per_page     : int, default 20, max 100

    Responses:
      200 OK           — Paginated resume list
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not a candidate
      422 Unprocessable — Invalid filter parameters
    """
    candidate = get_current_user()
    raw: dict = _get_query_args()

    try:
        filters: dict = _list_filter_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "Resume list filter validation failed | candidate_id=%s | errors=%s",
            candidate.id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    result: dict = resume_service.list_resumes(candidate, filters)

    return success_response(
        message="Resumes retrieved successfully.",
        data=result["resumes"],
        meta={"pagination": result["pagination"]},
    )


@resumes_bp.get("/stats")
@candidate_required
def get_resume_stats():
    """
    Return aggregate resume statistics for the candidate's dashboard.

    Provides a summary suitable for a dashboard card — total count,
    per-status breakdown, primary resume summary, and total storage used.
    Uses a single GROUP BY query rather than loading all resumes.

    Responses:
      200 OK           — Stats returned
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not a candidate

    Response data:
      total              : int  total non-deleted resume count
      by_status          : dict { parse_status: count }
      primary_resume     : dict (compact resume) or null
      total_size_bytes   : int  total storage used by all non-deleted resumes
      total_size_readable: str  human-readable size (e.g. '4.2 MB')
    """
    candidate = get_current_user()

    stats: dict = resume_service.get_resume_stats(candidate)

    return success_response(
        message="Resume statistics retrieved successfully.",
        data=stats,
    )


@resumes_bp.get("/<resume_id>")
@candidate_required
def get_resume(resume_id: str):
    """
    Fetch a single resume by ID.

    Returns the full resume dict including all AI-processed fields
    (parsed_data, quality_report, ai_metadata, ai_summary) — all None
    until the AI pipeline runs in a later stage.

    Path parameters:
      resume_id : UUID of the resume

    Responses:
      200 OK           — Resume data returned
      400 Bad Request  — Malformed UUID
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this resume
      404 Not Found    — Resume not found or soft-deleted
    """
    candidate = get_current_user()

    resume_data: dict = resume_service.get_resume(resume_id, candidate)

    return success_response(
        message="Resume retrieved successfully.",
        data=resume_data,
    )


@resumes_bp.patch("/<resume_id>/set-primary")
@candidate_required
def set_primary(resume_id: str):
    """
    Set a specific resume as the candidate's primary/active resume.

    Atomically demotes the current primary and promotes the target resume.
    Idempotent: if the resume is already primary, returns 200 with no change.

    Path parameters:
      resume_id : UUID of the resume to promote

    Request body (JSON — all optional):
      note : string — optional note recorded in the audit log

    Responses:
      200 OK           — Resume is now the primary
      400 Bad Request  — Malformed UUID
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this resume
      404 Not Found    — Resume not found or soft-deleted
      422 Unprocessable — Validation errors on body fields
    """
    candidate = get_current_user()
    raw: dict = _get_json()

    try:
        params: dict = _set_primary_schema.load(raw)
    except ValidationError as exc:
        logger.debug(
            "set-primary validation failed | resume_id=%s | errors=%s",
            resume_id,
            exc.messages,
        )
        return validation_error_response(exc.messages)

    resume_data: dict = resume_service.set_primary(
        resume_id,
        candidate,
        note=params.get("note"),
        **_ctx(),
    )

    return success_response(
        message="Primary resume updated successfully.",
        data=resume_data,
    )


@resumes_bp.delete("/<resume_id>")
@candidate_required
def delete_resume(resume_id: str):
    """
    Soft-delete a resume and remove its file from storage.

    The DB record is soft-deleted (deleted_at set). The physical file is
    deleted from the filesystem after the DB commit. If the deleted resume
    was the primary, the next most recent resume is auto-promoted.

    Guard: Cannot delete a resume with active (non-terminal) applications.
    The candidate must withdraw those applications first.

    Path parameters:
      resume_id : UUID of the resume to delete

    Responses:
      200 OK           — Resume deleted successfully
      400 Bad Request  — Resume has active applications, or malformed UUID
      401 Unauthorized — Missing or invalid JWT
      403 Forbidden    — User is not the owner of this resume
      404 Not Found    — Resume not found or already deleted
    """
    candidate = get_current_user()

    resume_service.delete_resume(resume_id, candidate, **_ctx())

    return success_response(
        message="Resume deleted successfully.",
        data=None,
    )

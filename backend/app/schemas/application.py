"""
schemas/application.py
-----------------------
Marshmallow validation schemas for the job application endpoints.

Provides request validation for:
  ApplicationCreateSchema      : POST /applications/
  ApplicationStatusUpdateSchema: PATCH /applications/<id>/status
  ApplicationListFilterSchema  : GET /applications/ (query-string)
  RecruiterNotesSchema         : PATCH /applications/<id>/notes

Design decisions consistent with schemas/job.py and schemas/resume.py:
  - All schemas inherit directly from marshmallow.Schema.
  - Field names match Application model column names exactly.
  - unknown = RAISE on mutation schemas (explicit client feedback).
  - unknown = EXCLUDE on query-string filter schemas.
  - All string inputs are stripped of leading/trailing whitespace.
  - Allowed value sets are defined once as module-level constants
    (mirroring Application model values without importing the model,
    preventing circular imports at schema load time).

Status transition rules are NOT enforced here — that belongs in the
service layer which has access to the current application state.
The schema only validates that the requested target status is a
syntactically valid value.
"""

from __future__ import annotations

from marshmallow import EXCLUDE, RAISE, Schema, ValidationError, fields, validate, validates_schema


# ---------------------------------------------------------------------------
# Allowed value sets (mirrored from ApplicationStatus enum — no circular import)
# ---------------------------------------------------------------------------

_ALL_APPLICATION_STATUSES: tuple = (
    "applied",
    "screening",
    "assessment_pending",
    "assessment_completed",
    "shortlisted",
    "technical_interview",
    "hr_interview",
    "final_interview",
    "offer_sent",
    "offer_accepted",
    "offer_declined",
    "rejected",
    "withdrawn",
)

# Statuses a recruiter may manually set via PATCH /applications/<id>/status.
# Candidates use the dedicated POST /applications/<id>/withdraw endpoint.
# "applied" is the initial state set by the service — not a valid transition target.
_RECRUITER_TRANSITION_STATUSES: tuple = (
    "screening",
    "assessment_pending",
    "assessment_completed",
    "shortlisted",
    "technical_interview",
    "hr_interview",
    "final_interview",
    "offer_sent",
    "offer_accepted",
    "offer_declined",
    "rejected",
)

_VALID_SORT_BY: tuple = (
    "newest",
    "oldest",
    "score_desc",
    "score_asc",
    "status",
)

# Field length caps
_MAX_NOTES_LEN: int = 5_000   # recruiter_notes — private, longer is fine
_MAX_COVER_LEN: int = 5_000   # cover_letter (future field, capped conservatively)

# Pagination (consistent with resume.py and job.py)
_DEFAULT_PAGE: int = 1
_DEFAULT_PER_PAGE: int = 20
_MAX_PER_PAGE: int = 100


# ---------------------------------------------------------------------------
# Schema 1: ApplicationCreateSchema
# ---------------------------------------------------------------------------


class ApplicationCreateSchema(Schema):
    """
    Validates POST /api/v1/applications/ request body.

    A candidate submits an application for a job using a specific resume.
    Both job_id and resume_id are required. The service layer verifies:
      - The job is PUBLISHED.
      - The candidate owns the resume and it is not deleted.
      - No duplicate application exists for this (job, candidate) pair.

    The `status` field is never accepted here — it is always set to
    'applied' by the service layer.

    Fields:
      job_id    : UUID string, required — the job to apply for
      resume_id : UUID string, required — the resume to submit

    Responses on validation failure:
      422 Unprocessable — missing required field or invalid UUID format
    """

    class Meta:
        unknown = RAISE   # no stray fields accepted from candidates

    job_id = fields.UUID(
        required=True,
        metadata={"description": "UUID of the published job to apply for."},
    )

    resume_id = fields.UUID(
        required=True,
        metadata={"description": "UUID of the candidate's own resume to submit."},
    )


# ---------------------------------------------------------------------------
# Schema 2: ApplicationStatusUpdateSchema
# ---------------------------------------------------------------------------


class ApplicationStatusUpdateSchema(Schema):
    """
    Validates PATCH /api/v1/applications/<id>/status request body.

    Used exclusively by recruiters to advance an application through the
    hiring lifecycle. The schema accepts any recruiter-initiatable status.
    The service layer enforces that the transition is valid from the
    application's current state.

    Fields:
      status : string, required — the target status value

    Valid target statuses (recruiter-only):
      screening, assessment_pending, assessment_completed, shortlisted,
      technical_interview, hr_interview, final_interview,
      offer_sent, offer_accepted, offer_declined, rejected

    Candidates use the dedicated POST /applications/<id>/withdraw endpoint.
    The 'applied' status is the initial state and is not a valid transition.

    Responses on validation failure:
      422 Unprocessable — missing or invalid status value
    """

    class Meta:
        unknown = RAISE

    status = fields.Str(
        required=True,
        validate=validate.OneOf(
            choices=_RECRUITER_TRANSITION_STATUSES,
            error=(
                "status must be one of: "
                + ", ".join(_RECRUITER_TRANSITION_STATUSES) + "."
            ),
        ),
        metadata={"description": "Target application status for this transition."},
    )

    @validates_schema
    def strip_status(self, data: dict, **kwargs) -> None:
        """Strip whitespace and lowercase the status value."""
        if "status" in data and isinstance(data["status"], str):
            data["status"] = data["status"].strip().lower()


# ---------------------------------------------------------------------------
# Schema 3: ApplicationListFilterSchema
# ---------------------------------------------------------------------------


class ApplicationListFilterSchema(Schema):
    """
    Validates query-string parameters for application list endpoints.

    Used by both:
      GET /api/v1/applications/           (candidate — their own applications)
      GET /api/v1/jobs/<job_id>/applicants (recruiter — all applicants for a job)

    All fields are optional. Missing fields produce safe defaults.

    Fields:
      status   : string — filter by application status
      sort_by  : string — sort order (newest|oldest|score_desc|score_asc|status)
      page     : int    — 1-based page number, default 1
      per_page : int    — results per page (1–100), default 20
    """

    class Meta:
        unknown = EXCLUDE   # silently ignore extra query params

    status = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            _ALL_APPLICATION_STATUSES,
            error=(
                "status must be one of: "
                + ", ".join(_ALL_APPLICATION_STATUSES) + "."
            ),
        ),
        metadata={"description": "Filter applications by status."},
    )

    sort_by = fields.Str(
        load_default="newest",
        validate=validate.OneOf(
            _VALID_SORT_BY,
            error=(
                "sort_by must be one of: "
                + ", ".join(_VALID_SORT_BY) + "."
            ),
        ),
        metadata={"description": "Sort order for the application list."},
    )

    page = fields.Integer(
        load_default=_DEFAULT_PAGE,
        validate=validate.Range(
            min=1,
            error="page must be a positive integer.",
        ),
        metadata={"description": "1-based page number."},
    )

    per_page = fields.Integer(
        load_default=_DEFAULT_PER_PAGE,
        validate=validate.Range(
            min=1,
            max=_MAX_PER_PAGE,
            error=f"per_page must be between 1 and {_MAX_PER_PAGE}.",
        ),
        metadata={"description": f"Results per page (1–{_MAX_PER_PAGE})."},
    )


# ---------------------------------------------------------------------------
# Schema 4: RecruiterNotesSchema
# ---------------------------------------------------------------------------


class RecruiterNotesSchema(Schema):
    """
    Validates PATCH /api/v1/applications/<id>/notes request body.

    Recruiter-only endpoint to save private notes about an application.
    Notes are never exposed to the candidate in any API response.

    Fields:
      notes : string, required — private notes (max 5,000 characters).
               Pass an empty string or null to clear existing notes.
    """

    class Meta:
        unknown = RAISE

    notes = fields.Str(
        required=True,
        allow_none=True,
        validate=validate.Length(
            max=_MAX_NOTES_LEN,
            error=f"Recruiter notes must not exceed {_MAX_NOTES_LEN} characters.",
        ),
        metadata={"description": "Private recruiter notes. Pass null to clear."},
    )

    @validates_schema
    def strip_notes(self, data: dict, **kwargs) -> None:
        """Strip whitespace; convert blank string to None (clear notes)."""
        notes = data.get("notes")
        if notes is not None and isinstance(notes, str):
            stripped = notes.strip()
            data["notes"] = stripped if stripped else None

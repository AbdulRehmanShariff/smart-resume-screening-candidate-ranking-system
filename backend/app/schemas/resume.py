"""
schemas/resume.py
-----------------
Marshmallow validation schemas for resume management endpoints.

Schemas:
  ResumeUploadSchema        : Form-data fields accompanying a multipart file upload.
  ResumeListFilterSchema    : Query-string filters for GET /resumes/.
  SetPrimarySchema          : Body for PATCH /resumes/<id>/set-primary.

Design decisions consistent with Stage 3 (job.py):
  - All schemas inherit directly from marshmallow.Schema (no Flask-specific base).
  - Field-level validators are defined inline using validate.*; cross-field
    validators use @validates_schema.
  - unknown = EXCLUDE on query-string schemas to silently ignore extra params.
  - unknown = RAISE on mutation schemas so clients get explicit feedback.
  - All string inputs are stripped of leading/trailing whitespace.
  - Choices for enumerated fields are defined once and reused in both validation
    and error messages.
"""

from __future__ import annotations

from marshmallow import EXCLUDE, RAISE, Schema, ValidationError, fields, validate, validates_schema


# ---------------------------------------------------------------------------
# Allowed value sets (single source of truth shared across schemas)
# ---------------------------------------------------------------------------

_VALID_PARSE_STATUSES: tuple = (
    "uploaded",
    "queued",
    "parsing",
    "parsed",
    "embedding_generated",
    "ranked",
    "completed",
    "failed",
)

_VALID_FILE_TYPES: tuple = ("pdf", "docx", "txt", "image")

_VALID_SORT_BY: tuple = (
    "newest",
    "oldest",
    "name_asc",
    "name_desc",
    "size_asc",
    "size_desc",
)


# ---------------------------------------------------------------------------
# Batch 4A — Schema 1: ResumeUploadSchema
# ---------------------------------------------------------------------------


class ResumeUploadSchema(Schema):
    """
    Validates the optional form-data fields that accompany a resume file upload.

    The actual file itself is not validated here — it is handled by LocalStorage
    (MIME type, size, SHA-256 deduplication). This schema validates the
    *metadata* fields submitted alongside the file.

    Fields:
      set_as_primary : bool, optional — if True, mark this upload as the
                       candidate's primary/active resume after a successful upload.
                       Defaults to False.
      note           : string, optional — a free-text label the candidate can
                       attach to this upload (e.g. 'Tailored for engineering roles').
                       Max 500 characters.

    Usage (in route):
        form_data = request.form.to_dict()
        data = ResumeUploadSchema().load(form_data)
    """

    class Meta:
        unknown = EXCLUDE   # ignore unexpected form fields (e.g. browser cruft)

    set_as_primary: fields.Boolean = fields.Boolean(
        load_default=False,
        metadata={"description": (
            "If true, this upload becomes the candidate's active primary resume "
            "immediately after upload. The current primary (if any) is demoted."
        )},
    )

    note: fields.String = fields.String(
        load_default=None,
        allow_none=True,
        validate=validate.Length(
            max=500,
            error="Resume note must not exceed 500 characters.",
        ),
        metadata={"description": "Optional free-text label for this resume version."},
    )

    @validates_schema
    def strip_note(self, data: dict, **kwargs) -> None:
        """Strip whitespace from note and convert empty string to None."""
        note = data.get("note")
        if note is not None:
            stripped = note.strip()
            data["note"] = stripped if stripped else None


# ---------------------------------------------------------------------------
# Batch 4A — Schema 2: ResumeListFilterSchema
# ---------------------------------------------------------------------------


class ResumeListFilterSchema(Schema):
    """
    Validates query-string parameters for GET /api/v1/resumes/.

    All fields are optional. Missing fields use safe defaults that return
    the candidate's full resume list (newest first, page 1, 20 per page).

    Fields:
      parse_status  : string — filter by AI processing status
      file_type     : string — filter by file category (pdf, docx, txt, image)
      is_primary    : bool   — filter to show only the primary resume
      sort_by       : string — sort order
      page          : int    — 1-based page number
      per_page      : int    — results per page (1–100)

    Usage (in route):
        raw = _get_query_args()
        filters = ResumeListFilterSchema().load(raw)
    """

    class Meta:
        unknown = EXCLUDE   # silently ignore extra query params

    parse_status: fields.String = fields.String(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            _VALID_PARSE_STATUSES,
            error=(
                "parse_status must be one of: "
                + ", ".join(_VALID_PARSE_STATUSES) + "."
            ),
        ),
        metadata={"description": "Filter resumes by AI processing status."},
    )

    file_type: fields.String = fields.String(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            _VALID_FILE_TYPES,
            error=(
                "file_type must be one of: "
                + ", ".join(_VALID_FILE_TYPES) + "."
            ),
        ),
        metadata={"description": "Filter resumes by file category."},
    )

    is_primary: fields.Boolean = fields.Boolean(
        load_default=None,
        allow_none=True,
        metadata={"description": "If true, return only the primary resume."},
    )

    sort_by: fields.String = fields.String(
        load_default="newest",
        validate=validate.OneOf(
            _VALID_SORT_BY,
            error=(
                "sort_by must be one of: "
                + ", ".join(_VALID_SORT_BY) + "."
            ),
        ),
        metadata={"description": "Sort order for the resume list."},
    )

    page: fields.Integer = fields.Integer(
        load_default=1,
        validate=validate.Range(
            min=1,
            error="page must be a positive integer.",
        ),
        metadata={"description": "1-based page number."},
    )

    per_page: fields.Integer = fields.Integer(
        load_default=20,
        validate=validate.Range(
            min=1,
            max=100,
            error="per_page must be between 1 and 100.",
        ),
        metadata={"description": "Number of results per page (1–100)."},
    )


# ---------------------------------------------------------------------------
# Batch 4A — Schema 3: SetPrimarySchema
# ---------------------------------------------------------------------------


class SetPrimarySchema(Schema):
    """
    Validates the optional JSON body for PATCH /api/v1/resumes/<id>/set-primary.

    No required fields — the action (set this resume as primary) is implied
    by the endpoint itself. The schema exists to:
      1. Reject unknown fields so that clients get explicit feedback.
      2. Validate the optional recruiter-facing note if provided.

    Fields:
      note : string, optional — recorded in the audit log.

    Usage (in route):
        raw = _get_json()
        data = SetPrimarySchema().load(raw)
    """

    class Meta:
        unknown = RAISE   # raise on unknown fields to catch client mistakes

    note: fields.String = fields.String(
        load_default=None,
        allow_none=True,
        validate=validate.Length(
            max=500,
            error="Note must not exceed 500 characters.",
        ),
        metadata={"description": "Optional note recorded in the audit log."},
    )

    @validates_schema
    def strip_note(self, data: dict, **kwargs) -> None:
        """Strip whitespace from note and convert empty string to None."""
        note = data.get("note")
        if note is not None:
            stripped = note.strip()
            data["note"] = stripped if stripped else None

"""
schemas/job.py
--------------
Marshmallow schemas for all job management endpoints.

Provides request validation for:
  - CreateJobSchema          : POST   /jobs/
  - UpdateJobSchema          : PATCH  /jobs/<job_id>
  - JobFilterSchema          : GET    /jobs/          (query-string validation)
  - JobStatusTransitionSchema: POST   /jobs/<job_id>/publish|pause|close|archive

Design decisions:
  - Field names match the Job model column names exactly so service functions
    can unpack validated data directly with `job.column = data[column]`.
  - CreateJobSchema uses EXCLUDE (not RAISE) for unknown fields. Job postings
    may receive extra context keys from API consumers; silently dropping them
    is safer than rejecting a valid post because a client sent an extra key.
  - UpdateJobSchema mirrors CreateJobSchema but all fields are optional
    (load_default=None / allow_none). The service layer applies only the
    non-None keys, enabling true partial update semantics.
  - JobFilterSchema handles query-string deserialization. Query-string values
    arrive as strings; fields.Bool and fields.Int handle coercion. EXCLUDE is
    mandatory because the Flask request.args dict always contains extra keys
    that Marshmallow should not touch (e.g. Flask-internal routing keys).
  - Salary cross-field validation (salary_max >= salary_min) is enforced in
    @validates_schema on both Create and Update schemas — matching the database
    CHECK constraint so invalid data is rejected at the API layer before hitting
    the DB.
  - skills_required and nice_to_have_skills are JSONB arrays on the model.
    They are validated as fields.List(fields.Str()) here, which:
      - Accepts JSON arrays of strings
      - Strips whitespace from each skill
      - Deduplicates via @validates_schema
      - Rejects blank strings within the array

Marshmallow version: 3.26.x (schema-level unknown field handling via Meta.unknown)
"""

from typing import Any

from marshmallow import (
    EXCLUDE,
    Schema,
    ValidationError,
    fields,
    post_load,
    pre_load,
    validate,
    validates,
    validates_schema,
)

# ---------------------------------------------------------------------------
# Shared Constants — mirrored from Job model to avoid circular imports
# ---------------------------------------------------------------------------
# These are kept in sync with Job class constants. If the model adds a new
# status or type, add the value here too. Using plain tuples instead of
# importing the Job class prevents circular import chains at schema load time.

_ALL_JOB_STATUSES: tuple = ("draft", "published", "paused", "closed", "archived")
_ALL_JOB_TYPES: tuple = ("full_time", "part_time", "contract", "internship", "freelance")
_ALL_EXPERIENCE_LEVELS: tuple = ("intern", "fresher", "junior", "mid", "senior", "lead")
_ALL_CURRENCIES: tuple = (
    "USD", "EUR", "GBP", "INR", "CAD", "AUD", "SGD", "AED",
    "JPY", "CHF", "MYR", "PKR", "SAR", "BDT", "NZD",
)

# Recruiter-initiatable status transitions only (excludes 'draft' which is
# the default creation state, not a valid transition target via the API).
_TRANSITION_TARGET_STATUSES: tuple = ("published", "paused", "closed", "archived")

# Field length caps
_TITLE_MAX_LEN: int = 255
_LOCATION_MAX_LEN: int = 255
_CURRENCY_MAX_LEN: int = 10
_MAX_SKILLS_PER_LIST: int = 100       # Hard cap per list to prevent abuse
_MAX_SKILL_LEN: int = 100             # Max chars per individual skill string
_MAX_SALARY: float = 999_999_999.99   # Numeric(12,2) ceiling
_MAX_DESCRIPTION_LEN: int = 50_000    # ~50 KB of text
_MAX_REQUIREMENTS_LEN: int = 20_000
_MAX_RESPONSIBILITIES_LEN: int = 20_000
_MAX_DEADLINE_FUTURE_YEARS: int = 5   # Sanity cap on application_deadline

# Pagination
_DEFAULT_PAGE: int = 1
_DEFAULT_PER_PAGE: int = 20
_MAX_PER_PAGE: int = 100


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _normalise_skills(raw: list | None) -> list:
    """
    Strip whitespace from each skill, remove blank entries.

    Called in @pre_load to clean skill arrays before field-level validation.
    Returns an empty list if raw is None or not a list (field validation
    handles the type error).
    """
    if not isinstance(raw, list):
        return raw  # let field validator surface the type error
    cleaned = []
    for item in raw:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                cleaned.append(stripped)
        else:
            cleaned.append(item)  # keep non-strings so field validator errors
    return cleaned


def _deduplicate_skills(skills: list) -> list:
    """
    Remove duplicate skills (case-insensitive), preserving original casing
    of the first occurrence.

    Example: ["Python", "python", "PYTHON"] → ["Python"]
    """
    seen: set = set()
    result = []
    for skill in skills:
        if isinstance(skill, str):
            key = skill.lower()
            if key not in seen:
                seen.add(key)
                result.append(skill)
        else:
            result.append(skill)
    return result


# ---------------------------------------------------------------------------
# CreateJobSchema  (Batch 3A)
# ---------------------------------------------------------------------------


class CreateJobSchema(Schema):
    """
    Validate POST /jobs/ request body.

    Creates a new job posting. New jobs are always created in 'draft' status
    regardless of the `status` field — the service layer enforces this. The
    schema does NOT accept a `status` field in the creation payload; status is
    controlled exclusively via the transition endpoints.

    Required fields:
      title       : string, 1–255 chars
      description : string, 1–50,000 chars  (the AI pipeline's primary input)
      job_type    : string, one of the 5 job type constants

    Optional fields:
      requirements         : text, up to 20,000 chars
      responsibilities     : text, up to 20,000 chars
      location             : string, up to 255 chars
      is_remote            : bool, default False
      experience_level     : string, one of the 6 experience level constants
      salary_min           : decimal ≥ 0
      salary_max           : decimal ≥ salary_min (cross-field validated)
      salary_currency      : ISO 4217 code, default "USD"
      skills_required      : list of strings, max 100 entries, max 100 chars each
      nice_to_have_skills  : list of strings, max 100 entries, max 100 chars each
      application_deadline : ISO 8601 datetime, must be in the future

    Cross-field validation:
      - salary_max >= salary_min (when both provided)
      - application_deadline must be a future datetime

    Example valid payload:
      {
        "title": "Senior Backend Engineer",
        "description": "We are looking for a Python expert ...",
        "job_type": "full_time",
        "experience_level": "senior",
        "skills_required": ["Python", "Flask", "PostgreSQL"],
        "salary_min": 80000,
        "salary_max": 120000,
        "salary_currency": "USD",
        "location": "New York, NY",
        "is_remote": true
      }
    """

    class Meta:
        # EXCLUDE unknown keys — job posting clients may send extra metadata.
        unknown = EXCLUDE

    # ------------------------------------------------------------------
    # Required Fields
    # ------------------------------------------------------------------

    title = fields.Str(
        required=True,
        validate=validate.Length(
            min=1,
            max=_TITLE_MAX_LEN,
            error=f"Job title must be between 1 and {_TITLE_MAX_LEN} characters.",
        ),
        metadata={"description": "Job title displayed to candidates."},
    )

    description = fields.Str(
        required=True,
        validate=validate.Length(
            min=1,
            max=_MAX_DESCRIPTION_LEN,
            error=(
                f"Job description must be between 1 and "
                f"{_MAX_DESCRIPTION_LEN} characters."
            ),
        ),
        metadata={
            "description": (
                "Full job description. This is the primary text used by the "
                "AI pipeline for semantic matching against resumes."
            )
        },
    )

    job_type = fields.Str(
        required=True,
        validate=validate.OneOf(
            choices=_ALL_JOB_TYPES,
            error=(
                "job_type must be one of: "
                + ", ".join(_ALL_JOB_TYPES) + "."
            ),
        ),
        metadata={
            "description": (
                "Employment type. "
                "One of: full_time, part_time, contract, internship, freelance."
            )
        },
    )

    # ------------------------------------------------------------------
    # Optional Classification Fields
    # ------------------------------------------------------------------

    requirements = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(
            max=_MAX_REQUIREMENTS_LEN,
            error=f"Requirements must not exceed {_MAX_REQUIREMENTS_LEN} characters.",
        ),
        metadata={
            "description": "Technical, educational, or certification requirements."
        },
    )

    responsibilities = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(
            max=_MAX_RESPONSIBILITIES_LEN,
            error=(
                f"Responsibilities must not exceed "
                f"{_MAX_RESPONSIBILITIES_LEN} characters."
            ),
        ),
        metadata={"description": "Day-to-day duties and responsibilities."},
    )

    location = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(
            max=_LOCATION_MAX_LEN,
            error=f"Location must not exceed {_LOCATION_MAX_LEN} characters.",
        ),
        metadata={
            "description": 'Physical office location. Example: "New York, NY".'
        },
    )

    is_remote = fields.Bool(
        load_default=False,
        metadata={"description": "True if remote work is fully or partially supported."},
    )

    experience_level = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_ALL_EXPERIENCE_LEVELS,
            error=(
                "experience_level must be one of: "
                + ", ".join(_ALL_EXPERIENCE_LEVELS) + "."
            ),
        ),
        metadata={
            "description": (
                "Required experience level. "
                "One of: intern, fresher, junior, mid, senior, lead."
            )
        },
    )

    # ------------------------------------------------------------------
    # Compensation Fields
    # ------------------------------------------------------------------

    salary_min = fields.Float(
        load_default=None,
        allow_none=True,
        validate=validate.Range(
            min=0.0,
            max=_MAX_SALARY,
            error=f"salary_min must be between 0 and {_MAX_SALARY:,.2f}.",
        ),
        metadata={"description": "Minimum salary in salary_currency. NULL = not disclosed."},
    )

    salary_max = fields.Float(
        load_default=None,
        allow_none=True,
        validate=validate.Range(
            min=0.0,
            max=_MAX_SALARY,
            error=f"salary_max must be between 0 and {_MAX_SALARY:,.2f}.",
        ),
        metadata={
            "description": (
                "Maximum salary in salary_currency. "
                "Must be >= salary_min when both are provided. NULL = not disclosed."
            )
        },
    )

    salary_currency = fields.Str(
        load_default="USD",
        validate=validate.OneOf(
            choices=_ALL_CURRENCIES,
            error=(
                "salary_currency must be a supported ISO 4217 code. "
                "Supported: " + ", ".join(_ALL_CURRENCIES) + "."
            ),
        ),
        metadata={"description": "ISO 4217 currency code. Defaults to 'USD'."},
    )

    # ------------------------------------------------------------------
    # Skills Fields
    # ------------------------------------------------------------------

    skills_required = fields.List(
        fields.Str(
            validate=validate.Length(
                min=1,
                max=_MAX_SKILL_LEN,
                error=f"Each skill must be 1–{_MAX_SKILL_LEN} characters.",
            )
        ),
        load_default=list,
        validate=validate.Length(
            max=_MAX_SKILLS_PER_LIST,
            error=f"skills_required must not contain more than {_MAX_SKILLS_PER_LIST} entries.",
        ),
        metadata={
            "description": (
                "Must-have skills. Used for hard matching and AI ranking. "
                'Example: ["Python", "Flask", "PostgreSQL"].'
            )
        },
    )

    nice_to_have_skills = fields.List(
        fields.Str(
            validate=validate.Length(
                min=1,
                max=_MAX_SKILL_LEN,
                error=f"Each skill must be 1–{_MAX_SKILL_LEN} characters.",
            )
        ),
        load_default=list,
        validate=validate.Length(
            max=_MAX_SKILLS_PER_LIST,
            error=(
                f"nice_to_have_skills must not contain "
                f"more than {_MAX_SKILLS_PER_LIST} entries."
            ),
        ),
        metadata={
            "description": (
                "Preferred but not mandatory skills. "
                "Candidates with these rank higher. "
                'Example: ["Kubernetes", "Terraform"].'
            )
        },
    )

    # ------------------------------------------------------------------
    # Deadline
    # ------------------------------------------------------------------

    application_deadline = fields.DateTime(
        load_default=None,
        allow_none=True,
        format="iso",
        metadata={
            "description": (
                "ISO 8601 UTC datetime after which applications are no longer accepted. "
                "NULL = no deadline. Must be a future datetime."
            )
        },
    )

    # ------------------------------------------------------------------
    # Pre-load: normalise before field-level validation
    # ------------------------------------------------------------------

    @pre_load
    def normalise_input(self, data: dict, **kwargs: Any) -> dict:
        """
        Normalise raw request payload before Marshmallow field validation.

        - title, location: strip leading/trailing whitespace.
        - requirements, responsibilities: strip whitespace; treat blank as None.
        - job_type, experience_level, salary_currency: strip + lowercase.
        - skills_required, nice_to_have_skills: strip each element, drop blanks.
        - is_remote: normalise truthful string representations to bool.
        """
        if not isinstance(data, dict):
            return data

        # String fields — strip whitespace
        for field in ("title", "description", "location"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip()

        # Nullable text fields — strip; blank becomes None
        for field in ("requirements", "responsibilities"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip() or None

        # Enum fields — strip and lowercase for forgiving matching
        for field in ("job_type", "experience_level"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip().lower()

        # Currency: strip and uppercase (ISO 4217 codes are uppercase by convention)
        if "salary_currency" in data and isinstance(data["salary_currency"], str):
            data["salary_currency"] = data["salary_currency"].strip().upper()

        # Skill lists — clean each element
        for field in ("skills_required", "nice_to_have_skills"):
            if field in data:
                data[field] = _normalise_skills(data[field])

        return data

    # ------------------------------------------------------------------
    # Field-level validators
    # ------------------------------------------------------------------

    @validates("title")
    def validate_title(self, value: str) -> None:
        """Reject blank titles (strip-then-empty guard)."""
        if not value or not value.strip():
            raise ValidationError("Job title must not be blank.")

    @validates("description")
    def validate_description(self, value: str) -> None:
        """Reject blank descriptions."""
        if not value or not value.strip():
            raise ValidationError("Job description must not be blank.")

    @validates("skills_required")
    def validate_skills_required(self, value: list) -> None:
        """Each skill entry must be a non-empty string."""
        for i, skill in enumerate(value):
            if not isinstance(skill, str) or not skill.strip():
                raise ValidationError(
                    f"skills_required[{i}] must be a non-empty string."
                )

    @validates("nice_to_have_skills")
    def validate_nice_to_have_skills(self, value: list) -> None:
        """Each skill entry must be a non-empty string."""
        for i, skill in enumerate(value):
            if not isinstance(skill, str) or not skill.strip():
                raise ValidationError(
                    f"nice_to_have_skills[{i}] must be a non-empty string."
                )

    # ------------------------------------------------------------------
    # Cross-field validators
    # ------------------------------------------------------------------

    @validates_schema
    def validate_salary_range(self, data: dict, **kwargs: Any) -> None:
        """
        Enforce salary_max >= salary_min when both fields are provided.

        This mirrors the database CHECK constraint so invalid data is
        rejected at the API layer before reaching PostgreSQL.
        """
        salary_min = data.get("salary_min")
        salary_max = data.get("salary_max")

        if salary_min is not None and salary_max is not None:
            if salary_max < salary_min:
                raise ValidationError(
                    {
                        "salary_max": (
                            "salary_max must be greater than or equal to salary_min. "
                            f"Got salary_min={salary_min}, salary_max={salary_max}."
                        )
                    }
                )

    @validates_schema
    def validate_deadline_is_future(self, data: dict, **kwargs: Any) -> None:
        """
        Reject application_deadline values that are in the past.

        A past deadline would immediately close the job to applications —
        almost certainly a user error rather than intentional behaviour.
        """
        from datetime import datetime, timezone

        deadline = data.get("application_deadline")
        if deadline is not None:
            now = datetime.now(timezone.utc)
            # Make deadline timezone-aware if it is naive (assume UTC)
            if deadline.tzinfo is None:
                from datetime import timezone as _tz
                deadline = deadline.replace(tzinfo=_tz.utc)
            if deadline <= now:
                raise ValidationError(
                    {"application_deadline": "application_deadline must be a future datetime."}
                )

    @post_load
    def deduplicate_skills(self, data: dict, **kwargs: Any) -> dict:
        """
        Remove duplicate skills (case-insensitive) from both skill lists.

        Applied after all field-level validation passes, so the deduplication
        operates on already-clean, validated strings.
        """
        if "skills_required" in data and isinstance(data["skills_required"], list):
            data["skills_required"] = _deduplicate_skills(data["skills_required"])
        if "nice_to_have_skills" in data and isinstance(data["nice_to_have_skills"], list):
            data["nice_to_have_skills"] = _deduplicate_skills(data["nice_to_have_skills"])
        return data


# ---------------------------------------------------------------------------
# UpdateJobSchema  (Batch 3A)
# ---------------------------------------------------------------------------


class UpdateJobSchema(Schema):
    """
    Validate PATCH /jobs/<job_id> request body.

    All fields are optional — only supplied, non-None values are applied
    by the service layer (true partial update semantics).

    Status is NOT updatable via this schema. All status changes go through
    the dedicated transition endpoints (publish, pause, close, archive).

    Validation rules are identical to CreateJobSchema. Cross-field checks
    for salary range and deadline apply only when both related fields are
    present in the same request.

    Example valid payload (partial update — only provided fields change):
      {
        "title": "Lead Backend Engineer",
        "salary_max": 140000,
        "nice_to_have_skills": ["GraphQL", "Kubernetes"]
      }
    """

    class Meta:
        unknown = EXCLUDE

    # ------------------------------------------------------------------
    # All fields optional — same rules as CreateJobSchema
    # ------------------------------------------------------------------

    title = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(min=1, max=_TITLE_MAX_LEN),
        metadata={"description": "Updated job title."},
    )

    description = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(min=1, max=_MAX_DESCRIPTION_LEN),
        metadata={"description": "Updated job description."},
    )

    job_type = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_ALL_JOB_TYPES,
            error="job_type must be one of: " + ", ".join(_ALL_JOB_TYPES) + ".",
        ),
        metadata={"description": "Updated employment type."},
    )

    requirements = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_MAX_REQUIREMENTS_LEN),
        metadata={"description": "Updated requirements text."},
    )

    responsibilities = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_MAX_RESPONSIBILITIES_LEN),
        metadata={"description": "Updated responsibilities text."},
    )

    location = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_LOCATION_MAX_LEN),
        metadata={"description": "Updated office location."},
    )

    is_remote = fields.Bool(
        load_default=None,
        allow_none=True,
        metadata={"description": "Updated remote-work flag."},
    )

    experience_level = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_ALL_EXPERIENCE_LEVELS,
            error=(
                "experience_level must be one of: "
                + ", ".join(_ALL_EXPERIENCE_LEVELS) + "."
            ),
        ),
        metadata={"description": "Updated required experience level."},
    )

    salary_min = fields.Float(
        load_default=None,
        allow_none=True,
        validate=validate.Range(min=0.0, max=_MAX_SALARY),
        metadata={"description": "Updated minimum salary."},
    )

    salary_max = fields.Float(
        load_default=None,
        allow_none=True,
        validate=validate.Range(min=0.0, max=_MAX_SALARY),
        metadata={"description": "Updated maximum salary (must be >= salary_min)."},
    )

    salary_currency = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_ALL_CURRENCIES,
            error=(
                "salary_currency must be a supported ISO 4217 code. "
                "Supported: " + ", ".join(_ALL_CURRENCIES) + "."
            ),
        ),
        metadata={"description": "Updated salary currency code."},
    )

    skills_required = fields.List(
        fields.Str(validate=validate.Length(min=1, max=_MAX_SKILL_LEN)),
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_MAX_SKILLS_PER_LIST),
        metadata={"description": "Updated required skills list (replaces existing list)."},
    )

    nice_to_have_skills = fields.List(
        fields.Str(validate=validate.Length(min=1, max=_MAX_SKILL_LEN)),
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_MAX_SKILLS_PER_LIST),
        metadata={"description": "Updated preferred skills list (replaces existing list)."},
    )

    application_deadline = fields.DateTime(
        load_default=None,
        allow_none=True,
        format="iso",
        metadata={
            "description": (
                "Updated application deadline. "
                "Set to null to remove the deadline. "
                "Must be a future datetime if provided."
            )
        },
    )

    # ------------------------------------------------------------------
    # Pre-load: same normalisation as CreateJobSchema
    # ------------------------------------------------------------------

    @pre_load
    def normalise_input(self, data: dict, **kwargs: Any) -> dict:
        """Normalise raw request payload — mirrors CreateJobSchema.normalise_input."""
        if not isinstance(data, dict):
            return data

        for field in ("title", "description", "location"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip() or None

        for field in ("requirements", "responsibilities"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip() or None

        for field in ("job_type", "experience_level"):
            if field in data and isinstance(data[field], str):
                val = data[field].strip().lower()
                data[field] = val if val else None

        # Currency: strip and uppercase
        if "salary_currency" in data and isinstance(data["salary_currency"], str):
            val = data["salary_currency"].strip().upper()
            data["salary_currency"] = val if val else None

        for field in ("skills_required", "nice_to_have_skills"):
            if field in data and data[field] is not None:
                data[field] = _normalise_skills(data[field])

        return data

    # ------------------------------------------------------------------
    # Cross-field validators
    # ------------------------------------------------------------------

    @validates_schema
    def validate_salary_range(self, data: dict, **kwargs: Any) -> None:
        """Salary range cross-field check — identical to CreateJobSchema."""
        salary_min = data.get("salary_min")
        salary_max = data.get("salary_max")
        if salary_min is not None and salary_max is not None:
            if salary_max < salary_min:
                raise ValidationError(
                    {
                        "salary_max": (
                            "salary_max must be >= salary_min. "
                            f"Got salary_min={salary_min}, salary_max={salary_max}."
                        )
                    }
                )

    @validates_schema
    def validate_deadline_is_future(self, data: dict, **kwargs: Any) -> None:
        """Deadline must be in the future when provided (and not explicitly None)."""
        from datetime import datetime, timezone

        deadline = data.get("application_deadline")
        # None means "keep existing" or "remove deadline" — both are valid
        if deadline is not None:
            now = datetime.now(timezone.utc)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline <= now:
                raise ValidationError(
                    {
                        "application_deadline": (
                            "application_deadline must be a future datetime."
                        )
                    }
                )

    @validates_schema
    def validate_at_least_one_field(self, data: dict, **kwargs: Any) -> None:
        """
        Reject PATCH requests where every field is None (i.e. no-op updates).

        A PATCH with no meaningful changes wastes a DB write and indicates
        a client bug. Requiring at least one non-None value gives a clear
        error message instead of silently succeeding.
        """
        provided = {k for k, v in data.items() if v is not None}
        if not provided:
            raise ValidationError(
                "At least one field must be provided for a partial update."
            )

    @post_load
    def deduplicate_skills(self, data: dict, **kwargs: Any) -> dict:
        """Deduplicate skill lists after all validation passes."""
        for field in ("skills_required", "nice_to_have_skills"):
            if field in data and isinstance(data[field], list):
                data[field] = _deduplicate_skills(data[field])
        return data


# ---------------------------------------------------------------------------
# JobFilterSchema  (Batch 3A)
# ---------------------------------------------------------------------------


class JobFilterSchema(Schema):
    """
    Validate GET /jobs/ query-string parameters for candidate job search.

    All fields are optional — omitting a filter means "no restriction on
    that dimension". Multiple filters are ANDed together.

    Query-string parameters:
      status            : string — default 'published'; one of the 5 statuses.
                          Candidates always receive only 'published'. Recruiters
                          viewing /my may pass any status.
      job_type          : string — one of the 5 job type constants
      experience_level  : string — one of the 6 experience level constants
      is_remote         : bool   — true/false
      location          : string — partial match (case-insensitive ILIKE)
      skills            : list[string] — jobs must require ALL listed skills
      salary_min        : float  — minimum of the job's salary range
      salary_max        : float  — maximum of the job's salary range
      salary_currency   : string — ISO 4217 currency filter
      search            : string — free-text search against title + description
      sort_by           : string — one of: newest, oldest, salary_asc,
                                   salary_desc, applications_asc, applications_desc
      page              : int    — 1-based page number, default 1
      per_page          : int    — results per page, 1–100, default 20

    Example query string:
      /jobs/?job_type=full_time&experience_level=senior&is_remote=true
            &skills=Python&skills=Flask&page=1&per_page=20

    Note on `skills` multi-value: Flask's request.args.getlist("skills")
    returns a list. The service layer calls this schema with:
      data = dict(request.args.to_dict(flat=False))
    so list fields like `skills` arrive as lists of strings.
    """

    class Meta:
        # EXCLUDE is mandatory for query-string schemas — Flask routing
        # may inject extra keys that should not cause validation errors.
        unknown = EXCLUDE

    # ------------------------------------------------------------------
    # Filter Fields
    # ------------------------------------------------------------------

    status = fields.Str(
        load_default="published",
        validate=validate.OneOf(
            choices=_ALL_JOB_STATUSES,
            error="status must be one of: " + ", ".join(_ALL_JOB_STATUSES) + ".",
        ),
        metadata={
            "description": (
                "Job status filter. Defaults to 'published'. "
                "Candidates always receive published jobs only."
            )
        },
    )

    job_type = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_ALL_JOB_TYPES,
            error="job_type must be one of: " + ", ".join(_ALL_JOB_TYPES) + ".",
        ),
        metadata={"description": "Filter by employment type."},
    )

    experience_level = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_ALL_EXPERIENCE_LEVELS,
            error=(
                "experience_level must be one of: "
                + ", ".join(_ALL_EXPERIENCE_LEVELS) + "."
            ),
        ),
        metadata={"description": "Filter by required experience level."},
    )

    is_remote = fields.Bool(
        load_default=None,
        allow_none=True,
        metadata={"description": "True = only remote jobs; False = only on-site."},
    )

    location = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(
            max=_LOCATION_MAX_LEN,
            error=f"location filter must not exceed {_LOCATION_MAX_LEN} characters.",
        ),
        metadata={
            "description": (
                "Partial location match (case-insensitive). "
                'Example: "New York" matches "New York, NY".'
            )
        },
    )

    skills = fields.List(
        fields.Str(validate=validate.Length(min=1, max=_MAX_SKILL_LEN)),
        load_default=list,
        validate=validate.Length(
            max=20,
            error="Cannot filter by more than 20 skills at once.",
        ),
        metadata={
            "description": (
                "Filter to jobs that require ALL listed skills. "
                "Repeat the parameter for multiple skills: "
                "?skills=Python&skills=Docker"
            )
        },
    )

    salary_min = fields.Float(
        load_default=None,
        allow_none=True,
        validate=validate.Range(
            min=0.0,
            error="salary_min filter must be >= 0.",
        ),
        metadata={"description": "Minimum salary filter (inclusive)."},
    )

    salary_max = fields.Float(
        load_default=None,
        allow_none=True,
        validate=validate.Range(
            min=0.0,
            error="salary_max filter must be >= 0.",
        ),
        metadata={"description": "Maximum salary filter (inclusive)."},
    )

    salary_currency = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_ALL_CURRENCIES,
            error=(
                "salary_currency must be a supported ISO 4217 code. "
                "Supported: " + ", ".join(_ALL_CURRENCIES) + "."
            ),
        ),
        metadata={"description": "Filter by salary currency."},
    )

    search = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(
            max=500,
            error="search term must not exceed 500 characters.",
        ),
        metadata={
            "description": (
                "Free-text search term. "
                "Matched against job title and description (case-insensitive)."
            )
        },
    )

    sort_by = fields.Str(
        load_default="newest",
        validate=validate.OneOf(
            choices=[
                "newest",
                "oldest",
                "salary_asc",
                "salary_desc",
                "applications_asc",
                "applications_desc",
            ],
            error=(
                "sort_by must be one of: newest, oldest, salary_asc, "
                "salary_desc, applications_asc, applications_desc."
            ),
        ),
        metadata={
            "description": (
                "Sort order. "
                "One of: newest (default), oldest, salary_asc, salary_desc, "
                "applications_asc, applications_desc."
            )
        },
    )

    # ------------------------------------------------------------------
    # Pagination Fields
    # ------------------------------------------------------------------

    page = fields.Int(
        load_default=_DEFAULT_PAGE,
        validate=validate.Range(
            min=1,
            error="page must be >= 1.",
        ),
        metadata={"description": f"Page number (1-based). Default: {_DEFAULT_PAGE}."},
    )

    per_page = fields.Int(
        load_default=_DEFAULT_PER_PAGE,
        validate=validate.Range(
            min=1,
            max=_MAX_PER_PAGE,
            error=f"per_page must be between 1 and {_MAX_PER_PAGE}.",
        ),
        metadata={
            "description": (
                f"Results per page. "
                f"Default: {_DEFAULT_PER_PAGE}. Max: {_MAX_PER_PAGE}."
            )
        },
    )

    # ------------------------------------------------------------------
    # Pre-load: normalise query-string values
    # ------------------------------------------------------------------

    @pre_load
    def normalise_input(self, data: dict, **kwargs: Any) -> dict:
        """
        Normalise raw query-string values before field-level validation.

        Query strings arrive as strings (or lists of strings). Marshmallow's
        fields.Int and fields.Bool handle coercion from string automatically,
        but enum fields need lowercase normalisation to be forgiving.
        """
        if not isinstance(data, dict):
            return data

        # Lowercase enum fields for forgiving matching
        for field in ("status", "job_type", "experience_level", "sort_by"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip().lower()

        # Currency: strip and uppercase
        if "salary_currency" in data and isinstance(data["salary_currency"], str):
            data["salary_currency"] = data["salary_currency"].strip().upper()

        # Strip location and search strings
        for field in ("location", "search"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip() or None

        # Normalise skills list entries
        if "skills" in data:
            data["skills"] = _normalise_skills(data["skills"])

        return data

    # ------------------------------------------------------------------
    # Cross-field validators
    # ------------------------------------------------------------------

    @validates_schema
    def validate_salary_filter_range(self, data: dict, **kwargs: Any) -> None:
        """
        Reject filter requests where salary_max < salary_min.

        This would return zero results and indicates a client error.
        Surfacing it as a validation error saves a DB round-trip.
        """
        salary_min = data.get("salary_min")
        salary_max = data.get("salary_max")
        if salary_min is not None and salary_max is not None:
            if salary_max < salary_min:
                raise ValidationError(
                    {
                        "salary_max": (
                            "salary_max filter must be >= salary_min filter. "
                            f"Got salary_min={salary_min}, salary_max={salary_max}."
                        )
                    }
                )


# ---------------------------------------------------------------------------
# JobStatusTransitionSchema  (Batch 3A)
# ---------------------------------------------------------------------------


class JobStatusTransitionSchema(Schema):
    """
    Validate status transition endpoints:
      POST /jobs/<job_id>/publish
      POST /jobs/<job_id>/pause
      POST /jobs/<job_id>/close
      POST /jobs/<job_id>/archive

    The target status is derived from the URL endpoint in the route handler;
    this schema validates the optional request body which may contain a
    recruiter-facing note about the reason for the transition.

    Accepted fields (all optional):
      note : string — recruiter's reason for the transition (stored in audit log)

    Design rationale:
      - The target status is intentionally NOT a field in this schema. The route
        handler passes the target status from the URL path (e.g., "published"
        from POST /publish) to the service. Accepting the status in the body
        would allow clients to pass a contradictory status (e.g., POST /pause
        with body {"target_status": "published"}).
      - The schema exists primarily to validate the optional `note` field and
        to provide a consistent schema-validation pattern across all endpoints.

    Example valid payload:
      {
        "note": "Sufficient applicants received — closing the role."
      }

    Example valid payload (empty body):
      {}

    Both are accepted — the note is always optional.
    """

    class Meta:
        unknown = EXCLUDE

    note = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(
            max=500,
            error="Transition note must not exceed 500 characters.",
        ),
        metadata={
            "description": (
                "Optional recruiter note explaining the reason for this status "
                "transition. Stored in the audit log for compliance."
            )
        },
    )

    @pre_load
    def normalise_note(self, data: dict, **kwargs: Any) -> dict:
        """Strip whitespace from the note field; treat blank as None."""
        if not isinstance(data, dict):
            return data
        if "note" in data and isinstance(data["note"], str):
            data["note"] = data["note"].strip() or None
        return data

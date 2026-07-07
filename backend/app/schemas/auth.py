"""
schemas/auth.py
---------------
Marshmallow schemas for all authentication endpoints.

Provides request validation for:
  - CandidateRegistrationSchema : POST /auth/register/candidate
  - RecruiterRegistrationSchema : POST /auth/register/recruiter
  - LoginSchema                 : POST /auth/login
  - ForgotPasswordSchema        : POST /auth/forgot-password   (Batch 2B)
  - ResetPasswordSchema         : POST /auth/reset-password    (Batch 2B)
  - ResendVerificationSchema    : POST /auth/resend-verification (Batch 2B)

Design decisions:
  - Input is normalized (email lowercased, strings stripped) via @pre_load
    so the service layer always receives clean, consistent data.
  - Password is marked load_only=True so it can never appear in a
    serialized (dump) output — an extra safeguard against accidental exposure.
  - Phone is validated for basic length sanity only; no E.164 enforcement
    at this layer (different countries have wildly varying formats).
  - company_name is required for RecruiterRegistrationSchema because the
    RecruiterProfile model enforces nullable=False on that column.
  - All `validate` arguments use marshmallow's built-in validators where
    possible, and @validates methods for more complex field-level logic.
  - Field names match the model column names exactly to simplify service-layer
    mapping with **data unpacking.

Marshmallow version: 3.26.x (schema-level unknown field handling via Meta.unknown)
"""

import re
from typing import Any

from marshmallow import (
    EXCLUDE,
    RAISE,
    Schema,
    ValidationError,
    fields,
    pre_load,
    validate,
    validates,
)


# ---------------------------------------------------------------------------
# Reusable Validators
# ---------------------------------------------------------------------------

# Minimum password length aligned with NIST SP 800-63B guidance.
_MIN_PASSWORD_LEN: int = 8
_MAX_PASSWORD_LEN: int = 128  # Prevents bcrypt truncation at 72 bytes via length guard

# Basic phone sanity — digits, spaces, hyphens, parentheses, plus sign.
_PHONE_RE: re.Pattern = re.compile(r"^[\d\s\-()+]{7,20}$")

# Disallow control characters and leading/trailing whitespace in name fields.
_NAME_MAX_LEN: int = 100
_EMAIL_MAX_LEN: int = 255


# ---------------------------------------------------------------------------
# Base Registration Schema
# ---------------------------------------------------------------------------


class _BaseRegistrationSchema(Schema):
    """
    Shared fields for both candidate and recruiter registration.

    Not instantiated directly — subclassed by CandidateRegistrationSchema
    and RecruiterRegistrationSchema.
    """

    class Meta:
        # Raise ValidationError for any unrecognised request field.
        # This prevents clients from passing extra fields that might be
        # mistakenly processed by a future code path.
        unknown = RAISE

    # --- Required identity fields ---

    email = fields.Email(
        required=True,
        validate=validate.Length(max=_EMAIL_MAX_LEN),
        metadata={"description": "Valid email address. Used as login identifier."},
    )

    password = fields.Str(
        required=True,
        load_only=True,   # NEVER appears in serialized output
        validate=validate.Length(
            min=_MIN_PASSWORD_LEN,
            max=_MAX_PASSWORD_LEN,
            error=(
                f"Password must be between {_MIN_PASSWORD_LEN} "
                f"and {_MAX_PASSWORD_LEN} characters."
            ),
        ),
        metadata={"description": "Plaintext password. Stored as a bcrypt hash."},
    )

    first_name = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=_NAME_MAX_LEN),
        metadata={"description": "Given name."},
    )

    last_name = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=_NAME_MAX_LEN),
        metadata={"description": "Family name."},
    )

    phone = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=20),
        metadata={"description": "Optional phone number."},
    )

    # ------------------------------------------------------------------
    # Pre-load: Normalise input before field-level validation runs
    # ------------------------------------------------------------------

    @pre_load
    def normalise_input(self, data: dict, **kwargs: Any) -> dict:
        """
        Normalise raw request data before field-level validation.

        - Email: strip whitespace and lowercase.
        - String fields: strip leading/trailing whitespace.
        - Phone: convert empty string to None.
        """
        if not isinstance(data, dict):
            return data

        if "email" in data and isinstance(data["email"], str):
            data["email"] = data["email"].strip().lower()

        for field in ("first_name", "last_name"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip()

        # Normalise phone: treat blank string as absent
        if "phone" in data:
            if isinstance(data["phone"], str):
                data["phone"] = data["phone"].strip() or None

        return data

    # ------------------------------------------------------------------
    # Field-level validators
    # ------------------------------------------------------------------

    @validates("first_name")
    def validate_first_name(self, value: str) -> None:
        """Reject names that consist entirely of whitespace or digits."""
        if not value or not value.strip():
            raise ValidationError("First name must not be blank.")
        if value.strip().isdigit():
            raise ValidationError("First name must contain at least one letter.")

    @validates("last_name")
    def validate_last_name(self, value: str) -> None:
        """Reject names that consist entirely of whitespace or digits."""
        if not value or not value.strip():
            raise ValidationError("Last name must not be blank.")
        if value.strip().isdigit():
            raise ValidationError("Last name must contain at least one letter.")

    @validates("phone")
    def validate_phone(self, value: str | None) -> None:
        """Validate phone number format if provided."""
        if value is None:
            return
        if not _PHONE_RE.match(value):
            raise ValidationError(
                "Phone number must be 7–20 characters and may only contain "
                "digits, spaces, hyphens, parentheses, and the plus sign."
            )

    @validates("password")
    def validate_password_strength(self, value: str) -> None:
        """
        Enforce minimal password complexity beyond length.

        Requires at least one letter and one digit. This is a lightweight
        check — full password strength meters belong on the frontend.
        """
        has_letter = any(c.isalpha() for c in value)
        has_digit = any(c.isdigit() for c in value)
        if not has_letter or not has_digit:
            raise ValidationError(
                "Password must contain at least one letter and one digit."
            )


# ---------------------------------------------------------------------------
# Candidate Registration Schema
# ---------------------------------------------------------------------------


class CandidateRegistrationSchema(_BaseRegistrationSchema):
    """
    Validate POST /auth/register/candidate request body.

    Accepted fields (all others raise ValidationError):
      email       : string, required, valid email format
      password    : string, required, 8–128 chars, load_only
      first_name  : string, required, 1–100 chars
      last_name   : string, required, 1–100 chars
      phone       : string, optional, 7–20 chars

    Example valid payload:
      {
        "email": "jane@example.com",
        "password": "Secure123",
        "first_name": "Jane",
        "last_name": "Doe"
      }
    """
    # Candidate registration has no additional required fields beyond the base.
    # Optional profile fields (headline, availability, etc.) are set via the
    # candidate profile update endpoint after registration.
    pass


# ---------------------------------------------------------------------------
# Recruiter Registration Schema
# ---------------------------------------------------------------------------


class RecruiterRegistrationSchema(_BaseRegistrationSchema):
    """
    Validate POST /auth/register/recruiter request body.

    Accepted fields:
      email          : string, required, valid email format
      password       : string, required, 8–128 chars, load_only
      first_name     : string, required, 1–100 chars
      last_name      : string, required, 1–100 chars
      phone          : string, optional
      company_name   : string, required — maps to recruiter_profiles.company_name (NOT NULL)
      company_size   : string, optional — one of the RecruiterProfile size constants
      industry       : string, optional
      designation    : string, optional

    Example valid payload:
      {
        "email": "hr@acme.com",
        "password": "Secure123",
        "first_name": "Alice",
        "last_name": "Smith",
        "company_name": "Acme Corp"
      }
    """

    company_name = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=255),
        metadata={"description": "Company name. Required for recruiter accounts."},
    )

    company_size = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=["startup", "small", "medium", "large", "enterprise"],
            error=(
                "company_size must be one of: "
                "startup, small, medium, large, enterprise."
            ),
        ),
        metadata={"description": "Company size band. Optional."},
    )

    industry = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=100),
        metadata={"description": "Industry or sector. Optional."},
    )

    designation = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=255),
        metadata={"description": "Recruiter's job title. Optional."},
    )

    # ------------------------------------------------------------------
    # Pre-load override: normalise recruiter-specific fields in addition
    # to the base fields handled by the parent @pre_load hook.
    # ------------------------------------------------------------------

    @pre_load
    def normalise_input(self, data: dict, **kwargs: Any) -> dict:
        """Extend base normalisation with recruiter-specific field stripping."""
        # Run base normalisation first
        data = super().normalise_input(data, **kwargs)

        for field in ("company_name", "industry", "designation"):
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip() or None

        # company_name is required — if it stripped to None/empty, leave it
        # so the required-field validation catches it with a clear message.
        if "company_name" in data and not data.get("company_name"):
            data["company_name"] = ""  # triggers Length(min=1) validation

        return data

    @validates("company_name")
    def validate_company_name(self, value: str) -> None:
        """Reject blank company names."""
        if not value or not value.strip():
            raise ValidationError("Company name must not be blank.")


# ---------------------------------------------------------------------------
# Login Schema
# ---------------------------------------------------------------------------


class LoginSchema(Schema):
    """
    Validate POST /auth/login request body.

    Accepted fields:
      email    : string, required, valid email format
      password : string, required, load_only

    Extra fields are silently excluded (EXCLUDE) rather than rejected
    because some clients may send additional fields (e.g. device_id)
    that will be used in future stages.

    Example valid payload:
      {
        "email": "jane@example.com",
        "password": "Secure123"
      }
    """

    class Meta:
        # EXCLUDE rather than RAISE — login is a well-known endpoint and
        # some clients may include extra context (device type, timezone).
        unknown = EXCLUDE

    email = fields.Email(
        required=True,
        validate=validate.Length(max=_EMAIL_MAX_LEN),
        metadata={"description": "Registered email address."},
    )

    password = fields.Str(
        required=True,
        load_only=True,
        validate=validate.Length(min=1, max=_MAX_PASSWORD_LEN),
        metadata={"description": "Account password."},
    )

    @pre_load
    def normalise_email(self, data: dict, **kwargs: Any) -> dict:
        """Normalise email to lowercase before validation."""
        if not isinstance(data, dict):
            return data
        if "email" in data and isinstance(data["email"], str):
            data["email"] = data["email"].strip().lower()
        return data


# ---------------------------------------------------------------------------
# Forgot Password Schema
# ---------------------------------------------------------------------------


class ForgotPasswordSchema(Schema):
    """
    Validate POST /auth/forgot-password request body.

    Deliberately simple: only an email address is required.
    The route always returns 200 regardless of whether the email
    is registered, to prevent user enumeration.

    Accepted fields:
      email : string, required, valid email format

    Extra fields are excluded (EXCLUDE) for forwards compatibility.
    """

    class Meta:
        unknown = EXCLUDE

    email = fields.Email(
        required=True,
        validate=validate.Length(max=_EMAIL_MAX_LEN),
        metadata={"description": "The email address associated with the account."},
    )

    @pre_load
    def normalise_email(self, data: dict, **kwargs: Any) -> dict:
        """Normalise email to lowercase before validation."""
        if not isinstance(data, dict):
            return data
        if "email" in data and isinstance(data["email"], str):
            data["email"] = data["email"].strip().lower()
        return data


# ---------------------------------------------------------------------------
# Reset Password Schema
# ---------------------------------------------------------------------------


class ResetPasswordSchema(Schema):
    """
    Validate POST /auth/reset-password request body.

    Accepted fields:
      token        : string, required — secure token from the reset email
      new_password : string, required — 8-128 chars, at least 1 letter + 1 digit

    Extra fields are excluded (EXCLUDE) for forwards compatibility.

    Example valid payload:
      {
        "token": "abc123...",
        "new_password": "NewSecure456"
      }
    """

    class Meta:
        unknown = EXCLUDE

    token = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=512),
        metadata={"description": "Password reset token from the reset email link."},
    )

    new_password = fields.Str(
        required=True,
        load_only=True,
        validate=validate.Length(
            min=_MIN_PASSWORD_LEN,
            max=_MAX_PASSWORD_LEN,
            error=(
                f"Password must be between {_MIN_PASSWORD_LEN} "
                f"and {_MAX_PASSWORD_LEN} characters."
            ),
        ),
        metadata={"description": "The new plaintext password. Will be bcrypt-hashed."},
    )

    @pre_load
    def strip_token(self, data: dict, **kwargs: Any) -> dict:
        """Strip accidental whitespace from the token string."""
        if not isinstance(data, dict):
            return data
        if "token" in data and isinstance(data["token"], str):
            data["token"] = data["token"].strip()
        return data

    @validates("new_password")
    def validate_password_strength(self, value: str) -> None:
        """Require at least one letter and one digit."""
        has_letter = any(c.isalpha() for c in value)
        has_digit = any(c.isdigit() for c in value)
        if not has_letter or not has_digit:
            raise ValidationError(
                "Password must contain at least one letter and one digit."
            )


# ---------------------------------------------------------------------------
# Resend Verification Schema
# ---------------------------------------------------------------------------


class ResendVerificationSchema(Schema):
    """
    Validate POST /auth/resend-verification request body.

    Accepted fields:
      email : string, required, valid email format

    The route always returns 200 regardless of whether the email is
    registered or already verified, to prevent enumeration.

    Extra fields are excluded (EXCLUDE) for forwards compatibility.
    """

    class Meta:
        unknown = EXCLUDE

    email = fields.Email(
        required=True,
        validate=validate.Length(max=_EMAIL_MAX_LEN),
        metadata={"description": "The email address to resend verification to."},
    )

    @pre_load
    def normalise_email(self, data: dict, **kwargs: Any) -> dict:
        """Normalise email to lowercase before validation."""
        if not isinstance(data, dict):
            return data
        if "email" in data and isinstance(data["email"], str):
            data["email"] = data["email"].strip().lower()
        return data


# ---------------------------------------------------------------------------
# Change Password Schema  (Batch 2D)
# ---------------------------------------------------------------------------


class ChangePasswordSchema(Schema):
    """
    Validate POST /auth/change-password request body.

    Requires the caller's current password for re-authentication so that
    a stolen session token alone cannot be used to change the password.

    Accepted fields:
      current_password : string, required, load_only
      new_password     : string, required, load_only, 8-128 chars
    """

    class Meta:
        unknown = EXCLUDE

    current_password = fields.Str(
        required=True,
        load_only=True,
        validate=validate.Length(min=1, max=_MAX_PASSWORD_LEN),
        metadata={"description": "The user's current password for re-authentication."},
    )

    new_password = fields.Str(
        required=True,
        load_only=True,
        validate=validate.Length(
            min=_MIN_PASSWORD_LEN,
            max=_MAX_PASSWORD_LEN,
            error=(
                f"New password must be between {_MIN_PASSWORD_LEN} "
                f"and {_MAX_PASSWORD_LEN} characters."
            ),
        ),
        metadata={"description": "The replacement password. Will be bcrypt-hashed."},
    )

    @validates("new_password")
    def validate_new_password_strength(self, value: str) -> None:
        """Require at least one letter and one digit."""
        has_letter = any(c.isalpha() for c in value)
        has_digit = any(c.isdigit() for c in value)
        if not has_letter or not has_digit:
            raise ValidationError(
                "New password must contain at least one letter and one digit."
            )

    def validate_passwords_different(
        self, data: dict, **kwargs: Any
    ) -> None:
        """Reject new_password == current_password (post-load cross-field check)."""
        cur = data.get("current_password", "")
        new = data.get("new_password", "")
        if cur and new and cur == new:
            raise ValidationError(
                {"new_password": ["New password must differ from the current password."]}
            )


# ---------------------------------------------------------------------------
# Change Email Schema  (Batch 2D)
# ---------------------------------------------------------------------------


class ChangeEmailSchema(Schema):
    """
    Validate POST /auth/change-email request body.

    Changing email re-triggers the email verification flow: the new address
    is stored unverified and a verification link is dispatched.
    The current password must be confirmed to prevent account takeover
    via a stolen session token.

    Accepted fields:
      new_email        : string, required, valid email format
      current_password : string, required, load_only
    """

    class Meta:
        unknown = EXCLUDE

    new_email = fields.Email(
        required=True,
        validate=validate.Length(max=_EMAIL_MAX_LEN),
        metadata={"description": "The new email address to switch to."},
    )

    current_password = fields.Str(
        required=True,
        load_only=True,
        validate=validate.Length(min=1, max=_MAX_PASSWORD_LEN),
        metadata={"description": "Current password for re-authentication."},
    )

    @pre_load
    def normalise_input(self, data: dict, **kwargs: Any) -> dict:
        """Normalise new_email to lowercase and strip whitespace."""
        if not isinstance(data, dict):
            return data
        if "new_email" in data and isinstance(data["new_email"], str):
            data["new_email"] = data["new_email"].strip().lower()
        return data


# ---------------------------------------------------------------------------
# Update Candidate Profile Schema  (Batch 2D)
# ---------------------------------------------------------------------------


_URL_MAX_LEN: int = 500
_CANDIDATE_AVAILABILITY_CHOICES = [
    "immediately", "two_weeks", "one_month", "not_looking",
]


class UpdateCandidateProfileSchema(Schema):
    """
    Validate PATCH /auth/profile/candidate request body.

    All fields are optional — the client sends only the fields to update.
    Omitted fields are left unchanged in the database (partial update).

    Accepted fields (all optional):
      first_name          : string, 1-100 chars
      last_name           : string, 1-100 chars
      phone               : string, optional
      headline            : string, max 255 chars
      summary             : string (Text — no max enforced here)
      location            : string, max 255 chars
      linkedin_url        : URL string, max 500 chars
      github_url          : URL string, max 500 chars
      portfolio_url       : URL string, max 500 chars
      years_of_experience : float, >= 0, <= 50
      availability        : one of the CandidateProfile availability constants
    """

    class Meta:
        unknown = EXCLUDE

    # -- User table fields --
    first_name = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(min=1, max=_NAME_MAX_LEN),
        metadata={"description": "Given name."},
    )

    last_name = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(min=1, max=_NAME_MAX_LEN),
        metadata={"description": "Family name."},
    )

    phone = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=20),
        metadata={"description": "Optional phone number."},
    )

    # -- Candidate profile table fields --
    headline = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=255),
        metadata={"description": "Professional headline."},
    )

    summary = fields.Str(
        load_default=None,
        allow_none=True,
        metadata={"description": "Professional summary / bio."},
    )

    location = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=255),
        metadata={"description": "City and/or country."},
    )

    linkedin_url = fields.Url(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_URL_MAX_LEN),
        metadata={"description": "LinkedIn profile URL."},
    )

    github_url = fields.Url(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_URL_MAX_LEN),
        metadata={"description": "GitHub profile URL."},
    )

    portfolio_url = fields.Url(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_URL_MAX_LEN),
        metadata={"description": "Portfolio/personal site URL."},
    )

    years_of_experience = fields.Float(
        load_default=None,
        allow_none=True,
        validate=validate.Range(
            min=0,
            max=50,
            error="years_of_experience must be between 0 and 50.",
        ),
        metadata={"description": "Total years of professional experience."},
    )

    availability = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_CANDIDATE_AVAILABILITY_CHOICES,
            error=(
                "availability must be one of: "
                + ", ".join(_CANDIDATE_AVAILABILITY_CHOICES) + "."
            ),
        ),
        metadata={"description": "Current job-search availability status."},
    )

    @pre_load
    def normalise_input(self, data: dict, **kwargs: Any) -> dict:
        """Strip whitespace from string fields; convert empty strings to None."""
        if not isinstance(data, dict):
            return data
        str_fields = (
            "first_name", "last_name", "phone", "headline",
            "summary", "location",
        )
        for field in str_fields:
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip() or None
        url_fields = ("linkedin_url", "github_url", "portfolio_url")
        for field in url_fields:
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip() or None
        return data

    @validates("first_name")
    def validate_first_name(self, value: str | None) -> None:
        if value is not None and not value.strip():
            raise ValidationError("First name must not be blank.")

    @validates("last_name")
    def validate_last_name(self, value: str | None) -> None:
        if value is not None and not value.strip():
            raise ValidationError("Last name must not be blank.")

    @validates("phone")
    def validate_phone(self, value: str | None) -> None:
        if value is None:
            return
        if not _PHONE_RE.match(value):
            raise ValidationError(
                "Phone number must be 7–20 characters and may only contain "
                "digits, spaces, hyphens, parentheses, and the plus sign."
            )


# ---------------------------------------------------------------------------
# Update Recruiter Profile Schema  (Batch 2D)
# ---------------------------------------------------------------------------


_RECRUITER_SIZE_CHOICES = ["startup", "small", "medium", "large", "enterprise"]


class UpdateRecruiterProfileSchema(Schema):
    """
    Validate PATCH /auth/profile/recruiter request body.

    All fields are optional — the client sends only the fields to update.

    Accepted fields (all optional):
      first_name      : string, 1-100 chars
      last_name       : string, 1-100 chars
      phone           : string, optional
      company_name    : string, 1-255 chars (required at DB level, validated here)
      company_website : URL string, max 500 chars
      company_size    : one of the RecruiterProfile size constants
      industry        : string, max 100 chars
      designation     : string, max 255 chars
    """

    class Meta:
        unknown = EXCLUDE

    # -- User table fields --
    first_name = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(min=1, max=_NAME_MAX_LEN),
        metadata={"description": "Given name."},
    )

    last_name = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(min=1, max=_NAME_MAX_LEN),
        metadata={"description": "Family name."},
    )

    phone = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=20),
        metadata={"description": "Optional phone number."},
    )

    # -- Recruiter profile table fields --
    company_name = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(min=1, max=255),
        metadata={"description": "Company name."},
    )

    company_website = fields.Url(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=_URL_MAX_LEN),
        metadata={"description": "Company website URL."},
    )

    company_size = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            choices=_RECRUITER_SIZE_CHOICES,
            error=(
                "company_size must be one of: "
                + ", ".join(_RECRUITER_SIZE_CHOICES) + "."
            ),
        ),
        metadata={"description": "Approximate number of employees."},
    )

    industry = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=100),
        metadata={"description": "Industry or sector."},
    )

    designation = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=255),
        metadata={"description": "Recruiter's job title."},
    )

    @pre_load
    def normalise_input(self, data: dict, **kwargs: Any) -> dict:
        """Strip whitespace from string fields; convert empty strings to None."""
        if not isinstance(data, dict):
            return data
        str_fields = (
            "first_name", "last_name", "phone",
            "company_name", "industry", "designation",
        )
        for field in str_fields:
            if field in data and isinstance(data[field], str):
                data[field] = data[field].strip() or None
        if "company_website" in data and isinstance(data["company_website"], str):
            data["company_website"] = data["company_website"].strip() or None
        return data

    @validates("first_name")
    def validate_first_name(self, value: str | None) -> None:
        if value is not None and not value.strip():
            raise ValidationError("First name must not be blank.")

    @validates("last_name")
    def validate_last_name(self, value: str | None) -> None:
        if value is not None and not value.strip():
            raise ValidationError("Last name must not be blank.")

    @validates("phone")
    def validate_phone(self, value: str | None) -> None:
        if value is None:
            return
        if not _PHONE_RE.match(value):
            raise ValidationError(
                "Phone number must be 7–20 characters and may only contain "
                "digits, spaces, hyphens, parentheses, and the plus sign."
            )

    @validates("company_name")
    def validate_company_name(self, value: str | None) -> None:
        if value is not None and not value.strip():
            raise ValidationError("Company name must not be blank.")


# ---------------------------------------------------------------------------
# Account Deactivation Schema  (Batch 2E)
# ---------------------------------------------------------------------------


class AccountDeactivationSchema(Schema):
    """
    Validate POST /auth/deactivate request body.

    Requires the caller's current password so that a stolen session token
    cannot silently deactivate an account. The account is soft-deactivated
    (is_active=False) rather than deleted so that the history is preserved
    and the account can be reactivated by an admin.

    Accepted fields:
      current_password : string, required, load_only
      reason           : string, optional — user's stated reason for deactivation
    """

    class Meta:
        unknown = EXCLUDE

    current_password = fields.Str(
        required=True,
        load_only=True,
        validate=validate.Length(min=1, max=_MAX_PASSWORD_LEN),
        metadata={"description": "Current password for identity re-confirmation."},
    )

    reason = fields.Str(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=500),
        metadata={
            "description": (
                "Optional reason for deactivation. Stored in the audit log."
            )
        },
    )

    @pre_load
    def strip_reason(self, data: dict, **kwargs: Any) -> dict:
        """Strip whitespace from the optional reason field."""
        if not isinstance(data, dict):
            return data
        if "reason" in data and isinstance(data["reason"], str):
            data["reason"] = data["reason"].strip() or None
        return data


# ---------------------------------------------------------------------------
# Token Introspect Schema  (Batch 2E)
# ---------------------------------------------------------------------------


class TokenIntrospectSchema(Schema):
    """
    Validate POST /auth/introspect request body.

    Accepts a raw JWT string and returns its decoded claims if valid.
    This is a developer/debugging endpoint — it does NOT modify any state.

    Accepted fields:
      token : string, required — the raw JWT to inspect
    """

    class Meta:
        unknown = EXCLUDE

    token = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=4096),
        metadata={"description": "The raw JWT string to introspect."},
    )

    @pre_load
    def strip_token(self, data: dict, **kwargs: Any) -> dict:
        """Strip accidental whitespace from the token string."""
        if not isinstance(data, dict):
            return data
        if "token" in data and isinstance(data["token"], str):
            data["token"] = data["token"].strip()
        return data


# ---------------------------------------------------------------------------
# Update Security Settings Schema  (Batch 2E)
# ---------------------------------------------------------------------------


class UpdateSecuritySettingsSchema(Schema):
    """
    Validate PATCH /auth/security-settings request body.

    All fields are optional (partial update). Controls per-user security
    preferences that do not require changing core credentials.

    Accepted fields (all optional):
      login_notifications  : bool — receive an email on each new login
      session_timeout_hours: int  — preferred session inactivity timeout (1–168 h)
    """

    class Meta:
        unknown = EXCLUDE

    login_notifications = fields.Bool(
        load_default=None,
        allow_none=True,
        metadata={
            "description": (
                "If True, send an email notification on each successful login."
            )
        },
    )

    session_timeout_hours = fields.Int(
        load_default=None,
        allow_none=True,
        validate=validate.Range(
            min=1,
            max=168,
            error="session_timeout_hours must be between 1 and 168 (7 days).",
        ),
        metadata={
            "description": (
                "Preferred session inactivity timeout in hours (1–168). "
                "This is a preference only; enforcement is in the JWT TTL config."
            )
        },
    )


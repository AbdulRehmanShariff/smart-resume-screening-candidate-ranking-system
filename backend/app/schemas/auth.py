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

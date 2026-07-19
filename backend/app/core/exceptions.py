"""
core/exceptions.py
------------------
Custom exception hierarchy for the Smart Resume Screening System.

All application exceptions inherit from AppException. Each subclass maps
to a specific HTTP status code and carries a default user-facing message.
The application factory registers a global handler that converts these
exceptions into standard JSON error responses automatically.

Raising an exception from any route, service, or utility will automatically
produce the correct HTTP response — no try/except boilerplate needed in routes.

Usage:
  from app.core.exceptions import NotFoundError, ValidationError

  raise NotFoundError("Job posting not found")
  raise ValidationError("Email is already registered", errors={"email": ["Already in use"]})
"""

from http import HTTPStatus
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Base Exception
# ---------------------------------------------------------------------------


class AppException(Exception):
    """
    Base exception for all application-level errors.

    Attributes:
        status_code     : HTTP status code to return in the response.
        default_message : Fallback message if none is provided at raise time.
        message         : The actual message for this exception instance.
        errors          : Optional field-level or structured error details.
    """

    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR.value
    default_message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: Optional[str] = None,
        errors: Any = None,
    ) -> None:
        """
        Args:
            message : Override the default message for this instance.
            errors  : Optional error details (dict of field errors, list, etc.).
        """
        self.message: str = message if message is not None else self.default_message
        self.errors: Any = errors
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """
        Serialize the exception to a JSON-safe dictionary.
        Used by the global error handler in the application factory.

        Returns:
            A dict with `success`, `message`, and optionally `errors`.
        """
        payload: dict = {
            "success": False,
            "message": self.message,
        }
        if self.errors is not None:
            payload["errors"] = self.errors
        return payload

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"status_code={self.status_code}, "
            f"message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# 400 Bad Request
# ---------------------------------------------------------------------------


class BadRequestError(AppException):
    """
    Raised when the request is malformed or contains invalid parameters
    that cannot be processed even after validation passes.

    HTTP 400 Bad Request.
    """

    status_code: int = HTTPStatus.BAD_REQUEST.value
    default_message: str = "Invalid request. Please check your input and try again"


# ---------------------------------------------------------------------------
# 401 Unauthorized
# ---------------------------------------------------------------------------


class AuthenticationError(AppException):
    """
    Raised when a request requires authentication and the provided
    credentials are missing, invalid, or expired.

    HTTP 401 Unauthorized.
    """

    status_code: int = HTTPStatus.UNAUTHORIZED.value
    default_message: str = "Authentication failed. Please log in and try again"


# ---------------------------------------------------------------------------
# 403 Forbidden
# ---------------------------------------------------------------------------


class AuthorizationError(AppException):
    """
    Raised when an authenticated user attempts an action they do not have
    permission to perform (role-based access control failure).

    HTTP 403 Forbidden.
    """

    status_code: int = HTTPStatus.FORBIDDEN.value
    default_message: str = "You do not have permission to perform this action"


# ---------------------------------------------------------------------------
# 404 Not Found
# ---------------------------------------------------------------------------


class NotFoundError(AppException):
    """
    Raised when a requested resource does not exist in the database.

    HTTP 404 Not Found.
    """

    status_code: int = HTTPStatus.NOT_FOUND.value
    default_message: str = "The requested resource was not found"


# ---------------------------------------------------------------------------
# 409 Conflict
# ---------------------------------------------------------------------------


class ConflictError(AppException):
    """
    Raised when an action cannot be completed because it conflicts with
    the current state of the resource — for example, registering with an
    email address that already exists.

    HTTP 409 Conflict.
    """

    status_code: int = HTTPStatus.CONFLICT.value
    default_message: str = "A conflict occurred with the current state of the resource"


# ---------------------------------------------------------------------------
# 422 Unprocessable Entity
# ---------------------------------------------------------------------------


class ValidationError(AppException):
    """
    Raised when input data fails business-level validation after passing
    schema-level checks. Carries field-level error details in `errors`.

    HTTP 422 Unprocessable Entity.
    """

    status_code: int = HTTPStatus.UNPROCESSABLE_ENTITY.value
    default_message: str = "Request validation failed"


# ---------------------------------------------------------------------------
# 429 Too Many Requests
# ---------------------------------------------------------------------------


class RateLimitError(AppException):
    """
    Raised when a client exceeds the allowed request rate.

    HTTP 429 Too Many Requests.
    """

    status_code: int = HTTPStatus.TOO_MANY_REQUESTS.value
    default_message: str = "Too many requests. Please wait before trying again"


# ---------------------------------------------------------------------------
# 503 Service Unavailable
# ---------------------------------------------------------------------------


class ServiceUnavailableError(AppException):
    """
    Raised when an external dependency (AI service, email provider, etc.)
    is temporarily unavailable.

    HTTP 503 Service Unavailable.
    """

    status_code: int = HTTPStatus.SERVICE_UNAVAILABLE.value
    default_message: str = (
        "The service is temporarily unavailable. Please try again later"
    )


# ---------------------------------------------------------------------------
# 500 Internal Server Error (explicit)
# ---------------------------------------------------------------------------


class InternalError(AppException):
    """
    Raised explicitly when an unexpected internal failure occurs that is
    not covered by a more specific exception. Prefer specific subclasses
    whenever possible.

    HTTP 500 Internal Server Error.
    """

    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR.value
    default_message: str = "An internal error occurred. Please contact support"


# ---------------------------------------------------------------------------
# AI Pipeline Control Flow Exceptions
# ---------------------------------------------------------------------------

class ResumeNotReadyError(Exception):
    """
    Internal flow-control exception raised by the AI dispatcher when an
    APPLICATION_RANK job executes before its corresponding RESUME_PARSE
    job has finished.
    
    The AI worker catches this and safely requeues the job with a short
    delay without consuming retry budgets or logging it as a failure.
    """
    pass

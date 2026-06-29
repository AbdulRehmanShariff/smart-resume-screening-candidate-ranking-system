"""
core/responses.py
-----------------
Standard API response envelope for all REST endpoints.

Every API endpoint in this system must return responses through one of
these functions to guarantee a consistent JSON structure for the frontend.

Standard success envelope:
  {
    "success": true,
    "message": "Human-readable description",
    "data": { ... } | [ ... ] | null,
    "meta": { "pagination": { ... } }   (optional)
  }

Standard error envelope:
  {
    "success": false,
    "message": "Human-readable error description",
    "errors": { "field": ["error detail"] }   (optional)
  }

Usage:
  from app.core.responses import success_response, error_response

  return success_response(message="Job created", data=job_data, status_code=201)
  return error_response(message="Validation failed", errors=field_errors, status_code=422)
"""

from typing import Any, Optional

from flask import jsonify


# ---------------------------------------------------------------------------
# Success Responses
# ---------------------------------------------------------------------------


def success_response(
    message: str = "Request completed successfully",
    data: Any = None,
    status_code: int = 200,
    meta: Optional[dict] = None,
) -> tuple:
    """
    Return a standard success response.

    Args:
        message     : Human-readable description of the result.
        data        : Response payload — dict, list, or None.
        status_code : HTTP status code (default 200).
        meta        : Optional metadata (e.g. pagination).

    Returns:
        A (Response, int) tuple ready to be returned from any Flask route.
    """
    body: dict = {
        "success": True,
        "message": message,
        "data": data,
    }
    if meta is not None:
        body["meta"] = meta

    return jsonify(body), status_code


def created_response(
    message: str = "Resource created successfully",
    data: Any = None,
) -> tuple:
    """
    Convenience wrapper for HTTP 201 Created responses.

    Args:
        message : Human-readable description.
        data    : The newly created resource representation.

    Returns:
        A (Response, 201) tuple.
    """
    return success_response(message=message, data=data, status_code=201)


def no_content_response() -> tuple:
    """
    Return HTTP 204 No Content for delete operations.

    Returns:
        A (Response, 204) tuple with an empty body.
    """
    return jsonify({}), 204


def paginated_response(
    message: str,
    data: Any,
    page: int,
    per_page: int,
    total: int,
) -> tuple:
    """
    Return a paginated success response with pagination metadata.

    Args:
        message  : Human-readable description.
        data     : The current page of results.
        page     : Current page number (1-indexed).
        per_page : Number of items per page.
        total    : Total number of items across all pages.

    Returns:
        A (Response, 200) tuple with pagination in the `meta` field.
    """
    total_pages: int = max(1, (total + per_page - 1) // per_page)

    meta: dict = {
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        }
    }
    return success_response(message=message, data=data, meta=meta)


# ---------------------------------------------------------------------------
# Error Responses
# ---------------------------------------------------------------------------


def error_response(
    message: str = "An error occurred",
    errors: Any = None,
    status_code: int = 400,
) -> tuple:
    """
    Return a standard error response.

    Args:
        message     : Human-readable error description.
        errors      : Detailed error information — dict of field errors,
                      list of error strings, or None.
        status_code : HTTP status code (default 400).

    Returns:
        A (Response, int) tuple ready to be returned from any Flask route.
    """
    body: dict = {
        "success": False,
        "message": message,
    }
    if errors is not None:
        body["errors"] = errors

    return jsonify(body), status_code


def validation_error_response(errors: Any) -> tuple:
    """
    Convenience wrapper for HTTP 422 Unprocessable Entity responses.
    Used when request data fails validation.

    Args:
        errors : Field-level validation errors.

    Returns:
        A (Response, 422) tuple.
    """
    return error_response(
        message="Request validation failed",
        errors=errors,
        status_code=422,
    )


def unauthorized_response(message: str = "Authentication required") -> tuple:
    """
    Return HTTP 401 Unauthorized.

    Args:
        message : Human-readable explanation.

    Returns:
        A (Response, 401) tuple.
    """
    return error_response(message=message, status_code=401)


def forbidden_response(
    message: str = "You do not have permission to perform this action",
) -> tuple:
    """
    Return HTTP 403 Forbidden.

    Args:
        message : Human-readable explanation.

    Returns:
        A (Response, 403) tuple.
    """
    return error_response(message=message, status_code=403)


def not_found_response(
    message: str = "The requested resource was not found",
) -> tuple:
    """
    Return HTTP 404 Not Found.

    Args:
        message : Human-readable explanation.

    Returns:
        A (Response, 404) tuple.
    """
    return error_response(message=message, status_code=404)

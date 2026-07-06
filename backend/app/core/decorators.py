"""
core/decorators.py
------------------
Authentication and authorization decorators for the Smart Resume Screening System.

Provides route-level protection via three layers:
  1. @jwt_required_user   : Require a valid, non-revoked JWT. Loads and exposes
                            the current User instance via Flask's g object.
  2. @candidate_required  : Require jwt_required_user AND the candidate role.
  3. @recruiter_required  : Require jwt_required_user AND the recruiter role.
  4. @admin_required      : Require jwt_required_user AND the admin role.

Usage on routes:
  from app.core.decorators import candidate_required, get_current_user

  @bp.get("/profile")
  @candidate_required
  def get_profile():
      user = get_current_user()  # Always succeeds here — decorator guarantees it
      return success_response(data=user.to_public_dict())

Design decisions:
  - Each decorator is self-contained: it calls verify_jwt_in_request() and
    loads the user itself. This avoids decorator-stacking issues where two
    decorators might fight over the JWT validation state.
  - User loading uses an explicit SELECT with a UUID identity to avoid any
    risk of the identity string being treated differently across code paths.
  - All imports of models are done INSIDE the decorator functions (lazy imports)
    to prevent circular import issues at module initialization time.
  - Responses from decorators always follow the same JSON envelope as the
    rest of the API (success: false, message: ...) for frontend consistency.
  - The `g` object is the correct Flask mechanism for passing per-request
    data from a decorator to the route function it protects.

Token blocklist:
  The @jwt.token_in_blocklist_loader callback in app/__init__.py calls
  TokenBlocklist.is_jti_blocklisted() before any protected route handler runs.
  The decorators here do NOT need to repeat that check — Flask-JWT-Extended
  has already handled it by the time the decorator body executes.
"""

import logging
import uuid
from functools import wraps
from typing import Optional

from flask import g, jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _load_user_from_jwt() -> Optional[object]:
    """
    Read the JWT identity and load the matching User from the database.

    Called by every decorator after verify_jwt_in_request() succeeds.

    Returns:
        User instance if found and eligible to log in.
        None  if the identity is malformed, the user doesn't exist,
              or the user is not eligible (inactive / suspended / deleted).
    """
    # Imports are inside the function to prevent circular import issues
    # at module load time (decorators.py → extensions.py → ok, but
    # decorators.py → models/user.py → models/base.py → extensions.py
    # could form a cycle if resolved at import time in some configurations).
    from sqlalchemy import select

    from app.extensions import db
    from app.models.user import User

    identity: Optional[str] = get_jwt_identity()
    if not identity:
        logger.warning("JWT identity is empty or None")
        return None

    try:
        user_id: uuid.UUID = uuid.UUID(identity)
    except (ValueError, AttributeError):
        logger.warning("JWT identity is not a valid UUID: %r", identity)
        return None

    user: Optional[User] = db.session.execute(
        select(User).where(User.id == user_id)
    ).scalar_one_or_none()

    if user is None:
        logger.warning("No user found for JWT identity: %s", identity)
        return None

    if not user.can_login:
        logger.warning(
            "User is not eligible to access resources | user_id=%s | "
            "active=%s | verified=%s | suspended=%s | deleted=%s",
            user.id,
            user.is_active,
            user.is_verified,
            user.is_suspended,
            user.is_deleted,
        )
        return None

    return user


def _auth_error_response(message: str, status_code: int = 401):
    """Return a JSON error response using the standard API envelope."""
    return jsonify({"success": False, "message": message}), status_code


def _forbidden_response(role_required: str):
    """Return a 403 JSON response when the user lacks the required role."""
    return jsonify({
        "success": False,
        "message": (
            f"Access denied. This endpoint requires the '{role_required}' role."
        ),
    }), 403


# ---------------------------------------------------------------------------
# g.current_user Accessor
# ---------------------------------------------------------------------------


def get_current_user():
    """
    Return the current authenticated user set by a decorator.

    Must only be called inside a route that is protected by one of the
    role decorators below. Accessing g.current_user outside a decorated
    route raises an AttributeError.

    Returns:
        User: The authenticated User instance for the current request.
    """
    return g.current_user


# ---------------------------------------------------------------------------
# Base Decorator: JWT Required + User Load
# ---------------------------------------------------------------------------


def jwt_required_user(fn):
    """
    Require a valid, non-revoked JWT access token.

    On success: loads the User from the database and stores it in
    g.current_user for the duration of the request. Also stores the
    user's role name in g.current_user_role for convenience.

    On failure:
      - Missing or malformed token → 401
      - Token revoked (blocklist)  → 401 (handled by Flask-JWT-Extended
                                          before this decorator body runs)
      - User not found             → 401
      - User ineligible (inactive, suspended, etc.) → 401

    Usage:
      @bp.get("/me")
      @jwt_required_user
      def get_me():
          return success_response(data=get_current_user().to_public_dict())
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # verify_jwt_in_request() validates the token signature, expiry,
        # and triggers the blocklist loader registered in app/__init__.py.
        verify_jwt_in_request()

        user = _load_user_from_jwt()
        if user is None:
            return _auth_error_response(
                "Authentication failed. Please log in again."
            )

        g.current_user = user
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Role Decorators
# ---------------------------------------------------------------------------


def candidate_required(fn):
    """
    Require a valid JWT and the 'candidate' role.

    Combines JWT validation, user loading, and role check in one decorator.
    Stores the user in g.current_user on success.

    Returns:
      401 if the token is invalid or the user cannot be loaded.
      403 if the user's role is not 'candidate'.

    Usage:
      @bp.post("/apply")
      @candidate_required
      def submit_application():
          candidate = get_current_user()
          ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()

        user = _load_user_from_jwt()
        if user is None:
            return _auth_error_response(
                "Authentication failed. Please log in again."
            )

        if not user.is_candidate:
            logger.warning(
                "Role check failed | required=candidate | actual=%s | user_id=%s",
                user.role_name,
                user.id,
            )
            return _forbidden_response("candidate")

        g.current_user = user
        return fn(*args, **kwargs)

    return wrapper


def recruiter_required(fn):
    """
    Require a valid JWT and the 'recruiter' role.

    Returns:
      401 if the token is invalid or the user cannot be loaded.
      403 if the user's role is not 'recruiter'.

    Usage:
      @bp.post("/jobs")
      @recruiter_required
      def create_job():
          recruiter = get_current_user()
          ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()

        user = _load_user_from_jwt()
        if user is None:
            return _auth_error_response(
                "Authentication failed. Please log in again."
            )

        if not user.is_recruiter:
            logger.warning(
                "Role check failed | required=recruiter | actual=%s | user_id=%s",
                user.role_name,
                user.id,
            )
            return _forbidden_response("recruiter")

        g.current_user = user
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    """
    Require a valid JWT and the 'admin' role.

    Returns:
      401 if the token is invalid or the user cannot be loaded.
      403 if the user's role is not 'admin'.

    Usage:
      @admin_bp.get("/users")
      @admin_required
      def list_all_users():
          admin = get_current_user()
          ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()

        user = _load_user_from_jwt()
        if user is None:
            return _auth_error_response(
                "Authentication failed. Please log in again."
            )

        if not user.is_admin:
            logger.warning(
                "Role check failed | required=admin | actual=%s | user_id=%s",
                user.role_name,
                user.id,
            )
            return _forbidden_response("admin")

        g.current_user = user
        return fn(*args, **kwargs)

    return wrapper

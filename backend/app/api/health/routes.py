"""
api/health/routes.py
--------------------
Health check endpoint for the Smart Resume Screening System.

GET /api/v1/health

Returns real-time system status including:
  - Application version
  - Environment name
  - Server uptime in seconds
  - Database connectivity status
  - Python version

This endpoint is used by:
  - Load balancers and container orchestrators (Kubernetes readiness probes)
  - Deployment pipelines to verify the service is running
  - Monitoring tools for uptime checks
  - Developers verifying the local environment is working

No authentication is required for this endpoint.
"""

import logging
import platform
import time

from flask import Blueprint, current_app
from sqlalchemy import text

from app.core.responses import success_response
from app.extensions import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------

health_bp = Blueprint("health", __name__)

# Record the time when this module is first imported.
# Used to compute server uptime on each request.
_SERVER_START_TIME: float = time.time()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@health_bp.route("/health", methods=["GET"])
def health_check():
    """
    Return the current health status of the application.

    This endpoint performs a lightweight database connectivity check
    and returns system metadata. It should always respond quickly
    (under 100ms) and must never return 5xx unless the app itself
    is broken.

    Returns:
        200 OK  — The service is healthy (even if the DB is unavailable,
                   we return 200 with `database: "unavailable"` so the
                   app itself is considered running).
    """
    uptime_seconds: int = int(time.time() - _SERVER_START_TIME)
    db_status: str = _check_database_connectivity()

    data: dict = {
        "status": "ok",
        "version": "1.0.0",
        "application": current_app.config.get("APP_NAME"),
        "environment": current_app.config.get("ENV_NAME", "development"),
        "uptime_seconds": uptime_seconds,
        "database": db_status,
        "python_version": platform.python_version(),
    }

    logger.debug("Health check requested | db_status=%s", db_status)

    return success_response(message="Service is healthy", data=data)


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _check_database_connectivity() -> str:
    """
    Attempt a minimal database query to verify connectivity.

    Returns:
        "healthy"     — The database responded correctly.
        "unavailable" — The database could not be reached.
    """
    try:
        db.session.execute(text("SELECT 1"))
        return "healthy"
    except Exception as exc:
        logger.warning("Database connectivity check failed: %s", str(exc))
        return "unavailable"

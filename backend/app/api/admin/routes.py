"""
api/admin/routes.py
-------------------
Admin Blueprint — route handlers for system monitoring and queue management.
"""

from flask import Blueprint

from app.core.decorators import get_current_user, jwt_required_user
from app.core.responses import success_response
from app.models.role import Role
from app.core.exceptions import AuthorizationError
from app.services import admin_service, ai_service

admin_bp = Blueprint("admin", __name__)

def admin_required(fn):
    """Decorator to enforce admin role."""
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if user.role != Role.ADMIN:
            raise AuthorizationError("Admin access required.")
        return fn(*args, **kwargs)
    return wrapper

@admin_bp.get("/ai/stats")
@jwt_required_user
@admin_required
def get_ai_stats():
    """
    Retrieve statistics on AI processing jobs.
    """
    admin_user = get_current_user()
    stats = admin_service.get_ai_queue_statistics(admin_user)
    
    return success_response(
        message="AI statistics retrieved successfully.",
        data=stats
    )

@admin_bp.post("/ai/jobs/<job_id>/retry")
@jwt_required_user
@admin_required
def retry_job(job_id: str):
    """
    Retry a failed AI processing job.
    """
    admin_user = get_current_user()
    result = ai_service.retry_failed_job(job_id, admin_user)
    
    return success_response(
        message=result["message"],
        data={"job_id": result["job_id"]}
    )

@admin_bp.post("/ai/jobs/<job_id>/cancel")
@jwt_required_user
@admin_required
def cancel_job(job_id: str):
    """
    Cancel a queued AI processing job.
    """
    admin_user = get_current_user()
    result = ai_service.cancel_job(job_id, admin_user)
    
    return success_response(
        message=result["message"],
        data={"job_id": result["job_id"]}
    )

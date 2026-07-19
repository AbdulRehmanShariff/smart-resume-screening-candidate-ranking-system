"""
services/admin_service.py
-------------------------
Admin-specific service operations, including system monitoring and AI queue statistics.
"""

from typing import Dict, Any
from sqlalchemy import func

from app.extensions import db
from app.models.ai_processing_job import AIProcessingJob, AIJobStatus
from app.models.user import User
from app.models.role import Role
from app.core.exceptions import AuthorizationError

def get_ai_queue_statistics(admin: User) -> Dict[str, Any]:
    """
    Retrieve statistics on AI processing jobs for the admin dashboard.
    """
    if admin.role != Role.ADMIN:
        raise AuthorizationError("Only administrators can access system statistics.")

    # Status counts
    status_counts = db.session.query(
        AIProcessingJob.status, func.count(AIProcessingJob.id)
    ).group_by(AIProcessingJob.status).all()

    status_dict = {status.value: 0 for status in AIJobStatus}
    for status, count in status_counts:
        status_dict[status] = count

    # Failed jobs
    failed_jobs = db.session.query(AIProcessingJob).filter_by(status=AIJobStatus.FAILED.value).order_by(AIProcessingJob.queued_at.desc()).limit(20).all()
    failed_data = [job.to_status_dict() for job in failed_jobs]

    # Average processing time for completed jobs (last 100)
    recent_completed = db.session.query(AIProcessingJob.processing_time_ms).filter(
        AIProcessingJob.status == AIJobStatus.COMPLETED.value,
        AIProcessingJob.processing_time_ms.isnot(None)
    ).order_by(AIProcessingJob.completed_at.desc()).limit(100).all()

    avg_time = sum(j[0] for j in recent_completed) / len(recent_completed) if recent_completed else 0

    return {
        "queue_stats": status_dict,
        "total_jobs": sum(status_dict.values()),
        "average_processing_time_ms": int(avg_time),
        "recent_failed_jobs": failed_data
    }

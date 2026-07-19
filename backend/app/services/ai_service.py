"""
services/ai_service.py
----------------------
Service layer for AI operations, job status, and ranking result retrieval.
"""

import uuid
from typing import Dict, Any

from app.extensions import db
from app.models.application import Application
from app.models.ai_processing_job import AIProcessingJob, AIJobStatus
from app.models.user import User
from app.models.role import Role
from app.core.exceptions import NotFoundError, AuthorizationError, BadRequestError


def get_application_ai_results(application_id: str, recruiter: User) -> Dict[str, Any]:
    """
    Retrieve the AI ranking results for a specific application.
    Only the recruiter who owns the job can access these results.
    """
    try:
        app_uuid = uuid.UUID(application_id)
    except ValueError:
        raise BadRequestError("Invalid application ID format.")

    application = db.session.get(Application, app_uuid)
    if not application:
        raise NotFoundError("Application not found.")

    if application.job.recruiter_id != recruiter.id:
        raise AuthorizationError("You do not have permission to view this application's AI results.")

    return {
        "id": str(application.id),
        "overall_score": float(application.match_score) if application.match_score is not None else None,
        "quality_score": float(application.resume.quality_score) if application.resume.quality_score is not None else None,
        "semantic_score": application.score_breakdown.get("semantic_similarity") if application.score_breakdown else None,
        "score_breakdown": application.score_breakdown,
        "strengths": application.ranking_reason.get("strengths", []) if application.ranking_reason else [],
        "weaknesses": application.ranking_reason.get("areas_for_development", []) if application.ranking_reason else [],
        "missing_skills": application.skill_gap.get("missing_required", []) + application.skill_gap.get("missing_preferred", []) if application.skill_gap else [],
        "interview_questions": application.interview_questions,
        "ai_summary": application.resume.ai_summary,
        "ranking_reason": application.ranking_reason,
        "processing_status": application.resume.parse_status
    }


def get_ai_processing_status(entity_type: str, entity_id: str, user: User) -> Dict[str, Any]:
    """
    Retrieve the AI processing status for a specific entity (resume, application, or job).
    """
    try:
        ent_uuid = uuid.UUID(entity_id)
    except ValueError:
        raise BadRequestError("Invalid entity ID format.")

    # Find the most recent job for this entity
    job = (
        db.session.query(AIProcessingJob)
        .filter_by(entity_type=entity_type, entity_id=ent_uuid)
        .order_by(AIProcessingJob.queued_at.desc())
        .first()
    )

    if not job:
        raise NotFoundError("No AI processing jobs found for this entity.")

    # Simple auth check: if user is not admin, we might want to ensure they own the entity.
    # To keep it generic and safe, we allow candidates to see their own resumes/apps,
    # and recruiters to see their own jobs/apps.
    if user.role == Role.CANDIDATE:
        if entity_type == "resume":
            from app.models.resume import Resume
            resume = db.session.get(Resume, ent_uuid)
            if not resume or resume.user_id != user.id:
                raise AuthorizationError("Access denied.")
        elif entity_type == "application":
            app_obj = db.session.get(Application, ent_uuid)
            if not app_obj or app_obj.candidate_id != user.id:
                raise AuthorizationError("Access denied.")
        else:
            raise AuthorizationError("Access denied.")
    elif user.role == Role.RECRUITER:
        if entity_type == "job":
            from app.models.job import Job
            job_obj = db.session.get(Job, ent_uuid)
            if not job_obj or job_obj.recruiter_id != user.id:
                raise AuthorizationError("Access denied.")
        elif entity_type == "application":
            app_obj = db.session.get(Application, ent_uuid)
            if not app_obj or app_obj.job.recruiter_id != user.id:
                raise AuthorizationError("Access denied.")
        else:
            # Maybe recruiters checking resume status? Handled via application.
            pass

    return {
        "job_id": str(job.id),
        "job_type": job.job_type,
        "status": job.status,
        "status_label": job.status_label,
        "error_message": job.error_message,
        "retry_count": job.retry_count,
        "queued_at": job.queued_at.isoformat() if job.queued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def retry_failed_job(job_id: str, user: User) -> Dict[str, Any]:
    """Retry a failed AI processing job."""
    if user.role != Role.ADMIN:
        raise AuthorizationError("Only administrators can manage AI jobs directly.")

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise BadRequestError("Invalid job ID format.")

    job = db.session.get(AIProcessingJob, job_uuid)
    if not job:
        raise NotFoundError("Job not found.")

    if job.status != AIJobStatus.FAILED.value:
        raise BadRequestError("Only failed jobs can be retried.")

    job.status = AIJobStatus.QUEUED.value
    job.retry_count = 0
    job.next_retry_at = None
    job.error_message = None
    job.error_traceback = None
    job.completed_at = None
    
    db.session.commit()
    
    return {"message": "Job successfully re-queued for processing.", "job_id": str(job.id)}


def cancel_job(job_id: str, user: User) -> Dict[str, Any]:
    """Cancel a queued AI processing job."""
    if user.role != Role.ADMIN:
        raise AuthorizationError("Only administrators can manage AI jobs directly.")

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise BadRequestError("Invalid job ID format.")

    job = db.session.get(AIProcessingJob, job_uuid)
    if not job:
        raise NotFoundError("Job not found.")

    if job.status != AIJobStatus.QUEUED.value:
        raise BadRequestError(f"Cannot cancel job in status: {job.status}")

    job.cancel()
    db.session.commit()
    
    return {"message": "Job successfully cancelled.", "job_id": str(job.id)}

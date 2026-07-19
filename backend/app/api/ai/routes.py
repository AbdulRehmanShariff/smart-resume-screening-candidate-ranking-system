"""
api/ai/routes.py
----------------
AI Blueprint — route handlers for retrieving AI results and job statuses.
"""

from flask import Blueprint

from app.core.decorators import get_current_user, jwt_required_user, recruiter_required
from app.core.responses import success_response
from app.services import ai_service

ai_bp = Blueprint("ai", __name__)

@ai_bp.get("/applications/<application_id>/results")
@recruiter_required
def get_ai_results(application_id: str):
    """
    Retrieve AI ranking results for a specific application.
    """
    recruiter = get_current_user()
    results = ai_service.get_application_ai_results(application_id, recruiter)
    
    return success_response(
        message="AI results retrieved successfully.",
        data=results
    )

@ai_bp.get("/status/<entity_type>/<entity_id>")
@jwt_required_user
def get_processing_status(entity_type: str, entity_id: str):
    """
    Retrieve AI processing status for an entity (resume, application, job).
    """
    user = get_current_user()
    status_data = ai_service.get_ai_processing_status(entity_type, entity_id, user)
    
    return success_response(
        message="Processing status retrieved successfully.",
        data=status_data
    )

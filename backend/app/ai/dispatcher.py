"""
ai/dispatcher.py
----------------
Routes AI processing jobs to their corresponding pipeline implementations.
"""

import logging
import os
from typing import Any, Dict

from flask import current_app

from app.models.ai_processing_job import AIJobType
from app.models.resume import Resume
from app.models.application import Application
from app.models.job import Job
from app.models.system_settings import SystemSettings
from app.extensions import db
from app.core.exceptions import BadRequestError

from app.ai.extractors import extract_text
from app.ai.pipelines.resume_parser import parse_resume
from app.ai.pipelines.embedder import embed_text
from app.ai.pipelines.quality_scorer import score_resume
from app.ai.pipelines.ranker import rank_candidate

logger = logging.getLogger(__name__)

def dispatch(job, faiss_store, api_key: str, model_name: str, temperature: float) -> Dict[str, Any]:
    """
    Route the job to the correct pipeline based on its type.
    """
    if job.job_type == AIJobType.RESUME_PARSE.value:
        return _dispatch_resume_parse(job, faiss_store, api_key, model_name, temperature)
        
    elif job.job_type == AIJobType.APPLICATION_RANK.value:
        return _dispatch_application_rank(job, faiss_store, api_key, model_name, temperature)
        
    elif job.job_type == AIJobType.BULK_RERANK.value:
        return _dispatch_bulk_rerank(job)
        
    else:
        raise BadRequestError(f"Unsupported job type: {job.job_type}")


def _dispatch_resume_parse(job, faiss_store, api_key: str, model_name: str, temperature: float) -> dict:
    if job.entity_type != "resume":
        raise BadRequestError(f"resume_parse job requires entity_type 'resume', got {job.entity_type}")
        
    resume = db.session.get(Resume, job.entity_id)
    if not resume:
        raise BadRequestError(f"Resume {job.entity_id} not found.")

    embed_model_name = SystemSettings.get_value_by_key("ai.embedding_model", default="all-MiniLM-L6-v2")

    # 1. EXTRACT
    resume.parse_status = Resume.PARSING
    db.session.commit()
    
    upload_folder = current_app.config.get("UPLOAD_FOLDER", "uploads")
    if os.path.isabs(resume.file_path):
        abs_file_path = resume.file_path
    else:
        abs_file_path = os.path.join(upload_folder, resume.file_path)
    
    raw_text = extract_text(abs_file_path, resume.file_type)
    
    # 2. PARSE
    parsed_data = parse_resume(raw_text, api_key, model_name, temperature)
    resume.parsed_data = parsed_data
    resume.parse_status = Resume.PARSED
    
    from datetime import datetime, timezone
    resume.parsed_at = datetime.now(timezone.utc)
    
    if not resume.ai_metadata:
        resume.ai_metadata = {}
    
    resume.ai_metadata["parse"] = {
        "model_name": model_name,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    db.session.commit()
    
    # 3. EMBED
    text_to_embed = parsed_data.get("summary", "") or raw_text[:8000]
    vector = embed_text(text_to_embed, embed_model_name)
    faiss_store.add(resume.id, vector)
    faiss_store.save()
    
    resume.embedding_stored = True
    resume.parse_status = Resume.EMBEDDING_GENERATED
    resume.ai_metadata["embed"] = {
        "model_name": embed_model_name,
        "dim": len(vector),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    db.session.commit()
    
    # 4. QUALITY SCORE
    quality = score_resume(parsed_data, api_key, model_name, temperature)
    resume.quality_score = quality.get("quality_score")
    resume.quality_report = quality.get("quality_report")
    resume.ai_summary = quality.get("ai_summary")
    resume.parse_status = Resume.COMPLETED
    resume.ai_metadata["quality"] = quality.get("metadata", quality.get("ai_metadata"))
    db.session.commit()
    
    return {"status": "success", "steps_completed": ["extract", "parse", "embed", "quality_score"]}


def _dispatch_application_rank(job, faiss_store, api_key: str, model_name: str, temperature: float) -> dict:
    if job.entity_type != "application":
        raise BadRequestError(f"application_rank job requires entity_type 'application', got {job.entity_type}")
        
    application = db.session.get(Application, job.entity_id)
    if not application:
        raise BadRequestError(f"Application {job.entity_id} not found.")
        
    resume = application.resume
    target_job = application.job
    
    if resume.parse_status not in Resume.PARSEABLE_STATUSES:
        raise BadRequestError("Resume not yet parsed")
        
    embed_model_name = SystemSettings.get_value_by_key("ai.embedding_model", default="all-MiniLM-L6-v2")
    
    try:
        faiss_weight = float(SystemSettings.get_value_by_key("ai.faiss_weight", default="0.30"))
    except ValueError:
        faiss_weight = 0.30
        
    ranking = rank_candidate(
        resume=resume,
        job=target_job,
        faiss_store=faiss_store,
        api_key=api_key,
        model_name=model_name,
        temperature=temperature,
        embed_model_name=embed_model_name,
        faiss_weight=faiss_weight
    )
    
    application.match_score = ranking.get("match_score")
    application.score_breakdown = ranking.get("score_breakdown")
    application.ranking_reason = ranking.get("ranking_reason")
    application.skill_gap = ranking.get("skill_gap")
    application.interview_questions = ranking.get("interview_questions")
    application.ai_metadata = ranking.get("ai_metadata")
    application.record_ai_processing()
    
    resume.parse_status = Resume.RANKED
    db.session.commit()
    
    return {"status": "success", "match_score": application.match_score}


def _dispatch_bulk_rerank(job) -> dict:
    if job.entity_type != "job":
        raise BadRequestError(f"bulk_rerank job requires entity_type 'job', got {job.entity_type}")
        
    target_job = db.session.get(Job, job.entity_id)
    if not target_job:
        raise BadRequestError(f"Job {job.entity_id} not found.")
        
    from app.models.ai_processing_job import AIProcessingJob, AIJobType
    
    enqueued_count = 0
    for app in target_job.applications:
        if app.is_active:
            new_job = AIProcessingJob(
                job_type=AIJobType.APPLICATION_RANK.value,
                entity_type="application",
                entity_id=app.id,
                priority=AIProcessingJob.PRIORITY_LOW
            )
            db.session.add(new_job)
            enqueued_count += 1
            
    db.session.commit()
    return {"status": "success", "enqueued": enqueued_count}

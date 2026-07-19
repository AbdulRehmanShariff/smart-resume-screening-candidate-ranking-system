"""
ai/pipelines/ranker.py
----------------------
Ranks a candidate against a job description using FAISS and Gemini.
"""

import os
import json
import time
import logging
import re
from datetime import datetime, timezone

import google.generativeai as genai
from google.generativeai.types import GenerationConfig
from app.core.exceptions import InternalError, ServiceUnavailableError, BadRequestError
from app.ai.pipelines.embedder import embed_text

logger = logging.getLogger(__name__)

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompts')
_RANK_PROMPT_PATH = os.path.join(_PROMPTS_DIR, 'rank_candidate.txt')

try:
    with open(_RANK_PROMPT_PATH, 'r', encoding='utf-8') as f:
        _RANK_PROMPT_TEMPLATE = f.read()
except Exception as e:
    logger.error("Failed to load rank_candidate.txt prompt: %s", e)
    _RANK_PROMPT_TEMPLATE = ""


def _safe_parse_json(response_text: str) -> dict:
    if not response_text:
        return {}
    text = response_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    logger.warning("Failed to parse JSON from ranker response.")
    return {}


def rank_candidate(
    resume, 
    job, 
    faiss_store, 
    api_key: str, 
    model_name: str, 
    temperature: float,
    embed_model_name: str = "all-MiniLM-L6-v2",
    faiss_weight: float = 0.30
) -> dict:
    """
    Rank a candidate against a job. Uses FAISS for structural similarity
    and Gemini for semantic match, gap analysis, and questions.
    """
    if not _RANK_PROMPT_TEMPLATE:
        raise InternalError("Rank candidate prompt template not loaded.")
        
    if resume.parse_status not in ['parsed', 'embedding_generated', 'ranked', 'completed']:
        raise BadRequestError("Resume not yet parsed. Cannot rank.")

    start_time = time.time()
    
    # 1. FAISS SIMILARITY
    job_text = f"{job.title}\n{job.description}\n{json.dumps(job.skills_required)}"
    try:
        job_vector = embed_text(job_text, embed_model_name)
        
        faiss_results = faiss_store.search(job_vector, top_k=max(100, faiss_store.index.ntotal))
        
        resume_id_str = str(resume.id)
        faiss_score = 0.0
        for res in faiss_results:
            if res["resume_id"] == resume_id_str:
                faiss_score = res["score"]
                break
                
        # Normalize inner product (clip to 0.0 - 1.0)
        faiss_score = max(0.0, min(1.0, float(faiss_score)))
    except Exception as e:
        logger.warning("FAISS similarity computation failed: %s", e)
        faiss_score = 0.0

    # 2. GEMINI RANKING
    genai.configure(api_key=api_key)
    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config=GenerationConfig(
                temperature=temperature,
                response_mime_type="application/json"
            )
        )
        
        # Safely handle enums if present
        job_level = job.experience_level.value if hasattr(job.experience_level, 'value') else job.experience_level
        
        prompt = _RANK_PROMPT_TEMPLATE
        prompt = prompt.replace("{job_title}", str(job.title))
        prompt = prompt.replace("{job_level}", str(job_level))
        prompt = prompt.replace("{job_description}", str(job.description))
        prompt = prompt.replace("{required_skills}", json.dumps(job.skills_required, indent=2))
        prompt = prompt.replace("{preferred_skills}", json.dumps(job.skills_preferred, indent=2))
        prompt = prompt.replace("{resume_data}", json.dumps(resume.parsed_data, indent=2))
        
        response = model.generate_content(prompt)
        gemini_data = _safe_parse_json(response.text)
        
    except Exception as e:
        logger.error("Gemini API call failed during candidate ranking: %s", e)
        raise ServiceUnavailableError(f"AI ranking failed: {str(e)}")
        
    gemini_score = float(gemini_data.get("match_score", 0.0))
    
    # 3. BLEND
    final_score = (faiss_weight * (faiss_score * 100)) + ((1.0 - faiss_weight) * gemini_score)
    final_score = round(max(0.0, min(100.0, final_score)), 2)
    
    score_breakdown = gemini_data.get("score_breakdown", {})
    score_breakdown["faiss_similarity"] = faiss_score
    
    processing_time_ms = int((time.time() - start_time) * 1000)
    
    return {
        "match_score": final_score,
        "score_breakdown": score_breakdown,
        "ranking_reason": gemini_data.get("ranking_reason", {}),
        "skill_gap": gemini_data.get("skill_gap", {}),
        "interview_questions": gemini_data.get("interview_questions", []),
        "ai_metadata": {
            "model": model_name,
            "faiss_score": faiss_score,
            "gemini_score": gemini_score,
            "blend_weights": {"faiss": faiss_weight, "gemini": 1.0 - faiss_weight},
            "processing_time_ms": processing_time_ms,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    }

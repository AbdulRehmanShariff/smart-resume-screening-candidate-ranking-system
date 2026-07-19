"""
ai/pipelines/quality_scorer.py
------------------------------
Generates a quality score and recruiter summary for a parsed resume using Gemini.
"""

import os
import json
import time
import logging
import re
from datetime import datetime, timezone

import google.generativeai as genai
from google.generativeai.types import GenerationConfig
from app.core.exceptions import InternalError, ServiceUnavailableError

logger = logging.getLogger(__name__)

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompts')
_QUALITY_PROMPT_PATH = os.path.join(_PROMPTS_DIR, 'quality_score.txt')

try:
    with open(_QUALITY_PROMPT_PATH, 'r', encoding='utf-8') as f:
        _QUALITY_PROMPT_TEMPLATE = f.read()
except Exception as e:
    logger.error("Failed to load quality_score.txt prompt: %s", e)
    _QUALITY_PROMPT_TEMPLATE = ""


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
    logger.warning("Failed to parse JSON from quality scorer response.")
    return {}


def score_resume(parsed_data: dict, api_key: str, model_name: str, temperature: float) -> dict:
    """
    Score a parsed resume using Gemini.
    """
    if not _QUALITY_PROMPT_TEMPLATE:
        raise InternalError("Quality score prompt template not loaded.")

    start_time = time.time()
    genai.configure(api_key=api_key)
    
    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config=GenerationConfig(
                temperature=temperature,
                response_mime_type="application/json"
            )
        )
        
        prompt = _QUALITY_PROMPT_TEMPLATE.replace("{resume_data}", json.dumps(parsed_data, indent=2))
        response = model.generate_content(prompt)
        scored_data = _safe_parse_json(response.text)
        
        processing_time_ms = int((time.time() - start_time) * 1000)
        ai_metadata = {
            "model": model_name,
            "processing_time_ms": processing_time_ms,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        return {
            "quality_score": float(scored_data.get("overall_score", 0.0)),
            "quality_report": {
                "breakdown": scored_data.get("breakdown", {}),
                "strengths": scored_data.get("strengths", []),
                "improvements": scored_data.get("improvements", [])
            },
            "ai_summary": scored_data.get("ai_summary", ""),
            "ai_metadata": ai_metadata
        }
        
    except Exception as e:
        logger.error("Gemini API call failed during quality scoring: %s", e)
        raise ServiceUnavailableError(f"AI quality scoring failed: {str(e)}")

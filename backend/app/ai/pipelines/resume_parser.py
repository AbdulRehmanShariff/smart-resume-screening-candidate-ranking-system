"""
ai/pipelines/resume_parser.py
-----------------------------
Pipeline for structured resume information extraction using Gemini.
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone

import google.generativeai as genai
from google.generativeai.types import GenerationConfig
from app.core.exceptions import InternalError, ServiceUnavailableError

logger = logging.getLogger(__name__)

# Pre-load the prompt template
_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompts')
_PARSE_PROMPT_PATH = os.path.join(_PROMPTS_DIR, 'parse_resume.txt')

try:
    with open(_PARSE_PROMPT_PATH, 'r', encoding='utf-8') as f:
        _PARSE_PROMPT_TEMPLATE = f.read()
except Exception as e:
    logger.error("Failed to load parse_resume.txt prompt: %s", e)
    _PARSE_PROMPT_TEMPLATE = ""


def _safe_parse_json(response_text: str) -> dict:
    """
    Safely extract and parse JSON from a Gemini response.
    Handles raw JSON, markdown-fenced JSON, and gracefully falls back.
    """
    if not response_text:
        return {"raw_text": ""}

    text = response_text.strip()
    
    # 1. Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
        
    # 2. Try to extract JSON from markdown fences (```json ... ```)
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Last resort fallback to prevent pipeline failure
    logger.warning("Failed to parse structured JSON from Gemini response. Falling back to raw text dict.")
    return {"raw_text": text, "_parsing_error": "JSON output was invalid"}


def parse_resume(raw_text: str, api_key: str, model_name: str, temperature: float) -> dict:
    """
    Sends raw resume text to Gemini and extracts structured JSON.
    
    Args:
        raw_text: Cleaned text from the resume.
        api_key: Gemini API key.
        model_name: Gemini model name (e.g., gemini-1.5-flash).
        temperature: Temperature for the model.
        
    Returns:
        dict containing the structured `parsed_data` and `ai_metadata`.
    """
    if not _PARSE_PROMPT_TEMPLATE:
        raise InternalError("Prompt template not loaded.")

    start_time = time.time()
    
    # Configure the global gemini client with the provided key
    genai.configure(api_key=api_key)
    
    try:
        # We explicitly request application/json, which gemini-1.5 models support well
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config=GenerationConfig(
                temperature=temperature,
                response_mime_type="application/json"
            )
        )
        
        prompt = _PARSE_PROMPT_TEMPLATE.replace("{resume_text}", raw_text)
        
        response = model.generate_content(prompt)
        
        parsed_data = _safe_parse_json(response.text)
        
        processing_time_ms = int((time.time() - start_time) * 1000)
        
        ai_metadata = {
            "model": model_name,
            "processing_time_ms": processing_time_ms,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        return {
            "parsed_data": parsed_data,
            "ai_metadata": ai_metadata
        }
        
    except Exception as e:
        logger.error("Gemini API call failed during resume parsing: %s", e)
        raise ServiceUnavailableError(f"AI parsing failed: {str(e)}")

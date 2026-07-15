"""
ai/pipelines/embedder.py
------------------------
Generates semantic embeddings from text using Sentence Transformers.
"""

import logging
import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# Module-level cache for the model instance to avoid reloading it on every call.
# The worker will preflight this before the polling loop starts.
_MODEL_CACHE = {}

def get_embedder(model_name: str) -> SentenceTransformer:
    """Load and cache the SentenceTransformer model."""
    if model_name not in _MODEL_CACHE:
        logger.info("Loading SentenceTransformer model: %s", model_name)
        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _MODEL_CACHE[model_name]

def embed_text(text: str, model_name: str) -> np.ndarray:
    """
    Generate an L2-normalized embedding for the given text.
    L2 normalization is required for inner product (IndexFlatIP) to equal cosine similarity.
    
    Args:
        text: Raw text to embed.
        model_name: Name of the SentenceTransformer model.
        
    Returns:
        A numpy float32 array representing the embedding vector.
    """
    if not text:
        text = ""
        
    model = get_embedder(model_name)
    
    # encode() with normalize_embeddings=True handles L2 normalization automatically
    vector = model.encode(text, normalize_embeddings=True, convert_to_numpy=True)
    return vector.astype(np.float32)

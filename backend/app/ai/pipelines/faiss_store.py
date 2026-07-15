"""
ai/pipelines/faiss_store.py
---------------------------
Manages the FAISS vector index and UUID mapping file.
Uses portalocker for process-safe concurrent writes.
"""

import os
import json
import logging
import uuid
from typing import List, Dict, Any
import numpy as np
import faiss
import portalocker

logger = logging.getLogger(__name__)

class FaissResumeStore:
    def __init__(self, index_path: str, dimension: int = 384):
        self.index_path = index_path
        self.mapping_path = index_path.replace('.faiss', '_ids.json')
        self.dimension = dimension
        
        # In-memory state
        self.index = faiss.IndexFlatIP(self.dimension)
        self.uuid_list: List[str] = []
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.index_path)), exist_ok=True)
        
        # Load index if it exists
        self.load()

    def add(self, resume_id: uuid.UUID, vector: np.ndarray) -> None:
        """Add a new vector to the index in memory."""
        resume_id_str = str(resume_id)
        
        if len(vector.shape) == 1:
            vector = np.expand_dims(vector, axis=0)
            
        # Deduplication: remove existing vector if present before adding
        if resume_id_str in self.uuid_list:
            self.remove(resume_id)
            
        self.index.add(vector)
        self.uuid_list.append(resume_id_str)
        
    def search(self, query_vector: np.ndarray, top_k: int = 5) -> List[Dict[str, Any]]:
        """Search for top K similar resumes."""
        if self.index.ntotal == 0:
            return []
            
        if len(query_vector.shape) == 1:
            query_vector = np.expand_dims(query_vector, axis=0)
            
        scores, indices = self.index.search(query_vector, min(top_k, self.index.ntotal))
        
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx >= 0 and idx < len(self.uuid_list):
                results.append({
                    "resume_id": self.uuid_list[idx],
                    "score": float(score)
                })
        return results

    def remove(self, resume_id: uuid.UUID) -> None:
        """
        Removes a resume from the index.
        Since IndexFlatIP doesn't natively support remove_ids easily without IndexIDMap,
        we rebuild the index in memory excluding the deleted ID.
        This is an expensive operation but rarely needed in production (we post-filter instead).
        """
        resume_id_str = str(resume_id)
        if resume_id_str not in self.uuid_list:
            return
            
        idx_to_remove = self.uuid_list.index(resume_id_str)
        
        # Rebuild index
        new_index = faiss.IndexFlatIP(self.dimension)
        new_uuid_list = []
        
        # We extract all vectors via rev_swig_ptr.
        if self.index.ntotal > 0:
            all_vectors = faiss.rev_swig_ptr(self.index.get_xb(), self.index.ntotal * self.dimension).reshape(self.index.ntotal, self.dimension)
            
            mask = np.ones(self.index.ntotal, dtype=bool)
            mask[idx_to_remove] = False
            
            vectors_to_keep = all_vectors[mask]
            
            if len(vectors_to_keep) > 0:
                new_index.add(vectors_to_keep)
                
            for i, u in enumerate(self.uuid_list):
                if i != idx_to_remove:
                    new_uuid_list.append(u)
                    
        self.index = new_index
        self.uuid_list = new_uuid_list

    def save(self) -> None:
        """Persist index and mapping to disk with an exclusive lock."""
        lock_path = self.index_path + '.lock'
        
        try:
            # Exclusive write lock
            with portalocker.Lock(lock_path, 'w', timeout=10) as _:
                # Write to temporary files first to prevent corruption on crash
                temp_index = self.index_path + '.tmp'
                temp_mapping = self.mapping_path + '.tmp'
                
                faiss.write_index(self.index, temp_index)
                with open(temp_mapping, 'w', encoding='utf-8') as f:
                    json.dump(self.uuid_list, f)
                    
                # Atomic replace ensures safe overwrite
                os.replace(temp_index, self.index_path)
                os.replace(temp_mapping, self.mapping_path)
                logger.debug("Successfully saved FAISS index to %s", self.index_path)
                
        except portalocker.exceptions.LockException:
            logger.error("Failed to acquire exclusive lock to save FAISS index: %s", self.index_path)
            raise

    def load(self) -> None:
        """Load index and mapping from disk with a shared lock."""
        if not os.path.exists(self.index_path) or not os.path.exists(self.mapping_path):
            logger.info("FAISS index files not found, starting fresh.")
            self.index = faiss.IndexFlatIP(self.dimension)
            self.uuid_list = []
            return
            
        lock_path = self.index_path + '.lock'
        
        try:
            # Shared read lock
            with portalocker.Lock(lock_path, 'r', flags=portalocker.LOCK_SH, timeout=10) as _:
                self.index = faiss.read_index(self.index_path)
                with open(self.mapping_path, 'r', encoding='utf-8') as f:
                    self.uuid_list = json.load(f)
                    
                # Sanity check
                if self.index.ntotal != len(self.uuid_list):
                    logger.warning("FAISS index count mismatch! Rebuilding empty index.")
                    self.index = faiss.IndexFlatIP(self.dimension)
                    self.uuid_list = []
                else:
                    logger.debug("Loaded FAISS index with %d vectors.", self.index.ntotal)
                    
        except portalocker.exceptions.LockException:
            logger.error("Failed to acquire shared lock to load FAISS index: %s", self.index_path)
            raise

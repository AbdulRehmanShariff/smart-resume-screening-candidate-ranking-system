"""
ai/worker.py
------------
Background polling worker that consumes ai_processing_jobs and orchestrates the AI pipelines.
"""

import time
import logging
import traceback
import platform
import os
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import text
from app.extensions import db
from app.models.ai_processing_job import AIProcessingJob, AIJobStatus
from app.ai.dispatcher import dispatch
from app.ai.pipelines.faiss_store import FAISSStore

logger = logging.getLogger(__name__)

class AIWorker:
    def __init__(self, app, faiss_index_path: str):
        self.app = app
        self.worker_id = f"{platform.node()}:{os.getpid()}"
        self.faiss_index_path = faiss_index_path
        self.faiss_store = None
        self.running = False

    def preflight(self):
        """Initialise models and FAISS store before accepting jobs."""
        with self.app.app_context():
            logger.info("Initializing FAISS store...")
            self.faiss_store = FAISSStore(self.faiss_index_path)
            self.faiss_store.load()
            
            logger.info("Pre-loading embedding model...")
            from app.models.system_settings import SystemSettings
            embed_model_name = SystemSettings.get_value_by_key("ai.embedding_model", default="all-MiniLM-L6-v2")
            from app.ai.pipelines.embedder import embed_text
            # dummy embed to trigger download if not already cached
            embed_text("preflight initialization", embed_model_name)
            logger.info("Worker preflight complete.")

    def run(self, poll_interval: int = 5, max_jobs: int = 0):
        """Run the worker loop."""
        self.running = True
        jobs_processed = 0
        
        with self.app.app_context():
            api_key = current_app.config.get("GOOGLE_API_KEY")
            model_name = current_app.config.get("AI_MODEL_NAME", "gemini-2.5-flash")
            temperature = current_app.config.get("AI_TEMPERATURE", 0.2)
            
            logger.info(f"Worker {self.worker_id} started polling. Interval: {poll_interval}s")
            
            while self.running and (max_jobs == 0 or jobs_processed < max_jobs):
                job = self._claim_next_job()
                
                if not job:
                    time.sleep(poll_interval)
                    continue
                    
                try:
                    start_time = time.time()
                    logger.info(f"Worker {self.worker_id} executing job {job.id} ({job.job_type})")
                    
                    result = dispatch(
                        job=job,
                        faiss_store=self.faiss_store,
                        api_key=api_key,
                        model_name=job.model_name or model_name,
                        temperature=temperature
                    )
                    
                    processing_time_ms = int((time.time() - start_time) * 1000)
                    job.mark_completed(
                        result_data=result,
                        processing_time_ms=processing_time_ms,
                        model_name=job.model_name or model_name
                    )
                    
                except Exception as e:
                    logger.error(f"Job {job.id} failed: {e}")
                    job.mark_failed(
                        error_message=str(e),
                        error_traceback=traceback.format_exc()
                    )
                finally:
                    db.session.commit()
                    jobs_processed += 1

    def _claim_next_job(self) -> AIProcessingJob | None:
        """Finds the next highest priority queued job and atomically claims it using SKIP LOCKED."""
        try:
            query = (
                db.session.query(AIProcessingJob)
                .filter(AIProcessingJob.status == AIJobStatus.QUEUED.value)
                .filter(
                    db.or_(
                        AIProcessingJob.next_retry_at.is_(None),
                        AIProcessingJob.next_retry_at <= datetime.now(timezone.utc)
                    )
                )
                .order_by(AIProcessingJob.priority.asc(), AIProcessingJob.queued_at.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = query.first()
            if job:
                job.mark_started(self.worker_id)
                db.session.commit()
                return job
            
            db.session.rollback()
            return None
            
        except Exception as e:
            logger.error(f"Error claiming job: {e}")
            db.session.rollback()
            return None

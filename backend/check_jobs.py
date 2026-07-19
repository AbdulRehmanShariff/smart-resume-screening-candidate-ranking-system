import os
from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.extensions import db
from app.models.ai_processing_job import AIProcessingJob
from sqlalchemy import select

def check_db():
    app = create_app()
    with app.app_context():
        jobs = db.session.execute(select(AIProcessingJob)).scalars().all()
        print(f"Total jobs: {len(jobs)}")
        for job in jobs:
            print(f"Job: {job.id} | Type: {job.job_type} | Status: {job.status} | Entity ID: {job.entity_id}")

if __name__ == "__main__":
    check_db()

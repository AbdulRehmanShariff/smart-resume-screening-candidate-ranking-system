import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask
from app.extensions import db
from app.services.resume_service import upload_resume
from app.models.user import User
from app import create_app
import uuid
import sys
import logging

logging.basicConfig(level=logging.DEBUG)

def run_trace():
    app = create_app()
    with app.app_context():
        # Find a candidate user
        candidate = User.query.filter_by(is_candidate=True).first()
        if not candidate:
            print("No candidate found, creating one.")
            candidate = User(
                first_name="Test",
                last_name="Candidate",
                email=f"test_{uuid.uuid4()}@example.com",
                is_candidate=True,
                auth_provider="local"
            )
            candidate.set_password("password")
            db.session.add(candidate)
            db.session.commit()
            
        print(f"Using candidate: {candidate.id}")
        
        # Create a dummy file object
        import io
        from werkzeug.datastructures import FileStorage
        file_content = b"Dummy resume content PDF format"
        file = FileStorage(
            stream=io.BytesIO(file_content),
            filename="dummy_resume.pdf",
            content_type="application/pdf"
        )
        
        try:
            print("Calling upload_resume...")
            result = upload_resume(
                candidate=candidate,
                file=file,
                data={"set_as_primary": True},
                ip_address="127.0.0.1",
                user_agent="TestTrace/1.0"
            )
            print("Upload result:", result)
        except Exception as e:
            print("Exception occurred:", e)
            import traceback
            traceback.print_exc()
        
        # Check if AIProcessingJob was created
        from app.models.ai_processing_job import AIProcessingJob
        from sqlalchemy import select
        jobs = db.session.execute(select(AIProcessingJob).order_by(AIProcessingJob.queued_at.desc())).scalars().all()
        print(f"Total AI Jobs in DB: {len(jobs)}")
        for j in jobs:
            print(f"Job: {j.id} | Type: {j.job_type} | Status: {j.status} | Entity ID: {j.entity_id}")
            
if __name__ == "__main__":
    run_trace()

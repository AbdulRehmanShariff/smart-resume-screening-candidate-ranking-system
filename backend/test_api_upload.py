import os
from dotenv import load_dotenv
load_dotenv()

import io
from app import create_app
from app.extensions import db
from app.models.user import User
from app.models.role import Role
from app.models.ai_processing_job import AIProcessingJob
from sqlalchemy import select
from flask_jwt_extended import create_access_token

def run_test():
    app = create_app()
    with app.test_client() as client:
        with app.app_context():
            candidate_role = db.session.execute(select(Role).filter_by(name="candidate")).scalar_one()
            candidate = db.session.execute(select(User).filter_by(role_id=candidate_role.id)).scalars().first()
            if not candidate:
                print("No candidate found.")
                return
            
            token = create_access_token(identity=str(candidate.id))
            headers = {"Authorization": f"Bearer {token}"}
            
            # initial job count
            initial_jobs = db.session.execute(select(AIProcessingJob)).scalars().all()
            print(f"Initial jobs: {len(initial_jobs)}")
            
        print("Uploading resume via API...")
        data = {
            'resume': (io.BytesIO(b"Dummy PDF content for testing upload via API"), 'test_api_resume.pdf'),
            'set_as_primary': 'true'
        }
        
        response = client.post(
            "/api/v1/resumes/",
            data=data,
            headers=headers,
            content_type="multipart/form-data"
        )
        
        print(f"API Response Status: {response.status_code}")
        print(f"API Response Body: {response.json}")
        
        with app.app_context():
            final_jobs = db.session.execute(select(AIProcessingJob)).scalars().all()
            print(f"Final jobs: {len(final_jobs)}")
            
            new_jobs = [j for j in final_jobs if j not in initial_jobs]
            print(f"New jobs created: {len(new_jobs)}")
            for j in new_jobs:
                print(f"Job: {j.id} | Type: {j.job_type} | Entity ID: {j.entity_id}")

if __name__ == "__main__":
    run_test()

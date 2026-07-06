"""
schemas/__init__.py
--------------------
Marshmallow schema package for the Smart Resume Screening System.

All request validation schemas live here, organized by domain:
  auth.py         : Registration and login schemas (Stage 2)
  jobs.py         : Job creation and update schemas (Stage 3)
  resumes.py      : Resume upload schemas (Stage 4)
  applications.py : Application schemas (Stage 5)

Usage:
  from app.schemas.auth import CandidateRegistrationSchema
  schema = CandidateRegistrationSchema()
  data = schema.load(request.get_json())
"""

"""
api/resumes/__init__.py
------------------------
Resumes Blueprint package.

Exports the resumes_bp Blueprint which is registered by the application
factory under the /api/v1/resumes URL prefix.

Registered routes (Batch 4C):
  POST   /api/v1/resumes/                          — Upload a resume file
  GET    /api/v1/resumes/                          — List own resumes (paginated)
  GET    /api/v1/resumes/stats                     — Aggregate resume statistics
  GET    /api/v1/resumes/<resume_id>               — Get single resume detail
  PATCH  /api/v1/resumes/<resume_id>/set-primary   — Set as primary resume
  DELETE /api/v1/resumes/<resume_id>               — Soft-delete a resume

Planned routes (future stages):
  GET    /api/v1/resumes/<resume_id>/download      — Stream file download (Stage 5)
  GET    /api/v1/resumes/<resume_id>/parsed-data   — AI-parsed content (Stage 5)
"""

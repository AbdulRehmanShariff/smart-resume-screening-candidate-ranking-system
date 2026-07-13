"""
api/jobs/__init__.py
---------------------
Jobs Blueprint package.

Exports the jobs_bp Blueprint which is registered by the application
factory under the /api/v1/jobs URL prefix.

Registered routes (Batch 3C):
  POST   /api/v1/jobs                         — Create a new job posting (recruiter)
  GET    /api/v1/jobs                         — Candidate-facing public job search
  GET    /api/v1/jobs/my                      — Recruiter's own jobs (all statuses)
  GET    /api/v1/jobs/<job_id>                — Fetch a single job (visibility-aware)
  PATCH  /api/v1/jobs/<job_id>               — Partial update of a job posting (recruiter)
  DELETE /api/v1/jobs/<job_id>               — Soft-delete a job posting (recruiter)
  POST   /api/v1/jobs/<job_id>/publish        — Transition: draft/paused → published
  POST   /api/v1/jobs/<job_id>/pause          — Transition: published → paused
  POST   /api/v1/jobs/<job_id>/close          — Transition: published/paused → closed
  POST   /api/v1/jobs/<job_id>/archive        — Transition: closed → archived

Planned routes (future batches):
  GET    /api/v1/jobs/<job_id>/stats          — Application count breakdown (Stage 3D)
  GET    /api/v1/jobs/<job_id>/applications   — Applicant list (Stage 5)
"""

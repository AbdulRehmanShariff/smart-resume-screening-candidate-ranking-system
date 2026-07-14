"""
api/applications/__init__.py
-----------------------------
Applications Blueprint package.

Exports the applications_bp Blueprint which is registered by the application
factory under the /api/v1/applications URL prefix.

Registered routes (Batch 5C):
  POST   /api/v1/applications/                      — Submit a new application (candidate)
  GET    /api/v1/applications/                      — List own applications (candidate)
  GET    /api/v1/applications/<application_id>      — Get single application (candidate or recruiter)
  POST   /api/v1/applications/<application_id>/withdraw — Withdraw application (candidate)
  PATCH  /api/v1/applications/<application_id>/status  — Update status (recruiter)
  PATCH  /api/v1/applications/<application_id>/notes   — Update private notes (recruiter)

Recruiter job-scoped applicant list:
  GET    /api/v1/jobs/<job_id>/applicants           — All applicants for a job (recruiter)
  (This endpoint is registered on the jobs blueprint in app/api/jobs/routes.py
   and calls application_service.list_job_applicants() directly.)
"""

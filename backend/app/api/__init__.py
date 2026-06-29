"""
api/__init__.py
---------------
API package for the Smart Resume Screening System.

All REST API blueprints live under this package, organized by domain:

  health/     : System health check (Stage 1A)
  auth/       : Authentication — register, login, JWT, email verify (Stage 2)
  jobs/       : Job posting CRUD (Stage 3)
  resume/     : Resume upload and management (Stage 4)
  candidates/ : Candidate profiles and applications (Stage 5)
  recruiters/ : Recruiter-specific views and tools (Stage 5)
  admin/      : Platform administration (Stage 6)
  ai/         : AI-powered features — ranking, parsing, chat (Stage 4+)

All blueprints are registered in app/__init__.py under the /api/v1 prefix.
"""

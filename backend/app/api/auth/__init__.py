"""
api/auth/__init__.py
---------------------
Authentication Blueprint package.

Exports the auth_bp Blueprint which is registered by the application
factory under the /api/v1/auth URL prefix.

Registered routes (Batch 2A):
  POST /api/v1/auth/register/candidate — Candidate registration
  POST /api/v1/auth/register/recruiter — Recruiter registration
  POST /api/v1/auth/login              — User login (returns JWT tokens)

Planned routes (future batches):
  POST /api/v1/auth/logout             — Revoke tokens (Batch 2B)
  POST /api/v1/auth/refresh            — Refresh access token (Batch 2B)
  GET  /api/v1/auth/verify-email       — Email verification (Batch 2B)
  POST /api/v1/auth/forgot-password    — Request password reset (Batch 2B)
  POST /api/v1/auth/reset-password     — Complete password reset (Batch 2B)
  GET  /api/v1/auth/me                 — Current user profile (Batch 2B)
"""

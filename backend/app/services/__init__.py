"""
services/__init__.py
---------------------
Service layer package for the Smart Resume Screening System.

All business logic lives in this package, organized by domain:
  auth_service.py          : Registration, login, token management (Stage 2)
  resume_service.py        : Resume upload, parsing, management (Stage 4)
  job_service.py           : Job posting CRUD and lifecycle (Stage 3)
  application_service.py   : Application submission and status management (Stage 5)
  ai_service.py            : AI ranking, parsing, interview generation (Stage 4+)
  notification_service.py  : In-app notification creation and delivery (Stage 5+)
  email_service.py         : Email sending via Flask-Mail (Stage 2+)

Coding contract for all service modules:
  - Functions receive plain Python dicts/primitives; not Flask request objects.
  - All database commits happen inside service functions; callers do NOT commit.
  - Exceptions are raised using app.core.exceptions; route handlers catch them.
  - No Flask request context is accessed directly — context data (ip_address,
    user_agent) is passed in as function arguments for testability.
"""

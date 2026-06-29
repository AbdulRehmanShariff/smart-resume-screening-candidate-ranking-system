"""
tests/__init__.py
-----------------
Test package for the Smart Resume Screening System backend.

Test organization:
  test_health.py      : Health check endpoint tests (Stage 1A)
  test_auth.py        : Authentication tests (Stage 2)
  test_jobs.py        : Job posting CRUD tests (Stage 3)
  test_resume.py      : Resume upload and parsing tests (Stage 4)
  test_candidates.py  : Candidate profile tests (Stage 5)
  test_ranking.py     : AI ranking and scoring tests (Stage 5)

Run all tests:
  pytest tests/ -v

Run a specific class:
  pytest tests/test_health.py::TestHealthEndpoint -v

Run with a specific marker:
  pytest tests/ -m unit -v
"""

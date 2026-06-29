"""
wsgi.py
-------
Production WSGI entry point for the Smart Resume Screening System.

This file is used by Gunicorn (and compatible WSGI servers) in production.
The WSGI callable is named `application` following the WSGI spec (PEP 3333).

Production start command:
  gunicorn wsgi:application --bind 0.0.0.0:5000 --workers 4 --timeout 120

Recommended Gunicorn options for production:
  --workers     : Set to (2 × CPU cores) + 1. Example: 4 workers for 2-core machine.
  --worker-class: Use 'gthread' for I/O-bound workloads (AI API calls, DB queries).
  --timeout     : Set to 120 seconds for AI processing endpoints.
  --access-logfile: Enable access logging for monitoring.
  --error-logfile : Separate error log file.

Example Render.com start command:
  gunicorn wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120

Do NOT set debug=True or use the Flask development server in production.
"""

import os

from dotenv import load_dotenv

# Load environment variables from .env file (if present).
# On cloud platforms (Render, Heroku), env vars are injected by the platform
# and .env will not exist — load_dotenv() is a no-op in that case.
load_dotenv()

from app import create_app  # noqa: E402 — import after load_dotenv intentional

# The WSGI callable must be named `application` for WSGI server compatibility.
application = create_app(os.environ.get("FLASK_ENV", "production"))

if __name__ == "__main__":
    # Allows running `python wsgi.py` locally as a quick sanity check.
    # In production, always use Gunicorn — never run wsgi.py directly.
    application.run()

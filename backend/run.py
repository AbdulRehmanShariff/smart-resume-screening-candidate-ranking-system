"""
run.py
------
Development server entry point for the Smart Resume Screening System.

Usage:
  python run.py

Environment variables read from .env:
  FLASK_ENV  : Configuration profile (default: development)
  FLASK_HOST : Host to bind (default: 0.0.0.0)
  FLASK_PORT : Port to listen on (default: 5000)

Do NOT use this file in production. Use wsgi.py with Gunicorn instead:
  gunicorn --bind 0.0.0.0:5000 wsgi:application
"""

import os

from dotenv import load_dotenv

# Load environment variables from .env before importing the app factory.
# This must happen before any Flask or SQLAlchemy code runs so that
# all os.environ.get() calls in config.py return the correct values.
load_dotenv()

from app import create_app  # noqa: E402 — import after load_dotenv intentional

app = create_app(os.environ.get("FLASK_ENV", "development"))

if __name__ == "__main__":
    host: str = os.environ.get("FLASK_HOST", "0.0.0.0")
    port: int = int(os.environ.get("FLASK_PORT", 5000))
    debug: bool = app.config.get("DEBUG", False)

    app.run(
        host=host,
        port=port,
        debug=debug,
        use_reloader=debug,     # Auto-reload on code changes in development
    )

"""
tests/conftest.py
-----------------
Pytest fixtures shared across all test modules.

Fixtures defined here:
  app    : A Flask application instance configured for testing.
           Scope is 'session' — created once and reused across all tests.
  client : A Flask test client scoped to a single test function.
           Each test gets a fresh client to prevent state leakage.
  db_session : (Added in Stage 1B) Database session with transaction rollback.

Usage in test files:
  def test_something(client):
      response = client.get("/api/v1/health")
      assert response.status_code == 200
"""

import pytest

from app import create_app
from app.extensions import db as _db


@pytest.fixture(scope="session")
def app():
    """
    Create a Flask application configured for the test suite.

    Uses 'testing' configuration which:
      - Uses SQLite in-memory database (or TEST_DATABASE_URL if set)
      - Disables email sending
      - Reduces bcrypt cost for fast tests
      - Suppresses file-based logging

    Scope: session — the app is created once for the entire test run.
    """
    flask_app = create_app("testing")

    # Establish application context for the entire test session.
    with flask_app.app_context():
        yield flask_app


@pytest.fixture(scope="function")
def client(app):
    """
    Create a Flask test client for a single test function.

    Each test receives a fresh client. Using function scope ensures
    that request context state does not leak between tests.

    Args:
        app : The session-scoped Flask application fixture.

    Yields:
        flask.testing.FlaskClient : The test client.
    """
    with app.test_client() as test_client:
        yield test_client

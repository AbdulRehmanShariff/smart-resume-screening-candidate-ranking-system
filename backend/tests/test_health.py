"""
tests/test_health.py
--------------------
Test suite for the health check endpoint.

Endpoint: GET /api/v1/health
Auth:      None required
Stage:     1A — Project Foundation

Tests verify:
  1. The endpoint exists and responds.
  2. The HTTP status code is 200.
  3. The response body follows the standard API envelope.
  4. All expected data fields are present.
  5. Non-existent endpoints return 404.
  6. Unsupported HTTP methods return 405.
"""

import pytest


class TestHealthEndpoint:
    """
    Tests for GET /api/v1/health.

    Groups all health-check related tests under a single class for
    clean output and easy targeting with `pytest -k TestHealthEndpoint`.
    """

    def test_health_returns_http_200(self, client):
        """
        The health endpoint must always return HTTP 200.
        A non-200 response indicates the application failed to start.
        """
        response = client.get("/api/v1/health")
        assert response.status_code == 200

    def test_health_content_type_is_json(self, client):
        """
        The response Content-Type must be application/json.
        All API endpoints in this system return JSON exclusively.
        """
        response = client.get("/api/v1/health")
        assert "application/json" in response.content_type

    def test_health_envelope_structure(self, client):
        """
        Response body must conform to the standard API envelope:
          { "success": true, "message": "...", "data": { ... } }
        """
        response = client.get("/api/v1/health")
        body = response.get_json()

        assert body is not None, "Response body must be valid JSON"
        assert "success" in body, "Envelope must contain 'success'"
        assert "message" in body, "Envelope must contain 'message'"
        assert "data" in body, "Envelope must contain 'data'"

    def test_health_success_flag_is_true(self, client):
        """
        The `success` field must be True for a healthy response.
        """
        body = client.get("/api/v1/health").get_json()
        assert body["success"] is True

    def test_health_data_status_is_ok(self, client):
        """
        data.status must equal 'ok' when the application is running.
        """
        body = client.get("/api/v1/health").get_json()
        assert body["data"]["status"] == "ok"

    def test_health_data_version_present(self, client):
        """
        data.version must be present and follow semantic versioning format.
        """
        body = client.get("/api/v1/health").get_json()
        version = body["data"].get("version")
        assert version is not None, "Version field must be present"
        assert isinstance(version, str), "Version must be a string"
        # Verify semantic versioning format (e.g. '1.0.0')
        parts = version.split(".")
        assert len(parts) == 3, f"Version '{version}' must follow X.Y.Z format"

    def test_health_data_application_name_present(self, client):
        """
        data.application must contain the project name.
        """
        body = client.get("/api/v1/health").get_json()
        assert body["data"].get("application") is not None

    def test_health_data_uptime_seconds_is_non_negative_integer(self, client):
        """
        data.uptime_seconds must be a non-negative integer representing
        the number of seconds the application has been running.
        """
        body = client.get("/api/v1/health").get_json()
        uptime = body["data"].get("uptime_seconds")
        assert uptime is not None, "uptime_seconds must be present"
        assert isinstance(uptime, int), "uptime_seconds must be an integer"
        assert uptime >= 0, "uptime_seconds must be non-negative"

    def test_health_data_database_field_present(self, client):
        """
        data.database must be present and indicate connectivity status.
        Expected values: 'healthy' | 'unavailable'
        """
        body = client.get("/api/v1/health").get_json()
        db_status = body["data"].get("database")
        assert db_status is not None, "database field must be present"
        assert db_status in ("healthy", "unavailable"), (
            f"database status must be 'healthy' or 'unavailable', got '{db_status}'"
        )

    def test_health_data_python_version_present(self, client):
        """
        data.python_version must be a non-empty string.
        """
        body = client.get("/api/v1/health").get_json()
        python_version = body["data"].get("python_version")
        assert python_version is not None, "python_version must be present"
        assert isinstance(python_version, str) and len(python_version) > 0


class TestErrorHandlers:
    """
    Tests for global Flask error handlers registered in the app factory.
    Verifies that errors return the standard JSON envelope.
    """

    def test_unknown_endpoint_returns_404(self, client):
        """
        Requests to non-existent endpoints must return HTTP 404 with
        the standard error envelope.
        """
        response = client.get("/api/v1/this-endpoint-does-not-exist")
        assert response.status_code == 404

        body = response.get_json()
        assert body is not None
        assert body["success"] is False
        assert "message" in body

    def test_method_not_allowed_returns_405(self, client):
        """
        Using an unsupported HTTP method on a known endpoint must return
        HTTP 405 with the standard error envelope.
        """
        # Health endpoint only accepts GET — POST should be rejected
        response = client.post("/api/v1/health")
        assert response.status_code == 405

        body = response.get_json()
        assert body is not None
        assert body["success"] is False
        assert "message" in body

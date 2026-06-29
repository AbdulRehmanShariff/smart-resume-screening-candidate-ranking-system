"""
app/__init__.py
---------------
Application Factory for the Smart Resume Screening System.

The create_app() function is the single entry point for building a Flask
application instance. It accepts a configuration name (development /
production / testing) and returns a fully initialized Flask app.

Responsibilities:
  1. Load environment-specific configuration.
  2. Configure logging before anything else.
  3. Initialize all Flask extensions (db, jwt, bcrypt, mail, cors).
  4. Register all API blueprints under the versioned prefix /api/v1/.
  5. Register global error handlers for consistent JSON error responses.
  6. Register JWT event callbacks (token expiry, invalid token, etc.).

Usage:
  # In run.py (development):
  from app import create_app
  app = create_app("development")

  # In wsgi.py (production):
  from app import create_app
  application = create_app("production")

  # In tests:
  from app import create_app
  app = create_app("testing")
"""

import importlib
import logging
import os

from flask import Flask, jsonify

from app.config import config_by_name
from app.core.exceptions import AppException
from app.core.logger import configure_logging
from app.extensions import bcrypt, cors, db, jwt, mail, migrate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public Factory
# ---------------------------------------------------------------------------


def create_app(config_name: str = None) -> Flask:
    """
    Create and configure a Flask application instance.

    Args:
        config_name : One of 'development', 'production', 'testing', or
                      'default'. If None, reads the FLASK_ENV environment
                      variable; falls back to 'development'.

    Returns:
        A fully configured Flask application instance.
    """
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(__name__)

    # ------------------------------------------------------------------
    # Step 1: Load configuration
    # ------------------------------------------------------------------
    config_class = config_by_name.get(config_name, config_by_name["default"])
    app.config.from_object(config_class)

    # ------------------------------------------------------------------
    # Step 2: Configure logging (must come before anything else logs)
    # ------------------------------------------------------------------
    configure_logging(app)

    # ------------------------------------------------------------------
    # Step 3: Initialize Flask extensions
    # ------------------------------------------------------------------
    _init_extensions(app)

    # ------------------------------------------------------------------
    # Step 3.5: Import all models so Alembic can discover them
    # This block must run after extensions are initialized and before
    # any Flask-Migrate commands are invoked.
    # ------------------------------------------------------------------
    with app.app_context():
        # Use importlib.import_module rather than 'import app.models' to avoid
        # Python silently rebinding the local variable 'app' (Flask instance) to
        # sys.modules['app'] (the package module). The plain import statement
        # `import app.models` always binds the top-level name 'app' in the local
        # scope, overwriting the Flask instance and causing every subsequent
        # reference to app.config to resolve to the config.py submodule instead
        # of Flask's Config dict.
        importlib.import_module("app.models")  # noqa: F401 — side-effect import for Alembic

    # ------------------------------------------------------------------
    # Step 4: Register API blueprints
    # ------------------------------------------------------------------
    _register_blueprints(app)

    # ------------------------------------------------------------------
    # Step 5: Register global error handlers
    # ------------------------------------------------------------------
    _register_error_handlers(app)

    # ------------------------------------------------------------------
    # Step 6: Register JWT event callbacks
    # ------------------------------------------------------------------
    _register_jwt_callbacks(app)

    logger.info(
        "Application started | name='%s' | env='%s'",
        app.config.get("APP_NAME"),
        config_name,
    )

    return app


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _init_extensions(app: Flask) -> None:
    """
    Initialize all Flask extensions with the application instance.

    Extensions are bound to the app here using init_app() so that they
    can be imported and used anywhere without circular import issues.
    """
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    bcrypt.init_app(app)
    mail.init_app(app)

    cors.init_app(
        app,
        resources={
            r"/api/*": {
                "origins": app.config.get("CORS_ORIGINS", []),
                "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                "allow_headers": ["Content-Type", "Authorization"],
            }
        },
        supports_credentials=True,
    )

    logger.debug("All Flask extensions initialized successfully")


def _register_blueprints(app: Flask) -> None:
    """
    Register all API blueprints under the versioned URL prefix.

    Each blueprint corresponds to one domain of the application.
    Blueprints for features not yet implemented will be added here
    in their respective sub-stages.
    """
    api_prefix: str = app.config.get("API_PREFIX", "/api/v1")

    # ---- Health Check (Stage 1A) ----
    from app.api.health.routes import health_bp
    app.register_blueprint(health_bp, url_prefix=api_prefix)

    # ---- Authentication (Stage 2) ----
    # from app.api.auth.routes import auth_bp
    # app.register_blueprint(auth_bp, url_prefix=f"{api_prefix}/auth")

    # ---- Jobs (Stage 3) ----
    # from app.api.jobs.routes import jobs_bp
    # app.register_blueprint(jobs_bp, url_prefix=f"{api_prefix}/jobs")

    # ---- Resume (Stage 4) ----
    # from app.api.resume.routes import resume_bp
    # app.register_blueprint(resume_bp, url_prefix=f"{api_prefix}/resumes")

    # ---- Candidates (Stage 5) ----
    # from app.api.candidates.routes import candidates_bp
    # app.register_blueprint(candidates_bp, url_prefix=f"{api_prefix}/candidates")

    # ---- Recruiters (Stage 5) ----
    # from app.api.recruiters.routes import recruiters_bp
    # app.register_blueprint(recruiters_bp, url_prefix=f"{api_prefix}/recruiters")

    # ---- Admin (Stage 6) ----
    # from app.api.admin.routes import admin_bp
    # app.register_blueprint(admin_bp, url_prefix=f"{api_prefix}/admin")

    # ---- AI (Stage 4+) ----
    # from app.api.ai.routes import ai_bp
    # app.register_blueprint(ai_bp, url_prefix=f"{api_prefix}/ai")

    logger.debug("Blueprints registered under prefix '%s'", api_prefix)


def _register_error_handlers(app: Flask) -> None:
    """
    Register global Flask error handlers.

    All application exceptions are caught here and serialized to a
    consistent JSON error envelope so that the frontend can always
    rely on the same response structure regardless of the error type.
    """

    @app.errorhandler(AppException)
    def handle_app_exception(error: AppException):
        """Handle all custom application exceptions."""
        logger.warning(
            "AppException | status=%s | message=%s",
            error.status_code,
            error.message,
        )
        return jsonify(error.to_dict()), error.status_code

    @app.errorhandler(400)
    def handle_bad_request(error):
        return jsonify({
            "success": False,
            "message": "Bad request. Please check your input",
        }), 400

    @app.errorhandler(404)
    def handle_not_found(error):
        return jsonify({
            "success": False,
            "message": "The requested endpoint does not exist",
        }), 404

    @app.errorhandler(405)
    def handle_method_not_allowed(error):
        return jsonify({
            "success": False,
            "message": "HTTP method not allowed on this endpoint",
        }), 405

    @app.errorhandler(413)
    def handle_payload_too_large(error):
        return jsonify({
            "success": False,
            "message": "File size exceeds the maximum allowed limit of 10 MB",
        }), 413

    @app.errorhandler(429)
    def handle_too_many_requests(error):
        return jsonify({
            "success": False,
            "message": "Too many requests. Please slow down and try again later",
        }), 429

    @app.errorhandler(500)
    def handle_internal_server_error(error):
        logger.error(
            "Unhandled internal server error: %s",
            str(error),
            exc_info=True,
        )
        return jsonify({
            "success": False,
            "message": "An internal server error occurred. Please try again later",
        }), 500

    logger.debug("Global error handlers registered")


def _register_jwt_callbacks(app: Flask) -> None:
    """
    Register JWT lifecycle event callbacks.

    These callbacks ensure that JWT errors produce the same JSON envelope
    as all other errors in the system. The token blocklist loader will be
    added in Stage 2 when the TokenBlocklist model is available.
    """

    @jwt.expired_token_loader
    def handle_expired_token(jwt_header: dict, jwt_payload: dict):
        """Called when an access or refresh token has expired."""
        return jsonify({
            "success": False,
            "message": "Your session has expired. Please log in again",
        }), 401

    @jwt.invalid_token_loader
    def handle_invalid_token(error: str):
        """Called when the token cannot be decoded or is malformed."""
        return jsonify({
            "success": False,
            "message": "Invalid authentication token. Please log in again",
        }), 401

    @jwt.unauthorized_loader
    def handle_missing_token(error: str):
        """Called when a protected endpoint is accessed without a token."""
        return jsonify({
            "success": False,
            "message": "Authentication token is required to access this resource",
        }), 401

    @jwt.revoked_token_loader
    def handle_revoked_token(jwt_header: dict, jwt_payload: dict):
        """Called when a token has been explicitly revoked (e.g. after logout)."""
        return jsonify({
            "success": False,
            "message": "This session has been revoked. Please log in again",
        }), 401

    @jwt.needs_fresh_token_loader
    def handle_fresh_token_required(jwt_header: dict, jwt_payload: dict):
        """Called when a fresh token is required (e.g. sensitive operations)."""
        return jsonify({
            "success": False,
            "message": "A fresh login is required to perform this action",
        }), 401

    @jwt.token_in_blocklist_loader
    def check_if_token_is_revoked(jwt_header: dict, jwt_payload: dict) -> bool:
        """
        Called automatically on every request protected by @jwt_required().
        Checks whether the token's JTI has been explicitly revoked
        (e.g. user logged out, admin suspended account).

        Returns:
            True  — token is blocklisted; Flask-JWT-Extended returns 401.
            False — token is clean; request proceeds normally.
        """
        from app.models.token_blocklist import TokenBlocklist
        jti: str = jwt_payload.get("jti", "")
        return TokenBlocklist.is_jti_blocklisted(jti)

    logger.debug("JWT event callbacks registered")

"""
extensions.py
-------------
Flask extension instances for the Smart Resume Screening System.

All extensions are instantiated here without being bound to any Flask app.
They are initialized inside the application factory (app/__init__.py) using
the init_app() pattern. This prevents circular imports and allows the same
extension instances to be reused across the application and test suite.

Import pattern:
  from app.extensions import db, jwt, bcrypt, mail, cors, migrate
"""

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import JWTManager
from flask_bcrypt import Bcrypt
from flask_mail import Mail
from flask_cors import CORS


# ---------------------------------------------------------------------------
# Database ORM
# Used throughout the application for all model definitions and queries.
# ---------------------------------------------------------------------------
db: SQLAlchemy = SQLAlchemy()

# ---------------------------------------------------------------------------
# Database Migration Manager
# Wraps Alembic to provide `flask db init / migrate / upgrade` commands.
# Initialized with both `app` and `db` inside the factory.
# ---------------------------------------------------------------------------
migrate: Migrate = Migrate()

# ---------------------------------------------------------------------------
# JWT Authentication Manager
# Handles token creation, verification, and custom error callbacks.
# Token blocklist integration is registered in the app factory (Stage 2).
# ---------------------------------------------------------------------------
jwt: JWTManager = JWTManager()

# ---------------------------------------------------------------------------
# Password Hashing
# Provides bcrypt hashing for user passwords. Never store plain-text passwords.
# ---------------------------------------------------------------------------
bcrypt: Bcrypt = Bcrypt()

# ---------------------------------------------------------------------------
# Email Sender
# Used for email verification, password reset, and interview invitations.
# ---------------------------------------------------------------------------
mail: Mail = Mail()

# ---------------------------------------------------------------------------
# Cross-Origin Resource Sharing
# Configured in the factory to restrict access to known frontend origins only.
# ---------------------------------------------------------------------------
cors: CORS = CORS()

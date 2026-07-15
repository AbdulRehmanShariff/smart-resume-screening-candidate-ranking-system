"""
config.py
---------
Environment-based configuration for the Smart Resume Screening System.

Three configurations are provided:
  - DevelopmentConfig : Local development with verbose logging and debug mode.
  - ProductionConfig  : Hardened settings for live deployment.
  - TestingConfig     : Isolated settings for the pytest suite.

Usage:
  from app.config import config_by_name
  config_class = config_by_name["development"]
"""

import os
from datetime import timedelta


class BaseConfig:
    """
    Shared configuration inherited by all environments.
    All values are read from environment variables with safe defaults.
    Sensitive values have no defaults and will raise errors if missing
    when the application starts.
    """

    # ------------------------------------------------------------------
    # Application Identity
    # ------------------------------------------------------------------
    APP_NAME: str = "Smart Resume Screening & Candidate Ranking System"
    API_VERSION: str = "v1"
    API_PREFIX: str = "/api/v1"

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    SECRET_KEY: str = os.environ.get("SECRET_KEY")
    JWT_SECRET_KEY: str = os.environ.get("JWT_SECRET_KEY")

    # Token lifetimes
    JWT_ACCESS_TOKEN_EXPIRES: timedelta = timedelta(hours=1)
    JWT_REFRESH_TOKEN_EXPIRES: timedelta = timedelta(days=30)

    # Algorithm — HS256 is sufficient for symmetric signing
    JWT_ALGORITHM: str = "HS256"

    # Enable token blocklist checks (logout / revocation — Stage 2)
    JWT_BLACKLIST_ENABLED: bool = True
    JWT_BLACKLIST_TOKEN_CHECKS: list = ["access", "refresh"]

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    SQLALCHEMY_DATABASE_URI: str = os.environ.get("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    SQLALCHEMY_ECHO: bool = False

    # Connection pool tuning for PostgreSQL
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_pre_ping": True,     # Detect stale connections
        "pool_recycle": 300,       # Recycle connections every 5 minutes
        "pool_size": 10,           # Maintain up to 10 connections
        "max_overflow": 20,        # Allow 20 overflow connections under load
    }

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    CORS_ORIGINS: list = os.environ.get(
        "CORS_ORIGINS", "http://localhost:3000"
    ).split(",")

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------
    MAIL_SERVER: str = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT: int = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS: bool = os.environ.get("MAIL_USE_TLS", "True").lower() == "true"
    MAIL_USE_SSL: bool = False
    MAIL_USERNAME: str = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD: str = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER: str = os.environ.get(
        "MAIL_DEFAULT_SENDER", "noreply@smartresume.com"
    )

    # Base URL of the frontend — used to build email verification and
    # password reset links embedded in transactional emails.
    FRONTEND_URL: str = os.environ.get("FRONTEND_URL", "http://localhost:3000")

    # ------------------------------------------------------------------
    # File Uploads
    # ------------------------------------------------------------------
    MAX_CONTENT_LENGTH: int = int(
        os.environ.get("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)  # 10 MB
    )
    UPLOAD_FOLDER: str = os.environ.get("UPLOAD_FOLDER", "uploads")

    ALLOWED_RESUME_EXTENSIONS: frozenset = frozenset(
        {"pdf", "docx", "txt", "png", "jpg", "jpeg"}
    )
    ALLOWED_JD_EXTENSIONS: frozenset = frozenset({"pdf", "txt"})

    # Maximum file sizes (read by LocalStorage.from_app())
    MAX_RESUME_SIZE_MB: int = int(os.environ.get("MAX_RESUME_SIZE_MB", 10))
    MAX_JD_SIZE_MB: int = int(os.environ.get("MAX_JD_SIZE_MB", 5))

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.environ.get("LOG_FILE", "logs/app.log")
    LOG_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB per log file
    LOG_BACKUP_COUNT: int = 5              # Keep 5 rotated files

    # ------------------------------------------------------------------
    # Pagination Defaults
    # ------------------------------------------------------------------
    DEFAULT_PAGE_SIZE: int = 20
    MAX_PAGE_SIZE: int = 100

    # ------------------------------------------------------------------
    # AI Services (activated in Stage 6)
    # ------------------------------------------------------------------
    GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY")
    GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")

    # Stage 6 AI Pipeline Config
    GOOGLE_API_KEY: str = os.environ.get("GOOGLE_API_KEY", GEMINI_API_KEY)
    AI_MODEL_NAME: str = os.environ.get("AI_MODEL_NAME", "gemini-2.5-flash")
    AI_TEMPERATURE: float = float(os.environ.get("AI_TEMPERATURE", 0.2))
    AI_EMBEDDING_MODEL: str = os.environ.get("AI_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    EMBEDDINGS_DIR: str = os.environ.get("EMBEDDINGS_DIR", "embeddings/")
    FAISS_INDEX_PATH: str = os.environ.get("FAISS_INDEX_PATH", "embeddings/resumes.faiss")
    AI_WORKER_POLL_INTERVAL: int = int(os.environ.get("AI_WORKER_POLL_INTERVAL", 5))
    AI_WORKER_BATCH_SIZE: int = int(os.environ.get("AI_WORKER_BATCH_SIZE", 5))


# --------------------------------------------------------------------------


class DevelopmentConfig(BaseConfig):
    """
    Development configuration.
    Enables debug mode, verbose SQL echo, and DEBUG-level logging.
    """

    DEBUG: bool = True
    TESTING: bool = False
    ENV_NAME: str = "development"

    # Log all SQL queries to console for easier debugging
    SQLALCHEMY_ECHO: bool = True

    # More verbose logging during development
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "DEBUG")


# --------------------------------------------------------------------------


class ProductionConfig(BaseConfig):
    """
    Production configuration.
    Enforces strict security settings and suppresses verbose output.
    """

    DEBUG: bool = False
    TESTING: bool = False
    ENV_NAME: str = "production"

    # Shorter access token lifetime in production (security hardening)
    JWT_ACCESS_TOKEN_EXPIRES: timedelta = timedelta(minutes=30)

    # SQL echo is off in production — never expose query details
    SQLALCHEMY_ECHO: bool = False

    # Reduce log verbosity — only warnings and above
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "WARNING")

    # Session cookie hardening
    SESSION_COOKIE_SECURE: bool = True
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"


# --------------------------------------------------------------------------


class TestingConfig(BaseConfig):
    """
    Testing configuration.
    Uses an in-memory SQLite database by default for speed and isolation.
    Disables email sending and reduces bcrypt cost for fast test runs.
    """

    DEBUG: bool = True
    TESTING: bool = True
    ENV_NAME: str = "testing"

    # Use test database if specified, otherwise fall back to SQLite
    SQLALCHEMY_DATABASE_URI: str = os.environ.get(
        "TEST_DATABASE_URL", "sqlite:///:memory:"
    )

    # Disable SQL echo in tests to keep output clean
    SQLALCHEMY_ECHO: bool = False

    # Short token lifetime — tests should not depend on token expiry
    JWT_ACCESS_TOKEN_EXPIRES: timedelta = timedelta(minutes=5)
    JWT_REFRESH_TOKEN_EXPIRES: timedelta = timedelta(minutes=10)

    # Reduced bcrypt rounds for fast test execution (never use in prod)
    BCRYPT_LOG_ROUNDS: int = 4

    # Suppress actual emails during tests
    MAIL_SUPPRESS_SEND: bool = True

    # Disable CSRF for API tests
    WTF_CSRF_ENABLED: bool = False

    # Keep test logs to console only — no file output
    LOG_LEVEL: str = "ERROR"


# --------------------------------------------------------------------------

# Registry mapping environment name strings to config classes.
# Used by the application factory and by run.py / wsgi.py.
config_by_name: dict = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}

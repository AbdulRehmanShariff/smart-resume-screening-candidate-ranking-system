"""
core/logger.py
--------------
Centralized logging configuration for the Smart Resume Screening System.

Provides:
  - configure_logging(app) : Call once inside the application factory.
  - get_logger(name)        : Use in every module for named loggers.

Log output:
  - Console (StreamHandler) : Always active.
  - Rotating file           : Active in non-testing environments.
    Each file caps at 10 MB; 5 rotated backups are kept.

Log format:
  [2025-01-15 14:32:01] INFO     app.api.auth.routes            Login successful for user@example.com
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask


# ---------------------------------------------------------------------------
# Format Constants
# ---------------------------------------------------------------------------

LOG_FORMAT: str = (
    "[%(asctime)s] %(levelname)-8s %(name)-40s %(message)s"
)
DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_logging(app: Flask) -> None:
    """
    Configure application-wide logging.

    This function must be called once inside create_app(), after the app
    config is loaded but before any blueprints are registered.

    Args:
        app: The Flask application instance with config already loaded.
    """
    log_level_name: str = app.config.get("LOG_LEVEL", "INFO")
    log_level: int = getattr(logging, log_level_name.upper(), logging.INFO)

    log_file: str = app.config.get("LOG_FILE", "logs/app.log")
    max_bytes: int = app.config.get("LOG_MAX_BYTES", 10 * 1024 * 1024)
    backup_count: int = app.config.get("LOG_BACKUP_COUNT", 5)

    # ---- Prepare the root logger ----
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove any existing handlers to avoid duplicate output when
    # create_app() is called multiple times in tests.
    root_logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # ---- Console handler (always active) ----
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # ---- Rotating file handler (disabled in testing) ----
    if not app.testing:
        _add_file_handler(
            root_logger=root_logger,
            log_file=log_file,
            log_level=log_level,
            formatter=formatter,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )

    # ---- Silence noisy third-party loggers in non-debug mode ----
    if not app.debug:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)

    app.logger.info(
        "Logging initialized | level=%s | file=%s",
        log_level_name,
        "console-only" if app.testing else log_file,
    )


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger for use in any module.

    Usage:
        from app.core.logger import get_logger
        logger = get_logger(__name__)
        logger.info("Something happened")

    Args:
        name: Typically pass __name__ to get the module-scoped logger.

    Returns:
        A standard Python Logger instance.
    """
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _add_file_handler(
    root_logger: logging.Logger,
    log_file: str,
    log_level: int,
    formatter: logging.Formatter,
    max_bytes: int,
    backup_count: int,
) -> None:
    """
    Create the log directory if it does not exist and attach a
    RotatingFileHandler to the root logger.

    Args:
        root_logger  : The root logging.Logger instance.
        log_file     : Relative or absolute path to the log file.
        log_level    : Integer log level (e.g. logging.INFO).
        formatter    : The log message formatter.
        max_bytes    : Maximum size of a single log file in bytes.
        backup_count : Number of rotated backup files to retain.
    """
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    try:
        file_handler = RotatingFileHandler(
            filename=log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError as exc:
        # If the log file cannot be opened (e.g. permission issues),
        # continue with console-only logging rather than crashing the app.
        root_logger.warning(
            "Could not open log file '%s': %s. Falling back to console only.",
            log_file,
            exc,
        )

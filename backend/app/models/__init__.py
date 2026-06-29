"""
models/__init__.py
------------------
SQLAlchemy models package for the Smart Resume Screening System.

All models are defined in their own modules and imported here
to ensure Alembic (Flask-Migrate) can discover every table when
running `flask db migrate`.

Models are imported in dependency order:
  1. base        — Shared mixins (no DB dependencies)
  2. role        — Referenced by User
  3. user        — Referenced by all domain models
  4. token_blocklist
  5. candidate_profile, recruiter_profile
  6. job, resume
  7. application, notification
  8. audit_log, ai_processing_job, email_log, system_settings
"""

# Base & Auth Models
from app.models.base import SoftDeleteMixin, TimestampMixin  # noqa: F401
from app.models.role import Role  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.token_blocklist import TokenBlocklist  # noqa: F401

# Profile Models
from app.models.candidate_profile import CandidateProfile  # noqa: F401
from app.models.recruiter_profile import RecruiterProfile  # noqa: F401

# Domain Models
from app.models.job import Job  # noqa: F401
from app.models.resume import Resume  # noqa: F401
from app.models.application import Application, ApplicationStatus  # noqa: F401
from app.models.notification import (  # noqa: F401
    Notification,
    NotificationCategory,
    NotificationPriority,
    NotificationType,
)

# System & Audit Models
from app.models.audit_log import AuditLog, AuditAction  # noqa: F401
from app.models.ai_processing_job import (  # noqa: F401
    AIProcessingJob,
    AIJobStatus,
    AIJobType,
)
from app.models.email_log import (  # noqa: F401
    EmailLog,
    EmailStatus,
    EmailTemplate,
)
from app.models.system_settings import (  # noqa: F401
    SystemSettings,
    SettingsCategory,
    SettingsValueType,
)

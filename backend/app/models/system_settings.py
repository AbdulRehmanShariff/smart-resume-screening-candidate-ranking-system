"""
models/system_settings.py
--------------------------
SQLAlchemy model for the `system_settings` table.

Provides a flexible key-value configuration store for platform-wide settings
that must be changeable at runtime without code deployments. Admin users
can update settings through the admin panel — no environment variables or
code changes required.

Use cases:
  - AI configuration: model name, temperature, max tokens, provider API key
  - Storage: max upload size, allowed file types, upload path
  - Email: from address, reply-to, provider selection
  - Security: password policy, max login attempts, session timeout
  - Platform: maintenance mode, feature flags, pagination limits

Architecture decisions:
  - `key` VARCHAR UNIQUE: human-readable dot-namespaced key.
    Example: 'ai.model_name', 'storage.max_upload_mb', 'security.session_timeout_hours'.
  - `value` JSONB: stores any JSON-compatible type (string, integer, float,
    boolean, list, object). JSONB is flexible enough to store all setting types
    without separate columns.
  - `value_type` VARCHAR: tells `get_typed_value()` how to coerce the JSONB value
    to its canonical Python type. Required because JSONB stores 50 and 50.0 and
    "50" differently — the type annotation clarifies intent.
  - `is_sensitive` BOOLEAN: sensitive settings (API keys, secrets) are
    automatically masked to '***REDACTED***' in API responses via to_dict().
    The raw value is only accessible through get_typed_value() in server code.
  - `is_public` BOOLEAN: controls whether a setting appears in the public
    settings API (accessible without authentication). Non-public settings
    are restricted to admin-authenticated requests.
  - `updated_by` FK: tracks which admin last changed a setting for audit purposes.
    Combined with TimestampMixin.updated_at to form a lightweight change log.

Note: For a full change history, every admin settings update should also be
recorded in audit_logs with AuditAction.ADMIN_SETTINGS_UPDATED.

Seeding: Default settings are created via a Flask CLI command or a database
seed script. The `get_value_by_key()` classmethod provides the recommended
way to read settings throughout the codebase.

Relationships:
    system_settings N:1 users (as updated_by, nullable)
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


# ---------------------------------------------------------------------------
# SettingsCategory Enum
# ---------------------------------------------------------------------------


class SettingsCategory(str, enum.Enum):
    """
    Functional grouping for system settings, stored as VARCHAR(50).

    Used to organise the admin settings panel into logical sections.
    """

    AI = "ai"             # AI model, provider, parameters
    STORAGE = "storage"   # File upload limits, allowed types, storage paths
    EMAIL = "email"       # Email provider, from address, reply-to
    SECURITY = "security" # Password policy, session duration, rate limits
    PLATFORM = "platform" # Feature flags, maintenance mode, pagination, branding

    @property
    def label(self) -> str:
        """Return the human-readable label for this category."""
        return _CATEGORY_LABELS.get(self, self.value.capitalize())

    @classmethod
    def values(cls) -> tuple:
        """Return all valid category value strings."""
        return tuple(c.value for c in cls)


_CATEGORY_LABELS: dict = {
    SettingsCategory.AI: "AI Configuration",
    SettingsCategory.STORAGE: "Storage & Uploads",
    SettingsCategory.EMAIL: "Email Settings",
    SettingsCategory.SECURITY: "Security & Auth",
    SettingsCategory.PLATFORM: "Platform",
}


# ---------------------------------------------------------------------------
# SettingsValueType Enum
# ---------------------------------------------------------------------------


class SettingsValueType(str, enum.Enum):
    """
    The canonical Python type of a setting's value, stored as VARCHAR(20).

    JSONB in PostgreSQL stores Python types faithfully, but the declared
    value_type field makes the intended type explicit and enables
    `get_typed_value()` to safely coerce the JSONB value to its correct type.
    """

    STRING = "string"     # str: 'gemini-1.5-pro', 'noreply@example.com'
    INTEGER = "integer"   # int: 50, 3, 100
    FLOAT = "float"       # float: 0.7, 1.5
    BOOLEAN = "boolean"   # bool: true, false
    JSON = "json"         # dict: arbitrary JSON object
    LIST = "list"         # list: ['pdf', 'docx', 'txt']

    @classmethod
    def values(cls) -> tuple:
        """Return all valid value_type strings."""
        return tuple(t.value for t in cls)


# ---------------------------------------------------------------------------
# SystemSettings Model
# ---------------------------------------------------------------------------


class SystemSettings(TimestampMixin, db.Model):
    """
    A single configurable platform setting stored as a key-value pair.

    Keys are dot-namespaced strings (e.g. 'ai.model_name', 'storage.max_upload_mb').
    Values are JSONB (any JSON-compatible type). The `value_type` field declares
    the canonical Python type for safe deserialization via `get_typed_value()`.

    Reading a setting from application code:
        max_mb = SystemSettings.get_value_by_key('storage.max_upload_mb', default=25)
        model  = SystemSettings.get_value_by_key('ai.model_name', default='gemini-1.5-pro')

    Writing a setting from the admin panel (via the service layer):
        setting = SystemSettings.get_by_key('ai.temperature')
        setting.set_typed_value(0.7)
        db.session.commit()
    """

    __tablename__ = "system_settings"
    __table_args__ = (
        Index("ix_system_settings_category", "category"),
        Index("ix_system_settings_is_public", "is_public"),
        {
            "comment": (
                "Runtime-configurable key-value settings. "
                "Admin-editable without code changes or redeployment."
            )
        },
    )

    # ------------------------------------------------------------------
    # Expose Enums
    # ------------------------------------------------------------------

    Category = SettingsCategory
    ValueType = SettingsValueType

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        doc="Unique setting identifier (UUID v4).",
    )

    # ------------------------------------------------------------------
    # Key & Category
    # ------------------------------------------------------------------

    key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        doc=(
            "Dot-namespaced setting key. Unique across all settings. "
            "Convention: '<category>.<descriptor>'. "
            "Examples: 'ai.model_name', 'storage.max_upload_mb', "
            "'security.session_timeout_hours', 'platform.maintenance_mode'."
        ),
    )

    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc=(
            "Functional category for grouping in the admin panel. "
            "Use SettingsCategory enum constants."
        ),
    )

    # ------------------------------------------------------------------
    # Value
    # ------------------------------------------------------------------

    value: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "The setting value as a JSONB-compatible Python type. "
            "Retrieve via get_typed_value() for proper Python type coercion. "
            "Examples: 'gemini-1.5-pro' (string), 50 (integer), "
            "true (boolean), ['pdf', 'docx'] (list)."
        ),
    )

    value_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SettingsValueType.STRING.value,
        doc=(
            "Declared Python type of the value. "
            "Use SettingsValueType enum constants. "
            "Enables safe type coercion in get_typed_value()."
        ),
    )

    # ------------------------------------------------------------------
    # Documentation
    # ------------------------------------------------------------------

    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc=(
            "Human-readable name shown in the admin panel. "
            'Example: "AI Model Name", "Max Upload Size (MB)".'
        ),
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "Detailed description of what this setting controls and "
            "what valid values look like. Shown as help text in the admin panel."
        ),
    )

    # ------------------------------------------------------------------
    # Access Control
    # ------------------------------------------------------------------

    is_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc=(
            "True = this setting is accessible via the public settings API "
            "without authentication. "
            "Use for frontend configuration (e.g. allowed file types, "
            "platform name, feature flags). "
            "Never set True for sensitive settings."
        ),
    )

    is_sensitive: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc=(
            "True = the value contains sensitive data (API key, secret, password). "
            "Sensitive settings are masked to '***REDACTED***' in all "
            "API responses from to_dict(). "
            "The raw value is only accessible via get_typed_value() in server code."
        ),
    )

    # ------------------------------------------------------------------
    # Change Tracking
    # ------------------------------------------------------------------

    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        doc=(
            "UUID of the admin user who last modified this setting. "
            "NULL if set by a migration or seed script. "
            "SET NULL: setting is preserved if the admin is deleted."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    last_updated_by_user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="updated_settings",
        foreign_keys=[updated_by],
        lazy="select",
        doc="The admin user who last changed this setting.",
    )

    # ------------------------------------------------------------------
    # Value Accessors
    # ------------------------------------------------------------------

    def get_typed_value(self) -> Any:
        """
        Return the setting value coerced to its declared Python type.

        JSONB in PostgreSQL preserves most Python types faithfully. This
        method adds an explicit coercion layer for safety and to handle
        edge cases (e.g. an integer stored as a float due to JSON precision).

        Returns:
            The value coerced to the type declared in `value_type`.
            None if `value` is None.

        Raises:
            Does not raise — falls back to the raw JSONB value if coercion fails.
        """
        if self.value is None:
            return None

        vtype = self.value_type
        raw = self.value

        try:
            if vtype == SettingsValueType.STRING.value:
                return str(raw) if not isinstance(raw, str) else raw
            elif vtype == SettingsValueType.INTEGER.value:
                return int(raw)
            elif vtype == SettingsValueType.FLOAT.value:
                return float(raw)
            elif vtype == SettingsValueType.BOOLEAN.value:
                if isinstance(raw, bool):
                    return raw
                return str(raw).lower() in ("true", "1", "yes")
            elif vtype in (SettingsValueType.JSON.value, SettingsValueType.LIST.value):
                return raw  # JSONB already deserialized to dict/list
            else:
                return raw
        except (ValueError, TypeError):
            return raw  # Return raw value rather than raising

    def set_typed_value(self, value: Any) -> None:
        """
        Store a Python value in the JSONB `value` field.

        JSONB handles Python → JSON serialization automatically for all
        supported types. This method is a convenience wrapper to maintain
        a clean API surface for the service layer.

        Args:
            value: The new setting value. Must be JSONB-compatible
                   (str, int, float, bool, list, dict, or None).
        """
        self.value = value

    # ------------------------------------------------------------------
    # Class Methods
    # ------------------------------------------------------------------

    @classmethod
    def get_by_key(cls, key: str) -> Optional["SystemSettings"]:
        """
        Look up a setting by its unique key.

        Args:
            key: The dot-namespaced setting key (e.g. 'ai.model_name').

        Returns:
            The SystemSettings instance, or None if the key does not exist.
        """
        return db.session.execute(
            select(cls).where(cls.key == key)
        ).scalar_one_or_none()

    @classmethod
    def get_value_by_key(cls, key: str, default: Any = None) -> Any:
        """
        Get the typed value for a setting key, returning `default` if not found.

        This is the primary way to read settings from application code.
        It returns a properly typed Python value (not a raw JSONB object).

        Args:
            key    : The dot-namespaced setting key.
            default: Value to return if the key is not found. Defaults to None.

        Returns:
            The typed setting value, or `default` if the key does not exist.

        Usage:
            max_mb = SystemSettings.get_value_by_key('storage.max_upload_mb', default=25)
            model  = SystemSettings.get_value_by_key('ai.model_name', default='gemini-1.5-pro')
        """
        setting = cls.get_by_key(key)
        if setting is None:
            return default
        typed = setting.get_typed_value()
        return typed if typed is not None else default

    @classmethod
    def get_public_settings(cls) -> list:
        """
        Return all settings marked as public, as a list of to_dict() dicts.

        Used by the public settings API endpoint (no authentication required).

        Returns:
            list[dict]: All is_public=True settings, safe for unauthenticated access.
        """
        results = db.session.execute(
            select(cls).where(cls.is_public.is_(True))
        ).scalars().all()
        return [s.to_dict() for s in results]

    # ------------------------------------------------------------------
    # Dunder Methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<SystemSettings key={self.key!r} "
            f"category={self.category!r} "
            f"is_sensitive={self.is_sensitive}>"
        )

    def __str__(self) -> str:
        return f"{self.display_name} ({self.key})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SystemSettings):
            return self.key == other.key
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.key)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Serialize the setting to a JSON-safe dictionary.

        Sensitive settings (is_sensitive=True) have their value replaced
        with '***REDACTED***' for safety in API responses. The raw value
        is only accessible via get_typed_value() in server-side code.

        Returns:
            dict: Setting fields for admin panel API responses.
        """
        safe_value = (
            "***REDACTED***"
            if self.is_sensitive
            else self.get_typed_value()
        )

        return {
            "id": str(self.id),
            "key": self.key,
            "category": self.category,
            "category_label": (
                SettingsCategory(self.category).label
                if self.category in SettingsCategory.values()
                else self.category
            ),
            "display_name": self.display_name,
            "description": self.description,
            "value": safe_value,
            "value_type": self.value_type,
            "is_public": self.is_public,
            "is_sensitive": self.is_sensitive,
            "updated_by": (
                str(self.updated_by) if self.updated_by else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    def to_public_dict(self) -> dict:
        """
        Serialization for the unauthenticated public settings API.

        Returns only fields safe for unauthenticated exposure:
        key, display_name, and value (only if is_public=True).
        Never returns sensitive values.

        Returns:
            dict: Minimal public setting fields.
        """
        return {
            "key": self.key,
            "display_name": self.display_name,
            "value": (
                "***REDACTED***"
                if self.is_sensitive
                else self.get_typed_value()
            ),
        }

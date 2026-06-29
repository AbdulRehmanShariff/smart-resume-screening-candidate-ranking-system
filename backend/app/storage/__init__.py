"""
storage/__init__.py
--------------------
File storage package for the Smart Resume Screening System.

This package provides the storage abstraction layer for all uploaded files.
The LocalStorage adapter handles local filesystem storage for development
and self-hosted deployments. A cloud adapter (S3, GCS) will be added
in a future stage when cloud deployment is introduced.

Exports:
    LocalStorage  — Local filesystem storage adapter
    StorageResult — Dataclass returned by all save operations
    StorageError  — Exception raised on validation or write failures
"""

from app.storage.local_storage import LocalStorage, StorageError, StorageResult

__all__ = [
    "LocalStorage",
    "StorageError",
    "StorageResult",
]

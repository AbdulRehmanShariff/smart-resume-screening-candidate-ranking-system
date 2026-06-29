"""
storage/local_storage.py
-------------------------
Local filesystem storage adapter for resume and job description uploads.

Handles every step of secure file storage:
  1. Read uploaded bytes from Flask's FileStorage object
  2. Compute SHA-256 hash for duplicate detection
  3. Detect MIME type from file content (not extension)
  4. Validate MIME type and file size against configured limits
  5. Sanitize the original filename to prevent path traversal
  6. Generate a unique, collision-free storage path
  7. Write the file to disk, creating directories as needed
  8. Return a StorageResult with all metadata needed to create DB records

Storage path structure:
    {UPLOAD_FOLDER}/
      resumes/
        {year}/{month}/
          {user_id}/
            {uuid}_{safe_name}.{ext}
      jd_files/
        {year}/{month}/
          {recruiter_id}/
            {uuid}_{safe_name}.pdf

Only the relative path from UPLOAD_FOLDER is stored in the database.
The absolute path is reconstructed at runtime using get_absolute_path().
This allows the UPLOAD_FOLDER to move without requiring DB migration.

MIME detection:
  - Primary: python-magic (libmagic) — content-based, most reliable
  - Fallback: mimetypes (stdlib) — extension-based, used if magic unavailable
  Install python-magic: pip install python-magic
  Windows also requires the native libmagic DLL — see python-magic docs.

Security:
  - Filenames are sanitized to remove all non-alphanumeric/safe characters
  - UUID prefix ensures no two files can share a name
  - All paths are resolved and verified to be within UPLOAD_FOLDER
  - MIME type is validated from file bytes, not from the client-supplied
    Content-Type header or file extension
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MIME type detection — python-magic preferred, stdlib fallback
# ---------------------------------------------------------------------------

try:
    import magic as _magic  # python-magic package
    _MAGIC_AVAILABLE = True
    logger.debug("python-magic available — using content-based MIME detection")
except ImportError:
    _MAGIC_AVAILABLE = False
    import mimetypes as _mimetypes
    logger.warning(
        "python-magic not available — falling back to extension-based MIME detection. "
        "Install python-magic for secure content-based type detection: "
        "pip install python-magic"
    )


# ---------------------------------------------------------------------------
# Public Exceptions
# ---------------------------------------------------------------------------


class StorageError(Exception):
    """
    Raised when a file storage operation fails.

    Covers validation failures (wrong type, too large, duplicate) and
    I/O errors (write failure, permission denied, disk full).

    Attributes:
        message : Human-readable error description.
        code    : Machine-readable error code for API response mapping.
    """

    INVALID_TYPE = "INVALID_FILE_TYPE"
    TOO_LARGE = "FILE_TOO_LARGE"
    DUPLICATE = "DUPLICATE_FILE"
    WRITE_FAILED = "WRITE_FAILED"
    NOT_FOUND = "FILE_NOT_FOUND"

    def __init__(self, message: str, code: str = "STORAGE_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# ---------------------------------------------------------------------------
# StorageResult Dataclass
# ---------------------------------------------------------------------------


@dataclass
class StorageResult:
    """
    Metadata returned by LocalStorage after a successful file save.

    All string values are safe to store directly in the database.
    `file_path` is relative to UPLOAD_FOLDER (not absolute) so the
    database record survives the upload folder being relocated.

    Fields:
        file_path       : Relative path from UPLOAD_FOLDER.
                          Example: 'resumes/2026/06/{user_id}/{uuid}_cv.pdf'
        sha256_hash     : SHA-256 of the file contents, 64-char hex string.
        mime_type       : Detected MIME type from file content.
                          Example: 'application/pdf'
        file_type       : Normalised category. One of: pdf, docx, txt, image.
        file_size_bytes : Exact size in bytes.
        original_filename: The sanitized version of the uploaded filename.
    """

    file_path: str
    sha256_hash: str
    mime_type: str
    file_type: str
    file_size_bytes: int
    original_filename: str


# ---------------------------------------------------------------------------
# MIME type → file_type mapping
# ---------------------------------------------------------------------------

_MIME_TO_FILE_TYPE: dict = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "docx",
    "text/plain": "txt",
    "image/png": "image",
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/webp": "image",
    "image/tiff": "image",
}

# Default allowed MIME types for resume uploads
RESUME_ALLOWED_MIME_TYPES: frozenset = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain",
    "image/png",
    "image/jpeg",
    "image/webp",
})

# Default allowed MIME types for job description file uploads
JD_ALLOWED_MIME_TYPES: frozenset = frozenset({
    "application/pdf",
    "text/plain",
})


# ---------------------------------------------------------------------------
# LocalStorage Adapter
# ---------------------------------------------------------------------------


class LocalStorage:
    """
    Stores uploaded files on the local filesystem.

    Designed for development and self-hosted production deployments.
    In cloud deployments, this class can be replaced with an S3 or GCS
    adapter that exposes the same public interface:
      save_resume(), save_jd_file(), delete_file(), get_absolute_path()

    Usage (in service layer):
        storage = LocalStorage.from_app(current_app)
        result = storage.save_resume(flask_file, user_id=current_user.id)
        # result.file_path, result.sha256_hash, etc.

    Configuration keys read from Flask app config:
        UPLOAD_FOLDER         — Base directory for all uploads. Required.
        MAX_RESUME_SIZE_MB    — Max resume file size in MB. Default: 10.
        MAX_JD_SIZE_MB        — Max JD file size in MB. Default: 5.
    """

    def __init__(
        self,
        upload_folder: str,
        max_resume_size_mb: int = 10,
        max_jd_size_mb: int = 5,
    ) -> None:
        """
        Initialise the storage adapter.

        Args:
            upload_folder       : Absolute path to the upload root directory.
            max_resume_size_mb  : Maximum resume file size in MB.
            max_jd_size_mb      : Maximum job description file size in MB.
        """
        self.upload_folder = Path(upload_folder).resolve()
        self.max_resume_size_bytes = max_resume_size_mb * 1024 * 1024
        self.max_jd_size_bytes = max_jd_size_mb * 1024 * 1024

        # Ensure the root upload directory exists
        self.upload_folder.mkdir(parents=True, exist_ok=True)
        logger.debug("LocalStorage initialised at: %s", self.upload_folder)

    @classmethod
    def from_app(cls, app) -> "LocalStorage":
        """
        Create a LocalStorage instance from a Flask application's config.

        Args:
            app: The Flask application instance.

        Returns:
            Configured LocalStorage instance.

        Raises:
            ValueError: If UPLOAD_FOLDER is not set in the app config.
        """
        upload_folder = app.config.get("UPLOAD_FOLDER")
        if not upload_folder:
            raise ValueError(
                "UPLOAD_FOLDER must be set in Flask app config before "
                "using LocalStorage."
            )
        return cls(
            upload_folder=upload_folder,
            max_resume_size_mb=app.config.get("MAX_RESUME_SIZE_MB", 10),
            max_jd_size_mb=app.config.get("MAX_JD_SIZE_MB", 5),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_resume(
        self,
        file,
        user_id: uuid.UUID,
        allowed_mime_types: Optional[Set[str]] = None,
    ) -> StorageResult:
        """
        Save an uploaded resume file to local storage.

        Validates the file type and size, computes the SHA-256 hash,
        sanitizes the filename, generates a unique storage path, and
        writes the file to disk.

        Args:
            file              : Flask FileStorage object (from request.files).
                                Must have a `.filename` attribute and `.read()` method.
            user_id           : UUID of the uploading candidate.
                                Used to organise files into user-specific directories.
            allowed_mime_types: Set of permitted MIME types. Defaults to
                                RESUME_ALLOWED_MIME_TYPES if not provided.

        Returns:
            StorageResult: File metadata ready to be stored in the DB.

        Raises:
            StorageError: If the file type is not allowed, the file is too
                          large, or the write fails.
        """
        allowed = allowed_mime_types or RESUME_ALLOWED_MIME_TYPES
        return self._save_file(
            file=file,
            owner_id=user_id,
            subfolder="resumes",
            allowed_mime_types=allowed,
            max_size_bytes=self.max_resume_size_bytes,
        )

    def save_jd_file(
        self,
        file,
        recruiter_id: uuid.UUID,
        allowed_mime_types: Optional[Set[str]] = None,
    ) -> StorageResult:
        """
        Save an uploaded job description file to local storage.

        Same pipeline as save_resume() but with JD-specific limits and
        allowed MIME types (PDF and TXT only by default).

        Args:
            file              : Flask FileStorage object.
            recruiter_id      : UUID of the uploading recruiter.
            allowed_mime_types: Set of permitted MIME types. Defaults to
                                JD_ALLOWED_MIME_TYPES if not provided.

        Returns:
            StorageResult: File metadata ready to be stored in the DB.

        Raises:
            StorageError: If validation or write fails.
        """
        allowed = allowed_mime_types or JD_ALLOWED_MIME_TYPES
        return self._save_file(
            file=file,
            owner_id=recruiter_id,
            subfolder="jd_files",
            allowed_mime_types=allowed,
            max_size_bytes=self.max_jd_size_bytes,
        )

    def delete_file(self, relative_path: str) -> bool:
        """
        Delete a stored file by its relative path.

        Args:
            relative_path: The relative path stored in the database.
                           Example: 'resumes/2026/06/{user_id}/{uuid}_cv.pdf'

        Returns:
            True  — file was found and deleted.
            False — file did not exist (already deleted or path incorrect).

        Raises:
            StorageError: If the path is invalid or outside UPLOAD_FOLDER.
        """
        absolute_path = self._resolve_safe_path(relative_path)

        if not absolute_path.exists():
            logger.warning("delete_file: path not found: %s", absolute_path)
            return False

        try:
            absolute_path.unlink()
            logger.info("Deleted file: %s", absolute_path)
            return True
        except OSError as exc:
            logger.error("Failed to delete %s: %s", absolute_path, exc)
            raise StorageError(
                f"Failed to delete file '{relative_path}': {exc}",
                code=StorageError.WRITE_FAILED,
            ) from exc

    def get_absolute_path(self, relative_path: str) -> str:
        """
        Resolve a relative storage path to its absolute filesystem path.

        Args:
            relative_path: The relative path stored in the database.

        Returns:
            Absolute path string.

        Raises:
            StorageError: If the path resolves outside UPLOAD_FOLDER
                          (path traversal prevention).
        """
        return str(self._resolve_safe_path(relative_path))

    def file_exists(self, relative_path: str) -> bool:
        """
        Check whether a stored file exists at the given relative path.

        Args:
            relative_path: The relative path stored in the database.

        Returns:
            True if the file exists, False otherwise.
        """
        try:
            absolute_path = self._resolve_safe_path(relative_path)
            return absolute_path.is_file()
        except StorageError:
            return False

    # ------------------------------------------------------------------
    # Internal Pipeline
    # ------------------------------------------------------------------

    def _save_file(
        self,
        file,
        owner_id: uuid.UUID,
        subfolder: str,
        allowed_mime_types: Set[str],
        max_size_bytes: int,
    ) -> StorageResult:
        """
        Core file save pipeline. Called by save_resume() and save_jd_file().

        Steps:
            1. Read file bytes
            2. Compute SHA-256
            3. Detect MIME type
            4. Validate type and size
            5. Sanitize filename
            6. Build unique storage path
            7. Write to disk

        Args:
            file              : Flask FileStorage object.
            owner_id          : UUID of the file's owner (user or recruiter).
            subfolder         : Top-level subdirectory ('resumes' or 'jd_files').
            allowed_mime_types: Set of permitted MIME types.
            max_size_bytes    : Maximum allowed file size in bytes.

        Returns:
            StorageResult with all file metadata.
        """
        original_filename = getattr(file, "filename", "") or "unknown"

        # Step 1: Read all bytes into memory for hashing and MIME detection.
        # For very large files in production, consider streaming + tmp file.
        try:
            file_bytes = file.read()
        except Exception as exc:
            raise StorageError(
                f"Failed to read uploaded file: {exc}",
                code=StorageError.WRITE_FAILED,
            ) from exc

        # Step 2: Compute SHA-256 hash for duplicate detection.
        sha256_hash = self._compute_sha256(file_bytes)
        file_size_bytes = len(file_bytes)

        # Step 3: Detect MIME type from file content (not extension).
        mime_type = self._detect_mime_type(file_bytes, original_filename)

        # Step 4: Validate.
        self._validate_file(
            file_size_bytes=file_size_bytes,
            mime_type=mime_type,
            max_size_bytes=max_size_bytes,
            allowed_mime_types=allowed_mime_types,
        )

        # Step 5: Determine file type category and extension.
        file_type = self._normalize_file_type(mime_type)
        extension = self._extension_for_mime(mime_type, original_filename)

        # Step 6: Build a collision-free, sanitized storage path.
        safe_name = self._sanitize_filename(
            Path(original_filename).stem
        )
        unique_id = str(uuid.uuid4()).replace("-", "")[:12]
        stored_filename = f"{unique_id}_{safe_name}{extension}"

        now = datetime.now(timezone.utc)
        relative_path = (
            f"{subfolder}/"
            f"{now.year:04d}/{now.month:02d}/"
            f"{owner_id}/"
            f"{stored_filename}"
        )
        absolute_path = self.upload_folder / relative_path

        # Step 7: Write to disk.
        try:
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_path.write_bytes(file_bytes)
            logger.info(
                "Saved %s (%d bytes, %s) → %s",
                original_filename,
                file_size_bytes,
                mime_type,
                relative_path,
            )
        except OSError as exc:
            logger.error("File write failed for %s: %s", relative_path, exc)
            raise StorageError(
                f"Failed to write file to disk: {exc}",
                code=StorageError.WRITE_FAILED,
            ) from exc

        return StorageResult(
            file_path=relative_path,
            sha256_hash=sha256_hash,
            mime_type=mime_type,
            file_type=file_type,
            file_size_bytes=file_size_bytes,
            original_filename=Path(original_filename).name,
        )

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_sha256(file_bytes: bytes) -> str:
        """
        Compute the SHA-256 hash of file bytes.

        Returns a 64-character lowercase hex string.
        This is the same format as: hashlib.sha256(data).hexdigest()

        Args:
            file_bytes: Raw file content as bytes.

        Returns:
            64-character hex string of the SHA-256 digest.
        """
        return hashlib.sha256(file_bytes).hexdigest()

    @staticmethod
    def _detect_mime_type(file_bytes: bytes, filename: str) -> str:
        """
        Detect the MIME type of a file from its binary content.

        Uses python-magic (libmagic) if available for content-based detection.
        Falls back to Python's mimetypes module (extension-based) if not.

        Content-based detection is strongly preferred for security — a
        malicious actor can rename a PHP file to resume.pdf, but they
        cannot fake the binary magic bytes that libmagic reads.

        Args:
            file_bytes: The raw file bytes (at least the first 1KB is sufficient).
            filename  : Original filename — used only for the stdlib fallback.

        Returns:
            MIME type string. Example: 'application/pdf'.
            Falls back to 'application/octet-stream' if detection fails.
        """
        if _MAGIC_AVAILABLE:
            try:
                mime = _magic.from_buffer(file_bytes[:4096], mime=True)
                return mime or "application/octet-stream"
            except Exception as exc:
                logger.warning("python-magic detection failed: %s", exc)

        # Stdlib fallback — extension-based only
        mime, _ = _mimetypes.guess_type(filename)
        return mime or "application/octet-stream"

    @staticmethod
    def _validate_file(
        file_size_bytes: int,
        mime_type: str,
        max_size_bytes: int,
        allowed_mime_types: Set[str],
    ) -> None:
        """
        Validate file type and size. Raises StorageError on any violation.

        Args:
            file_size_bytes  : Size of the uploaded file in bytes.
            mime_type        : Detected MIME type of the file.
            max_size_bytes   : Maximum allowed size in bytes.
            allowed_mime_types: Set of acceptable MIME type strings.

        Raises:
            StorageError(INVALID_TYPE): If MIME type is not in allowed set.
            StorageError(TOO_LARGE)   : If file exceeds the size limit.
        """
        if mime_type not in allowed_mime_types:
            readable_allowed = ", ".join(sorted(allowed_mime_types))
            raise StorageError(
                f"File type '{mime_type}' is not permitted. "
                f"Allowed types: {readable_allowed}",
                code=StorageError.INVALID_TYPE,
            )

        if file_size_bytes > max_size_bytes:
            max_mb = max_size_bytes / (1024 * 1024)
            actual_mb = file_size_bytes / (1024 * 1024)
            raise StorageError(
                f"File size {actual_mb:.2f} MB exceeds the "
                f"{max_mb:.0f} MB limit.",
                code=StorageError.TOO_LARGE,
            )

    @staticmethod
    def _normalize_file_type(mime_type: str) -> str:
        """
        Map a MIME type to a normalised file_type category string.

        Returns one of: 'pdf', 'docx', 'txt', 'image'.
        Falls back to 'other' for any unrecognised MIME type.

        Args:
            mime_type: Detected MIME type string.

        Returns:
            Normalised file type category string.
        """
        return _MIME_TO_FILE_TYPE.get(mime_type, "other")

    @staticmethod
    def _extension_for_mime(mime_type: str, original_filename: str) -> str:
        """
        Determine the file extension to use for the stored filename.

        Prefers the extension from the original filename when it is consistent
        with the detected MIME type. Falls back to a canonical extension
        derived from the MIME type.

        Args:
            mime_type        : Detected MIME type string.
            original_filename: Original filename as uploaded.

        Returns:
            Extension string including the leading dot. Example: '.pdf'.
        """
        _MIME_TO_EXTENSION: dict = {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/msword": ".doc",
            "text/plain": ".txt",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/tiff": ".tiff",
        }
        canonical_ext = _MIME_TO_EXTENSION.get(mime_type)
        if canonical_ext:
            return canonical_ext

        # Use original extension if canonical is not known
        original_ext = Path(original_filename).suffix.lower()
        return original_ext if original_ext else ""

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """
        Remove unsafe characters from a filename stem.

        Steps:
            1. Unicode normalize (NFKD) to decompose accented characters.
            2. ASCII encode/decode to strip non-ASCII.
            3. Replace whitespace and path separators with underscores.
            4. Remove any remaining non-alphanumeric/safe characters.
            5. Collapse consecutive underscores.
            6. Truncate to 40 characters to keep paths manageable.
            7. Fallback to 'file' if nothing remains.

        Args:
            name: The filename stem (without extension).

        Returns:
            A safe, filesystem-compatible filename stem.
        """
        # Normalize and strip non-ASCII
        normalized = unicodedata.normalize("NFKD", name)
        ascii_name = normalized.encode("ascii", "ignore").decode("ascii")

        # Replace separators with underscores
        cleaned = re.sub(r"[\s/\\:*?\"<>|]+", "_", ascii_name)

        # Remove non-alphanumeric (keep underscores, hyphens, dots)
        cleaned = re.sub(r"[^\w\-]", "", cleaned)

        # Collapse consecutive underscores
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")

        # Truncate and ensure non-empty
        cleaned = cleaned[:40] or "file"

        return cleaned.lower()

    def _resolve_safe_path(self, relative_path: str) -> Path:
        """
        Resolve a relative path to an absolute path within UPLOAD_FOLDER.

        Performs a path traversal check: verifies that the resolved path
        is inside UPLOAD_FOLDER before returning it.

        Args:
            relative_path: A relative path as stored in the database.

        Returns:
            Resolved absolute Path object.

        Raises:
            StorageError: If the resolved path is outside UPLOAD_FOLDER
                          (attempted path traversal).
        """
        absolute = (self.upload_folder / relative_path).resolve()

        # Path traversal prevention: the resolved path must be inside upload_folder.
        try:
            absolute.relative_to(self.upload_folder)
        except ValueError:
            raise StorageError(
                f"Invalid path '{relative_path}' — outside UPLOAD_FOLDER.",
                code=StorageError.NOT_FOUND,
            )

        return absolute

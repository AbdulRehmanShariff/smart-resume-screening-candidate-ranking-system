"""
make_resume_sha256_index_partial_where_deleted_at_is_null

Revision ID: 9ed979bec81a
Revises: ba24561c4834
Create Date: 2026-07-14

Bug fix: convert uix_resumes_user_sha256 from an unconditional unique index
to a partial unique index scoped to active (non-deleted) resumes only.

Root cause of the bug
---------------------
The original index was:
    UNIQUE (user_id, sha256_hash)               -- covers ALL rows

This caused a PostgreSQL IntegrityError when a candidate attempted to
re-upload a file they had previously soft-deleted. The duplicate detection
query in upload_resume() correctly filtered by deleted_at IS NULL, but the
database constraint did not match that semantics, so the INSERT was rejected
even though no active duplicate existed.

Fix
---
The new index is:
    UNIQUE (user_id, sha256_hash) WHERE deleted_at IS NULL

This precisely expresses the business rule: a candidate cannot have two
*active* resumes with identical file content. Soft-deleted records retain
their sha256_hash permanently — historical and audit integrity is fully
preserved.

This mirrors the design of uix_resumes_one_primary_per_active_user, which
uses the same partial-index pattern on the same table.

Downgrade safety
----------------
The downgrade recreates the original unconditional index. Note that if any
soft-deleted rows share a sha256_hash with another row for the same user_id,
the downgrade will fail. In practice this only occurs if a file was deleted
and re-uploaded — exactly the scenario this migration fixes. The database
must be cleaned manually before downgrading in that case.
"""

from alembic import op


# revision identifiers, used by Alembic
revision = "9ed979bec81a"
down_revision = "ba24561c4834"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the old unconditional unique index
    op.drop_index("uix_resumes_user_sha256", table_name="resumes")

    # Recreate as a partial unique index — uniqueness enforced only on active rows
    op.execute(
        """
        CREATE UNIQUE INDEX uix_resumes_user_sha256
        ON resumes (user_id, sha256_hash)
        WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    # Drop the partial index
    op.drop_index("uix_resumes_user_sha256", table_name="resumes")

    # Restore the original unconditional unique index
    op.create_index(
        "uix_resumes_user_sha256",
        "resumes",
        ["user_id", "sha256_hash"],
        unique=True,
    )

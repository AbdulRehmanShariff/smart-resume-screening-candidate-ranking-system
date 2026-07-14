"""
make_primary_resume_constraint_deferrable — SUPERSEDED

Revision ID: 2bc3b7f03247
Revises: 9ed979bec81a
Create Date: 2026-07-14

This migration is intentionally a no-op.

Background
----------
An attempt was made to convert uix_resumes_one_primary_per_active_user
into a DEFERRABLE INITIALLY DEFERRED constraint to fix a flush-order
IntegrityError that occurred when swapping the primary resume flag.

The approach failed because PostgreSQL prohibits attaching a DEFERRABLE
qualifier via ALTER TABLE ... ADD CONSTRAINT ... UNIQUE USING INDEX
on a PARTIAL index (a partial index has a WHERE clause). This is a hard
engine limitation, not a configuration problem.

Actual fix
----------
The flush-order issue is fixed at the service layer in resume_service.py
by inserting an explicit db.session.flush() between the demote UPDATE and
the promote UPDATE in upload_resume(), set_primary(), and delete_resume().
This guarantees PostgreSQL always evaluates the immediate constraint in a
state where at most one row satisfies the predicate.

No schema change is required. This migration advances the revision chain
and serves as a permanent record of the investigation.
"""

from alembic import op  # noqa: F401 — imported for Alembic compatibility


revision = "2bc3b7f03247"
down_revision = "9ed979bec81a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Intentional no-op — fix applied at service layer.
    pass


def downgrade() -> None:
    # Intentional no-op — nothing to undo.
    pass

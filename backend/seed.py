"""
seed.py
-------
Database seed script for the Smart Resume Screening System.

Seeds the `roles` table with the three required system roles:
  - candidate : Job seekers who upload resumes and apply to jobs.
  - recruiter : HR professionals who post jobs and review candidates.
  - admin     : Platform administrators with full management access.

This script is idempotent — safe to run multiple times. It uses
INSERT ... ON CONFLICT DO NOTHING so re-running never duplicates rows.

Usage:
    python seed.py

Must be run once after `flask db upgrade` to populate lookup tables
before any application data can be created.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.extensions import db
from app.models.role import Role

ROLE_DEFINITIONS: list[dict] = [
    {
        "name": Role.CANDIDATE,
        "description": (
            "Job seekers who create profiles, upload resumes, and apply "
            "to job postings. Can receive AI-generated feedback on their "
            "resumes and track their application status."
        ),
    },
    {
        "name": Role.RECRUITER,
        "description": (
            "HR professionals and hiring managers who post job openings, "
            "review AI-ranked candidate lists, and manage the full "
            "application pipeline from screening to offer."
        ),
    },
    {
        "name": Role.ADMIN,
        "description": (
            "Platform administrators with full management access. "
            "Can manage users, configure system settings, view audit logs, "
            "and oversee all platform activity."
        ),
    },
]


def seed_roles(app) -> None:
    """
    Seed the roles table with the three system roles.

    Idempotent: skips any role that already exists (matched by name).
    """
    with app.app_context():
        seeded = 0
        skipped = 0

        for role_def in ROLE_DEFINITIONS:
            from sqlalchemy import select

            existing = db.session.execute(
                select(Role).where(Role.name == role_def["name"])
            ).scalar_one_or_none()

            if existing:
                print(f"  [SKIP]   Role '{role_def['name']}' already exists.")
                skipped += 1
                continue

            role = Role(
                name=role_def["name"],
                description=role_def["description"],
            )
            db.session.add(role)
            print(f"  [SEED]   Role '{role_def['name']}' created.")
            seeded += 1

        db.session.commit()
        print(f"\n  Done: {seeded} seeded, {skipped} already existed.")


if __name__ == "__main__":
    print("=" * 55)
    print("  Smart Resume Screening System — Database Seed")
    print("=" * 55)
    print("\nSeeding roles...\n")

    application = create_app(os.environ.get("FLASK_ENV", "development"))
    seed_roles(application)

    print("\nSeed complete.")

"""
scratch/selfcheck_batch3a.py
-----------------------------
Lightweight self-check for Batch 3A — app/schemas/job.py

Tests:
  - All four schemas importable and instantiable
  - CreateJobSchema: valid full payload
  - CreateJobSchema: valid minimal payload (required fields only)
  - CreateJobSchema: rejects missing required fields
  - CreateJobSchema: rejects blank title
  - CreateJobSchema: rejects invalid job_type
  - CreateJobSchema: rejects invalid experience_level
  - CreateJobSchema: rejects salary_max < salary_min
  - CreateJobSchema: accepts salary_max == salary_min (edge case)
  - CreateJobSchema: rejects past application_deadline
  - CreateJobSchema: deduplicates skill lists (post_load)
  - CreateJobSchema: normalises job_type to lowercase
  - CreateJobSchema: normalises skills (strips whitespace, drops blanks)
  - UpdateJobSchema: valid partial payload (one field)
  - UpdateJobSchema: rejects empty payload (no fields)
  - UpdateJobSchema: salary cross-field validation works
  - UpdateJobSchema: deadline future check works
  - JobFilterSchema: valid defaults (empty dict)
  - JobFilterSchema: valid full filter
  - JobFilterSchema: rejects invalid sort_by
  - JobFilterSchema: rejects per_page > 100
  - JobFilterSchema: rejects salary_max < salary_min in filter
  - JobFilterSchema: normalises status to lowercase
  - JobStatusTransitionSchema: empty body accepted
  - JobStatusTransitionSchema: note stripped
  - JobStatusTransitionSchema: rejects note > 500 chars
  - No circular imports
  - App boots cleanly with new schema file present
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

PASS = "[PASS]"
FAIL = "[FAIL]"
results = []

def check(label, fn):
    try:
        fn()
        results.append((True, label))
    except Exception as exc:
        results.append((False, f"{label}\n         => {type(exc).__name__}: {exc}"))

# ---------------------------------------------------------------------------
# 0. Imports
# ---------------------------------------------------------------------------
def _imports():
    from app.schemas.job import (
        CreateJobSchema,
        UpdateJobSchema,
        JobFilterSchema,
        JobStatusTransitionSchema,
    )
    CreateJobSchema()
    UpdateJobSchema()
    JobFilterSchema()
    JobStatusTransitionSchema()

check("All four schemas importable and instantiable", _imports)

# Convenience: import once for all subsequent checks
from app.schemas.job import (
    CreateJobSchema,
    UpdateJobSchema,
    JobFilterSchema,
    JobStatusTransitionSchema,
)
from marshmallow import ValidationError as ME

# ---------------------------------------------------------------------------
# 1. CreateJobSchema — Valid payloads
# ---------------------------------------------------------------------------
def _create_valid_full():
    s = CreateJobSchema()
    d = s.load({
        "title": "  Senior Python Developer  ",
        "description": "We need a Python expert with strong API skills.",
        "job_type": "FULL_TIME",   # uppercase — should normalise
        "requirements": "5+ years Python",
        "responsibilities": "Build APIs, mentor juniors",
        "location": "  New York, NY  ",
        "is_remote": True,
        "experience_level": "Senior",  # mixed case — should normalise
        "salary_min": 80000,
        "salary_max": 120000,
        "salary_currency": "usd",   # lowercase — should normalise
        "skills_required": ["  Python  ", "Flask", "PostgreSQL"],
        "nice_to_have_skills": ["Docker", "Kubernetes"],
    })
    assert d["title"] == "Senior Python Developer", f"title not stripped: {d['title']}"
    assert d["job_type"] == "full_time", f"job_type not normalised: {d['job_type']}"
    assert d["experience_level"] == "senior", f"exp not normalised: {d['experience_level']}"
    assert d["salary_currency"] == "USD", f"currency not normalised: {d['salary_currency']}"
    assert "Python" in d["skills_required"], "Python not in skills_required"
    assert "Python" == d["skills_required"][0], f"Skill not stripped: {d['skills_required'][0]}"

check("CreateJobSchema: valid full payload loads and normalises", _create_valid_full)

def _create_valid_minimal():
    s = CreateJobSchema()
    d = s.load({
        "title": "Junior Dev",
        "description": "Entry level role.",
        "job_type": "contract",
    })
    assert d["title"] == "Junior Dev"
    assert d["is_remote"] is False
    assert d["salary_min"] is None
    assert d["skills_required"] == []
    assert d["nice_to_have_skills"] == []
    assert d["salary_currency"] == "USD"

check("CreateJobSchema: valid minimal payload uses correct defaults", _create_valid_minimal)

# ---------------------------------------------------------------------------
# 2. CreateJobSchema — Rejection cases
# ---------------------------------------------------------------------------
def _create_missing_title():
    s = CreateJobSchema()
    try:
        s.load({"description": "Desc", "job_type": "full_time"})
        raise AssertionError("Should require title")
    except ME as e:
        assert "title" in e.messages

check("CreateJobSchema: rejects missing title", _create_missing_title)

def _create_missing_description():
    s = CreateJobSchema()
    try:
        s.load({"title": "Dev", "job_type": "full_time"})
        raise AssertionError("Should require description")
    except ME as e:
        assert "description" in e.messages

check("CreateJobSchema: rejects missing description", _create_missing_description)

def _create_missing_job_type():
    s = CreateJobSchema()
    try:
        s.load({"title": "Dev", "description": "Desc"})
        raise AssertionError("Should require job_type")
    except ME as e:
        assert "job_type" in e.messages

check("CreateJobSchema: rejects missing job_type", _create_missing_job_type)

def _create_blank_title():
    s = CreateJobSchema()
    try:
        s.load({"title": "   ", "description": "Desc", "job_type": "full_time"})
        raise AssertionError("Should reject blank title")
    except ME as e:
        assert "title" in e.messages

check("CreateJobSchema: rejects blank title (whitespace only)", _create_blank_title)

def _create_invalid_job_type():
    s = CreateJobSchema()
    try:
        s.load({"title": "Dev", "description": "Desc", "job_type": "gig_economy"})
        raise AssertionError("Should reject invalid job_type")
    except ME as e:
        assert "job_type" in e.messages

check("CreateJobSchema: rejects invalid job_type", _create_invalid_job_type)

def _create_invalid_exp_level():
    s = CreateJobSchema()
    try:
        s.load({"title": "Dev", "description": "Desc",
                "job_type": "full_time", "experience_level": "wizard"})
        raise AssertionError("Should reject invalid experience_level")
    except ME as e:
        assert "experience_level" in e.messages

check("CreateJobSchema: rejects invalid experience_level", _create_invalid_exp_level)

def _create_salary_max_lt_min():
    s = CreateJobSchema()
    try:
        s.load({"title": "Dev", "description": "Desc", "job_type": "full_time",
                "salary_min": 100000, "salary_max": 50000})
        raise AssertionError("Should reject salary_max < salary_min")
    except ME as e:
        assert "salary_max" in e.messages

check("CreateJobSchema: rejects salary_max < salary_min", _create_salary_max_lt_min)

def _create_salary_equal():
    # salary_max == salary_min is valid (exact salary)
    s = CreateJobSchema()
    d = s.load({"title": "Dev", "description": "Desc", "job_type": "full_time",
                "salary_min": 80000, "salary_max": 80000})
    assert d["salary_min"] == 80000
    assert d["salary_max"] == 80000

check("CreateJobSchema: accepts salary_max == salary_min", _create_salary_equal)

def _create_past_deadline():
    s = CreateJobSchema()
    try:
        s.load({
            "title": "Dev", "description": "Desc", "job_type": "full_time",
            "application_deadline": "2020-01-01T00:00:00",
        })
        raise AssertionError("Should reject past deadline")
    except ME as e:
        assert "application_deadline" in e.messages

check("CreateJobSchema: rejects past application_deadline", _create_past_deadline)

def _create_future_deadline():
    s = CreateJobSchema()
    d = s.load({
        "title": "Dev", "description": "Desc", "job_type": "full_time",
        "application_deadline": "2099-12-31T23:59:59",
    })
    assert d["application_deadline"] is not None

check("CreateJobSchema: accepts future application_deadline", _create_future_deadline)

def _create_skill_deduplication():
    s = CreateJobSchema()
    d = s.load({
        "title": "Dev", "description": "Desc", "job_type": "full_time",
        "skills_required": ["Python", "python", "PYTHON", "Flask", "flask"],
    })
    lower_skills = [sk.lower() for sk in d["skills_required"]]
    assert lower_skills.count("python") == 1, f"Duplicate Python not removed: {d['skills_required']}"
    assert lower_skills.count("flask") == 1, f"Duplicate Flask not removed: {d['skills_required']}"

check("CreateJobSchema: deduplicates skill lists (case-insensitive)", _create_skill_deduplication)

def _create_skills_strip_blanks():
    s = CreateJobSchema()
    d = s.load({
        "title": "Dev", "description": "Desc", "job_type": "full_time",
        "skills_required": ["  Python  ", "", "Flask", "  "],
    })
    # Blank strings stripped in pre_load, leaving ["Python", "Flask"]
    assert "" not in d["skills_required"]
    assert "Python" in d["skills_required"]

check("CreateJobSchema: strips blank skills from list", _create_skills_strip_blanks)

# ---------------------------------------------------------------------------
# 3. UpdateJobSchema
# ---------------------------------------------------------------------------
def _update_valid_partial():
    s = UpdateJobSchema()
    d = s.load({"title": "  Lead Engineer  "})
    assert d["title"] == "Lead Engineer"

check("UpdateJobSchema: valid single-field partial update", _update_valid_partial)

def _update_empty_rejects():
    s = UpdateJobSchema()
    try:
        s.load({})
        raise AssertionError("Should reject empty payload")
    except ME:
        pass  # expected

check("UpdateJobSchema: rejects empty payload (no fields)", _update_empty_rejects)

def _update_salary_cross_field():
    s = UpdateJobSchema()
    try:
        s.load({"salary_min": 100, "salary_max": 50})
        raise AssertionError("Should reject salary_max < salary_min")
    except ME as e:
        assert "salary_max" in e.messages

check("UpdateJobSchema: salary cross-field check works", _update_salary_cross_field)

def _update_past_deadline():
    s = UpdateJobSchema()
    try:
        s.load({"application_deadline": "2019-06-15T00:00:00"})
        raise AssertionError("Should reject past deadline")
    except ME as e:
        assert "application_deadline" in e.messages

check("UpdateJobSchema: rejects past application_deadline", _update_past_deadline)

def _update_null_deadline_valid():
    # Explicitly setting deadline to None means "remove the deadline" — valid
    s = UpdateJobSchema()
    # None alone triggers "at least one field" — send another field
    d = s.load({"title": "Updated Title", "application_deadline": None})
    assert d["application_deadline"] is None

check("UpdateJobSchema: null deadline valid (means remove deadline)", _update_null_deadline_valid)

# ---------------------------------------------------------------------------
# 4. JobFilterSchema
# ---------------------------------------------------------------------------
def _filter_defaults():
    s = JobFilterSchema()
    d = s.load({})
    assert d["status"] == "published"
    assert d["sort_by"] == "newest"
    assert d["page"] == 1
    assert d["skills"] == []

check("JobFilterSchema: empty dict loads with correct defaults", _filter_defaults)

def _filter_valid_full():
    s = JobFilterSchema()
    d = s.load({
        "status": "PUBLISHED",  # uppercase — normalised
        "job_type": "full_time",
        "experience_level": "senior",
        "is_remote": True,
        "location": "London",
        "skills": ["Python", "Docker"],
        "salary_min": 60000,
        "salary_max": 100000,
        "salary_currency": "GBP",
        "search": "backend engineer",
        "sort_by": "salary_desc",
        "page": 2,
        "per_page": 50,
    })
    assert d["status"] == "published"
    assert d["sort_by"] == "salary_desc"
    assert d["page"] == 2
    assert d["per_page"] == 50

check("JobFilterSchema: valid full filter payload loads", _filter_valid_full)

def _filter_invalid_sort():
    s = JobFilterSchema()
    try:
        s.load({"sort_by": "random"})
        raise AssertionError("Should reject invalid sort_by")
    except ME as e:
        assert "sort_by" in e.messages

check("JobFilterSchema: rejects invalid sort_by", _filter_invalid_sort)

def _filter_per_page_too_large():
    s = JobFilterSchema()
    try:
        s.load({"per_page": 999})
        raise AssertionError("Should reject per_page > 100")
    except ME as e:
        assert "per_page" in e.messages

check("JobFilterSchema: rejects per_page > 100", _filter_per_page_too_large)

def _filter_salary_range_cross():
    s = JobFilterSchema()
    try:
        s.load({"salary_min": 100000, "salary_max": 50000})
        raise AssertionError("Should reject salary_max < salary_min")
    except ME as e:
        assert "salary_max" in e.messages

check("JobFilterSchema: rejects salary_max < salary_min in filter", _filter_salary_range_cross)

def _filter_status_normalise():
    s = JobFilterSchema()
    d = s.load({"status": "DRAFT"})
    assert d["status"] == "draft"

check("JobFilterSchema: normalises status to lowercase", _filter_status_normalise)

def _filter_invalid_status():
    s = JobFilterSchema()
    try:
        s.load({"status": "open"})
        raise AssertionError("Should reject invalid status")
    except ME as e:
        assert "status" in e.messages

check("JobFilterSchema: rejects invalid status value", _filter_invalid_status)

# ---------------------------------------------------------------------------
# 5. JobStatusTransitionSchema
# ---------------------------------------------------------------------------
def _transition_empty_body():
    s = JobStatusTransitionSchema()
    d = s.load({})
    assert d["note"] is None

check("JobStatusTransitionSchema: empty body accepted, note=None", _transition_empty_body)

def _transition_with_note():
    s = JobStatusTransitionSchema()
    d = s.load({"note": "  Enough applicants. Closing early.  "})
    assert d["note"] == "Enough applicants. Closing early."

check("JobStatusTransitionSchema: note stripped of whitespace", _transition_with_note)

def _transition_note_too_long():
    s = JobStatusTransitionSchema()
    try:
        s.load({"note": "x" * 501})
        raise AssertionError("Should reject note > 500 chars")
    except ME as e:
        assert "note" in e.messages

check("JobStatusTransitionSchema: rejects note > 500 chars", _transition_note_too_long)

def _transition_blank_note_becomes_none():
    s = JobStatusTransitionSchema()
    d = s.load({"note": "   "})
    assert d["note"] is None

check("JobStatusTransitionSchema: blank note becomes None", _transition_blank_note_becomes_none)

def _transition_extra_fields_ignored():
    s = JobStatusTransitionSchema()
    # Extra fields should be silently excluded (EXCLUDE)
    d = s.load({"note": "Closing.", "target_status": "published"})
    assert "target_status" not in d

check("JobStatusTransitionSchema: extra fields silently excluded", _transition_extra_fields_ignored)

# ---------------------------------------------------------------------------
# 6. No circular imports + app boots
# ---------------------------------------------------------------------------
def _app_boots():
    from app import create_app
    app = create_app("development")
    with app.app_context():
        pass

check("App boots cleanly with job.py schema present", _app_boots)

def _prior_schemas_intact():
    from app.schemas.auth import (
        CandidateRegistrationSchema, RecruiterRegistrationSchema,
        LoginSchema, ForgotPasswordSchema, ResetPasswordSchema,
        ResendVerificationSchema, ChangePasswordSchema, ChangeEmailSchema,
        UpdateCandidateProfileSchema, UpdateRecruiterProfileSchema,
        AccountDeactivationSchema, TokenIntrospectSchema,
        UpdateSecuritySettingsSchema,
    )

check("All prior auth schemas still importable", _prior_schemas_intact)

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
print("\n" + "=" * 66)
passed = sum(1 for ok, _ in results if ok)
total  = len(results)
for ok, label in results:
    print(f"  {PASS if ok else FAIL} {label}")
print("=" * 66)
print(f"  RESULT: {passed}/{total} checks passed  "
      f"[{'ALL PASSED' if passed == total else 'FAILURES DETECTED'}]")
print("=" * 66)
sys.exit(0 if passed == total else 1)

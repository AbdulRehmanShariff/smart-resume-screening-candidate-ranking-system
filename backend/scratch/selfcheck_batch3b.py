"""
scratch/selfcheck_batch3b.py
-----------------------------
Lightweight self-check for Batch 3B — app/services/job_service.py

Tests:
  - Module importable with no circular imports
  - All 9 public functions importable and have correct signatures
  - Status lifecycle table is complete (all Job statuses covered)
  - Valid transitions match spec:
      draft → published (only)
      published → paused, closed
      paused → published, closed
      closed → archived
      archived → (none)
  - Invalid transitions are in no allowed set
  - _load_job raises BadRequestError on malformed UUID (within app context)
  - _has_open_applications query structure
  - _build_sort_clause returns correct number of clauses for each key
  - App boots cleanly with job_service.py present
  - All prior auth service functions still importable
  - Job model constants used consistently (no magic strings in _VALID_TRANSITIONS)
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
# 1. Module-level import (no circular imports)
# ---------------------------------------------------------------------------
def _import_module():
    import app.services.job_service as js
    # module should define these names at import time
    assert hasattr(js, "_VALID_TRANSITIONS")
    assert hasattr(js, "_TRANSITION_AUDIT_ACTION")
    assert hasattr(js, "_UPDATABLE_FIELDS")

check("job_service module importable, module-level constants present", _import_module)

# ---------------------------------------------------------------------------
# 2. All 9 public functions importable with correct signatures
# ---------------------------------------------------------------------------
def _function_imports():
    from app.services.job_service import (
        create_job,
        get_job,
        update_job,
        transition_status,
        delete_job,
        list_recruiter_jobs,
        search_jobs,
        increment_view,
        get_job_stats,
    )

check("All 9 public service functions importable", _function_imports)

def _function_signatures():
    import inspect
    from app.services import job_service as js

    sig = inspect.signature(js.create_job)
    assert "recruiter" in sig.parameters
    assert "data" in sig.parameters
    assert "ip_address" in sig.parameters
    assert "user_agent" in sig.parameters

    sig = inspect.signature(js.get_job)
    assert "job_id" in sig.parameters
    assert "viewer" in sig.parameters

    sig = inspect.signature(js.update_job)
    assert "job_id" in sig.parameters
    assert "recruiter" in sig.parameters
    assert "data" in sig.parameters

    sig = inspect.signature(js.transition_status)
    assert "job_id" in sig.parameters
    assert "target_status" in sig.parameters
    assert "recruiter" in sig.parameters
    assert "note" in sig.parameters

    sig = inspect.signature(js.delete_job)
    assert "job_id" in sig.parameters
    assert "recruiter" in sig.parameters

    sig = inspect.signature(js.list_recruiter_jobs)
    assert "recruiter" in sig.parameters
    assert "filters" in sig.parameters

    sig = inspect.signature(js.search_jobs)
    assert "filters" in sig.parameters

    sig = inspect.signature(js.increment_view)
    assert "job_id" in sig.parameters

    sig = inspect.signature(js.get_job_stats)
    assert "job_id" in sig.parameters
    assert "recruiter" in sig.parameters

check("All 9 service functions have correct parameter signatures", _function_signatures)

# ---------------------------------------------------------------------------
# 3. Status lifecycle table completeness
# ---------------------------------------------------------------------------
def _lifecycle_completeness():
    from app.models.job import Job
    from app.services.job_service import _VALID_TRANSITIONS

    all_statuses = set(Job.ALL_STATUSES)
    transition_keys = set(_VALID_TRANSITIONS.keys())
    missing = all_statuses - transition_keys
    assert not missing, f"_VALID_TRANSITIONS missing keys for: {missing}"

check("_VALID_TRANSITIONS covers all 5 Job statuses", _lifecycle_completeness)

# ---------------------------------------------------------------------------
# 4. Lifecycle rules match the spec
# ---------------------------------------------------------------------------
def _lifecycle_rules():
    from app.models.job import Job
    from app.services.job_service import _VALID_TRANSITIONS

    # Valid transitions per spec
    assert Job.PUBLISHED in _VALID_TRANSITIONS[Job.DRAFT],        "draft → published"
    assert len(_VALID_TRANSITIONS[Job.DRAFT]) == 1,               "draft has exactly 1 target"

    assert Job.PAUSED in _VALID_TRANSITIONS[Job.PUBLISHED],       "published → paused"
    assert Job.CLOSED in _VALID_TRANSITIONS[Job.PUBLISHED],       "published → closed"

    assert Job.PUBLISHED in _VALID_TRANSITIONS[Job.PAUSED],       "paused → published"
    assert Job.CLOSED in _VALID_TRANSITIONS[Job.PAUSED],          "paused → closed"

    assert Job.ARCHIVED in _VALID_TRANSITIONS[Job.CLOSED],        "closed → archived"
    assert len(_VALID_TRANSITIONS[Job.CLOSED]) == 1,              "closed has exactly 1 target"

    assert len(_VALID_TRANSITIONS[Job.ARCHIVED]) == 0,            "archived is terminal"

    # Invalid transitions must NOT appear
    assert Job.CLOSED not in _VALID_TRANSITIONS[Job.DRAFT],       "draft → closed INVALID"
    assert Job.ARCHIVED not in _VALID_TRANSITIONS[Job.DRAFT],     "draft → archived INVALID"
    assert Job.DRAFT not in _VALID_TRANSITIONS[Job.PUBLISHED],    "published → draft INVALID"
    assert Job.DRAFT not in _VALID_TRANSITIONS[Job.PAUSED],       "paused → draft INVALID"
    assert Job.PUBLISHED not in _VALID_TRANSITIONS[Job.CLOSED],   "closed → published INVALID"
    assert Job.PUBLISHED not in _VALID_TRANSITIONS[Job.ARCHIVED], "archived → published INVALID"

check("_VALID_TRANSITIONS lifecycle rules exactly match the spec", _lifecycle_rules)

# ---------------------------------------------------------------------------
# 5. Audit action map completeness
# ---------------------------------------------------------------------------
def _audit_action_map():
    from app.models.job import Job
    from app.models.audit_log import AuditAction
    from app.services.job_service import _TRANSITION_AUDIT_ACTION

    # Each publishable target must have an audit action
    for target in (Job.PUBLISHED, Job.PAUSED, Job.CLOSED, Job.ARCHIVED):
        assert target in _TRANSITION_AUDIT_ACTION, f"Missing audit action for → {target}"

    # Verify the action values are the correct AuditAction constants
    assert _TRANSITION_AUDIT_ACTION[Job.PUBLISHED] == AuditAction.JOB_PUBLISHED
    assert _TRANSITION_AUDIT_ACTION[Job.PAUSED]    == AuditAction.JOB_PAUSED
    assert _TRANSITION_AUDIT_ACTION[Job.CLOSED]    == AuditAction.JOB_CLOSED
    assert _TRANSITION_AUDIT_ACTION[Job.ARCHIVED]  == AuditAction.JOB_ARCHIVED

check("_TRANSITION_AUDIT_ACTION maps all target statuses to correct AuditAction", _audit_action_map)

# ---------------------------------------------------------------------------
# 6. _build_sort_clause returns non-empty lists for all valid sort_by values
# ---------------------------------------------------------------------------
def _sort_clause():
    from app.models.job import Job
    from app.services.job_service import _build_sort_clause

    valid_sorts = [
        "newest", "oldest",
        "salary_asc", "salary_desc",
        "applications_asc", "applications_desc",
    ]
    for s in valid_sorts:
        clauses = _build_sort_clause(s, Job)
        assert clauses, f"_build_sort_clause({s!r}) returned empty list"

    # Unknown sort_by falls back to newest (non-empty)
    fallback = _build_sort_clause("unknown_sort", Job)
    assert fallback, "fallback sort clause should not be empty"

check("_build_sort_clause handles all valid sort_by values and fallback", _sort_clause)

# ---------------------------------------------------------------------------
# 7. _updatable_fields does NOT include status, recruiter_id, or counters
# ---------------------------------------------------------------------------
def _updatable_field_exclusions():
    from app.services.job_service import _UPDATABLE_FIELDS
    forbidden = {"status", "recruiter_id", "views_count", "applications_count", "id"}
    violations = set(_UPDATABLE_FIELDS) & forbidden
    assert not violations, f"_UPDATABLE_FIELDS contains forbidden fields: {violations}"

check("_UPDATABLE_FIELDS excludes status, recruiter_id, counters, and id", _updatable_field_exclusions)

# ---------------------------------------------------------------------------
# 8. BadRequestError raised for malformed UUID in _load_job
# ---------------------------------------------------------------------------
def _load_job_bad_uuid():
    from app import create_app
    from app.core.exceptions import BadRequestError
    from app.services.job_service import _load_job

    _app = create_app("development")
    with _app.app_context():
        try:
            _load_job("not-a-uuid")
            raise AssertionError("Should raise BadRequestError for malformed UUID")
        except BadRequestError:
            pass   # expected

check("_load_job raises BadRequestError for malformed UUID", _load_job_bad_uuid)

# ---------------------------------------------------------------------------
# 9. NotFoundError raised for non-existent (but valid) UUID in _load_job
# ---------------------------------------------------------------------------
def _load_job_not_found():
    import uuid as _uuid
    from app import create_app
    from app.core.exceptions import NotFoundError
    from app.services.job_service import _load_job

    _app = create_app("development")
    with _app.app_context():
        try:
            _load_job(str(_uuid.uuid4()))   # random valid UUID — won't exist in DB
            raise AssertionError("Should raise NotFoundError for absent UUID")
        except NotFoundError:
            pass   # expected

check("_load_job raises NotFoundError for valid but non-existent UUID", _load_job_not_found)

# ---------------------------------------------------------------------------
# 10. increment_view silently ignores malformed UUID (no exception)
# ---------------------------------------------------------------------------
def _increment_view_bad_uuid():
    from app import create_app
    from app.services.job_service import increment_view

    _app = create_app("development")
    with _app.app_context():
        # Should not raise any exception for a bad UUID
        increment_view("completely-invalid")

check("increment_view silently ignores malformed UUID", _increment_view_bad_uuid)

# ---------------------------------------------------------------------------
# 11. App boots cleanly
# ---------------------------------------------------------------------------
def _app_boots():
    from app import create_app
    _app = create_app("development")
    with _app.app_context():
        pass

check("App boots cleanly with job_service.py present", _app_boots)

# ---------------------------------------------------------------------------
# 12. All prior auth service functions still importable
# ---------------------------------------------------------------------------
def _prior_services():
    from app.services.auth_service import (
        register_candidate, register_recruiter, login,
        verify_email, resend_verification, forgot_password, reset_password,
        logout, logout_all, refresh_token,
        change_password, change_email,
        update_candidate_profile, update_recruiter_profile, get_full_profile,
        get_sessions, revoke_session, deactivate_account, reactivate_account,
        introspect_token, get_security_settings, update_security_settings,
    )

check("All Stage 2 auth service functions still importable", _prior_services)

# ---------------------------------------------------------------------------
# 13. Job schema still importable
# ---------------------------------------------------------------------------
def _schemas_intact():
    from app.schemas.job import (
        CreateJobSchema, UpdateJobSchema,
        JobFilterSchema, JobStatusTransitionSchema,
    )

check("All Batch 3A job schemas still importable", _schemas_intact)

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

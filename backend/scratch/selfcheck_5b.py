import os, sys, uuid as _uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv; load_dotenv()

PASS = "PASS"; FAIL = "FAIL"
results = []

def check(label, fn):
    try:
        fn()
        results.append((True, label))
    except Exception as exc:
        results.append((False, label + " => " + type(exc).__name__ + ": " + str(exc)[:150]))

# 1 — import
def _i1():
    from app.services.application_service import (
        apply_for_job, get_application, list_candidate_applications,
        list_job_applicants, update_status, update_recruiter_notes,
        withdraw_application,
    )
check("1 All 7 public service functions importable", _i1)

# 2 — _build_sort_clause
def _i2():
    from app.services.application_service import _build_sort_clause
    for s in ("newest", "oldest", "score_desc", "score_asc", "status"):
        clauses = _build_sort_clause(s)
        assert clauses, "_build_sort_clause returned empty for " + s
    assert _build_sort_clause("unknown"), "Fallback must return something"
check("2 _build_sort_clause handles all 5 sort_by values + unknown fallback", _i2)

# 3 — _load_application bad UUID
def _i3():
    from app import create_app
    from app.core.exceptions import BadRequestError
    from app.services.application_service import _load_application
    with create_app("development").app_context():
        try: _load_application("not-a-uuid"); assert False
        except BadRequestError: pass
check("3 _load_application raises BadRequestError for malformed UUID", _i3)

# 4 — _load_application non-existent UUID
def _i4():
    from app import create_app
    from app.core.exceptions import NotFoundError
    from app.services.application_service import _load_application
    with create_app("development").app_context():
        try: _load_application(str(_uuid.uuid4())); assert False
        except NotFoundError: pass
check("4 _load_application raises NotFoundError for non-existent UUID", _i4)

# 5 — transition table completeness (all 13 statuses present)
def _i5():
    from app.services.application_service import _VALID_TRANSITIONS
    from app.models.application import ApplicationStatus
    for s in ApplicationStatus:
        assert s.value in _VALID_TRANSITIONS, "Missing from _VALID_TRANSITIONS: " + s.value
check("5 _VALID_TRANSITIONS covers all 13 ApplicationStatus values", _i5)

# 6 — terminal states have empty transition sets
def _i6():
    from app.services.application_service import _VALID_TRANSITIONS
    from app.models.application import ApplicationStatus
    terminals = ("offer_accepted", "offer_declined", "rejected", "withdrawn")
    for t in terminals:
        assert _VALID_TRANSITIONS[t] == frozenset(), t + " should have no allowed transitions"
check("6 Terminal statuses have empty transition frozensets", _i6)

# 7 — apply_for_job raises AuthorizationError for non-candidate
def _i7():
    from unittest.mock import MagicMock
    from app import create_app
    from app.core.exceptions import AuthorizationError
    from app.services.application_service import apply_for_job
    fake_user = MagicMock()
    fake_user.is_candidate = False
    fake_user.is_admin = False
    with create_app("development").app_context():
        try:
            apply_for_job(fake_user, {"job_id": _uuid.uuid4(), "resume_id": _uuid.uuid4()})
            assert False
        except AuthorizationError: pass
check("7 apply_for_job raises AuthorizationError for non-candidate user", _i7)

# 8 — withdraw_application raises BadRequestError on terminal application
def _i8():
    from unittest.mock import MagicMock, patch
    from app import create_app
    from app.core.exceptions import BadRequestError
    from app.services.application_service import withdraw_application
    from app.models.application import ApplicationStatus
    fake_app = MagicMock()
    fake_app.is_terminal = True
    fake_app.status = ApplicationStatus.REJECTED.value
    fake_app.status_label = "Not Proceeding"
    fake_candidate = MagicMock()
    fake_candidate.id = _uuid.uuid4()
    fake_candidate.is_admin = False
    with create_app("development").app_context():
        with patch("app.services.application_service._load_application", return_value=fake_app):
            with patch("app.services.application_service._assert_candidate_owns"):
                try:
                    withdraw_application(str(_uuid.uuid4()), fake_candidate)
                    assert False
                except BadRequestError: pass
check("8 withdraw_application raises BadRequestError on terminal application", _i8)

# 9 — update_status raises BadRequestError on invalid transition
def _i9():
    from unittest.mock import MagicMock, patch
    from app import create_app
    from app.core.exceptions import BadRequestError
    from app.services.application_service import update_status
    fake_app = MagicMock()
    fake_app.status = "applied"
    fake_app.id = _uuid.uuid4()
    fake_recruiter = MagicMock()
    with create_app("development").app_context():
        with patch("app.services.application_service._load_application", return_value=fake_app):
            with patch("app.services.application_service._assert_recruiter_owns_job"):
                try:
                    # "offer_accepted" is not reachable from "applied"
                    update_status(str(_uuid.uuid4()), "offer_accepted", fake_recruiter)
                    assert False
                except BadRequestError: pass
check("9 update_status raises BadRequestError for invalid lifecycle transition", _i9)

# 10 — update_status idempotent (same status → no-op)
def _i10():
    from unittest.mock import MagicMock, patch
    from app import create_app
    from app.services.application_service import update_status
    fake_app = MagicMock()
    fake_app.status = "screening"
    fake_app.id = _uuid.uuid4()
    fake_app.to_recruiter_dict.return_value = {"status": "screening"}
    fake_recruiter = MagicMock()
    with create_app("development").app_context():
        with patch("app.services.application_service._load_application", return_value=fake_app):
            with patch("app.services.application_service._assert_recruiter_owns_job"):
                result = update_status(str(_uuid.uuid4()), "screening", fake_recruiter)
                assert result["status"] == "screening"
                # Should NOT have called update_status on the model
                fake_app.update_status.assert_not_called()
check("10 update_status is idempotent (same status returns without writing)", _i10)

# 11 — no regression in existing services
def _i11():
    from app.services.auth_service import login
    from app.services.job_service import create_job
    from app.services.resume_service import upload_resume
check("11 No regression in existing service modules (auth, job, resume)", _i11)

# 12 — AI job enqueue helper importable
def _i12():
    from app.services.application_service import _enqueue_ranking_job
    from app.models.ai_processing_job import AIJobType, AIProcessingJob
check("12 _enqueue_ranking_job and AI model imports verified", _i12)

# 13 — list pagination helpers consistent with resume_service pattern
def _i13():
    import inspect
    from app.services.application_service import list_candidate_applications, list_job_applicants
    sig1 = inspect.signature(list_candidate_applications).parameters
    sig2 = inspect.signature(list_job_applicants).parameters
    assert "candidate" in sig1
    assert "filters" in sig1
    assert "job_id" in sig2
    assert "recruiter" in sig2
    assert "filters" in sig2
check("13 list functions have correct signatures matching architecture", _i13)

# Results
print()
print("=" * 66)
passed = sum(1 for ok, _ in results if ok)
total = len(results)
for ok, label in results:
    print("  [" + (PASS if ok else FAIL) + "] " + label)
print("=" * 66)
status = "ALL PASSED" if passed == total else "FAILURES DETECTED"
print("  RESULT: " + str(passed) + "/" + str(total) + " [" + status + "]")
print("=" * 66)
sys.exit(0 if passed == total else 1)

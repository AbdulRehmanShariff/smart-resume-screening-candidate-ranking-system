"""
scratch/selfcheck_batch2d.py
-----------------------------
Lightweight self-check for Batch 2D:
  - change_password service
  - change_email service
  - update_candidate_profile service
  - update_recruiter_profile service
  - get_full_profile service
  - New schemas: ChangePasswordSchema, ChangeEmailSchema,
    UpdateCandidateProfileSchema, UpdateRecruiterProfileSchema
  - New routes registered: /change-password, /change-email,
    /profile/candidate, /profile/recruiter
  - Existing routes still intact
  - No circular imports
  - App boots cleanly
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
        results.append((False, f"{label} => {type(exc).__name__}: {exc}"))

# ---------------------------------------------------------------------------
# 1. Schema imports
# ---------------------------------------------------------------------------
def _schema_imports():
    from app.schemas.auth import (
        ChangePasswordSchema,
        ChangeEmailSchema,
        UpdateCandidateProfileSchema,
        UpdateRecruiterProfileSchema,
    )

check("ChangePasswordSchema importable", _schema_imports)

def _schema_instantiate():
    from app.schemas.auth import (
        ChangePasswordSchema,
        ChangeEmailSchema,
        UpdateCandidateProfileSchema,
        UpdateRecruiterProfileSchema,
    )
    ChangePasswordSchema()
    ChangeEmailSchema()
    UpdateCandidateProfileSchema()
    UpdateRecruiterProfileSchema()

check("All Batch 2D schemas instantiate without error", _schema_instantiate)

# ---------------------------------------------------------------------------
# 2. Schema validation behaviour
# ---------------------------------------------------------------------------
def _change_password_valid():
    from app.schemas.auth import ChangePasswordSchema
    s = ChangePasswordSchema()
    data = s.load({"current_password": "OldPass1!", "new_password": "NewPass2@"})
    assert "current_password" in data
    assert "new_password" in data

check("ChangePasswordSchema: valid payload loads", _change_password_valid)

def _change_password_strength_fail():
    from app.schemas.auth import ChangePasswordSchema
    from marshmallow import ValidationError as ME
    s = ChangePasswordSchema()
    try:
        s.load({"current_password": "OldPass1!", "new_password": "allletters"})
        raise AssertionError("Should have failed — no digit in new_password")
    except ME:
        pass  # expected

check("ChangePasswordSchema: rejects new_password with no digit", _change_password_strength_fail)

def _change_email_valid():
    from app.schemas.auth import ChangeEmailSchema
    s = ChangeEmailSchema()
    data = s.load({"new_email": "  NEW@Example.COM  ", "current_password": "Pass1!"})
    assert data["new_email"] == "new@example.com", f"Expected lowercase, got {data['new_email']!r}"

check("ChangeEmailSchema: normalises email to lowercase", _change_email_valid)

def _update_candidate_valid():
    from app.schemas.auth import UpdateCandidateProfileSchema
    s = UpdateCandidateProfileSchema()
    data = s.load({"headline": "  Senior Dev  ", "years_of_experience": 5.5,
                   "availability": "immediately"})
    assert data["headline"] == "Senior Dev"
    assert data["years_of_experience"] == 5.5

check("UpdateCandidateProfileSchema: valid partial payload loads", _update_candidate_valid)

def _update_candidate_bad_availability():
    from app.schemas.auth import UpdateCandidateProfileSchema
    from marshmallow import ValidationError as ME
    s = UpdateCandidateProfileSchema()
    try:
        s.load({"availability": "tomorrow"})
        raise AssertionError("Should reject unknown availability")
    except ME:
        pass

check("UpdateCandidateProfileSchema: rejects unknown availability", _update_candidate_bad_availability)

def _update_recruiter_valid():
    from app.schemas.auth import UpdateRecruiterProfileSchema
    s = UpdateRecruiterProfileSchema()
    data = s.load({"company_name": "  Acme Corp  ", "company_size": "startup"})
    assert data["company_name"] == "Acme Corp"

check("UpdateRecruiterProfileSchema: valid partial payload loads", _update_recruiter_valid)

def _update_recruiter_bad_size():
    from app.schemas.auth import UpdateRecruiterProfileSchema
    from marshmallow import ValidationError as ME
    s = UpdateRecruiterProfileSchema()
    try:
        s.load({"company_size": "giant"})
        raise AssertionError("Should reject unknown company_size")
    except ME:
        pass

check("UpdateRecruiterProfileSchema: rejects unknown company_size", _update_recruiter_bad_size)

# ---------------------------------------------------------------------------
# 3. Service imports
# ---------------------------------------------------------------------------
def _service_imports():
    from app.services.auth_service import (
        change_password,
        change_email,
        update_candidate_profile,
        update_recruiter_profile,
        get_full_profile,
    )

check("Batch 2D service functions importable", _service_imports)

def _service_signatures():
    import inspect
    from app.services import auth_service

    sig = inspect.signature(auth_service.change_password)
    params = list(sig.parameters.keys())
    assert "user" in params
    assert "current_password" in params
    assert "new_password" in params
    assert "current_access_jti" in params

    sig2 = inspect.signature(auth_service.change_email)
    params2 = list(sig2.parameters.keys())
    assert "user" in params2
    assert "new_email" in params2
    assert "current_password" in params2

    sig3 = inspect.signature(auth_service.update_candidate_profile)
    assert "user" in sig3.parameters and "data" in sig3.parameters

    sig4 = inspect.signature(auth_service.update_recruiter_profile)
    assert "user" in sig4.parameters and "data" in sig4.parameters

    sig5 = inspect.signature(auth_service.get_full_profile)
    assert "user" in sig5.parameters

check("Batch 2D service function signatures correct", _service_signatures)

# ---------------------------------------------------------------------------
# 4. App boot + route registration
# ---------------------------------------------------------------------------
from app import create_app
app = create_app("development")

def _app_boots():
    with app.app_context():
        pass  # if we got here, app started cleanly

check("App boots cleanly after Batch 2D changes", _app_boots)

def _routes_registered():
    with app.app_context():
        rules = {r.rule for r in app.url_map.iter_rules()}
        expected = [
            "/api/v1/auth/change-password",
            "/api/v1/auth/change-email",
            "/api/v1/auth/profile/candidate",
            "/api/v1/auth/profile/recruiter",
        ]
        for route in expected:
            assert route in rules, f"Route missing: {route}"

check("All Batch 2D routes registered", _routes_registered)

def _existing_routes_intact():
    with app.app_context():
        rules = {r.rule for r in app.url_map.iter_rules()}
        for route in [
            "/api/v1/auth/register/candidate",
            "/api/v1/auth/register/recruiter",
            "/api/v1/auth/login",
            "/api/v1/auth/verify-email",
            "/api/v1/auth/resend-verification",
            "/api/v1/auth/forgot-password",
            "/api/v1/auth/reset-password",
            "/api/v1/auth/me",
            "/api/v1/auth/refresh",
            "/api/v1/auth/logout",
            "/api/v1/auth/logout-all",
        ]:
            assert route in rules, f"Existing route missing: {route}"

check("All existing routes still registered", _existing_routes_intact)

# ---------------------------------------------------------------------------
# 5. HTTP method checks (Flask test client)
# ---------------------------------------------------------------------------
client = app.test_client()

def _method_change_password():
    with app.app_context():
        r = client.post("/api/v1/auth/change-password",
                        json={"current_password": "x", "new_password": "y"},
                        headers={"Authorization": "Bearer invalid"})
        # 401 (invalid token) or 422 (validation) — NOT 404 or 405
        assert r.status_code not in (404, 405), f"Got {r.status_code}"

check("POST /api/v1/auth/change-password  (reachable, not 404/405)", _method_change_password)

def _method_change_email():
    with app.app_context():
        r = client.post("/api/v1/auth/change-email",
                        json={"new_email": "x@y.com", "current_password": "z"},
                        headers={"Authorization": "Bearer invalid"})
        assert r.status_code not in (404, 405), f"Got {r.status_code}"

check("POST /api/v1/auth/change-email     (reachable, not 404/405)", _method_change_email)

def _method_update_candidate():
    with app.app_context():
        r = client.patch("/api/v1/auth/profile/candidate",
                         json={"headline": "Dev"},
                         headers={"Authorization": "Bearer invalid"})
        assert r.status_code not in (404, 405), f"Got {r.status_code}"

check("PATCH /api/v1/auth/profile/candidate (reachable, not 404/405)", _method_update_candidate)

def _method_update_recruiter():
    with app.app_context():
        r = client.patch("/api/v1/auth/profile/recruiter",
                         json={"company_name": "Acme"},
                         headers={"Authorization": "Bearer invalid"})
        assert r.status_code not in (404, 405), f"Got {r.status_code}"

check("PATCH /api/v1/auth/profile/recruiter (reachable, not 404/405)", _method_update_recruiter)

def _method_me_get():
    with app.app_context():
        r = client.get("/api/v1/auth/me",
                       headers={"Authorization": "Bearer invalid"})
        assert r.status_code not in (404, 405), f"Got {r.status_code}"

check("GET  /api/v1/auth/me               (reachable, not 404/405)", _method_me_get)

# ---------------------------------------------------------------------------
# 6. AuditAction constants used by Batch 2D
# ---------------------------------------------------------------------------
def _audit_actions():
    from app.models.audit_log import AuditAction
    assert hasattr(AuditAction, "ACCOUNT_PASSWORD_CHANGED")
    assert hasattr(AuditAction, "ACCOUNT_PROFILE_UPDATED")

check("AuditAction.ACCOUNT_PASSWORD_CHANGED + ACCOUNT_PROFILE_UPDATED exist", _audit_actions)

# ---------------------------------------------------------------------------
# 7. Previously passing Batch 2A/2B/2C functions still importable
# ---------------------------------------------------------------------------
def _prior_batches():
    from app.services.auth_service import (
        register_candidate, register_recruiter, login,
        verify_email, resend_verification, forgot_password, reset_password,
        logout, logout_all, refresh_token,
    )

check("All prior-batch service functions still importable", _prior_batches)

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
print("\n" + "=" * 62)
passed = sum(1 for ok, _ in results if ok)
total  = len(results)
for ok, label in results:
    print(f"  {PASS if ok else FAIL} {label}")
print("=" * 62)
print(f"  RESULT: {passed}/{total} checks passed  "
      f"[{'ALL PASSED' if passed == total else 'FAILURES DETECTED'}]")
print("=" * 62)
sys.exit(0 if passed == total else 1)

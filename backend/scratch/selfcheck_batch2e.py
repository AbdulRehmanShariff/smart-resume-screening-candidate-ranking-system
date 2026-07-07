"""
scratch/selfcheck_batch2e.py
-----------------------------
Lightweight self-check for Batch 2E:
  - New schemas: AccountDeactivationSchema, TokenIntrospectSchema,
    UpdateSecuritySettingsSchema
  - New service functions: get_sessions, revoke_session, deactivate_account,
    reactivate_account, introspect_token, get_security_settings,
    update_security_settings
  - New routes: GET/DELETE /sessions, POST /deactivate, POST /reactivate/<id>,
    POST /introspect, GET+PATCH /security-settings
  - All prior routes still intact
  - App boots cleanly, no circular imports
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
# 1. Schema imports and instantiation
# ---------------------------------------------------------------------------
def _schema_imports():
    from app.schemas.auth import (
        AccountDeactivationSchema,
        TokenIntrospectSchema,
        UpdateSecuritySettingsSchema,
    )
    AccountDeactivationSchema()
    TokenIntrospectSchema()
    UpdateSecuritySettingsSchema()

check("Batch 2E schemas importable and instantiate", _schema_imports)

# ---------------------------------------------------------------------------
# 2. Schema validation behaviour
# ---------------------------------------------------------------------------
def _deactivation_valid():
    from app.schemas.auth import AccountDeactivationSchema
    s = AccountDeactivationSchema()
    d = s.load({"current_password": "Pass1!", "reason": "  No longer needed  "})
    assert d["reason"] == "No longer needed", f"Got {d['reason']!r}"

check("AccountDeactivationSchema: strips reason whitespace", _deactivation_valid)

def _deactivation_no_password():
    from app.schemas.auth import AccountDeactivationSchema
    from marshmallow import ValidationError as ME
    s = AccountDeactivationSchema()
    try:
        s.load({})
        raise AssertionError("Should require current_password")
    except ME:
        pass

check("AccountDeactivationSchema: rejects missing current_password", _deactivation_no_password)

def _introspect_valid():
    from app.schemas.auth import TokenIntrospectSchema
    s = TokenIntrospectSchema()
    d = s.load({"token": "  abc.def.ghi  "})
    assert d["token"] == "abc.def.ghi", f"Got {d['token']!r}"

check("TokenIntrospectSchema: strips token whitespace", _introspect_valid)

def _introspect_empty():
    from app.schemas.auth import TokenIntrospectSchema
    from marshmallow import ValidationError as ME
    s = TokenIntrospectSchema()
    try:
        s.load({"token": ""})
        raise AssertionError("Should reject empty token")
    except ME:
        pass

check("TokenIntrospectSchema: rejects empty token", _introspect_empty)

def _security_valid():
    from app.schemas.auth import UpdateSecuritySettingsSchema
    s = UpdateSecuritySettingsSchema()
    d = s.load({"login_notifications": True, "session_timeout_hours": 48})
    assert d["login_notifications"] is True
    assert d["session_timeout_hours"] == 48

check("UpdateSecuritySettingsSchema: valid payload loads", _security_valid)

def _security_bad_timeout():
    from app.schemas.auth import UpdateSecuritySettingsSchema
    from marshmallow import ValidationError as ME
    s = UpdateSecuritySettingsSchema()
    try:
        s.load({"session_timeout_hours": 999})
        raise AssertionError("Should reject 999 hours")
    except ME:
        pass

check("UpdateSecuritySettingsSchema: rejects timeout > 168", _security_bad_timeout)

# ---------------------------------------------------------------------------
# 3. Service function imports and signatures
# ---------------------------------------------------------------------------
def _service_imports():
    from app.services.auth_service import (
        get_sessions,
        revoke_session,
        deactivate_account,
        reactivate_account,
        introspect_token,
        get_security_settings,
        update_security_settings,
    )

check("Batch 2E service functions importable", _service_imports)

def _service_signatures():
    import inspect
    from app.services import auth_service

    sig = inspect.signature(auth_service.get_sessions)
    assert "user" in sig.parameters

    sig = inspect.signature(auth_service.revoke_session)
    assert "user" in sig.parameters and "jti" in sig.parameters

    sig = inspect.signature(auth_service.deactivate_account)
    assert "user" in sig.parameters and "current_password" in sig.parameters

    sig = inspect.signature(auth_service.reactivate_account)
    assert "target_user_id" in sig.parameters and "admin_user" in sig.parameters

    sig = inspect.signature(auth_service.introspect_token)
    assert "raw_token" in sig.parameters

    sig = inspect.signature(auth_service.get_security_settings)
    assert "user" in sig.parameters

    sig = inspect.signature(auth_service.update_security_settings)
    assert "user" in sig.parameters and "data" in sig.parameters

check("All Batch 2E service function signatures correct", _service_signatures)

# ---------------------------------------------------------------------------
# 4. introspect_token — invalid token returns {valid: false}, doesn't raise
# ---------------------------------------------------------------------------
def _introspect_invalid_token():
    from app.create_app import create_app as _ca
    _app = _ca("development")
    with _app.app_context():
        from app.services.auth_service import introspect_token
        result = introspect_token("not.a.valid.jwt")
        assert result["valid"] is False
        assert "error" in result

check("introspect_token: returns valid=false for garbage token (no raise)", _introspect_invalid_token)

# ---------------------------------------------------------------------------
# 5. App boot and route registration
# ---------------------------------------------------------------------------
from app import create_app
app = create_app("development")

def _app_boots():
    with app.app_context():
        pass

check("App boots cleanly after Batch 2E", _app_boots)

def _new_routes_registered():
    with app.app_context():
        rules = {r.rule for r in app.url_map.iter_rules()}
        expected = [
            "/api/v1/auth/sessions",
            "/api/v1/auth/sessions/<jti>",
            "/api/v1/auth/deactivate",
            "/api/v1/auth/reactivate/<user_id>",
            "/api/v1/auth/introspect",
            "/api/v1/auth/security-settings",
        ]
        for route in expected:
            assert route in rules, f"Missing: {route}"

check("All Batch 2E routes registered", _new_routes_registered)

def _prior_routes_intact():
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
            "/api/v1/auth/change-password",
            "/api/v1/auth/change-email",
            "/api/v1/auth/profile/candidate",
            "/api/v1/auth/profile/recruiter",
        ]:
            assert route in rules, f"Existing route missing: {route}"

check("All prior routes (2A–2D) still registered", _prior_routes_intact)

# ---------------------------------------------------------------------------
# 6. HTTP method checks
# ---------------------------------------------------------------------------
client = app.test_client()

def _check_reachable(method, route, body=None):
    with app.app_context():
        fn = getattr(client, method)
        kwargs = {"headers": {"Authorization": "Bearer invalid"}}
        if body is not None:
            kwargs["json"] = body
        r = fn(route, **kwargs)
        assert r.status_code not in (404, 405), \
            f"{method.upper()} {route} => {r.status_code}"

check("GET  /api/v1/auth/sessions              reachable",
      lambda: _check_reachable("get",    "/api/v1/auth/sessions"))
check("DELETE /api/v1/auth/sessions/test-jti  reachable",
      lambda: _check_reachable("delete", "/api/v1/auth/sessions/test-jti-123"))
check("POST /api/v1/auth/deactivate            reachable",
      lambda: _check_reachable("post",   "/api/v1/auth/deactivate",
                               {"current_password": "x"}))
check("POST /api/v1/auth/reactivate/<id>       reachable",
      lambda: _check_reachable("post",   "/api/v1/auth/reactivate/some-uuid"))
check("POST /api/v1/auth/introspect            reachable",
      lambda: _check_reachable("post",   "/api/v1/auth/introspect",
                               {"token": "x.y.z"}))
check("GET  /api/v1/auth/security-settings     reachable",
      lambda: _check_reachable("get",    "/api/v1/auth/security-settings"))
check("PATCH /api/v1/auth/security-settings   reachable",
      lambda: _check_reachable("patch",  "/api/v1/auth/security-settings",
                               {"login_notifications": True}))

# ---------------------------------------------------------------------------
# 7. Prior-batch service functions still importable
# ---------------------------------------------------------------------------
def _prior_services():
    from app.services.auth_service import (
        register_candidate, register_recruiter, login,
        verify_email, resend_verification, forgot_password, reset_password,
        logout, logout_all, refresh_token,
        change_password, change_email,
        update_candidate_profile, update_recruiter_profile, get_full_profile,
    )

check("All prior-batch service functions still importable", _prior_services)

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
print("\n" + "=" * 64)
passed = sum(1 for ok, _ in results if ok)
total  = len(results)
for ok, label in results:
    print(f"  {PASS if ok else FAIL} {label}")
print("=" * 64)
print(f"  RESULT: {passed}/{total} checks passed  "
      f"[{'ALL PASSED' if passed == total else 'FAILURES DETECTED'}]")
print("=" * 64)
sys.exit(0 if passed == total else 1)

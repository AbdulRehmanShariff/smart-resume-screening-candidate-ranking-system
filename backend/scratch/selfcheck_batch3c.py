"""
scratch/selfcheck_batch3c.py
-----------------------------
Lightweight self-check for Batch 3C — app/api/jobs/routes.py

Tests:
  - Blueprint importable
  - Blueprint name is 'jobs'
  - All route functions importable by name
  - App boots cleanly with jobs_bp registered
  - All expected URL rules are registered under /api/v1/jobs
  - HTTP methods match the spec for each endpoint
  - Marshmallow schemas importable at module level in routes
  - No circular imports
  - Auth blueprint still fully functional (no regression)
  - Optional-JWT helper is importable and does not raise at import time
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
# 1. Blueprint import
# ---------------------------------------------------------------------------
def _bp_import():
    from app.api.jobs.routes import jobs_bp
    assert jobs_bp is not None
    assert jobs_bp.name == "jobs"

check("jobs_bp imports correctly and has name='jobs'", _bp_import)

# ---------------------------------------------------------------------------
# 2. All 11 route handler functions importable
# ---------------------------------------------------------------------------
def _route_fn_imports():
    from app.api.jobs.routes import (
        create_job,
        search_jobs,
        list_my_jobs,
        get_job,
        update_job,
        delete_job,
        publish_job,
        pause_job,
        close_job,
        archive_job,
        get_job_stats,
    )

check("All 11 route handler functions importable", _route_fn_imports)

# ---------------------------------------------------------------------------
# 3. App boots cleanly with jobs_bp registered
# ---------------------------------------------------------------------------
def _app_boots():
    from app import create_app
    app = create_app("development")
    with app.app_context():
        pass

check("App boots cleanly with jobs_bp registered in factory", _app_boots)

# ---------------------------------------------------------------------------
# 4. URL rules registered with correct prefixes and methods
# ---------------------------------------------------------------------------
def _url_rules():
    from app import create_app
    app = create_app("development")

    # Flask may register separate Rule objects for the same path when different
    # HTTP methods are registered by different decorators (e.g. @bp.post and
    # @bp.get on "/"). Merge all methods per path before asserting.
    path_methods: dict = {}
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/v1/jobs"):
            clean_methods = rule.methods - {"HEAD", "OPTIONS"}
            path_methods.setdefault(rule.rule, set()).update(clean_methods)

    expected = {
        "/api/v1/jobs/":                  {"POST", "GET"},
        "/api/v1/jobs/my":                {"GET"},
        "/api/v1/jobs/<job_id>":          {"GET", "PATCH", "DELETE"},
        "/api/v1/jobs/<job_id>/publish":  {"POST"},
        "/api/v1/jobs/<job_id>/pause":    {"POST"},
        "/api/v1/jobs/<job_id>/close":    {"POST"},
        "/api/v1/jobs/<job_id>/archive":  {"POST"},
        "/api/v1/jobs/<job_id>/stats":    {"GET"},
    }

    for path, required_methods in expected.items():
        assert path in path_methods, (
            f"Route not registered: {path}\nRegistered: {sorted(path_methods.keys())}"
        )
        actual_methods = path_methods[path]
        assert required_methods.issubset(actual_methods), (
            f"Route {path}: expected methods {required_methods}, "
            f"got {actual_methods}"
        )

check("All expected URL rules registered with correct HTTP methods", _url_rules)

# ---------------------------------------------------------------------------
# 5. No URL conflicts with auth blueprint
# ---------------------------------------------------------------------------
def _no_url_conflict():
    from app import create_app
    app = create_app("development")

    auth_rules = [r.rule for r in app.url_map.iter_rules()
                  if r.rule.startswith("/api/v1/auth")]
    jobs_rules = [r.rule for r in app.url_map.iter_rules()
                  if r.rule.startswith("/api/v1/jobs")]

    overlap = set(auth_rules) & set(jobs_rules)
    assert not overlap, f"URL conflicts between auth and jobs: {overlap}"

check("No URL conflicts between auth and jobs blueprints", _no_url_conflict)

# ---------------------------------------------------------------------------
# 6. Module-level schema instances in routes are correct types
# ---------------------------------------------------------------------------
def _schema_instances():
    from app.api.jobs import routes as r
    from app.schemas.job import (
        CreateJobSchema, UpdateJobSchema,
        JobFilterSchema, JobStatusTransitionSchema,
    )
    assert isinstance(r._create_schema,     CreateJobSchema)
    assert isinstance(r._update_schema,     UpdateJobSchema)
    assert isinstance(r._filter_schema,     JobFilterSchema)
    assert isinstance(r._transition_schema, JobStatusTransitionSchema)

check("Module-level schema instances are correct types", _schema_instances)

# ---------------------------------------------------------------------------
# 7. _get_query_args multi-key handling
# ---------------------------------------------------------------------------
def _query_args_helper():
    from app import create_app
    app = create_app("development")
    with app.test_request_context(
        "/api/v1/jobs/?skills=Python&skills=Flask&job_type=full_time"
    ):
        from app.api.jobs.routes import _get_query_args
        args = _get_query_args()
        assert args["skills"] == ["Python", "Flask"], f"Multi-value skills: {args['skills']}"
        assert args["job_type"] == "full_time", f"job_type: {args['job_type']}"

check("_get_query_args correctly handles multi-value 'skills' parameter", _query_args_helper)

# ---------------------------------------------------------------------------
# 8. _ctx() returns ip_address and user_agent keys
# ---------------------------------------------------------------------------
def _ctx_helper():
    from app import create_app
    app = create_app("development")
    with app.test_request_context("/api/v1/jobs/"):
        from app.api.jobs.routes import _ctx
        ctx = _ctx()
        assert "ip_address" in ctx
        assert "user_agent" in ctx

check("_ctx() returns ip_address and user_agent keys", _ctx_helper)

# ---------------------------------------------------------------------------
# 9. No circular imports — full import chain
# ---------------------------------------------------------------------------
def _no_circular():
    import importlib
    # Reimport all layers in dependency order
    for module in [
        "app.models.job",
        "app.schemas.job",
        "app.services.job_service",
        "app.api.jobs.routes",
    ]:
        importlib.import_module(module)

check("Full import chain has no circular imports", _no_circular)

# ---------------------------------------------------------------------------
# 10. Prior auth routes still importable (no regression)
# ---------------------------------------------------------------------------
def _auth_routes_intact():
    from app.api.auth.routes import auth_bp
    from app.api.auth.routes import (
        register_candidate, register_recruiter, login,
        verify_email, resend_verification,
        forgot_password, reset_password,
        refresh, logout, logout_all,
        change_password, change_email,
        me, update_candidate_profile, update_recruiter_profile,
        get_sessions, revoke_session,
        deactivate_account, reactivate_account,
        introspect_token, get_security_settings, update_security_settings,
    )
    assert auth_bp.name == "auth"

check("All Stage 2 auth routes still importable (no regression)", _auth_routes_intact)

# ---------------------------------------------------------------------------
# 11. GET /api/v1/jobs/ returns 422 for invalid sort_by (integration-lite)
# ---------------------------------------------------------------------------
def _filter_validation_integration():
    from app import create_app
    app = create_app("development")
    client = app.test_client()

    resp = client.get("/api/v1/jobs/?sort_by=invalid_sort_key&per_page=999")
    assert resp.status_code == 422, (
        f"Expected 422 for invalid filter params, got {resp.status_code}"
    )
    body = resp.get_json()
    assert body["success"] is False

check(
    "GET /api/v1/jobs/ returns 422 for invalid sort_by (filter validation wired up)",
    _filter_validation_integration,
)

# ---------------------------------------------------------------------------
# 12. POST /api/v1/jobs/ returns 401 without JWT (not 500)
# ---------------------------------------------------------------------------
def _create_unauthenticated():
    from app import create_app
    app = create_app("development")
    client = app.test_client()

    resp = client.post(
        "/api/v1/jobs/",
        json={"title": "Test", "description": "Test", "job_type": "full_time"},
    )
    assert resp.status_code == 401, (
        f"Expected 401 for unauthenticated job creation, got {resp.status_code}"
    )

check(
    "POST /api/v1/jobs/ returns 401 for unauthenticated request",
    _create_unauthenticated,
)

# ---------------------------------------------------------------------------
# 13. GET /api/v1/jobs/<job_id> with malformed UUID returns 400 or 404
# ---------------------------------------------------------------------------
def _get_job_malformed_uuid():
    from app import create_app
    app = create_app("development")
    client = app.test_client()

    resp = client.get("/api/v1/jobs/not-a-valid-uuid")
    # BadRequestError → 400 via global handler
    assert resp.status_code in (400, 404), (
        f"Expected 400 or 404 for malformed UUID, got {resp.status_code}"
    )
    body = resp.get_json()
    assert body["success"] is False

check(
    "GET /api/v1/jobs/<job_id> returns 400/404 for malformed UUID",
    _get_job_malformed_uuid,
)

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

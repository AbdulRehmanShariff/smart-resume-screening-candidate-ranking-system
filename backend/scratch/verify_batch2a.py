"""
scratch/verify_batch2a.py
--------------------------
Batch 2A self-validation: schemas, decorators, service imports, and live
endpoint tests using Flask test client.

Run from backend/:
    python scratch/verify_batch2a.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

PASS = "[PASS]"
FAIL = "[FAIL]"
results = []

def check(label, ok, detail=""):
    results.append(ok)
    mark = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {mark} {label}{suffix}")

print("=" * 70)
print("  BATCH 2A SELF-VALIDATION -- Smart Resume Screening System")
print("=" * 70)

# -----------------------------------------------------------------------
# 1. Import checks
# -----------------------------------------------------------------------
print("\n[1] IMPORT CHECKS\n")

try:
    from app.schemas.auth import (
        CandidateRegistrationSchema,
        LoginSchema,
        RecruiterRegistrationSchema,
    )
    check("schemas/auth.py imports cleanly", True)
except Exception as e:
    check("schemas/auth.py imports cleanly", False, str(e))

try:
    from app.services.auth_service import login, register_candidate, register_recruiter
    check("services/auth_service.py imports cleanly", True)
except Exception as e:
    check("services/auth_service.py imports cleanly", False, str(e))

try:
    from app.core.decorators import (
        admin_required,
        candidate_required,
        get_current_user,
        jwt_required_user,
        recruiter_required,
    )
    check("core/decorators.py imports cleanly", True)
except Exception as e:
    check("core/decorators.py imports cleanly", False, str(e))

try:
    from app.api.auth.routes import auth_bp
    check("api/auth/routes.py imports cleanly", True)
except Exception as e:
    check("api/auth/routes.py imports cleanly", False, str(e))

# -----------------------------------------------------------------------
# 2. Schema validation tests
# -----------------------------------------------------------------------
print("\n[2] SCHEMA VALIDATION TESTS\n")

from marshmallow import ValidationError

cschema = CandidateRegistrationSchema()
rschema = RecruiterRegistrationSchema()
lschema = LoginSchema()

# Valid candidate
try:
    data = cschema.load({"email": "  Jane@EXAMPLE.com ", "password": "Secure1!", "first_name": "Jane", "last_name": "Doe"})
    check("Candidate schema: valid payload loads", data["email"] == "jane@example.com", f"email={data['email']}")
except Exception as e:
    check("Candidate schema: valid payload loads", False, str(e))

# Email normalised
try:
    data = cschema.load({"email": "  TEST@Test.COM  ", "password": "Secure1!", "first_name": "A", "last_name": "B"})
    check("Candidate schema: email lowercased+stripped", data["email"] == "test@test.com", data["email"])
except Exception as e:
    check("Candidate schema: email lowercased+stripped", False, str(e))

# Password load_only (not in dump)
try:
    data = cschema.load({"email": "a@b.com", "password": "Secure1!", "first_name": "A", "last_name": "B"})
    dumped = cschema.dump(data)
    check("Candidate schema: password is load_only (not in dump)", "password" not in dumped)
except Exception as e:
    check("Candidate schema: password is load_only (not in dump)", False, str(e))

# Weak password rejected
try:
    cschema.load({"email": "a@b.com", "password": "onlyletters", "first_name": "A", "last_name": "B"})
    check("Candidate schema: weak password rejected (no digit)", False, "no error raised")
except ValidationError:
    check("Candidate schema: weak password rejected (no digit)", True)

# Short password rejected
try:
    cschema.load({"email": "a@b.com", "password": "Ab1", "first_name": "A", "last_name": "B"})
    check("Candidate schema: short password rejected (<8 chars)", False, "no error raised")
except ValidationError:
    check("Candidate schema: short password rejected (<8 chars)", True)

# Extra field rejected (RAISE)
try:
    cschema.load({"email": "a@b.com", "password": "Secure1!", "first_name": "A", "last_name": "B", "evil_field": "x"})
    check("Candidate schema: extra fields rejected (Meta.unknown=RAISE)", False, "no error raised")
except ValidationError:
    check("Candidate schema: extra fields rejected (Meta.unknown=RAISE)", True)

# Missing required field
try:
    cschema.load({"password": "Secure1!", "first_name": "A", "last_name": "B"})
    check("Candidate schema: missing email field rejected", False, "no error raised")
except ValidationError:
    check("Candidate schema: missing email field rejected", True)

# Valid recruiter
try:
    data = rschema.load({"email": "hr@co.com", "password": "Secure1!", "first_name": "Alice", "last_name": "Smith", "company_name": "Acme Corp"})
    check("Recruiter schema: valid payload loads", data["company_name"] == "Acme Corp")
except Exception as e:
    check("Recruiter schema: valid payload loads", False, str(e))

# Recruiter missing company_name
try:
    rschema.load({"email": "hr@co.com", "password": "Secure1!", "first_name": "Alice", "last_name": "Smith"})
    check("Recruiter schema: missing company_name rejected", False, "no error raised")
except ValidationError:
    check("Recruiter schema: missing company_name rejected", True)

# Invalid company_size
try:
    rschema.load({"email": "hr@co.com", "password": "Secure1!", "first_name": "A", "last_name": "B", "company_name": "X", "company_size": "giant"})
    check("Recruiter schema: invalid company_size rejected", False, "no error raised")
except ValidationError:
    check("Recruiter schema: invalid company_size rejected", True)

# Login schema: email normalised
try:
    data = lschema.load({"email": "  ADMIN@SITE.COM  ", "password": "any"})
    check("Login schema: email normalised", data["email"] == "admin@site.com", data["email"])
except Exception as e:
    check("Login schema: email normalised", False, str(e))

# Login schema: extra fields excluded (EXCLUDE)
try:
    data = lschema.load({"email": "a@b.com", "password": "pw", "device": "mobile"})
    check("Login schema: extra fields excluded (EXCLUDE)", "device" not in data)
except Exception as e:
    check("Login schema: extra fields excluded", False, str(e))

# -----------------------------------------------------------------------
# 3. App startup and blueprint registration
# -----------------------------------------------------------------------
print("\n[3] APP STARTUP & BLUEPRINT REGISTRATION\n")

try:
    from app import create_app
    app = create_app("development")
    check("App boots without errors", True)
except Exception as e:
    check("App boots without errors", False, str(e))
    print("  Cannot continue without app. Aborting.")
    sys.exit(1)

blueprints = list(app.blueprints.keys())
check("'health' blueprint registered", "health" in blueprints, str(blueprints))
check("'auth' blueprint registered", "auth" in blueprints, str(blueprints))

rules = {r.rule: r for r in app.url_map.iter_rules()}
check("GET  /api/v1/health registered",                  "/api/v1/health" in rules)
check("POST /api/v1/auth/login registered",              "/api/v1/auth/login" in rules)
check("POST /api/v1/auth/register/candidate registered", "/api/v1/auth/register/candidate" in rules)
check("POST /api/v1/auth/register/recruiter registered", "/api/v1/auth/register/recruiter" in rules)

# Verify methods
if "/api/v1/auth/login" in rules:
    methods = rules["/api/v1/auth/login"].methods
    check("Login route: only POST allowed", "POST" in methods and "GET" not in methods)

# -----------------------------------------------------------------------
# 4. Live endpoint tests (test client)
# -----------------------------------------------------------------------
print("\n[4] LIVE ENDPOINT TESTS\n")

# Pre-test cleanup: remove any users left by a previous run of this script
# so that registration tests always exercise the 201 path.
with app.app_context():
    from sqlalchemy import delete as sa_delete
    from app.models.user import User as _User
    from app.extensions import db as _db
    _db.session.execute(
        sa_delete(_User).where(
            _User.email.in_(["testcandidate@example.com", "testrecruiter@example.com"])
        )
    )
    _db.session.commit()

with app.test_client() as client:

    # 4a. Candidate registration -- success
    r = client.post("/api/v1/auth/register/candidate", json={
        "email": "testcandidate@example.com",
        "password": "Secure123",
        "first_name": "Test",
        "last_name": "Candidate",
    })
    body = r.get_json()
    ok = r.status_code == 201 and body.get("success") is True
    check("POST /register/candidate -> 201", ok, f"status={r.status_code}")
    if ok:
        check("  - user.id present in response", "id" in body.get("data", {}))
        check("  - role is 'candidate'", body.get("data", {}).get("role") == "candidate")
        check("  - password NOT in response", "password" not in str(body.get("data", {})))

    # 4b. Duplicate email -- 409
    r = client.post("/api/v1/auth/register/candidate", json={
        "email": "testcandidate@example.com",
        "password": "Secure123",
        "first_name": "Test",
        "last_name": "Candidate",
    })
    check("POST /register/candidate duplicate email -> 409", r.status_code == 409, f"status={r.status_code}")

    # 4c. Validation error -- 422
    r = client.post("/api/v1/auth/register/candidate", json={
        "email": "not-an-email",
        "password": "x",
    })
    body = r.get_json()
    ok = r.status_code == 422 and body.get("success") is False
    check("POST /register/candidate invalid data -> 422", ok, f"status={r.status_code}")

    # 4d. Recruiter registration -- success
    r = client.post("/api/v1/auth/register/recruiter", json={
        "email": "testrecruiter@example.com",
        "password": "Secure123",
        "first_name": "Alice",
        "last_name": "Recruiter",
        "company_name": "Test Corp",
        "company_size": "startup",
        "industry": "Technology",
    })
    body = r.get_json()
    ok = r.status_code == 201 and body.get("success") is True
    check("POST /register/recruiter -> 201", ok, f"status={r.status_code}")
    if ok:
        check("  - role is 'recruiter'", body.get("data", {}).get("role") == "recruiter")

    # 4e. Login with candidate -- success
    r = client.post("/api/v1/auth/login", json={
        "email": "testcandidate@example.com",
        "password": "Secure123",
    })
    body = r.get_json()
    ok = r.status_code == 200 and body.get("success") is True
    check("POST /login (candidate) -> 200", ok, f"status={r.status_code}")
    if ok:
        data = body.get("data", {})
        check("  - access_token present", "access_token" in data)
        check("  - refresh_token present", "refresh_token" in data)
        check("  - token_type is Bearer", data.get("token_type") == "Bearer")
        check("  - user object present", "user" in data)

    # 4f. Wrong password -- 401
    r = client.post("/api/v1/auth/login", json={
        "email": "testcandidate@example.com",
        "password": "WrongPassword1",
    })
    check("POST /login wrong password -> 401", r.status_code == 401, f"status={r.status_code}")

    # 4g. Non-existent email -- 401
    r = client.post("/api/v1/auth/login", json={
        "email": "nobody@nowhere.com",
        "password": "Secure123",
    })
    check("POST /login nonexistent email -> 401", r.status_code == 401, f"status={r.status_code}")

    # 4h. Missing body -- 422
    r = client.post("/api/v1/auth/login", json={})
    check("POST /login empty body -> 422", r.status_code == 422, f"status={r.status_code}")

    # 4i. Wrong method on auth route -- 405
    r = client.get("/api/v1/auth/login")
    check("GET /auth/login (wrong method) -> 405", r.status_code == 405, f"status={r.status_code}")

# -----------------------------------------------------------------------
# 5. Decorator checks
# -----------------------------------------------------------------------
print("\n[5] DECORATOR CHECKS\n")

from app.core.decorators import (
    admin_required,
    candidate_required,
    get_current_user,
    jwt_required_user,
    recruiter_required,
)
check("All 5 decorator symbols importable", True)
check("jwt_required_user is callable", callable(jwt_required_user))
check("candidate_required is callable", callable(candidate_required))
check("recruiter_required is callable", callable(recruiter_required))
check("admin_required is callable", callable(admin_required))
check("get_current_user is callable", callable(get_current_user))

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
passed = sum(results)
total = len(results)
print("\n" + "=" * 70)
mark = "[ALL PASSED]" if passed == total else f"[{total - passed} FAILED]"
print(f"  RESULT: {passed}/{total} checks passed  {mark}")
print("=" * 70)

"""
scratch/selfcheck_batch2c.py
-----------------------------
Batch 2C self-check: imports, routes, service logic, blocklist integration.
Run from backend/:  python scratch/selfcheck_batch2c.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

results = []

def check(label, ok, detail=""):
    results.append(ok)
    mark = "[PASS]" if ok else "[FAIL]"
    suffix = f"  ({detail})" if detail else ""
    print(f"  {mark} {label}{suffix}")

print("=" * 62)
print("  BATCH 2C SELF-CHECK")
print("=" * 62)

# ------------------------------------------------------------
# 1. Import checks
# ------------------------------------------------------------
print("\n[1] IMPORT CHECKS\n")

try:
    from app.services.auth_service import logout, logout_all, refresh_token
    check("auth_service: logout / logout_all / refresh_token importable", True)
except Exception as e:
    check("auth_service: logout / logout_all / refresh_token importable", False, str(e))

try:
    from app.api.auth.routes import auth_bp
    check("routes.py imports cleanly (no circular imports)", True)
except Exception as e:
    check("routes.py imports cleanly (no circular imports)", False, str(e))

try:
    from app.models.token_blocklist import TokenBlocklist
    check("TokenBlocklist importable from auth_service context", True)
except Exception as e:
    check("TokenBlocklist importable from auth_service context", False, str(e))

try:
    from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request
    check("Flask-JWT-Extended helpers (get_jwt, verify_jwt_in_request) importable", True)
except Exception as e:
    check("Flask-JWT-Extended helpers importable", False, str(e))

# ------------------------------------------------------------
# 2. App startup + route registration
# ------------------------------------------------------------
print("\n[2] ROUTE REGISTRATION\n")

try:
    from app import create_app
    app = create_app("development")
    check("App boots cleanly after Batch 2C changes", True)
except Exception as e:
    check("App boots cleanly", False, str(e))
    sys.exit(1)

rules = {r.rule: r.methods for r in app.url_map.iter_rules()}
expected = [
    # Batch 2C (new)
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/logout-all"),
    # Batch 2A/2B regression
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/register/candidate"),
    ("POST", "/api/v1/auth/register/recruiter"),
    ("GET",  "/api/v1/auth/verify-email"),
    ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/reset-password"),
    ("GET",  "/api/v1/auth/me"),
]
for method, path in expected:
    present = path in rules and method in rules[path]
    check(f"{method} {path}", present)

# ------------------------------------------------------------
# 3. JWT blocklist callback still wired
# ------------------------------------------------------------
print("\n[3] JWT BLOCKLIST CALLBACK\n")
with app.app_context():
    try:
        from app.models.token_blocklist import TokenBlocklist
        result = TokenBlocklist.is_jti_blocklisted("non-existent-jti-xyz")
        check("TokenBlocklist.is_jti_blocklisted() executes without error", result == False, f"returned {result}")
    except Exception as e:
        check("TokenBlocklist.is_jti_blocklisted() executes without error", False, str(e))

# ------------------------------------------------------------
# 4. TokenBlocklist.revoke_token() contract
# ------------------------------------------------------------
print("\n[4] TOKEN BLOCKLIST LOGIC\n")

with app.app_context():
    # Invalid token_type raises ValueError
    try:
        from app.models.token_blocklist import TokenBlocklist
        import uuid
        TokenBlocklist.revoke_token(jti="x", token_type="invalid", user_id=uuid.uuid4())
        check("revoke_token: invalid token_type raises ValueError", False, "no exception raised")
    except ValueError:
        check("revoke_token: invalid token_type raises ValueError", True)
    except Exception as e:
        check("revoke_token: invalid token_type raises ValueError", False, str(e))

    # Valid constants exist
    check("TokenBlocklist.ACCESS constant = 'access'", TokenBlocklist.ACCESS == "access")
    check("TokenBlocklist.REFRESH constant = 'refresh'", TokenBlocklist.REFRESH == "refresh")

# ------------------------------------------------------------
# 5. Service function signatures (smoke test — no DB needed)
# ------------------------------------------------------------
print("\n[5] SERVICE SIGNATURE CHECK\n")
import inspect
from app.services.auth_service import logout, logout_all, refresh_token

# logout: access_jti, refresh_jti, user_id, *, ip_address, user_agent
sig = inspect.signature(logout)
params = list(sig.parameters.keys())
check("logout() has access_jti parameter",  "access_jti" in params)
check("logout() has refresh_jti parameter", "refresh_jti" in params)
check("logout() has user_id parameter",     "user_id" in params)
check("logout() has ip_address parameter",  "ip_address" in params)

# logout_all: current_access_jti, user_id, *, ip_address, user_agent
sig2 = inspect.signature(logout_all)
params2 = list(sig2.parameters.keys())
check("logout_all() has current_access_jti parameter", "current_access_jti" in params2)
check("logout_all() has user_id parameter",            "user_id" in params2)

# refresh_token: current_refresh_jti, user_id, *, rotate, ip_address, user_agent
sig3 = inspect.signature(refresh_token)
params3 = list(sig3.parameters.keys())
check("refresh_token() has current_refresh_jti parameter", "current_refresh_jti" in params3)
check("refresh_token() has rotate parameter",              "rotate" in params3)
check("refresh_token() rotate defaults to True",
      sig3.parameters["rotate"].default == True)

# ------------------------------------------------------------
# 6. Regression: Batch 2A/2B functions still importable
# ------------------------------------------------------------
print("\n[6] REGRESSION — BATCH 2A/2B FUNCTIONS\n")
try:
    from app.services.auth_service import (
        register_candidate, register_recruiter, login,
        verify_email, resend_verification, forgot_password, reset_password,
    )
    check("All Batch 2A/2B service functions still importable", True)
except Exception as e:
    check("All Batch 2A/2B service functions still importable", False, str(e))

# Summary
passed = sum(results)
total = len(results)
print("\n" + "=" * 62)
mark = "[ALL PASSED]" if passed == total else f"[{total - passed} FAILED]"
print(f"  RESULT: {passed}/{total} checks passed  {mark}")
print("=" * 62)

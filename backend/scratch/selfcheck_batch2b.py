"""
scratch/selfcheck_batch2b.py
-----------------------------
Batch 2B self-check: imports, route registration, schema validation, config.
Run from backend/:  python scratch/selfcheck_batch2b.py
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

print("=" * 60)
print("  BATCH 2B SELF-CHECK")
print("=" * 60)

# 1. Import checks
print("\n[1] IMPORT CHECKS\n")
try:
    from app.schemas.auth import (
        ForgotPasswordSchema,
        ResetPasswordSchema,
        ResendVerificationSchema,
    )
    check("New schemas (forgot/reset/resend) import cleanly", True)
except Exception as e:
    check("New schemas (forgot/reset/resend) import cleanly", False, str(e))

try:
    from app.services.email_service import (
        send_verification_email,
        send_password_reset_email,
        send_welcome_email,
    )
    check("email_service.py imports cleanly", True)
except Exception as e:
    check("email_service.py imports cleanly", False, str(e))

try:
    from app.services.auth_service import (
        verify_email,
        resend_verification,
        forgot_password,
        reset_password,
    )
    check("New auth_service functions importable", True)
except Exception as e:
    check("New auth_service functions importable", False, str(e))

try:
    from app.api.auth.routes import auth_bp
    check("Updated routes.py imports cleanly", True)
except Exception as e:
    check("Updated routes.py imports cleanly", False, str(e))

# 2. App startup and route registration
print("\n[2] ROUTE REGISTRATION\n")
try:
    from app import create_app
    app = create_app("development")
    check("App boots cleanly", True)
except Exception as e:
    check("App boots cleanly", False, str(e))
    print("  Cannot continue. Aborting.")
    sys.exit(1)

rules = {r.rule: r.methods for r in app.url_map.iter_rules()}
expected_routes = [
    ("GET",  "/api/v1/auth/verify-email"),
    ("POST", "/api/v1/auth/resend-verification"),
    ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/reset-password"),
    ("GET",  "/api/v1/auth/me"),
    # Batch 2A (regression)
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/register/candidate"),
    ("POST", "/api/v1/auth/register/recruiter"),
]
for method, path in expected_routes:
    present = path in rules and method in rules[path]
    check(f"{method} {path}", present)

# 3. Schema validation
print("\n[3] SCHEMA VALIDATION\n")
from marshmallow import ValidationError
from app.schemas.auth import (
    ForgotPasswordSchema,
    ResetPasswordSchema,
    ResendVerificationSchema,
)
fs = ForgotPasswordSchema()
rs = ResetPasswordSchema()
rv = ResendVerificationSchema()

# Forgot: email normalised
try:
    d = fs.load({"email": "  TEST@TEST.COM  "})
    check("ForgotPasswordSchema: email normalised to lowercase", d["email"] == "test@test.com", d["email"])
except Exception as e:
    check("ForgotPasswordSchema: email normalised to lowercase", False, str(e))

# Forgot: missing email rejected
try:
    fs.load({})
    check("ForgotPasswordSchema: missing email rejected", False, "no error")
except ValidationError:
    check("ForgotPasswordSchema: missing email rejected", True)

# Reset: valid load
try:
    d = rs.load({"token": "  abc123  ", "new_password": "Secure1!"})
    check("ResetPasswordSchema: valid payload loads", d["token"] == "abc123", d["token"])
except Exception as e:
    check("ResetPasswordSchema: valid payload loads", False, str(e))

# Reset: weak password
try:
    rs.load({"token": "abc", "new_password": "allletter"})
    check("ResetPasswordSchema: weak password rejected", False, "no error")
except ValidationError:
    check("ResetPasswordSchema: weak password rejected (no digit)", True)

# Reset: new_password load_only
try:
    d = rs.load({"token": "t", "new_password": "Secure1!"})
    dumped = rs.dump(d)
    check("ResetPasswordSchema: new_password is load_only", "new_password" not in dumped)
except Exception as e:
    check("ResetPasswordSchema: new_password is load_only", False, str(e))

# Resend: email normalised, extra excluded
try:
    d = rv.load({"email": "USER@DOMAIN.COM", "extra": "ignored"})
    check("ResendVerificationSchema: email normalised + extra excluded",
          d["email"] == "user@domain.com" and "extra" not in d,
          str(d))
except Exception as e:
    check("ResendVerificationSchema: email normalised + extra excluded", False, str(e))

# 4. Config check
print("\n[4] CONFIG CHECK\n")
with app.app_context():
    from flask import current_app
    url = current_app.config.get("FRONTEND_URL")
    check("FRONTEND_URL in config", bool(url), url or "missing")
    mail_server = current_app.config.get("MAIL_SERVER")
    check("MAIL_SERVER in config", bool(mail_server), mail_server or "missing")
    mail_suppress = current_app.config.get("MAIL_SUPPRESS_SEND", False)
    check("MAIL_SUPPRESS_SEND off in development (real sends enabled)", not mail_suppress)

# 5. Template rendering
print("\n[5] TEMPLATE RENDERING\n")
with app.test_request_context():
    from flask import render_template
    try:
        html = render_template(
            "email/verification.html",
            first_name="Alice",
            verification_url="http://localhost:3000/verify-email?token=abc",
        )
        check("verification.html renders without error", len(html) > 100)
        check("verification.html contains first_name", "Alice" in html)
        check("verification.html contains URL", "abc" in html)
    except Exception as e:
        check("verification.html renders without error", False, str(e))

    try:
        html = render_template(
            "email/password_reset.html",
            first_name="Bob",
            email="bob@example.com",
            reset_url="http://localhost:3000/reset-password?token=xyz",
            ip_address="127.0.0.1",
        )
        check("password_reset.html renders without error", len(html) > 100)
        check("password_reset.html contains reset URL", "xyz" in html)
        check("password_reset.html contains IP", "127.0.0.1" in html)
    except Exception as e:
        check("password_reset.html renders without error", False, str(e))

    try:
        html = render_template(
            "email/welcome.html",
            first_name="Carol",
            role="candidate",
            role_label="Candidate",
            dashboard_url="http://localhost:3000/dashboard",
        )
        check("welcome.html renders without error (candidate)", len(html) > 100)
        check("welcome.html contains first_name", "Carol" in html)
    except Exception as e:
        check("welcome.html renders without error", False, str(e))

# Summary
passed = sum(results)
total = len(results)
print("\n" + "=" * 60)
mark = "[ALL PASSED]" if passed == total else f"[{total - passed} FAILED]"
print(f"  RESULT: {passed}/{total} checks passed  {mark}")
print("=" * 60)

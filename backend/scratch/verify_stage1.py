"""
scratch/verify_stage1.py
------------------------
Stage 1 verification script.
Run from backend/ directory:
    python scratch/verify_stage1.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app import create_app

app = create_app("development")

print("=" * 65)
print("  STAGE 1 VERIFICATION — Smart Resume Screening System")
print("=" * 65)

# -------------------------------------------------------------------
# 1. Route table
# -------------------------------------------------------------------
print("\n[1] REGISTERED ROUTES\n")
rules = sorted(app.url_map.iter_rules(), key=lambda r: r.rule)
for rule in rules:
    methods = ", ".join(sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS")))
    print(f"  {methods:<10}  {rule.rule:<35}  -> {rule.endpoint}")

print(f"\n  Total routes: {len(rules)}")

# -------------------------------------------------------------------
# 2. Blueprint inventory
# -------------------------------------------------------------------
print("\n[2] REGISTERED BLUEPRINTS\n")
for name, bp in app.blueprints.items():
    print(f"  {name:<20}  {bp.__class__.__module__}")

# -------------------------------------------------------------------
# 3. Live endpoint tests
# -------------------------------------------------------------------
print("\n[3] ENDPOINT TESTS\n")

PASS = "PASS"
FAIL = "FAIL"

results = []

with app.test_client() as client:

    # GET / -> 404 (expected — no root route defined in Stage 1)
    r = client.get("/")
    body = r.get_json()
    expected_status = 404
    ok = r.status_code == expected_status and body.get("success") is False
    results.append(ok)
    status = PASS if ok else FAIL
    print(f"  [{status}] GET /                -> HTTP {r.status_code}  (expected 404, no root route)")

    # GET /api/v1/health -> 200
    r = client.get("/api/v1/health")
    body = r.get_json()
    ok = (
        r.status_code == 200
        and body.get("success") is True
        and body.get("data", {}).get("status") == "ok"
    )
    results.append(ok)
    status = PASS if ok else FAIL
    db_status = body.get("data", {}).get("database", "unknown") if body else "no body"
    py_ver = body.get("data", {}).get("python_version", "?") if body else "?"
    print(f"  [{status}] GET /api/v1/health   -> HTTP {r.status_code}  db={db_status}  py={py_ver}")

    # GET /health (bare, without prefix) -> 404
    r = client.get("/health")
    ok = r.status_code == 404
    results.append(ok)
    status = PASS if ok else FAIL
    print(f"  [{status}] GET /health          -> HTTP {r.status_code}  (expected 404, no bare /health)")

    # POST /api/v1/health -> 405 Method Not Allowed
    r = client.post("/api/v1/health")
    ok = r.status_code == 405
    results.append(ok)
    status = PASS if ok else FAIL
    print(f"  [{status}] POST /api/v1/health  -> HTTP {r.status_code}  (expected 405, GET only)")

    # GET /api/v1/nonexistent -> 404
    r = client.get("/api/v1/nonexistent")
    ok = r.status_code == 404
    results.append(ok)
    status = PASS if ok else FAIL
    print(f"  [{status}] GET /api/v1/none     -> HTTP {r.status_code}  (expected 404)")

# -------------------------------------------------------------------
# 4. Summary
# -------------------------------------------------------------------
passed = sum(results)
total  = len(results)
print("\n" + "=" * 65)
mark = "[OK]" if passed == total else "[FAILED]"
print(f"  RESULT: {passed}/{total} checks passed  {mark}")
print("=" * 65)

# -------------------------------------------------------------------
# 5. Health response payload
# -------------------------------------------------------------------
print("\n[4] HEALTH ENDPOINT FULL RESPONSE\n")
with app.test_client() as client:
    r = client.get("/api/v1/health")
    import json
    print(json.dumps(r.get_json(), indent=2))

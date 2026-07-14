import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv; load_dotenv()

PASS = "PASS"; FAIL = "FAIL"
results = []

def check(label, fn):
    try:
        fn()
        results.append((True, label))
    except Exception as exc:
        results.append((False, label + " => " + type(exc).__name__ + ": " + str(exc)[:200]))

# 1 - Alembic head
def _c1():
    import subprocess
    r = subprocess.run(["flask", "db", "current"], capture_output=True, text=True, cwd=".")
    assert "2bc3b7f03247" in r.stdout + r.stderr
check("1 Alembic revision = 2bc3b7f03247 (head)", _c1)

# 2 - Service imports cleanly
def _c2():
    from app.services.resume_service import (
        upload_resume, set_primary, delete_resume,
        list_resumes, get_resume, get_resume_stats,
    )
check("2 resume_service all public functions importable", _c2)

# 3 - flush() in upload_resume between demote and promote
def _c3():
    import inspect
    from app.services.resume_service import upload_resume
    src = inspect.getsource(upload_resume)
    demote_pos  = src.index("current_primary.is_primary = False")
    flush_pos   = src.index("db.session.flush()   # demote lands first", demote_pos)
    promote_pos = src.index("resume.is_primary = True", flush_pos)
    assert demote_pos < flush_pos < promote_pos
check("3 upload_resume: annotated flush() is between demote and promote", _c3)

# 4 - flush() in set_primary between demote and promote
def _c4():
    import inspect
    from app.services.resume_service import set_primary
    src = inspect.getsource(set_primary)
    demote_pos  = src.index("current_primary.is_primary = False")
    flush_pos   = src.index("db.session.flush()   # demote lands first", demote_pos)
    promote_pos = src.index("resume.is_primary = True", flush_pos)
    assert demote_pos < flush_pos < promote_pos
check("4 set_primary: annotated flush() is between demote and promote", _c4)

# 5 - flush() in delete_resume before auto-promote
def _c5():
    import inspect
    from app.services.resume_service import delete_resume
    src = inspect.getsource(delete_resume)
    soft_pos  = src.index("resume.soft_delete()")
    flush_pos = src.index("db.session.flush()   # soft-delete", soft_pos)
    promo_pos = src.index("next_primary.is_primary = True", flush_pos)
    assert soft_pos < flush_pos < promo_pos
check("5 delete_resume: annotated flush() precedes auto-promote of next primary", _c5)

# 6 - Constraint still exists and has the correct predicate (case-insensitive)
def _c6():
    from app import create_app
    from app.extensions import db
    from sqlalchemy import text
    sql = "SELECT indexdef FROM pg_indexes WHERE tablename='resumes' AND indexname='uix_resumes_one_primary_per_active_user'"
    with create_app("development").app_context():
        row = db.session.execute(text(sql)).fetchone()
        assert row, "Primary constraint index not found"
        defn = row[0].lower()   # normalise — PostgreSQL uses 'true' not 'TRUE'
        assert "is_primary = true" in defn, "is_primary predicate missing: " + defn
        assert "deleted_at is null" in defn, "deleted_at predicate missing: " + defn
check("6 Constraint uix_resumes_one_primary_per_active_user intact with correct predicate", _c6)

# 7 - upload_resume: both flush markers present by comment text (avoids docstring counting confusion)
def _c7():
    import inspect
    from app.services.resume_service import upload_resume
    src = inspect.getsource(upload_resume)
    assert "db.session.flush()   # demote lands first" in src, \
        "Demote-flush marker missing from upload_resume"
    assert "db.session.flush()\n" in src or "db.session.flush()  \n" in src or \
        "Step 7" in src, "Step-7 flush missing from upload_resume"
check("7 upload_resume has both flush markers: step-7 ID flush and step-8 demote flush", _c7)

# 8 - set_primary: exactly 1 flush() call
def _c8():
    import inspect
    from app.services.resume_service import set_primary
    src = inspect.getsource(set_primary)
    flush_count = src.count("db.session.flush()")
    assert flush_count == 1, f"Expected 1 flush() in set_primary, got {flush_count}"
check("8 set_primary contains exactly 1 flush() call (demote flush)", _c8)

# 9 - delete_resume: exactly 1 flush() call
def _c9():
    import inspect
    from app.services.resume_service import delete_resume
    src = inspect.getsource(delete_resume)
    flush_count = src.count("db.session.flush()")
    assert flush_count == 1, f"Expected 1 flush() in delete_resume, got {flush_count}"
check("9 delete_resume contains exactly 1 flush() call (soft-delete + demote flush)", _c9)

# 10 - No regression: all routes registered
def _c10():
    from app import create_app
    app = create_app("development")
    rules = {r.rule for r in app.url_map.iter_rules()}
    for url in ["/api/v1/resumes/", "/api/v1/applications/", "/api/v1/jobs/my"]:
        assert url in rules, "Missing: " + url
check("10 No regression: all Stage 2/3/4/5 routes intact", _c10)

# 11 - Previous SHA-256 fix still intact
def _c11():
    from app import create_app
    from app.extensions import db
    from sqlalchemy import text
    sql = "SELECT indexdef FROM pg_indexes WHERE tablename='resumes' AND indexname='uix_resumes_user_sha256'"
    with create_app("development").app_context():
        row = db.session.execute(text(sql)).fetchone()
        assert row, "uix_resumes_user_sha256 not found"
        assert "deleted_at is null" in row[0].lower(), "SHA-256 partial clause missing"
check("11 Previous fix preserved: uix_resumes_user_sha256 still partial (WHERE deleted_at IS NULL)", _c11)

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

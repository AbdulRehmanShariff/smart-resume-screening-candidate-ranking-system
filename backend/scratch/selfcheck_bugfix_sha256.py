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
        results.append((False, label + " => " + type(exc).__name__ + ": " + str(exc)[:160]))

# 1 — Live DB index has WHERE clause
def _c1():
    from app import create_app
    from app.extensions import db
    from sqlalchemy import text
    sql = "SELECT indexdef FROM pg_indexes WHERE tablename='resumes' AND indexname='uix_resumes_user_sha256'"
    with create_app("development").app_context():
        row = db.session.execute(text(sql)).fetchone()
        assert row, "Index uix_resumes_user_sha256 not found in pg_indexes"
        defn = row[0]
        assert "WHERE (deleted_at IS NULL)" in defn, "No WHERE clause in: " + defn
check("1 Live DB index has WHERE (deleted_at IS NULL) partial clause", _c1)

# 2 — Service has no sha256_hash = None mutation (workaround removed)
def _c2():
    import inspect
    from app.services import resume_service
    src = inspect.getsource(resume_service)
    assert "sha256_hash = None" not in src, "Workaround sha256_hash = None still present in service"
check("2 resume_service has no sha256_hash = None mutation (workaround removed)", _c2)

# 3 — Model index has postgresql_where
def _c3():
    from app.models.resume import Resume
    found = False
    for arg in Resume.__table_args__:
        if hasattr(arg, "name") and arg.name == "uix_resumes_user_sha256":
            found = True
            pg_opts = dict(arg.dialect_options.get("postgresql", {}))
            assert "where" in pg_opts, "postgresql_where missing. Got opts: " + str(pg_opts)
    assert found, "uix_resumes_user_sha256 not found in Resume.__table_args__"
check("3 Resume model index has postgresql_where clause in __table_args__", _c3)

# 4 — App starts cleanly, all blueprints registered
def _c4():
    from app import create_app
    app = create_app("development")
    rules = {r.rule for r in app.url_map.iter_rules()}
    for url in ["/api/v1/resumes/", "/api/v1/applications/", "/api/v1/jobs/"]:
        assert url in rules, "Missing route: " + url
check("4 App starts cleanly — resumes, applications, jobs blueprints all registered", _c4)

# 5 — Alembic current revision is the new head
def _c5():
    import subprocess
    r = subprocess.run(
        ["flask", "db", "current"],
        capture_output=True, text=True, cwd="."
    )
    out = r.stdout + r.stderr
    assert "9ed979bec81a" in out, "Revision 9ed979bec81a not current. Got: " + out[:300]
check("5 Alembic current revision = 9ed979bec81a (head)", _c5)

# 6 — Active duplicate still raises ConflictError
def _c6():
    import inspect
    from app.services.resume_service import upload_resume
    src = inspect.getsource(upload_resume)
    assert "ConflictError" in src, "ConflictError missing from upload_resume"
    assert "deleted_at.is_(None)" in src, "Active-only filter missing from duplicate check"
check("6 Active duplicate detection preserved — ConflictError still raised for active files", _c6)

# 7 — sha256_hash NOT NULL column unchanged (no data model weakening)
def _c7():
    from app.models.resume import Resume
    col = Resume.__table__.c["sha256_hash"]
    assert not col.nullable, "sha256_hash became nullable — unexpected model change"
check("7 sha256_hash column remains NOT NULL (historical records fully intact)", _c7)

# 8 — Migration file exists and has correct revision chain
def _c8():
    import os
    migration_path = os.path.join(
        "migrations", "versions",
        "9ed979bec81a_make_resume_sha256_index_partial_where_.py"
    )
    assert os.path.exists(migration_path), "Migration file not found: " + migration_path
    with open(migration_path) as f:
        src = f.read()
    assert "down_revision = \"ba24561c4834\"" in src, "down_revision chain broken"
    assert "WHERE deleted_at IS NULL" in src, "Partial index SQL missing from migration"
    assert "def upgrade" in src and "def downgrade" in src, "upgrade/downgrade missing"
check("8 Migration file exists with correct revision chain and DDL", _c8)

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

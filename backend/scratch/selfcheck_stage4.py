import os, sys, io, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv; load_dotenv()

PASS = "PASS"; FAIL = "FAIL"
results = []

def check(label, fn):
    try:
        fn()
        results.append((True, label))
    except Exception as exc:
        results.append((False, label + " => " + type(exc).__name__ + ": " + str(exc)[:120]))

# 4A-1
def _s1():
    from app.schemas.resume import ResumeUploadSchema
    d = ResumeUploadSchema().load({})
    assert d["set_as_primary"] is False and d["note"] is None
check("4A-1 upload schema defaults", _s1)

# 4A-2
def _s2():
    from app.schemas.resume import ResumeUploadSchema
    d = ResumeUploadSchema().load({"note": "  hi  "}); assert d["note"] == "hi"
    d2= ResumeUploadSchema().load({"note": "  "}); assert d2["note"] is None
check("4A-2 upload schema note strip", _s2)

# 4A-3
def _s3():
    from app.schemas.resume import ResumeListFilterSchema
    d = ResumeListFilterSchema().load({})
    assert d["page"]==1 and d["per_page"]==20 and d["sort_by"]=="newest"
check("4A-3 list filter defaults", _s3)

# 4A-4
def _s4():
    from marshmallow import ValidationError
    from app.schemas.resume import ResumeListFilterSchema
    s = ResumeListFilterSchema()
    try: s.load({"sort_by":"bad"}); assert False
    except ValidationError: pass
    try: s.load({"per_page":"200"}); assert False
    except ValidationError: pass
check("4A-4 list filter validation", _s4)

# 4A-5
def _s5():
    from marshmallow import ValidationError
    from app.schemas.resume import SetPrimarySchema
    try: SetPrimarySchema().load({"x":1}); assert False
    except ValidationError: pass
    SetPrimarySchema().load({})
check("4A-5 set-primary unknown field rejected", _s5)

# 4A-6
def _s6():
    from app.schemas.resume import SetPrimarySchema
    d  = SetPrimarySchema().load({"note":"  t  "}); assert d["note"]=="t"
    d2 = SetPrimarySchema().load({"note":""}); assert d2["note"] is None
check("4A-6 set-primary note strip", _s6)

# 4B-7
def _b7():
    import app.services.resume_service as rs
    assert hasattr(rs,"_RESUME_ALLOWED_MIME_TYPES")
    assert rs._HASH_CHUNK_SIZE == 64*1024
check("4B-7 service module + constants", _b7)

# 4B-8
def _b8():
    import inspect
    from app.services.resume_service import (
        upload_resume, list_resumes, get_resume,
        set_primary, delete_resume, get_resume_stats
    )
    assert "file"      in inspect.signature(upload_resume).parameters
    assert "note"      in inspect.signature(set_primary).parameters
    assert "candidate" in inspect.signature(get_resume_stats).parameters
check("4B-8 all 6 service functions + signatures", _b8)

# 4B-9
def _b9():
    from app.services.resume_service import _RESUME_ALLOWED_MIME_TYPES
    assert "application/pdf" in _RESUME_ALLOWED_MIME_TYPES
    assert "text/plain" not in _RESUME_ALLOWED_MIME_TYPES
    assert "image/png"  not in _RESUME_ALLOWED_MIME_TYPES
check("4B-9 allowed mimes PDF/DOC/DOCX only", _b9)

# 4B-10
def _b10():
    from app.services.resume_service import _compute_sha256_streaming
    content = b"hello world"
    expected = hashlib.sha256(content).hexdigest()
    buf = io.BytesIO(content)
    result = _compute_sha256_streaming(buf)
    assert result == expected
    assert buf.tell() == len(content)
check("4B-10 streaming SHA-256 correct + pointer at end", _b10)

# 4B-11
def _b11():
    from app import create_app
    from app.core.exceptions import BadRequestError
    from app.services.resume_service import _load_resume
    with create_app("development").app_context():
        try: _load_resume("bad"); assert False
        except BadRequestError: pass
check("4B-11 _load_resume bad UUID raises BadRequestError", _b11)

# 4B-12
def _b12():
    import uuid as _u
    from app import create_app
    from app.core.exceptions import NotFoundError
    from app.services.resume_service import _load_resume
    with create_app("development").app_context():
        try: _load_resume(str(_u.uuid4())); assert False
        except NotFoundError: pass
check("4B-12 _load_resume non-existent UUID raises NotFoundError", _b12)

# 4B-13
def _b13():
    from app.services.resume_service import _build_sort_clause
    for s in ("newest","oldest","name_asc","name_desc","size_asc","size_desc"):
        assert _build_sort_clause(s)
    assert _build_sort_clause("unknown")
check("4B-13 _build_sort_clause all values", _b13)

# 4C-14
def _c14():
    from app.api.resumes.routes import resumes_bp
    assert resumes_bp.name == "resumes"
check("4C-14 resumes_bp named resumes", _c14)

# 4C-15
def _c15():
    from app.api.resumes.routes import (
        upload_resume, list_resumes, get_resume_stats,
        get_resume, set_primary, delete_resume
    )
check("4C-15 all 6 route fns importable", _c15)

# 4C-16
def _c16():
    from app import create_app
    with create_app("development").app_context(): pass
check("4C-16 app boots with resumes_bp", _c16)

# 4C-17
def _c17():
    from app import create_app
    app = create_app("development")
    pm = {}
    for r in app.url_map.iter_rules():
        if "/resumes" in r.rule:
            pm.setdefault(r.rule, set()).update(r.methods - {"HEAD","OPTIONS"})
    expected = {
        "/api/v1/resumes/":                        {"POST","GET"},
        "/api/v1/resumes/stats":                   {"GET"},
        "/api/v1/resumes/<resume_id>":             {"GET","DELETE"},
        "/api/v1/resumes/<resume_id>/set-primary": {"PATCH"},
    }
    for path, methods in expected.items():
        assert path in pm, "Missing: " + path
        assert methods.issubset(pm[path]), path + " methods wrong: " + str(pm[path])
check("4C-17 all URL rules registered with correct methods", _c17)

# 4C-18
def _c18():
    from app import create_app
    app = create_app("development")
    client = app.test_client()
    for m,p in [("POST","/api/v1/resumes/"),("GET","/api/v1/resumes/"),("GET","/api/v1/resumes/stats")]:
        r = client.open(p, method=m)
        assert r.status_code == 401, m + " " + p + " got " + str(r.status_code)
check("4C-18 unauthenticated requests return 401", _c18)

# 4C-19
def _c19():
    from app import create_app
    r = create_app("development").test_client().get("/api/v1/resumes/not-a-uuid")
    assert r.status_code in (400,401,404), "Got " + str(r.status_code)
check("4C-19 bad UUID returns 400/401/404 not 500", _c19)

# 4C-20
def _c20():
    from app import create_app
    app = create_app("development")
    rr = {r.rule for r in app.url_map.iter_rules() if "/resumes" in r.rule}
    aa = {r.rule for r in app.url_map.iter_rules() if "/auth"    in r.rule}
    jj = {r.rule for r in app.url_map.iter_rules() if "/jobs"    in r.rule}
    assert not (rr & aa) and not (rr & jj)
check("4C-20 no URL conflicts across blueprints", _c20)

# 4C-21
def _c21():
    from app import create_app
    app = create_app("development")
    assert isinstance(app.config.get("MAX_RESUME_SIZE_MB"), int)
    assert isinstance(app.config.get("MAX_JD_SIZE_MB"), int)
    assert app.config["MAX_RESUME_SIZE_MB"] > 0
check("4C-21 config has MAX_RESUME_SIZE_MB and MAX_JD_SIZE_MB", _c21)

# 4C-22
def _c22():
    from app.schemas.auth import LoginSchema
    from app.schemas.job import CreateJobSchema
    from app.services.auth_service import login
    from app.services.job_service import create_job
    from app.api.auth.routes import auth_bp
    from app.api.jobs.routes import jobs_bp
    assert auth_bp.name == "auth" and jobs_bp.name == "jobs"
check("4C-22 stages 1-3 no regression", _c22)

# Results
print()
print("=" * 62)
passed = sum(1 for ok,_ in results if ok)
total  = len(results)
for ok, label in results:
    print("  [" + (PASS if ok else FAIL) + "] " + label)
print("=" * 62)
status = "ALL PASSED" if passed == total else "FAILURES DETECTED"
print("  RESULT: " + str(passed) + "/" + str(total) + " [" + status + "]")
print("=" * 62)
sys.exit(0 if passed == total else 1)

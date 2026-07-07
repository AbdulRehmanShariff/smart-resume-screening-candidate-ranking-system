"""
scratch/diagnose_login.py
--------------------------
Diagnostic: register a fresh user, then immediately call login() service
to isolate whether the bug is in the password storage, verification, or
the login query.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from app import create_app
app = create_app("development")

TEST_EMAIL    = "diagtest_login@example.com"
TEST_PASSWORD = "DiagPass99!"

with app.app_context():
    from sqlalchemy import select, text
    from app.extensions import db, bcrypt
    from app.models.user import User

    # ----------------------------------------------------------------
    # 0. Show existing user state
    # ----------------------------------------------------------------
    print("=== Existing users ===")
    rows = db.session.execute(
        text("SELECT email, is_verified, is_active, is_suspended, deleted_at, LEFT(password_hash,10) as h FROM users ORDER BY created_at DESC")
    ).fetchall()
    print(f"Total users: {len(rows)}")
    for r in rows:
        print(f"  {r[0]!r:45s} verified={r[1]} active={r[2]} suspended={r[3]} deleted={r[4]} hash={r[5]!r}")

    # ----------------------------------------------------------------
    # 1. Clean up any leftover test user
    # ----------------------------------------------------------------
    db.session.execute(text("DELETE FROM users WHERE email = :e"), {"e": TEST_EMAIL})
    db.session.commit()

    # ----------------------------------------------------------------
    # 2. Register a fresh user via the service
    # ----------------------------------------------------------------
    print("\n=== Registering fresh test user ===")
    from app.schemas.auth import CandidateRegistrationSchema
    from app.services import auth_service

    try:
        data = CandidateRegistrationSchema().load({
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "first_name": "Diag",
            "last_name": "Test",
        })
        reg_result = auth_service.register_candidate(data)
        print(f"  Registered OK | email={reg_result['email']} | is_verified={reg_result['is_verified']}")
    except Exception as e:
        print(f"  Registration FAILED: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    # ----------------------------------------------------------------
    # 3. Probe the stored hash directly from the DB
    # ----------------------------------------------------------------
    print("\n=== DB-level hash probe ===")
    user = db.session.execute(
        select(User).where(User.email == TEST_EMAIL)
    ).scalar_one_or_none()

    if not user:
        print("  ERROR: user not found after registration!")
        sys.exit(1)

    print(f"  password_hash value  : {user.password_hash!r}")
    print(f"  password_hash length : {len(user.password_hash)}")
    print(f"  password_hash type   : {type(user.password_hash).__name__}")

    # Direct bcrypt checks
    r_correct = bcrypt.check_password_hash(user.password_hash, TEST_PASSWORD)
    r_wrong   = bcrypt.check_password_hash(user.password_hash, "WrongPassword!")
    print(f"  bcrypt.check_password_hash(hash, correct_pw) = {r_correct}")
    print(f"  bcrypt.check_password_hash(hash, wrong_pw)   = {r_wrong}")

    # User model method
    m_correct = user.check_password(TEST_PASSWORD)
    m_wrong   = user.check_password("WrongPassword!")
    print(f"  user.check_password(correct_pw) = {m_correct}")
    print(f"  user.check_password(wrong_pw)   = {m_wrong}")

    # ----------------------------------------------------------------
    # 4. Manually verify email, then call login()
    # ----------------------------------------------------------------
    print("\n=== Login flow (email manually verified) ===")
    db.session.execute(
        text("UPDATE users SET is_verified=true WHERE email = :e"),
        {"e": TEST_EMAIL}
    )
    db.session.commit()
    # Expire cached object so the updated is_verified is visible
    db.session.expire(user)

    from app.schemas.auth import LoginSchema
    try:
        login_data = LoginSchema().load({
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
        })
        print(f"  Schema loaded: email={login_data['email']!r}")
        print(f"  Password from schema: {login_data['password']!r}")
        tokens = auth_service.login(login_data)
        print(f"  LOGIN SUCCESS | token_type={tokens['token_type']}")
    except Exception as e:
        print(f"  LOGIN FAILED: {type(e).__name__}: {e}")

    # ----------------------------------------------------------------
    # 5. Inspect LoginSchema — does it transform the password?
    # ----------------------------------------------------------------
    print("\n=== Schema inspection ===")
    from app.schemas.auth import LoginSchema as LS
    import inspect, marshmallow
    ls = LS()
    print(f"  LoginSchema fields: {list(ls.fields.keys())}")
    pw_field = ls.fields.get("password")
    if pw_field:
        print(f"  password field type: {type(pw_field).__name__}")
        print(f"  password field attrs: load_only={getattr(pw_field, 'load_only', '?')}")
        # Check if there are any pre/post load methods that transform the password
        for name, method in inspect.getmembers(ls, predicate=inspect.ismethod):
            if any(hasattr(method, attr) for attr in ["marshmallow_hook", "_hooks"]):
                print(f"    hook method: {name}")
        hooks = getattr(ls, "_hooks", {})
        print(f"  Schema hooks: {list(hooks.keys()) if hooks else 'none'}")

    # ----------------------------------------------------------------
    # Cleanup
    # ----------------------------------------------------------------
    db.session.execute(text("DELETE FROM users WHERE email = :e"), {"e": TEST_EMAIL})
    db.session.commit()
    print("\n=== Cleanup done ===")

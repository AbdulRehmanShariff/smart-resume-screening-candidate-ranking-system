"""
scratch/diagnose_login2.py
---------------------------
Diagnose exactly why login fails for existing users.
Shows every user's account state and verifies check_password works
against the actual stored hash for a given password.

Usage:
    python scratch/diagnose_login2.py [email] [password]

If no args, just shows user state.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from app import create_app
app = create_app("development")

test_email    = sys.argv[1] if len(sys.argv) > 1 else None
test_password = sys.argv[2] if len(sys.argv) > 2 else None

with app.app_context():
    from sqlalchemy import select, text
    from app.extensions import db, bcrypt
    from app.models.user import User

    # ------------------------------------------------------------------
    # Show every user's complete account state
    # ------------------------------------------------------------------
    print("=" * 70)
    print("ALL USERS — ACCOUNT STATE")
    print("=" * 70)

    users = db.session.execute(
        text("""
            SELECT u.email, u.is_verified, u.is_active, u.is_suspended,
                   u.deleted_at, r.name as role,
                   LEFT(u.password_hash, 7) as hash_prefix,
                   LENGTH(u.password_hash) as hash_len,
                   u.created_at
            FROM users u
            JOIN roles r ON r.id = u.role_id
            ORDER BY u.created_at DESC
        """)
    ).fetchall()

    print(f"{'EMAIL':<40} {'VFY':>3} {'ACT':>3} {'SUS':>3} {'DEL':>5} {'ROLE':>10} {'HASH':>12}")
    print("-" * 80)
    for row in users:
        email, vfy, act, sus, dlt, role, hpfx, hlen, created = row
        print(f"{email:<40} {str(vfy):>3} {str(act):>3} {str(sus):>3} {str(bool(dlt)):>5} {role:>10} {hpfx or '?':>7}(len={hlen})")

    print()

    # ------------------------------------------------------------------
    # Show can_login status for each user via the model
    # ------------------------------------------------------------------
    print("=" * 70)
    print("can_login PROPERTY CHECK")
    print("=" * 70)
    orm_users = db.session.execute(select(User)).scalars().all()
    for u in orm_users:
        print(f"  {u.email:<40}  can_login={u.can_login}  "
              f"(active={u.is_active}, verified={u.is_verified}, "
              f"suspended={u.is_suspended}, deleted={u.is_deleted})")

    # ------------------------------------------------------------------
    # If email+password provided, test directly
    # ------------------------------------------------------------------
    if test_email and test_password:
        print()
        print("=" * 70)
        print(f"DIRECT LOGIN TEST: {test_email!r}")
        print("=" * 70)

        user = db.session.execute(
            select(User).where(User.email == test_email)
        ).scalar_one_or_none()

        if user is None:
            print(f"  ERROR: No user found with email {test_email!r}")
        else:
            print(f"  email         : {user.email}")
            print(f"  is_verified   : {user.is_verified}")
            print(f"  is_active     : {user.is_active}")
            print(f"  is_suspended  : {user.is_suspended}")
            print(f"  is_deleted    : {user.is_deleted}")
            print(f"  can_login     : {user.can_login}")
            print(f"  hash[:10]     : {user.password_hash[:10]!r}")
            print(f"  hash length   : {len(user.password_hash)}")
            pw_ok = user.check_password(test_password)
            print(f"  check_password({test_password!r}) = {pw_ok}")
            raw_ok = bcrypt.check_password_hash(user.password_hash, test_password)
            print(f"  bcrypt.check_password_hash(hash, pw) = {raw_ok}")

            print()
            if not user.is_verified:
                print("  *** DIAGNOSIS: Login fails because is_verified=False ***")
                print("  The user registered after Batch 2B was deployed.")
                print("  They must verify their email first, OR an admin must")
                print("  manually set is_verified=True in the database.")
                print()
                print("  FIX OPTIONS:")
                print("  1. Use POST /api/v1/auth/resend-verification to resend the email link.")
                print("  2. Run the SQL below to manually verify this user:")
                print(f"     UPDATE users SET is_verified=true WHERE email='{test_email}';")
            elif not pw_ok:
                print("  *** DIAGNOSIS: Password mismatch — check_password returned False ***")
                print("  The stored hash does not match the provided password.")
            elif not user.can_login:
                print(f"  *** DIAGNOSIS: can_login=False ***")
                print(f"  active={user.is_active} verified={user.is_verified} "
                      f"suspended={user.is_suspended} deleted={user.is_deleted}")
            else:
                print("  *** All checks pass — login should succeed ***")
                from app.services import auth_service
                from app.schemas.auth import LoginSchema
                try:
                    data = LoginSchema().load({"email": test_email, "password": test_password})
                    result = auth_service.login(data)
                    print(f"  auth_service.login() result: SUCCESS | token_type={result['token_type']}")
                except Exception as e:
                    print(f"  auth_service.login() FAILED: {type(e).__name__}: {e}")
    else:
        print()
        print("Tip: pass email and password as arguments to test a specific login:")
        print("  python scratch/diagnose_login2.py user@example.com MyPassword123")

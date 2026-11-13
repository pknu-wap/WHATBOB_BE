"""Run one bounded cleanup. The operator schedules this every 600 seconds.

No per-ASGI-worker scheduler is started; the execution owner is an operations decision.
"""
from app.auth.sessions import cleanup_auth_sessions_once
from app.core.database import SessionLocal


if __name__ == "__main__":
    with SessionLocal() as db:
        deleted = cleanup_auth_sessions_once(db)
    print(f"Deleted {deleted} expired/revoked authentication sessions; users were retained.")

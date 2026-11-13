from app.auth.sessions import new_session
from app.core.security import create_access_token


def session_token(db, user, *, expires_delta=None):
    db.flush()
    record, _ = new_session(user.id)
    db.add(record)
    db.commit()
    return create_access_token({"sub": str(user.id), "sid": str(record.id)}, expires_delta=expires_delta)

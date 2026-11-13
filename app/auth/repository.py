from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import AuthSession
from app.users.models import User


def find_session_by_hash(db: Session, secret_hash: bytes, *, lock: bool = False):
    statement = select(AuthSession).where(AuthSession.secret_hash == secret_hash)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return db.execute(statement).scalar_one_or_none()


def find_session_user(db: Session, session_id: int):
    return db.execute(
        select(AuthSession, User).outerjoin(User, User.id == AuthSession.user_id)
        .where(AuthSession.id == session_id).execution_options(populate_existing=True)
    ).one_or_none()

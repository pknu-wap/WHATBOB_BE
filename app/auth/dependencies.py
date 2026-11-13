from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.auth.errors import AuthError
from app.auth.sessions import authenticate_access_token
from app.users.models import User

bearer_scheme = HTTPBearer(auto_error=False)


def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User | None:
    if credentials is None:
        return None
    return get_current_user(credentials=credentials, db=db)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError("ACCESS_TOKEN_MISSING", "로그인이 필요합니다.", bearer=True)
    user, payload = authenticate_access_token(db, credentials.credentials)
    user._auth_session_id = int(payload["sid"])
    user._auth_payload = payload
    return user

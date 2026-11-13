import base64
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.errors import AuthError, database_errors
from app.auth.models import AuthSession
from app.auth import repository
from app.auth.schemas import PreparationResponse, TokenResponse
from app.core.config import settings
from app.core.security import create_access_token, decode_access_token
from app.users.models import User
from app.users.schemas import UserResponse

PREPARATION_SECONDS = 300
SESSION_SECONDS = 30 * 24 * 60 * 60
PREPARATION_COOKIE_SECONDS = SESSION_SECONDS + PREPARATION_SECONDS
DEFAULT_PROFILE_IMAGE = "/static/default-profile.svg"
CLEANUP_BATCH_SIZE = 100
NICKNAME_ADJECTIVES = ("즐거운", "든든한", "신나는", "행복한", "배부른", "웃는", "멋진", "용감한")
NICKNAME_FOODS = ("김밥", "만두", "두부", "국수", "감자", "주먹밥", "비빔밥", "고구마")


def utc_now() -> datetime:
    """MySQL DATETIME stores UTC without timezone metadata."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def hash_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", secret) is None:
        raise AuthError("INVALID_AUTH_COOKIE", "인증 쿠키 형식을 확인해 주세요.", 400)
    decoded = base64.urlsafe_b64decode(secret + "=")
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != secret:
        raise AuthError("INVALID_AUTH_COOKIE", "인증 쿠키 형식을 확인해 주세요.", 400)
    return hashlib.sha256(secret.encode("ascii")).digest()


def new_session(user_id: int | None = None) -> tuple[AuthSession, str]:
    secret = secrets.token_urlsafe(32)
    now = utc_now()
    return AuthSession(user_id=user_id, secret_hash=hash_secret(secret), created_at=now,
                       expires_at=now + timedelta(seconds=SESSION_SECONDS if user_id else PREPARATION_SECONDS)), secret


def validate_session(record: AuthSession | None, *, bearer: bool = False) -> AuthSession:
    if record is None:
        raise AuthError("SESSION_NOT_FOUND", "인증 기록을 찾을 수 없습니다.", bearer=bearer)
    if record.revoked_at is not None:
        raise AuthError("SESSION_REVOKED", "종료된 인증입니다.", bearer=bearer)
    if utc_now() >= as_utc_naive(record.expires_at):
        code = "PREPARATION_EXPIRED" if record.user_id is None else "SESSION_EXPIRED"
        raise AuthError(code, "인증 유효 시간이 지났습니다.", bearer=bearer)
    return record


def require_secret(secret: str | None) -> bytes:
    if secret is None:
        raise AuthError("AUTH_COOKIE_MISSING", "인증 쿠키가 필요합니다.")
    return hash_secret(secret)


def prepare_authentication(db: Session, secret: str | None):
    secret_hash = hash_secret(secret) if secret is not None else None
    with database_errors(db):
        record = repository.find_session_by_hash(db, secret_hash) if secret_hash is not None else None
        notice = None
        if record is not None:
            try:
                validate_session(record)
            except AuthError as error:
                if error.code not in {"PREPARATION_EXPIRED", "SESSION_EXPIRED"}:
                    raise
                if record.user_id is not None:
                    notice = "PREVIOUS_SESSION_UNAVAILABLE"
            else:
                if record.user_id is not None and db.get(User, record.user_id) is None:
                    raise AuthError("SESSION_UNAVAILABLE", "이 인증으로 사용자를 복원할 수 없습니다.")
                return PreparationResponse(next_action="CREATE_USER" if record.user_id is None else "ISSUE_TOKEN"), None
        elif secret is not None:
            notice = "PREVIOUS_SESSION_UNAVAILABLE"
        record, new_secret = new_session()
        db.add(record)
        db.commit()
        return PreparationResponse(next_action="CREATE_USER", notice=notice), new_secret


def generate_nickname() -> str:
    return f"{secrets.choice(NICKNAME_ADJECTIVES)}{secrets.choice(NICKNAME_FOODS)}{secrets.randbelow(100000):05d}"


def create_uuid_user(db: Session) -> User:
    from uuid import uuid4
    for _ in range(settings.AUTH_NICKNAME_ATTEMPTS):
        nickname = generate_nickname()
        now = utc_now()
        generated_uuid = str(uuid4())
        user = User(
            uuid=generated_uuid,
            nickname=nickname,
            nickname_key=nickname.encode("utf-8"),
            profile_image=DEFAULT_PROFILE_IMAGE,
            nickname_source_uuid=generated_uuid,
            nickname_source_created_at=now,
            profile_image_source_uuid=generated_uuid,
            profile_image_source_created_at=now,
        )
        try:
            with db.begin_nested():
                db.add(user)
                db.flush()
            user.nickname_source_id = user.id
            user.profile_image_source_id = user.id
            return user
        except IntegrityError as error:
            # Only uniqueness collisions are retryable; FK/schema failures must surface.
            if "UNIQUE" not in str(error.orig).upper() and getattr(error.orig, "args", [None])[0] != 1062:
                raise
    raise AuthError("AUTH_TEMPORARILY_UNAVAILABLE", "사용자 생성이 혼잡합니다. 잠시 후 다시 시도해 주세요.", 503)


def token_response(user: User, record: AuthSession, *, is_new_user: bool = False) -> TokenResponse:
    return TokenResponse(access_token=create_access_token({"sub": str(user.id), "sid": str(record.id)}),
                         user=UserResponse.model_validate(user), is_new_user=is_new_user)


def issue_cookie_token(
    db: Session,
    secret: str | None,
    *,
    create_user: bool = False,
    expected_context: str | None = None,
):
    secret_hash = require_secret(secret)
    with database_errors(db):
        try:
            record = validate_session(repository.find_session_by_hash(db, secret_hash, lock=True))
            created = False
            if record.user_id is None and not create_user:
                raise AuthError("AUTHENTICATION_NOT_READY", "사용자 생성이 필요합니다.", 409)
            existing_user = db.get(User, record.user_id) if record.user_id is not None else None
            if record.user_id is not None and existing_user is None:
                raise AuthError("SESSION_UNAVAILABLE", "이 인증으로 사용자를 복원할 수 없습니다.")
            if expected_context is not None and expected_context.strip():
                ctx = expected_context.strip()
                user_uuid = existing_user.uuid if existing_user is not None else None
                if ctx != str(record.id) and (user_uuid is None or ctx != user_uuid):
                    raise AuthError("AUTH_CONTEXT_CHANGED", "다른 탭에서 계정이 변경되었습니다.", 409)
            # Establish the outer write transaction before nickname savepoints.
            record.expires_at = utc_now() + timedelta(seconds=SESSION_SECONDS)
            db.flush()
            if record.user_id is None:
                user = create_uuid_user(db)
                record.user_id = user.id
                created = True
            else:
                user = existing_user
            result = token_response(user, record, is_new_user=created)
            db.commit()
            return result
        except AuthError:
            db.rollback()
            raise


def authenticate_access_token(db: Session, token: str):
    payload = decode_access_token(token)
    with database_errors(db, bearer=True):
        result = repository.find_session_user(db, int(payload["sid"]))
        record = validate_session(result[0] if result else None, bearer=True)
        if record.user_id is None or str(record.user_id) != payload["sub"]:
            raise AuthError("ACCESS_TOKEN_INVALID", "접근 토큰의 사용자 정보가 일치하지 않습니다.", bearer=True)
        user = result[1]
        if user is None:
            raise AuthError("SESSION_UNAVAILABLE", "사용자 인증을 확인할 수 없습니다.", bearer=True)
        return user, payload


def revoke_session(db: Session, session_id: int) -> None:
    with database_errors(db):
        record = db.execute(select(AuthSession).where(AuthSession.id == session_id)
                            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if record is not None and record.revoked_at is None:
            record.revoked_at = utc_now()
        db.commit()


def cleanup_auth_sessions_once(db: Session) -> int:
    now = utc_now()
    with database_errors(db):
        revoked = db.execute(select(AuthSession.id, AuthSession.revoked_at)
                             .where(AuthSession.revoked_at.is_not(None))
                             .order_by(AuthSession.revoked_at, AuthSession.id).limit(CLEANUP_BATCH_SIZE)).all()
        expired = db.execute(select(AuthSession.id, AuthSession.expires_at)
                             .where(AuthSession.revoked_at.is_(None), AuthSession.expires_at <= now)
                             .order_by(AuthSession.expires_at, AuthSession.id).limit(CLEANUP_BATCH_SIZE)).all()
        ids = sorted(row[0] for row in sorted([*revoked, *expired], key=lambda row: (row[1], row[0]))[:CLEANUP_BATCH_SIZE])
        if not ids:
            return 0
        records = db.execute(select(AuthSession).where(AuthSession.id.in_(ids))
                             .order_by(AuthSession.id).with_for_update()
                             .execution_options(populate_existing=True)).scalars().all()
        deleted = 0
        for record in records:
            if record.revoked_at is not None or as_utc_naive(record.expires_at) <= now:
                db.delete(record)
                deleted += 1
        db.commit()
        return deleted

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.errors import AuthError
from app.auth.models import AuthSession, RequestOperation
from app.auth.sessions import utc_now, validate_session
from app.users.models import User

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def get_user_session_id(user: User) -> int | None:
    sid = getattr(user, "_auth_session_id", None)
    return int(sid) if sid is not None else None


def lock_user_and_verify_session(db: Session, current_user: User) -> User:
    """Lock the User row and re-verify the active AuthSession so revoked/merged sessions fail immediately."""
    locked_user = (
        db.execute(
            select(User)
            .where(User.id == current_user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalar_one_or_none()
    )
    if locked_user is None:
        raise AuthError("SESSION_UNAVAILABLE", "사용자 인증을 확인할 수 없습니다.", bearer=True)

    session_id = get_user_session_id(current_user)
    if session_id is not None:
        record = (
            db.execute(
                select(AuthSession)
                .where(AuthSession.id == session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            .scalar_one_or_none()
        )
        record = validate_session(record, bearer=True)
        if record.user_id != locked_user.id:
            raise AuthError("ACCESS_TOKEN_INVALID", "접근 토큰의 사용자 정보가 일치하지 않습니다.", bearer=True)
        locked_user._auth_session_id = session_id

    return locked_user


def normalize_idempotency_key(raw_key: str | None, *, required: bool = False) -> str | None:
    if raw_key is None:
        if required:
            raise ValueError("Idempotency-Key 헤더가 필요합니다. (IDEMPOTENCY_KEY_REQUIRED)")
        return None
    cleaned = raw_key.strip()
    if not cleaned:
        if required:
            raise ValueError("Idempotency-Key 헤더가 필요합니다. (IDEMPOTENCY_KEY_REQUIRED)")
        return None
    if IDEMPOTENCY_KEY_PATTERN.fullmatch(cleaned) is None:
        raise ValueError("Idempotency-Key 형식이 올바르지 않습니다. (INVALID_IDEMPOTENCY_KEY)")
    return cleaned


def compute_payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def acquire_or_inspect_operation(
    db: Session,
    *,
    user_id: int,
    auth_session_id: int | None,
    scope: str,
    operation_key: str,
    payload_hash: str,
    room_id: str | None = None,
) -> tuple[RequestOperation, bool]:
    """
    Lock or create a RequestOperation row.
    Returns (operation, is_existing).
    """
    existing = (
        db.execute(
            select(RequestOperation)
            .where(
                RequestOperation.user_id == user_id,
                RequestOperation.scope == scope,
                RequestOperation.operation_key == operation_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalar_one_or_none()
    )
    if existing is not None:
        return existing, True

    now = utc_now()
    op = RequestOperation(
        scope=scope,
        operation_key=operation_key,
        user_id=user_id,
        auth_session_id=auth_session_id,
        status="PENDING",
        payload_hash=payload_hash,
        room_id=room_id,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(op)
            db.flush()
        return op, False
    except IntegrityError:
        existing = (
            db.execute(
                select(RequestOperation)
                .where(
                    RequestOperation.user_id == user_id,
                    RequestOperation.scope == scope,
                    RequestOperation.operation_key == operation_key,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            .scalar_one()
        )
        return existing, True

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.auth.cookie_router import require_csrf, set_auth_cookie
from app.auth.dependencies import bearer_scheme, get_current_user
from app.auth.schemas import (
    TransitionCommitResponse,
    TransitionPrepareRequest,
    TransitionPrepareResponse,
    TransitionStatusResponse,
)
from app.auth.sessions import SESSION_SECONDS
from app.auth.transitions import (
    commit_account_transition,
    get_account_transition_status,
    prepare_account_transition,
    recover_account_transition,
)
from app.core.config import settings
from app.core.database import get_db
from app.users.models import User


def set_transition_prepare_cookie(response: Response, secret: str, max_age: int) -> None:
    response.set_cookie(
        settings.TRANSITION_PREPARE_COOKIE_NAME,
        secret,
        max_age=max_age,
        expires=datetime.now(timezone.utc) + timedelta(seconds=max_age),
        path="/api/auth",
        secure=True,
        httponly=True,
        samesite="none",
    )


def clear_transition_prepare_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.TRANSITION_PREPARE_COOKIE_NAME,
        path="/api/auth",
        secure=True,
        httponly=True,
        samesite="none",
    )


router = APIRouter(prefix="/api/auth/transitions", tags=["AuthTransitions"])


@router.post(
    "",
    response_model=TransitionPrepareResponse,
    dependencies=[Depends(require_csrf)],
)
async def prepare_transition_endpoint(
    payload: TransitionPrepareRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """카카오 연결(KAKAO_LINK) 또는 로그아웃(LOGOUT) 1단계 준비를 수행하고 별도 준비 쿠키를 발급한다."""
    raw_idem = idempotency_key if isinstance(idempotency_key, str) else None
    result, prepare_secret = await prepare_account_transition(
        db,
        current_user,
        payload,
        expected_context=request.headers.get("x-whatbob-auth-context"),
        auth_cookie_secret=request.cookies.get(settings.AUTH_COOKIE_NAME),
        idempotency_key_header=raw_idem,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    set_transition_prepare_cookie(
        response,
        prepare_secret,
        int(settings.TRANSITION_RECOVERY_SECONDS),
    )
    return result


@router.post(
    "/{transition_id}/commit",
    response_model=TransitionCommitResponse,
    dependencies=[Depends(require_csrf)],
)
def commit_transition_endpoint(
    transition_id: str,
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    """준비 비밀 쿠키를 검증하고 계정 전환(무손실 이관/병합 + 원본 물리 삭제 또는 로그아웃 새 UUID 발급)을 원자적으로 확정한다."""
    prepare_secret = (
        request.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME)
        or request.cookies.get(settings.AUTH_COOKIE_NAME)
    )
    bearer_token = (
        credentials.credentials
        if credentials is not None and credentials.scheme.lower() == "bearer"
        else None
    )
    result, active_secret = commit_account_transition(
        db,
        transition_id,
        prepare_secret=prepare_secret,
        expected_context=request.headers.get("x-whatbob-auth-context"),
        bearer_token=bearer_token,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    set_auth_cookie(response, active_secret, SESSION_SECONDS)
    clear_transition_prepare_cookie(response)
    return result


@router.get(
    "/{transition_id}",
    response_model=TransitionStatusResponse,
)
def get_transition_status_endpoint(
    transition_id: str,
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    """소유권(준비 쿠키 또는 원본/대상 세션 JWT)이 확인된 본인 전환 작업의 상태를 조회한다."""
    bearer_token = (
        credentials.credentials
        if credentials is not None and credentials.scheme.lower() == "bearer"
        else None
    )
    result = get_account_transition_status(
        db,
        transition_id,
        prepare_secret=request.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME),
        auth_cookie_secret=request.cookies.get(settings.AUTH_COOKIE_NAME),
        bearer_token=bearer_token,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{transition_id}/recovery",
    response_model=TransitionCommitResponse,
    dependencies=[Depends(require_csrf)],
)
def recover_transition_endpoint(
    transition_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """COMMITTED 후 응답이 유실된 전환 결과를 준비 비밀 쿠키로 복구한다 (원본 물리 삭제 후에도 동작)."""
    result, active_secret = recover_account_transition(
        db,
        transition_id,
        prepare_secret=request.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME),
        auth_cookie_secret=request.cookies.get(settings.AUTH_COOKIE_NAME),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    set_auth_cookie(response, active_secret, SESSION_SECONDS)
    clear_transition_prepare_cookie(response)
    return result

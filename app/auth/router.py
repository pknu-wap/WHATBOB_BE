from datetime import timedelta
import secrets
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.auth.cookie_router import get_request_auth_secret, is_mobile_app_request
from app.auth.dependencies import bearer_scheme
from app.auth.models import AuthSession
from app.auth import repository
from app.auth.schemas import KakaoLoginRequest, TokenResponse
from app.auth.service import process_kakao_login
from app.auth.sessions import (
    PREPARATION_SECONDS,
    authenticate_access_token,
    hash_oauth_state,
    hash_secret,
    utc_now,
    validate_session,
)
from app.core.config import settings
from app.core.database import get_db
from app.auth.errors import AuthError, database_errors

router = APIRouter(prefix="/api/auth/kakao", tags=["Auth"])


def require_standalone_kakao_login(request: Request):
    # Until T06 implements authenticated record migration, do not silently
    # replace a browser's UUID identity with an unrelated provider login.
    if settings.AUTH_COOKIE_NAME in request.cookies:
        raise AuthError("ACCOUNT_LINKING_NOT_AVAILABLE", "현재 사용자 기록의 카카오 연결은 아직 지원하지 않습니다.", 409)


def prevent_token_caching(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _resolve_active_session_for_oauth_url(
    db: Session,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> AuthSession | None:
    if credentials is not None and credentials.scheme.lower() == "bearer" and credentials.credentials:
        _, payload = authenticate_access_token(db, credentials.credentials)
        sid = int(payload["sid"])
        record = db.get(AuthSession, sid)
        return validate_session(record, bearer=True)

    secret = get_request_auth_secret(request)
    if secret:
        secret_hash = hash_secret(secret)
        with database_errors(db):
            record = repository.find_session_by_hash(db, secret_hash)
            return validate_session(record)
    return None


@router.get("/login/url")
def get_kakao_login_url(
    request: Request,
    response: Response,
    state: str | None = Query(default=None, min_length=1, description="선택적 로그인 요청 식별값(생략 시 서버가 생성)"),
    platform: Literal["web", "app"] | None = Query(default=None, description="클라이언트 플랫폼(web 또는 app)"),
    redirect_uri: str | None = Query(default=None, description="명시적 redirect_uri"),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    """세션의 재연결 필요 여부(requires_reauth)에 따라 prompt=login을 강제하고 최신 1회용 state를 세션에 기록합니다."""
    prevent_token_caching(response)
    session_record = _resolve_active_session_for_oauth_url(db, request, credentials)

    if session_record is not None:
        effective_state = state.strip() if state and state.strip() else secrets.token_urlsafe(24)
        now = utc_now()
        with database_errors(db):
            locked_session = db.get(AuthSession, int(session_record.id))
            validate_session(locked_session)
            locked_session.oauth_state_hash = hash_oauth_state(effective_state)
            locked_session.oauth_state_expires_at = now + timedelta(seconds=PREPARATION_SECONDS)
            requires_reauth = bool(locked_session.requires_reauth)
            db.commit()
    else:
        if not state or not state.strip():
            raise HTTPException(status_code=422, detail="state query parameter is required")
        effective_state = state.strip()
        requires_reauth = False

    is_app = platform == "app" or is_mobile_app_request(request)
    if redirect_uri and redirect_uri.strip():
        effective_redirect_uri = redirect_uri.strip()
    elif is_app:
        effective_redirect_uri = settings.KAKAO_APP_REDIRECT_URI
    else:
        effective_redirect_uri = settings.KAKAO_REDIRECT_URI

    params = {
        "client_id": settings.KAKAO_REST_API_KEY,
        "redirect_uri": effective_redirect_uri,
        "response_type": "code",
        "state": effective_state,
    }
    prompt_val: str | None = None
    if requires_reauth:
        params["prompt"] = "login"
        prompt_val = "login"

    ua = (request.headers.get("user-agent") or "").lower()
    kakao_in_app_browser = "kakaotalk" in ua
    requires_external_browser = bool(requires_reauth and kakao_in_app_browser)

    url = "https://kauth.kakao.com/oauth/authorize?" + urlencode(params)
    return {
        "login_url": url,
        "state": effective_state,
        "requires_reauth": requires_reauth,
        "prompt": prompt_val,
        "kakao_in_app_browser": kakao_in_app_browser,
        "requires_external_browser": requires_external_browser,
    }


@router.get("/app-callback")
def kakao_app_callback_bridge(request: Request):
    """
    모바일 앱(Expo iOS/Android) 시스템 인증 창에서 카카오 인가 후 복귀하는 https:// 브릿지.
    카카오가 전달한 code/state/error 파라미터를 그대로 앱 전용 스킴(whatbob://...)으로 302 중계합니다.
    """
    base_deep_link = settings.APP_DEEP_LINK_CALLBACK_URI.strip()
    query_string = str(request.url.query or "").strip()
    target_url = f"{base_deep_link}?{query_string}" if query_string else base_deep_link
    redirect_resp = RedirectResponse(url=target_url, status_code=302)
    prevent_token_caching(redirect_resp)
    return redirect_resp


@router.get("/callback", response_model=TokenResponse)
async def kakao_callback_test(request: Request, response: Response,
                              code: str = Query(..., description="카카오 인가 코드"), db: Session = Depends(get_db)):
    """
    기존 백엔드 콜백 테스트용 경로입니다.
    프론트 콜백 구성에서는 이 경로 대신 프론트가 POST /login으로 코드를 전달합니다.
    이 경로를 직접 사용하려면 Redirect URI 설정도 이 주소로 맞춰야 합니다.
    """
    require_standalone_kakao_login(request)
    prevent_token_caching(response)
    return await process_kakao_login(code, db)


@router.post("/login", response_model=TokenResponse)
async def kakao_login(request: KakaoLoginRequest, http_request: Request, response: Response,
                      db: Session = Depends(get_db)):
    """
    [프론트엔드 연동용 엔드포인트]
    프론트엔드에서 카카오 인가 코드를 받아 JSON 본문으로 전달할 때 사용합니다.
    """
    require_standalone_kakao_login(http_request)
    prevent_token_caching(response)
    return await process_kakao_login(request.code, db)


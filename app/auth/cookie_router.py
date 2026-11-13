from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.auth.errors import AuthError
from app.auth.schemas import PreparationResponse, TokenResponse
from app.auth.sessions import (
    PREPARATION_COOKIE_SECONDS, SESSION_SECONDS, issue_cookie_token, prepare_authentication,
)
from app.core.config import settings
from app.core.database import get_db


def allowed_web_origins() -> list[str]:
    return list(dict.fromkeys([
        settings.FRONTEND_BASE_URL.rstrip("/"), "http://localhost:8081", "http://127.0.0.1:8081",
    ]))


def require_csrf(request: Request) -> None:
    if (request.headers.get("origin") not in allowed_web_origins()
            or request.headers.get("x-whatbob-request") != "1"):
        raise AuthError("CSRF_REJECTED", "허용되지 않은 인증 요청입니다.", 403)


def set_auth_cookie(response: Response, secret: str, max_age: int) -> None:
    response.set_cookie(settings.AUTH_COOKIE_NAME, secret, max_age=max_age,
                        expires=datetime.now(timezone.utc) + timedelta(seconds=max_age),
                        path="/api/auth", secure=True, httponly=True, samesite="none")


router = APIRouter(prefix="/api/auth", tags=["Auth"], dependencies=[Depends(require_csrf)])


@router.post("/preparations", response_model=PreparationResponse)
def prepare_authentication_endpoint(request: Request, response: Response, db: Session = Depends(get_db)):
    result, secret = prepare_authentication(db, request.cookies.get(settings.AUTH_COOKIE_NAME))
    response.headers["Cache-Control"] = "no-store"
    if secret is not None:
        set_auth_cookie(response, secret, PREPARATION_COOKIE_SECONDS)
    return result


@router.post("/anonymous-users", response_model=TokenResponse)
def create_anonymous_user_endpoint(request: Request, response: Response, db: Session = Depends(get_db)):
    return _issue_token(request, response, db, create_user=True)


@router.post("/tokens", response_model=TokenResponse)
def issue_token_endpoint(request: Request, response: Response, db: Session = Depends(get_db)):
    return _issue_token(request, response, db)


def _issue_token(request: Request, response: Response, db: Session, *, create_user: bool = False):
    secret = request.cookies.get(settings.AUTH_COOKIE_NAME)
    expected_context = request.headers.get("x-whatbob-auth-context") if not create_user else None
    result = issue_cookie_token(db, secret, create_user=create_user, expected_context=expected_context)
    response.status_code = 201 if result.is_new_user else 200
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    set_auth_cookie(response, secret, SESSION_SECONDS)
    return result

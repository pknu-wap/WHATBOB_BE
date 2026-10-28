from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.auth.schemas import KakaoLoginRequest, TokenResponse
from app.auth.service import process_kakao_login
from app.core.config import settings
from app.core.database import get_db

router = APIRouter(prefix="/api/auth/kakao", tags=["Auth"])


@router.get("/login/url")
def get_kakao_login_url(
    state: str = Query(..., min_length=1, description="프론트에서 생성·저장한 로그인 요청 식별값"),
):
    """프론트 콜백으로 돌아오는 로그인 URL을 반환합니다. state 검증은 프론트에서 수행합니다."""
    params = {
        "client_id": settings.KAKAO_REST_API_KEY,
        "redirect_uri": settings.KAKAO_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    url = (
        "https://kauth.kakao.com/oauth/authorize?"
        + urlencode(params)
    )
    return {"login_url": url}


@router.get("/callback", response_model=TokenResponse)
async def kakao_callback_test(code: str = Query(..., description="카카오 인가 코드"), db: Session = Depends(get_db)):
    """
    기존 백엔드 콜백 테스트용 경로입니다.
    프론트 콜백 구성에서는 이 경로 대신 프론트가 POST /login으로 코드를 전달합니다.
    이 경로를 직접 사용하려면 Redirect URI 설정도 이 주소로 맞춰야 합니다.
    """
    return await process_kakao_login(code, db)


@router.post("/login", response_model=TokenResponse)
async def kakao_login(request: KakaoLoginRequest, db: Session = Depends(get_db)):
    """
    [프론트엔드 연동용 엔드포인트]
    프론트엔드에서 카카오 인가 코드를 받아 JSON 본문으로 전달할 때 사용합니다.
    """
    return await process_kakao_login(request.code, db)


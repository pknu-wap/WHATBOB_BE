from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.auth.schemas import KakaoLoginRequest, TokenResponse
from app.auth.service import process_kakao_login
from app.core.config import settings
from app.core.database import get_db

router = APIRouter(prefix="/api/auth/kakao", tags=["Auth"])


@router.get("/login/url")
def get_kakao_login_url():
    """브라우저에서 테스트할 수 있도록 카카오 로그인 시작 링크를 반환합니다."""
    url = (
        f"https://kauth.kakao.com/oauth/authorize"
        f"?client_id={settings.KAKAO_REST_API_KEY}"
        f"&redirect_uri={settings.KAKAO_REDIRECT_URI}"
        f"&response_type=code"
    )
    return {"login_url": url}


@router.get("/callback", response_model=TokenResponse)
async def kakao_callback_test(code: str = Query(..., description="카카오 인가 코드"), db: Session = Depends(get_db)):
    """
    [로컬 브라우저 테스트용 콜백 엔드포인트]
    카카오 로그인을 마치면 카카오가 이 주소로 인가 코드(?code=...)를 보내옵니다.
    """
    return await process_kakao_login(code, db)


@router.post("/login", response_model=TokenResponse)
async def kakao_login(request: KakaoLoginRequest, db: Session = Depends(get_db)):
    """
    [프론트엔드 연동용 엔드포인트]
    프론트엔드에서 카카오 인가 코드를 받아 JSON 본문으로 전달할 때 사용합니다.
    """
    return await process_kakao_login(request.code, db)


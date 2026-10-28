import httpx
from fastapi import HTTPException, status
from app.core.config import settings

KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_USER_INFO_URL = "https://kapi.kakao.com/v2/user/me"


async def get_kakao_access_token(code: str) -> str:
    """카카오 인가 코드를 받아 카카오 Access Token을 요청합니다."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"
    }
    data = {
        "grant_type": "authorization_code",
        "client_id": settings.KAKAO_REST_API_KEY.strip(),
        "redirect_uri": settings.KAKAO_REDIRECT_URI.strip(),
        "code": code.strip(),
    }
    if settings.KAKAO_CLIENT_SECRET:
        data["client_secret"] = settings.KAKAO_CLIENT_SECRET.strip()

    async with httpx.AsyncClient() as client:
        response = await client.post(KAKAO_TOKEN_URL, headers=headers, data=data)
        if response.status_code != 200:
            error_data = response.json()
            print(f"[카카오 토큰 에러 응답]: {error_data}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"카카오 토큰 발급 실패: {error_data.get('error_description', error_data)}"
            )
        token_json = response.json()
        return token_json.get("access_token")


async def get_kakao_user_info(access_token: str) -> dict:
    """카카오 Access Token으로 사용자 프로필 정보를 조회합니다."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(KAKAO_USER_INFO_URL, headers=headers)
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="카카오 사용자 정보 조회에 실패했습니다."
            )
        user_info = response.json()
        
        kakao_id = user_info.get("id")
        kakao_account = user_info.get("kakao_account", {})
        profile = kakao_account.get("profile", {})

        return {
            "kakao_id": kakao_id,
            "nickname": profile.get("nickname"),
            "profile_image": profile.get("profile_image_url"),
            "email": kakao_account.get("email"),
        }


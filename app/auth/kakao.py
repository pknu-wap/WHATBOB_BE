import httpx
from fastapi import HTTPException, status
from app.core.config import settings

KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_USER_INFO_URL = "https://kapi.kakao.com/v2/user/me"
KAKAO_LOGOUT_URL = "https://kapi.kakao.com/v1/user/logout"


class KakaoTransientError(Exception):
    """재시도 가능한 카카오 네트워크/서버 일시 장애"""


class KakaoPermanentError(Exception):
    """재시도로 해결할 수 없는 카카오 토큰 영구 오류"""


class KakaoTokenExpiredError(Exception):
    """카카오 액세스 토큰 만료(401)로 리프레시 토큰 갱신이 필요한 상태"""


async def exchange_kakao_authorization_code(
    code: str,
    *,
    redirect_uri: str | None = None,
) -> dict:
    """카카오 인가 코드를 교환해 토큰 쌍과 만료 시간(초)을 반환합니다."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"
    }
    effective_redirect_uri = (redirect_uri or settings.KAKAO_REDIRECT_URI).strip()
    data = {
        "grant_type": "authorization_code",
        "client_id": settings.KAKAO_REST_API_KEY.strip(),
        "redirect_uri": effective_redirect_uri,
        "code": code.strip(),
    }
    if settings.KAKAO_CLIENT_SECRET:
        data["client_secret"] = settings.KAKAO_CLIENT_SECRET.strip()

    async with httpx.AsyncClient() as client:
        response = await client.post(KAKAO_TOKEN_URL, headers=headers, data=data)
        if response.status_code != 200:
            try:
                error_data = response.json()
                error_desc = error_data.get("error_description") or error_data.get("error") or "token_exchange_failed"
            except Exception:
                error_desc = "token_exchange_failed"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"카카오 토큰 발급 실패: {error_desc}",
            )
        token_json = response.json()
        return {
            "access_token": token_json.get("access_token"),
            "refresh_token": token_json.get("refresh_token"),
            "expires_in": token_json.get("expires_in"),
            "refresh_token_expires_in": token_json.get("refresh_token_expires_in"),
        }


async def get_kakao_access_token(code: str) -> str:
    """카카오 인가 코드를 받아 카카오 Access Token을 요청합니다."""
    token_bundle = await exchange_kakao_authorization_code(code)
    return token_bundle.get("access_token")


async def get_kakao_user_info(access_token: str) -> dict:
    """카카오 Access Token으로 사용자 프로필 정보를 조회합니다."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            KAKAO_USER_INFO_URL,
            headers=headers,
            params={"secure_resource": "true"},
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="카카오 사용자 정보 조회에 실패했습니다."
            )
        user_info = response.json()
        
        kakao_id = user_info.get("id")
        if not isinstance(kakao_id, int) or isinstance(kakao_id, bool) or kakao_id <= 0:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="카카오 사용자 식별 정보를 확인할 수 없습니다.",
            )

        kakao_account = user_info.get("kakao_account") or {}
        profile = kakao_account.get("profile") or {}
        properties = user_info.get("properties") or {}

        return {
            "kakao_id": kakao_id,
            "nickname": profile.get("nickname") or properties.get("nickname"),
            "profile_image": (
                profile.get("profile_image_url")
                or profile.get("thumbnail_image_url")
                or properties.get("profile_image")
                or properties.get("thumbnail_image")
            ),
            "email": kakao_account.get("email"),
        }


async def refresh_kakao_tokens(refresh_token: str, *, timeout: float = 2.0) -> dict:
    """카카오 리프레시 토큰으로 액세스 토큰(및 선택적 회전 리프레시 토큰)을 갱신합니다."""
    cleaned_refresh = (refresh_token or "").strip()
    if not cleaned_refresh:
        raise KakaoPermanentError("missing_refresh_token")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    data = {
        "grant_type": "refresh_token",
        "client_id": settings.KAKAO_REST_API_KEY.strip(),
        "refresh_token": cleaned_refresh,
    }
    if settings.KAKAO_CLIENT_SECRET:
        data["client_secret"] = settings.KAKAO_CLIENT_SECRET.strip()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(KAKAO_TOKEN_URL, headers=headers, data=data)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise KakaoTransientError("refresh_network_error") from exc

    if response.status_code == 200:
        try:
            token_json = response.json()
        except Exception as exc:
            raise KakaoTransientError("invalid_refresh_json") from exc
        new_access = token_json.get("access_token")
        if not new_access or not str(new_access).strip():
            raise KakaoPermanentError("missing_access_token_in_refresh")
        return {
            "access_token": str(new_access).strip(),
            "expires_in": token_json.get("expires_in"),
            "refresh_token": token_json.get("refresh_token"),
            "refresh_token_expires_in": token_json.get("refresh_token_expires_in"),
        }

    if response.status_code == 429 or response.status_code >= 500:
        raise KakaoTransientError(f"refresh_http_{response.status_code}")

    raise KakaoPermanentError(f"refresh_http_{response.status_code}")


async def logout_kakao_access_token(access_token: str, *, timeout: float = 2.0) -> None:
    """카카오 POST /v1/user/logout을 호출해 해당 세션의 토큰 쌍만 폐기합니다."""
    cleaned_access = (access_token or "").strip()
    if not cleaned_access:
        raise KakaoPermanentError("missing_access_token")

    headers = {
        "Authorization": f"Bearer {cleaned_access}",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(KAKAO_LOGOUT_URL, headers=headers)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise KakaoTransientError("logout_network_error") from exc

    if response.status_code == 200:
        return
    if response.status_code == 401:
        raise KakaoTokenExpiredError("logout_http_401")
    if response.status_code == 429 or response.status_code >= 500:
        raise KakaoTransientError(f"logout_http_{response.status_code}")

    raise KakaoPermanentError(f"logout_http_{response.status_code}")

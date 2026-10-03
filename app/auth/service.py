from sqlalchemy.orm import Session
from app.auth.kakao import get_kakao_access_token, get_kakao_user_info
from app.auth.schemas import TokenResponse, UserResponse
from app.core.security import create_access_token
from app.users.models import User


async def process_kakao_login(code: str, db: Session) -> TokenResponse:
    """
    1. 카카오 토큰 발급
    2. 카카오 사용자 정보 조회
    3. DB 조회 (신규 회원이면 자동 회원가입, 기존 회원이면 로그인)
    4. 자체 서비스 JWT 발급
    """
    # 1. 인가 코드로 카카오 액세스 토큰 받기
    kakao_access_token = await get_kakao_access_token(code)

    # 2. 카카오 액세스 토큰으로 사용자 정보 조회
    kakao_profile = await get_kakao_user_info(kakao_access_token)
    kakao_id = kakao_profile["kakao_id"]

    # 3. DB 조회 및 회원가입/로그인 분기 처리
    user = db.query(User).filter(User.kakao_id == kakao_id).first()
    is_new_user = False

    if not user:
        # [신규 회원 -> 회원가입]
        user = User(
            kakao_id=kakao_id,
            nickname=kakao_profile["nickname"],
            email=kakao_profile["email"],
            profile_image=kakao_profile["profile_image"],
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        is_new_user = True
    else:
        # [기존 회원 -> 정보 동기화 및 로그인]
        user.nickname = kakao_profile["nickname"] or user.nickname
        user.profile_image = kakao_profile["profile_image"] or user.profile_image
        if kakao_profile["email"]:
            user.email = kakao_profile["email"]
        db.commit()
        db.refresh(user)

    # 4. 서비스 자체 JWT 발급
    jwt_payload = {
        "sub": str(user.id),
        "kakao_id": user.kakao_id,
    }
    jwt_token = create_access_token(data=jwt_payload)

    return TokenResponse(
        access_token=jwt_token,
        token_type="bearer",
        user=UserResponse.model_validate(user),
        is_new_user=is_new_user,
    )


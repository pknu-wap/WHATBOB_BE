from sqlalchemy.orm import Session
from datetime import timedelta
from app.auth.errors import database_errors
from app.auth.sessions import new_session, token_response, create_uuid_user, utc_now, SESSION_SECONDS
from app.auth.kakao import get_kakao_access_token, get_kakao_user_info
from app.auth.schemas import TokenResponse
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

    with database_errors(db):
        record, _ = new_session()
        db.add(record)
        db.flush()
        user = db.query(User).filter(User.kakao_id == kakao_id).first()
        is_new_user = user is None
        if user is None:
            nickname = kakao_profile["nickname"]
            key = nickname.strip().encode("utf-8") if nickname and nickname.strip() else None
            if key is not None and db.query(User.id).filter(User.nickname_key == key).first():
                user = create_uuid_user(db)
                user.kakao_id = kakao_id
                user.email = kakao_profile["email"]
                user.profile_image = kakao_profile["profile_image"]
            else:
                user = User(kakao_id=kakao_id, nickname=nickname, nickname_key=key,
                            email=kakao_profile["email"], profile_image=kakao_profile["profile_image"])
                db.add(user)
            db.flush()
        # A provider login must not replace an existing WhatBap profile.
        record.user_id = user.id
        record.expires_at = utc_now() + timedelta(seconds=SESSION_SECONDS)
        result = token_response(user, record, is_new_user=is_new_user)
        db.commit()
        return result


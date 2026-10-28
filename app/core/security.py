from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from app.core.config import settings

ALGORITHM = "HS256"


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """우리 서비스 전용 JWT Access Token을 생성합니다."""
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
        
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> dict:
    """JWT 토큰을 검증하고 페이로드를 반환합니다."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])


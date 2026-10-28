from typing import Optional
from pydantic import BaseModel


class KakaoLoginRequest(BaseModel):
    code: str


class UserResponse(BaseModel):
    id: int
    kakao_id: int
    nickname: Optional[str] = None
    email: Optional[str] = None
    profile_image: Optional[str] = None

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
    is_new_user: bool


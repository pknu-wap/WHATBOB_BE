from pydantic import BaseModel
from app.users.schemas import UserResponse


class KakaoLoginRequest(BaseModel):
    code: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
    is_new_user: bool


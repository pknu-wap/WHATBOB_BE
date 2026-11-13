from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from app.users.schemas import UserResponse


class KakaoLoginRequest(BaseModel):
    code: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600
    user: UserResponse
    is_new_user: bool


class PreparationResponse(BaseModel):
    next_action: Literal["CREATE_USER", "ISSUE_TOKEN"]
    notice: Literal["PREVIOUS_SESSION_UNAVAILABLE"] | None = None


class TransitionPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["KAKAO_LINK", "LOGOUT"]
    code: str | None = Field(default=None, description="KAKAO_LINK 시 카카오 인가 코드")
    state: str | None = Field(default=None, description="프론트 OAuth state 값")
    expected_state: str | None = Field(default=None, description="기대하는 OAuth state 값 (전달 시 일치 검증)")
    operation_key: str | None = Field(default=None, description="멱등 전환 작업 키 (UUIDv4)")
    transition_id: str | None = Field(default=None, description="준비 응답 유실 시 기존 PREPARED 작업 재준비 ID")


class TransitionPrepareResponse(BaseModel):
    transition_id: str
    action: Literal["KAKAO_LINK", "LOGOUT"]
    status: Literal["PREPARED"] = "PREPARED"
    expires_at: datetime


class TransitionCommitResponse(BaseModel):
    transition_id: str
    action: Literal["KAKAO_LINK", "LOGOUT"]
    status: Literal["COMMITTED"] = "COMMITTED"
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600
    user: UserResponse
    is_new_user: bool


class TransitionStatusResponse(BaseModel):
    transition_id: str
    action: Literal["KAKAO_LINK", "LOGOUT"]
    status: str
    origin_uuid: str
    result_uuid: str | None = None
    expires_at: datetime


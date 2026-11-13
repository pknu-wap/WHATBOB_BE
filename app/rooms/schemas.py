from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RoomCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_members: int = Field(default=8, ge=2, le=100)


class RoomCreateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    room_id: str
    join_url: str
    host_user_id: int
    status: str
    max_members: int
    created_at: datetime
    expires_at: datetime


class RoomJoinResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    room_id: str
    user_id: int
    role: str
    room_status: str
    member_count: int
    max_members: int
    joined_at: datetime


class RoomMemberResponse(BaseModel):
    user_id: int
    nickname: str | None
    profile_image: str | None
    role: str
    joined_at: datetime
    is_online: bool
    disconnected_at: datetime | None
    order_sheet_completed: bool
    order_sheet_completed_at: datetime | None


class RoomDetailResponse(BaseModel):
    room_id: str
    join_url: str
    host_user_id: int
    status: str
    max_members: int
    member_count: int
    is_full: bool
    is_member: bool
    my_role: str | None
    created_at: datetime
    expires_at: datetime
    members: list[RoomMemberResponse]
    completed_member_count: int
    all_members_completed: bool


class RoomOrderSheetCompleteResponse(BaseModel):
    room_id: str
    user_id: int
    order_sheet_completed: bool
    order_sheet_completed_at: datetime
    completed_member_count: int
    total_member_count: int
    all_members_completed: bool


class RoomDeleteResponse(BaseModel):
    room_id: str
    deleted: bool
    close_reason: str
    closed_at: datetime


class RoomMemberLeaveResponse(BaseModel):
    room_id: str
    user_id: int
    deleted: bool
    member_count: int
    completed_member_count: int
    all_members_completed: bool


class RoomOperationResponse(BaseModel):
    operation_key: str
    status: str
    scope: str | None = None
    room_id: str | None = None
    room_status: str | None = None
    result: dict[str, Any] | None = None

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RoomCreateRequest(BaseModel):
    max_members: int = Field(default=8, ge=2, le=100)


class RoomCreateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    room_id: str
    join_url: str
    host_user_id: int
    status: str
    max_members: int
    created_at: datetime


class RoomJoinResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    room_id: str
    user_id: int
    role: str
    room_status: str
    member_count: int
    max_members: int
    joined_at: datetime

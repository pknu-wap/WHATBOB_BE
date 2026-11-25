from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.recommend.schemas import AnswerItemRequest, OrderSheetQuestionDTO


class RoomCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_members: int = Field(default=8, ge=2, le=100)


class RoomCreateResponse(BaseModel):
    common_state: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)

    room_id: str
    join_url: str
    host_user_id: int
    status: str
    max_members: int
    created_at: datetime
    expires_at: datetime


class RoomJoinResponse(BaseModel):
    common_state: dict[str, Any] | None = None

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
    state_version: int = 0
    common_state: dict[str, Any] | None = None
    viewer_state: dict[str, Any] | None = None
    current_round: dict[str, Any] | None = None

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
    common_state: dict[str, Any] | None = None

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


class RoomOrderSheetSubmitRequest(BaseModel):
    """D20: Group order-sheet submission accepts only answers (and optional round fences); extra_exclusions/exclusions are forbidden."""
    model_config = ConfigDict(extra="forbid")

    answers: list[AnswerItemRequest] = Field(..., min_length=1)
    round_id: int | None = None
    round_no: int | None = None


class RoomOrderSheetNoResultsRequest(RoomOrderSheetSubmitRequest):
    round_id: int


class RoomVoteSubmitRequest(BaseModel):
    """T04 / T06: Multiple-choice vote submission with vote_round_no epoch fence."""
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[int]
    vote_round_no: int | None = None
    round_id: int | None = None
    round_no: int | None = None


class RoomDepartureDecisionRequest(BaseModel):
    """T05 / T06: Host CONTINUE/CANCEL decision with optimistic decision_version token."""
    model_config = ConfigDict(extra="forbid")

    action: str
    decision_version: int = Field(..., ge=1)
    round_id: int | None = None


class RoomRoundQuestionsResponse(BaseModel):
    room_id: str
    round_id: int
    round_no: int
    round_status: str
    questions: list[OrderSheetQuestionDTO]
    total_count: int
    exploitation_count: int
    exploration_count: int
    has_submitted: bool = False
    submitted_at: datetime | None = None


class RoomRoundCommandResponse(BaseModel):
    room_id: str
    round_id: int
    round_no: int
    status: str
    room_status: str
    vote_round_no: int
    decision_version: int
    cancel_reason: str | None = None
    common_state: dict[str, Any] | None = None
    viewer_state: dict[str, Any] | None = None


class RoomRoundOrderSheetSubmitResponse(BaseModel):
    room_id: str
    round_id: int
    round_no: int
    user_id: int
    round_status: str
    submitted: bool
    submitted_at: datetime | None = None
    recommendation_id: int | None = None
    submitted_member_count: int
    active_member_count: int
    all_orders_submitted: bool
    common_state: dict[str, Any] | None = None
    viewer_state: dict[str, Any] | None = None


class RoomRoundVoteSubmitResponse(BaseModel):
    room_id: str
    round_id: int
    round_no: int
    vote_round_no: int
    user_id: int
    round_status: str
    voted: bool
    voted_at: datetime | None = None
    voted_member_count: int
    active_member_count: int
    all_votes_submitted: bool
    common_state: dict[str, Any] | None = None
    viewer_state: dict[str, Any] | None = None


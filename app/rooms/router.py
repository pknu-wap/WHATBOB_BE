from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomCreateResponse,
    RoomDeleteResponse,
    RoomDetailResponse,
    RoomJoinResponse,
    RoomMemberLeaveResponse,
    RoomOrderSheetCompleteResponse,
)
from app.rooms.service import (
    close_room_by_host,
    complete_room_order_sheet,
    create_room,
    delete_closed_room,
    get_room,
    join_room,
    leave_room_member,
)
from app.rooms.websocket import room_connection_manager
from app.users.models import User

router = APIRouter(prefix="/api/rooms", tags=["Rooms"])


async def _notify_and_delete_closed_room(
    db: Session,
    room_id: str,
    close_reason: str,
    closed_at: datetime,
) -> None:
    await room_connection_manager.broadcast(
        room_id,
        {
            "type": "room.closed",
            "room_id": room_id,
            "reason": close_reason,
            "closed_at": closed_at.isoformat(),
        },
    )
    await room_connection_manager.close_room_connections(room_id)
    await run_in_threadpool(delete_closed_room, db, room_id)


@router.post(
    "",
    response_model=RoomCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_room_endpoint(
    request: RoomCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return create_room(db=db, host=current_user, request=request)


@router.post(
    "/{room_id}/join",
    response_model=RoomJoinResponse,
    status_code=status.HTTP_201_CREATED,
)
async def join_room_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response = await run_in_threadpool(
        join_room,
        db,
        room_id,
        current_user,
    )
    await room_connection_manager.broadcast(
        room_id,
        {
            "type": "room.updated",
            "room_id": room_id,
            "reason": "MEMBER_JOINED",
            "user_id": current_user.id,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return response


@router.get(
    "/{room_id}",
    response_model=RoomDetailResponse,
)
def get_room_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_room(db=db, room_public_id=room_id, user=current_user)


@router.delete(
    "/{room_id}",
    response_model=RoomDeleteResponse,
)
async def delete_room_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    closed_room = await run_in_threadpool(
        close_room_by_host,
        db,
        room_id,
        current_user,
    )
    await _notify_and_delete_closed_room(
        db=db,
        room_id=closed_room.room_id,
        close_reason=closed_room.reason,
        closed_at=closed_room.closed_at,
    )
    return RoomDeleteResponse(
        room_id=closed_room.room_id,
        deleted=True,
        close_reason=closed_room.reason,
        closed_at=closed_room.closed_at,
    )


@router.delete(
    "/{room_id}/members/{user_id}",
    response_model=RoomMemberLeaveResponse,
)
async def leave_room_member_endpoint(
    room_id: str,
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response = await run_in_threadpool(
        leave_room_member,
        db,
        room_id,
        user_id,
        current_user,
    )
    room_connection_manager.cancel_member_disconnect(room_id, user_id)
    await room_connection_manager.broadcast(
        room_id,
        {
            "type": "room.updated",
            "room_id": room_id,
            "reason": "MEMBER_LEFT",
            "user_id": user_id,
            "member_count": response.member_count,
            "completed_member_count": response.completed_member_count,
            "all_members_completed": response.all_members_completed,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await room_connection_manager.close_user_connections(room_id, user_id)
    return response


@router.post(
    "/{room_id}/order-sheet/complete",
    response_model=RoomOrderSheetCompleteResponse,
)
async def complete_room_order_sheet_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response, completion_changed = await run_in_threadpool(
        complete_room_order_sheet,
        db,
        room_id,
        current_user,
    )
    if completion_changed:
        await room_connection_manager.broadcast(
            room_id,
            {
                "type": "room.updated",
                "room_id": room_id,
                "reason": "ORDER_SHEET_COMPLETED",
                "user_id": current_user.id,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    return response

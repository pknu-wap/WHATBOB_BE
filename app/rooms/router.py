from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomCreateResponse,
    RoomDetailResponse,
    RoomJoinResponse,
    RoomOrderSheetCompleteResponse,
)
from app.rooms.service import (
    complete_room_order_sheet,
    create_room,
    get_room,
    join_room,
)
from app.rooms.websocket import room_connection_manager
from app.users.models import User

router = APIRouter(prefix="/api/rooms", tags=["Rooms"])


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

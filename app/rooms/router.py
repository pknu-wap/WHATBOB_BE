from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomCreateResponse,
    RoomJoinResponse,
)
from app.rooms.service import create_room, join_room
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
def join_room_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return join_room(db=db, room_public_id=room_id, user=current_user)

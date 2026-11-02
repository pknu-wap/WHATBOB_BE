from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.rooms.schemas import RoomCreateRequest, RoomCreateResponse
from app.rooms.service import create_room
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

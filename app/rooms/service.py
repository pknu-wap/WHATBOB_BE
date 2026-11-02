from sqlalchemy.orm import Session

from app.core.config import settings
from app.rooms.models import Room, RoomMember
from app.rooms.schemas import RoomCreateRequest, RoomCreateResponse
from app.users.models import User


def create_room(
    db: Session,
    host: User,
    request: RoomCreateRequest,
) -> RoomCreateResponse:
    """Create a room and register its creator as the host atomically."""
    try:
        room = Room(
            host_user_id=host.id,
            status="WAITING",
            max_members=request.max_members,
        )
        db.add(room)
        db.flush()

        db.add(
            RoomMember(
                room_id=room.id,
                user_id=host.id,
                role="HOST",
            )
        )
        db.commit()
        db.refresh(room)
    except Exception:
        db.rollback()
        raise

    frontend_base_url = settings.FRONTEND_BASE_URL.rstrip("/")
    return RoomCreateResponse(
        room_id=room.public_id,
        join_url=f"{frontend_base_url}/rooms/{room.public_id}/join",
        host_user_id=room.host_user_id,
        status=room.status,
        max_members=room.max_members,
        created_at=room.created_at,
    )

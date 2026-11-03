from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.rooms.models import Room, RoomMember
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomCreateResponse,
    RoomJoinResponse,
)
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


def join_room(
    db: Session,
    room_public_id: str,
    user: User,
) -> RoomJoinResponse:
    """Register an authenticated user as a room member atomically."""
    try:
        # Lock the room so concurrent joins cannot exceed its capacity.
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .with_for_update()
            .one_or_none()
        )
        if room is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found",
            )

        if room.status != "WAITING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Room is not accepting members",
            )

        existing_member = (
            db.query(RoomMember)
            .filter(
                RoomMember.room_id == room.id,
                RoomMember.user_id == user.id,
            )
            .one_or_none()
        )
        if existing_member is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a room member",
            )

        member_count = (
            db.query(func.count(RoomMember.id))
            .filter(RoomMember.room_id == room.id)
            .scalar()
        )
        if member_count >= room.max_members:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Room is full",
            )

        member = RoomMember(
            room_id=room.id,
            user_id=user.id,
            role="MEMBER",
        )
        db.add(member)
        db.commit()
        db.refresh(member)
    except Exception:
        db.rollback()
        raise

    return RoomJoinResponse(
        room_id=room.public_id,
        user_id=member.user_id,
        role=member.role,
        room_status=room.status,
        member_count=member_count + 1,
        max_members=room.max_members,
        joined_at=member.joined_at,
    )

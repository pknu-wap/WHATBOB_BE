from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.rooms.models import Room, RoomMember
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomCreateResponse,
    RoomDetailResponse,
    RoomJoinResponse,
    RoomMemberResponse,
)
from app.users.models import User


def build_join_url(room_public_id: str) -> str:
    frontend_base_url = settings.FRONTEND_BASE_URL.rstrip("/")
    return f"{frontend_base_url}/rooms/{room_public_id}/join"


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

    return RoomCreateResponse(
        room_id=room.public_id,
        join_url=build_join_url(room.public_id),
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

        member_rows = (
            db.query(RoomMember.user_id)
            .filter(RoomMember.room_id == room.id)
            .with_for_update()
            .all()
        )
        member_user_ids = {row[0] for row in member_rows}
        if user.id in member_user_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a room member",
            )

        member_count = len(member_rows)
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


def get_room(
    db: Session,
    room_public_id: str,
    user: User,
) -> RoomDetailResponse:
    """Return room state and member details visible to the current user."""
    room = (
        db.query(Room)
        .filter(Room.public_id == room_public_id)
        .one_or_none()
    )
    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    member_rows = (
        db.query(RoomMember, User)
        .join(User, User.id == RoomMember.user_id)
        .filter(RoomMember.room_id == room.id)
        .order_by(RoomMember.joined_at, RoomMember.id)
        .all()
    )
    current_member = next(
        (
            room_member
            for room_member, member_user in member_rows
            if member_user.id == user.id
        ),
        None,
    )
    is_member = current_member is not None

    members = []
    if is_member:
        members = [
            RoomMemberResponse(
                user_id=member_user.id,
                nickname=member_user.nickname,
                profile_image=member_user.profile_image,
                role=room_member.role,
                joined_at=room_member.joined_at,
            )
            for room_member, member_user in member_rows
        ]

    member_count = len(member_rows)
    return RoomDetailResponse(
        room_id=room.public_id,
        join_url=build_join_url(room.public_id),
        host_user_id=room.host_user_id,
        status=room.status,
        max_members=room.max_members,
        member_count=member_count,
        is_full=member_count >= room.max_members,
        is_member=is_member,
        my_role=current_member.role if current_member else None,
        created_at=room.created_at,
        members=members,
    )

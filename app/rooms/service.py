from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomCreateResponse,
    RoomDetailResponse,
    RoomJoinResponse,
    RoomMemberResponse,
    RoomOrderSheetCompleteResponse,
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
        db.query(RoomMember, User, RoomMemberOrderSheet)
        .join(User, User.id == RoomMember.user_id)
        .outerjoin(
            RoomMemberOrderSheet,
            RoomMemberOrderSheet.room_member_id == RoomMember.id,
        )
        .filter(RoomMember.room_id == room.id)
        .order_by(RoomMember.joined_at, RoomMember.id)
        .all()
    )
    current_member = next(
        (
            room_member
            for room_member, member_user, _ in member_rows
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
                order_sheet_completed=bool(
                    order_sheet and order_sheet.is_completed
                ),
                order_sheet_completed_at=(
                    order_sheet.completed_at if order_sheet else None
                ),
            )
            for room_member, member_user, order_sheet in member_rows
        ]

    member_count = len(member_rows)
    completed_member_count = sum(
        1
        for _, _, order_sheet in member_rows
        if order_sheet and order_sheet.is_completed
    )
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
        completed_member_count=completed_member_count,
        all_members_completed=(
            member_count > 0
            and completed_member_count == member_count
        ),
    )


def complete_room_order_sheet(
    db: Session,
    room_public_id: str,
    user: User,
) -> tuple[RoomOrderSheetCompleteResponse, bool]:
    """Mark the current room member's order sheet as completed."""
    try:
        # Joining and completing both lock the room, keeping member and
        # completion counts consistent under concurrent requests.
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
        if room.status == "CLOSED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Room is closed",
            )

        member_rows = (
            db.query(RoomMember.id, RoomMember.user_id)
            .filter(RoomMember.room_id == room.id)
            .with_for_update()
            .all()
        )
        current_member_id = next(
            (
                room_member_id
                for room_member_id, member_user_id in member_rows
                if member_user_id == user.id
            ),
            None,
        )
        if current_member_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only room members can complete an order sheet",
            )

        room_member_ids = [row[0] for row in member_rows]
        order_sheet_rows = (
            db.query(RoomMemberOrderSheet)
            .filter(
                RoomMemberOrderSheet.room_member_id.in_(room_member_ids)
            )
            .with_for_update()
            .all()
        )
        current_order_sheet = next(
            (
                order_sheet
                for order_sheet in order_sheet_rows
                if order_sheet.room_member_id == current_member_id
            ),
            None,
        )
        if current_order_sheet is None:
            current_order_sheet = RoomMemberOrderSheet(
                room_member_id=current_member_id,
            )
            db.add(current_order_sheet)
            order_sheet_rows.append(current_order_sheet)

        completion_changed = not current_order_sheet.is_completed
        if completion_changed:
            current_order_sheet.is_completed = True
            current_order_sheet.completed_at = datetime.now(timezone.utc)

        completed_member_count = sum(
            1 for order_sheet in order_sheet_rows if order_sheet.is_completed
        )
        total_member_count = len(member_rows)
        db.commit()
        db.refresh(current_order_sheet)
    except Exception:
        db.rollback()
        raise

    return (
        RoomOrderSheetCompleteResponse(
            room_id=room.public_id,
            user_id=user.id,
            order_sheet_completed=current_order_sheet.is_completed,
            order_sheet_completed_at=current_order_sheet.completed_at,
            completed_member_count=completed_member_count,
            total_member_count=total_member_count,
            all_members_completed=(
                total_member_count > 0
                and completed_member_count == total_member_count
            ),
        ),
        completion_changed,
    )

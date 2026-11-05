from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


ROOM_CLOSE_REASON_HOST_LEFT = "HOST_LEFT"
ROOM_CLOSE_REASON_HOST_DISCONNECTED = "HOST_DISCONNECTED"
ROOM_CLOSE_REASON_EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class ClosedRoom:
    room_id: str
    reason: str
    closed_at: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _close_room(room: Room, reason: str, closed_at: datetime) -> ClosedRoom:
    room.status = "CLOSED"
    room.closed_at = closed_at
    room.close_reason = reason
    return ClosedRoom(room_id=room.public_id, reason=reason, closed_at=closed_at)


def _ensure_room_not_expired(db: Session, room: Room) -> None:
    now = utcnow()
    if _as_utc(room.expires_at) > now:
        return
    _close_room(room, ROOM_CLOSE_REASON_EXPIRED, now)
    db.commit()
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Room has expired",
    )


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
        now = utcnow()
        room = Room(
            host_user_id=host.id,
            status="WAITING",
            max_members=request.max_members,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.ROOM_TTL_SECONDS),
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
        expires_at=room.expires_at,
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

        _ensure_room_not_expired(db, room)

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
    _ensure_room_not_expired(db, room)
    if room.status == "CLOSED":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Room is closed",
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
        expires_at=room.expires_at,
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
        _ensure_room_not_expired(db, room)
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


def close_room_by_host(
    db: Session,
    room_public_id: str,
    user: User,
) -> ClosedRoom:
    """Close a room after an explicit request from its host."""
    try:
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
        if room.host_user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the room host can delete the room",
            )

        closed_room = _close_room(
            room,
            ROOM_CLOSE_REASON_HOST_LEFT,
            utcnow(),
        )
        db.commit()
        return closed_room
    except Exception:
        db.rollback()
        raise


def delete_closed_room(db: Session, room_public_id: str) -> bool:
    """Physically delete a closed room and all dependent rows."""
    try:
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .with_for_update()
            .one_or_none()
        )
        if room is None:
            return False
        if room.status != "CLOSED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Room must be closed before deletion",
            )

        member_ids = [
            row[0]
            for row in db.query(RoomMember.id)
            .filter(RoomMember.room_id == room.id)
            .all()
        ]
        if member_ids:
            (
                db.query(RoomMemberOrderSheet)
                .filter(RoomMemberOrderSheet.room_member_id.in_(member_ids))
                .delete(synchronize_session=False)
            )
        (
            db.query(RoomMember)
            .filter(RoomMember.room_id == room.id)
            .delete(synchronize_session=False)
        )
        db.delete(room)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def mark_host_disconnected(
    db: Session,
    room_public_id: str,
    user_id: int,
    disconnected_at: datetime | None = None,
) -> bool:
    """Persist the start of the host reconnect grace period."""
    try:
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .with_for_update()
            .one_or_none()
        )
        if room is None or room.status == "CLOSED" or room.host_user_id != user_id:
            return False
        room.host_disconnected_at = disconnected_at or utcnow()
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def clear_host_disconnected(
    db: Session,
    room_public_id: str,
    user_id: int,
) -> bool:
    """Cancel a pending host disconnect deletion after reconnection."""
    try:
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .with_for_update()
            .one_or_none()
        )
        if room is None or room.host_user_id != user_id:
            return False
        changed = room.host_disconnected_at is not None
        room.host_disconnected_at = None
        db.commit()
        return changed
    except Exception:
        db.rollback()
        raise


def close_room_after_host_disconnect(
    db: Session,
    room_public_id: str,
    user_id: int,
    now: datetime | None = None,
) -> ClosedRoom | None:
    """Close a room only if its host did not reconnect during the grace period."""
    try:
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .with_for_update()
            .one_or_none()
        )
        if (
            room is None
            or room.status == "CLOSED"
            or room.host_user_id != user_id
            or room.host_disconnected_at is None
        ):
            return None

        closed_at = now or utcnow()
        deadline = _as_utc(room.host_disconnected_at) + timedelta(
            seconds=settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS
        )
        if deadline > closed_at:
            return None

        closed_room = _close_room(
            room,
            ROOM_CLOSE_REASON_HOST_DISCONNECTED,
            closed_at,
        )
        db.commit()
        return closed_room
    except Exception:
        db.rollback()
        raise


def close_due_rooms(
    db: Session,
    now: datetime | None = None,
) -> list[ClosedRoom]:
    """Close expired, abandoned, and previously half-cleaned rooms for cleanup."""
    current_time = now or utcnow()
    disconnect_cutoff = current_time - timedelta(
        seconds=settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS
    )
    try:
        rooms = db.query(Room).with_for_update().all()
        closed_rooms: list[ClosedRoom] = []
        for room in rooms:
            if room.status == "CLOSED":
                closed_rooms.append(
                    ClosedRoom(
                        room_id=room.public_id,
                        reason=room.close_reason or ROOM_CLOSE_REASON_EXPIRED,
                        closed_at=room.closed_at or current_time,
                    )
                )
                continue
            if _as_utc(room.expires_at) <= current_time:
                closed_rooms.append(
                    _close_room(room, ROOM_CLOSE_REASON_EXPIRED, current_time)
                )
                continue
            if (
                room.host_disconnected_at is not None
                and _as_utc(room.host_disconnected_at) <= disconnect_cutoff
            ):
                closed_rooms.append(
                    _close_room(
                        room,
                        ROOM_CLOSE_REASON_HOST_DISCONNECTED,
                        current_time,
                    )
                )
        db.commit()
        return closed_rooms
    except Exception:
        db.rollback()
        raise

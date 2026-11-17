from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, DataError, IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.models import RequestOperation
from app.auth.operations import (
    compute_payload_hash,
    get_user_session_id,
    lock_user_and_verify_session,
    normalize_idempotency_key,
)
from app.auth.sessions import utc_now
from app.core.config import settings
from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet
from app.rooms.transactions import (
    room_transaction, commit_room_transaction, current_read_session, is_lock_conflict,
)
from app.rooms.departures import (
    MemberDeparture, lock_room, lock_room_members, lock_order_sheets,
    remove_locked_members,
)
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomCreateResponse,
    RoomDetailResponse,
    RoomJoinResponse,
    RoomMemberLeaveResponse,
    RoomMemberResponse,
    RoomOperationResponse,
    RoomOrderSheetCompleteResponse,
)
from app.users.models import User

ROOM_CLOSE_REASON_HOST_LEFT = "HOST_LEFT"
ROOM_CLOSE_REASON_HOST_DISCONNECTED = "HOST_DISCONNECTED"
ROOM_CLOSE_REASON_EXPIRED = "EXPIRED"
ROOM_OPERATION_SCOPES = ("ROOM_CREATE", "ROOM_JOIN", "ROOM_OPERATION")


@dataclass(frozen=True)
class ClosedRoom:
    room_id: str
    reason: str
    closed_at: datetime


@dataclass(frozen=True)
class LeftRoomMember:
    room_id: str
    user_id: int
    member_count: int = 0
    completed_member_count: int = 0
    all_members_completed: bool = False


OfflineRoomMember = LeftRoomMember


def _departure_events(result: MemberDeparture) -> list[LeftRoomMember]:
    return [LeftRoomMember(result.room_id, uid, result.member_count,
                           result.completed_member_count, result.all_members_completed)
            for uid in result.user_ids]


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


def _apply_lazy_room_deadlines(
    db: Session,
    room: Room,
    *,
    now: datetime | None = None,
    closed_status_code: int = status.HTTP_409_CONFLICT,
) -> list[LeftRoomMember]:
    """
    백그라운드 정리 작업 실행 여부와 무관하게 방 만료, 호스트 60초 유예 만료,
    일반 참가자 60초 유예 만료를 즉시 판정하고 정리한다.
    """
    current_time = now or utcnow()
    if _as_utc(room.expires_at) <= current_time:
        _close_room(room, ROOM_CLOSE_REASON_EXPIRED, current_time)
        commit_room_transaction(db)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Room has expired",
        )

    if room.host_disconnected_at is not None:
        host_deadline = _as_utc(room.host_disconnected_at) + timedelta(
            seconds=settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS
        )
        if host_deadline <= current_time:
            _close_room(room, ROOM_CLOSE_REASON_HOST_DISCONNECTED, current_time)
            commit_room_transaction(db)
            raise HTTPException(
                status_code=closed_status_code,
                detail="Room is closed",
            )

    if room.status == "CLOSED":
        raise HTTPException(
            status_code=closed_status_code,
            detail="Room is closed",
        )

    member_cutoff = current_time - timedelta(
        seconds=settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS
    )
    members = lock_room_members(db, room.id)
    overdue = [m for m in members if m.role == "MEMBER"
               and m.disconnected_at is not None
               and _as_utc(m.disconnected_at) <= member_cutoff]
    return _departure_events(remove_locked_members(db, room, overdue))


def build_join_url(room_public_id: str) -> str:
    frontend_base_url = settings.FRONTEND_BASE_URL.rstrip("/")
    return f"{frontend_base_url}/rooms/{room_public_id}/join"


def _find_room_operation(
    db: Session,
    user_id: int,
    operation_key: str,
    *,
    for_update: bool = False,
) -> RequestOperation | None:
    stmt = select(RequestOperation).where(
        RequestOperation.user_id == user_id,
        RequestOperation.operation_key == operation_key,
        RequestOperation.scope.in_(ROOM_OPERATION_SCOPES),
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.execute(stmt).scalar_one_or_none()


def _acquire_room_operation(
    db: Session,
    *,
    user: User,
    scope: str,
    operation_key: str,
    payload_hash: str,
    room_public_id: str | None = None,
) -> tuple[RequestOperation, bool]:
    existing = _find_room_operation(db, user.id, operation_key, for_update=True)
    if existing is not None:
        return existing, True

    now = utc_now()
    op = RequestOperation(
        scope=scope,
        operation_key=operation_key,
        user_id=user.id,
        auth_session_id=get_user_session_id(user),
        status="PENDING",
        payload_hash=payload_hash,
        room_id=room_public_id,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(op)
            db.flush()
        return op, False
    except IntegrityError:
        existing = _find_room_operation(db, user.id, operation_key, for_update=True)
        if existing is None:
            raise
        return existing, True


@room_transaction(http=True)
def create_room(
    db: Session,
    host: User,
    request: RoomCreateRequest,
    *,
    idempotency_key: str | None = None,
) -> RoomCreateResponse:
    """Create a room and register its creator as the host atomically."""
    try:
        locked_host = lock_user_and_verify_session(db, host)
        try:
            op_key = normalize_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        payload_hash = compute_payload_hash({"max_members": int(request.max_members)})
        operation = None
        if op_key is not None:
            operation, is_existing = _acquire_room_operation(
                db,
                user=locked_host,
                scope="ROOM_CREATE",
                operation_key=op_key,
                payload_hash=payload_hash,
            )
            if is_existing:
                if operation.status == "CANCELLED":
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Operation was cancelled (OPERATION_CANCELLED)",
                    )
                if operation.scope != "ROOM_CREATE" or operation.payload_hash != payload_hash:
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency-Key reused with different payload (IDEMPOTENCY_KEY_REUSED)",
                    )
                if operation.status == "SUCCEEDED" and operation.response_json:
                    db.rollback()
                    return RoomCreateResponse.model_validate(operation.response_json)

        now = utcnow()
        room = Room(
            host_user_id=locked_host.id,
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
                user_id=locked_host.id,
                role="HOST",
            )
        )
        response = RoomCreateResponse(
            room_id=room.public_id,
            join_url=build_join_url(room.public_id),
            host_user_id=room.host_user_id,
            status=room.status,
            max_members=room.max_members,
            created_at=room.created_at,
            expires_at=room.expires_at,
        )
        if operation is not None:
            operation.status = "SUCCEEDED"
            operation.room_id = room.public_id
            operation.resource_id = room.public_id
            operation.response_status = status.HTTP_201_CREATED
            operation.response_json = response.model_dump(mode="json")
            operation.updated_at = utc_now()

        commit_room_transaction(db)
        return response
    except Exception:
        db.rollback()
        raise


@room_transaction(http=True)
def join_room_with_meta(
    db: Session,
    room_public_id: str,
    user: User,
    *,
    idempotency_key: str | None = None,
) -> tuple[RoomJoinResponse, bool, list[LeftRoomMember]]:
    """
    Register an authenticated user as a room member atomically.
    Returns (response, is_idempotent_replay, lazily_removed_members).
    """
    try:
        locked_user = lock_user_and_verify_session(db, user)
        try:
            op_key = normalize_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        payload_hash = compute_payload_hash({"room_id": room_public_id})
        operation = None
        if op_key is not None:
            operation, is_existing = _acquire_room_operation(
                db,
                user=locked_user,
                scope="ROOM_JOIN",
                operation_key=op_key,
                payload_hash=payload_hash,
                room_public_id=room_public_id,
            )
            if is_existing:
                if operation.status == "CANCELLED":
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Operation was cancelled (OPERATION_CANCELLED)",
                    )
                if operation.scope != "ROOM_JOIN" or operation.payload_hash != payload_hash:
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency-Key reused with different payload (IDEMPOTENCY_KEY_REUSED)",
                    )
                if operation.status == "SUCCEEDED" and operation.response_json:
                    db.rollback()
                    return RoomJoinResponse.model_validate(operation.response_json), True, []

        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found",
            )

        left_events = _apply_lazy_room_deadlines(
            db,
            room,
            closed_status_code=status.HTTP_409_CONFLICT,
        )

        if room.status != "WAITING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Room is not accepting members",
            )

        member_rows = (
            db.query(RoomMember.user_id)
            .filter(RoomMember.room_id == room.id)
            .order_by(RoomMember.id)
            .execution_options(populate_existing=True).with_for_update()
            .all()
        )
        member_user_ids = {row[0] for row in member_rows}
        if locked_user.id in member_user_ids:
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
            user_id=locked_user.id,
            role="MEMBER",
        )
        db.add(member)
        db.flush()

        response = RoomJoinResponse(
            room_id=room.public_id,
            user_id=member.user_id,
            role=member.role,
            room_status=room.status,
            member_count=member_count + 1,
            max_members=room.max_members,
            joined_at=member.joined_at,
        )
        if operation is not None:
            operation.status = "SUCCEEDED"
            operation.room_id = room.public_id
            operation.resource_id = str(member.id)
            operation.response_status = status.HTTP_201_CREATED
            operation.response_json = response.model_dump(mode="json")
            operation.updated_at = utc_now()

        commit_room_transaction(db)
        return response, False, left_events
    except Exception:
        db.rollback()
        raise


def join_room(
    db: Session,
    room_public_id: str,
    user: User,
    *,
    idempotency_key: str | None = None,
) -> RoomJoinResponse:
    response, _, _ = join_room_with_meta(
        db=db,
        room_public_id=room_public_id,
        user=user,
        idempotency_key=idempotency_key,
    )
    return response


def _resolve_current_room_status(db: Session, room_public_id: str | None) -> str | None:
    if not room_public_id:
        return None
    room = db.query(Room).filter(Room.public_id == room_public_id).one_or_none()
    if room is None:
        return "DELETED"
    return room.status


def get_room_operation(
    db: Session,
    user: User,
    operation_key: str,
) -> RoomOperationResponse:
    locked_user = lock_user_and_verify_session(db, user)
    try:
        op_key = normalize_idempotency_key(operation_key, required=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    op = _find_room_operation(db, locked_user.id, op_key, for_update=False)
    if op is None:
        return RoomOperationResponse(
            operation_key=op_key,
            status="UNKNOWN",
            scope=None,
            room_id=None,
            room_status=None,
            result=None,
        )
    return RoomOperationResponse(
        operation_key=op.operation_key,
        status=op.status,
        scope=op.scope,
        room_id=op.room_id,
        room_status=_resolve_current_room_status(db, op.room_id),
        result=op.response_json,
    )


@room_transaction(http=True)
def cancel_room_operation(
    db: Session,
    user: User,
    operation_key: str,
) -> RoomOperationResponse:
    """
    진행 중이거나 아직 도착하지 않은 방 생성/참여 요청에 대해 취소 표식(tombstone)을 기록한다.
    이미 성공한 참여/생성은 방을 삭제하거나 퇴장시키지 않고 현재 SUCCEEDED 상태를 그대로 반환한다.
    """
    try:
        locked_user = lock_user_and_verify_session(db, user)
        try:
            op_key = normalize_idempotency_key(operation_key, required=True)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        existing = _find_room_operation(db, locked_user.id, op_key, for_update=True)
        now = utc_now()
        if existing is None:
            tombstone = RequestOperation(
                scope="ROOM_OPERATION",
                operation_key=op_key,
                user_id=locked_user.id,
                auth_session_id=get_user_session_id(locked_user),
                status="CANCELLED",
                payload_hash="CANCELLED",
                room_id=None,
                created_at=now,
                updated_at=now,
            )
            db.add(tombstone)
            commit_room_transaction(db)
            return RoomOperationResponse(
                operation_key=op_key,
                status="CANCELLED",
                scope="ROOM_OPERATION",
                room_id=None,
                room_status=None,
                result=None,
            )

        if existing.status == "SUCCEEDED":
            room_status = _resolve_current_room_status(db, existing.room_id)
            commit_room_transaction(db)
            return RoomOperationResponse(
                operation_key=existing.operation_key,
                status="SUCCEEDED",
                scope=existing.scope,
                room_id=existing.room_id,
                room_status=room_status,
                result=existing.response_json,
            )

        existing.status = "CANCELLED"
        existing.updated_at = now
        room_status = _resolve_current_room_status(db, existing.room_id)
        commit_room_transaction(db)
        return RoomOperationResponse(
            operation_key=existing.operation_key,
            status="CANCELLED",
            scope=existing.scope,
            room_id=existing.room_id,
            room_status=room_status,
            result=None,
        )
    except Exception:
        db.rollback()
        raise


@room_transaction(http=True)
def get_room(
    db: Session,
    room_public_id: str,
    user: User,
) -> RoomDetailResponse:
    """Return room state and member details visible to the current user."""
    lock_user_and_verify_session(db, user)
    room = lock_room(db, room_public_id)
    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )
    _apply_lazy_room_deadlines(
        db,
        room,
        closed_status_code=status.HTTP_410_GONE,
    )

    room_members = lock_room_members(db, room.id)
    sheets = {s.room_member_id: s for s in lock_order_sheets(db, [m.id for m in room_members])}
    # A current member can reference a user created after auth's old snapshot.
    # Read profiles without taking User locks after the Room/member locks.
    with current_read_session(db) as read_db:
        users = {u.id: u for u in read_db.query(User).filter(User.id.in_([m.user_id for m in room_members])).all()}
    member_rows = [(m, users[m.user_id], sheets.get(m.id))
                   for m in sorted(room_members, key=lambda m: (m.joined_at, m.id))]
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
                is_online=room_member.is_online,
                disconnected_at=room_member.disconnected_at,
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
    response = RoomDetailResponse(
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
    commit_room_transaction(db)
    return response


@room_transaction(http=True)
def complete_room_order_sheet(
    db: Session,
    room_public_id: str,
    user: User,
) -> tuple[RoomOrderSheetCompleteResponse, bool]:
    """Mark the current room member's order sheet as completed."""
    try:
        locked_user = lock_user_and_verify_session(db, user)
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found",
            )
        _apply_lazy_room_deadlines(
            db,
            room,
            closed_status_code=status.HTTP_409_CONFLICT,
        )

        member_rows = (
            db.query(RoomMember.id, RoomMember.user_id)
            .filter(RoomMember.room_id == room.id)
            .order_by(RoomMember.id)
            .execution_options(populate_existing=True).with_for_update()
            .all()
        )
        current_member_id = next(
            (
                room_member_id
                for room_member_id, member_user_id in member_rows
                if member_user_id == locked_user.id
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
            .order_by(RoomMemberOrderSheet.id)
            .execution_options(populate_existing=True).with_for_update()
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
        commit_room_transaction(db)
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


@room_transaction(http=True)
def close_room_by_host(
    db: Session,
    room_public_id: str,
    user: User,
) -> ClosedRoom:
    """Close a room after an explicit request from its host."""
    try:
        locked_user = lock_user_and_verify_session(db, user)
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found",
            )
        if room.host_user_id != locked_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the room host can delete the room",
            )

        closed_room = _close_room(
            room,
            ROOM_CLOSE_REASON_HOST_LEFT,
            utcnow(),
        )
        commit_room_transaction(db)
        return closed_room
    except Exception:
        db.rollback()
        raise


@room_transaction(http=True)
def leave_room_member(
    db: Session,
    room_public_id: str,
    requested_user_id: int,
    user: User,
) -> RoomMemberLeaveResponse:
    """Remove a non-host member and recalculate room completion state."""
    if user.id != requested_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Users can only leave a room as themselves",
        )

    try:
        locked_user = lock_user_and_verify_session(db, user)
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found",
            )
        _apply_lazy_room_deadlines(
            db,
            room,
            closed_status_code=status.HTTP_409_CONFLICT,
        )

        member = next((m for m in lock_room_members(db, room.id) if m.user_id == locked_user.id), None)
        if member is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room member not found",
            )
        if member.role == "HOST" or room.host_user_id == locked_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The room host must delete the room instead",
            )

        departure = remove_locked_members(db, room, [member])
        member_count = departure.member_count
        completed_member_count = departure.completed_member_count
        all_completed = departure.all_members_completed
        commit_room_transaction(db)
    except Exception:
        db.rollback()
        raise

    return RoomMemberLeaveResponse(
        room_id=room.public_id,
        user_id=requested_user_id,
        deleted=True,
        member_count=member_count,
        completed_member_count=completed_member_count,
        all_members_completed=all_completed,
    )


@room_transaction()
def delete_closed_room(db: Session, room_public_id: str) -> bool:
    """Physically delete a closed room and all dependent rows."""
    try:
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None:
            return False
        if room.status != "CLOSED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Room must be closed before deletion",
            )

        members = lock_room_members(db, room.id)
        for sheet in lock_order_sheets(db, [m.id for m in members]):
            db.delete(sheet)
        for member in members:
            db.delete(member)
        db.delete(room)
        commit_room_transaction(db)
        return True
    except Exception:
        db.rollback()
        raise


@room_transaction()
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
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None or room.status == "CLOSED" or room.host_user_id != user_id:
            return False
        room.host_disconnected_at = disconnected_at or utcnow()
        commit_room_transaction(db)
        return True
    except Exception:
        db.rollback()
        raise


@room_transaction()
def clear_host_disconnected(
    db: Session,
    room_public_id: str,
    user_id: int,
) -> bool:
    """Cancel a pending host disconnect deletion after reconnection."""
    try:
        current_time = utcnow()
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None or room.host_user_id != user_id or room.status == "CLOSED":
            return False
        if _as_utc(room.expires_at) <= current_time:
            _close_room(room, ROOM_CLOSE_REASON_EXPIRED, current_time)
            commit_room_transaction(db)
            return False
        if (
            room.host_disconnected_at is not None
            and _as_utc(room.host_disconnected_at)
            <= current_time - timedelta(seconds=settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS)
        ):
            _close_room(room, ROOM_CLOSE_REASON_HOST_DISCONNECTED, current_time)
            commit_room_transaction(db)
            return False
        changed = room.host_disconnected_at is not None
        room.host_disconnected_at = None
        commit_room_transaction(db)
        return changed
    except Exception:
        db.rollback()
        raise


@room_transaction()
def mark_room_member_disconnected(
    db: Session,
    room_public_id: str,
    user_id: int,
    disconnected_at: datetime | None = None,
) -> bool:
    """Start a room member's reconnect grace period."""
    try:
        room = lock_room(db, room_public_id)
        if room is None or room.status == "CLOSED":
            return False
        member = next((m for m in lock_room_members(db, room.id) if m.user_id == user_id), None)
        if member is None:
            return False
        member.disconnected_at = disconnected_at or utcnow()
        commit_room_transaction(db)
        return True
    except Exception:
        db.rollback()
        raise


@room_transaction()
def mark_room_member_online(
    db: Session,
    room_public_id: str,
    user_id: int,
    *,
    user: User | None = None,
    require_membership: bool = False,
) -> bool:
    """Mark a member online and report whether an offline event was visible."""
    try:
        if user is not None:
            lock_user_and_verify_session(db, user)
        current_time = utcnow()
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None or room.status == "CLOSED":
            if require_membership:
                raise HTTPException(403, detail="Room membership required")
            return False
        if _as_utc(room.expires_at) <= current_time:
            _close_room(room, ROOM_CLOSE_REASON_EXPIRED, current_time)
            commit_room_transaction(db)
            if require_membership:
                raise HTTPException(403, detail="Room membership required")
            return False
        if (
            room.host_disconnected_at is not None
            and _as_utc(room.host_disconnected_at)
            <= current_time - timedelta(seconds=settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS)
        ):
            _close_room(room, ROOM_CLOSE_REASON_HOST_DISCONNECTED, current_time)
            commit_room_transaction(db)
            if require_membership:
                raise HTTPException(403, detail="Room membership required")
            return False

        member = next((m for m in lock_room_members(db, room.id) if m.user_id == user_id), None)
        if member is None:
            commit_room_transaction(db)
            if require_membership:
                raise HTTPException(403, detail="Room membership required")
            return False
        if (
            member.role == "MEMBER"
            and member.disconnected_at is not None
            and _as_utc(member.disconnected_at)
            <= current_time - timedelta(seconds=settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS)
        ):
            remove_locked_members(db, room, [member])
            commit_room_transaction(db)
            if require_membership:
                raise HTTPException(403, detail="Room membership required")
            return False
        became_online = not member.is_online
        member.is_online = True
        member.disconnected_at = None
        commit_room_transaction(db)
        return became_online
    except Exception:
        db.rollback()
        raise


@room_transaction()
def expire_room_member_after_grace(
    db: Session,
    room_public_id: str,
    user_id: int,
    now: datetime | None = None,
) -> LeftRoomMember | None:
    """
    일반 참가자의 60초 재연결 유예가 만료되면 해당 참가자의 주문서와 참여 행을 삭제하고
    남은 인원 기준 통계를 재계산해 반환한다.
    """
    try:
        room = (
            db.query(Room)
            .filter(Room.public_id == room_public_id)
            .execution_options(populate_existing=True).with_for_update()
            .one_or_none()
        )
        if room is None or room.status == "CLOSED":
            return None

        member = next((m for m in lock_room_members(db, room.id)
                       if m.user_id == user_id and m.role == "MEMBER"), None)
        if member is None or member.disconnected_at is None:
            return None

        current_time = now or utcnow()
        deadline = _as_utc(member.disconnected_at) + timedelta(
            seconds=settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS
        )
        if deadline > current_time:
            return None

        departure = remove_locked_members(db, room, [member])
        member_count = departure.member_count
        completed_member_count = departure.completed_member_count
        all_completed = departure.all_members_completed
        commit_room_transaction(db)
        return LeftRoomMember(
            room_id=room.public_id,
            user_id=user_id,
            member_count=member_count,
            completed_member_count=completed_member_count,
            all_members_completed=all_completed,
        )
    except Exception:
        db.rollback()
        raise


def mark_room_member_offline_after_grace(
    db: Session,
    room_public_id: str,
    user_id: int,
    now: datetime | None = None,
) -> LeftRoomMember | bool:
    """
    Backward-compatible wrapper: returns LeftRoomMember (truthy) when the grace period
    expired and the member was automatically removed, or False otherwise.
    """
    result = expire_room_member_after_grace(db, room_public_id, user_id, now=now)
    return result if result is not None else False


def _due_member_room_ids(db: Session, current_time: datetime) -> list[int]:
    cutoff = current_time - timedelta(seconds=settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS)
    # Discovery only: never use a JOIN locking read as a lock-order guarantee.
    return [row[0] for row in db.query(Room.id).filter(
        Room.status != "CLOSED",
        Room.id.in_(select(RoomMember.room_id).where(
            RoomMember.role == "MEMBER", RoomMember.disconnected_at <= cutoff,
        )),
    ).order_by(Room.id).all()]


def _expire_members_in_room(db: Session, room_id: int, current_time: datetime) -> list[LeftRoomMember]:
    room = (db.query(Room).filter(Room.id == room_id)
            .execution_options(populate_existing=True).with_for_update().one_or_none())
    if room is None or room.status == "CLOSED":
        return []
    cutoff = current_time - timedelta(seconds=settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS)
    overdue = [m for m in lock_room_members(db, room.id) if m.role == "MEMBER"
               and m.disconnected_at is not None and _as_utc(m.disconnected_at) <= cutoff]
    return _departure_events(remove_locked_members(db, room, overdue))


@room_transaction()
def expire_due_room_members(db: Session, now: datetime | None = None) -> list[LeftRoomMember]:
    """Recovery's all-or-nothing member-cleanup stage; caller failures propagate."""
    current_time = now or utcnow()
    try:
        results = []
        for room_id in _due_member_room_ids(db, current_time):
            results.extend(_expire_members_in_room(db, room_id, current_time))
        commit_room_transaction(db)
        return results
    except Exception:
        db.rollback()
        raise


@room_transaction()
def _expire_member_room_transaction(db: Session, room_id: int, now: datetime | None):
    results = _expire_members_in_room(db, room_id, now or utcnow())
    commit_room_transaction(db)
    return results


def iter_due_room_member_departures(db: Session, now: datetime | None = None):
    """Yield committed batches so a later global failure cannot lose delivery.

    Only the periodic job isolates room failures. Recovery and account transition
    keep their existing transaction and failure propagation boundaries.
    """
    room_ids = _due_member_room_ids(db, now or utcnow())
    db.rollback()  # discovery's snapshot and connection do not span room work
    for room_id in room_ids:
        try:
            result = _expire_member_room_transaction(db, room_id, now)
        except Exception as exc:
            # Disconnection, unavailable schema/connection and programming errors
            # cannot be repaired by skipping a room. Cancellation is BaseException.
            local_db_error = isinstance(exc, (IntegrityError, DataError)) or (
                isinstance(exc, OperationalError) and is_lock_conflict(exc)
            )
            if (isinstance(exc, SQLAlchemyError) and not local_db_error) or (
                isinstance(exc, DBAPIError) and exc.connection_invalidated
            ):
                raise
            logging.getLogger(__name__).exception('Periodic departure failed for room %s', room_id)
            continue
        if result:
            yield result


def mark_due_room_members_offline(db: Session, now: datetime | None = None) -> list[LeftRoomMember]:
    return [member for batch in iter_due_room_member_departures(db, now) for member in batch]


@room_transaction()
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
            .execution_options(populate_existing=True).with_for_update()
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
        commit_room_transaction(db)
        return closed_room
    except Exception:
        db.rollback()
        raise


@room_transaction()
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
        rooms = db.query(Room).order_by(Room.id.asc()).populate_existing().with_for_update().all()
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
        commit_room_transaction(db)
        return closed_rooms
    except Exception:
        db.rollback()
        raise


def recover_rooms_after_restart(
    db: Session,
    now: datetime | None = None,
) -> dict[str, int]:
    """
    서버 재시작 시 이미 만료된 방과 60초 유예가 지난 참가자를 먼저 정리하고,
    이전에 is_online=True 이고 disconnected_at IS NULL 인 호스트·참가자에 대해
    재시작 시각(now)부터 60초 재연결 유예를 시작한다.
    이미 disconnected_at 이 기록된 행의 유예 시각은 연장하지 않는다.
    """
    current_time = now or utcnow()
    left_members = expire_due_room_members(db, now=current_time)
    closed_rooms = close_due_rooms(db, now=current_time)
    deleted_rooms = 0
    for closed in closed_rooms:
        if delete_closed_room(db, closed.room_id):
            deleted_rooms += 1

    try:
        rooms = (db.query(Room).filter(Room.status != "CLOSED").order_by(Room.id)
                 .execution_options(populate_existing=True).with_for_update().all())
        rows = [(member, room) for room in rooms for member in lock_room_members(db, room.id)]
        grace_started = 0
        for member, room in rows:
            if member.is_online and member.disconnected_at is None:
                member.disconnected_at = current_time
                grace_started += 1
                if (member.role == "HOST" or room.host_user_id == member.user_id) and room.host_disconnected_at is None:
                    room.host_disconnected_at = current_time
        commit_room_transaction(db)
        return {
            "expired_members": len(left_members),
            "deleted_rooms": deleted_rooms,
            "grace_started_members": grace_started,
        }
    except Exception:
        db.rollback()
        raise


def has_active_or_pending_room_activity(
    db: Session,
    user_ids: list[int],
    *,
    now: datetime | None = None,
) -> bool:
    """
    지정된 사용자들(원본 UUID 사용자, 대상 카카오 사용자 등) 중 누구라도
    1) 만료되지 않은 방에 참여 중(온라인 또는 60초 재연결 유예 중)이거나,
    2) 진행 중(PENDING)인 방 생성/참여 작업이 있으면 True를 반환한다.
    판정 전에 만료된 방과 60초 유예가 지난 일반 참가자는 지연 정리한다.
    """
    uid_set = sorted({int(uid) for uid in user_ids if uid is not None})
    if not uid_set:
        return False

    current_time = _as_utc(now) if now is not None else utcnow()
    pending_op = (
        db.execute(
            select(RequestOperation.id)
            .where(
                RequestOperation.user_id.in_(uid_set),
                RequestOperation.scope.in_(("ROOM_CREATE", "ROOM_JOIN")),
                RequestOperation.status == "PENDING",
            )
            .limit(1)
            .execution_options(populate_existing=True).with_for_update()
        )
        .scalar_one_or_none()
    )
    if pending_op is not None:
        return True

    def discover_room_ids(read_db: Session) -> list[int]:
        return [row[0] for row in read_db.query(RoomMember.room_id)
                .filter(RoomMember.user_id.in_(uid_set)).distinct().order_by(RoomMember.room_id).all()]

    # The caller's User locks serialize new joins. A fresh, short read sees
    # candidates committed after authentication's RR snapshot without locking
    # members ahead of Room or committing the account transition's work.
    with current_read_session(db) as discovery:
        room_ids = discover_room_ids(discovery)
    rooms = (db.query(Room).filter(Room.id.in_(room_ids))
             .order_by(Room.id).execution_options(populate_existing=True).with_for_update().all())
    host_cutoff = current_time - timedelta(seconds=settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS)
    member_cutoff = current_time - timedelta(seconds=settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS)

    for room in rooms:
        if room.status == "CLOSED":
            continue
        if _as_utc(room.expires_at) <= current_time:
            _close_room(room, ROOM_CLOSE_REASON_EXPIRED, current_time)
            continue
        if room.host_disconnected_at is not None and _as_utc(room.host_disconnected_at) <= host_cutoff:
            _close_room(room, ROOM_CLOSE_REASON_HOST_DISCONNECTED, current_time)
            continue

        members = lock_room_members(db, room.id)
        overdue = [m for m in members if m.role == "MEMBER"
                   and m.disconnected_at is not None and _as_utc(m.disconnected_at) <= member_cutoff]
        departure = remove_locked_members(db, room, overdue)
        removed_ids = set(departure.user_ids)
        if any(m.user_id in uid_set and m.user_id not in removed_ids for m in members):
            db.flush()
            return True

    db.flush()
    return False

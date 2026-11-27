from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.auth.operations import lock_user_and_verify_session
from app.rooms.departures import lock_room, lock_room_members
from app.rooms.models import Room, RoomRoundSearchOrigin
from app.rooms.rounds import get_latest_round, lock_round_candidates, lock_round_members
from app.rooms.schemas import RoomRoundSearchCoordinates, RoomRoundSearchOriginResponse
from app.rooms.service import _apply_lazy_room_deadlines
from app.rooms.state import mark_room_changed, response_common_state
from app.rooms.transactions import commit_room_transaction, room_transaction
from app.users.models import User


def _get_result_menu(
    db: Session, room_id: str, round_id: int, user: User, *, host_only: bool = False,
) -> tuple[Room, int]:
    """Authorize access and read the saved result while holding the room lock."""
    locked_user = lock_user_and_verify_session(db, user)
    room = lock_room(db, room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Room not found")
    _apply_lazy_room_deadlines(db, room, closed_status_code=status.HTTP_410_GONE)
    if not any(member.user_id == locked_user.id for member in lock_room_members(db, room.id)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only room members can access the group map")
    if host_only and room.host_user_id != locked_user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only the room host can set the search location")

    current_round = get_latest_round(db, room.id, for_update=True)
    if current_round is None or current_round.id != round_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={
            "code": "STALE_ROUND_ID", "message": "현재 라운드에 대한 요청이 아닙니다.",
        })
    if not any(m.user_id == locked_user.id and m.member_status == "ACTIVE"
               for m in lock_round_members(db, round_id)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only active round participants can access the group map")
    if current_round.status != "RESULT":
        raise HTTPException(status.HTTP_409_CONFLICT, detail={
            "code": "ROUND_RESULT_PENDING", "message": "메뉴가 확정된 뒤 주변 음식점을 검색할 수 있습니다.",
        })

    winners = [c for c in lock_round_candidates(db, round_id) if c.is_active and c.is_winner]
    if len(winners) != 1:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={
            "code": "ROUND_WINNER_UNAVAILABLE", "message": "확정된 메뉴 하나가 있어야 검색할 수 있습니다.",
        })
    return room, winners[0].menu_id


@room_transaction(http=True)
def set_room_round_search_origin(
    db: Session, room_id: str, round_id: int, user: User, coordinates: RoomRoundSearchCoordinates,
) -> RoomRoundSearchOriginResponse:
    room, _ = _get_result_menu(db, room_id, round_id, user, host_only=True)
    origin = db.get(RoomRoundSearchOrigin, round_id, populate_existing=True, with_for_update=True)
    if origin is None:
        db.add(RoomRoundSearchOrigin(round_id=round_id, **coordinates.model_dump()))
        mark_room_changed(db, room, "ROUND_NEARBY_LOCATION_READY", room.host_user_id)
    elif (origin.latitude, origin.longitude) != (coordinates.latitude, coordinates.longitude):
        raise HTTPException(status.HTTP_409_CONFLICT, detail={
            "code": "ROUND_NEARBY_LOCATION_ALREADY_SET", "message": "이미 이 라운드의 검색 위치가 확정됐습니다.",
        })

    result = RoomRoundSearchOriginResponse(round_id=round_id, common_state=response_common_state(db, room))
    commit_room_transaction(db)
    return result


@room_transaction(http=True)
def get_room_round_search_origin(
    db: Session, room_id: str, round_id: int, user: User,
) -> tuple[int, RoomRoundSearchCoordinates]:
    _, menu_id = _get_result_menu(db, room_id, round_id, user)
    origin = db.get(RoomRoundSearchOrigin, round_id, populate_existing=True, with_for_update=True)
    if origin is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={
            "code": "ROUND_NEARBY_LOCATION_PENDING", "message": "방장이 검색 위치를 등록할 때까지 기다려 주세요.",
        })
    coordinates = RoomRoundSearchCoordinates(latitude=origin.latitude, longitude=origin.longitude)
    commit_room_transaction(db)
    return menu_id, coordinates

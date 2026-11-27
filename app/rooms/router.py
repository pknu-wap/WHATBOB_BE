from app.rooms.notifications import dispatch_room
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.map.google_places import GooglePlaceSearchClient, get_google_place_search_client
from app.map.kakao_local import PlaceSearchClient, get_place_search_client
from app.map.schemas import SearchRadius
from app.map.service import find_nearby_restaurants
from app.recommend.schemas import ApiResponse, OrderSheetNoResultsResponse
from app.rooms.result_places import get_room_round_search_origin, set_room_round_search_origin
from app.rooms.rounds import (
    decide_room_round_departure,
    get_room_round_no_results_suggestions,
    get_room_round_questions,
    start_room_round,
    submit_room_round_order_sheet,
    submit_room_round_votes,
)
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomCreateResponse,
    RoomDeleteResponse,
    RoomDepartureDecisionRequest,
    RoomDetailResponse,
    RoomJoinResponse,
    RoomMemberLeaveResponse,
    RoomOperationResponse,
    RoomOrderSheetCompleteResponse,
    RoomOrderSheetNoResultsRequest,
    RoomOrderSheetSubmitRequest,
    RoomRoundCommandResponse,
    RoomRoundNearbyPlacesResponse,
    RoomRoundOrderSheetSubmitResponse,
    RoomRoundQuestionsResponse,
    RoomRoundSearchCoordinates,
    RoomRoundSearchOriginResponse,
    RoomRoundVoteSubmitResponse,
    RoomVoteSubmitRequest,
)
from app.rooms.service import (
    cancel_room_operation,
    close_room_by_host,
    complete_room_order_sheet,
    create_room,
    delete_closed_room,
    get_room,
    get_room_operation,
    join_room_with_meta,
    leave_room_member,
)
from app.rooms.websocket import room_connection_manager
from app.users.models import User

router = APIRouter(prefix="/api/rooms", tags=["Rooms"])
operations_router = APIRouter(prefix="/api/room-operations", tags=["Room Operations"])


async def _notify_and_delete_closed_room(
    db: Session,
    room_id: str,
    close_reason: str,
    closed_at: datetime,
) -> None:
    await room_connection_manager.broadcast(
        room_id,
        {
            "type": "room.closed",
            "room_id": room_id,
            "reason": close_reason,
            "closed_at": closed_at.isoformat(),
        },
    )
    await room_connection_manager.close_room_connections(room_id)
    await run_in_threadpool(delete_closed_room, db, room_id)


@router.post(
    "",
    response_model=RoomCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_room_endpoint(
    request: RoomCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    raw_idem_key = idempotency_key if isinstance(idempotency_key, str) else None
    result = await run_in_threadpool(create_room,
        db=db,
        host=current_user,
        request=request,
        idempotency_key=raw_idem_key,
    )
    await dispatch_room(db.get_bind(), result.room_id)
    return result


@router.post(
    "/{room_id}/join",
    response_model=RoomJoinResponse,
    status_code=status.HTTP_201_CREATED,
)
async def join_room_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    raw_idem_key = idempotency_key if isinstance(idempotency_key, str) else None
    response, is_replay, left_events = await run_in_threadpool(
        join_room_with_meta,
        db,
        room_id,
        current_user,
        idempotency_key=raw_idem_key,
    )
    for left in left_events:
        room_connection_manager.cancel_member_disconnect(room_id, left.user_id)
        await dispatch_room(db.get_bind(), room_id)
    if not is_replay:
        await dispatch_room(db.get_bind(), room_id)
    return response


@router.get(
    "/{room_id}",
    response_model=RoomDetailResponse,
)
async def get_room_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = await run_in_threadpool(get_room, db=db, room_public_id=room_id, user=current_user)
    await dispatch_room(db.get_bind(), room_id)
    return result


@router.delete(
    "/{room_id}",
    response_model=RoomDeleteResponse,
)
async def delete_room_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    closed_room = await run_in_threadpool(
        close_room_by_host,
        db,
        room_id,
        current_user,
    )
    await _notify_and_delete_closed_room(
        db=db,
        room_id=closed_room.room_id,
        close_reason=closed_room.reason,
        closed_at=closed_room.closed_at,
    )
    return RoomDeleteResponse(
        room_id=closed_room.room_id,
        deleted=True,
        close_reason=closed_room.reason,
        closed_at=closed_room.closed_at,
    )


@router.delete(
    "/{room_id}/members/{user_id}",
    response_model=RoomMemberLeaveResponse,
)
async def leave_room_member_endpoint(
    room_id: str,
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response = await run_in_threadpool(
        leave_room_member,
        db,
        room_id,
        user_id,
        current_user,
    )
    room_connection_manager.cancel_member_disconnect(room_id, user_id)
    await dispatch_room(db.get_bind(), room_id)
    await room_connection_manager.close_user_connections(room_id, user_id)
    return response


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
        await dispatch_room(db.get_bind(), room_id)
    return response


@router.post(
    "/{room_id}/rounds/start",
    response_model=RoomRoundCommandResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/{room_id}/start",
    response_model=RoomRoundCommandResponse,
    status_code=status.HTTP_200_OK,
)
async def start_room_round_endpoint(
    room_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bind = db.get_bind()
    try:
        response, is_replay, left_events = await run_in_threadpool(
            start_room_round,
            db,
            room_id,
            current_user,
        )
        for left in left_events:
            room_connection_manager.cancel_member_disconnect(room_id, left.user_id)
            await room_connection_manager.close_user_connections(room_id, left.user_id)
        if not is_replay or left_events:
            await dispatch_room(bind, room_id)
        return response
    finally:
        await dispatch_room(bind, room_id)


@router.get(
    "/{room_id}/rounds/current/questions",
    response_model=RoomRoundQuestionsResponse,
    status_code=status.HTTP_200_OK,
)
@router.get(
    "/{room_id}/order-sheet/questions",
    response_model=RoomRoundQuestionsResponse,
    status_code=status.HTTP_200_OK,
)
@router.get(
    "/{room_id}/questions",
    response_model=RoomRoundQuestionsResponse,
    status_code=status.HTTP_200_OK,
)
def get_room_round_questions_endpoint(
    room_id: str,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return get_room_round_questions(db=db, room_public_id=room_id, user=current_user)


@router.post(
    "/{room_id}/rounds/current/order-sheet/no-results",
    response_model=ApiResponse[OrderSheetNoResultsResponse],
    status_code=status.HTTP_200_OK,
)
def get_room_round_no_results_suggestions_endpoint(
    room_id: str,
    request: RoomOrderSheetNoResultsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = get_room_round_no_results_suggestions(db, room_id, current_user, request)
    return ApiResponse(success=True, data=result)


@router.post(
    "/{room_id}/rounds/current/order-sheet",
    response_model=RoomRoundOrderSheetSubmitResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/{room_id}/order-sheet/submit",
    response_model=RoomRoundOrderSheetSubmitResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/{room_id}/order-sheet",
    response_model=RoomRoundOrderSheetSubmitResponse,
    status_code=status.HTTP_200_OK,
)
async def submit_room_round_order_sheet_endpoint(
    room_id: str,
    request: RoomOrderSheetSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bind = db.get_bind()
    try:
        response, is_replay, left_events = await run_in_threadpool(
            submit_room_round_order_sheet,
            db,
            room_id,
            current_user,
            request,
        )
        for left in left_events:
            room_connection_manager.cancel_member_disconnect(room_id, left.user_id)
            await room_connection_manager.close_user_connections(room_id, left.user_id)
        if not is_replay or left_events:
            await dispatch_room(bind, room_id)
        return response
    finally:
        await dispatch_room(bind, room_id)


@router.post(
    "/{room_id}/rounds/current/votes",
    response_model=RoomRoundVoteSubmitResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/{room_id}/votes",
    response_model=RoomRoundVoteSubmitResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/{room_id}/vote",
    response_model=RoomRoundVoteSubmitResponse,
    status_code=status.HTTP_200_OK,
)
async def submit_room_round_votes_endpoint(
    room_id: str,
    request: RoomVoteSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bind = db.get_bind()
    try:
        response, is_replay, left_events = await run_in_threadpool(
            submit_room_round_votes,
            db,
            room_id,
            current_user,
            request,
        )
        for left in left_events:
            room_connection_manager.cancel_member_disconnect(room_id, left.user_id)
            await room_connection_manager.close_user_connections(room_id, left.user_id)
        if not is_replay or left_events:
            await dispatch_room(bind, room_id)
        return response
    finally:
        await dispatch_room(bind, room_id)


@router.put(
    "/{room_id}/rounds/{round_id}/nearby/location",
    response_model=RoomRoundSearchOriginResponse,
    summary="방장이 함께 검색할 기준 위치 등록",
)
async def set_room_round_search_origin_endpoint(
    room_id: str,
    round_id: Annotated[int, Path(ge=1)],
    coordinates: RoomRoundSearchCoordinates,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bind = db.get_bind()
    try:
        return await run_in_threadpool(
            set_room_round_search_origin, db, room_id, round_id, current_user, coordinates,
        )
    finally:
        await dispatch_room(bind, room_id)


@router.get(
    "/{room_id}/rounds/{round_id}/nearby",
    response_model=ApiResponse[RoomRoundNearbyPlacesResponse],
    summary="방장 위치에서 확정 메뉴의 주변 음식점 검색",
)
async def search_room_round_nearby_places_endpoint(
    room_id: str,
    round_id: Annotated[int, Path(ge=1)],
    response: Response,
    radius: SearchRadius = Query(SearchRadius.DEFAULT),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: PlaceSearchClient = Depends(get_place_search_client),
    google_client: GooglePlaceSearchClient = Depends(get_google_place_search_client),
):
    bind = db.get_bind()
    try:
        menu_id, coordinates = await run_in_threadpool(
            get_room_round_search_origin, db, room_id, round_id, current_user,
        )
    finally:
        await dispatch_room(bind, room_id)

    result = await run_in_threadpool(
        find_nearby_restaurants,
        db=db,
        client=client,
        menu_id=menu_id,
        latitude=coordinates.latitude,
        longitude=coordinates.longitude,
        radius=radius,
        google_client=google_client,
        google_max_concurrency=settings.GOOGLE_PLACES_MAX_CONCURRENCY,
        google_total_budget_seconds=settings.GOOGLE_PLACES_TOTAL_BUDGET_SECONDS,
        origin_label="방장 위치",
    )
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(success=True, data=RoomRoundNearbyPlacesResponse(
        round_id=round_id, search_origin=coordinates, **result.model_dump(),
    ))


@router.post(
    "/{room_id}/rounds/current/departure-decision",
    response_model=RoomRoundCommandResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/{room_id}/rounds/current/decision",
    response_model=RoomRoundCommandResponse,
    status_code=status.HTTP_200_OK,
)
@router.post(
    "/{room_id}/decision",
    response_model=RoomRoundCommandResponse,
    status_code=status.HTTP_200_OK,
)
async def decide_room_round_departure_endpoint(
    room_id: str,
    request: RoomDepartureDecisionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bind = db.get_bind()
    try:
        response, left_events = await run_in_threadpool(
            decide_room_round_departure,
            db,
            room_id,
            current_user,
            request,
        )
        for left in left_events:
            room_connection_manager.cancel_member_disconnect(room_id, left.user_id)
            await room_connection_manager.close_user_connections(room_id, left.user_id)
        await dispatch_room(bind, room_id)
        return response
    finally:
        await dispatch_room(bind, room_id)


@operations_router.get(
    "/{operation_key}",
    response_model=RoomOperationResponse,
    status_code=status.HTTP_200_OK,
)
def get_room_operation_endpoint(
    operation_key: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_room_operation(db=db, user=current_user, operation_key=operation_key)


@operations_router.post(
    "/{operation_key}/cancellation",
    response_model=RoomOperationResponse,
    status_code=status.HTTP_200_OK,
)
def cancel_room_operation_endpoint(
    operation_key: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return cancel_room_operation(db=db, user=current_user, operation_key=operation_key)

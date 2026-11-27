from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.core.config import settings
from app.map.google_places import GooglePlaceSearchClient, get_google_place_search_client
from app.map.kakao_local import PlaceSearchClient, get_place_search_client
from app.map.schemas import (
    NearbyPlacesResponse,
    PlaceSatisfactionDTO,
    PlaceSatisfactionUpsertRequest,
    PlaceSelectionCreateRequest,
    PlaceSelectionDTO,
    PlaceSelectionListResponse,
    SearchRadius,
)
from app.map.service import (
    create_place_selection,
    find_nearby_restaurants,
    get_place_selection,
    list_place_selections,
    record_place_satisfaction,
)
from app.recommend.schemas import ApiResponse
from app.users.models import User

router = APIRouter(prefix="/api/places", tags=["Map"])


@router.get(
    "/nearby",
    response_model=ApiResponse[NearbyPlacesResponse],
    status_code=status.HTTP_200_OK,
    summary="추천 메뉴 기준 주변 음식점 검색",
    description=(
        "추천 메뉴(menu_id)의 base_name으로 사용자 현재 위치 주변 음식점을 카카오 로컬 API에서 검색합니다. "
        "음식점(FD6) 카테고리만 허용하고 직선거리순 최대 10개를 반환합니다. "
        "각 음식점에는 일치가 확실한 경우에만 Google 장소 ID를 보강하며, 조회 실패 시 null을 반환합니다. "
        "반경은 1km(기본) 또는 3km만 허용하며, 결과가 없어도 반경을 자동으로 넓히지 않습니다. "
        "요청으로 받은 현재 위치를 기준으로 검색합니다. 같이 고르기의 공통 검색은 방 라운드 API를 사용합니다."
    ),
)
def search_nearby_places(
    menu_id: int = Query(..., ge=1, description="추천 결과의 메뉴 ID"),
    latitude: float = Query(..., ge=-90, le=90, description="사용자 현재 위도"),
    longitude: float = Query(..., ge=-180, le=180, description="사용자 현재 경도"),
    radius: SearchRadius = Query(SearchRadius.DEFAULT, description="검색 반경(m): 1000 또는 3000"),
    db: Session = Depends(get_db),
    client: PlaceSearchClient = Depends(get_place_search_client),
    google_client: GooglePlaceSearchClient = Depends(get_google_place_search_client),
    current_user: User = Depends(get_current_user),
):
    result = find_nearby_restaurants(
        db=db,
        client=client,
        menu_id=menu_id,
        latitude=latitude,
        longitude=longitude,
        radius=radius,
        google_client=google_client,
        google_max_concurrency=settings.GOOGLE_PLACES_MAX_CONCURRENCY,
        google_total_budget_seconds=settings.GOOGLE_PLACES_TOTAL_BUDGET_SECONDS,
    )
    return ApiResponse(success=True, data=result, message=None)


@router.post(
    "/selections",
    response_model=ApiResponse[PlaceSelectionDTO],
    status_code=status.HTTP_201_CREATED,
    summary="지도에서 음식점 명시적 선택 기록",
    description=(
        "추천 메뉴에서 지도로 이동한 사용자가 음식점을 명시적으로 선택했을 때 기록합니다. "
        "가게 선택만으로는 가게 만족이 미확인(null) 상태로 남으며 메뉴 좋아요·싫어요가 자동으로 생성되지 않습니다."
    ),
)
def record_place_selection(
    request: PlaceSelectionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = create_place_selection(db=db, current_user=current_user, request=request)
    return ApiResponse(success=True, data=result, message=None)


@router.get(
    "/selections",
    response_model=ApiResponse[PlaceSelectionListResponse],
    status_code=status.HTTP_200_OK,
    summary="내 지도 음식점 선택 기록 목록 조회",
)
def read_place_selections(
    recommendation_id: int | None = Query(default=None, ge=1, description="주문서 추천 ID 필터"),
    menu_id: int | None = Query(default=None, ge=1, description="메뉴 ID 필터"),
    limit: int = Query(default=20, ge=1, le=100, description="최대 조회 건수"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = list_place_selections(
        db=db,
        current_user=current_user,
        recommendation_id=recommendation_id,
        menu_id=menu_id,
        limit=limit,
    )
    return ApiResponse(success=True, data=result, message=None)


@router.get(
    "/selections/{selection_id}",
    response_model=ApiResponse[PlaceSelectionDTO],
    status_code=status.HTTP_200_OK,
    summary="지도 음식점 선택 기록 단건 조회",
)
def read_place_selection(
    selection_id: int = Path(..., ge=1, description="가게 선택 기록 ID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = get_place_selection(db=db, current_user=current_user, selection_id=selection_id)
    return ApiResponse(success=True, data=result, message=None)


@router.post(
    "/selections/{selection_id}/satisfaction",
    response_model=ApiResponse[PlaceSatisfactionDTO],
    status_code=status.HTTP_200_OK,
    summary="선택한 음식점에 대한 명시적 만족 기록",
    description=(
        "선택한 가게에 대한 사용자의 명시적 만족 여부를 가게 선택 기록과 별도로 저장합니다. "
        "같은 선택 기록에 대해 다시 제출하면 중복 생성하지 않고 안전하게 갱신하며, 메뉴 좋아요·싫어요를 생성하지 않습니다."
    ),
)
def upsert_place_satisfaction(
    request: PlaceSatisfactionUpsertRequest,
    selection_id: int = Path(..., ge=1, description="가게 선택 기록 ID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = record_place_satisfaction(
        db=db,
        current_user=current_user,
        selection_id=selection_id,
        request=request,
    )
    return ApiResponse(success=True, data=result, message=None)

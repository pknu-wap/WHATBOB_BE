from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.core.config import settings
from app.map.google_places import GooglePlaceSearchClient, get_google_place_search_client
from app.map.kakao_local import PlaceSearchClient, get_place_search_client
from app.map.schemas import NearbyPlacesResponse, SearchRadius
from app.map.service import find_nearby_restaurants
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
        "개인 추천과 같이 고르기 최종 결과 모두 같은 API를 사용하며 각 사용자의 현재 위치를 기준으로 합니다."
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

from datetime import datetime, timezone
import math
from typing import Any
from urllib.parse import quote

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.map.google_places import GooglePlaceSearchClient
from app.map.place_enrichment import enrich_google_place_ids
from app.auth.operations import lock_user_and_verify_session
from app.map.exceptions import (
    PlaceRecommendationForbiddenException,
    PlaceRecommendationNotFoundException,
    PlaceSearchMenuNotFoundException,
    PlaceSelectionForbiddenException,
    PlaceSelectionInvalidException,
    PlaceSelectionNotFoundException,
)
from app.map.kakao_local import KAKAO_RESTAURANT_CATEGORY_CODE, PlaceSearchClient
from app.map.models import PlaceSatisfaction, PlaceSelection
from app.map.schemas import (
    NearbyPlaceDTO,
    NearbyPlacesResponse,
    PlaceSatisfactionDTO,
    PlaceSatisfactionUpsertRequest,
    PlaceSelectionCreateRequest,
    PlaceSelectionDTO,
    PlaceSelectionListResponse,
    SearchRadius,
)
from app.map.search_keywords import build_place_search_keyword
from app.menus.models import Menu, Recommendation
from app.users.models import User

MAX_NEARBY_PLACES = 10
KAKAO_MAP_LINK_BASE_URL = "https://map.kakao.com/link"
CURRENT_LOCATION_LABEL = "내 위치"
EARTH_RADIUS_M = 6_371_000


def find_nearby_restaurants(
    db: Session,
    client: PlaceSearchClient,
    menu_id: int,
    latitude: float,
    longitude: float,
    radius: SearchRadius,
    google_client: GooglePlaceSearchClient,
    google_max_concurrency: int,
    google_total_budget_seconds: float,
    *,
    origin_label: str = CURRENT_LOCATION_LABEL,
) -> NearbyPlacesResponse:
    """메뉴를 검색어로 써서 전달받은 좌표 주변 음식점을 직선거리순 최대 10개 찾는다.

    검색 결과가 없어도 반경을 자동으로 넓히지 않는다. 확대는 사용자가 선택한다.
    """
    menu = db.get(Menu, menu_id)
    if menu is None:
        raise PlaceSearchMenuNotFoundException(menu_id)

    keyword = build_place_search_keyword(menu.base_name, menu.name)
    radius_m = int(radius)
    documents = client.search_restaurants(
        keyword=keyword,
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        size=MAX_NEARBY_PLACES,
    )

    places: list[NearbyPlaceDTO] = []
    seen_ids: set[str] = set()
    for document in documents:
        place = _to_nearby_place(document, latitude, longitude, origin_label)
        if place is None or place.distance_m > radius_m or place.kakao_place_id in seen_ids:
            continue
        seen_ids.add(place.kakao_place_id)
        places.append(place)

    places.sort(key=lambda p: p.distance_m)
    places = places[:MAX_NEARBY_PLACES]
    enrich_google_place_ids(
        places,
        google_client,
        max_concurrency=google_max_concurrency,
        total_budget_seconds=google_total_budget_seconds,
    )

    return NearbyPlacesResponse(
        menu_id=menu.id,
        menu_base_name=menu.base_name,
        search_keyword=keyword,
        radius_m=radius_m,
        total_count=len(places),
        places=places,
    )


def _to_nearby_place(
    document: dict[str, Any], origin_lat: float, origin_lng: float, origin_label: str,
) -> NearbyPlaceDTO | None:
    """외부 응답을 신뢰하지 않고 필수 필드·카테고리·좌표를 검증해 DTO로 변환한다."""
    if document.get("category_group_code") != KAKAO_RESTAURANT_CATEGORY_CODE:
        return None
    place_id = str(document.get("id") or "").strip()
    name = str(document.get("place_name") or "").strip()
    if not place_id or not name:
        return None
    try:
        latitude = float(document["y"])
        longitude = float(document["x"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    distance_m = _parse_distance(document.get("distance"))
    if distance_m is None:
        distance_m = _straight_line_distance_m(origin_lat, origin_lng, latitude, longitude)

    return NearbyPlaceDTO(
        kakao_place_id=place_id,
        name=name,
        category_name=_blank_to_none(document.get("category_name")),
        phone=_blank_to_none(document.get("phone")),
        address=_blank_to_none(document.get("address_name")),
        road_address=_blank_to_none(document.get("road_address_name")),
        latitude=latitude,
        longitude=longitude,
        distance_m=distance_m,
        place_url=_blank_to_none(document.get("place_url")),
        directions_url=f"{KAKAO_MAP_LINK_BASE_URL}/to/{quote(place_id, safe='')}",
        walking_directions_url=build_walking_directions_url(
            origin_lat, origin_lng, name, latitude, longitude, origin_label=origin_label,
        ),
    )


def build_walking_directions_url(
    origin_lat: float,
    origin_lng: float,
    destination_name: str,
    destination_lat: float,
    destination_lng: float,
    *,
    origin_label: str = CURRENT_LOCATION_LABEL,
) -> str:
    """카카오맵 도보 길찾기 URL(/link/by/walk/출발/도착)을 만든다.

    URL 구분자인 ','와 '/'가 장소명에 있으면 경로가 깨지므로 공백으로 바꾼 뒤 인코딩한다.
    """
    origin = _link_point(origin_label, origin_lat, origin_lng)
    destination = _link_point(destination_name, destination_lat, destination_lng)
    return f"{KAKAO_MAP_LINK_BASE_URL}/by/walk/{origin}/{destination}"


def _link_point(name: str, latitude: float, longitude: float) -> str:
    safe_name = " ".join(name.replace(",", " ").replace("/", " ").split()) or "도착지"
    return f"{quote(safe_name, safe='')},{latitude},{longitude}"


def _parse_distance(value: Any) -> int | None:
    try:
        distance = int(float(value))
    except (TypeError, ValueError):
        return None
    return distance if distance >= 0 else None


def _straight_line_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return round(2 * EARTH_RADIUS_M * math.asin(math.sqrt(a)))


def _blank_to_none(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _to_place_satisfaction_dto(satisfaction: PlaceSatisfaction) -> PlaceSatisfactionDTO:
    return PlaceSatisfactionDTO(
        id=satisfaction.id,
        place_selection_id=satisfaction.place_selection_id,
        recommendation_id=satisfaction.recommendation_id,
        menu_id=satisfaction.menu_id,
        kakao_place_id=satisfaction.kakao_place_id,
        is_satisfied=bool(satisfaction.is_satisfied),
        created_at=satisfaction.created_at,
        updated_at=satisfaction.updated_at,
    )


def _to_place_selection_dto(
    selection: PlaceSelection,
    satisfaction: PlaceSatisfaction | None = None,
) -> PlaceSelectionDTO:
    return PlaceSelectionDTO(
        id=selection.id,
        recommendation_id=selection.recommendation_id,
        menu_id=selection.menu_id,
        kakao_place_id=selection.kakao_place_id,
        place_name=selection.place_name,
        place_address=selection.place_address,
        created_at=selection.created_at,
        satisfaction=_to_place_satisfaction_dto(satisfaction) if satisfaction is not None else None,
    )


def create_place_selection(
    db: Session,
    current_user: User,
    request: PlaceSelectionCreateRequest,
) -> PlaceSelectionDTO:
    """사용자가 지도에서 명시적으로 선택한 가게를 기록한다.

    가게 선택만으로 가게 만족(PlaceSatisfaction)이나 메뉴 피드백을 생성하지 않는다.
    """
    locked_user = lock_user_and_verify_session(db, current_user)

    menu = db.get(Menu, request.menu_id)
    if menu is None:
        raise PlaceSearchMenuNotFoundException(request.menu_id)

    recommendation_id = request.recommendation_id
    if recommendation_id is not None:
        rec = db.get(Recommendation, recommendation_id)
        if rec is None:
            raise PlaceRecommendationNotFoundException(recommendation_id)
        if rec.user_id != locked_user.id:
            raise PlaceRecommendationForbiddenException()
        recommended_menu_ids = [int(mid) for mid in (rec.recommended_menu_ids or [])]
        if menu.id not in recommended_menu_ids:
            raise PlaceSelectionInvalidException("해당 추천 결과에 포함된 메뉴만 가게 선택에 연결할 수 있습니다.")

    now = datetime.now(timezone.utc)
    selection = PlaceSelection(
        user_id=locked_user.id,
        recommendation_id=recommendation_id,
        menu_id=menu.id,
        kakao_place_id=request.kakao_place_id,
        place_name=request.place_name,
        place_address=request.place_address,
        created_at=now,
    )
    db.add(selection)
    db.commit()
    db.refresh(selection)
    return _to_place_selection_dto(selection, satisfaction=None)


def get_place_selection(
    db: Session,
    current_user: User,
    selection_id: int,
) -> PlaceSelectionDTO:
    """본인의 단일 가게 선택 기록과 명시적 만족 상태(없으면 None)를 조회한다."""
    selection = (
        db.query(PlaceSelection)
        .options(selectinload(PlaceSelection.satisfaction))
        .filter(PlaceSelection.id == selection_id)
        .first()
    )
    if selection is None:
        raise PlaceSelectionNotFoundException(selection_id)
    if selection.user_id != current_user.id:
        raise PlaceSelectionForbiddenException()
    return _to_place_selection_dto(selection, satisfaction=selection.satisfaction)


def list_place_selections(
    db: Session,
    current_user: User,
    *,
    recommendation_id: int | None = None,
    menu_id: int | None = None,
    limit: int = 20,
) -> PlaceSelectionListResponse:
    """본인의 가게 선택 기록 목록을 최신순으로 조회한다."""
    query = (
        db.query(PlaceSelection)
        .options(selectinload(PlaceSelection.satisfaction))
        .filter(PlaceSelection.user_id == current_user.id)
    )
    if recommendation_id is not None:
        query = query.filter(PlaceSelection.recommendation_id == recommendation_id)
    if menu_id is not None:
        query = query.filter(PlaceSelection.menu_id == menu_id)

    rows = (
        query.order_by(PlaceSelection.created_at.desc(), PlaceSelection.id.desc())
        .limit(limit)
        .all()
    )
    items = [_to_place_selection_dto(row, satisfaction=row.satisfaction) for row in rows]
    return PlaceSelectionListResponse(total_count=len(items), selections=items)


def record_place_satisfaction(
    db: Session,
    current_user: User,
    selection_id: int,
    request: PlaceSatisfactionUpsertRequest,
) -> PlaceSatisfactionDTO:
    """선택한 가게에 대한 명시적 만족 여부를 가게 선택·메뉴 피드백과 별도로 기록(또는 갱신)한다."""
    locked_user = lock_user_and_verify_session(db, current_user)

    selection = (
        db.query(PlaceSelection)
        .filter(PlaceSelection.id == selection_id)
        .with_for_update()
        .first()
    )
    if selection is None:
        raise PlaceSelectionNotFoundException(selection_id)
    if selection.user_id != locked_user.id:
        raise PlaceSelectionForbiddenException()

    now = datetime.now(timezone.utc)
    satisfaction = (
        db.query(PlaceSatisfaction)
        .filter(PlaceSatisfaction.place_selection_id == selection.id)
        .with_for_update()
        .first()
    )
    if satisfaction is None:
        new_row = PlaceSatisfaction(
            place_selection_id=selection.id,
            user_id=locked_user.id,
            recommendation_id=selection.recommendation_id,
            menu_id=selection.menu_id,
            kakao_place_id=selection.kakao_place_id,
            is_satisfied=bool(request.is_satisfied),
            created_at=now,
            updated_at=now,
        )
        try:
            with db.begin_nested():
                db.add(new_row)
                db.flush()
            satisfaction = new_row
        except IntegrityError:
            satisfaction = (
                db.query(PlaceSatisfaction)
                .filter(PlaceSatisfaction.place_selection_id == selection.id)
                .with_for_update()
                .one()
            )
            satisfaction.is_satisfied = bool(request.is_satisfied)
            satisfaction.updated_at = now
    else:
        satisfaction.is_satisfied = bool(request.is_satisfied)
        satisfaction.updated_at = now

    db.commit()
    db.refresh(satisfaction)
    return _to_place_satisfaction_dto(satisfaction)

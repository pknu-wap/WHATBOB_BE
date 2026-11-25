import math
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.map.exceptions import PlaceSearchMenuNotFoundException
from app.map.google_places import GooglePlaceSearchClient
from app.map.kakao_local import KAKAO_RESTAURANT_CATEGORY_CODE, PlaceSearchClient
from app.map.place_enrichment import enrich_google_place_ids
from app.map.schemas import NearbyPlaceDTO, NearbyPlacesResponse, SearchRadius
from app.map.search_keywords import build_place_search_keyword
from app.menus.models import Menu

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
) -> NearbyPlacesResponse:
    """추천 메뉴로 사용자 주변 음식점을 직선거리순 최대 10개 찾는다.

    좌표는 검색 요청에만 사용하고 저장하거나 로그에 남기지 않는다.
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
        place = _to_nearby_place(document, latitude, longitude)
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


def _to_nearby_place(document: dict[str, Any], origin_lat: float, origin_lng: float) -> NearbyPlaceDTO | None:
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
            origin_lat, origin_lng, name, latitude, longitude
        ),
    )


def build_walking_directions_url(
    origin_lat: float,
    origin_lng: float,
    destination_name: str,
    destination_lat: float,
    destination_lng: float,
) -> str:
    """카카오맵 도보 길찾기 URL(/link/by/walk/출발/도착)을 만든다.

    URL 구분자인 ','와 '/'가 장소명에 있으면 경로가 깨지므로 공백으로 바꾼 뒤 인코딩한다.
    """
    origin = _link_point(CURRENT_LOCATION_LABEL, origin_lat, origin_lng)
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

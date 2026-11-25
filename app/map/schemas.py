from enum import IntEnum
from typing import Literal

from pydantic import BaseModel


class SearchRadius(IntEnum):
    """허용 검색 반경(m). 기본 1km, 결과가 없을 때 사용자가 3km로 확대한다."""

    DEFAULT = 1000
    EXPANDED = 3000


class NearbyPlaceDTO(BaseModel):
    kakao_place_id: str
    google_place_id: str | None = None
    name: str
    category_name: str | None = None
    phone: str | None = None
    address: str | None = None
    road_address: str | None = None
    latitude: float
    longitude: float
    distance_m: int
    place_url: str | None = None
    directions_url: str
    walking_directions_url: str


class NearbyPlacesResponse(BaseModel):
    menu_id: int
    menu_base_name: str
    search_keyword: str
    radius_m: int
    provider: Literal["KAKAO"] = "KAKAO"
    # 카카오 로컬 API의 distance는 검색 중심점과의 직선거리이며 실제 도보 경로 거리가 아니다.
    distance_type: Literal["STRAIGHT_LINE"] = "STRAIGHT_LINE"
    total_count: int
    places: list[NearbyPlaceDTO]

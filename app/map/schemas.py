from datetime import datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class PlaceSelectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation_id: int | None = Field(default=None, ge=1, description="원래 주문서 추천 ID")
    menu_id: int = Field(..., ge=1, description="선택한 메뉴 ID")
    kakao_place_id: str = Field(..., min_length=1, max_length=100, description="카카오 장소 ID")
    place_name: str = Field(..., min_length=1, max_length=200, description="선택한 음식점 이름")
    place_address: str | None = Field(default=None, max_length=300, description="선택한 음식점 주소(선택)")

    @field_validator("kakao_place_id", "place_name")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("공백만 입력할 수 없습니다.")
        return cleaned

    @field_validator("place_address")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class PlaceSatisfactionUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_satisfied: bool = Field(..., description="가게에 대한 명시적 만족 여부")


class PlaceSatisfactionDTO(BaseModel):
    id: int
    place_selection_id: int
    recommendation_id: int | None = None
    menu_id: int | None = None
    kakao_place_id: str
    is_satisfied: bool
    created_at: datetime
    updated_at: datetime


class PlaceSelectionDTO(BaseModel):
    id: int
    recommendation_id: int | None = None
    menu_id: int | None = None
    kakao_place_id: str
    place_name: str
    place_address: str | None = None
    created_at: datetime
    satisfaction: PlaceSatisfactionDTO | None = None


class PlaceSelectionListResponse(BaseModel):
    total_count: int
    selections: list[PlaceSelectionDTO]

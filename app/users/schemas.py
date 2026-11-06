from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator

MAX_DISLIKED_FOODS = 100
DislikedFoodKeyword = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100),
]


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kakao_id: int
    nickname: str | None = None
    email: str | None = None
    profile_image: str | None = None


class DislikedFoodsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[DislikedFoodKeyword] = Field(
        max_length=MAX_DISLIKED_FOODS,
        description="비선호 목록 전체. 빈 배열은 비선호 설정만 모두 해제합니다.",
    )

    @field_validator("keywords")
    @classmethod
    def remove_duplicate_keywords(cls, keywords: list[str]) -> list[str]:
        unique: dict[str, str] = {}
        for keyword in keywords:
            unique.setdefault(keyword.casefold(), keyword)
        return list(unique.values())


class DislikedFoodResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    keyword: str


class DislikedFoodsResponse(BaseModel):
    items: list[DislikedFoodResponse]
    total_count: int


class UserMealCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    menu_id: StrictInt = Field(ge=1, description="먹은 음식(메뉴) ID")
    eaten_at: datetime | None = Field(
        default=None,
        description="식사 일시 (ISO-8601, 생략 시 현재 UTC 시각으로 저장)",
    )

    @field_validator("eaten_at", mode="before")
    @classmethod
    def reject_numeric_timestamp(cls, value: object) -> object:
        if isinstance(value, (bool, int, float)):
            raise ValueError("eaten_at은 ISO-8601 날짜·시간 문자열이어야 합니다.")
        return value

    @field_validator("eaten_at", mode="after")
    @classmethod
    def normalize_eaten_at_to_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class UserMealMenuDTO(BaseModel):
    menu_id: int
    food_code: str
    name: str
    base_name: str
    category_large: str
    category_medium: str | None = None
    meal_type: str
    has_soup: bool
    cooking_style: str
    main_ingredient: str
    flavor_intensity: str
    temperature: str
    heaviness: str
    kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carb_g: float | None = None
    sugar_g: float | None = None
    sodium_mg: float | None = None


class UserMealItemResponse(BaseModel):
    id: int
    menu_id: int | None = None
    menu_name: str
    eaten_at: datetime
    created_at: datetime
    menu: UserMealMenuDTO | None = None


class UserMealsResponse(BaseModel):
    items: list[UserMealItemResponse]
    total_count: int

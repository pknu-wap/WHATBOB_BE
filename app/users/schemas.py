from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

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

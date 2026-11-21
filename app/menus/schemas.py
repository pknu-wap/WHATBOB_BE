from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class PopularMenuItemResponse(BaseModel):
    food_kind_id: int
    name: str
    image_url: str


class PopularMenusResponse(BaseModel):
    source: Literal["initial", "aggregated"]
    as_of: datetime | None = None
    items: list[PopularMenuItemResponse]

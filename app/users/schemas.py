from pydantic import BaseModel, ConfigDict


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kakao_id: int
    nickname: str | None = None
    email: str | None = None
    profile_image: str | None = None

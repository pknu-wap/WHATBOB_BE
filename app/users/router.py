from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.users.models import User
from app.users.schemas import UserResponse

router = APIRouter(prefix="/api/users", tags=["Users"])


@router.get("/me", response_model=UserResponse)
def get_my_profile(current_user: User = Depends(get_current_user)):
    """서비스 JWT로 로그인한 사용자의 저장된 프로필을 조회한다."""
    return UserResponse.model_validate(current_user)

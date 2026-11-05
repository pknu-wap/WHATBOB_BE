from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.recommend.schemas import ApiResponse
from app.users.models import User
from app.users.schemas import DislikedFoodsResponse, DislikedFoodsUpdateRequest, UserResponse
from app.users.service import delete_my_disliked_food, get_my_disliked_foods, replace_my_disliked_foods

router = APIRouter(prefix="/api/users", tags=["Users"])


@router.get("/me", response_model=UserResponse)
def get_my_profile(current_user: User = Depends(get_current_user)):
    """서비스 JWT로 로그인한 사용자의 저장된 프로필을 조회한다."""
    return UserResponse.model_validate(current_user)


@router.get("/me/dislikes", response_model=ApiResponse[DislikedFoodsResponse])
def get_my_disliked_foods_endpoint(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """서비스 JWT로 인증한 본인의 비선호 음식 목록만 조회한다."""
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=get_my_disliked_foods(db, current_user.id))


@router.put("/me/dislikes", response_model=ApiResponse[DislikedFoodsResponse])
def replace_my_disliked_foods_endpoint(
    request: DislikedFoodsUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 비선호 목록 전체를 저장한다. 빈 keywords 배열로 모두 해제할 수 있다."""
    return ApiResponse(data=replace_my_disliked_foods(db, current_user.id, request))


@router.delete("/me/dislikes/{dislike_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_disliked_food_endpoint(
    dislike_id: int = Path(ge=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 비선호 항목 하나를 삭제한다. 다른 사용자의 항목은 조회하지 않는다."""
    delete_my_disliked_food(db, current_user.id, dislike_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

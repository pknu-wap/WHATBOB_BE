from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.recommend.schemas import ApiResponse
from app.users.models import User
from app.users.schemas import (
    DislikedFoodsResponse,
    DislikedFoodsUpdateRequest,
    UserMealCreateRequest,
    UserMealItemResponse,
    UserMealsResponse,
    UserResponse,
)
from app.users.service import (
    delete_my_disliked_food,
    get_my_disliked_foods,
    get_my_meals,
    record_my_meal,
    replace_my_disliked_foods,
)

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


@router.get("/me/meals", response_model=ApiResponse[UserMealsResponse])
def get_my_meals_endpoint(
    response: Response,
    limit: int = Query(default=50, ge=1, le=200, description="최대 조회 개수 (기본 50, 최대 200)"),
    offset: int = Query(default=0, ge=0, description="건너뛸 항목 수"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """로그인한 사용자의 식사 기록(먹은 음식 목록)을 최신순으로 조회한다."""
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=get_my_meals(db, current_user.id, limit=limit, offset=offset))


@router.post(
    "/me/meals",
    response_model=ApiResponse[UserMealItemResponse],
    status_code=status.HTTP_201_CREATED,
)
def record_my_meal_endpoint(
    request: UserMealCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """로그인한 사용자가 먹은 음식을 user_meals 테이블에 저장한다."""
    return ApiResponse(data=record_my_meal(db, current_user.id, request))

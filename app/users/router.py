from fastapi import APIRouter, Depends, File, Header, Path, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.database import get_db
from app.recommend.schemas import ApiResponse
from app.users.models import User
from app.users.profile_image import replace_my_profile_image, reset_my_profile_image
from app.users.schemas import (
    CatalogExclusionsResponse,
    CatalogExclusionsUpdateRequest,
    DislikedFoodsResponse,
    DislikedFoodsUpdateRequest,
    NicknameUpdateRequest,
    UserMealCreateRequest,
    UserMealItemResponse,
    UserMealsResponse,
    UserResponse,
)
from app.users.service import (
    delete_my_disliked_food,
    delete_my_meal,
    get_my_catalog_exclusions,
    get_my_disliked_foods,
    get_my_meals,
    record_my_meal,
    replace_my_catalog_exclusions,
    replace_my_disliked_foods,
    update_my_nickname,
)

router = APIRouter(prefix="/api/users", tags=["Users"])


@router.get("/me", response_model=UserResponse)
def get_my_profile(response: Response, current_user: User = Depends(get_current_user)):
    """서비스 JWT로 로그인한 사용자의 저장된 프로필을 조회한다."""
    response.headers["Cache-Control"] = "no-store"
    return UserResponse.model_validate(current_user)


@router.patch("/me/nickname", response_model=UserResponse)
def update_my_nickname_endpoint(
    request: NicknameUpdateRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 표시 닉네임을 수정한다. 앞뒤 공백 제거 후 2~20자 한글/영문/숫자/공백 및 전체 고유성을 검증한다."""
    response.headers["Cache-Control"] = "no-store"
    return update_my_nickname(db, current_user, request)


@router.put("/me/profile-image", response_model=UserResponse)
def replace_my_profile_image_endpoint(
    response: Response,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 프로필 사진을 업로드·교체한다 (5MB·1,200만 픽셀 이하 JPG/PNG/WebP → 긴 변 512px 정지 이미지 정규화)."""
    response.headers["Cache-Control"] = "no-store"
    return replace_my_profile_image(db, current_user, file)


@router.delete("/me/profile-image", response_model=UserResponse)
def delete_my_profile_image_endpoint(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 프로필 사진을 기본 이미지(/static/default-profile.svg)로 복원한다."""
    response.headers["Cache-Control"] = "no-store"
    return reset_my_profile_image(db, current_user)



@router.get("/me/allergies", response_model=ApiResponse[CatalogExclusionsResponse])
def get_my_allergies_endpoint(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """서비스 JWT로 인증한 본인의 식약처 19대 알레르기 제외 목록을 조회한다."""
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=get_my_catalog_exclusions(db, current_user.id, "ALLERGY"))


@router.put("/me/allergies", response_model=ApiResponse[CatalogExclusionsResponse])
def replace_my_allergies_endpoint(
    request: CatalogExclusionsUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 알레르기 제외 목록 전체를 표준 코드 배열로 저장한다. 빈 배열로 모두 해제할 수 있다."""
    return ApiResponse(
        data=replace_my_catalog_exclusions(
            db,
            current_user.id,
            "ALLERGY",
            request,
            current_user=current_user,
        )
    )


@router.get("/me/unables", response_model=ApiResponse[CatalogExclusionsResponse])
def get_my_unables_endpoint(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """서비스 JWT로 인증한 본인의 섭취불가 재료군(8종) 제외 목록을 조회한다."""
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=get_my_catalog_exclusions(db, current_user.id, "UNABLE"))


@router.put("/me/unables", response_model=ApiResponse[CatalogExclusionsResponse])
def replace_my_unables_endpoint(
    request: CatalogExclusionsUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 섭취불가 재료군 제외 목록 전체를 표준 코드 배열로 저장한다. 빈 배열로 모두 해제할 수 있다."""
    return ApiResponse(
        data=replace_my_catalog_exclusions(
            db,
            current_user.id,
            "UNABLE",
            request,
            current_user=current_user,
        )
    )


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
    return ApiResponse(data=replace_my_disliked_foods(db, current_user.id, request, current_user=current_user))


@router.delete("/me/dislikes/{dislike_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_disliked_food_endpoint(
    dislike_id: int = Path(ge=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 비선호 항목 하나를 삭제한다. 다른 사용자의 항목은 조회하지 않는다."""
    delete_my_disliked_food(db, current_user.id, dislike_id, current_user=current_user)
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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """로그인한 사용자가 먹은 음식을 user_meals 테이블에 저장한다."""
    raw_idem_key = idempotency_key if isinstance(idempotency_key, str) else None
    return ApiResponse(
        data=record_my_meal(
            db,
            current_user.id,
            request,
            current_user=current_user,
            idempotency_key=raw_idem_key,
        )
    )


@router.delete("/me/meals/{meal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_meal_endpoint(
    meal_id: int = Path(ge=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """본인의 식사 기록 항목 하나를 삭제한다."""
    delete_my_meal(db, current_user.id, meal_id, current_user=current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.operations import lock_user_and_verify_session
from app.core.database import get_db
from app.menus.display import format_menu_name
from app.menus.models import Menu
from app.recommend.common import (
    ALLERGEN_CATALOG_VERSION,
    OFFICIAL_ALLERGEN_ITEMS,
    UNABLE_CATALOG_VERSION,
    UNABLE_INGREDIENT_ITEMS,
)
from app.recommend.order_sheet import (
    DEFAULT_EXPLOITATION_COUNT,
    DEFAULT_TOTAL_QUESTIONS,
    create_order_sheet_recommendation,
    issue_order_sheet_questions,
    record_recommendation_feedback,
    select_order_sheet_questions,
    tune_question_weights,
)
from app.recommend.random_pick import pick_random_menu
from app.recommend.schemas import (
    AllergenItemDTO,
    AllergenListResponse,
    ApiResponse,
    OrderSheetQuestionsResponse,
    OrderSheetRecommendRequest,
    OrderSheetRecommendResponse,
    OrderSheetWeightTuneRequest,
    OrderSheetWeightTuneResponse,
    RandomMenuDTO,
    RandomRecommendResponse,
    RecommendationFeedbackRequest,
    RecommendationFeedbackResponse,
    UnableIngredientItemDTO,
    UnableIngredientListResponse,
)
from app.users.models import User

router = APIRouter(prefix="/api/recommendations", tags=["Recommendations"])


def _to_random_menu_dto(menu: Menu) -> RandomMenuDTO:
    return RandomMenuDTO(
        menu_id=menu.id,
        food_code=menu.food_code,
        name=format_menu_name(menu.name),
        base_name=menu.base_name,
    )


@router.get(
    "/allergens",
    response_model=ApiResponse[AllergenListResponse],
    status_code=status.HTTP_200_OK,
    summary="식약처 법정 19대 알레르기 유발물질 목록 조회",
    description="식품 등의 표시·광고에 관한 법률 시행규칙 [별표 2] 기준 식약처 고시 19대 알레르기 유발물질 표준 목록을 반환합니다.",
)
def get_official_allergens():
    """프론트엔드 알레르기 선택 화면 렌더링용 식약처 19대 알레르기 목록 반환"""
    dtos = [AllergenItemDTO(**item) for item in OFFICIAL_ALLERGEN_ITEMS]
    return ApiResponse(
        success=True,
        data=AllergenListResponse(
            catalog_version=ALLERGEN_CATALOG_VERSION,
            total_count=len(dtos),
            allergens=dtos,
            items=dtos,
        ),
        message=None,
    )


@router.get(
    "/unable-ingredients",
    response_model=ApiResponse[UnableIngredientListResponse],
    status_code=status.HTTP_200_OK,
    summary="섭취불가 8대 재료군 카탈로그 조회",
    description="종교·신념·체질 등으로 먹지 못하는 8대 주요 재료군 표준 카탈로그 목록을 반환합니다.",
)
def get_unable_ingredients():
    """프론트엔드 섭취불가 재료군 선택 화면 렌더링용 8종 카탈로그 반환"""
    dtos = [UnableIngredientItemDTO(**item) for item in UNABLE_INGREDIENT_ITEMS]
    return ApiResponse(
        success=True,
        data=UnableIngredientListResponse(
            catalog_version=UNABLE_CATALOG_VERSION,
            total_count=len(dtos),
            unable_ingredients=dtos,
            items=dtos,
        ),
        message=None,
    )


@router.get(
    "/random",
    response_model=ApiResponse[RandomRecommendResponse],
    status_code=status.HTTP_200_OK,
)
def get_random_recommendation(
    response: Response,
    exclusions: list[str] | None = Query(
        default=None,
        description="이번 랜덤 추천에서 추가로 제외할 음식 또는 재료 키워드",
    ),
    db: Session = Depends(get_db),
    http_request: Request = None,
    current_user: User = Depends(get_current_user),
):
    """제외 조건을 적용한 뒤 음식 종류와 실제 메뉴를 차례로 무작위 선택한다."""
    if http_request is not None and "user_id" in http_request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user_id parameter is not allowed.",
        )
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    user_id = None
    if isinstance(current_user, User):
        locked_user = lock_user_and_verify_session(db, current_user)
        user_id = locked_user.id
    result = pick_random_menu(
        db=db,
        user_id=user_id,
        extra_exclusions=exclusions,
    )
    return ApiResponse(
        success=True,
        data=RandomRecommendResponse(
            selected_menu=_to_random_menu_dto(result.menu),
        ),
        message=None,
    )


@router.get(
    "/order-sheet/questions",
    response_model=ApiResponse[OrderSheetQuestionsResponse],
    status_code=status.HTTP_200_OK,
)
def get_order_sheet_questions(
    total_count: int | None = Query(
        default=None,
        ge=1,
        le=20,
        description="반환할 주문서 전체 질문 개수 (미지정 시 서버 설정 ORDER_SHEET_QUESTION_COUNT 사용)",
    ),
    exploitation_count: int | None = Query(
        default=None,
        ge=1,
        le=20,
        description="가중치 최상위 고정 질문 개수 (미지정 시 기본 주문서 전체 문항 수 적용)",
    ),
    issuance_key: str | None = Query(
        default=None,
        description="동일 주문서 진입 요청 재전송 식별자 (Idempotency-Key 헤더와 동일 역할)",
    ),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    http_request: Request = None,
    response: Response = None,
    current_user: User = Depends(get_current_user),
):
    """
    개인 주문서 진입 시 오늘 답할 9문항(서버 설정 기준)을 발급한다.
    동일한 Idempotency-Key(또는 issuance_key)로 재전송하면 기존 발급 주문서를 그대로 반환하고,
    새로고침·재입장 시 새 주문서를 발급한다.
    """
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    user_id = None
    scope_key = None
    if isinstance(current_user, User):
        locked_user = lock_user_and_verify_session(db, current_user)
        user_id = locked_user.id
        scope_key = f"user:{locked_user.id}"
    raw_idem_key = idempotency_key if isinstance(idempotency_key, str) else None
    raw_issuance_key = issuance_key if isinstance(issuance_key, str) else None
    result = issue_order_sheet_questions(
        db=db,
        user_id=user_id,
        total_count=total_count,
        exploitation_count=exploitation_count,
        scope_key=scope_key,
        idempotency_key=raw_idem_key,
        issuance_key=raw_issuance_key,
    )
    return ApiResponse(success=True, data=result, message=None)


@router.post(
    "/order-sheet",
    response_model=ApiResponse[OrderSheetRecommendResponse],
    status_code=status.HTTP_201_CREATED,
)
def submit_order_sheet(
    request: OrderSheetRecommendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    사용자의 주문서 답변을 받아 알레르기/비선호 음식을 먼저 제외(Hard Filter)한 뒤,
    엔트로피 기반 초기 점수와 사용자 피드백 통계로 조정한 질문 중요도를 합산해 메뉴를 추천합니다.
    """
    actor = current_user if isinstance(current_user, User) else None
    result = create_order_sheet_recommendation(
        db=db,
        request=request,
        user_id=actor.id if actor is not None else None,
        current_user=actor,
    )
    return ApiResponse(success=True, data=result, message=None)


@router.post(
    "/{recommendation_id}/feedback",
    response_model=ApiResponse[RecommendationFeedbackResponse],
    status_code=status.HTTP_200_OK,
)
def submit_recommendation_feedback(
    recommendation_id: int,
    request: RecommendationFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """
    추천 결과에 대한 사용자의 만족/불만족 피드백을 저장하고,
    조건 일치 여부별 누적 만족도 통계와 초기 점수를 결합해 질문 중요도를 재계산합니다.
    """
    actor = current_user if isinstance(current_user, User) else None
    raw_idem_key = idempotency_key if isinstance(idempotency_key, str) else None
    result = record_recommendation_feedback(
        db=db,
        recommendation_id=recommendation_id,
        request=request,
        user_id=actor.id if actor is not None else None,
        current_user=actor,
        idempotency_key=raw_idem_key,
    )
    return ApiResponse(success=True, data=result, message=None)


@router.post(
    "/order-sheet/weights",
    response_model=ApiResponse[OrderSheetWeightTuneResponse],
    status_code=status.HTTP_200_OK,
)
def adjust_order_sheet_weights(
    request: OrderSheetWeightTuneRequest,
    db: Session = Depends(get_db),
):
    """
    [4단계 개발팀 사전 피드백 튜닝 및 일괄 재계산]
    특정 질문의 중요도를 직접 조정하거나 누적 사용자 피드백 통계로 재계산합니다.
    """
    result = tune_question_weights(db=db, request=request)
    return ApiResponse(success=True, data=result, message=None)

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.recommend.order_sheet import (
    DEFAULT_EXPLOITATION_COUNT,
    DEFAULT_TOTAL_QUESTIONS,
    create_order_sheet_recommendation,
    record_recommendation_feedback,
    select_order_sheet_questions,
    tune_question_weights,
)
from app.recommend.schemas import (
    ApiResponse,
    OrderSheetQuestionsResponse,
    OrderSheetRecommendRequest,
    OrderSheetRecommendResponse,
    OrderSheetWeightTuneRequest,
    OrderSheetWeightTuneResponse,
    RecommendationFeedbackRequest,
    RecommendationFeedbackResponse,
)

router = APIRouter(prefix="/api/recommendations", tags=["Recommendations"])


@router.get(
    "/order-sheet/questions",
    response_model=ApiResponse[OrderSheetQuestionsResponse],
    status_code=status.HTTP_200_OK,
)
def get_order_sheet_questions(
    total_count: int = Query(
        default=DEFAULT_TOTAL_QUESTIONS,
        ge=1,
        le=20,
        description="반환할 주문서 전체 질문 개수 (기본 9개)",
    ),
    exploitation_count: int = Query(
        default=DEFAULT_EXPLOITATION_COUNT,
        ge=1,
        le=20,
        description="가중치 최상위 고정 질문 개수 (나머지는 후보 탐색 질문)",
    ),
    db: Session = Depends(get_db),
):
    """
    MySQL에 적재된 질문 풀에서 ML 가중치 상위 질문(기본 7개)과
    새로운 피드백 수집을 위한 탐색 질문(기본 2개)을 합쳐 총 9개의 주문서 질문지를 반환합니다.
    """
    result = select_order_sheet_questions(
        db=db,
        total_count=total_count,
        exploitation_count=min(exploitation_count, total_count),
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
):
    """
    사용자의 주문서 답변을 받아 알레르기/비선호 음식을 먼저 제외(Hard Filter)한 뒤,
    각 질문의 ML 중요도 가중치를 반영하여 최적의 메뉴를 추천합니다.
    """
    result = create_order_sheet_recommendation(db=db, request=request)
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
):
    """
    추천 결과에 대한 사용자(또는 사전 튜닝 단계)의 만족/불만족 피드백을 저장하고,
    누적된 피드백 데이터를 바탕으로 질문 중요도 가중치(ml_weight)를 즉시 재학습합니다.
    """
    result = record_recommendation_feedback(
        db=db,
        recommendation_id=recommendation_id,
        request=request,
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
    [4단계 개발팀 사전 피드백 튜닝 및 일괄 재학습]
    특정 질문의 가중치를 직접 조정하거나 누적 피드백 기반 ML 재학습을 실행합니다.
    """
    result = tune_question_weights(db=db, request=request)
    return ApiResponse(success=True, data=result, message=None)


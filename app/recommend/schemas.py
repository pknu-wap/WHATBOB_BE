from typing import Any, Generic, Optional, TypeVar
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """AGENTS.md 공통 API 응답 형식"""
    success: bool = True
    data: Optional[T] = None
    message: Optional[str] = None


class FilterRuleDTO(BaseModel):
    field: str
    op: str
    value: Any


class QuestionOptionDTO(BaseModel):
    option_id: str
    label: str
    rules: list[FilterRuleDTO] = Field(default_factory=list)


class OrderSheetQuestionDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    question_code: str
    cluster_key: str
    question_text: str
    options: list[QuestionOptionDTO]
    # alias는 기존 프론트엔드의 JSON 키와 호환하기 위한 이름이다.
    initial_score: float = Field(
        alias="info_gain_score",
        description="샤논 엔트로피와 커버리지로 계산한 초기 질문 점수",
    )
    importance_weight: float = Field(
        alias="ml_weight",
        description="초기 점수·주제 다양성 및 사용자 피드백 통계로 조정하는 질문 중요도",
    )
    shown_count: int = 0
    positive_count: int = 0
    is_exploration: bool = False


class OrderSheetQuestionsResponse(BaseModel):
    questions: list[OrderSheetQuestionDTO]
    total_count: int
    exploitation_count: int
    exploration_count: int


class AnswerItemRequest(BaseModel):
    question_id: int
    option_id: str


class OrderSheetRecommendRequest(BaseModel):
    user_id: Optional[int] = None
    exclusions: list[str] = Field(
        default_factory=list,
        description="추가로 제외할 알레르기/섭취불가/비선호 음식 또는 재료 키워드",
    )
    answers: list[AnswerItemRequest] = Field(
        ...,
        min_length=1,
        description="주문서 질문에 대한 사용자 응답 목록",
    )
    top_k: int = Field(default=5, ge=1, le=20)


class RecommendedMenuDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    menu_id: int
    food_code: str
    name: str
    base_name: str
    category_large: str
    category_medium: Optional[str] = None
    meal_type: str
    has_soup: bool
    cooking_style: str
    main_ingredient: str
    flavor_intensity: str
    temperature: str
    heaviness: str
    kcal: Optional[float] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carb_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    match_score: float
    matched_question_codes: list[str] = Field(default_factory=list)


class OrderSheetRecommendResponse(BaseModel):
    recommendation_id: int
    recommended_menus: list[RecommendedMenuDTO]
    excluded_count: int
    candidate_count: int


class RecommendationFeedbackRequest(BaseModel):
    user_id: Optional[int] = None
    selected_menu_id: Optional[int] = None
    is_satisfied: bool


class RecommendationFeedbackResponse(BaseModel):
    feedback_id: int
    recommendation_id: int
    is_satisfied: bool
    updated_weights: dict[str, float]


class WeightAdjustmentItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    question_id: int
    importance_weight: Optional[float] = Field(
        default=None, ge=0.01, le=5.0, alias="ml_weight",
        description="직접 지정할 질문 중요도 (기존 JSON 키 유지)",
    )
    feedback_delta: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    is_active: Optional[bool] = None


class OrderSheetWeightTuneRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    adjustments: list[WeightAdjustmentItem] = Field(default_factory=list)
    recalculate_from_feedback: bool = Field(
        default=True, alias="run_ml_retrain",
        description="누적 사용자 피드백 통계로 질문 중요도를 재계산할지 여부 (기존 JSON 키 유지)",
    )


class OrderSheetWeightTuneResponse(BaseModel):
    updated_count: int
    top_questions: list[OrderSheetQuestionDTO]


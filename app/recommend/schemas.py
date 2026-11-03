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
    model_config = ConfigDict(from_attributes=True)

    id: int
    question_code: str
    cluster_key: str
    question_text: str
    options: list[QuestionOptionDTO]
    info_gain_score: float
    ml_weight: float
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
    question_id: int
    ml_weight: Optional[float] = Field(default=None, ge=0.01, le=5.0)
    feedback_delta: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    is_active: Optional[bool] = None


class OrderSheetWeightTuneRequest(BaseModel):
    adjustments: list[WeightAdjustmentItem] = Field(default_factory=list)
    run_ml_retrain: bool = True


class OrderSheetWeightTuneResponse(BaseModel):
    updated_count: int
    top_questions: list[OrderSheetQuestionDTO]


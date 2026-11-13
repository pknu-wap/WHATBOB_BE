from typing import Any, Generic, Optional, TypeVar
from pydantic import BaseModel, ConfigDict, Field
from app.recommend.question_pool import MAX_QUESTION_TEXT_LENGTH, QUESTION_OPTION_COUNT

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
    question_text: str = Field(min_length=1, max_length=MAX_QUESTION_TEXT_LENGTH)
    options: list[QuestionOptionDTO] = Field(
        min_length=2, max_length=QUESTION_OPTION_COUNT,
    )
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


class AllergenItemDTO(BaseModel):
    id: str = Field(description="알레르기 고유 코드 (예: egg, milk, peanut)")
    code: str = Field(description="알레르기 카탈로그 코드 (id와 동일)")
    name: str = Field(description="화면 표시용 이름 (예: 알류(달걀), 우유, 땅콩)")
    label: str = Field(description="화면 표시용 라벨 (name과 동일)")
    legal_name: str = Field(description="식약처 고시 법정 명칭")
    description: str = Field(description="해당 재료 및 대표 가공식품 예시")


class AllergenListResponse(BaseModel):
    catalog_version: int = Field(default=1, description="알레르기 카탈로그 버전")
    total_count: int = Field(description="알레르기 유발물질 총 개수 (식약처 고시 19종)")
    allergens: list[AllergenItemDTO]
    items: list[AllergenItemDTO] = Field(default_factory=list)


class UnableIngredientItemDTO(BaseModel):
    id: str = Field(description="섭취불가 재료군 고유 코드 (예: pork, beef, poultry)")
    code: str = Field(description="섭취불가 재료군 카탈로그 코드 (id와 동일)")
    name: str = Field(description="화면 표시용 이름")
    label: str = Field(description="화면 표시용 라벨 (name과 동일)")
    category: str = Field(description="재료 대분류")
    description: str = Field(description="해당 재료군 대표 식품 예시")


class UnableIngredientListResponse(BaseModel):
    catalog_version: int = Field(default=1, description="섭취불가 재료군 카탈로그 버전")
    total_count: int = Field(description="섭취불가 재료군 총 개수 (8종)")
    unable_ingredients: list[UnableIngredientItemDTO]
    items: list[UnableIngredientItemDTO] = Field(default_factory=list)


class OrderSheetQuestionsResponse(BaseModel):
    questions: list[OrderSheetQuestionDTO]
    total_count: int
    exploitation_count: int
    exploration_count: int


class AnswerItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: int
    option_id: str


class OrderSheetRecommendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    name: str = Field(description="원본 음식명의 구분자와 중복 분류를 정리한 화면 표시명")
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


class RandomMenuDTO(BaseModel):
    menu_id: int
    food_code: str
    name: str
    base_name: str


class RandomRecommendResponse(BaseModel):
    selected_menu: RandomMenuDTO


class RecommendationFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


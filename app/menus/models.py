from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
)
from app.core.database import Base


class Menu(Base):
    """식약처 식품영양성분 CSV 기반 메뉴 및 정규화 속성 테이블"""
    __tablename__ = "menus"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    food_code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(200), index=True, nullable=False)
    base_name = Column(String(100), index=True, nullable=False)

    category_large = Column(String(50), index=True, nullable=False)
    category_medium = Column(String(50), index=True, nullable=True)
    category_small = Column(String(50), nullable=True)
    origin_type = Column(String(50), index=True, nullable=False, default="외식")

    # 주문서 조건식(filter_rule) 매칭용 정규화 속성
    meal_type = Column(String(30), index=True, nullable=False, default="main_meal")
    has_soup = Column(Boolean, index=True, nullable=False, default=False)
    cooking_style = Column(String(30), index=True, nullable=False, default="other")
    main_ingredient = Column(String(30), index=True, nullable=False, default="other")
    flavor_intensity = Column(String(30), index=True, nullable=False, default="mild_clean")
    temperature = Column(String(20), index=True, nullable=False, default="hot")
    heaviness = Column(String(20), index=True, nullable=False, default="moderate")

    # 100g(ml) 기준 영양 성분 수치
    kcal = Column(Float, nullable=True)
    protein_g = Column(Float, nullable=True)
    fat_g = Column(Float, nullable=True)
    carb_g = Column(Float, nullable=True)
    sugar_g = Column(Float, nullable=True)
    sodium_mg = Column(Float, nullable=True)

    # 알레르기 및 제외 키워드 목록 (예: ["해산물", "돼지고기", "우유"])
    allergen_tags = Column(JSON, nullable=False, default=list)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserFoodExclusion(Base):
    """사용자별 알레르기(ALLERGY), 섭취불가(UNABLE), 비선호(DISLIKE) 음식/재료 테이블"""
    __tablename__ = "user_food_exclusions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    exclusion_type = Column(String(20), index=True, nullable=False, default="ALLERGY")
    keyword = Column(String(100), nullable=False)
    catalog_code = Column(String(50), nullable=True, index=True)
    catalog_version = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserMeal(Base):
    """사용자별 먹은 음식(식사 기록) 테이블"""
    __tablename__ = "user_meals"
    __table_args__ = (
        Index("ix_user_meals_user_eaten_at", "user_id", "eaten_at"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    menu_id = Column(Integer, ForeignKey("menus.id", ondelete="SET NULL"), index=True, nullable=True)
    menu_name = Column(String(200), nullable=False)
    eaten_at = Column(DateTime, index=True, nullable=False, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class OrderSheetQuestion(Base):
    """주문서 질문 후보와 엔트로피·사용자 피드백 기반 중요도 테이블"""
    __tablename__ = "order_sheet_questions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    question_code = Column(String(80), unique=True, index=True, nullable=False)
    cluster_key = Column(String(50), index=True, nullable=False)
    question_text = Column(String(300), nullable=False)

    # 각 선택지(option_id, label, rules)를 담은 JSON 배열
    options_json = Column(JSON, nullable=False)

    # 초기 점수는 선택지 분포의 샤논 엔트로피와 커버리지로 계산한다.
    # 기존 DB 컬럼명을 매핑해 이미 저장된 질문과 피드백을 그대로 사용한다.
    initial_score = Column("info_gain_score", Float, nullable=False, default=0.5)

    # 초기 주제 다양성 보정, 수동 조정 및 누적 만족도 통계로 갱신하는 중요도.
    importance_weight = Column("ml_weight", Float, index=True, nullable=False, default=0.5)

    shown_count = Column(Integer, nullable=False, default=0)
    positive_count = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, index=True, nullable=False, default=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),)


class QuestionSelectionState(Base):
    """로그인 사용자 또는 익명 브라우저별 탐색 질문 순환 상태."""
    __tablename__ = "question_selection_states"

    scope_key = Column(String(80), primary_key=True)
    last_clusters = Column(JSON, nullable=False, default=list)
    topic_counts = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class Recommendation(Base):
    """주문서 추천 요청 및 결과 이력 테이블"""
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    shown_question_ids = Column(JSON, nullable=False, default=list)
    answers_json = Column(JSON, nullable=False, default=list)
    recommended_menu_ids = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class RecommendationFeedback(Base):
    """추천 만족 여부와 질문별 조건 일치 결과를 저장하는 피드백 테이블"""
    __tablename__ = "recommendation_feedbacks"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    recommendation_id = (
        Column(
            Integer,
            ForeignKey("recommendations.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    selected_menu_id = Column(Integer, ForeignKey("menus.id", ondelete="SET NULL"), nullable=True)
    menu_key = Column(String(100), nullable=True, index=True)
    menu_base_name = Column(String(100), nullable=True)
    menu_main_ingredient = Column(String(30), nullable=True)
    operation_key = Column(String(64), nullable=True, index=True)
    is_satisfied = Column(Boolean, nullable=False)

    # 질문별 조건 일치 여부: {질문 ID 문자열: 1.0(일치) 또는 0.0(불일치)}.
    answer_match_results = Column("feature_match_vector", JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class RecommendationFeedbackState(Base):
    """추천×음식(menu_key) 단위 현재 유효 피드백 상태 테이블"""
    __tablename__ = "recommendation_feedback_states"
    __table_args__ = (
        Index("uq_rec_feedback_states_rec_menu_key", "recommendation_id", "menu_key", unique=True),
        Index("ix_rec_feedback_states_user_updated", "user_id", "updated_at"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    recommendation_id = Column(
        Integer,
        ForeignKey("recommendations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    selected_menu_id = Column(Integer, ForeignKey("menus.id", ondelete="SET NULL"), nullable=True)
    menu_key = Column(String(100), nullable=False)
    menu_base_name = Column(String(100), nullable=False)
    menu_main_ingredient = Column(String(30), nullable=False, default="other")
    is_satisfied = Column(Boolean, nullable=False)
    answer_match_results = Column("feature_match_vector", JSON, nullable=False, default=dict)
    latest_feedback_id = Column(
        Integer,
        ForeignKey("recommendation_feedbacks.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class RecommendationItemSnapshot(Base):
    """추천 시점 메뉴별 피처·점수 스냅샷 테이블 (M1A 모델 학습 및 추론 검증용)"""
    __tablename__ = "recommendation_item_snapshots"
    __table_args__ = (
        Index("uq_rec_item_snapshots_rec_menu", "recommendation_id", "menu_id", unique=True),
        Index("ix_rec_item_snapshots_rec_menu_key", "recommendation_id", "menu_key"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    recommendation_id = Column(
        Integer,
        ForeignKey("recommendations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    menu_id = Column(Integer, ForeignKey("menus.id", ondelete="SET NULL"), nullable=True)
    menu_key = Column(String(100), nullable=False)
    menu_base_name = Column(String(100), nullable=False)
    menu_main_ingredient = Column(String(30), nullable=False, default="other")
    rank_position = Column(Integer, nullable=False, default=0)
    rule_score = Column(Float, nullable=False, default=0.0)
    recent_meal_adjustment = Column(Float, nullable=False, default=0.0)
    personal_adjustment = Column(Float, nullable=False, default=0.0)
    final_score = Column(Float, nullable=False, default=0.0)
    draw_weight = Column(Float, nullable=False, default=1.0)
    feature_schema_version = Column(Integer, nullable=False, default=1)
    features_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


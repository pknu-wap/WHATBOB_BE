from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.core.database import Base


class FoodKind(Base):
    """홈 최근에 먹은 메뉴·요즘 인기 메뉴 공통 분류 단위(음식 종류) 테이블"""
    __tablename__ = "food_kinds"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    kind_key = Column(String(100), unique=True, index=True, nullable=False)
    name = Column(String(100), index=True, nullable=False)
    representative_image_url = Column(String(500), nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class InitialPopularFoodKind(Base):
    """실제 인기 순위가 처음 발행되기 전 홈에 공개하는 초기 인기 메뉴 8종 순서 테이블"""
    __tablename__ = "initial_popular_food_kinds"
    __table_args__ = (
        CheckConstraint(
            "position >= 1 AND position <= 8",
            name="ck_initial_popular_food_kinds_position_range",
        ),
        UniqueConstraint("position", name="uq_initial_popular_food_kinds_position"),
        UniqueConstraint("food_kind_id", name="uq_initial_popular_food_kinds_food_kind_id"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    position = Column(Integer, nullable=False)
    food_kind_id = Column(
        Integer,
        ForeignKey("food_kinds.id", name="fk_initial_popular_food_kinds_food_kind"),
        nullable=False,
    )
    food_kind = relationship("FoodKind")
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PopularMenuRankingState(Base):
    """마지막 실제 인기 순위 공개 상태 및 최근 성공 집계일 단일 행 테이블"""
    __tablename__ = "popular_menu_ranking_state"
    __table_args__ = (
        CheckConstraint(
            "id = 1",
            name="ck_popular_menu_ranking_state_singleton",
        ),
    )

    id = Column(Integer, primary_key=True, default=1, autoincrement=False)
    last_succeeded_kst_date = Column(Date, nullable=True)
    published_as_of = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PopularMenuRankingItem(Base):
    """현재 공개 중인 실제 인기 메뉴 순위(최대 8종) 단일 세트 테이블"""
    __tablename__ = "popular_menu_ranking_items"
    __table_args__ = (
        CheckConstraint(
            "rank_position >= 1 AND rank_position <= 8",
            name="ck_popular_menu_ranking_items_rank_range",
        ),
        CheckConstraint(
            "user_count >= 1",
            name="ck_popular_menu_ranking_items_user_count_positive",
        ),
        UniqueConstraint(
            "food_kind_id",
            name="uq_popular_menu_ranking_items_food_kind_id",
        ),
    )

    rank_position = Column(Integer, primary_key=True, autoincrement=False)
    food_kind_id = Column(
        Integer,
        ForeignKey("food_kinds.id", name="fk_popular_ranking_items_food_kind"),
        nullable=False,
    )
    user_count = Column(Integer, nullable=False)
    last_eaten_at = Column(DateTime, nullable=False)
    food_kind = relationship("FoodKind")


class Menu(Base):
    """식약처 식품영양성분 CSV 기반 메뉴 및 정규화 속성 테이블"""
    __tablename__ = "menus"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    food_code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(200), index=True, nullable=False)
    base_name = Column(String(100), index=True, nullable=False)
    food_kind_id = Column(
        Integer,
        ForeignKey("food_kinds.id", name="fk_menus_food_kind"),
        index=True,
        nullable=False,
    )
    food_kind = relationship("FoodKind")

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


class MenuIllustrationException(Base):
    """홈 최근 메뉴 카드에서 대표 일러스트 대신 공통 이미지를 표시할 메뉴별 예외 테이블"""
    __tablename__ = "menu_illustration_exceptions"

    menu_id = Column(
        Integer,
        ForeignKey(
            "menus.id",
            ondelete="CASCADE",
            name="fk_menu_illustration_exceptions_menu",
        ),
        primary_key=True,
    )
    reason = Column(String(200), nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


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
    initial_candidate_count = Column(Integer, nullable=True)
    list_rejected_at = Column(DateTime, nullable=True, index=True)
    closed_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class OrderSheetIssuance(Base):
    """개인 주문서 발급 이력 및 동일 발급 재전송 멱등성 테이블"""
    __tablename__ = "order_sheet_issuances"
    __table_args__ = (
        UniqueConstraint("user_id", "issuance_key", name="uq_order_sheet_issuances_user_key"),
        Index("ix_order_sheet_issuances_user_created", "user_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    order_sheet_id = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    issuance_key = Column(String(128), nullable=True, index=True)
    question_ids_json = Column(JSON, nullable=False, default=list)
    questions_snapshot_json = Column(JSON, nullable=False, default=list)
    question_roles_json = Column(JSON, nullable=False, default=dict)
    total_count = Column(Integer, nullable=False, default=9)
    exploitation_count = Column(Integer, nullable=False, default=9)
    exploration_count = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="ISSUED", index=True)
    recommendation_id = Column(
        Integer,
        ForeignKey("recommendations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)


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
    food_kind_id = Column(Integer, ForeignKey("food_kinds.id", ondelete="SET NULL"), index=True, nullable=True)
    exposure_rank = Column(Integer, nullable=True)
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
    food_kind_id = Column(Integer, ForeignKey("food_kinds.id", ondelete="SET NULL"), index=True, nullable=True)
    exposure_rank = Column(Integer, nullable=True)
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


class OrderSheetHardFilterTrace(Base):
    """주문서별 하드필터 적용 순서·질문·답변 및 단계별 전후 후보 수 점검 기록 테이블 (Spec #77 / #87)"""
    __tablename__ = "order_sheet_hard_filter_traces"
    __table_args__ = (
        CheckConstraint("step_order >= 1", name="ck_order_sheet_hf_traces_step_order_positive"),
        UniqueConstraint("recommendation_id", "step_order", name="uq_order_sheet_hf_traces_rec_step"),
        Index("ix_order_sheet_hf_traces_user_created", "user_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    recommendation_id = Column(
        Integer,
        ForeignKey("recommendations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    step_order = Column(Integer, nullable=False)
    question_id = Column(Integer, index=True, nullable=False)
    question_code = Column(String(80), index=True, nullable=False)
    cluster_key = Column(String(50), index=True, nullable=False)
    option_id = Column(String(80), nullable=False)
    before_count = Column(Integer, nullable=False)
    after_count = Column(Integer, nullable=False)
    before_kind_count = Column(Integer, nullable=False)
    after_kind_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


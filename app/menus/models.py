from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class OrderSheetQuestion(Base):
    """LLM 생성 주문서 질문 후보 풀 및 ML 중요도 가중치 테이블"""
    __tablename__ = "order_sheet_questions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    question_code = Column(String(80), unique=True, index=True, nullable=False)
    cluster_key = Column(String(50), index=True, nullable=False)
    question_text = Column(String(300), nullable=False)

    # 각 선택지(option_id, label, rules)를 담은 JSON 배열
    options_json = Column(JSON, nullable=False)

    # 3단계: 데이터 0건일 때 음식 분포로부터 계산한 초기 정보 이득 점수 (0.0 ~ 1.0)
    info_gain_score = Column(Float, nullable=False, default=0.5)

    # 4~5단계: 개발팀 사전 피드백 및 실사용자 피드백 재학습으로 갱신되는 중요도 가중치
    ml_weight = Column(Float, index=True, nullable=False, default=0.5)

    shown_count = Column(Integer, nullable=False, default=0)
    positive_count = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, index=True, nullable=False, default=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),)


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
    """추천 결과에 대한 사용자 피드백 및 ML 재학습용 특징 매칭 벡터 로그 테이블"""
    __tablename__ = "recommendation_feedbacks"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    recommendation_id = Column(
        Integer,
        ForeignKey("recommendations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    selected_menu_id = Column(Integer, ForeignKey("menus.id", ondelete="SET NULL"), nullable=True)
    is_satisfied = Column(Boolean, nullable=False)

    # {question_id_str: 1.0 (매칭됨) or 0.0 (불일치)} 형태의 학습용 입력 벡터 X
    feature_match_vector = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


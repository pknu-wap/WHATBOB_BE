from pathlib import Path
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedback,
    UserFoodExclusion,
)
from app.recommend.common import NoAvailableFoodException
from app.recommend.router import (
    adjust_order_sheet_weights,
    get_order_sheet_questions,
    submit_order_sheet,
    submit_recommendation_feedback,
)
from app.recommend.schemas import (
    AnswerItemRequest,
    OrderSheetRecommendRequest,
    OrderSheetWeightTuneRequest,
    RecommendationFeedbackRequest,
    WeightAdjustmentItem,
)
from app.users.models import User
from scripts.seed_menus import (
    DEFAULT_CSV_PATH,
    build_candidate_question_pool,
    parse_and_clean_csv,
    seed_database,
)


@pytest.fixture(scope="module")
def seeded_engine():
    """인메모리 SQLite 엔진을 생성하고 실제 CSV 상위 120개 식사 메뉴와 200+ 질문 풀을 1회 적재합니다."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = TestingSessionLocal()
    try:
        summary = seed_database(
            db=session,
            csv_path=Path(DEFAULT_CSV_PATH),
            max_menus=120,
            include_beverages_and_bakery=False,
            use_llm=False,
        )
        assert summary["inserted_menus"] == 120
        assert summary["total_questions_in_pool"] >= 190
    finally:
        session.close()
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def seeded_db(seeded_engine):
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=seeded_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_csv_parser_and_large_question_pool_generation():
    """1~2단계: CSV 파싱으로 정규화 속성이 생성되고 200개 이상의 질문+조건식 풀이 만들어지는지 검증"""
    parsed = parse_and_clean_csv(
        csv_path=Path(DEFAULT_CSV_PATH),
        max_menus=50,
        include_beverages_and_bakery=False,
    )
    assert len(parsed) == 50
    sample = parsed[0]
    assert sample["meal_type"] == "main_meal"
    assert isinstance(sample["has_soup"], bool)
    assert sample["cooking_style"] in {
        "rice", "noodle", "soup_stew", "grill_stir", "steamed_braised", "fried_pan", "raw_salad", "other"
    }

    pool = build_candidate_question_pool()
    assert len(pool) >= 190
    for q in pool:
        assert "question_code" in q
        assert "cluster_key" in q
        assert len(q["options"]) == 3
        assert 1 <= len(q["question_text"]) <= 24


def test_get_official_allergens_returns_19_legal_allergens():
    """식품 등의 표시·광고에 관한 법률 시행규칙 [별표 2] 기준 식약처 고시 19대 알레르기 유발물질 목록 반환 검증"""
    from app.recommend.router import get_official_allergens
    resp = get_official_allergens()
    assert resp.success is True
    data = resp.data
    assert data.total_count == 19
    assert len(data.allergens) == 19

    ids = [item.id for item in data.allergens]
    for required_id in ["egg", "milk", "buckwheat", "peanut", "soybean", "wheat", "mackerel", "crab", "shrimp", "pork", "peach", "tomato", "sulfite", "walnut", "chicken", "beef", "squid", "shellfish", "pine_nut"]:
        assert required_id in ids


def test_get_order_sheet_questions_returns_top7_and_explore2(seeded_db):
    """3단계 & 5단계: 질문 조회 시 가중치 상위 7개(서로 다른 클러스터 우선) + 후보 탐색 2개 = 총 9개 반환 검증"""
    resp = get_order_sheet_questions(total_count=9, exploitation_count=7, db=seeded_db)
    assert resp.success is True

    data = resp.data
    assert data.total_count == 9
    assert data.exploitation_count == 7
    assert data.exploration_count == 2

    questions = data.questions
    assert len(questions) == 9
    exploit_qs = [q for q in questions if not q.is_exploration]
    explore_qs = [q for q in questions if q.is_exploration]
    assert len(exploit_qs) == 7
    assert len(explore_qs) == 2

    exploit_clusters = {q.cluster_key for q in exploit_qs}
    assert len(exploit_clusters) == 7


def test_order_sheet_excludes_allergies_and_disliked_foods(seeded_db):
    """AGENTS.md 도메인 규칙: 사용자의 알레르기/섭취불가/비선호 음식은 추천 후보에서 선제 제외되는지 검증"""
    user = User(kakao_id=999001, nickname="알레르기테스터")
    seeded_db.add(user)
    seeded_db.commit()
    seeded_db.refresh(user)

    seeded_db.add_all([
        UserFoodExclusion(user_id=user.id, exclusion_type="ALLERGY", keyword="해산물"),
        UserFoodExclusion(user_id=user.id, exclusion_type="DISLIKE", keyword="홍어"),
    ])
    seeded_db.commit()

    q_soup = (
        seeded_db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.cluster_key == "has_soup")
        .first()
    )
    q_ing = (
        seeded_db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.cluster_key == "main_ingredient")
        .first()
    )

    req = OrderSheetRecommendRequest(
        user_id=user.id,
        exclusions=["훠궈"],
        answers=[
            AnswerItemRequest(question_id=q_soup.id, option_id="soup_yes"),
            AnswerItemRequest(question_id=q_ing.id, option_id="non_meat"),
        ],
        top_k=5,
    )
    resp = submit_order_sheet(request=req, db=seeded_db)
    assert resp.success is True
    data = resp.data
    assert data.excluded_count > 0

    for menu in data.recommended_menus:
        assert "해산물" not in menu.name
        assert "홍어" not in menu.name
        assert "훠궈" not in menu.name
        menu_entity = seeded_db.query(Menu).filter(Menu.id == menu.menu_id).first()
        assert "해산물" not in (menu_entity.allergen_tags or [])


def test_order_sheet_raises_when_no_candidates_remain(seeded_db):
    """AGENTS.md 도메인 규칙: 추천 후보가 없을 때 제외 조건을 임의로 해제하지 않고 NoAvailableFoodException 발생 검증"""
    all_base_names = {m.base_name for m in seeded_db.query(Menu).all()}
    q_first = seeded_db.query(OrderSheetQuestion).first()

    req = OrderSheetRecommendRequest(
        exclusions=list(all_base_names),
        answers=[
            AnswerItemRequest(
                question_id=q_first.id,
                option_id=q_first.options_json[0]["option_id"],
            )
        ],
    )
    with pytest.raises(NoAvailableFoodException) as exc_info:
        submit_order_sheet(request=req, db=seeded_db)
    assert exc_info.value.status_code == 404
    assert "추천 가능 메뉴가 없습니다" in exc_info.value.message


def test_feedback_accumulation_and_weight_recalculation(seeded_db):
    """4~5단계: 누적 사용자 피드백 통계로 질문 중요도가 재계산되는지 검증"""
    q_soup = (
        seeded_db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.cluster_key == "has_soup")
        .order_by(OrderSheetQuestion.importance_weight.desc())
        .first()
    )
    q_temp = (
        seeded_db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.cluster_key == "temperature")
        .order_by(OrderSheetQuestion.importance_weight.desc())
        .first()
    )
    initial_soup_weight = float(q_soup.importance_weight)

    rec_resp = submit_order_sheet(
        request=OrderSheetRecommendRequest(
            answers=[
                AnswerItemRequest(question_id=q_soup.id, option_id="soup_yes"),
                AnswerItemRequest(question_id=q_temp.id, option_id="hot"),
            ],
            top_k=3,
        ),
        db=seeded_db,
    )
    assert rec_resp.success is True
    rec_id = rec_resp.data.recommendation_id
    top_menu_id = rec_resp.data.recommended_menus[0].menu_id

    for _ in range(4):
        fb_resp = submit_recommendation_feedback(
            recommendation_id=rec_id,
            request=RecommendationFeedbackRequest(
                selected_menu_id=top_menu_id,
                is_satisfied=True,
            ),
            db=seeded_db,
        )
        assert fb_resp.success is True

    seeded_db.refresh(q_soup)
    assert q_soup.positive_count >= 4
    assert float(q_soup.importance_weight) != initial_soup_weight
    assert seeded_db.query(Recommendation).count() >= 1
    assert seeded_db.query(RecommendationFeedback).count() >= 4


def test_developer_weight_tuning_endpoint(seeded_db):
    """4단계: 개발팀이 특정 질문의 가중치를 직접 조정하거나 증감(feedback_delta)해 서버에 반영하는 기능 검증"""
    target_q = (
        seeded_db.query(OrderSheetQuestion)
        .order_by(OrderSheetQuestion.importance_weight.asc())
        .first()
    )

    resp = adjust_order_sheet_weights(
        request=OrderSheetWeightTuneRequest(
            adjustments=[
                WeightAdjustmentItem(question_id=target_q.id, importance_weight=1.95)
            ],
            recalculate_from_feedback=False,
        ),
        db=seeded_db,
    )
    assert resp.success is True
    assert resp.data.updated_count == 1
    top_q_ids = [q.id for q in resp.data.top_questions]
    assert top_q_ids[0] == target_q.id
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedback,
    UserFoodExclusion,
)
from app.recommend.common import NoAvailableFoodException
from app.recommend.router import (
    adjust_order_sheet_weights,
    get_order_sheet_questions,
    submit_order_sheet,
    submit_recommendation_feedback,
)
from app.recommend.schemas import (
    AnswerItemRequest,
    OrderSheetRecommendRequest,
    OrderSheetWeightTuneRequest,
    RecommendationFeedbackRequest,
    WeightAdjustmentItem,
)
from app.users.models import User
from scripts.seed_menus import (
    DEFAULT_CSV_PATH,
    build_candidate_question_pool,
    parse_and_clean_csv,
    seed_database,
)


@pytest.fixture(scope="module")
def seeded_engine():
    """인메모리 SQLite 엔진을 생성하고 실제 CSV 상위 120개 식사 메뉴와 200+ 질문 풀을 1회 적재합니다."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = TestingSessionLocal()
    try:
        summary = seed_database(
            db=session,
            csv_path=Path(DEFAULT_CSV_PATH),
            max_menus=120,
            include_beverages_and_bakery=False,
            use_llm=False,
        )
        assert summary["inserted_menus"] == 120
        assert summary["total_questions_in_pool"] >= 190
    finally:
        session.close()
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def seeded_db(seeded_engine):
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=seeded_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_csv_parser_and_large_question_pool_generation():
    """1~2단계: CSV 파싱으로 정규화 속성이 생성되고 200개 이상의 질문+조건식 풀이 만들어지는지 검증"""
    parsed = parse_and_clean_csv(
        csv_path=Path(DEFAULT_CSV_PATH),
        max_menus=50,
        include_beverages_and_bakery=False,
    )
    assert len(parsed) == 50
    sample = parsed[0]
    assert sample["meal_type"] == "main_meal"
    assert isinstance(sample["has_soup"], bool)
    assert sample["cooking_style"] in {
        "rice", "noodle", "soup_stew", "grill_stir", "steamed_braised", "fried_pan", "raw_salad", "other"
    }

    pool = build_candidate_question_pool()
    assert len(pool) >= 190
    for q in pool:
        assert "question_code" in q
        assert "cluster_key" in q
        assert len(q["options"]) == 3
        assert 1 <= len(q["question_text"]) <= 24


def test_get_official_allergens_returns_19_legal_allergens():
    """식품 등의 표시·광고에 관한 법률 시행규칙 [별표 2] 기준 식약처 고시 19대 알레르기 유발물질 목록 반환 검증"""
    from app.recommend.router import get_official_allergens
    resp = get_official_allergens()
    assert resp.success is True
    data = resp.data
    assert data.total_count == 19
    assert len(data.allergens) == 19

    ids = [item.id for item in data.allergens]
    for required_id in ["egg", "milk", "buckwheat", "peanut", "soybean", "wheat", "mackerel", "crab", "shrimp", "pork", "peach", "tomato", "sulfite", "walnut", "chicken", "beef", "squid", "shellfish", "pine_nut"]:
        assert required_id in ids


def test_get_order_sheet_questions_returns_top7_and_explore2(seeded_db):
    """3단계 & 5단계: 질문 조회 시 가중치 상위 7개(서로 다른 클러스터 우선) + 후보 탐색 2개 = 총 9개 반환 검증"""
    resp = get_order_sheet_questions(total_count=9, exploitation_count=7, db=seeded_db)
    assert resp.success is True

    data = resp.data
    assert data.total_count == 9
    assert data.exploitation_count == 7
    assert data.exploration_count == 2

    questions = data.questions
    assert len(questions) == 9
    exploit_qs = [q for q in questions if not q.is_exploration]
    explore_qs = [q for q in questions if q.is_exploration]
    assert len(exploit_qs) == 7
    assert len(explore_qs) == 2

    exploit_clusters = {q.cluster_key for q in exploit_qs}
    assert len(exploit_clusters) == 7


def test_order_sheet_excludes_allergies_and_disliked_foods(seeded_db):
    """AGENTS.md 도메인 규칙: 사용자의 알레르기/섭취불가/비선호 음식은 추천 후보에서 선제 제외되는지 검증"""
    user = User(kakao_id=999001, nickname="알레르기테스터")
    seeded_db.add(user)
    seeded_db.commit()
    seeded_db.refresh(user)

    seeded_db.add_all([
        UserFoodExclusion(user_id=user.id, exclusion_type="ALLERGY", keyword="해산물"),
        UserFoodExclusion(user_id=user.id, exclusion_type="DISLIKE", keyword="홍어"),
    ])
    seeded_db.commit()

    q_soup = (
        seeded_db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.cluster_key == "has_soup")
        .first()
    )
    q_ing = (
        seeded_db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.cluster_key == "main_ingredient")
        .first()
    )

    req = OrderSheetRecommendRequest(
        user_id=user.id,
        exclusions=["훠궈"],
        answers=[
            AnswerItemRequest(question_id=q_soup.id, option_id="soup_yes"),
            AnswerItemRequest(question_id=q_ing.id, option_id="non_meat"),
        ],
        top_k=5,
    )
    resp = submit_order_sheet(request=req, db=seeded_db)
    assert resp.success is True
    data = resp.data
    assert data.excluded_count > 0

    for menu in data.recommended_menus:
        assert "해산물" not in menu.name
        assert "홍어" not in menu.name
        assert "훠궈" not in menu.name
        menu_entity = seeded_db.query(Menu).filter(Menu.id == menu.menu_id).first()
        assert "해산물" not in (menu_entity.allergen_tags or [])


def test_order_sheet_raises_when_no_candidates_remain(seeded_db):
    """AGENTS.md 도메인 규칙: 추천 후보가 없을 때 제외 조건을 임의로 해제하지 않고 NoAvailableFoodException 발생 검증"""
    all_base_names = {m.base_name for m in seeded_db.query(Menu).all()}
    q_first = seeded_db.query(OrderSheetQuestion).first()

    req = OrderSheetRecommendRequest(
        exclusions=list(all_base_names),
        answers=[
            AnswerItemRequest(
                question_id=q_first.id,
                option_id=q_first.options_json[0]["option_id"],
            )
        ],
    )
    with pytest.raises(NoAvailableFoodException) as exc_info:
        submit_order_sheet(request=req, db=seeded_db)
    assert exc_info.value.status_code == 404
    assert "추천 가능 메뉴가 없습니다" in exc_info.value.message


def test_feedback_accumulation_and_weight_recalculation(seeded_db):
    """4~5단계: 누적 사용자 피드백 통계로 질문 중요도가 재계산되는지 검증"""
    q_soup = (
        seeded_db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.cluster_key == "has_soup")
        .order_by(OrderSheetQuestion.importance_weight.desc())
        .first()
    )
    q_temp = (
        seeded_db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.cluster_key == "temperature")
        .order_by(OrderSheetQuestion.importance_weight.desc())
        .first()
    )
    initial_soup_weight = float(q_soup.importance_weight)

    rec_resp = submit_order_sheet(
        request=OrderSheetRecommendRequest(
            answers=[
                AnswerItemRequest(question_id=q_soup.id, option_id="soup_yes"),
                AnswerItemRequest(question_id=q_temp.id, option_id="hot"),
            ],
            top_k=3,
        ),
        db=seeded_db,
    )
    assert rec_resp.success is True
    rec_id = rec_resp.data.recommendation_id
    top_menu_id = rec_resp.data.recommended_menus[0].menu_id

    for _ in range(4):
        fb_resp = submit_recommendation_feedback(
            recommendation_id=rec_id,
            request=RecommendationFeedbackRequest(
                selected_menu_id=top_menu_id,
                is_satisfied=True,
            ),
            db=seeded_db,
        )
        assert fb_resp.success is True

    seeded_db.refresh(q_soup)
    assert q_soup.positive_count >= 4
    assert float(q_soup.importance_weight) != initial_soup_weight
    assert seeded_db.query(Recommendation).count() >= 1
    assert seeded_db.query(RecommendationFeedback).count() >= 4


def test_developer_weight_tuning_endpoint(seeded_db):
    """4단계: 개발팀이 특정 질문의 가중치를 직접 조정하거나 증감(feedback_delta)해 서버에 반영하는 기능 검증"""
    target_q = (
        seeded_db.query(OrderSheetQuestion)
        .order_by(OrderSheetQuestion.importance_weight.asc())
        .first()
    )

    resp = adjust_order_sheet_weights(
        request=OrderSheetWeightTuneRequest(
            adjustments=[
                WeightAdjustmentItem(question_id=target_q.id, importance_weight=1.95)
            ],
            recalculate_from_feedback=False,
        ),
        db=seeded_db,
    )
    assert resp.success is True
    assert resp.data.updated_count == 1
    top_q_ids = [q.id for q in resp.data.top_questions]
    assert top_q_ids[0] == target_q.id


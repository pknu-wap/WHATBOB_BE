"""Spec #77 / Ticket #78: 개인 주문서 9문항 발급, 1/N 엔트로피·커버리지 순위, 알레르기·의미 중복 필터링 및 발급 생명주기 테스트."""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base
from app.menus.models import (
    PERSONAL_RETRY_LIST,
    Menu,
    OrderSheetIssuance,
    OrderSheetQuestion,
    QuestionSelectionState,
    Recommendation,
    UserFoodExclusion,
)
from app.recommend.common import evaluate_option_rules
from app.recommend.feedback import _question_options as feedback_question_options
from app.recommend.order_sheet import (
    _find_valid_order_sheet_issuance,
    calculate_question_entropy_score,
    calculate_question_ranking_metrics,
    create_order_sheet_recommendation,
    issue_order_sheet_questions,
    rank_initial_question_candidates,
    resolve_question_role,
    select_order_sheet_questions,
)
from app.recommend.question_pool import QUESTION_OPTION_COUNT, build_candidate_question_pool
from app.recommend.schemas import (
    AnswerItemRequest,
    OrderSheetQuestionDTO,
    OrderSheetRecommendRequest,
)
from app.users.models import User


def test_server_settings_expose_order_sheet_defaults_and_role_overrides(monkeypatch):
    assert settings.ORDER_SHEET_QUESTION_COUNT == 9
    assert settings.ORDER_SHEET_ENTROPY_WEIGHT == pytest.approx(0.75)
    assert settings.ORDER_SHEET_COVERAGE_WEIGHT == pytest.approx(0.25)

    for cluster in ("has_soup", "cooking_style", "main_ingredient"):
        assert resolve_question_role(question_id=100, cluster_key=cluster) == "hard_filter"
    for cluster in (
        "sodium_level",
        "heaviness",
        "flavor_intensity",
        "origin_type",
        "carb_level",
        "protein_level",
    ):
        assert resolve_question_role(question_id=100, cluster_key=cluster) == "score"

    monkeypatch.setattr(
        settings,
        "ORDER_SHEET_QUESTION_ROLE_OVERRIDES",
        {101: "hard_filter", 102: "score"},
    )
    assert resolve_question_role(question_id=101, cluster_key="flavor_intensity") == "hard_filter"
    assert resolve_question_role(question_id=102, cluster_key="has_soup") == "score"


def test_q8_cooking_style_has_four_mutually_exclusive_options_and_dto_allows_four():
    assert QUESTION_OPTION_COUNT == 4
    pool = build_candidate_question_pool()
    cooking_questions = [q for q in pool if q["cluster_key"] == "cooking_style"]
    assert len(cooking_questions) == 17

    q8 = cooking_questions[0]
    assert [opt["option_id"] for opt in q8["options"]] == ["rice", "noodle", "other_style", "any"]
    assert [opt["label"] for opt in q8["options"]] == ["밥", "면", "그 외", "상관없어요"]
    assert q8["options"][-1]["rules"] == []

    specific_options = [opt for opt in q8["options"] if opt["rules"]]
    for style, expected_opt_id in (
        ("rice", "rice"),
        ("noodle", "noodle"),
        ("soup_stew", "other_style"),
        ("fried_pan", "other_style"),
        ("bakery", "other_style"),
    ):
        menu = Menu(cooking_style=style)
        matched = [opt["option_id"] for opt in specific_options if evaluate_option_rules(menu, opt["rules"])]
        assert matched == [expected_opt_id]

    missing_menu = Menu(cooking_style=None)
    assert [opt["option_id"] for opt in specific_options if evaluate_option_rules(missing_menu, opt["rules"])] == []

    dto = OrderSheetQuestionDTO(
        id=8,
        question_code=q8["question_code"],
        cluster_key=q8["cluster_key"],
        question_text=q8["question_text"],
        options=q8["options"],
        initial_score=0.7,
        importance_weight=0.7,
    )
    assert len(dto.options) == 4
    dumped = dto.model_dump(by_alias=True)
    assert dumped["info_gain_score"] == 0.7
    assert dumped["ml_weight"] == 0.7

    for invalid_count in (1, 5):
        with pytest.raises(ValidationError):
            OrderSheetQuestionDTO(
                id=8,
                question_code=q8["question_code"],
                cluster_key=q8["cluster_key"],
                question_text=q8["question_text"],
                options=[q8["options"][0]] * invalid_count,
                initial_score=0.7,
                importance_weight=0.7,
            )


def _question_by_cluster(cluster_key: str) -> dict:
    return next(q for q in build_candidate_question_pool() if q["cluster_key"] == cluster_key)


def test_food_kind_one_over_n_voting_weights_each_kind_equally():
    # Q18 worked example: Pizza (100 items: 20 spicy, 80 mild) + Bibimbap (1 item: spicy)
    # Kind votes: spicy = 0.2 + 1.0 = 1.2 (60%), not_spicy = 0.8 (40%), coverage = 1.0
    # Normalized entropy = -(0.6*log2(0.6) + 0.4*log2(0.4)) = 0.970950594...
    # Weighted score (75/25) = 0.75 * 0.970950594 + 0.25 * 1.0 = 0.9782
    menus = (
        [Menu(base_name="피자", name=f"피자_매운{i}", flavor_intensity="spicy_bold") for i in range(20)]
        + [Menu(base_name="피자", name=f"피자_순한{i}", flavor_intensity="mild_clean") for i in range(80)]
        + [Menu(base_name="비빔밥", name="비빔밥_매운", flavor_intensity="spicy_bold")]
    )
    spicy_q = _question_by_cluster("flavor_intensity")

    metrics = calculate_question_ranking_metrics(menus, spicy_q["options"])
    assert metrics.is_degenerate is False
    assert metrics.total_kind_votes == pytest.approx(2.0)
    assert metrics.coverage_ratio == pytest.approx(1.0)
    assert metrics.normalized_entropy == pytest.approx(0.97095, abs=1e-4)
    assert metrics.score == pytest.approx(0.9782, abs=1e-4)
    assert calculate_question_entropy_score(menus, spicy_q["options"]) == pytest.approx(0.9782, abs=1e-4)


def test_missing_attributes_stay_in_denominator_only_and_lower_coverage(monkeypatch):
    # Q19 worked example: Pizza (100 items: 1 spicy, 1 mild, 98 missing)
    # Votes: spicy = 0.01, not_spicy = 0.01 -> total_matched = 0.02, coverage = 0.02
    # Normalized entropy = 1.0, score (75/25) = 0.75 * 1.0 + 0.25 * 0.02 = 0.7550
    sparse_menus = (
        [Menu(base_name="피자", name="피자_매운", flavor_intensity="spicy_bold")]
        + [Menu(base_name="피자", name="피자_순한", flavor_intensity="mild_clean")]
        + [Menu(base_name="피자", name=f"피자_미입력{i}", flavor_intensity=None) for i in range(98)]
    )
    full_menus = (
        [Menu(base_name="피자", name=f"피자_매운{i}", flavor_intensity="spicy_bold") for i in range(50)]
        + [Menu(base_name="피자", name=f"피자_순한{i}", flavor_intensity="mild_clean") for i in range(50)]
    )
    spicy_q = _question_by_cluster("flavor_intensity")

    sparse_metrics = calculate_question_ranking_metrics(sparse_menus, spicy_q["options"])
    full_metrics = calculate_question_ranking_metrics(full_menus, spicy_q["options"])

    assert sparse_metrics.normalized_entropy == pytest.approx(1.0)
    assert sparse_metrics.coverage_ratio == pytest.approx(0.02)
    assert sparse_metrics.score == pytest.approx(0.7550, abs=1e-4)
    assert full_metrics.score == pytest.approx(1.0)
    assert sparse_metrics.score < full_metrics.score

    monkeypatch.setattr(settings, "ORDER_SHEET_ENTROPY_WEIGHT", 0.25)
    monkeypatch.setattr(settings, "ORDER_SHEET_COVERAGE_WEIGHT", 0.75)
    reweighted = calculate_question_ranking_metrics(sparse_menus, spicy_q["options"])
    assert reweighted.score == pytest.approx(0.2650, abs=1e-4)


def test_four_option_q8_entropy_and_canonical_food_kind_grouping_and_degenerate_exclusion():
    # Canonical aliases 돈까스/돈가스 share one FoodKind (tonkatsu): each gets 0.5 vote
    # Total 3 food kinds: 비빔밥 (rice=1.0), 라멘 (noodle=1.0), tonkatsu (other_style=0.5+0.5=1.0)
    menus = [
        Menu(base_name="비빔밥", name="비빔밥", cooking_style="rice", meal_type="main_meal"),
        Menu(base_name="라멘", name="라멘", cooking_style="noodle", meal_type="main_meal"),
        Menu(base_name="돈까스", name="등심돈까스", cooking_style="fried_pan", meal_type="main_meal"),
        Menu(base_name="돈가스", name="치즈돈가스", cooking_style="fried_pan", meal_type="main_meal"),
    ]
    q8 = _question_by_cluster("cooking_style")
    meal_q = _question_by_cluster("meal_type")

    q8_metrics = calculate_question_ranking_metrics(menus, q8["options"])
    assert q8_metrics.total_kind_votes == pytest.approx(3.0)
    assert q8_metrics.normalized_entropy == pytest.approx(1.0)
    assert q8_metrics.coverage_ratio == pytest.approx(1.0)
    assert q8_metrics.score == pytest.approx(1.0)

    meal_metrics = calculate_question_ranking_metrics(menus, meal_q["options"])
    assert meal_metrics.is_degenerate is True
    assert meal_metrics.score == 0.0

    ranked = rank_initial_question_candidates(menus, [meal_q, q8])
    assert [item["cluster_key"] for item in ranked] == ["cooking_style"]


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed_diverse_menus_and_full_pool(db: Session) -> list[Menu]:
    """모든 11개 유효 주제(밀·육류 제외 후에도 9개 이상)가 실제 후보를 구분하도록 메뉴와 질문 풀을 적재한다."""
    menus = [
        Menu(
            food_code="S001", name="김치찌개", base_name="김치찌개", category_large="찌개류",
            meal_type="main_meal", has_soup=True, cooking_style="soup_stew", main_ingredient="pork",
            flavor_intensity="spicy_bold", temperature="hot", heaviness="heavy",
            kcal=750.0, protein_g=22.0, carb_g=20.0, sugar_g=4.0, sodium_mg=900.0,
            origin_type="home_style", allergen_tags=["돼지고기"],
        ),
        Menu(
            food_code="S002", name="비빔밥", base_name="비빔밥", category_large="밥류",
            meal_type="main_meal", has_soup=False, cooking_style="rice", main_ingredient="veggie_tofu",
            flavor_intensity="spicy_bold", temperature="hot", heaviness="light",
            kcal=520.0, protein_g=14.0, carb_g=65.0, sugar_g=5.0, sodium_mg=480.0,
            origin_type="home_style", allergen_tags=[],
        ),
        Menu(
            food_code="S003", name="우동", base_name="우동", category_large="면류",
            meal_type="main_meal", has_soup=True, cooking_style="noodle", main_ingredient="seafood",
            flavor_intensity="mild_clean", temperature="hot", heaviness="light",
            kcal=480.0, protein_g=9.0, carb_g=70.0, sugar_g=2.0, sodium_mg=800.0,
            origin_type="restaurant", allergen_tags=["밀"],
        ),
        Menu(
            food_code="S004", name="냉모밀두부샐러드", base_name="두부샐러드", category_large="샐러드",
            meal_type="main_meal", has_soup=False, cooking_style="raw_salad", main_ingredient="veggie_tofu",
            flavor_intensity="mild_clean", temperature="cold", heaviness="light",
            kcal=310.0, protein_g=8.0, carb_g=10.0, sugar_g=1.5, sodium_mg=180.0,
            origin_type="franchise", allergen_tags=[],
        ),
        Menu(
            food_code="S005", name="해물순두부찌개", base_name="순두부찌개", category_large="찌개류",
            meal_type="main_meal", has_soup=True, cooking_style="soup_stew", main_ingredient="seafood",
            flavor_intensity="spicy_bold", temperature="hot", heaviness="heavy",
            kcal=720.0, protein_g=18.0, carb_g=12.0, sugar_g=2.0, sodium_mg=760.0,
            origin_type="restaurant", allergen_tags=["해산물"],
        ),
        Menu(
            food_code="S006", name="연어포케", base_name="포케", category_large="밥류",
            meal_type="main_meal", has_soup=False, cooking_style="rice", main_ingredient="seafood",
            flavor_intensity="mild_clean", temperature="cold", heaviness="heavy",
            kcal=740.0, protein_g=25.0, carb_g=45.0, sugar_g=6.0, sodium_mg=220.0,
            origin_type="franchise", allergen_tags=["해산물"],
        ),
        Menu(
            food_code="S007", name="소고기불고기", base_name="불고기", category_large="구이류",
            meal_type="main_meal", has_soup=False, cooking_style="grill_stir", main_ingredient="beef",
            flavor_intensity="sweet_savory", temperature="hot", heaviness="heavy",
            kcal=780.0, protein_g=30.0, carb_g=18.0, sugar_g=8.0, sodium_mg=620.0,
            origin_type="home_style", allergen_tags=["쇠고기(소고기)"],
        ),
        Menu(
            food_code="S008", name="닭가슴살볶음", base_name="닭볶음", category_large="볶음류",
            meal_type="main_meal", has_soup=False, cooking_style="grill_stir", main_ingredient="poultry",
            flavor_intensity="mild_clean", temperature="hot", heaviness="light",
            kcal=430.0, protein_g=32.0, carb_g=11.0, sugar_g=1.0, sodium_mg=210.0,
            origin_type="cafeteria", allergen_tags=["닭고기"],
        ),
        Menu(
            food_code="S009", name="버섯들깨탕", base_name="버섯탕", category_large="국류",
            meal_type="main_meal", has_soup=True, cooking_style="soup_stew", main_ingredient="veggie_tofu",
            flavor_intensity="mild_clean", temperature="hot", heaviness="heavy",
            kcal=710.0, protein_g=16.0, carb_g=14.0, sugar_g=2.5, sodium_mg=230.0,
            origin_type="home_style", allergen_tags=[],
        ),
        Menu(
            food_code="S010", name="오이냉국밥", base_name="냉국밥", category_large="국류",
            meal_type="main_meal", has_soup=True, cooking_style="rice", main_ingredient="veggie_tofu",
            flavor_intensity="mild_clean", temperature="cold", heaviness="light",
            kcal=410.0, protein_g=7.0, carb_g=58.0, sugar_g=4.5, sodium_mg=490.0,
            origin_type="restaurant", allergen_tags=[],
        ),
    ]
    db.add_all(menus)

    pool = build_candidate_question_pool()
    seen: set[str] = set()
    for idx, q in enumerate(pool):
        # 각 클러스터당 2개 변형 적재
        key = q["cluster_key"]
        if sum(1 for s in seen if s.startswith(f"{key}:")) >= 2:
            continue
        seen.add(f"{key}:{idx}")
        db.add(
            OrderSheetQuestion(
                question_code=q["question_code"],
                cluster_key=key,
                question_text=q["question_text"],
                options_json=q["options"],
                initial_score=0.8,
                importance_weight=0.8,
                is_active=True,
            )
        )
    db.commit()
    return menus


def test_default_selection_returns_spec_initial_nine_with_q8_four_options(db):
    _seed_diverse_menus_and_full_pool(db)
    user = User(kakao_id=880001, nickname="기본9문항유저")
    db.add(user)
    db.commit()

    result = select_order_sheet_questions(db, user_id=user.id)
    assert result.total_count == 9
    assert [q.cluster_key for q in result.questions] == [
        "sodium_level",
        "heaviness",
        "has_soup",
        "flavor_intensity",
        "origin_type",
        "carb_level",
        "protein_level",
        "cooking_style",
        "main_ingredient",
    ]
    q8 = result.questions[7]
    assert q8.cluster_key == "cooking_style"
    assert [opt.option_id for opt in q8.options] == ["rice", "noodle", "other_style", "any"]
    assert [opt.label for opt in q8.options] == ["밥", "면", "그 외", "상관없어요"]


def test_wheat_and_multiple_meat_allergies_omit_inappropriate_topics_and_fill_nine(db):
    _seed_diverse_menus_and_full_pool(db)
    wheat_user = User(kakao_id=880002, nickname="밀알레르기유저")
    multi_user = User(kakao_id=880003, nickname="복수알레르기유저")
    db.add_all([wheat_user, multi_user])
    db.flush()

    db.add(UserFoodExclusion(user_id=wheat_user.id, exclusion_type="ALLERGY", keyword="밀", catalog_code="wheat"))
    db.add_all([
        UserFoodExclusion(user_id=multi_user.id, exclusion_type="ALLERGY", keyword="밀", catalog_code="wheat"),
        UserFoodExclusion(user_id=multi_user.id, exclusion_type="ALLERGY", keyword="돼지고기", catalog_code="pork"),
        UserFoodExclusion(user_id=multi_user.id, exclusion_type="ALLERGY", keyword="쇠고기(소고기)", catalog_code="beef"),
        UserFoodExclusion(user_id=multi_user.id, exclusion_type="UNABLE", keyword="닭·가금류", catalog_code="poultry"),
    ])
    db.commit()

    wheat_res = select_order_sheet_questions(db, user_id=wheat_user.id)
    wheat_clusters = [q.cluster_key for q in wheat_res.questions]
    assert len(wheat_clusters) == 9
    assert len(set(wheat_clusters)) == 9
    assert "cooking_style" not in wheat_clusters
    assert "meal_type" not in wheat_clusters

    multi_res = select_order_sheet_questions(db, user_id=multi_user.id)
    multi_clusters = [q.cluster_key for q in multi_res.questions]
    assert len(multi_clusters) == 9
    assert len(set(multi_clusters)) == 9
    assert "cooking_style" not in multi_clusters
    assert "main_ingredient" not in multi_clusters
    assert {"sugar_level", "temperature"}.issubset(set(multi_clusters))


def test_semantic_duplicates_rephrasing_opposite_and_subtopic_are_excluded(db):
    _seed_diverse_menus_and_full_pool(db)
    # 의미 중복 후보 3종을 매우 높은 가중치로 추가:
    # 1) 재표현 (has_soup 다른 cluster_key)
    # 2) 반대 방향 (heaviness eq heavy '든든한 식사')
    # 3) 좁은 하위 주제 (main_ingredient eq pork '돼지고기 요리')
    db.add_all([
        OrderSheetQuestion(
            question_code="custom_soup_rephrase",
            cluster_key="broth_preference",
            question_text="따끈한 국물 요리가 당기나요?",
            options_json=[
                {"option_id": "broth_yes", "label": "예", "rules": [{"field": "has_soup", "op": "eq", "value": True}]},
                {"option_id": "broth_no", "label": "아니요", "rules": [{"field": "has_soup", "op": "eq", "value": False}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
            initial_score=0.99,
            importance_weight=2.9,
            is_active=True,
        ),
        OrderSheetQuestion(
            question_code="custom_filling_opposite",
            cluster_key="filling_meal",
            question_text="든든한 식사가 당기나요?",
            options_json=[
                {"option_id": "heavy_yes", "label": "예", "rules": [{"field": "heaviness", "op": "eq", "value": "heavy"}]},
                {"option_id": "heavy_no", "label": "아니요", "rules": [{"field": "heaviness", "op": "neq", "value": "heavy"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
            initial_score=0.99,
            importance_weight=2.8,
            is_active=True,
        ),
        OrderSheetQuestion(
            question_code="custom_pork_subtopic",
            cluster_key="pork_preference",
            question_text="돼지고기 요리를 먹고 싶나요?",
            options_json=[
                {"option_id": "pork_yes", "label": "예", "rules": [{"field": "main_ingredient", "op": "eq", "value": "pork"}]},
                {"option_id": "pork_no", "label": "아니요", "rules": [{"field": "main_ingredient", "op": "neq", "value": "pork"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
            initial_score=0.99,
            importance_weight=2.7,
            is_active=True,
        ),
    ])
    db.commit()

    user = User(kakao_id=880004, nickname="의미중복유저")
    db.add(user)
    db.commit()

    result = select_order_sheet_questions(db, user_id=user.id)
    codes = {q.question_code for q in result.questions}
    clusters = [q.cluster_key for q in result.questions]
    assert len(clusters) == 9
    assert len(set(clusters)) == 9
    assert "custom_soup_rephrase" not in codes
    assert "custom_filling_opposite" not in codes
    assert "custom_pork_subtopic" not in codes


def test_select_order_sheet_questions_respects_commit_false(db):
    _seed_diverse_menus_and_full_pool(db)
    result = select_order_sheet_questions(
        db,
        total_count=9,
        exploitation_count=7,
        scope_key="tx:rollback-check",
        commit=False,
    )
    assert result.total_count == 9
    # commit=False 이므로 rollback 하면 QuestionSelectionState 가 남지 않아야 한다.
    db.rollback()
    assert db.get(QuestionSelectionState, "tx:rollback-check") is None


def test_authenticated_user_cannot_claim_unowned_order_sheet_issuance(db):
    _seed_diverse_menus_and_full_pool(db)
    user = User(kakao_id=880099, nickname="인증소유경계유저")
    other_user = User(kakao_id=880100, nickname="다른인증유저")
    db.add_all([user, other_user])
    db.commit()

    unowned = issue_order_sheet_questions(db, user_id=None)
    owned = issue_order_sheet_questions(db, user_id=other_user.id)

    assert _find_valid_order_sheet_issuance(db, unowned.order_sheet_id, user_id=None) is not None
    assert _find_valid_order_sheet_issuance(db, unowned.order_sheet_id, user_id=user.id) is None
    assert _find_valid_order_sheet_issuance(db, owned.order_sheet_id, user_id=user.id) is None
    assert _find_valid_order_sheet_issuance(db, owned.order_sheet_id, user_id=None) is None

    rec_resp = create_order_sheet_recommendation(
        db=db,
        user_id=user.id,
        request=OrderSheetRecommendRequest(
            order_sheet_id=unowned.order_sheet_id,
            answers=[
                AnswerItemRequest(question_id=q.id, option_id="any")
                for q in unowned.questions
            ],
        ),
    )
    unowned_row = (
        db.query(OrderSheetIssuance)
        .filter(OrderSheetIssuance.order_sheet_id == unowned.order_sheet_id)
        .one()
    )
    assert unowned_row.status == "ISSUED"
    assert unowned_row.recommendation_id is None
    rec_row = db.get(Recommendation, rec_resp.recommendation_id)
    assert rec_row.order_sheet_list_scope == PERSONAL_RETRY_LIST


def test_feedback_question_options_normalizes_legacy_three_option_cooking_style(db):
    legacy_q8 = OrderSheetQuestion(
        question_code="q_binary_v1_cooking_style_01",
        cluster_key="cooking_style",
        question_text="밥이나 면 요리가 당기나요?",
        options_json=[
            {
                "option_id": "rice_noodle",
                "label": "예",
                "rules": [{"field": "cooking_style", "op": "in", "value": ["rice", "noodle"]}],
            },
            {
                "option_id": "other_style",
                "label": "아니요",
                "rules": [{"field": "cooking_style", "op": "not_in", "value": ["rice", "noodle"]}],
            },
            {"option_id": "any", "label": "상관없어요", "rules": []},
        ],
        initial_score=0.8,
        importance_weight=0.8,
        is_active=True,
    )
    normalized = feedback_question_options(legacy_q8)
    assert [opt["option_id"] for opt in normalized] == ["rice", "noodle", "other_style", "any"]

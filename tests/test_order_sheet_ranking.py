"""Spec #77 / Ticket #81: 3개 하드필터와 6개 점수제로 첫 메뉴 추천 단위·서비스 테스트."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base
from app.menus.models import (
    Menu,
    OrderSheetIssuance,
    OrderSheetQuestion,
    Recommendation,
    RecommendationItemSnapshot,
    UserFoodExclusion,
)
from app.recommend.common import NoAvailableFoodException
from app.recommend.feedback import OUTCOME_ZERO_CANDIDATES, classify_recommendation_outcome
from app.recommend.order_sheet import (
    create_order_sheet_recommendation,
    issue_order_sheet_questions,
)
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.ranking import evaluate_order_sheet_candidates
from app.recommend.schemas import AnswerItemRequest, OrderSheetRecommendRequest
from app.users.models import User


def _question_from_pool(cluster_key: str, *, qid: int) -> OrderSheetQuestion:
    raw = next(q for q in build_candidate_question_pool() if q["cluster_key"] == cluster_key)
    return OrderSheetQuestion(
        id=qid,
        question_code=raw["question_code"],
        cluster_key=raw["cluster_key"],
        question_text=raw["question_text"],
        options_json=raw["options"],
        initial_score=0.5,
        importance_weight=0.5,
        is_active=True,
    )


def _opt(question: OrderSheetQuestion, option_id: str) -> dict:
    return next(opt for opt in question.options_json if opt["option_id"] == option_id)


def _make_menu(
    *,
    menu_id: int,
    food_code: str,
    name: str,
    base_name: str | None = None,
    has_soup: bool | None = False,
    cooking_style: str | None = "rice",
    main_ingredient: str | None = "pork",
    sodium_mg: float | None = 500.0,
    heaviness: str | None = "moderate",
    flavor_intensity: str | None = "mild_clean",
    origin_type: str | None = "restaurant",
    carb_g: float | None = 40.0,
    protein_g: float | None = 15.0,
    allergen_tags: list[str] | None = None,
) -> Menu:
    return Menu(
        id=menu_id,
        food_code=food_code,
        name=name,
        base_name=base_name or name,
        category_large="식사",
        meal_type="main_meal",
        has_soup=has_soup,
        cooking_style=cooking_style,
        main_ingredient=main_ingredient,
        sodium_mg=sodium_mg,
        heaviness=heaviness,
        flavor_intensity=flavor_intensity,
        origin_type=origin_type,
        carb_g=carb_g,
        protein_g=protein_g,
        temperature="hot",
        allergen_tags=list(allergen_tags or []),
    )


def test_exclusions_first_and_strict_three_hard_filters_reject_missing_and_never_auto_relax():
    q_soup = _question_from_pool("has_soup", qid=3)
    q_style = _question_from_pool("cooking_style", qid=8)
    q_ing = _question_from_pool("main_ingredient", qid=9)

    menus = [
        # 제외 대상(돼지고기 알레르기)이지만 하드필터에는 일치 -> Step 0에서 먼저 제거되어야 함
        _make_menu(
            menu_id=1,
            food_code="F001",
            name="돼지국밥",
            base_name="국밥",
            has_soup=True,
            cooking_style="rice",
            main_ingredient="pork",
            allergen_tags=["돼지고기"],
        ),
        # 유일하게 제외와 3개 하드필터를 모두 통과하는 후보 (1종만 남아도 완화하지 않음)
        _make_menu(
            menu_id=2,
            food_code="F002",
            name="소고기국밥",
            base_name="소고기국밥",
            has_soup=True,
            cooking_style="rice",
            main_ingredient="beef",
        ),
        # has_soup=None (미확인) -> Q3 하드필터 탈락
        _make_menu(
            menu_id=3,
            food_code="F003",
            name="미확인국밥",
            base_name="미확인국밥",
            has_soup=None,
            cooking_style="rice",
            main_ingredient="beef",
        ),
        # cooking_style=None (미확인) -> Q8 하드필터 탈락
        _make_menu(
            menu_id=4,
            food_code="F004",
            name="미확인조리탕",
            base_name="미확인조리탕",
            has_soup=True,
            cooking_style=None,
            main_ingredient="beef",
        ),
        # main_ingredient=None (미확인) -> Q9 하드필터 탈락
        _make_menu(
            menu_id=5,
            food_code="F005",
            name="미확인재료국밥",
            base_name="미확인재료국밥",
            has_soup=True,
            cooking_style="rice",
            main_ingredient=None,
        ),
        # 국물 없음 -> Q3 하드필터 탈락 (자동 완화 금지 검증용 다수 후보)
        _make_menu(
            menu_id=6,
            food_code="F006",
            name="비빔밥",
            base_name="비빔밥",
            has_soup=False,
            cooking_style="rice",
            main_ingredient="beef",
        ),
        _make_menu(
            menu_id=7,
            food_code="F007",
            name="소고기덮밥",
            base_name="덮밥",
            has_soup=False,
            cooking_style="rice",
            main_ingredient="beef",
        ),
    ]

    result = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords={"allergy_code:pork"},
        validated_answers=[
            (q_soup, _opt(q_soup, "soup_yes")),
            (q_style, _opt(q_style, "rice")),
            (q_ing, _opt(q_ing, "meat")),
        ],
        top_k=5,
    )

    assert result.excluded_count == 1
    assert result.available_count == 6
    assert [c.menu.id for c in result.selected_candidates] == [2]
    assert len(result.surviving_kind_representatives) == 1

    # 단계별 하드필터 trace가 정확히 기록되어야 한다 (#87 준비)
    assert len(result.hard_filter_traces) == 3
    assert [(t.step_order, t.cluster_key, t.option_id, t.before_count, t.after_count) for t in result.hard_filter_traces] == [
        (1, "has_soup", "soup_yes", 6, 3),
        (2, "cooking_style", "rice", 3, 2),
        (3, "main_ingredient", "meat", 2, 1),
    ]
    assert [(t.before_kind_count, t.after_kind_count) for t in result.hard_filter_traces] == [
        (6, 3),
        (3, 2),
        (2, 1),
    ]

    # 하드필터에 '상관없어요(any)'를 선택하면 해당 필터는 건너뛴다
    neutral_result = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords={"allergy_code:pork"},
        validated_answers=[
            (q_soup, _opt(q_soup, "any")),
            (q_style, _opt(q_style, "rice")),
            (q_ing, _opt(q_ing, "meat")),
        ],
        top_k=5,
    )
    assert [c.menu.id for c in neutral_result.selected_candidates] == [2, 3, 6, 7]
    assert [t.cluster_key for t in neutral_result.hard_filter_traces] == ["cooking_style", "main_ingredient"]

    # 제외 조건만으로 후보가 0개면 NoAvailableFoodException 발생
    with pytest.raises(NoAvailableFoodException):
        evaluate_order_sheet_candidates(
            menus=[menus[0]],
            exclusion_keywords={"allergy_code:pork"},
            validated_answers=[(q_soup, _opt(q_soup, "any"))],
            top_k=5,
        )


def test_six_score_questions_equal_weight_missing_neutral_hooks_and_deterministic_tiebreak(monkeypatch):
    q1 = _question_from_pool("sodium_level", qid=1)
    q2 = _question_from_pool("heaviness", qid=2)
    q4 = _question_from_pool("flavor_intensity", qid=4)
    q5 = _question_from_pool("origin_type", qid=5)
    q6 = _question_from_pool("carb_level", qid=6)
    q7 = _question_from_pool("protein_level", qid=7)

    # DB importance_weight가 달라도 Spec #77 #81 초기 점수제 6문항은 동일 비중(1.0)이다.
    for idx, q in enumerate((q1, q2, q4, q5, q6, q7), start=1):
        q.importance_weight = 0.1 * idx

    menus = [
        # 6개 중 6개 모두 일치 -> 6.0점
        _make_menu(
            menu_id=10,
            food_code="M010",
            name="완벽메뉴",
            sodium_mg=200.0,
            heaviness="light",
            flavor_intensity="spicy_bold",
            origin_type="home_style",
            carb_g=10.0,
            protein_g=20.0,
        ),
        # carb_g, protein_g, sodium_mg가 미확인(None) -> 해당 3개 가점 0점, 나머지 3개 일치 -> 3.0점
        _make_menu(
            menu_id=20,
            food_code="M020",
            name="미확인메뉴",
            sodium_mg=None,
            heaviness="light",
            flavor_intensity="spicy_bold",
            origin_type="home_style",
            carb_g=None,
            protein_g=None,
        ),
        # 3개 일치 -> 3.0점, food_code="M015" < "M020" 이므로 동점 시 앞선다
        _make_menu(
            menu_id=30,
            food_code="M015",
            name="동점앞순위메뉴",
            sodium_mg=200.0,
            heaviness="light",
            flavor_intensity="spicy_bold",
            origin_type="restaurant",
            carb_g=50.0,
            protein_g=5.0,
        ),
        # 0개 일치 -> 0.0점
        _make_menu(
            menu_id=40,
            food_code="M001",
            name="불일치메뉴",
            sodium_mg=900.0,
            heaviness="heavy",
            flavor_intensity="mild_clean",
            origin_type="restaurant",
            carb_g=80.0,
            protein_g=4.0,
        ),
    ]

    answers = [
        (q1, _opt(q1, "low_sodium")),
        (q2, _opt(q2, "light")),
        (q4, _opt(q4, "spicy_bold")),
        (q5, _opt(q5, "home_style")),
        (q6, _opt(q6, "low_carb")),
        (q7, _opt(q7, "high_protein")),
    ]

    res = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords=set(),
        validated_answers=answers,
        top_k=5,
    )
    assert [(c.menu.id, c.rule_score, c.final_score) for c in res.selected_candidates] == [
        (10, 6.0, 6.0),
        (30, 3.0, 3.0),
        (20, 3.0, 3.0),
        (40, 0.0, 0.0),
    ]
    missing_cand = next(c for c in res.selected_candidates if c.menu.id == 20)
    assert missing_cand.question_scores == {1: 0.0, 2: 1.0, 4: 1.0, 5: 1.0, 6: 0.0, 7: 0.0}

    # '상관없어요(any)' 답변은 점수를 건너뛴다 (0점, matched_question_codes 미포함)
    answers_with_neutral = list(answers)
    answers_with_neutral[0] = (q1, _opt(q1, "any"))
    neutral_res = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords=set(),
        validated_answers=answers_with_neutral,
        top_k=5,
    )
    top_cand = neutral_res.selected_candidates[0]
    assert top_cand.menu.id == 10
    assert top_cand.rule_score == 5.0
    assert top_cand.question_scores[1] == 0.0
    assert q1.question_code not in top_cand.matched_question_codes

    # 장기 취향(#83) 및 최근 식사 감점(#82) 훅이 final_score에 합산된다
    hooked = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords=set(),
        validated_answers=answers,
        top_k=5,
        long_term_preference_scores={"미확인메뉴": 0.25},
        recent_meal_penalties={30: -1.0},
    )
    assert [(c.menu.id, c.final_score) for c in hooked.selected_candidates] == [
        (10, 6.0),
        (20, 3.25),
        (30, 2.0),
        (40, 0.0),
    ]

    # 서버 설정 ORDER_SHEET_SCORE_WEIGHTS로 특정 질문 비중 조정 가능
    monkeypatch.setattr(settings, "ORDER_SHEET_SCORE_WEIGHTS", {"origin_type": 5.0})
    weighted = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords=set(),
        validated_answers=answers,
        top_k=5,
    )
    assert [(c.menu.id, c.rule_score) for c in weighted.selected_candidates] == [
        (10, 10.0),
        (20, 7.0),
        (30, 3.0),
        (40, 0.0),
    ]


@pytest.mark.parametrize(
    "surviving_kind_count, expected_selected_count",
    [(0, 0), (1, 1), (3, 3), (5, 5), (6, 5), (8, 5)],
)
def test_food_kind_dedup_representative_selection_and_display_cap(
    surviving_kind_count: int,
    expected_selected_count: int,
):
    q_soup = _question_from_pool("has_soup", qid=3)
    q_spicy = _question_from_pool("flavor_intensity", qid=4)

    menus: list[Menu] = []
    next_id = 1
    # 각 음식 종류마다 2개 상품(높은 점수/낮은 점수)을 넣고, 돈까스/돈가스 별칭도 포함
    kind_names = ["돈까스", "햄버거", "김치찌개", "우동", "순두부찌개", "마라탕", "부대찌개", "칼국수"]
    for idx in range(surviving_kind_count):
        base = kind_names[idx]
        low_base = "돈가스" if base == "돈까스" else ("버거" if base == "햄버거" else base)
        menus.append(
            _make_menu(
                menu_id=next_id,
                food_code=f"K{idx:02d}_A",
                name=f"{low_base}_기본",
                base_name=low_base,
                has_soup=True,
                flavor_intensity="mild_clean",
            )
        )
        next_id += 1
        menus.append(
            _make_menu(
                menu_id=next_id,
                food_code=f"K{idx:02d}_B",
                name=f"{base}_매운맛",
                base_name=base,
                has_soup=True,
                flavor_intensity="spicy_bold",
            )
        )
        next_id += 1

    # 0종 테스트 시에도 전체 메뉴가 비어있지 않도록 국물 없는 메뉴 1개 추가 (제외 404가 아닌 하드필터 0종 검증)
    menus.append(
        _make_menu(
            menu_id=999,
            food_code="Z999",
            name="마른비빔밥",
            base_name="비빔밥",
            has_soup=False,
            flavor_intensity="spicy_bold",
        )
    )

    res = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords=set(),
        validated_answers=[
            (q_soup, _opt(q_soup, "soup_yes")),
            (q_spicy, _opt(q_spicy, "spicy_bold")),
        ],
        top_k=10,  # 요청 top_k=10이어도 서버 상한 ORDER_SHEET_MAX_RECOMMENDATIONS(5) 적용
    )

    assert len(res.surviving_kind_representatives) == surviving_kind_count
    assert len(res.selected_candidates) == expected_selected_count
    # 동일 음식 종류 중복 없음 & 매운맛 상품(높은 점수)이 각 종류의 대표로 선정됨
    selected_kinds = [c.food_kind_key for c in res.selected_candidates]
    assert len(selected_kinds) == len(set(selected_kinds))
    for cand in res.selected_candidates:
        assert cand.menu.name.endswith("_매운맛")
        assert cand.rule_score == 1.0


def test_role_overrides_switch_hard_filter_and_score_by_question_id(monkeypatch):
    q_soup = _question_from_pool("has_soup", qid=301)
    q_spicy = _question_from_pool("flavor_intensity", qid=401)
    menus = [
        _make_menu(menu_id=1, food_code="R001", name="매운비빔밥", has_soup=False, flavor_intensity="spicy_bold"),
        _make_menu(menu_id=2, food_code="R002", name="순한국밥", has_soup=True, flavor_intensity="mild_clean"),
        _make_menu(menu_id=3, food_code="R003", name="매운국밥", has_soup=True, flavor_intensity="spicy_bold"),
    ]

    # has_soup -> score, flavor_intensity -> hard_filter 로 역할 전환
    monkeypatch.setattr(
        settings,
        "ORDER_SHEET_QUESTION_ROLE_OVERRIDES",
        {301: "score", 401: "hard_filter"},
    )
    res = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords=set(),
        validated_answers=[
            (q_soup, _opt(q_soup, "soup_yes")),
            (q_spicy, _opt(q_spicy, "spicy_bold")),
        ],
        top_k=5,
    )

    # flavor_intensity가 하드필터이므로 순한국밥(id=2) 탈락, has_soup는 점수제이므로 매운국밥(1.0) > 매운비빔밥(0.0)
    assert [(c.menu.id, c.rule_score) for c in res.selected_candidates] == [
        (3, 1.0),
        (1, 0.0),
    ]
    assert [t.question_id for t in res.hard_filter_traces] == [401]
    assert res.score_question_ids == [301]


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed_initial_nine_questions(db: Session) -> dict[str, OrderSheetQuestion]:
    pool = build_candidate_question_pool()
    by_cluster: dict[str, OrderSheetQuestion] = {}
    for idx, cluster in enumerate(settings.ORDER_SHEET_INITIAL_CLUSTERS, start=1):
        raw = next(q for q in pool if q["cluster_key"] == cluster)
        q = OrderSheetQuestion(
            question_code=raw["question_code"],
            cluster_key=cluster,
            question_text=raw["question_text"],
            options_json=raw["options"],
            initial_score=round(0.2 + 0.07 * idx, 4),
            importance_weight=round(0.2 + 0.07 * idx, 4),
            is_active=True,
        )
        db.add(q)
        by_cluster[cluster] = q
    db.flush()
    return by_cluster


def test_service_zero_surviving_kinds_persists_zero_candidates_and_links_issuance(db):
    user = User(kakao_id=810001, nickname="후보0건유저")
    db.add(user)
    q_map = _seed_initial_nine_questions(db)
    db.add_all([
        Menu(
            food_code="Z001", name="비빔밥", base_name="비빔밥", category_large="밥류",
            meal_type="main_meal", has_soup=False, cooking_style="rice", main_ingredient="veggie_tofu",
            flavor_intensity="spicy_bold", temperature="hot", heaviness="light",
            origin_type="home_style", sodium_mg=300.0, carb_g=60.0, protein_g=12.0, allergen_tags=[],
        ),
        Menu(
            food_code="Z002", name="샐러드", base_name="샐러드", category_large="샐러드",
            meal_type="main_meal", has_soup=False, cooking_style="raw_salad", main_ingredient="veggie_tofu",
            flavor_intensity="mild_clean", temperature="cold", heaviness="light",
            origin_type="franchise", sodium_mg=180.0, carb_g=10.0, protein_g=8.0, allergen_tags=[],
        ),
    ])
    db.commit()

    issued = issue_order_sheet_questions(db, user_id=user.id, scope_key=f"user:{user.id}")
    assert issued.order_sheet_id is not None

    # 국물 있음(soup_yes) -> 제외 후 후보는 2개지만 하드필터 후 0종
    req = OrderSheetRecommendRequest(
        order_sheet_id=issued.order_sheet_id,
        answers=[
            AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
            AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="any"),
            AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="any"),
        ],
        top_k=5,
    )
    resp = create_order_sheet_recommendation(db=db, request=req, user_id=user.id)

    assert resp.recommended_menus == []
    assert resp.candidate_count == 0
    assert resp.excluded_count == 0

    rec = db.get(Recommendation, resp.recommendation_id)
    assert rec is not None
    assert rec.initial_candidate_count == 0
    assert rec.recommended_menu_ids == []
    assert classify_recommendation_outcome(db, rec.id).outcome == OUTCOME_ZERO_CANDIDATES

    issuance_row = (
        db.query(OrderSheetIssuance)
        .filter(OrderSheetIssuance.order_sheet_id == issued.order_sheet_id)
        .one()
    )
    assert issuance_row.recommendation_id == rec.id
    assert issuance_row.completed_at is not None
    assert issuance_row.status == "COMPLETED"


def test_service_multi_product_same_kind_no_relaxation_and_equal_score_snapshots(db):
    user = User(kakao_id=810002, nickname="단일종류유저")
    db.add(user)
    q_map = _seed_initial_nine_questions(db)
    # 국물 있는 국밥 상품 5개(서로 다른 점수) + 국물 없는 다른 종류 4개
    for idx in range(5):
        db.add(
            Menu(
                food_code=f"G00{idx}",
                name=f"국밥_{idx}",
                base_name="국밥",
                category_large="국류",
                meal_type="main_meal",
                has_soup=True,
                cooking_style="rice",
                main_ingredient="beef",
                sodium_mg=200.0 if idx == 3 else 700.0,
                heaviness="light" if idx == 3 else "heavy",
                flavor_intensity="spicy_bold",
                origin_type="home_style",
                carb_g=10.0 if idx == 3 else 50.0,
                protein_g=20.0,
                temperature="hot",
                allergen_tags=[],
            )
        )
    for idx, name in enumerate(("피자", "햄버거", "비빔밥", "김밥"), start=1):
        db.add(
            Menu(
                food_code=f"O00{idx}",
                name=name,
                base_name=name,
                category_large="식사",
                meal_type="main_meal",
                has_soup=False,
                cooking_style="rice",
                main_ingredient="beef",
                flavor_intensity="spicy_bold",
                origin_type="home_style",
                sodium_mg=200.0,
                heaviness="light",
                carb_g=10.0,
                protein_g=20.0,
                temperature="hot",
                allergen_tags=[],
            )
        )
    db.commit()

    resp = create_order_sheet_recommendation(
        db=db,
        request=OrderSheetRecommendRequest(
            answers=[
                AnswerItemRequest(question_id=q_map["sodium_level"].id, option_id="low_sodium"),
                AnswerItemRequest(question_id=q_map["heaviness"].id, option_id="light"),
                AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
                AnswerItemRequest(question_id=q_map["flavor_intensity"].id, option_id="spicy_bold"),
                AnswerItemRequest(question_id=q_map["origin_type"].id, option_id="home_style"),
                AnswerItemRequest(question_id=q_map["carb_level"].id, option_id="low_carb"),
                AnswerItemRequest(question_id=q_map["protein_level"].id, option_id="high_protein"),
                AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="rice"),
                AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
            ],
            top_k=5,
        ),
        user_id=user.id,
    )

    # 국밥 1종만 남았으므로 자동 완화 없이 국밥 1개(최고점 국밥_3)만 반환한다.
    assert resp.candidate_count == 1
    assert len(resp.recommended_menus) == 1
    top = resp.recommended_menus[0]
    assert top.food_code == "G003"
    assert top.match_score == 6.0

    rec = db.get(Recommendation, resp.recommendation_id)
    assert rec.initial_candidate_count == 1
    snap = (
        db.query(RecommendationItemSnapshot)
        .filter(RecommendationItemSnapshot.recommendation_id == rec.id)
        .one()
    )
    assert snap.rank_position == 1
    assert snap.rule_score == 6.0
    assert snap.recent_meal_adjustment == 0.0
    assert snap.personal_adjustment == 0.0
    assert snap.final_score == 6.0

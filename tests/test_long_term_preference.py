"""Spec #77 / Ticket #83: 반복된 메뉴 평가의 음식 종류별 장기 취향 보정 단위·서비스 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base
import app.map.models as map_models  # noqa: F401 - register place tables
from app.map.models import PlaceSatisfaction, PlaceSelection
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedbackState,
    RecommendationItemSnapshot,
)
from app.menus.popular import get_seoul_tz
from app.recommend.feedback import record_recommendation_feedback
from app.recommend.long_term_preference import (
    FoodKindReaction,
    calculate_long_term_preference_scores,
    load_long_term_preference_scores,
)
from app.recommend.order_sheet import create_order_sheet_recommendation
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.schemas import (
    AnswerItemRequest,
    OrderSheetRecommendRequest,
    RecommendationFeedbackRequest,
)
from app.users.models import User

KST = get_seoul_tz()


def test_long_term_preference_default_settings():
    assert settings.ORDER_SHEET_LONG_TERM_MIN_DAYS == 2
    assert settings.ORDER_SHEET_LONG_TERM_HALF_LIFE_DAYS == 42.0
    assert settings.ORDER_SHEET_LONG_TERM_MAX_SCORE == 0.25


def test_positive_scores_require_repeated_reactions_on_different_days():
    ref = datetime(2026, 11, 24, 18, 0, tzinfo=KST)

    # 같은 한국 날짜에 우동 메뉴 3건 좋아요 -> 날짜는 1일뿐이므로 보정 0점
    same_day_likes = [
        FoodKindReaction(food_kind_key="우동", is_satisfied=True, reacted_at=datetime(2026, 11, 24, 9, 0, tzinfo=KST)),
        FoodKindReaction(food_kind_key="우동", is_satisfied=True, reacted_at=datetime(2026, 11, 24, 12, 30, tzinfo=KST)),
        FoodKindReaction(food_kind_key="우동", is_satisfied=True, reacted_at=datetime(2026, 11, 24, 15, 0, tzinfo=KST)),
    ]
    scores_same_day = calculate_long_term_preference_scores(same_day_likes, reference_time=ref)
    assert scores_same_day.get("우동", 0.0) == 0.0

    # 가중합이 양수여도 어느 방향도 2일에 못 미치면 가점은 0점
    mixed_single_days = [
        FoodKindReaction("우동", True, ref - timedelta(days=1)),
        FoodKindReaction("우동", False, ref - timedelta(days=2)),
    ]
    scores_mixed = calculate_long_term_preference_scores(mixed_single_days, reference_time=ref)
    assert scores_mixed.get("우동", 0.0) == 0.0


def test_first_dislike_applies_immediately_and_repeated_dislike_restores_the_cap():
    ref = datetime(2026, 11, 24, 12, 0, tzinfo=KST)
    first_dislike = FoodKindReaction("우동", False, ref)
    tomorrow = ref + timedelta(days=1)
    second_dislike = FoodKindReaction("우동", False, tomorrow)

    assert calculate_long_term_preference_scores([first_dislike], reference_time=ref)["우동"] == -0.25
    assert calculate_long_term_preference_scores([first_dislike], reference_time=tomorrow)["우동"] == pytest.approx(
        -0.25 * 0.5 ** (1 / 42), abs=1e-6,
    )
    assert calculate_long_term_preference_scores(
        [first_dislike], reference_time=ref + timedelta(days=42),
    )["우동"] == -0.125
    assert calculate_long_term_preference_scores(
        [first_dislike, second_dislike], reference_time=tomorrow,
    )["우동"] == -0.25

    previous_like = FoodKindReaction("우동", True, ref - timedelta(days=42))
    assert calculate_long_term_preference_scores([previous_like, first_dislike], reference_time=ref)["우동"] == -0.125


def test_two_consecutive_kst_days_likes_yield_max_boost_and_respect_kst_midnight():
    # 같은 UTC 날짜(2026-11-23)이지만 KST 자정(15:00 UTC)을 경계로 11/23과 11/24 서로 다른 한국 날짜
    before_kst_midnight = datetime(2026, 11, 23, 14, 59, 0, tzinfo=timezone.utc)  # 2026-11-23 23:59 KST
    after_kst_midnight = datetime(2026, 11, 23, 15, 1, 0, tzinfo=timezone.utc)    # 2026-11-24 00:01 KST
    ref = datetime(2026, 11, 24, 12, 0, 0, tzinfo=KST)

    reactions = [
        FoodKindReaction(food_kind_key="우동", is_satisfied=True, reacted_at=before_kst_midnight),
        FoodKindReaction(food_kind_key="우동", is_satisfied=True, reacted_at=after_kst_midnight),
    ]
    scores = calculate_long_term_preference_scores(reactions, reference_time=ref)
    assert scores["우동"] == pytest.approx(0.25, abs=1e-6)


def test_spec_section3_worked_examples_with_42_day_half_life():
    ref = datetime(2026, 11, 24, 12, 0, tzinfo=KST)

    # 사례 1: 30일 전·28일 전 좋아요 + 1일 전 싫어요 -> 약 +0.064 (양수 유지)
    case_1 = [
        FoodKindReaction("우동", True, ref - timedelta(days=30)),
        FoodKindReaction("우동", True, ref - timedelta(days=28)),
        FoodKindReaction("우동", False, ref - timedelta(days=1)),
    ]
    score_1 = calculate_long_term_preference_scores(case_1, reference_time=ref)["우동"]
    assert score_1 > 0.0
    assert score_1 == pytest.approx(0.064, abs=0.001)

    # 사례 2: 30일 전·28일 전 좋아요 + 4일 전·1일 전 싫어요 -> 약 -0.170 (음수 역전)
    case_2 = [
        FoodKindReaction("우동", True, ref - timedelta(days=30)),
        FoodKindReaction("우동", True, ref - timedelta(days=28)),
        FoodKindReaction("우동", False, ref - timedelta(days=4)),
        FoodKindReaction("우동", False, ref - timedelta(days=1)),
    ]
    score_2 = calculate_long_term_preference_scores(case_2, reference_time=ref)["우동"]
    assert score_2 < 0.0
    assert score_2 == pytest.approx(-0.170, abs=0.001)

    # 사례 3: 50일 전·48일 전 좋아요 + 1일 전 싫어요 -> 약 -0.023 (오래된 좋아요는 최근 싫어요에 밀림)
    case_3 = [
        FoodKindReaction("우동", True, ref - timedelta(days=50)),
        FoodKindReaction("우동", True, ref - timedelta(days=48)),
        FoodKindReaction("우동", False, ref - timedelta(days=1)),
    ]
    score_3 = calculate_long_term_preference_scores(case_3, reference_time=ref)["우동"]
    assert score_3 < 0.0
    assert score_3 == pytest.approx(-0.023, abs=0.001)


def test_same_day_multiple_reactions_count_as_one_day_but_each_contributes_weight():
    ref = datetime(2026, 11, 24, 12, 0, tzinfo=KST)
    day_30_am = datetime(2026, 10, 25, 11, 0, tzinfo=KST)
    day_30_pm = datetime(2026, 10, 25, 19, 0, tzinfo=KST)
    day_28 = datetime(2026, 10, 27, 12, 0, tzinfo=KST)
    day_1 = datetime(2026, 11, 23, 12, 0, tzinfo=KST)

    # 30일 전 같은 날 서로 다른 메뉴 2건만 있을 때는 아직 1일 -> 0점
    only_one_day = [
        FoodKindReaction("우동", True, day_30_am),
        FoodKindReaction("우동", True, day_30_pm),
    ]
    assert calculate_long_term_preference_scores(only_one_day, reference_time=ref).get("우동", 0.0) == 0.0

    # 28일 전 좋아요 1건이 추가되어 2일이 되면, 30일 전 두 건이 모두 가중합에 기여한다
    # (2 * 0.5^(30/42) + 0.5^(28/42) - 0.5^(1/42)) * 0.25 ~= 0.216335
    with_second_day = [
        FoodKindReaction("우동", True, day_30_am),
        FoodKindReaction("우동", True, day_30_pm),
        FoodKindReaction("우동", True, day_28),
        FoodKindReaction("우동", False, day_1),
    ]
    score = calculate_long_term_preference_scores(with_second_day, reference_time=ref)["우동"]
    assert score == pytest.approx(0.216335, abs=0.001)


def test_scores_clamp_to_bounds_and_server_settings_are_configurable(monkeypatch):
    ref = datetime(2026, 11, 24, 12, 0, tzinfo=KST)

    # 반복 싫어요는 -0.25로 하한 고정
    repeated_dislikes = [
        FoodKindReaction("우동", False, ref - timedelta(days=3)),
        FoodKindReaction("우동", False, ref - timedelta(days=2)),
        FoodKindReaction("우동", False, ref - timedelta(days=1)),
    ]
    assert calculate_long_term_preference_scores(repeated_dislikes, reference_time=ref)["우동"] == pytest.approx(
        -0.25, abs=1e-6
    )

    # 반감기를 56일로 조정하면 50·48일 전 좋아요 + 1일 전 싫어요가 양수로 바뀐다 (설계 노트 기준)
    case_50_48_1 = [
        FoodKindReaction("우동", True, ref - timedelta(days=50)),
        FoodKindReaction("우동", True, ref - timedelta(days=48)),
        FoodKindReaction("우동", False, ref - timedelta(days=1)),
    ]
    monkeypatch.setattr(settings, "ORDER_SHEET_LONG_TERM_HALF_LIFE_DAYS", 56.0)
    assert calculate_long_term_preference_scores(case_50_48_1, reference_time=ref)["우동"] > 0.0

    # 적용 일수를 3일로 올리면 2일 좋아요는 0점
    two_day_likes = [
        FoodKindReaction("우동", True, ref - timedelta(days=2)),
        FoodKindReaction("우동", True, ref - timedelta(days=1)),
    ]
    monkeypatch.setattr(settings, "ORDER_SHEET_LONG_TERM_MIN_DAYS", 3)
    assert calculate_long_term_preference_scores(two_day_likes, reference_time=ref).get("우동", 0.0) == 0.0

    # 보정 상한을 0.40으로 조정하면 활성화 시 +0.40 적용
    monkeypatch.setattr(settings, "ORDER_SHEET_LONG_TERM_MIN_DAYS", 2)
    monkeypatch.setattr(settings, "ORDER_SHEET_LONG_TERM_MAX_SCORE", 0.40)
    assert calculate_long_term_preference_scores(two_day_likes, reference_time=ref)["우동"] == pytest.approx(
        0.40, abs=1e-6
    )


def test_future_reactions_and_excluded_recommendation_id_are_ignored():
    ref = datetime(2026, 11, 24, 12, 0, tzinfo=KST)
    reactions = [
        FoodKindReaction("우동", True, ref - timedelta(days=1), recommendation_id=10),
        FoodKindReaction("우동", True, ref + timedelta(seconds=1), recommendation_id=11),
    ]
    # 미래 시각 반응 제외 -> 1일뿐이라 0점
    assert calculate_long_term_preference_scores(reactions, reference_time=ref).get("우동", 0.0) == 0.0

    # 현재 주문서(recommendation_id=11) 제외 -> 1일뿐이라 0점
    same_or_earlier = [
        FoodKindReaction("우동", True, ref - timedelta(days=1), recommendation_id=10),
        FoodKindReaction("우동", True, ref, recommendation_id=11),
    ]
    assert (
        calculate_long_term_preference_scores(
            same_or_earlier,
            reference_time=ref,
            exclude_recommendation_id=11,
        ).get("우동", 0.0)
        == 0.0
    )


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _add_menu(
    db: Session,
    *,
    food_code: str,
    name: str,
    base_name: str,
    has_soup: bool = True,
    cooking_style: str = "noodle",
    main_ingredient: str = "other",
    sodium_mg: float = 500.0,
    heaviness: str = "moderate",
    flavor_intensity: str = "mild_clean",
    origin_type: str = "restaurant",
    carb_g: float = 50.0,
    protein_g: float = 12.0,
) -> Menu:
    menu = Menu(
        food_code=food_code,
        name=name,
        base_name=base_name,
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
        allergen_tags=[],
    )
    db.add(menu)
    db.flush()
    return menu


def _seed_feedback_state(
    db: Session,
    *,
    user_id: int,
    menu: Menu,
    is_satisfied: bool,
    reacted_at: datetime,
    food_kind_id_override: int | None = None,
    use_explicit_none_food_kind: bool = False,
) -> Recommendation:
    reacted_naive_utc = reacted_at.astimezone(timezone.utc).replace(tzinfo=None)
    rec = Recommendation(
        user_id=user_id,
        shown_question_ids=[],
        answers_json=[],
        recommended_menu_ids=[menu.id],
        initial_candidate_count=1,
        created_at=reacted_naive_utc,
    )
    db.add(rec)
    db.flush()
    fk_id = None if use_explicit_none_food_kind else (
        food_kind_id_override if food_kind_id_override is not None else menu.food_kind_id
    )
    state = RecommendationFeedbackState(
        recommendation_id=rec.id,
        user_id=user_id,
        selected_menu_id=menu.id,
        food_kind_id=fk_id,
        exposure_rank=1,
        menu_key=menu.base_name,
        menu_base_name=menu.base_name,
        menu_main_ingredient=menu.main_ingredient,
        is_satisfied=is_satisfied,
        answer_match_results={},
        created_at=reacted_naive_utc,
        updated_at=reacted_naive_utc,
    )
    db.add(state)
    db.flush()
    return rec


def test_load_long_term_scores_maps_canonical_kinds_isolates_users_and_ignores_list_reject_and_map_reactions(
    db: Session,
):
    user = User(kakao_id=830001, nickname="장기취향테스터")
    other_user = User(kakao_id=830002, nickname="다른사용자")
    db.add_all([user, other_user])
    db.flush()

    udon_a = _add_menu(db, food_code="U001", name="어묵우동", base_name="우동")
    udon_b = _add_menu(db, food_code="U002", name="튀김우동", base_name="우동")
    tonkatsu_a = _add_menu(db, food_code="T001", name="왕돈까스", base_name="돈까스", has_soup=False, cooking_style="fried_pan")
    tonkatsu_b = _add_menu(db, food_code="T002", name="등심돈가스", base_name="돈가스", has_soup=False, cooking_style="fried_pan")
    bibimbap = _add_menu(db, food_code="B001", name="비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice")
    ramen = _add_menu(db, food_code="R001", name="신라면", base_name="라면")

    ref = datetime(2026, 11, 24, 12, 0, tzinfo=KST)

    # 1. 우동: 30일 전(어묵우동), 28일 전(튀김우동) 좋아요 + 1일 전(어묵우동) 싫어요 -> 약 +0.064
    _seed_feedback_state(db, user_id=user.id, menu=udon_a, is_satisfied=True, reacted_at=ref - timedelta(days=30))
    _seed_feedback_state(db, user_id=user.id, menu=udon_b, is_satisfied=True, reacted_at=ref - timedelta(days=28))
    _seed_feedback_state(db, user_id=user.id, menu=udon_a, is_satisfied=False, reacted_at=ref - timedelta(days=1))

    # 2. 돈까스/돈가스 별칭 정규화(food_kind_id가 None인 레거시 행 포함) -> 같은 tonkatsu로 합산돼 +0.25
    _seed_feedback_state(
        db,
        user_id=user.id,
        menu=tonkatsu_a,
        is_satisfied=True,
        reacted_at=ref - timedelta(days=2),
        use_explicit_none_food_kind=True,
    )
    _seed_feedback_state(
        db,
        user_id=user.id,
        menu=tonkatsu_b,
        is_satisfied=True,
        reacted_at=ref - timedelta(days=1),
    )

    # 3. 다른 사용자의 라면 연속 좋아요 3일은 현재 사용자에게 섞이지 않아야 함
    for d in (3, 2, 1):
        _seed_feedback_state(db, user_id=other_user.id, menu=ramen, is_satisfied=True, reacted_at=ref - timedelta(days=d))

    # 4. 목록 전체 거절(list_rejected_at) 3일 연속 -> 비빔밥에 장기 취향 감점이 생기면 안 됨
    for d in (3, 2, 1):
        rejected_ts = (ref - timedelta(days=d)).astimezone(timezone.utc).replace(tzinfo=None)
        db.add(
            Recommendation(
                user_id=user.id,
                shown_question_ids=[],
                answers_json=[],
                recommended_menu_ids=[bibimbap.id],
                initial_candidate_count=1,
                list_rejected_at=rejected_ts,
                closed_at=rejected_ts,
                created_at=rejected_ts,
            )
        )

    # 5. 지도 가게 선택·만족 3일 연속 -> 비빔밥 장기 취향에 반영되면 안 됨
    for d in (3, 2, 1):
        ts = (ref - timedelta(days=d)).astimezone(timezone.utc).replace(tzinfo=None)
        sel = PlaceSelection(
            user_id=user.id,
            menu_id=bibimbap.id,
            kakao_place_id=f"place_{d}",
            place_name=f"비빔밥집_{d}",
            created_at=ts,
        )
        db.add(sel)
        db.flush()
        db.add(
            PlaceSatisfaction(
                place_selection_id=sel.id,
                user_id=user.id,
                menu_id=bibimbap.id,
                kakao_place_id=f"place_{d}",
                is_satisfied=True,
                created_at=ts,
                updated_at=ts,
            )
        )
    db.commit()

    scores = load_long_term_preference_scores(db, user.id, reference_time=ref)
    assert scores["우동"] == pytest.approx(0.064, abs=0.001)
    assert scores["tonkatsu"] == pytest.approx(0.25, abs=1e-6)
    assert scores.get("비빔밥", 0.0) == 0.0
    assert scores.get("라면", 0.0) == 0.0


def _seed_questions(db: Session) -> dict[str, OrderSheetQuestion]:
    pool = build_candidate_question_pool()
    by_cluster: dict[str, OrderSheetQuestion] = {}
    for cluster in settings.ORDER_SHEET_INITIAL_CLUSTERS:
        raw = next(q for q in pool if q["cluster_key"] == cluster)
        q = OrderSheetQuestion(
            question_code=raw["question_code"],
            cluster_key=cluster,
            question_text=raw["question_text"],
            options_json=raw["options"],
            initial_score=0.75,
            importance_weight=0.75,
            is_active=True,
        )
        db.add(q)
        by_cluster[cluster] = q
    db.flush()
    return by_cluster


def _neutral_request(q_map: dict[str, OrderSheetQuestion], overrides: dict[str, str] | None = None) -> OrderSheetRecommendRequest:
    opts = {cluster: "any" for cluster in settings.ORDER_SHEET_INITIAL_CLUSTERS}
    if overrides:
        opts.update(overrides)
    return OrderSheetRecommendRequest(
        answers=[
            AnswerItemRequest(question_id=q_map[cluster].id, option_id=opt_id)
            for cluster, opt_id in opts.items()
        ],
        top_k=5,
    )


def test_single_dislike_keeps_menu_as_candidate_and_does_not_leak_to_question_weights(db: Session):
    user = User(kakao_id=830010, nickname="단일싫어요유저")
    db.add(user)
    q_map = _seed_questions(db)
    # 우동이 동점 시 앞서는 food_code로 설정
    udon = _add_menu(db, food_code="A001", name="어묵우동", base_name="우동", has_soup=True, cooking_style="noodle")
    bibimbap = _add_menu(db, food_code="A002", name="비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice")
    db.commit()

    weights_before = {c: q.importance_weight for c, q in q_map.items()}

    first = create_order_sheet_recommendation(db, _neutral_request(q_map), user_id=user.id)
    assert [m.base_name for m in first.recommended_menus] == ["우동", "비빔밥"]

    # 첫 주문서에서 우동에 한 번 싫어요
    record_recommendation_feedback(
        db,
        recommendation_id=first.recommendation_id,
        request=RecommendationFeedbackRequest(selected_menu_id=udon.id, is_satisfied=False),
        user_id=user.id,
    )

    # 같은 날 다음 주문서에서 우동이 비빔밥 뒤로 내려간다.
    second = create_order_sheet_recommendation(db, _neutral_request(q_map), user_id=user.id)
    assert [m.base_name for m in second.recommended_menus] == ["비빔밥", "우동"]

    # 질문 가중치로 전파되지 않음
    for cluster, q in q_map.items():
        db.refresh(q)
        assert q.importance_weight == weights_before[cluster]


def test_order_sheet_ranking_reflects_spec_section3_udon_vs_bibimbap_scenarios_without_leaking(
    db: Session,
):
    q_map = _seed_questions(db)
    # food_code 동점 순서: 비빔밥(A001) < 라면(A002) < 짜장면(A003) < 우동(A004)
    # -> 보정이 0점이면 비빔밥이 우동보다 앞선다. 우동이 1위가 되려면 장기 취향이 양수여야 한다.
    bibimbap = _add_menu(db, food_code="A001", name="비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice")
    ramen = _add_menu(db, food_code="A002", name="신라면", base_name="라면", has_soup=True, cooking_style="noodle")
    jjajang = _add_menu(db, food_code="A003", name="간짜장", base_name="짜장면", has_soup=False, cooking_style="noodle")
    udon = _add_menu(db, food_code="A004", name="어묵우동", base_name="우동", has_soup=True, cooking_style="noodle")
    now = datetime.now(timezone.utc)

    # 시나리오 1: 지난달(30일·28일 전) 우동 좋아요 + 어제(1일 전) 우동 싫어요 -> 우동(+0.064) > 비빔밥(0)
    u1 = User(kakao_id=830021, nickname="시나리오1유저")
    db.add(u1)
    db.flush()
    _seed_feedback_state(db, user_id=u1.id, menu=udon, is_satisfied=True, reacted_at=now - timedelta(days=30))
    _seed_feedback_state(db, user_id=u1.id, menu=udon, is_satisfied=True, reacted_at=now - timedelta(days=28))
    _seed_feedback_state(db, user_id=u1.id, menu=udon, is_satisfied=False, reacted_at=now - timedelta(days=1))
    db.commit()

    rec1 = create_order_sheet_recommendation(db, _neutral_request(q_map), user_id=u1.id)
    assert [m.base_name for m in rec1.recommended_menus] == ["우동", "비빔밥", "라면", "짜장면"]
    snaps1 = {
        s.menu_id: s.personal_adjustment
        for s in db.query(RecommendationItemSnapshot)
        .filter(RecommendationItemSnapshot.recommendation_id == rec1.recommendation_id)
        .all()
    }
    assert snaps1[udon.id] == pytest.approx(0.064, abs=0.001)
    # 라면·짜장면·비빔밥에는 우동 반응이 전파되지 않음
    assert snaps1[bibimbap.id] == 0.0
    assert snaps1[ramen.id] == 0.0
    assert snaps1[jjajang.id] == 0.0

    # 시나리오 2·3에서는 우동이 동점 기준(food_code A000)으로 비빔밥(A001)보다 앞서도록 두어
    # 음수 장기 취향 보정이 실제로 순위를 역전(비빔밥 > 우동)시키는지 검증한다.
    udon.food_code = "A000"
    db.flush()

    # 시나리오 2: 30일·28일 전 우동 좋아요 + 이번 주(4일·1일 전) 우동 싫어요 -> 비빔밥(0) > 우동(-0.170)
    u2 = User(kakao_id=830022, nickname="시나리오2유저")
    db.add(u2)
    db.flush()
    _seed_feedback_state(db, user_id=u2.id, menu=udon, is_satisfied=True, reacted_at=now - timedelta(days=30))
    _seed_feedback_state(db, user_id=u2.id, menu=udon, is_satisfied=True, reacted_at=now - timedelta(days=28))
    _seed_feedback_state(db, user_id=u2.id, menu=udon, is_satisfied=False, reacted_at=now - timedelta(days=4))
    _seed_feedback_state(db, user_id=u2.id, menu=udon, is_satisfied=False, reacted_at=now - timedelta(days=1))
    db.commit()

    rec2 = create_order_sheet_recommendation(db, _neutral_request(q_map), user_id=u2.id)
    assert [m.base_name for m in rec2.recommended_menus] == ["비빔밥", "라면", "짜장면", "우동"]
    snaps2 = {
        s.menu_id: s.personal_adjustment
        for s in db.query(RecommendationItemSnapshot)
        .filter(RecommendationItemSnapshot.recommendation_id == rec2.recommendation_id)
        .all()
    }
    assert snaps2[udon.id] == pytest.approx(-0.170, abs=0.001)
    assert snaps2[ramen.id] == 0.0
    assert snaps2[jjajang.id] == 0.0

    # 시나리오 3: 50일·48일 전 우동 좋아요 + 어제(1일 전) 우동 싫어요 -> 비빔밥(0) > 우동(-0.023)
    u3 = User(kakao_id=830023, nickname="시나리오3유저")
    db.add(u3)
    db.flush()
    _seed_feedback_state(db, user_id=u3.id, menu=udon, is_satisfied=True, reacted_at=now - timedelta(days=50))
    _seed_feedback_state(db, user_id=u3.id, menu=udon, is_satisfied=True, reacted_at=now - timedelta(days=48))
    _seed_feedback_state(db, user_id=u3.id, menu=udon, is_satisfied=False, reacted_at=now - timedelta(days=1))
    db.commit()

    rec3 = create_order_sheet_recommendation(db, _neutral_request(q_map), user_id=u3.id)
    assert [m.base_name for m in rec3.recommended_menus] == ["비빔밥", "라면", "짜장면", "우동"]
    snaps3 = {
        s.menu_id: s.personal_adjustment
        for s in db.query(RecommendationItemSnapshot)
        .filter(RecommendationItemSnapshot.recommendation_id == rec3.recommendation_id)
        .all()
    }
    assert snaps3[udon.id] == pytest.approx(-0.023, abs=0.001)


def test_todays_hard_filter_and_score_answers_override_max_long_term_boost(db: Session):
    user = User(kakao_id=830030, nickname="오늘답변우선유저")
    db.add(user)
    q_map = _seed_questions(db)

    # 우동: 국물 있음, 면, 매운맛 아님(mild_clean)
    udon = _add_menu(
        db,
        food_code="A001",
        name="어묵우동",
        base_name="우동",
        has_soup=True,
        cooking_style="noodle",
        flavor_intensity="mild_clean",
    )
    # 비빔밥: 국물 없음, 밥, 매콤(spicy_bold)
    bibimbap = _add_menu(
        db,
        food_code="A002",
        name="매운비빔밥",
        base_name="비빔밥",
        has_soup=False,
        cooking_style="rice",
        flavor_intensity="spicy_bold",
    )
    now = datetime.now(timezone.utc)
    # 우동에 이틀 연속 좋아요 -> 최대 보정 +0.25
    _seed_feedback_state(db, user_id=user.id, menu=udon, is_satisfied=True, reacted_at=now - timedelta(days=2))
    _seed_feedback_state(db, user_id=user.id, menu=udon, is_satisfied=True, reacted_at=now - timedelta(days=1))
    db.commit()

    # 1. 오늘 하드필터(국물 없음) 선택 -> +0.25 보정이 있어도 우동은 탈락하고 비빔밥만 남는다
    hard_filtered = create_order_sheet_recommendation(
        db,
        _neutral_request(q_map, {"has_soup": "soup_no"}),
        user_id=user.id,
    )
    assert [m.base_name for m in hard_filtered.recommended_menus] == ["비빔밥"]

    # 2. 오늘 점수제(매콤한 음식) 1점 일치 -> 비빔밥(1.0)이 우동(0.0 + 0.25)을 이긴다
    score_priority = create_order_sheet_recommendation(
        db,
        _neutral_request(q_map, {"flavor_intensity": "spicy_bold"}),
        user_id=user.id,
    )
    assert [m.base_name for m in score_priority.recommended_menus] == ["비빔밥", "우동"]

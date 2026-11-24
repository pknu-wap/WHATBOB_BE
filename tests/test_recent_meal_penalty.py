"""Spec #77 / Ticket #82: 최근 먹은 메뉴의 반복 줄이기 단위·서비스 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    RecommendationItemSnapshot,
    UserMeal,
)
from app.menus.popular import get_seoul_tz
from app.recommend.meal_penalty import (
    RecentMealPenaltyConfig,
    compute_recent_meal_penalties,
    load_recent_meal_penalties,
)
from app.recommend.order_sheet import create_order_sheet_recommendation
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.ranking import evaluate_order_sheet_candidates
from app.recommend.schemas import AnswerItemRequest, OrderSheetRecommendRequest
from app.users.models import User


def _make_menu(
    *,
    menu_id: int,
    food_code: str,
    name: str,
    base_name: str | None = None,
    food_kind_id: int | None = None,
    has_soup: bool = False,
    cooking_style: str = "rice",
    main_ingredient: str = "pork",
    sodium_mg: float | None = 500.0,
    heaviness: str = "moderate",
    flavor_intensity: str = "mild_clean",
    origin_type: str = "restaurant",
    carb_g: float | None = 40.0,
    protein_g: float | None = 15.0,
) -> Menu:
    return Menu(
        id=menu_id,
        food_code=food_code,
        name=name,
        base_name=base_name or name,
        food_kind_id=food_kind_id,
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


def _meal(*, menu_id: int | None, menu_name: str, eaten_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(menu_id=menu_id, menu_name=menu_name, eaten_at=eaten_at)


def test_default_settings_and_kst_day_boundary_penalties():
    assert settings.ORDER_SHEET_RECENT_MEAL_PENALTY_SAME_DAY == -2.0
    assert settings.ORDER_SHEET_RECENT_MEAL_PENALTY_NEXT_DAY == -1.0
    assert settings.ORDER_SHEET_RECENT_MEAL_PENALTY_TWO_DAYS_AGO == -0.5
    assert settings.ORDER_SHEET_RECENT_MEAL_PENALTY_OTHER_SAME_KIND == 0.0

    kst = get_seoul_tz()
    # 기준 시각: 2026-11-24 00:30 KST (UTC로는 2026-11-23 15:30)
    now_early_kst = datetime(2026, 11, 24, 0, 30, tzinfo=kst)

    m_same_day = _make_menu(menu_id=1, food_code="D0", name="김치볶음밥", base_name="볶음밥")
    m_next_day = _make_menu(menu_id=2, food_code="D1", name="돈까스", base_name="돈까스")
    m_two_days = _make_menu(menu_id=3, food_code="D2", name="우동", base_name="우동")
    m_three_days = _make_menu(menu_id=4, food_code="D3", name="비빔밥", base_name="비빔밥")
    m_future = _make_menu(menu_id=5, food_code="DF", name="라멘", base_name="라멘")

    meals = [
        # 같은 KST 날짜 00:10 KST (20분 전) -> 당일 -2.0
        _meal(menu_id=1, menu_name="김치볶음밥", eaten_at=datetime(2026, 11, 24, 0, 10, tzinfo=kst)),
        # 전날 KST 23:30 (불과 1시간 전이지만 KST 날짜가 바뀜) -> 다음 날 -1.0
        _meal(menu_id=2, menu_name="돈까스", eaten_at=datetime(2026, 11, 23, 23, 30, tzinfo=kst)),
        # 이틀 전 KST 19:00 -> 이틀 뒤 -0.5
        _meal(menu_id=3, menu_name="우동", eaten_at=datetime(2026, 11, 22, 19, 0, tzinfo=kst)),
        # 3일 전 KST 23:59 -> 0.0 (기간 만료)
        _meal(menu_id=4, menu_name="비빔밥", eaten_at=datetime(2026, 11, 21, 23, 59, tzinfo=kst)),
        # 미래 날짜 KST -> 무시 (0.0)
        _meal(menu_id=5, menu_name="라멘", eaten_at=datetime(2026, 11, 25, 12, 0, tzinfo=kst)),
    ]

    penalties = compute_recent_meal_penalties(
        meals=meals,
        menus=[m_same_day, m_next_day, m_two_days, m_three_days, m_future],
        now=now_early_kst,
        config=RecentMealPenaltyConfig.from_settings(),
    )

    assert penalties.get(1, 0.0) == pytest.approx(-2.0)
    assert penalties.get(2, 0.0) == pytest.approx(-1.0)
    assert penalties.get(3, 0.0) == pytest.approx(-0.5)
    assert penalties.get(4, 0.0) == pytest.approx(0.0)
    assert penalties.get(5, 0.0) == pytest.approx(0.0)

    # 같은 KST 날짜 내 23시간 차이 (00:10 KST -> 23:30 KST, UTC 날짜는 바뀌지만 KST는 동일 날짜) -> 당일 -2.0
    now_late_kst = datetime(2026, 11, 24, 23, 30, tzinfo=kst)
    naive_utc_same_kst_morning = datetime(2026, 11, 23, 15, 10)  # naive UTC == 2026-11-24 00:10 KST
    penalties_late = compute_recent_meal_penalties(
        meals=[_meal(menu_id=1, menu_name="김치볶음밥", eaten_at=naive_utc_same_kst_morning)],
        menus=[m_same_day],
        now=now_late_kst,
    )
    assert penalties_late.get(1, 0.0) == pytest.approx(-2.0)


def test_cumulative_summing_exact_name_fallback_and_zero_other_same_kind():
    kst = get_seoul_tz()
    now = datetime(2026, 11, 24, 18, 0, tzinfo=kst)

    donkatsu = _make_menu(menu_id=10, food_code="T01", name="돈까스", base_name="돈까스", food_kind_id=100)
    chicken_katsu = _make_menu(menu_id=11, food_code="T02", name="치킨까스", base_name="돈까스", food_kind_id=100)
    chicken = _make_menu(menu_id=12, food_code="T03", name="치킨", base_name="치킨", food_kind_id=101)
    kimchi_rice = _make_menu(menu_id=20, food_code="R01", name="볶음밥_김치", base_name="볶음밥", food_kind_id=200)
    shrimp_rice = _make_menu(menu_id=21, food_code="R02", name="볶음밥_새우", base_name="볶음밥", food_kind_id=200)
    bibimbap = _make_menu(menu_id=22, food_code="R03", name="비빔밥", base_name="비빔밥", food_kind_id=201)

    meals = [
        # 돈까스: 오늘 점심(-2.0) + 어제(-1.0) + 그저께(-0.5) + 3일 전(0.0) -> 합계 -3.5
        _meal(menu_id=10, menu_name="돈까스", eaten_at=datetime(2026, 11, 24, 12, 0, tzinfo=kst)),
        _meal(menu_id=10, menu_name="돈까스", eaten_at=datetime(2026, 11, 23, 12, 0, tzinfo=kst)),
        _meal(menu_id=None, menu_name="  돈까스  ", eaten_at=datetime(2026, 11, 22, 12, 0, tzinfo=kst)),
        _meal(menu_id=10, menu_name="돈까스", eaten_at=datetime(2026, 11, 21, 12, 0, tzinfo=kst)),
        # 김치볶음밥: menu_id=None + 표시명("김치볶음밥")/원본명("볶음밥_김치") 매칭 합산 -> -1.0 + -0.5 = -1.5
        _meal(menu_id=None, menu_name="김치볶음밥", eaten_at=datetime(2026, 11, 23, 19, 0, tzinfo=kst)),
        _meal(menu_id=None, menu_name="볶음밥_김치", eaten_at=datetime(2026, 11, 22, 19, 0, tzinfo=kst)),
    ]

    penalties = compute_recent_meal_penalties(
        meals=meals,
        menus=[donkatsu, chicken_katsu, chicken, kimchi_rice, shrimp_rice, bibimbap],
        now=now,
    )

    assert penalties.get(10, 0.0) == pytest.approx(-3.5)
    assert penalties.get(11, 0.0) == pytest.approx(0.0)  # 치킨까스 0.0
    assert penalties.get(12, 0.0) == pytest.approx(0.0)  # 치킨 0.0
    assert penalties.get(20, 0.0) == pytest.approx(-1.5)
    assert penalties.get(21, 0.0) == pytest.approx(0.0)  # 새우볶음밥 0.0
    assert penalties.get(22, 0.0) == pytest.approx(0.0)  # 비빔밥 0.0


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


def test_donkatsu_scope_and_todays_explicit_preference_priority():
    kst = get_seoul_tz()
    now = datetime(2026, 11, 24, 18, 0, tzinfo=kst)

    q_soup = _question_from_pool("has_soup", qid=3)
    q_style = _question_from_pool("cooking_style", qid=8)
    q_ing = _question_from_pool("main_ingredient", qid=9)
    q_sodium = _question_from_pool("sodium_level", qid=1)
    q_heavy = _question_from_pool("heaviness", qid=2)
    q_spicy = _question_from_pool("flavor_intensity", qid=4)
    q_origin = _question_from_pool("origin_type", qid=5)

    # 돈까스·치킨까스·치킨: 모두 국물 없음 / 그 외 / 고기 요리
    donkatsu = _make_menu(
        menu_id=101,
        food_code="A001",
        name="돈까스",
        base_name="돈까스",
        has_soup=False,
        cooking_style="fried_pan",
        main_ingredient="pork",
        sodium_mg=200.0,
        heaviness="heavy",
        flavor_intensity="mild_clean",
        origin_type="restaurant",
    )
    chicken_katsu = _make_menu(
        menu_id=102,
        food_code="A002",
        name="치킨까스",
        base_name="치킨까스",
        has_soup=False,
        cooking_style="fried_pan",
        main_ingredient="poultry",
        sodium_mg=200.0,
        heaviness="heavy",
        flavor_intensity="mild_clean",
        origin_type="restaurant",
    )
    chicken = _make_menu(
        menu_id=103,
        food_code="A003",
        name="치킨",
        base_name="치킨",
        has_soup=False,
        cooking_style="fried_pan",
        main_ingredient="poultry",
        sodium_mg=200.0,
        heaviness="heavy",
        flavor_intensity="mild_clean",
        origin_type="restaurant",
    )
    menus = [donkatsu, chicken_katsu, chicken]

    # 어제 돈까스 식사 기록 1건 (-1.0)
    meals = [_meal(menu_id=101, menu_name="돈까스", eaten_at=datetime(2026, 11, 23, 12, 30, tzinfo=kst))]
    penalties = compute_recent_meal_penalties(meals, menus, now=now)

    # 1) 오늘 취향 일치도가 같을 때: 돈까스만 -1.0, 치킨까스·치킨은 0.0 -> 치킨까스(A002), 치킨(A003), 돈까스(A001)
    equal_answers = [
        (q_soup, _opt(q_soup, "soup_no")),
        (q_style, _opt(q_style, "other_style")),
        (q_ing, _opt(q_ing, "meat")),
        (q_sodium, _opt(q_sodium, "low_sodium")),
        (q_heavy, _opt(q_heavy, "filling")),
        (q_spicy, _opt(q_spicy, "not_spicy")),
        (q_origin, _opt(q_origin, "eating_out")),
    ]
    res_equal = evaluate_order_sheet_candidates(
        menus=menus,
        exclusion_keywords=set(),
        validated_answers=equal_answers,
        recent_meal_penalties=penalties,
    )
    assert [(c.menu.id, c.rule_score, c.recent_meal_penalty, c.final_score) for c in res_equal.selected_candidates] == [
        (102, 4.0, 0.0, 4.0),
        (103, 4.0, 0.0, 4.0),
        (101, 4.0, -1.0, 3.0),
    ]

    # 2) 오늘 명시한 취향이 돈까스에 강하게 맞을 때 (점수제 2점 이상 우위) -> 돈까스가 1위 유지
    donkatsu_strong = _make_menu(
        menu_id=101,
        food_code="A001",
        name="돈까스",
        base_name="돈까스",
        has_soup=False,
        cooking_style="fried_pan",
        main_ingredient="pork",
        sodium_mg=200.0,
        heaviness="light",
        flavor_intensity="spicy_bold",
        origin_type="home_style",
    )
    res_pref = evaluate_order_sheet_candidates(
        menus=[donkatsu_strong, chicken_katsu, chicken],
        exclusion_keywords=set(),
        validated_answers=[
            (q_soup, _opt(q_soup, "soup_no")),
            (q_style, _opt(q_style, "other_style")),
            (q_ing, _opt(q_ing, "meat")),
            (q_sodium, _opt(q_sodium, "low_sodium")),
            (q_heavy, _opt(q_heavy, "light")),
            (q_spicy, _opt(q_spicy, "spicy_bold")),
            (q_origin, _opt(q_origin, "home_style")),
        ],
        recent_meal_penalties=penalties,
    )
    assert res_pref.selected_candidates[0].menu.id == 101
    assert res_pref.selected_candidates[0].rule_score == 4.0
    assert res_pref.selected_candidates[0].recent_meal_penalty == -1.0
    assert res_pref.selected_candidates[0].final_score == 3.0
    assert res_pref.selected_candidates[1].final_score == 1.0

    # 3) 오늘 하드필터를 통과한 유일한 메뉴가 방금 먹은 메뉴라도 제외하지 않고 추천한다
    rice_only_kimchi = _make_menu(
        menu_id=201,
        food_code="B001",
        name="김치볶음밥",
        base_name="김치볶음밥",
        has_soup=False,
        cooking_style="rice",
        main_ingredient="pork",
    )
    same_day_meals = [_meal(menu_id=201, menu_name="김치볶음밥", eaten_at=datetime(2026, 11, 24, 12, 0, tzinfo=kst))]
    same_day_penalties = compute_recent_meal_penalties(same_day_meals, [rice_only_kimchi, chicken], now=now)
    res_hard_only = evaluate_order_sheet_candidates(
        menus=[rice_only_kimchi, chicken],
        exclusion_keywords=set(),
        validated_answers=[
            (q_soup, _opt(q_soup, "soup_no")),
            (q_style, _opt(q_style, "rice")),
            (q_ing, _opt(q_ing, "meat")),
        ],
        recent_meal_penalties=same_day_penalties,
    )
    assert [c.menu.id for c in res_hard_only.selected_candidates] == [201]
    assert res_hard_only.selected_candidates[0].recent_meal_penalty == -2.0


def test_fixed_records_compare_penalty_magnitudes_windows_and_other_same_kind():
    kst = get_seoul_tz()
    now = datetime(2026, 11, 24, 18, 0, tzinfo=kst)

    q_soup = _question_from_pool("has_soup", qid=3)
    q_sodium = _question_from_pool("sodium_level", qid=1)
    q_heavy = _question_from_pool("heaviness", qid=2)

    # 같은 음식 종류('볶음밥' kind_id=10)에 속한 두 메뉴와 다른 종류('비빔밥' kind_id=20, '우동' kind_id=30, '카레' kind_id=40)
    # 참고: 서로 다른 음식 종류로도 비교할 수 있게 김치볶음밥과 새우볶음밥이 다른 kind가 아닌
    # 같은 kind_id=10인 경우와, 개별 kind로 노출되는 경우를 모두 확인한다.
    kimchi_rice = _make_menu(
        menu_id=1, food_code="C001", name="김치볶음밥", base_name="김치볶음밥", food_kind_id=10,
        has_soup=False, sodium_mg=200.0, heaviness="light",
    )
    shrimp_rice = _make_menu(
        menu_id=2, food_code="C002", name="새우볶음밥", base_name="새우볶음밥", food_kind_id=10,
        has_soup=False, sodium_mg=200.0, heaviness="light",
    )
    bibimbap = _make_menu(
        menu_id=3, food_code="C003", name="비빔밥", base_name="비빔밥", food_kind_id=20,
        has_soup=False, sodium_mg=200.0, heaviness="light",
    )
    udon_dry = _make_menu(
        menu_id=4, food_code="C004", name="볶음우동", base_name="볶음우동", food_kind_id=30,
        has_soup=False, sodium_mg=200.0, heaviness="light",
    )
    curry = _make_menu(
        menu_id=5, food_code="C005", name="카레라이스", base_name="카레라이스", food_kind_id=40,
        has_soup=False, sodium_mg=600.0, heaviness="heavy",  # 점수제 0점 (나머지는 2점)
    )
    menus = [kimchi_rice, shrimp_rice, bibimbap, udon_dry, curry]

    fixed_meals = [
        # 어제 김치볶음밥 (day_diff=1)
        _meal(menu_id=1, menu_name="김치볶음밥", eaten_at=datetime(2026, 11, 23, 12, 0, tzinfo=kst)),
        # 3일 전 볶음우동 (day_diff=3)
        _meal(menu_id=4, menu_name="볶음우동", eaten_at=datetime(2026, 11, 21, 12, 0, tzinfo=kst)),
    ]
    fixed_answers = [
        (q_soup, _opt(q_soup, "soup_no")),
        (q_sodium, _opt(q_sodium, "low_sodium")),
        (q_heavy, _opt(q_heavy, "light")),
    ]

    # 비교 1: 무감점 기준선
    p_none = compute_recent_meal_penalties(
        fixed_meals,
        menus,
        now=now,
        config=RecentMealPenaltyConfig(same_day=0.0, next_day=0.0, two_days_ago=0.0, other_same_kind=0.0),
    )
    r_none = evaluate_order_sheet_candidates(
        menus=menus, exclusion_keywords=set(), validated_answers=fixed_answers, recent_meal_penalties=p_none,
    )
    assert [c.menu.id for c in r_none.selected_candidates] == [1, 3, 4, 5]

    # 비교 2: 확정 기본값 (-2.0 / -1.0 / -0.5, 3일 기간, other_same_kind=0.0)
    # 같은 음식 종류(10) 내 새우볶음밥(0.0)이 김치볶음밥(-1.0)을 제치고 대표로 올라오고,
    # 3일 전 볶음우동은 0.0으로 유지된다.
    p_default = compute_recent_meal_penalties(
        fixed_meals,
        menus,
        now=now,
        config=RecentMealPenaltyConfig.from_settings(),
    )
    assert p_default.get(1, 0.0) == pytest.approx(-1.0)
    assert p_default.get(2, 0.0) == pytest.approx(0.0)
    assert p_default.get(4, 0.0) == pytest.approx(0.0)
    r_default = evaluate_order_sheet_candidates(
        menus=menus, exclusion_keywords=set(), validated_answers=fixed_answers, recent_meal_penalties=p_default,
    )
    assert [c.menu.id for c in r_default.selected_candidates] == [2, 3, 4, 5]

    # 비교 3: 기간을 4일로 확장 (day_penalties=(-2.0, -1.0, -0.5, -0.25)) -> 3일 전 볶음우동도 -0.25 감점
    p_window4 = compute_recent_meal_penalties(
        fixed_meals,
        menus,
        now=now,
        config=RecentMealPenaltyConfig(day_penalties=(-2.0, -1.0, -0.5, -0.25)),
    )
    assert p_window4.get(4, 0.0) == pytest.approx(-0.25)
    r_window4 = evaluate_order_sheet_candidates(
        menus=menus, exclusion_keywords=set(), validated_answers=fixed_answers, recent_meal_penalties=p_window4,
    )
    # 비빔밥(2.0), 새우볶음밥(2.0), 볶음우동(1.75), 카레라이스(0.0)
    assert [c.menu.id for c in r_window4.selected_candidates] == [2, 3, 4, 5]
    udon_cand = next(c for c in r_window4.selected_candidates if c.menu.id == 4)
    assert udon_cand.final_score == pytest.approx(1.75)

    # 비교 4: 같은 음식 종류 다른 메뉴에 소감점(-0.4) 적용 비교 -> 새우볶음밥(1.6)이 비빔밥(2.0)·볶음우동(2.0) 뒤로 이동
    p_same_kind = compute_recent_meal_penalties(
        fixed_meals,
        menus,
        now=now,
        config=RecentMealPenaltyConfig(same_day=-2.0, next_day=-1.0, two_days_ago=-0.5, other_same_kind=-0.4),
    )
    assert p_same_kind.get(1, 0.0) == pytest.approx(-1.0)
    assert p_same_kind.get(2, 0.0) == pytest.approx(-0.4)
    assert p_same_kind.get(3, 0.0) == pytest.approx(0.0)
    r_same_kind = evaluate_order_sheet_candidates(
        menus=menus, exclusion_keywords=set(), validated_answers=fixed_answers, recent_meal_penalties=p_same_kind,
    )
    assert [c.menu.id for c in r_same_kind.selected_candidates] == [3, 4, 2, 5]

    # 비교 5: 강한 감점 크기(-3.0) 비교 (단독 종류 메뉴로 비교 시 점수제 2점 우위인 김치볶음밥도 카레라이스(0.0) 아래로 이동)
    solo_menus = [kimchi_rice, bibimbap, curry]
    p_strong = compute_recent_meal_penalties(
        fixed_meals,
        solo_menus,
        now=now,
        config=RecentMealPenaltyConfig(same_day=-4.0, next_day=-3.0, two_days_ago=-1.5),
    )
    r_strong = evaluate_order_sheet_candidates(
        menus=solo_menus, exclusion_keywords=set(), validated_answers=fixed_answers, recent_meal_penalties=p_strong,
    )
    assert [c.menu.id for c in r_strong.selected_candidates] == [3, 5, 1]


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


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


def test_load_recent_meal_penalties_filters_user_and_window_from_db(db: Session):
    user = User(kakao_id=820001, nickname="식사유저A")
    other_user = User(kakao_id=820002, nickname="식사유저B")
    db.add_all([user, other_user])

    donkatsu = Menu(
        food_code="DB01", name="돈까스", base_name="돈까스", category_large="튀김",
        meal_type="main_meal", has_soup=False, cooking_style="fried_pan", main_ingredient="pork",
        flavor_intensity="mild_clean", temperature="hot", heaviness="heavy",
        origin_type="restaurant", sodium_mg=500.0, carb_g=40.0, protein_g=20.0, allergen_tags=[],
    )
    chicken_katsu = Menu(
        food_code="DB02", name="치킨까스", base_name="치킨까스", category_large="튀김",
        meal_type="main_meal", has_soup=False, cooking_style="fried_pan", main_ingredient="poultry",
        flavor_intensity="mild_clean", temperature="hot", heaviness="heavy",
        origin_type="restaurant", sodium_mg=500.0, carb_g=40.0, protein_g=20.0, allergen_tags=[],
    )
    db.add_all([donkatsu, chicken_katsu])
    db.flush()

    kst = get_seoul_tz()
    now_kst = datetime(2026, 11, 24, 18, 0, tzinfo=kst)

    db.add_all([
        # user의 당일(-2.0) + 어제(-1.0) -> 합계 -3.0
        UserMeal(
            user_id=user.id, menu_id=donkatsu.id, menu_name="돈까스",
            eaten_at=datetime(2026, 11, 24, 12, 0, tzinfo=kst).astimezone(timezone.utc).replace(tzinfo=None),
        ),
        UserMeal(
            user_id=user.id, menu_id=donkatsu.id, menu_name="돈까스",
            eaten_at=datetime(2026, 11, 23, 19, 0, tzinfo=kst).astimezone(timezone.utc).replace(tzinfo=None),
        ),
        # user의 3일 전 식사 -> 기간 밖(0.0)
        UserMeal(
            user_id=user.id, menu_id=chicken_katsu.id, menu_name="치킨까스",
            eaten_at=datetime(2026, 11, 21, 19, 0, tzinfo=kst).astimezone(timezone.utc).replace(tzinfo=None),
        ),
        # 다른 사용자의 당일 치킨까스 식사 -> user에 영향 없음
        UserMeal(
            user_id=other_user.id, menu_id=chicken_katsu.id, menu_name="치킨까스",
            eaten_at=datetime(2026, 11, 24, 12, 0, tzinfo=kst).astimezone(timezone.utc).replace(tzinfo=None),
        ),
    ])
    db.commit()

    penalties = load_recent_meal_penalties(
        db=db,
        user_id=user.id,
        menus=[donkatsu, chicken_katsu],
        now=now_kst,
    )
    assert penalties == {donkatsu.id: -3.0}


def test_create_order_sheet_recommendation_applies_db_meal_penalties_and_persists_snapshot(
    db: Session,
    monkeypatch,
):
    user = User(kakao_id=820003, nickname="서비스연동유저")
    db.add(user)
    q_map = _seed_questions(db)

    donkatsu = Menu(
        food_code="S001", name="돈까스", base_name="돈까스", category_large="튀김",
        meal_type="main_meal", has_soup=False, cooking_style="fried_pan", main_ingredient="pork",
        flavor_intensity="mild_clean", temperature="hot", heaviness="heavy",
        origin_type="restaurant", sodium_mg=200.0, carb_g=10.0, protein_g=20.0, allergen_tags=[],
    )
    chicken_katsu = Menu(
        food_code="S002", name="치킨까스", base_name="치킨까스", category_large="튀김",
        meal_type="main_meal", has_soup=False, cooking_style="fried_pan", main_ingredient="poultry",
        flavor_intensity="mild_clean", temperature="hot", heaviness="heavy",
        origin_type="restaurant", sodium_mg=200.0, carb_g=10.0, protein_g=20.0, allergen_tags=[],
    )
    chicken = Menu(
        food_code="S003", name="치킨", base_name="치킨", category_large="튀김",
        meal_type="main_meal", has_soup=False, cooking_style="fried_pan", main_ingredient="poultry",
        flavor_intensity="mild_clean", temperature="hot", heaviness="heavy",
        origin_type="restaurant", sodium_mg=200.0, carb_g=10.0, protein_g=20.0, allergen_tags=[],
    )
    db.add_all([donkatsu, chicken_katsu, chicken])
    db.flush()

    # 어제 돈까스를 먹은 기록 (KST 기준 정확히 1일 전 12:00 KST)
    kst = get_seoul_tz()
    now_kst = datetime.now(timezone.utc).astimezone(kst)
    yesterday_kst_noon = (now_kst - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    db.add(
        UserMeal(
            user_id=user.id,
            menu_id=donkatsu.id,
            menu_name="돈까스",
            eaten_at=yesterday_kst_noon.astimezone(timezone.utc).replace(tzinfo=None),
        )
    )
    db.commit()

    req = OrderSheetRecommendRequest(
        answers=[
            AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_no"),
            AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="other_style"),
            AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
            AnswerItemRequest(question_id=q_map["sodium_level"].id, option_id="low_sodium"),
            AnswerItemRequest(question_id=q_map["heaviness"].id, option_id="filling"),
            AnswerItemRequest(question_id=q_map["flavor_intensity"].id, option_id="not_spicy"),
            AnswerItemRequest(question_id=q_map["origin_type"].id, option_id="eating_out"),
            AnswerItemRequest(question_id=q_map["carb_level"].id, option_id="low_carb"),
            AnswerItemRequest(question_id=q_map["protein_level"].id, option_id="high_protein"),
        ],
        top_k=5,
    )

    resp = create_order_sheet_recommendation(db=db, request=req, user_id=user.id)
    assert [m.food_code for m in resp.recommended_menus] == ["S002", "S003", "S001"]

    snaps = (
        db.query(RecommendationItemSnapshot)
        .filter(RecommendationItemSnapshot.recommendation_id == resp.recommendation_id)
        .order_by(RecommendationItemSnapshot.rank_position.asc())
        .all()
    )
    assert [(s.menu_id, s.rule_score, s.recent_meal_adjustment, s.final_score) for s in snaps] == [
        (chicken_katsu.id, 6.0, 0.0, 6.0),
        (chicken.id, 6.0, 0.0, 6.0),
        (donkatsu.id, 6.0, -1.0, 5.0),
    ]

    # recent_meal_penalties={} 를 명시 전달하면 DB 식사 기록 감점을 우회(비교/재현용)한다.
    resp_override = create_order_sheet_recommendation(
        db=db,
        request=req,
        user_id=user.id,
        recent_meal_penalties={},
    )
    assert [m.food_code for m in resp_override.recommended_menus] == ["S001", "S002", "S003"]

    # 서버 설정을 0.0으로 변경하면 DB 식사 기록이 있어도 감점이 0점이 된다.
    monkeypatch.setattr(settings, "ORDER_SHEET_RECENT_MEAL_PENALTY_NEXT_DAY", 0.0)
    resp_zero_cfg = create_order_sheet_recommendation(db=db, request=req, user_id=user.id)
    assert [m.food_code for m in resp_zero_cfg.recommended_menus] == ["S001", "S002", "S003"]




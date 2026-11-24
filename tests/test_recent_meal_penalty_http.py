"""Spec #77 / Ticket #82: POST /api/recommendations/order-sheet 최근 식사 감점 HTTP 통합 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import handle_recommendation_domain_exception
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    RecommendationItemSnapshot,
    UserMeal,
)
from app.menus.popular import get_seoul_tz
from app.recommend.common import RecommendationDomainException
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.router import router
from app.users.models import User


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _make_client(db: Session, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(RecommendationDomainException, handle_recommendation_domain_exception)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


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


def _add_menu(
    db: Session,
    *,
    food_code: str,
    name: str,
    base_name: str,
    has_soup: bool = False,
    cooking_style: str = "rice",
    main_ingredient: str = "pork",
    sodium_mg: float | None = 200.0,
    heaviness: str = "light",
    flavor_intensity: str = "mild_clean",
    origin_type: str = "restaurant",
    carb_g: float | None = 10.0,
    protein_g: float | None = 20.0,
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


def _kst_days_ago_utc_naive(days_ago: int) -> datetime:
    """현재 한국 날짜(KST) 기준 정확히 days_ago일 전의 안전한 UTC naive 시각을 반환한다."""
    kst = get_seoul_tz()
    now_kst = datetime.now(timezone.utc).astimezone(kst)
    target_date = now_kst.date() - timedelta(days=days_ago)
    if days_ago == 0:
        # 당일 00:00:00 KST는 항상 오늘 날짜이면서 now_kst 이하이다.
        dt_kst = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=kst)
    else:
        dt_kst = datetime(target_date.year, target_date.month, target_date.day, 12, 0, 0, tzinfo=kst)
    return dt_kst.astimezone(timezone.utc).replace(tzinfo=None)


def test_http_order_sheet_day_decay_and_cumulative_same_menu_penalty(db: Session):
    user = User(kakao_id=821001, nickname="HTTP감점테스터")
    db.add(user)
    q_map = _seed_questions(db)

    # 5종 모두 동일한 점수(rule_score=6.0)를 받도록 설정해 최근 식사 감점 차이가 순위를 결정하도록 한다.
    m_today_and_yest = _add_menu(db, food_code="P01", name="제육덮밥", base_name="제육덮밥")
    m_today = _add_menu(db, food_code="P02", name="김치볶음밥", base_name="김치볶음밥")
    m_yesterday = _add_menu(db, food_code="P03", name="오므라이스", base_name="오므라이스")
    m_two_days = _add_menu(db, food_code="P04", name="카레라이스", base_name="카레라이스")
    m_three_days = _add_menu(db, food_code="P05", name="비빔밥", base_name="비빔밥")

    db.add_all([
        # 제육덮밥: 오늘(-2.0) + 어제(-1.0) = -3.0
        UserMeal(user_id=user.id, menu_id=m_today_and_yest.id, menu_name="제육덮밥", eaten_at=_kst_days_ago_utc_naive(0)),
        UserMeal(user_id=user.id, menu_id=m_today_and_yest.id, menu_name="제육덮밥", eaten_at=_kst_days_ago_utc_naive(1)),
        # 김치볶음밥: 오늘(-2.0)
        UserMeal(user_id=user.id, menu_id=m_today.id, menu_name="김치볶음밥", eaten_at=_kst_days_ago_utc_naive(0)),
        # 오므라이스: 어제(-1.0)
        UserMeal(user_id=user.id, menu_id=m_yesterday.id, menu_name="오므라이스", eaten_at=_kst_days_ago_utc_naive(1)),
        # 카레라이스: 이틀 전(-0.5)
        UserMeal(user_id=user.id, menu_id=m_two_days.id, menu_name="카레라이스", eaten_at=_kst_days_ago_utc_naive(2)),
        # 비빔밥: 3일 전(0.0)
        UserMeal(user_id=user.id, menu_id=m_three_days.id, menu_name="비빔밥", eaten_at=_kst_days_ago_utc_naive(3)),
    ])
    db.commit()

    payload = {
        "answers": [
            {"question_id": q_map["has_soup"].id, "option_id": "soup_no"},
            {"question_id": q_map["cooking_style"].id, "option_id": "rice"},
            {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
            {"question_id": q_map["sodium_level"].id, "option_id": "low_sodium"},
            {"question_id": q_map["heaviness"].id, "option_id": "light"},
            {"question_id": q_map["flavor_intensity"].id, "option_id": "not_spicy"},
            {"question_id": q_map["origin_type"].id, "option_id": "eating_out"},
            {"question_id": q_map["carb_level"].id, "option_id": "low_carb"},
            {"question_id": q_map["protein_level"].id, "option_id": "high_protein"},
        ],
        "top_k": 5,
    }

    with _make_client(db, user) as client:
        res = client.post("/api/recommendations/order-sheet", json=payload)
        assert res.status_code == 201
        data = res.json()["data"]
        # 기대 순위: 비빔밥(0.0) > 카레라이스(-0.5) > 오므라이스(-1.0) > 김치볶음밥(-2.0) > 제육덮밥(-3.0)
        assert [m["food_code"] for m in data["recommended_menus"]] == [
            "P05",
            "P04",
            "P03",
            "P02",
            "P01",
        ]

        snaps = (
            db.query(RecommendationItemSnapshot)
            .filter(RecommendationItemSnapshot.recommendation_id == data["recommendation_id"])
            .order_by(RecommendationItemSnapshot.rank_position.asc())
            .all()
        )
        assert [(s.menu_id, s.recent_meal_adjustment, s.final_score) for s in snaps] == [
            (m_three_days.id, 0.0, 6.0),
            (m_two_days.id, -0.5, 5.5),
            (m_yesterday.id, -1.0, 5.0),
            (m_today.id, -2.0, 4.0),
            (m_today_and_yest.id, -3.0, 3.0),
        ]


def test_http_order_sheet_donkatsu_and_fried_rice_scope_plus_preference_priority_and_settings_override(
    db: Session,
    monkeypatch,
):
    user = User(kakao_id=821002, nickname="HTTP범위테스터")
    db.add(user)
    q_map = _seed_questions(db)

    # 돈까스·치킨까스·치킨 (국물 없음 / 그 외 / 고기)
    donkatsu = _add_menu(
        db, food_code="D01", name="돈까스", base_name="돈까스",
        cooking_style="fried_pan", main_ingredient="pork",
        sodium_mg=200.0, heaviness="light", flavor_intensity="spicy_bold",
        origin_type="home_style", carb_g=10.0, protein_g=20.0,
    )
    chicken_katsu = _add_menu(
        db, food_code="D02", name="치킨까스", base_name="치킨까스",
        cooking_style="fried_pan", main_ingredient="poultry",
        sodium_mg=200.0, heaviness="light", flavor_intensity="spicy_bold",
        origin_type="home_style", carb_g=10.0, protein_g=20.0,
    )
    chicken = _add_menu(
        db, food_code="D03", name="치킨", base_name="치킨",
        cooking_style="fried_pan", main_ingredient="poultry",
        sodium_mg=700.0, heaviness="heavy", flavor_intensity="mild_clean",
        origin_type="restaurant", carb_g=60.0, protein_g=5.0,
    )

    # 같은 음식 종류('볶음밥') 내 김치볶음밥 vs 새우볶음밥 (국물 없음 / 밥 / 고기)
    kimchi_fried_rice = _add_menu(
        db, food_code="F01", name="볶음밥_김치", base_name="볶음밥",
        cooking_style="rice", main_ingredient="pork",
        sodium_mg=200.0, heaviness="light", flavor_intensity="spicy_bold",
        origin_type="home_style", carb_g=10.0, protein_g=20.0,
    )
    shrimp_fried_rice = _add_menu(
        db, food_code="F02", name="볶음밥_새우", base_name="볶음밥",
        cooking_style="rice", main_ingredient="pork",
        sodium_mg=200.0, heaviness="light", flavor_intensity="spicy_bold",
        origin_type="home_style", carb_g=10.0, protein_g=20.0,
    )

    db.add_all([
        UserMeal(user_id=user.id, menu_id=donkatsu.id, menu_name="돈까스", eaten_at=_kst_days_ago_utc_naive(1)),
        UserMeal(user_id=user.id, menu_id=kimchi_fried_rice.id, menu_name="김치볶음밥", eaten_at=_kst_days_ago_utc_naive(1)),
    ])
    db.commit()

    with _make_client(db, user) as client:
        # 1) 돈까스/치킨까스/치킨 비교:
        #    치킨까스(6.0, 감점 0.0) > 돈까스(6.0 - 1.0 = 5.0) > 치킨(0.0, 감점 0.0)
        #    -> 어제 먹은 돈까스도 오늘 취향에 크게 맞으면 불일치 메뉴(치킨)보다 앞선다.
        res_katsu = client.post(
            "/api/recommendations/order-sheet",
            json={
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_no"},
                    {"question_id": q_map["cooking_style"].id, "option_id": "other_style"},
                    {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
                    {"question_id": q_map["sodium_level"].id, "option_id": "low_sodium"},
                    {"question_id": q_map["heaviness"].id, "option_id": "light"},
                    {"question_id": q_map["flavor_intensity"].id, "option_id": "spicy_bold"},
                    {"question_id": q_map["origin_type"].id, "option_id": "home_style"},
                    {"question_id": q_map["carb_level"].id, "option_id": "low_carb"},
                    {"question_id": q_map["protein_level"].id, "option_id": "high_protein"},
                ],
                "top_k": 5,
            },
        )
        assert res_katsu.status_code == 201
        katsu_data = res_katsu.json()["data"]
        assert [m["food_code"] for m in katsu_data["recommended_menus"]] == ["D02", "D01", "D03"]
        katsu_snaps = {
            s.menu_id: s.recent_meal_adjustment
            for s in db.query(RecommendationItemSnapshot)
            .filter(RecommendationItemSnapshot.recommendation_id == katsu_data["recommendation_id"])
            .all()
        }
        assert katsu_snaps == {
            chicken_katsu.id: 0.0,
            donkatsu.id: -1.0,
            chicken.id: 0.0,
        }

        # 2) 김치볶음밥 어제 식사 후 볶음밥(밥) 요청 -> 새우볶음밥은 감점 0.0으로 대표 선정
        res_rice = client.post(
            "/api/recommendations/order-sheet",
            json={
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_no"},
                    {"question_id": q_map["cooking_style"].id, "option_id": "rice"},
                    {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
                    {"question_id": q_map["sodium_level"].id, "option_id": "low_sodium"},
                    {"question_id": q_map["heaviness"].id, "option_id": "light"},
                    {"question_id": q_map["flavor_intensity"].id, "option_id": "spicy_bold"},
                    {"question_id": q_map["origin_type"].id, "option_id": "home_style"},
                    {"question_id": q_map["carb_level"].id, "option_id": "low_carb"},
                    {"question_id": q_map["protein_level"].id, "option_id": "high_protein"},
                ],
                "top_k": 5,
            },
        )
        assert res_rice.status_code == 201
        rice_data = res_rice.json()["data"]
        assert [m["food_code"] for m in rice_data["recommended_menus"]] == ["F02"]
        rice_snap = (
            db.query(RecommendationItemSnapshot)
            .filter(RecommendationItemSnapshot.recommendation_id == rice_data["recommendation_id"])
            .one()
        )
        assert rice_snap.menu_id == shrimp_fried_rice.id
        assert rice_snap.recent_meal_adjustment == 0.0

        # 3) 서버 설정으로 next_day 감점을 0.0으로 바꾸면 기존 food_code 순(F01 김치볶음밥)으로 복원된다
        monkeypatch.setattr(settings, "ORDER_SHEET_RECENT_MEAL_PENALTY_NEXT_DAY", 0.0)
        res_rice_no_penalty = client.post(
            "/api/recommendations/order-sheet",
            json={
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_no"},
                    {"question_id": q_map["cooking_style"].id, "option_id": "rice"},
                    {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
                    {"question_id": q_map["sodium_level"].id, "option_id": "low_sodium"},
                    {"question_id": q_map["heaviness"].id, "option_id": "light"},
                    {"question_id": q_map["flavor_intensity"].id, "option_id": "spicy_bold"},
                    {"question_id": q_map["origin_type"].id, "option_id": "home_style"},
                    {"question_id": q_map["carb_level"].id, "option_id": "low_carb"},
                    {"question_id": q_map["protein_level"].id, "option_id": "high_protein"},
                ],
                "top_k": 5,
            },
        )
        assert res_rice_no_penalty.status_code == 201
        assert [m["food_code"] for m in res_rice_no_penalty.json()["data"]["recommended_menus"]] == ["F01"]

"""Spec #77 / Ticket #83: POST /api/recommendations/order-sheet 장기 취향 보정 HTTP 통합 테스트."""

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
from app.main import (
    handle_place_search_exception,
    handle_recommendation_domain_exception,
)
from app.map.exceptions import PlaceSearchException
import app.map.models as map_models  # noqa: F401 - register place tables
from app.map.models import PlaceSatisfaction, PlaceSelection
from app.map.router import router as map_router
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedbackState,
    RecommendationItemSnapshot,
)
from app.recommend.common import RecommendationDomainException
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.router import router as recommend_router
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
    app.include_router(recommend_router)
    app.include_router(map_router)
    app.add_exception_handler(RecommendationDomainException, handle_recommendation_domain_exception)
    app.add_exception_handler(PlaceSearchException, handle_place_search_exception)
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
    has_soup: bool = True,
    cooking_style: str = "noodle",
    main_ingredient: str = "other",
    flavor_intensity: str = "mild_clean",
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
        sodium_mg=500.0,
        heaviness="moderate",
        flavor_intensity=flavor_intensity,
        origin_type="restaurant",
        carb_g=50.0,
        protein_g=12.0,
        temperature="hot",
        allergen_tags=[],
    )
    db.add(menu)
    db.flush()
    return menu


def _answers_payload(q_map: dict[str, OrderSheetQuestion], overrides: dict[str, str] | None = None) -> dict:
    opts = {cluster: "any" for cluster in settings.ORDER_SHEET_INITIAL_CLUSTERS}
    if overrides:
        opts.update(overrides)
    return {
        "answers": [
            {"question_id": q_map[cluster].id, "option_id": opt_id}
            for cluster, opt_id in opts.items()
        ],
        "top_k": 5,
    }


def test_http_first_menu_reactions_apply_same_day_and_exclude_reject_and_map_reactions(db: Session):
    user = User(kakao_id=831001, nickname="HTTP장기취향유저")
    db.add(user)
    q_map = _seed_questions(db)

    # 동점 순서: 비빔밥(A001) < 라면(A002) < 짜장면(A003) < 우동(A004)
    bibimbap = _add_menu(db, food_code="A001", name="비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice")
    ramen = _add_menu(db, food_code="A002", name="신라면", base_name="라면", has_soup=True, cooking_style="noodle")
    jjajang = _add_menu(db, food_code="A003", name="간짜장", base_name="짜장면", has_soup=False, cooking_style="noodle")
    udon_a = _add_menu(db, food_code="A004", name="어묵우동", base_name="우동", has_soup=True, cooking_style="noodle")
    db.commit()

    with _make_client(db, user) as client:
        # 1. 첫 주문서 제출 -> 비빔밥이 1위
        r1 = client.post("/api/recommendations/order-sheet", json=_answers_payload(q_map))
        assert r1.status_code == 201
        d1 = r1.json()["data"]
        assert [m["base_name"] for m in d1["recommended_menus"]] == ["비빔밥", "라면", "짜장면", "우동"]

        # 2. 짜장면에 첫 싫어요를 누르면 같은 날 다음 주문서에서 순위가 내려간다.
        fb1 = client.post(
            f"/api/recommendations/{d1['recommendation_id']}/feedback",
            json={"selected_menu_id": jjajang.id, "is_satisfied": False},
        )
        assert fb1.status_code == 200
        assert fb1.json()["data"]["updated_weights"] == {}

        r2 = client.post("/api/recommendations/order-sheet", json=_answers_payload(q_map))
        assert r2.status_code == 201
        d2 = r2.json()["data"]
        assert [m["base_name"] for m in d2["recommended_menus"]] == ["비빔밥", "라면", "우동", "짜장면"]

        # 3. 우동에 첫 좋아요를 누르면 같은 날 다음 주문서에서 우동이 1위가 된다.
        assert (
            client.post(
                f"/api/recommendations/{d2['recommendation_id']}/feedback",
                json={"selected_menu_id": udon_a.id, "is_satisfied": True},
            ).status_code
            == 200
        )
        r3 = client.post("/api/recommendations/order-sheet", json=_answers_payload(q_map))
        assert r3.status_code == 201
        d3 = r3.json()["data"]
        assert [m["base_name"] for m in d3["recommended_menus"]] == ["우동", "비빔밥", "라면", "짜장면"]

        # 목록 거절 + 지도 가게 선택·만족을 라면에 대해 기록 (서로 다른 날 시뮬레이션)
        reject_rec = client.post("/api/recommendations/order-sheet", json=_answers_payload(q_map)).json()["data"]
        assert (
            client.post(f"/api/recommendations/{reject_rec['recommendation_id']}/all-menu-reject").status_code
            == 200
        )
        sel1 = client.post(
            "/api/places/selections",
            json={
                "recommendation_id": d2["recommendation_id"],
                "menu_id": ramen.id,
                "kakao_place_id": "kp_ramen_1",
                "place_name": "라면가게1",
            },
        )
        assert sel1.status_code == 201
        sel1_id = sel1.json()["data"]["id"]
        assert (
            client.post(
                f"/api/places/selections/{sel1_id}/satisfaction",
                json={"is_satisfied": True},
            ).status_code
            == 200
        )
        sel2 = client.post(
            "/api/places/selections",
            json={
                "recommendation_id": d3["recommendation_id"],
                "menu_id": ramen.id,
                "kakao_place_id": "kp_ramen_2",
                "place_name": "라면가게2",
            },
        )
        assert sel2.status_code == 201
        sel2_id = sel2.json()["data"]["id"]
        assert (
            client.post(
                f"/api/places/selections/{sel2_id}/satisfaction",
                json={"is_satisfied": True},
            ).status_code
            == 200
        )

        # 지도 선택 1건을 어제로 이동시켜 2일치 지도 만족을 만들어도 장기 취향에는 반영되지 않는다
        yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        db.query(PlaceSelection).filter(PlaceSelection.id == sel1_id).update({"created_at": yesterday})
        db.query(PlaceSatisfaction).filter(PlaceSatisfaction.place_selection_id == sel1_id).update(
            {"created_at": yesterday, "updated_at": yesterday}
        )
        db.commit()

        # 목록 거절과 지도 만족을 남긴 뒤에도 메뉴별 보정 점수만 적용된다.
        r4 = client.post("/api/recommendations/order-sheet", json=_answers_payload(q_map))
        assert r4.status_code == 201
        d4 = r4.json()["data"]
        assert [m["base_name"] for m in d4["recommended_menus"]] == ["우동", "비빔밥", "라면", "짜장면"]

        snaps = {
            s.menu_id: s.personal_adjustment
            for s in db.query(RecommendationItemSnapshot)
            .filter(RecommendationItemSnapshot.recommendation_id == d4["recommendation_id"])
            .all()
        }
        assert snaps == {udon_a.id: 0.25, bibimbap.id: 0.0, ramen.id: 0.0, jjajang.id: -0.25}


def test_http_spec_section3_decay_reversal_and_todays_answer_priority(db: Session):
    user = User(kakao_id=831002, nickname="HTTP반감기유저")
    db.add(user)
    q_map = _seed_questions(db)

    # 동점 기준: 우동(A001) < 비빔밥(A002)
    udon = _add_menu(
        db,
        food_code="A001",
        name="어묵우동",
        base_name="우동",
        has_soup=True,
        cooking_style="noodle",
        flavor_intensity="mild_clean",
    )
    bibimbap = _add_menu(
        db,
        food_code="A002",
        name="매운비빔밥",
        base_name="비빔밥",
        has_soup=False,
        cooking_style="rice",
        flavor_intensity="spicy_bold",
    )
    db.commit()

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

    def _add_reaction(days_ago: int, is_satisfied: bool) -> None:
        ts = now_naive - timedelta(days=days_ago)
        rec = Recommendation(
            user_id=user.id,
            shown_question_ids=[],
            answers_json=[],
            recommended_menu_ids=[udon.id],
            initial_candidate_count=1,
            created_at=ts,
        )
        db.add(rec)
        db.flush()
        db.add(
            RecommendationFeedbackState(
                recommendation_id=rec.id,
                user_id=user.id,
                selected_menu_id=udon.id,
                food_kind_id=udon.food_kind_id,
                exposure_rank=1,
                menu_key="우동",
                menu_base_name="우동",
                menu_main_ingredient=udon.main_ingredient,
                is_satisfied=is_satisfied,
                answer_match_results={},
                created_at=ts,
                updated_at=ts,
            )
        )
        db.flush()

    # 30일·28일 전 좋아요 + 1일 전 싫어요 -> 우동(+0.064) > 비빔밥(0)
    _add_reaction(30, True)
    _add_reaction(28, True)
    _add_reaction(1, False)
    db.commit()

    with _make_client(db, user) as client:
        res_pos = client.post("/api/recommendations/order-sheet", json=_answers_payload(q_map))
        assert res_pos.status_code == 201
        assert [m["base_name"] for m in res_pos.json()["data"]["recommended_menus"]] == ["우동", "비빔밥"]

        # 오늘 답변(매콤한 음식)이 비빔밥에만 1점 일치 -> 우동 양수 보정이 있어도 비빔밥이 1위
        res_score = client.post(
            "/api/recommendations/order-sheet",
            json=_answers_payload(q_map, {"flavor_intensity": "spicy_bold"}),
        )
        assert res_score.status_code == 201
        assert [m["base_name"] for m in res_score.json()["data"]["recommended_menus"]] == ["비빔밥", "우동"]

        # 오늘 하드필터(국물 없음) -> 우동 제외, 비빔밥만 반환
        res_hard = client.post(
            "/api/recommendations/order-sheet",
            json=_answers_payload(q_map, {"has_soup": "soup_no"}),
        )
        assert res_hard.status_code == 201
        assert [m["base_name"] for m in res_hard.json()["data"]["recommended_menus"]] == ["비빔밥"]

        # 이번 주 4일 전 싫어요 추가 (30·28일 전 좋아요 + 4·1일 전 싫어요 -> -0.170) -> 비빔밥 > 우동 역전
        _add_reaction(4, False)
        db.commit()

        res_neg = client.post("/api/recommendations/order-sheet", json=_answers_payload(q_map))
        assert res_neg.status_code == 201
        assert [m["base_name"] for m in res_neg.json()["data"]["recommended_menus"]] == ["비빔밥", "우동"]

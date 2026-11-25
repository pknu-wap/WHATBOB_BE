"""Spec #77 / Ticket #81: POST /api/recommendations/order-sheet HTTP 통합 테스트."""

from __future__ import annotations

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
    OrderSheetIssuance,
    OrderSheetQuestion,
    Recommendation,
    UserFoodExclusion,
)
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
    has_soup: bool = True,
    cooking_style: str = "rice",
    main_ingredient: str = "beef",
    sodium_mg: float | None = 500.0,
    heaviness: str = "moderate",
    flavor_intensity: str = "mild_clean",
    origin_type: str = "restaurant",
    carb_g: float | None = 50.0,
    protein_g: float | None = 15.0,
    allergen_tags: list[str] | None = None,
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
        allergen_tags=list(allergen_tags or []),
    )
    db.add(menu)
    db.flush()
    return menu


@pytest.mark.parametrize(
    "surviving_kind_count, expected_shown_count",
    [(0, 0), (1, 1), (3, 3), (5, 5), (6, 5)],
)
def test_http_order_sheet_submit_counts_0_1_3_5_6_kinds_and_dedups_multi_products(
    db: Session,
    surviving_kind_count: int,
    expected_shown_count: int,
):
    user = User(kakao_id=811001, nickname="HTTP카운트테스터")
    db.add(user)
    q_map = _seed_questions(db)

    kind_names = ["국밥", "설렁탕", "갈비탕", "육개장", "곰탕", "추어탕"]
    for idx in range(surviving_kind_count):
        kind = kind_names[idx]
        # 같은 음식 종류에 상품 3개씩 생성: 마지막 상품만 매콤·고단백·저탄수 일치
        for prod_idx in range(3):
            is_best = prod_idx == 2
            _add_menu(
                db,
                food_code=f"K{idx:02d}_{prod_idx}",
                name=f"{kind}_상품{prod_idx}",
                base_name=kind,
                has_soup=True,
                cooking_style="rice",
                main_ingredient="beef",
                sodium_mg=200.0 if (is_best and idx == 0) else 700.0,
                heaviness="light" if is_best else "heavy",
                flavor_intensity="spicy_bold" if is_best else "mild_clean",
                origin_type="home_style" if is_best else "restaurant",
                carb_g=10.0 if is_best else None,  # 미확인 속성(None)은 가점 0점
                protein_g=20.0 if is_best else None,
            )

    # 하드필터(국물 있음)에서 탈락하는 비국물 메뉴 5종 (자동 완화 금지 검증)
    for idx, name in enumerate(("비빔밥", "불고기덮밥", "김치볶음밥", "카레라이스", "오므라이스"), start=1):
        _add_menu(
            db,
            food_code=f"DRY{idx:02d}",
            name=name,
            base_name=name,
            has_soup=False,
            cooking_style="rice",
            main_ingredient="beef",
            sodium_mg=200.0,
            heaviness="light",
            flavor_intensity="spicy_bold",
            origin_type="home_style",
            carb_g=10.0,
            protein_g=20.0,
        )
    db.commit()

    payload = {
        "answers": [
            {"question_id": q_map["sodium_level"].id, "option_id": "low_sodium"},
            {"question_id": q_map["heaviness"].id, "option_id": "light"},
            {"question_id": q_map["has_soup"].id, "option_id": "soup_yes"},
            {"question_id": q_map["flavor_intensity"].id, "option_id": "spicy_bold"},
            {"question_id": q_map["origin_type"].id, "option_id": "home_style"},
            {"question_id": q_map["carb_level"].id, "option_id": "low_carb"},
            {"question_id": q_map["protein_level"].id, "option_id": "high_protein"},
            {"question_id": q_map["cooking_style"].id, "option_id": "rice"},
            {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
        ],
        "top_k": 5,
    }

    with _make_client(db, user) as client:
        res = client.post("/api/recommendations/order-sheet", json=payload)
        assert res.status_code == 201
        body = res.json()
        assert body["success"] is True
        data = body["data"]
        assert data["candidate_count"] == surviving_kind_count
        assert len(data["recommended_menus"]) == expected_shown_count

        shown_bases = [item["base_name"] for item in data["recommended_menus"]]
        assert len(shown_bases) == len(set(shown_bases))
        for item in data["recommended_menus"]:
            assert item["has_soup"] is True
            assert item["food_code"].endswith("_2")
            assert "상품2" in item["name"]

        if surviving_kind_count >= 1:
            assert data["recommended_menus"][0]["base_name"] == "국밥"
            assert data["recommended_menus"][0]["match_score"] == 6.0

        rec = db.get(Recommendation, data["recommendation_id"])
        assert rec is not None
        assert rec.initial_candidate_count == surviving_kind_count
        assert len(rec.recommended_menu_ids) == expected_shown_count


def test_http_issue_then_submit_with_exclusions_missing_attrs_neutral_and_role_override(
    db: Session,
    monkeypatch,
):
    user = User(kakao_id=811002, nickname="HTTP전체흐름유저")
    db.add(user)
    db.flush()
    db.add(UserFoodExclusion(user_id=user.id, exclusion_type="ALLERGY", keyword="돼지고기", catalog_code="pork"))
    q_map = _seed_questions(db)

    # 돼지고기 알레르기 메뉴 (Step 0 제외)
    _add_menu(
        db,
        food_code="A001",
        name="돼지국밥",
        base_name="돼지국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="pork",
        allergen_tags=["돼지고기"],
    )
    # 정상 통과 + 점수 높음
    _add_menu(
        db,
        food_code="A002",
        name="소고기미역국밥",
        base_name="미역국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="beef",
        sodium_mg=200.0,
        heaviness="light",
        flavor_intensity="mild_clean",
        origin_type="home_style",
        carb_g=None,
        protein_g=22.0,
    )
    # 정상 통과 + 영양 미확인(carb_g=None, protein_g=None, sodium_mg=None)
    _add_menu(
        db,
        food_code="A003",
        name="닭곰탕밥",
        base_name="닭곰탕",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="poultry",
        sodium_mg=None,
        heaviness="heavy",
        flavor_intensity="mild_clean",
        origin_type="restaurant",
        carb_g=None,
        protein_g=None,
    )
    # 면 요리 (Q8=rice 하드필터에서 탈락)
    _add_menu(
        db,
        food_code="A004",
        name="소고기쌀국수",
        base_name="쌀국수",
        has_soup=True,
        cooking_style="noodle",
        main_ingredient="beef",
        sodium_mg=200.0,
        heaviness="light",
        flavor_intensity="spicy_bold",
        origin_type="home_style",
        carb_g=10.0,
        protein_g=25.0,
    )
    db.commit()

    with _make_client(db, user) as client:
        issued_res = client.get("/api/recommendations/order-sheet/questions")
        assert issued_res.status_code == 200
        order_sheet_id = issued_res.json()["data"]["order_sheet_id"]

        submit_res = client.post(
            "/api/recommendations/order-sheet",
            json={
                "order_sheet_id": order_sheet_id,
                "answers": [
                    {"question_id": q_map["sodium_level"].id, "option_id": "low_sodium"},
                    {"question_id": q_map["heaviness"].id, "option_id": "light"},
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_yes"},
                    {"question_id": q_map["flavor_intensity"].id, "option_id": "any"},
                    {"question_id": q_map["origin_type"].id, "option_id": "home_style"},
                    {"question_id": q_map["carb_level"].id, "option_id": "low_carb"},
                    {"question_id": q_map["protein_level"].id, "option_id": "high_protein"},
                    {"question_id": q_map["cooking_style"].id, "option_id": "rice"},
                    {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
                ],
                "top_k": 5,
            },
        )
        assert submit_res.status_code == 201
        data = submit_res.json()["data"]
        assert data["excluded_count"] == 1
        assert data["candidate_count"] == 2
        assert [(m["food_code"], m["match_score"]) for m in data["recommended_menus"]] == [
            ("A002", 4.0),  # low_sodium, light, home_style, high_protein (carb_g=None -> 0, flavor=any -> 0)
            ("A003", 0.0),
        ]

        issuance = (
            db.query(OrderSheetIssuance)
            .filter(OrderSheetIssuance.order_sheet_id == order_sheet_id)
            .one()
        )
        assert issuance.recommendation_id == data["recommendation_id"]
        assert issuance.completed_at is not None
        assert issuance.status == "COMPLETED"

        # 역할 오버라이드: cooking_style을 score로 바꾸면 쌀국수(A004)도 후보에 포함된다
        monkeypatch.setattr(
            settings,
            "ORDER_SHEET_QUESTION_ROLE_OVERRIDES",
            {q_map["cooking_style"].id: "score"},
        )
        override_res = client.post(
            "/api/recommendations/order-sheet",
            json={
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_yes"},
                    {"question_id": q_map["cooking_style"].id, "option_id": "rice"},
                    {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
                ],
                "top_k": 5,
            },
        )
        assert override_res.status_code == 201
        override_data = override_res.json()["data"]
        assert override_data["candidate_count"] == 3
        assert [m["food_code"] for m in override_data["recommended_menus"]] == ["A002", "A003", "A004"]

        # 제외 조건만으로 후보가 0개면 404 반환 (하드필터 0건의 201과 구분)
        all_excluded = client.post(
            "/api/recommendations/order-sheet",
            json={
                "exclusions": ["미역국밥", "닭곰탕", "쌀국수"],
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "any"},
                ],
                "top_k": 5,
            },
        )
        assert all_excluded.status_code == 404
        assert all_excluded.json()["success"] is False

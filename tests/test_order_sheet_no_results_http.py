"""Spec #77 / Ticket #84: POST /api/recommendations/order-sheet/no-results HTTP 통합 테스트."""

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
    QuestionSelectionState,
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


def _make_client(db: Session, user: User | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(RecommendationDomainException, handle_recommendation_domain_exception)
    app.dependency_overrides[get_db] = lambda: db
    if user is not None:
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


def test_http_no_results_is_read_only_sorts_by_capped_count_and_resubmit_matches(db: Session):
    user = User(kakao_id=840001, nickname="후보0건제안테스터")
    db.add(user)
    db.flush()
    db.add(
        UserFoodExclusion(
            user_id=user.id,
            exclusion_type="ALLERGY",
            keyword="돼지고기",
            catalog_code="pork",
        )
    )
    q_map = _seed_questions(db)

    # 원래 답변: has_soup=soup_yes, cooking_style=rice, main_ingredient=meat -> 0종
    # has_soup -> any: 6종 (상한 5종)
    for idx in range(6):
        kind = f"덮밥{idx}"
        _add_menu(
            db,
            food_code=f"S{idx:02d}",
            name=kind,
            base_name=kind,
            has_soup=False,
            cooking_style="rice",
            main_ingredient="beef",
        )
    # cooking_style -> any: 10종 (상한 5종 -> has_soup와 동률)
    for idx in range(10):
        kind = f"국수탕{idx}"
        _add_menu(
            db,
            food_code=f"C{idx:02d}",
            name=kind,
            base_name=kind,
            has_soup=True,
            cooking_style="noodle",
            main_ingredient="beef",
        )
    # main_ingredient -> any: 정상 2종 + 사용자 알레르기(돼지고기) 1종 + 요청 제외 키워드("오징어") 1종
    for idx, kind in enumerate(("해물국밥", "굴국밥")):
        _add_menu(
            db,
            food_code=f"M{idx:02d}",
            name=kind,
            base_name=kind,
            has_soup=True,
            cooking_style="rice",
            main_ingredient="seafood",
        )
    _add_menu(
        db,
        food_code="M_EXCL_ALLERGY",
        name="돼지육수국밥",
        base_name="돼지육수국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="seafood",
        allergen_tags=["돼지고기"],
    )
    _add_menu(
        db,
        food_code="M_EXCL_REQ",
        name="오징어국밥",
        base_name="오징어국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="seafood",
    )
    db.commit()

    with _make_client(db, user) as client:
        issued_res = client.get("/api/recommendations/order-sheet/questions")
        assert issued_res.status_code == 200
        order_sheet_id = issued_res.json()["data"]["order_sheet_id"]

        # 조회 전 DB 상태 스냅샷
        rec_count_before = db.query(Recommendation).count()
        issuance_before = (
            db.query(OrderSheetIssuance)
            .filter(OrderSheetIssuance.order_sheet_id == order_sheet_id)
            .one()
        )
        assert issuance_before.status == "ISSUED"
        assert issuance_before.recommendation_id is None
        shown_counts_before = {
            q.id: int(q.shown_count or 0) for q in db.query(OrderSheetQuestion).all()
        }
        state_before = db.get(QuestionSelectionState, f"user:{user.id}")
        state_snapshot_before = (
            (list(state_before.last_clusters or []), dict(state_before.topic_counts or {}))
            if state_before is not None
            else None
        )

        # 원래 제출 본문과 동일한 형식으로 /no-results 호출
        payload = {
            "order_sheet_id": order_sheet_id,
            "exclusions": ["오징어"],
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

        no_res = client.post("/api/recommendations/order-sheet/no-results", json=payload)
        assert no_res.status_code == 200
        body = no_res.json()
        assert body["success"] is True
        assert body["message"] is None
        data = body["data"]
        assert data["can_review_my_answers"] is True
        changes = data["suggested_answer_changes"]
        assert [
            (item["question_id"], item["replacement_option_id"], item["display_menu_count"])
            for item in changes
        ] == [
            (q_map["has_soup"].id, "any", 5),
            (q_map["cooking_style"].id, "any", 5),
            (q_map["main_ingredient"].id, "any", 2),
        ]

        # 읽기 전용 검증: DB 상태가 전혀 바뀌지 않았어야 한다
        db.expire_all()
        assert db.query(Recommendation).count() == rec_count_before
        issuance_after = (
            db.query(OrderSheetIssuance)
            .filter(OrderSheetIssuance.order_sheet_id == order_sheet_id)
            .one()
        )
        assert issuance_after.status == "ISSUED"
        assert issuance_after.recommendation_id is None
        assert issuance_after.completed_at is None
        shown_counts_after = {
            q.id: int(q.shown_count or 0) for q in db.query(OrderSheetQuestion).all()
        }
        assert shown_counts_after == shown_counts_before
        state_after = db.get(QuestionSelectionState, f"user:{user.id}")
        state_snapshot_after = (
            (list(state_after.last_clusters or []), dict(state_after.topic_counts or {}))
            if state_after is not None
            else None
        )
        assert state_snapshot_after == state_snapshot_before

        # 제안 중 하나(main_ingredient -> any, 2종)를 적용해 기존 제출 API로 재제출
        chosen = changes[2]
        resubmit_payload = {
            **payload,
            "answers": [
                {
                    "question_id": ans["question_id"],
                    "option_id": (
                        chosen["replacement_option_id"]
                        if ans["question_id"] == chosen["question_id"]
                        else ans["option_id"]
                    ),
                }
                for ans in payload["answers"]
            ],
        }
        resubmit_res = client.post("/api/recommendations/order-sheet", json=resubmit_payload)
        assert resubmit_res.status_code == 201
        resubmit_data = resubmit_res.json()["data"]
        assert len(resubmit_data["recommended_menus"]) == chosen["display_menu_count"] == 2
        assert resubmit_data["candidate_count"] == 2
        assert {m["base_name"] for m in resubmit_data["recommended_menus"]} == {"해물국밥", "굴국밥"}

        # 이제 제출되었으므로 주문서 발급 상태는 COMPLETED
        db.expire_all()
        issuance_completed = (
            db.query(OrderSheetIssuance)
            .filter(OrderSheetIssuance.order_sheet_id == order_sheet_id)
            .one()
        )
        assert issuance_completed.status == "COMPLETED"
        assert issuance_completed.recommendation_id == resubmit_data["recommendation_id"]


def test_http_submit_zero_then_no_results_then_resubmit_flow(db: Session):
    """실제 사용자 흐름: 0건 제출(201) -> /no-results 조회(200, 읽기 전용) -> 제안 적용 재제출(201, 예상 수 일치)."""
    user = User(kakao_id=840002, nickname="재제출흐름테스터")
    db.add(user)
    q_map = _seed_questions(db)

    # cooking_style -> any 일 때만 3종 등장
    for idx, kind in enumerate(("소고기쌀국수", "우육면", "소고기우동")):
        _add_menu(
            db,
            food_code=f"F{idx:02d}",
            name=kind,
            base_name=kind,
            has_soup=True,
            cooking_style="noodle",
            main_ingredient="beef",
        )
    db.commit()

    payload = {
        "answers": [
            {"question_id": q_map["has_soup"].id, "option_id": "soup_yes"},
            {"question_id": q_map["cooking_style"].id, "option_id": "rice"},
            {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
        ],
        "top_k": 5,
    }

    with _make_client(db, user) as client:
        first = client.post("/api/recommendations/order-sheet", json=payload)
        assert first.status_code == 201
        assert first.json()["data"]["candidate_count"] == 0
        assert first.json()["data"]["recommended_menus"] == []
        rec_count_after_first = db.query(Recommendation).count()

        no_res = client.post("/api/recommendations/order-sheet/no-results", json=payload)
        assert no_res.status_code == 200
        changes = no_res.json()["data"]["suggested_answer_changes"]
        assert len(changes) == 1
        assert changes[0]["question_id"] == q_map["cooking_style"].id
        assert changes[0]["replacement_option_id"] == "any"
        assert changes[0]["display_menu_count"] == 3
        assert db.query(Recommendation).count() == rec_count_after_first

        adjusted_payload = {
            **payload,
            "answers": [
                {
                    "question_id": ans["question_id"],
                    "option_id": (
                        changes[0]["replacement_option_id"]
                        if ans["question_id"] == changes[0]["question_id"]
                        else ans["option_id"]
                    ),
                }
                for ans in payload["answers"]
            ],
        }
        second = client.post("/api/recommendations/order-sheet", json=adjusted_payload)
        assert second.status_code == 201
        second_data = second.json()["data"]
        assert len(second_data["recommended_menus"]) == 3
        assert second_data["candidate_count"] == 3


def test_http_no_results_empty_cases_and_validation_and_auth(db: Session):
    user = User(kakao_id=840003, nickname="빈제안검증테스터")
    db.add(user)
    q_map = _seed_questions(db)
    _add_menu(
        db,
        food_code="E01",
        name="해물볶음면",
        base_name="해물볶음면",
        has_soup=False,
        cooking_style="noodle",
        main_ingredient="seafood",
    )
    db.commit()

    with _make_client(db, user) as client:
        # 1) 단일 하드필터 변경으로는 후보가 생기지 않는 경우 -> 빈 목록 + 내 답변 다시 보기
        no_single = client.post(
            "/api/recommendations/order-sheet/no-results",
            json={
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_yes"},
                    {"question_id": q_map["cooking_style"].id, "option_id": "rice"},
                    {"question_id": q_map["main_ingredient"].id, "option_id": "meat"},
                ],
                "top_k": 5,
            },
        )
        assert no_single.status_code == 200
        assert no_single.json()["data"] == {
            "suggested_answer_changes": [],
            "can_review_my_answers": True,
        }

        # 2) 제외 조건만으로 0개인 경우 -> 빈 목록
        all_excluded = client.post(
            "/api/recommendations/order-sheet/no-results",
            json={
                "exclusions": ["해물볶음면"],
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_yes"},
                ],
                "top_k": 5,
            },
        )
        assert all_excluded.status_code == 200
        assert all_excluded.json()["data"] == {
            "suggested_answer_changes": [],
            "can_review_my_answers": True,
        }

        # 3) 원래 답변으로 이미 후보가 1종 이상 있는 경우 -> 빈 목록
        already_matched = client.post(
            "/api/recommendations/order-sheet/no-results",
            json={
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_no"},
                    {"question_id": q_map["cooking_style"].id, "option_id": "noodle"},
                    {"question_id": q_map["main_ingredient"].id, "option_id": "non_meat"},
                ],
                "top_k": 5,
            },
        )
        assert already_matched.status_code == 200
        assert already_matched.json()["data"] == {
            "suggested_answer_changes": [],
            "can_review_my_answers": True,
        }

        # 4) 잘못된 선택지 ID -> 400
        invalid_opt = client.post(
            "/api/recommendations/order-sheet/no-results",
            json={
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "nonexistent_opt"},
                ],
                "top_k": 5,
            },
        )
        assert invalid_opt.status_code == 400
        assert invalid_opt.json()["success"] is False

    # 5) 미인증 요청 -> 401
    with _make_client(db, user=None) as anon_client:
        unauth = anon_client.post(
            "/api/recommendations/order-sheet/no-results",
            json={
                "answers": [
                    {"question_id": q_map["has_soup"].id, "option_id": "soup_yes"},
                ],
                "top_k": 5,
            },
        )
        assert unauth.status_code == 401

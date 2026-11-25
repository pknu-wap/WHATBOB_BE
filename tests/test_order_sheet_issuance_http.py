"""Spec #77 / Ticket #78: 개인 주문서 9문항 발급 HTTP API 및 계정 병합 통합 테스트."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import get_current_user
from app.auth.sessions import create_uuid_user, utc_now
from app.auth.transitions import _merge_uuid_user_into_kakao_target
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import handle_recommendation_domain_exception
from app.menus.models import (
    Menu,
    OrderSheetIssuance,
    OrderSheetQuestion,
    UserFoodExclusion,
)
from app.recommend.common import RecommendationDomainException
from app.recommend.router import router
from app.users.models import User
from tests.test_order_sheet_issuance import _seed_diverse_menus_and_full_pool


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


def test_http_default_nine_questions_q8_four_options_and_server_count_setting(db, monkeypatch):
    _seed_diverse_menus_and_full_pool(db)
    user = User(kakao_id=881001, nickname="http기본유저")
    db.add(user)
    db.commit()

    with _make_client(db, user) as client:
        res = client.get("/api/recommendations/order-sheet/questions")
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-store"
        body = res.json()
        assert body["success"] is True
        data = body["data"]
        assert isinstance(data["order_sheet_id"], str) and len(data["order_sheet_id"]) > 0
        assert data["total_count"] == 9
        assert len(data["questions"]) == 9

        q8 = next(q for q in data["questions"] if q["cluster_key"] == "cooking_style")
        assert [opt["option_id"] for opt in q8["options"]] == ["rice", "noodle", "other_style", "any"]
        assert [opt["label"] for opt in q8["options"]] == ["밥", "면", "그 외", "상관없어요"]
        for q in data["questions"]:
            assert "info_gain_score" in q and "ml_weight" in q
            assert "initial_score" not in q and "importance_weight" not in q

        # Q8의 4개 선택지 모두 기존 주문서 제출 API로 정상 제출된다.
        for opt_id in ("rice", "noodle", "other_style", "any"):
            submit = client.post(
                "/api/recommendations/order-sheet",
                json={"answers": [{"question_id": q8["id"], "option_id": opt_id}], "top_k": 5},
            )
            assert submit.status_code == 201

        # 서버 설정 한 곳에서 기본 문항 수를 변경하면 즉시 반영된다.
        monkeypatch.setattr(settings, "ORDER_SHEET_QUESTION_COUNT", 6)
        six_res = client.get("/api/recommendations/order-sheet/questions")
        assert six_res.status_code == 200
        six_data = six_res.json()["data"]
        assert six_data["total_count"] == 6
        assert len(six_data["questions"]) == 6
        assert len({q["cluster_key"] for q in six_data["questions"]}) == 6


def test_http_issuance_idempotency_replay_vs_refresh_reentry(db):
    _seed_diverse_menus_and_full_pool(db)
    user = User(kakao_id=881002, nickname="발급재전송유저")
    db.add(user)
    db.commit()

    with _make_client(db, user) as client:
        first = client.get(
            "/api/recommendations/order-sheet/questions",
            headers={"Idempotency-Key": "enter-screen-001"},
        )
        assert first.status_code == 200
        first_data = first.json()["data"]

        # 같은 발급 요청 재전송(헤더 또는 쿼리 파라미터)은 중간에 알레르기가 바뀌어도 최초 발급을 그대로 반환한다.
        db.add(UserFoodExclusion(user_id=user.id, exclusion_type="ALLERGY", keyword="밀", catalog_code="wheat"))
        db.commit()

        replay_header = client.get(
            "/api/recommendations/order-sheet/questions",
            headers={"Idempotency-Key": "enter-screen-001"},
        )
        assert replay_header.status_code == 200
        assert replay_header.json()["data"] == first_data

        replay_query = client.get(
            "/api/recommendations/order-sheet/questions",
            params={"issuance_key": "enter-screen-001"},
        )
        assert replay_query.status_code == 200
        assert replay_query.json()["data"] == first_data

        # 새로고침·재입장(키 없음 또는 새 키)은 새 주문서를 처음부터 발급하고 최신 알레르기를 반영한다.
        refreshed = client.get("/api/recommendations/order-sheet/questions")
        assert refreshed.status_code == 200
        refreshed_data = refreshed.json()["data"]
        assert refreshed_data["order_sheet_id"] != first_data["order_sheet_id"]
        assert "cooking_style" not in {q["cluster_key"] for q in refreshed_data["questions"]}
        assert len(refreshed_data["questions"]) == 9

        reentered = client.get(
            "/api/recommendations/order-sheet/questions",
            headers={"Idempotency-Key": "enter-screen-002"},
        )
        assert reentered.status_code == 200
        reentered_data = reentered.json()["data"]
        assert reentered_data["order_sheet_id"] not in {
            first_data["order_sheet_id"],
            refreshed_data["order_sheet_id"],
        }

        # 헤더와 쿼리 키가 서로 다르면 400
        mismatch = client.get(
            "/api/recommendations/order-sheet/questions",
            headers={"Idempotency-Key": "key-a"},
            params={"issuance_key": "key-b"},
        )
        assert mismatch.status_code == 400

    issuances = db.query(OrderSheetIssuance).filter(OrderSheetIssuance.user_id == user.id).all()
    assert len(issuances) == 3
    assert all(row.status == "ISSUED" for row in issuances)


def test_http_multiple_allergies_and_semantic_duplicates_fill_nine_distinct_topics(db):
    _seed_diverse_menus_and_full_pool(db)
    db.add_all([
        OrderSheetQuestion(
            question_code="dup_opposite_heavy",
            cluster_key="filling_extra",
            question_text="든든한 식사를 먹고 싶나요?",
            options_json=[
                {"option_id": "heavy", "label": "예", "rules": [{"field": "heaviness", "op": "eq", "value": "heavy"}]},
                {"option_id": "not_heavy", "label": "아니요", "rules": [{"field": "heaviness", "op": "neq", "value": "heavy"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
            initial_score=0.99,
            importance_weight=3.0,
            is_active=True,
        ),
        OrderSheetQuestion(
            question_code="dup_subtopic_pork",
            cluster_key="pork_extra",
            question_text="돼지고기 요리를 먹고 싶나요?",
            options_json=[
                {"option_id": "pork", "label": "예", "rules": [{"field": "main_ingredient", "op": "eq", "value": "pork"}]},
                {"option_id": "not_pork", "label": "아니요", "rules": [{"field": "main_ingredient", "op": "neq", "value": "pork"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
            initial_score=0.99,
            importance_weight=2.9,
            is_active=True,
        ),
    ])
    user = User(kakao_id=881003, nickname="복수알레르기HTTP유저")
    db.add(user)
    db.flush()
    db.add_all([
        UserFoodExclusion(user_id=user.id, exclusion_type="ALLERGY", keyword="밀", catalog_code="wheat"),
        UserFoodExclusion(user_id=user.id, exclusion_type="ALLERGY", keyword="돼지고기", catalog_code="pork"),
        UserFoodExclusion(user_id=user.id, exclusion_type="ALLERGY", keyword="쇠고기(소고기)", catalog_code="beef"),
        UserFoodExclusion(user_id=user.id, exclusion_type="UNABLE", keyword="닭·가금류", catalog_code="poultry"),
    ])
    db.commit()

    with _make_client(db, user) as client:
        res = client.get("/api/recommendations/order-sheet/questions")
        assert res.status_code == 200
        questions = res.json()["data"]["questions"]
        clusters = [q["cluster_key"] for q in questions]
        codes = {q["question_code"] for q in questions}
        assert len(clusters) == 9
        assert len(set(clusters)) == 9
        assert "cooking_style" not in clusters
        assert "main_ingredient" not in clusters
        assert "dup_opposite_heavy" not in codes
        assert "dup_subtopic_pork" not in codes
        assert {"sugar_level", "temperature"}.issubset(set(clusters))


def test_uuid_to_kakao_transition_merges_order_sheet_issuances_without_conflict(db):
    _seed_diverse_menus_and_full_pool(db)
    origin_user = create_uuid_user(db)
    target_user = User(kakao_id=881099, nickname="카카오타겟유저")
    db.add(target_user)
    db.commit()

    with _make_client(db, origin_user) as origin_client:
        r1 = origin_client.get(
            "/api/recommendations/order-sheet/questions",
            headers={"Idempotency-Key": "shared-key"},
        )
        r2 = origin_client.get(
            "/api/recommendations/order-sheet/questions",
            headers={"Idempotency-Key": "origin-only-key"},
        )
        assert r1.status_code == 200 and r2.status_code == 200

    with _make_client(db, target_user) as target_client:
        r3 = target_client.get(
            "/api/recommendations/order-sheet/questions",
            headers={"Idempotency-Key": "shared-key"},
        )
        assert r3.status_code == 200

    _merge_uuid_user_into_kakao_target(
        db,
        origin_user,
        target_user,
        kakao_id=target_user.kakao_id,
        kakao_email=None,
        now=utc_now(),
    )
    db.delete(origin_user)
    db.commit()

    merged_rows = (
        db.query(OrderSheetIssuance)
        .filter(OrderSheetIssuance.user_id == target_user.id)
        .all()
    )
    assert len(merged_rows) == 3
    assert {row.issuance_key for row in merged_rows if row.issuance_key} == {"shared-key", "origin-only-key"}

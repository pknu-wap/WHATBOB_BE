import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import handle_recommendation_domain_exception
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
)
from app.recommend.common import RecommendationDomainException
from app.recommend.feedback import (
    OUTCOME_FIRST_LIST_LIKED,
    OUTCOME_LIST_REJECTED,
    OUTCOME_ZERO_CANDIDATES,
    classify_recommendation_outcome,
    summarize_first_list_reactions,
)
from app.recommend.experiment_metrics import ArmMetrics
from app.recommend.router import router as recommend_router
from app.users.models import User
from tests.auth_helpers import session_token


@pytest.fixture
def http_env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()

    user = User(kakao_id=790001, nickname="HTTP주인")
    other = User(kakao_id=790002, nickname="HTTP타인")
    db.add_all([user, other])

    menus = [
        Menu(
            food_code=f"H00{i}",
            name=name,
            base_name=name,
            category_large="한식",
            has_soup=True,
            cooking_style="soup_stew",
            main_ingredient="pork",
            flavor_intensity="mild_clean",
            temperature="hot",
            heaviness="moderate",
            allergen_tags=[],
        )
        for i, name in enumerate(["김치찌개", "된장찌개", "순두부찌개", "우동"], start=1)
    ]
    db.add_all(menus)

    question = OrderSheetQuestion(
        question_code="q_http_soup_01",
        cluster_key="has_soup",
        question_text="국물 있는 음식이 당기나요?",
        options_json=[
            {"option_id": "soup_yes", "label": "국물", "rules": [{"field": "has_soup", "op": "eq", "value": True}]},
            {"option_id": "soup_no", "label": "국물 없음", "rules": [{"field": "has_soup", "op": "neq", "value": True}]},
            {"option_id": "any", "label": "상관없어요", "rules": []},
        ],
        initial_score=0.8,
        importance_weight=0.8,
    )
    db.add(question)
    db.commit()

    user_headers = {"Authorization": f"Bearer {session_token(db, user)}"}
    other_headers = {"Authorization": f"Bearer {session_token(db, other)}"}

    app = FastAPI()
    app.add_exception_handler(RecommendationDomainException, handle_recommendation_domain_exception)
    app.include_router(recommend_router)
    app.dependency_overrides[get_db] = lambda: db

    with TestClient(app) as client:
        yield {
            "client": client,
            "db": db,
            "user": user,
            "other": other,
            "user_headers": user_headers,
            "other_headers": other_headers,
            "question": question,
            "menus": menus,
        }

    db.close()
    Base.metadata.drop_all(bind=engine)


def test_http_feedback_preserves_contract_records_exposure_rank_and_deduplicates_retries(http_env):
    client = http_env["client"]
    db = http_env["db"]
    headers = http_env["user_headers"]
    other_headers = http_env["other_headers"]
    question = http_env["question"]

    rec_res = client.post(
        "/api/recommendations/order-sheet",
        headers=headers,
        json={"answers": [{"question_id": question.id, "option_id": "soup_yes"}], "top_k": 3},
    )
    assert rec_res.status_code == 201
    rec_data = rec_res.json()["data"]
    rec_id = rec_data["recommendation_id"]
    shown_ids = [m["menu_id"] for m in rec_data["recommended_menus"]]
    assert len(shown_ids) == 3

    # 비인증 401 / 타인 404
    assert client.post(
        f"/api/recommendations/{rec_id}/feedback",
        json={"selected_menu_id": shown_ids[0], "is_satisfied": True},
    ).status_code == 401
    assert client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers=other_headers,
        json={"selected_menu_id": shown_ids[0], "is_satisfied": True},
    ).status_code == 404

    # 1위 메뉴 좋아요 + 동일 클릭 재전송(Idempotency-Key 동일 & 키 없음)
    fb1 = client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers={**headers, "Idempotency-Key": "http-fb-1"},
        json={"selected_menu_id": shown_ids[0], "is_satisfied": True},
    )
    assert fb1.status_code == 200
    body1 = fb1.json()
    assert body1["success"] is True
    assert body1["data"]["recommendation_id"] == rec_id
    assert body1["data"]["is_satisfied"] is True
    assert body1["data"]["updated_weights"] == {}
    assert body1["data"]["exposure_rank"] == 1

    fb1_replay = client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers={**headers, "Idempotency-Key": "http-fb-1"},
        json={"selected_menu_id": shown_ids[0], "is_satisfied": True},
    )
    fb1_no_key_retry = client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers=headers,
        json={"selected_menu_id": shown_ids[0], "is_satisfied": True},
    )
    assert fb1_replay.status_code == 200
    assert fb1_no_key_retry.status_code == 200
    assert fb1_replay.json()["data"]["feedback_id"] == body1["data"]["feedback_id"]
    assert fb1_no_key_retry.json()["data"]["feedback_id"] == body1["data"]["feedback_id"]

    # 3위 메뉴도 좋아요 -> 메뉴 반응 2건, 첫 목록 성공 1건
    fb3 = client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers=headers,
        json={"selected_menu_id": shown_ids[2], "is_satisfied": True},
    )
    assert fb3.status_code == 200
    assert fb3.json()["data"]["exposure_rank"] == 3
    assert fb3.json()["data"]["updated_weights"] == {}

    db.refresh(question)
    assert question.positive_count == 0
    assert float(question.importance_weight) == pytest.approx(0.8)

    assert db.query(RecommendationFeedback).filter_by(recommendation_id=rec_id).count() == 2
    assert db.query(RecommendationFeedbackState).filter_by(recommendation_id=rec_id).count() == 2

    outcome = classify_recommendation_outcome(db, rec_id)
    assert outcome.outcome == OUTCOME_FIRST_LIST_LIKED
    assert outcome.liked_menu_count == 2
    assert outcome.first_liked_exposure_rank == 1


def dislike_all_recommended_menus(client, recommendation, *, expect_closed=True):
    menus = recommendation["recommended_menus"]
    assert menus
    for index, menu in enumerate(menus, start=1):
        response = client.post(
            f"/api/recommendations/{recommendation['recommendation_id']}/feedback",
            json={"selected_menu_id": menu["menu_id"], "is_satisfied": False},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["is_closed"] is (expect_closed and index == len(menus))


def test_disliking_each_displayed_menu_closes_sheet_and_blocks_changes(http_env):
    client = http_env["client"]
    db = http_env["db"]
    headers = http_env["user_headers"]
    question = http_env["question"]
    created = client.post(
        "/api/recommendations/order-sheet",
        headers=headers,
        json={"answers": [{"question_id": question.id, "option_id": "soup_yes"}], "top_k": 2},
    )
    assert created.status_code == 201
    recommendation = created.json()["data"]
    menu_a, menu_b = [menu["menu_id"] for menu in recommendation["recommended_menus"]]
    rec_id = recommendation["recommendation_id"]
    url = f"/api/recommendations/{rec_id}/feedback"

    for menu_id, satisfied in ((menu_a, False), (menu_a, False), (menu_a, True), (menu_b, False)):
        response = client.post(url, headers=headers, json={"selected_menu_id": menu_id, "is_satisfied": satisfied})
        assert response.status_code == 200
        assert response.json()["data"]["is_closed"] is False
    assert not classify_recommendation_outcome(db, rec_id).is_all_menus_disliked

    payload = {"selected_menu_id": menu_a, "is_satisfied": False}
    final_headers = {**headers, "Idempotency-Key": "last-menu-dislike"}
    final_response = client.post(url, headers=final_headers, json=payload)
    assert final_response.status_code == 200
    assert final_response.json()["data"]["is_closed"] is True
    assert client.post(url, headers=final_headers, json=payload).json() == final_response.json()

    for menu_id in (menu_a, menu_b):
        changed = client.post(url, headers=headers, json={"selected_menu_id": menu_id, "is_satisfied": True})
        assert changed.status_code == 409
        assert "ORDER_SHEET_CLOSED" in changed.text

    outcome = classify_recommendation_outcome(db, rec_id)
    assert outcome.is_closed and outcome.is_all_menus_disliked
    assert outcome.disliked_menu_count == 2
    assert db.query(RecommendationFeedback).filter_by(recommendation_id=rec_id).count() == 4
    assert ArmMetrics.from_summary(summarize_first_list_reactions(db, recommendation_ids=[rec_id])).rejected_count == 1


def test_http_reject_endpoint_auth_idempotency_closes_sheet_and_rejects_zero_candidates(http_env):
    client = http_env["client"]
    db = http_env["db"]
    user = http_env["user"]
    headers = http_env["user_headers"]
    other_headers = http_env["other_headers"]
    question = http_env["question"]

    rec_res = client.post(
        "/api/recommendations/order-sheet",
        headers=headers,
        json={"answers": [{"question_id": question.id, "option_id": "soup_yes"}], "top_k": 3},
    )
    assert rec_res.status_code == 201
    rec_id = rec_res.json()["data"]["recommendation_id"]
    first_menu_id = rec_res.json()["data"]["recommended_menus"][0]["menu_id"]

    # 비인증 401 / 타인 404
    assert client.post(f"/api/recommendations/{rec_id}/reject").status_code == 401
    assert client.post(f"/api/recommendations/{rec_id}/reject", headers=other_headers).status_code == 404

    # 명시적 목록 거절 200 + 멱등 재전송
    rej1 = client.post(
        f"/api/recommendations/{rec_id}/reject",
        headers={**headers, "Idempotency-Key": "http-reject-1"},
    )
    assert rej1.status_code == 200
    rej_body = rej1.json()
    assert rej_body["success"] is True
    assert rej_body["data"]["recommendation_id"] == rec_id
    assert rej_body["data"]["is_list_rejected"] is True
    assert rej_body["data"]["is_closed"] is True
    assert rej_body["data"]["outcome"] == OUTCOME_LIST_REJECTED

    rej2 = client.post(
        f"/api/recommendations/{rec_id}/reject",
        headers={**headers, "Idempotency-Key": "http-reject-1"},
    )
    rej3 = client.post(
        f"/api/recommendations/{rec_id}/reject",
        headers=headers,
    )
    assert rej2.status_code == 200 and rej3.status_code == 200
    assert rej2.json()["data"]["list_rejected_at"] == rej_body["data"]["list_rejected_at"]
    assert rej3.json()["data"]["list_rejected_at"] == rej_body["data"]["list_rejected_at"]

    # 종료된 주문서에 새 피드백 시도 -> 409
    fb_after_close = client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers=headers,
        json={"selected_menu_id": first_menu_id, "is_satisfied": True},
    )
    assert fb_after_close.status_code == 409
    assert fb_after_close.json()["success"] is False

    # 0건 후보 추천에 대한 목록 거절 -> 409, zero_candidates 유지
    zero_rec = Recommendation(
        user_id=user.id,
        recommended_menu_ids=[],
        initial_candidate_count=0,
    )
    db.add(zero_rec)
    db.commit()

    zero_rej = client.post(f"/api/recommendations/{zero_rec.id}/reject", headers=headers)
    assert zero_rej.status_code == 409
    assert zero_rej.json()["success"] is False

    summary = summarize_first_list_reactions(db=db, user_id=user.id)
    assert summary.total_recommendations == 2
    assert summary.shown_list_count == 1
    assert summary.list_rejected_count == 1
    assert summary.zero_candidates_count == 1
    assert ArmMetrics.from_summary(summary).rejected_count == 0
    assert classify_recommendation_outcome(db, zero_rec.id).outcome == OUTCOME_ZERO_CANDIDATES

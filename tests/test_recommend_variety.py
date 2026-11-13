import json
import random
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from tests.auth_helpers import session_token
from app.menus.models import Menu, OrderSheetQuestion, QuestionSelectionState, Recommendation, RecommendationFeedback
from app.recommend.common import InvalidOrderSheetException, NoAvailableFoodException
from app.recommend.order_sheet import create_order_sheet_recommendation, record_recommendation_feedback
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.router import router
from app.recommend.schemas import AnswerItemRequest, OrderSheetRecommendRequest, RecommendationFeedbackRequest
from app.recommend.variety import menu_group, select_diverse_menus
from app.users.models import User
from scripts.seed_menus import parse_and_clean_csv, seed_database


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_menu(db, name, base=None, soup=False):
    menu = Menu(food_code=f"code-{name}", name=name, base_name=base or name,
                category_large="식사", meal_type="main_meal", has_soup=soup)
    db.add(menu)
    db.flush()
    return menu


def add_question(db):
    source = next(q for q in build_candidate_question_pool() if q["cluster_key"] == "has_soup")
    question = OrderSheetQuestion(question_code=source["question_code"], cluster_key="has_soup",
                                 question_text=source["question_text"], options_json=source["options"],
                                 initial_score=1, importance_weight=1)
    db.add(question)
    db.commit()
    return question


def test_meal_data_restores_burgers_pizza_and_other_meal_breads():
    menus = parse_and_clean_csv(include_beverages_and_bakery=False)
    bases = {m["base_name"] for m in menus}
    for base in ("햄버거", "버거", "피자", "샌드위치", "핫도그", "토스트", "또띠아"):
        found = [m for m in menus if m["base_name"] == base]
        assert found and all(m["meal_type"] == "main_meal" for m in found)
        assert all(m["temperature"] == "hot" for m in found)
        assert all("밀" in m["allergen_tags"] for m in found)
    assert bases.isdisjoint({"케이크", "도넛", "마카롱", "아이스크림"})
    shared = json.loads((Path(__file__).resolve().parents[1] / "data/menus_meals_only.json").read_text(encoding="utf-8"))
    assert shared == menus


def test_reseed_updates_existing_snack_classification_without_changing_id(db, tmp_path):
    # 작은 CSV로 기존 DB 갱신과 반복 실행을 검증한다.
    import csv
    source = parse_and_clean_csv()
    burger = next(m for m in source if m["base_name"] == "햄버거")
    existing = Menu(**{**burger, "meal_type": "bakery_snack", "temperature": "neutral"})
    db.add(existing)
    db.commit()
    original_id = existing.id
    csv_path = tmp_path / "menus.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["식품코드", "식품명", "식품대분류명"])
        writer.writeheader()
        writer.writerow({"식품코드": burger["food_code"], "식품명": burger["name"], "식품대분류명": "빵 및 과자류"})
    result = seed_database(db, csv_path=csv_path, include_beverages_and_bakery=False)
    assert result["inserted_menus"] == 0
    db.refresh(existing)
    assert (existing.id, existing.meal_type, existing.temperature) == (original_id, "main_meal", "hot")
    assert seed_database(db, csv_path=csv_path, include_beverages_and_bakery=False)["inserted_questions"] == 0


def test_many_pizza_products_cannot_fill_result_and_equal_candidates_rotate(db, monkeypatch):
    menus = [add_menu(db, f"피자_{i}", base="피자") for i in range(50)]
    menus += [add_menu(db, name) for name in ("햄버거", "샌드위치", "국밥", "김밥", "우동", "비빔밥")]
    db.commit()
    candidates = [(1.0, ["matched"], m) for m in menus]
    seen = set()
    for seed in range(20):
        monkeypatch.setattr("app.recommend.variety.random.SystemRandom", lambda: random.Random(seed))
        selected = select_diverse_menus(db, candidates, None, 5, total_weight=1)
        groups = [menu_group(item[2]) for item in selected]
        assert len(groups) == len(set(groups)) == 5
        assert groups.count("피자") <= 1
        seen.update(groups)
    assert seen == {menu_group(m) for m in menus}


def test_score_gap_keeps_matching_food_first_and_shortage_does_not_repeat(db):
    matching = add_menu(db, "국밥", soup=True)
    others = [add_menu(db, "피자"), add_menu(db, "햄버거")]
    candidates = [(1.0, ["soup"], matching)] + [(0.0, [], m) for m in others]
    selected = select_diverse_menus(db, candidates, None, 5, total_weight=1)
    assert selected[0][2].id == matching.id
    assert len(selected) == 3


def test_recent_menu_groups_rotate_for_one_user_only(db):
    user = User(kakao_id=100)
    other = User(kakao_id=101)
    db.add_all([user, other])
    db.flush()
    menus = [add_menu(db, name) for name in ("피자", "햄버거", "김밥", "우동", "국밥", "비빔밥")]
    db.add(Recommendation(user_id=user.id, recommended_menu_ids=[m.id for m in menus[:5]]))
    db.commit()
    candidates = [(1.0, [], m) for m in menus]
    # 노출 패널티 .1 + 동점 허용 .1 경계에 기대지 않도록 두 번의 이력을 준다.
    db.add(Recommendation(user_id=user.id, recommended_menu_ids=[m.id for m in menus[:5]]))
    db.commit()
    assert select_diverse_menus(db, candidates, user.id, 1, 1)[0][2].id == menus[-1].id
    assert not db.query(Recommendation).filter(Recommendation.user_id == other.id).count()


def test_dislike_lowers_only_that_users_menu_and_later_like_restores_it(db, tmp_path, monkeypatch):
    from app.menus.models import RecommendationFeedbackState
    from app.recommend.personal_model import compute_artifact_checksum, FEATURE_NAMES

    monkeypatch.setattr("app.recommend.variety.NEAR_SCORE_RATIO", 0.002)
    artifact_file = tmp_path / "personal_active.json"
    payload = {
        "version": "test-v1",
        "feature_schema_version": 1,
        "feature_names": list(FEATURE_NAMES),
        "intercept": 0.0,
        "weights": [0.0, 4.0, 0.0, 0.0, 0.0, 0.0],
        "means": [0.0 for _ in FEATURE_NAMES],
        "stds": [1.0 for _ in FEATURE_NAMES],
        "metrics": {"val_log_loss": 0.4, "val_brier_score": 0.12},
        "validation_passed": True,
    }
    payload["checksum"] = compute_artifact_checksum(payload)
    artifact_file.write_text(json.dumps(payload), encoding="utf-8")

    user = User(kakao_id=102)
    other = User(kakao_id=103)
    db.add_all([user, other])
    pizza, burger = add_menu(db, "피자"), add_menu(db, "햄버거")
    db.flush()
    rec = Recommendation(user_id=user.id, recommended_menu_ids=[pizza.id, burger.id])
    db.add(rec)
    db.flush()
    state = RecommendationFeedbackState(
        recommendation_id=rec.id,
        user_id=user.id,
        selected_menu_id=pizza.id,
        menu_key="피자",
        menu_base_name="피자",
        menu_main_ingredient="other",
        is_satisfied=False,
    )
    db.add(state)
    db.commit()
    candidates = [(1.0, [], pizza), (0.995, [], burger)]
    assert select_diverse_menus(db, candidates, user.id, 1, 1, artifact_path=artifact_file)[0][2].id == burger.id
    assert select_diverse_menus(db, candidates, other.id, 1, 1, artifact_path=artifact_file)[0][2].id == pizza.id
    state.is_satisfied = True
    db.commit()
    assert select_diverse_menus(db, candidates, user.id, 1, 1, artifact_path=artifact_file)[0][2].id == pizza.id


def test_any_is_neutral_and_not_used_for_question_learning_including_legacy_binary(db):
    menus = [add_menu(db, "피자"), add_menu(db, "국밥", soup=True)]
    question = add_question(db)
    question.options_json = question.options_json[:2]  # 재시드 이전의 두 버튼 DB
    db.commit()
    result = create_order_sheet_recommendation(db, OrderSheetRecommendRequest(
        answers=[AnswerItemRequest(question_id=question.id, option_id="any")],
    ))
    assert {m.menu_id for m in result.recommended_menus} == {m.id for m in menus}
    assert all(m.match_score == 0 and not m.matched_question_codes for m in result.recommended_menus)
    record_recommendation_feedback(db, result.recommendation_id,
                                   RecommendationFeedbackRequest(is_satisfied=False))
    assert question.importance_weight == 1 and question.positive_count == 0
    assert db.query(RecommendationFeedback).one().answer_match_results == {}


def test_exclusions_stay_hard_with_any_and_variety(db):
    add_menu(db, "피자")
    add_menu(db, "햄버거")
    question = add_question(db)
    request = OrderSheetRecommendRequest(exclusions=["피자"], answers=[
        AnswerItemRequest(question_id=question.id, option_id="any"),
    ])
    result = create_order_sheet_recommendation(db, request)
    assert [m.name for m in result.recommended_menus] == ["햄버거"]
    with pytest.raises(NoAvailableFoodException):
        create_order_sheet_recommendation(db, request.model_copy(update={"exclusions": ["피자", "햄버거"]}))


def test_feedback_rejects_unrecommended_menu_and_mismatched_user(db):
    from app.recommend.common import RecommendationNotFoundException
    user = User(kakao_id=108)
    db.add(user)
    db.flush()
    pizza, burger = add_menu(db, "피자"), add_menu(db, "햄버거")
    rec = Recommendation(user_id=user.id, recommended_menu_ids=[pizza.id])
    db.add(rec)
    db.commit()
    with pytest.raises(InvalidOrderSheetException):
        record_recommendation_feedback(db, rec.id, RecommendationFeedbackRequest(
            selected_menu_id=burger.id, is_satisfied=False,
        ), user_id=user.id)
    with pytest.raises(RecommendationNotFoundException):
        record_recommendation_feedback(db, rec.id, RecommendationFeedbackRequest(
            selected_menu_id=pizza.id, is_satisfied=False,
        ), user_id=999)
    assert db.query(RecommendationFeedback).count() == 0


def test_authenticated_questions_and_recommendations_use_token_identity(db):
    user, other = User(kakao_id=104), User(kakao_id=105)
    db.add_all([user, other])
    add_menu(db, "피자")
    add_menu(db, "햄버거")
    question = add_question(db)
    token = session_token(db, user)
    headers = {"Authorization": f"Bearer {token}"}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        response = client.get("/api/recommendations/order-sheet/questions", headers=headers)
        assert response.status_code == 200
        assert len(response.json()["data"]["questions"][0]["options"]) == 3
        assert db.get(QuestionSelectionState, f"user:{user.id}") is not None
        assert not client.cookies.get("question_session")
        body = {"answers": [{"question_id": question.id, "option_id": "any"}]}
        response = client.post("/api/recommendations/order-sheet", json=body, headers=headers)
        assert response.status_code == 201
        record = db.get(Recommendation, response.json()["data"]["recommendation_id"])
        assert record.user_id == user.id
        response = client.post(f"/api/recommendations/{record.id}/feedback", json={"is_satisfied": False}, headers=headers)
        assert response.status_code == 200
        assert db.query(RecommendationFeedback).one().user_id == user.id
        response = client.post("/api/recommendations/order-sheet", json={**body, "user_id": other.id}, headers=headers)
        assert response.status_code == 422
        response = client.get("/api/recommendations/order-sheet/questions", headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401

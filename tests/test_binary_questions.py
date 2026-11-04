import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.menus.models import Menu, OrderSheetQuestion, Recommendation
from app.recommend.common import evaluate_option_rules
from app.recommend.order_sheet import record_recommendation_feedback
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.router import router
from app.recommend.schemas import OrderSheetQuestionDTO, RecommendationFeedbackRequest
from scripts import seed_menus


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        menus = seed_menus.parse_and_clean_csv(
            Path(seed_menus.DEFAULT_CSV_PATH), max_menus=30,
            include_beverages_and_bakery=False,
        )
        session.add_all(Menu(**menu) for menu in menus)
        session.commit()
        yield session
    engine.dispose()


def test_binary_options_cover_existing_menu_categories():
    """다섯 선택지를 단순 삭제하지 않고 모든 기존 분류를 두 그룹에 포함한다."""
    pool = build_candidate_question_pool()
    assert len(pool) == 204
    assert len({q["question_code"] for q in pool}) == 204
    assert len({q["cluster_key"] for q in pool}) == 12
    menus = seed_menus.parse_and_clean_csv(
        Path(seed_menus.DEFAULT_CSV_PATH), include_beverages_and_bakery=True,
    )
    for question in pool:
        assert 1 <= len(question["question_text"]) <= 24
        assert len(question["options"]) == 2
        assert len({o["option_id"] for o in question["options"]}) == 2
    # 같은 주제의 문구 변형은 동일한 두 선택지를 사용한다.
    for question in {q["cluster_key"]: q for q in pool}.values():
        options = question["options"]
        field = options[0]["rules"][0]["field"]
        for menu_data in menus:
            if menu_data[field] is not None:
                menu = Menu(**menu_data)
                assert sum(evaluate_option_rules(menu, o["rules"]) for o in options) == 1


@pytest.mark.parametrize("cluster,field,boundary", [
    ("protein_level", "protein_g", 10.0),
    ("carb_level", "carb_g", 15.0),
    ("sodium_level", "sodium_mg", 250.0),
    ("sugar_level", "sugar_g", 3.0),
])
def test_numeric_options_do_not_overlap_at_boundary(cluster, field, boundary):
    question = next(q for q in build_candidate_question_pool() if q["cluster_key"] == cluster)
    for value in (boundary - 0.01, boundary, boundary + 0.01):
        menu = Menu(**{field: value})
        assert sum(evaluate_option_rules(menu, o["rules"]) for o in question["options"]) == 1


def test_response_contract_enforces_24_characters_and_two_options():
    question = build_candidate_question_pool()[0]
    payload = dict(question, id=1, initial_score=0.5, importance_weight=0.5)
    payload["question_text"] = "가" * 24
    assert len(OrderSheetQuestionDTO(**payload).question_text) == 24
    with pytest.raises(ValidationError):
        OrderSheetQuestionDTO(**dict(payload, question_text="가" * 25))
    for count in (1, 3):
        with pytest.raises(ValidationError):
            OrderSheetQuestionDTO(**dict(payload, options=[question["options"][0]] * count))


def test_shared_question_json_matches_binary_pool():
    path = Path(__file__).resolve().parents[1] / "data" / "order_sheet_questions_pool.json"
    shared = json.loads(path.read_text(encoding="utf-8"))
    expected = {q["question_code"]: q for q in build_candidate_question_pool()}
    assert len(shared) == len(expected)
    assert {q["question_code"] for q in shared} == set(expected)
    for question in shared:
        source = expected[question["question_code"]]
        assert question["question_text"] == source["question_text"]
        assert question["options"] == source["options"]
        assert "info_gain_score" in question and "ml_weight" in question


def test_llm_discards_overlong_questions_without_truncating(monkeypatch):
    items = [
        {"cluster_key": "has_soup", "question_text": text}
        for text in ("가" * 24, "나" * 25, "   ", 123)
    ]
    def respond(request):
        prompt = json.loads(request.content)["messages"][0]["content"]
        assert "24자" in prompt
        assert "soup_yes" in prompt and "soup_no" in prompt
        return httpx.Response(200, json={"choices": [{
            "message": {"content": json.dumps(items, ensure_ascii=False)},
        }]})

    client_class = httpx.Client
    monkeypatch.setattr(seed_menus.httpx, "Client", lambda **kwargs: client_class(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    monkeypatch.setattr(seed_menus.settings, "LLM_API_KEY", "test-key")
    questions = seed_menus.fetch_llm_generated_questions(build_candidate_question_pool())
    assert len(questions) == 1
    assert questions[0]["question_text"] == "가" * 24
    assert len(questions[0]["options"]) == 2


def test_seed_preserves_old_feedback_and_serves_binary_questions(db):
    """이전 질문의 조건은 보존하고 새 질문만 조회·추천에 사용한다."""
    menu = db.query(Menu).first()
    old_options = [{"option_id": "legacy_soup", "label": "예전 선택지", "rules": [
        {"field": "has_soup", "op": "eq", "value": menu.has_soup},
    ]}, {"option_id": "any", "label": "상관없어요", "rules": []}]
    old = OrderSheetQuestion(
        question_code="q_has_soup_01", cluster_key="has_soup",
        question_text="국물이 있는 메뉴를 추천해 드릴까요, 없는 메뉴를 추천해 드릴까요?",
        options_json=old_options, initial_score=0.8, importance_weight=0.8,
    )
    db.add(old)
    db.flush()
    record = Recommendation(
        shown_question_ids=[old.id],
        answers_json=[{"question_id": old.id, "option_id": "legacy_soup"}],
        recommended_menu_ids=[menu.id],
    )
    db.add(record)
    db.commit()
    before_menus = [(m.id, m.name, m.kcal, m.allergen_tags) for m in db.query(Menu).all()]
    summary = seed_menus.seed_question_pool(db)
    assert summary["inserted_questions"] == 204
    assert summary["deactivated_questions"] == 1
    assert old.is_active is False
    assert old.options_json == old_options
    assert before_menus == [(m.id, m.name, m.kcal, m.allergen_tags) for m in db.query(Menu).all()]

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        response = client.get("/api/recommendations/order-sheet/questions")
        assert response.status_code == 200
        data = response.json()["data"]
        assert (data["total_count"], data["exploitation_count"], data["exploration_count"]) == (9, 7, 2)
        for question in data["questions"]:
            assert question["id"] != old.id
            assert len(question["question_text"]) <= 24
            assert len(question["options"]) == 2
        # 두 버튼 모두 추천과 피드백까지 처리할 수 있다.
        for index in (0, 1):
            response = client.post("/api/recommendations/order-sheet", json={
                "answers": [{"question_id": q["id"], "option_id": q["options"][index]["option_id"]}
                            for q in data["questions"]],
                "top_k": 5,
            })
            assert response.status_code == 201
            result = response.json()["data"]
            assert len(result["recommended_menus"]) == 5
            feedback = client.post(f'/api/recommendations/{result["recommendation_id"]}/feedback', json={
                "selected_menu_id": result["recommended_menus"][0]["menu_id"],
                "is_satisfied": index == 0,
            })
            assert feedback.status_code == 200

    record_recommendation_feedback(db, record.id, RecommendationFeedbackRequest(
        selected_menu_id=menu.id, is_satisfied=True,
    ))
    assert old.positive_count == 1
    new_question = db.query(OrderSheetQuestion).filter(OrderSheetQuestion.is_active.is_(True)).first()
    weight = new_question.importance_weight
    repeated = seed_menus.seed_question_pool(db)
    assert repeated["inserted_questions"] == 0
    assert repeated["deactivated_questions"] == 0
    assert new_question.importance_weight == weight

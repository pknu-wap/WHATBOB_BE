import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.menus.models import OrderSheetQuestion
from app.recommend.common import QuestionPoolEmptyException
from app.recommend.order_sheet import select_order_sheet_questions
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.router import router


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    OrderSheetQuestion.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_question_topics(db, count):
    pool = build_candidate_question_pool()
    clusters = list(dict.fromkeys(q["cluster_key"] for q in pool))[:count]
    representatives = []
    for index, cluster in enumerate(clusters):
        # 상위 주제의 문구 변형들이 탐색 질문을 차지하던 상황을 재현한다.
        variants = [q for q in pool if q["cluster_key"] == cluster][:4]
        for variant_index, question in enumerate(variants):
            row = OrderSheetQuestion(
                question_code=question["question_code"], cluster_key=cluster,
                question_text=question["question_text"], options_json=question["options"],
                initial_score=0.5, importance_weight=2.0 - index * 0.1 - variant_index * 0.001,
                shown_count=variant_index * 2, is_active=True,
            )
            db.add(row)
            if variant_index == 0:
                representatives.append(row)
    db.commit()
    return representatives


def test_top_seven_and_two_exploration_questions_have_distinct_topics(db):
    representatives = add_question_topics(db, 12)
    for seed in range(20):
        result = select_order_sheet_questions(db, random_seed=seed)
        exploit = [q for q in result.questions if not q.is_exploration]
        explore = [q for q in result.questions if q.is_exploration]
        assert (result.total_count, result.exploitation_count, result.exploration_count) == (9, 7, 2)
        assert [q.id for q in exploit] == [q.id for q in representatives[:7]]
        assert len({q.id for q in result.questions}) == 9
        assert len({q.cluster_key for q in result.questions}) == 9
        assert {q.cluster_key for q in exploit}.isdisjoint(q.cluster_key for q in explore)
    first = select_order_sheet_questions(db, random_seed=42)
    repeated = select_order_sheet_questions(db, random_seed=42)
    assert [q.id for q in first.questions] == [q.id for q in repeated.questions]


@pytest.mark.parametrize("topics,total,exploit,expected", [
    (1, 9, 7, (1, 1, 0)),
    (7, 9, 7, (7, 7, 0)),
    (8, 9, 7, (8, 7, 1)),
    (9, 9, 7, (9, 7, 2)),
    (12, 20, 7, (12, 7, 5)),
    (12, 4, 7, (4, 4, 0)),
    (12, 9, 9, (9, 9, 0)),
    (12, 9, 1, (9, 1, 8)),
])
def test_counts_follow_distinct_topics_without_duplicate_fill(db, topics, total, exploit, expected):
    add_question_topics(db, topics)
    result = select_order_sheet_questions(
        db, total_count=total, exploitation_count=exploit, random_seed=13,
    )
    assert (result.total_count, result.exploitation_count, result.exploration_count) == expected
    assert len(result.questions) == len({q.cluster_key for q in result.questions}) == expected[0]
    assert sum(q.is_exploration for q in result.questions) == expected[2]


def test_http_counts_ignore_inactive_topics(db):
    representatives = add_question_topics(db, 9)
    inactive_cluster = representatives[-1].cluster_key
    for question in db.query(OrderSheetQuestion).filter(OrderSheetQuestion.cluster_key == inactive_cluster):
        question.is_active = False
    # 한 주제의 최상위 질문이 비활성이면 그 주제의 다음 질문을 사용한다.
    representatives[0].is_active = False
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        response = client.get("/api/recommendations/order-sheet/questions")
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["total_count"], data["exploitation_count"], data["exploration_count"]) == (8, 7, 1)
    questions = data["questions"]
    assert len({q["cluster_key"] for q in questions}) == 8
    assert all(q["cluster_key"] != inactive_cluster for q in questions)
    assert all(q["id"] != representatives[0].id for q in questions)
    assert questions[0]["cluster_key"] == representatives[0].cluster_key


def test_empty_active_question_pool_keeps_domain_error(db):
    with pytest.raises(QuestionPoolEmptyException):
        select_order_sheet_questions(db)

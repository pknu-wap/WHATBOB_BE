import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import get_current_user
from app.core.database import Base, get_db
from app.menus.models import OrderSheetQuestion, QuestionSelectionState
from app.recommend.common import QuestionPoolEmptyException
from app.recommend.order_sheet import select_order_sheet_questions
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


def add_question_topics(db, count, *, include_overlapping_meal_type: bool = False):
    pool = build_candidate_question_pool()
    all_clusters = list(dict.fromkeys(q["cluster_key"] for q in pool))
    if not include_overlapping_meal_type:
        # Spec #77: meal_type('든든한 식사')과 heaviness('가벼운 식사')는 반대 방향 의미 중복이므로
        # 비겹침 주제 수 검증 시 meal_type을 제외한 11개 독립 주제를 사용한다.
        all_clusters = [c for c in all_clusters if c != "meal_type"]
    clusters = all_clusters[:count]
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
    representatives = add_question_topics(db, 11)
    for seed in range(20):
        result = select_order_sheet_questions(db, total_count=9, exploitation_count=7, random_seed=seed)
        exploit = [q for q in result.questions if not q.is_exploration]
        explore = [q for q in result.questions if q.is_exploration]
        assert (result.total_count, result.exploitation_count, result.exploration_count) == (9, 7, 2)
        assert [q.id for q in exploit] == [q.id for q in representatives[:7]]
        assert len({q.id for q in result.questions}) == 9
        assert len({q.cluster_key for q in result.questions}) == 9
        assert {q.cluster_key for q in exploit}.isdisjoint(q.cluster_key for q in explore)
    first = select_order_sheet_questions(db, total_count=9, exploitation_count=7, random_seed=42)
    repeated = select_order_sheet_questions(db, total_count=9, exploitation_count=7, random_seed=42)
    assert [q.id for q in first.questions] == [q.id for q in repeated.questions]


@pytest.mark.parametrize("topics,total,exploit,expected", [
    (1, 9, 7, (1, 1, 0)),
    (7, 9, 7, (7, 7, 0)),
    (8, 9, 7, (8, 7, 1)),
    (9, 9, 7, (9, 7, 2)),
    (11, 20, 7, (11, 7, 4)),
    (11, 4, 7, (4, 4, 0)),
    (11, 9, 9, (9, 9, 0)),
    (11, 9, 1, (9, 1, 8)),
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
    user = User(kakao_id=704001, nickname="질문조회유저")
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        response = client.get("/api/recommendations/order-sheet/questions")
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["total_count"], data["exploitation_count"], data["exploration_count"]) == (8, 8, 0)
    questions = data["questions"]
    assert len({q["cluster_key"] for q in questions}) == 8
    assert all(q["cluster_key"] != inactive_cluster for q in questions)
    assert all(q["id"] != representatives[0].id for q in questions)
    assert any(q["cluster_key"] == representatives[0].cluster_key for q in questions)


def test_empty_active_question_pool_keeps_domain_error(db):
    with pytest.raises(QuestionPoolEmptyException):
        select_order_sheet_questions(db)


def test_session_rotates_topics_without_submitting_and_persists_in_database(db):
    add_question_topics(db, 11)
    recent = set()
    seen = set()
    initial_count = sum(q.shown_count for q in db.query(OrderSheetQuestion))
    for _ in range(8):
        result = select_order_sheet_questions(
            db, total_count=9, exploitation_count=7, random_seed=1, scope_key="guest:one",
        )
        topics = {q.cluster_key for q in result.questions if q.is_exploration}
        assert len(topics) == 2 and topics.isdisjoint(recent)
        seen.update(topics)
        recent = topics
        # 질문 조회는 탐색 순환 상태를 저장하고, 노출 횟수는 주문서 제출 시에 집계한다.
        assert sum(q.shown_count for q in db.query(OrderSheetQuestion)) == initial_count
        db.expire_all()
    assert len(seen) == 4
    state = db.get(QuestionSelectionState, "guest:one")
    assert sum(state.topic_counts.values()) == 16
    select_order_sheet_questions(db, total_count=9, exploitation_count=7, scope_key="guest:two")
    assert sum(db.get(QuestionSelectionState, "guest:two").topic_counts.values()) == 2
    assert sum(state.topic_counts.values()) == 16


def test_topic_shortage_reuses_only_when_necessary(db):
    add_question_topics(db, 9)
    for _ in range(3):
        result = select_order_sheet_questions(db, scope_key="guest:small")
        assert result.total_count == len({q.cluster_key for q in result.questions}) == 9


def test_http_user_session_and_no_store_prevent_repeated_questions(db):
    add_question_topics(db, 11)
    user = User(kakao_id=704002, nickname="탐색순환유저")
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        first = client.get("/api/recommendations/order-sheet/questions", params={"exploitation_count": 7})
        second = client.get("/api/recommendations/order-sheet/questions", params={"exploitation_count": 7})
    assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"
    first_topics = {q["cluster_key"] for q in first.json()["data"]["questions"] if q["is_exploration"]}
    second_topics = {q["cluster_key"] for q in second.json()["data"]["questions"] if q["is_exploration"]}
    assert first_topics.isdisjoint(second_topics)
    assert db.query(QuestionSelectionState).count() == 1

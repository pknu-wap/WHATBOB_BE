"""Observe replacement review through first-list feedback, the worker, and its reader."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.auth.models  # noqa: F401
import app.map.models  # noqa: F401
from app.auth.sessions import utc_now
from app.core.database import Base
from app.menus.models import Menu, OrderSheetIssuance, OrderSheetQuestion, OrderSheetReplaySnapshot, Recommendation
from app.recommend.feedback import record_recommendation_feedback, reject_recommendation_list
from app.recommend.order_sheet import create_order_sheet_recommendation
from app.recommend.question_review import (
    load_question_review,
    load_ranked_question_ids,
    run_pending_question_reviews,
)
from app.recommend.schemas import AnswerItemRequest, OrderSheetRecommendRequest, RecommendationFeedbackRequest
from app.users.models import User
from tests.test_issue39_mysql_concurrency import mysql_env  # noqa: F401


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture(params=("sqlite", "mysql"))
def review_db(request):
    if request.param == "sqlite":
        yield request.getfixturevalue("db")
        return

    environment = request.getfixturevalue("mysql_env")
    Base.metadata.create_all(environment["engine"])
    with environment["SessionLocal"]() as session:
        yield session


def add_exposure(db, user, created_at, *, menu_ids=(101,), scope="personal_initial"):
    row = Recommendation(
        user_id=user.id,
        recommended_menu_ids=list(menu_ids),
        initial_candidate_count=len(menu_ids),
        order_sheet_list_scope=scope,
        created_at=created_at,
    )
    db.add(row)
    db.flush()
    return row


def dislike_all(db, rec):
    for menu_id in rec.recommended_menu_ids:
        record_recommendation_feedback(
            db,
            rec.id,
            RecommendationFeedbackRequest(selected_menu_id=menu_id, is_satisfied=False),
            user_id=rec.user_id,
        )


def test_review_counts_distinct_recent_personal_lists_with_every_menu_disliked(db):
    user = User(kakao_id=890001, nickname="reviewer")
    db.add(user)
    db.flush()
    now = utc_now()
    shown = [add_exposure(db, user, now - timedelta(minutes=5)) for _ in range(7)]
    add_exposure(db, user, now, menu_ids=())
    ignored = [
        add_exposure(db, user, now, scope="group"),
        add_exposure(db, user, now, scope="personal_retry"),
        add_exposure(db, user, now, scope=None),
        add_exposure(db, user, now - timedelta(days=31)),
    ]
    db.commit()

    for row in ignored[:3]:
        dislike_all(db, row)
        assert row.closed_at is None
    reject_recommendation_list(db, shown[3].id, user_id=user.id)
    assert run_pending_question_reviews(db) == 0

    for row in (shown[0], shown[1], ignored[3]):
        dislike_all(db, row)
    assert load_question_review(db, user.id) is None
    assert run_pending_question_reviews(db) == 1
    review = load_question_review(db, user.id)
    assert (review.rejected_count, review.shown_count, review.eligible) == (2, 7, False)
    assert load_ranked_question_ids(db, user.id) == ()
    reject_recommendation_list(db, shown[0].id, user_id=user.id)
    assert run_pending_question_reviews(db) == 0

    for _ in range(3):
        add_exposure(db, user, now)
    db.commit()
    dislike_all(db, shown[2])
    assert run_pending_question_reviews(db) == 1
    review = load_question_review(db, user.id)
    assert (review.rejected_count, review.shown_count, review.eligible) == (3, 10, True)
    assert load_ranked_question_ids(db, user.id) == ()


def add_replay(db, user, completed_at, shown_ids, candidates, *, question_ids, neutral_ids=()):
    rec = add_exposure(db, user, completed_at, menu_ids=shown_ids)
    rec.initial_candidate_count = len(candidates)
    db.add(OrderSheetReplaySnapshot(
        recommendation_id=rec.id,
        user_id=user.id,
        completed_at=completed_at,
        expires_at=completed_at + timedelta(days=30),
        shown_menu_ids_json=shown_ids,
        score_question_ids_json=question_ids,
        answers_json=[
            {"question_id": question_id, "option_id": "any" if question_id in neutral_ids else "left"}
            for question_id in question_ids
        ],
        candidates_json=[
            {
                "menu_id": menu_id,
                "food_code": str(menu_id),
                "food_kind_key": str(menu_id),
                "other_score": other_score,
                "question_scores": question_scores,
            }
            for menu_id, other_score, question_scores in candidates
        ],
    ))
    return rec


def test_review_ranks_changes_then_actual_likes_then_neutral_answers(review_db):
    db = review_db
    user = User(kakao_id=890002, nickname="question impact")
    db.add(user)
    db.add_all([
        Menu(id=menu_id, food_code=str(menu_id), name=f"Meal {menu_id}", base_name=f"Meal {menu_id}", category_large="meal")
        for menu_id in (101, 102, 103, 104, 105, 201, 202)
    ])
    db.flush()
    now = utc_now()
    five_shown = add_replay(
        db, user, now - timedelta(minutes=2), [101, 102, 103, 104, 105],
        [
            (101, 2, {"10": 2}), (102, 2, {"20": 2}), (103, 4, {}),
            (104, 4, {}), (105, 2, {"70": 2}), (106, 3, {}),
        ],
        question_ids=[10, 20, 30, 40, 50, 70], neutral_ids=[40, 50],
    )
    two_shown = add_replay(
        db, user, now - timedelta(minutes=1), [202, 201],
        [(201, 2, {}), (202, 1, {"60": 0.75, "70": 0.75})],
        question_ids=[10, 20, 30, 40, 60, 70],
    )
    db.commit()

    for satisfied in (True, False):
        record_recommendation_feedback(
            db,
            five_shown.id,
            RecommendationFeedbackRequest(selected_menu_id=102, is_satisfied=satisfied),
            user_id=user.id,
        )
    for rec in (five_shown, two_shown):
        dislike_all(db, rec)
    tick = utc_now()
    assert run_pending_question_reviews(db, now=tick) == 1

    assert load_ranked_question_ids(db, user.id, now=tick) == (40, 50, 30, 10, 60, 20, 70)
    review = load_question_review(db, user.id, now=tick)
    assert (review.rejected_count, review.shown_count) == (2, 2)
    scores = {score.question_id: score for score in review.ranked_questions}
    assert scores[10].changed_sheet_count == 1
    assert scores[20].liked_sheet_count == 1
    assert scores[70].changed_sheet_count == 2

    short, long = review.sheet_impacts
    assert (short.recommendation_id, long.recommendation_id) == (two_shown.id, five_shown.id)
    short_impact = {impact.question_id: impact for impact in short.questions}[60]
    assert short_impact.first_place_changed
    assert short_impact.entered_menu_ids == short_impact.left_menu_ids == ()
    long_impacts = {impact.question_id: impact for impact in long.questions}
    assert long_impacts[10].first_place_changed
    assert not long_impacts[20].first_place_changed
    assert long_impacts[20].entered_menu_ids == (106,)
    assert long_impacts[20].left_menu_ids == long_impacts[20].liked_original_only_menu_ids == (102,)


def test_disliking_all_menus_after_a_zero_candidate_first_list_does_not_start_review(db):
    user = User(kakao_id=890003, nickname="retry")
    question = OrderSheetQuestion(
        question_code="review_soup",
        cluster_key="has_soup",
        question_text="Soup?",
        options_json=[
            {"option_id": "yes", "rules": [{"field": "has_soup", "op": "eq", "value": True}]},
            {"option_id": "no", "rules": [{"field": "has_soup", "op": "eq", "value": False}]},
        ],
    )
    db.add_all([
        user,
        question,
        Menu(food_code="L001", name="rice", base_name="rice", category_large="meal", has_soup=False),
        Menu(food_code="L002", name="noodles", base_name="noodles", category_large="meal", has_soup=False),
    ])
    db.flush()
    issuance = OrderSheetIssuance(
        order_sheet_id="review-retry",
        user_id=user.id,
        question_ids_json=[question.id],
        question_roles_json={str(question.id): "hard_filter"},
    )
    db.add(issuance)
    db.commit()
    first, retry = (
        create_order_sheet_recommendation(
            db,
            OrderSheetRecommendRequest(
                order_sheet_id=issuance.order_sheet_id,
                answers=[AnswerItemRequest(question_id=question.id, option_id=option_id)],
            ),
            user_id=user.id,
        )
        for option_id in ("yes", "no")
    )

    assert (first.candidate_count, retry.candidate_count) == (0, 2)
    retry_rec = db.get(Recommendation, retry.recommendation_id)
    dislike_all(db, retry_rec)
    assert retry_rec.closed_at is None
    assert run_pending_question_reviews(db) == 0
    assert load_question_review(db, user.id) is None

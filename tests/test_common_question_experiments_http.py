"""Common question experiments through issuance, reactions, and the experiment worker."""

from datetime import datetime, timedelta, timezone

import pytest

from app.menus.models import (
    PERSONAL_INITIAL_LIST,
    CommonOrderSheetQuestionAssignment,
    CommonOrderSheetQuestionExperiment,
    OrderSheetIssuance,
    OrderSheetQuestion,
    PersonalOrderSheetQuestionExperiment,
    Recommendation,
    RecommendationFeedback,
)
from app.recommend.common_question_experiments import run_common_question_experiments
from app.users.models import User
from tests.test_order_sheet_issuance import _seed_diverse_menus_and_full_pool
from tests.test_order_sheet_issuance_http import _make_client, db
from tests.test_personal_question_experiments_http import _issue, _question_ids, _submit


def _add_user(db, number):
    user = User(kakao_id=930000 + number, nickname=f"common experiment {number}")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def adopted_question(db):
    _seed_diverse_menus_and_full_pool(db)
    existing_user = _add_user(db, 0)
    with _make_client(db, existing_user) as client:
        baseline = _issue(client)
    control = next(q for q in baseline["questions"] if q["cluster_key"] == "carb_level")
    replacement = db.query(OrderSheetQuestion).filter_by(cluster_key="temperature").first()
    personal = PersonalOrderSheetQuestionExperiment(
        user_id=existing_user.id,
        slot_key=control["cluster_key"],
        control_question_id=control["id"],
        variant_question_id=replacement.id,
        control_questions_json=baseline["questions"],
        variant_questions_json=[],
        question_roles_json={},
        adopted=True,
        completed_at=datetime.now(timezone.utc),
    )
    variant_id = replacement.id
    db.add(personal)
    db.flush()
    personal.active = None
    db.commit()
    return baseline, variant_id


def test_only_first_issuance_gets_the_assigned_question_and_request_retry_replays_it(
    db, adopted_question, monkeypatch,
):
    baseline, variant_id = adopted_question
    assert run_common_question_experiments(db) is True
    monkeypatch.setattr("app.recommend.common_question_experiments.random.choice", lambda choices: choices[1])

    with _make_client(db, _add_user(db, 1)) as client:
        first = _issue(client, "open-first-sheet")
        assert _issue(client, "open-first-sheet") == first
        after_reentry = _issue(client)

    assert variant_id in {question["id"] for question in first["questions"]}
    assert after_reentry["questions"] == baseline["questions"]
    assert sum(a != b for a, b in zip(first["questions"], baseline["questions"])) == 1


def _append_submissions(db, experiment, arm, submissions):
    """Load historical submissions with the event timestamps emitted by the public API."""
    snapshot = experiment.control_questions_json if arm == "control" else experiment.variant_questions_json
    questions = [entry["id"] for entry in snapshot]
    for index, (submitted_at, outcome) in enumerate(submissions):
        recommendation = Recommendation(
            shown_question_ids=questions,
            answers_json=[{"question_id": question, "option_id": "any"} for question in questions],
            recommended_menu_ids=[1],
            initial_candidate_count=1,
            order_sheet_list_scope=PERSONAL_INITIAL_LIST,
            created_at=submitted_at - timedelta(milliseconds=1),
        )
        db.add(recommendation)
        db.flush()
        issuance = OrderSheetIssuance(
            order_sheet_id=f"{arm}-{submitted_at.isoformat()}-{index}",
            question_ids_json=questions,
            questions_snapshot_json=snapshot,
            status="COMPLETED",
            recommendation_id=recommendation.id,
            completed_at=submitted_at,
        )
        db.add(issuance)
        db.flush()
        db.add(CommonOrderSheetQuestionAssignment(issuance_id=issuance.id, experiment_id=experiment.id, arm=arm))
        if outcome in {"liked", "last-moment-like", "late-like"}:
            reaction_delay = {
                "liked": timedelta(hours=1),
                "last-moment-like": timedelta(hours=24),
                "late-like": timedelta(hours=24, microseconds=1),
            }[outcome]
            db.add(RecommendationFeedback(
                recommendation_id=recommendation.id,
                selected_menu_id=1,
                is_satisfied=True,
                created_at=submitted_at + reaction_delay,
            ))
    db.commit()


@pytest.mark.parametrize("variant_likes, adopted", [(50, False), (54, True)])
def test_first_hundred_per_arm_and_each_reaction_window_determine_next_sheet(db, adopted_question, variant_likes, adopted):
    baseline, variant_id = adopted_question
    start = datetime.now(timezone.utc)
    assert run_common_question_experiments(db, now=start) is True
    experiment = db.query(CommonOrderSheetQuestionExperiment).one()

    _append_submissions(db, experiment, "control", [
        (start, "liked" if index < 40 else "no-response") for index in range(100)
    ])
    _append_submissions(db, experiment, "variant", [
        (start, "liked" if index < variant_likes - 1 else "late-like" if index < 60 else "no-response")
        for index in range(99)
    ])
    assert run_common_question_experiments(db, now=start + timedelta(hours=24)) is True

    _append_submissions(db, experiment, "control", [
        (start + timedelta(minutes=1), "liked") for _ in range(10)
    ])
    last_submission = start + timedelta(hours=1)
    _append_submissions(db, experiment, "variant", [(last_submission, "last-moment-like")])
    before_last_cutoff = last_submission + timedelta(hours=24, microseconds=-1)
    assert run_common_question_experiments(db, now=before_last_cutoff) is True

    caught_up_user = _add_user(db, 2)
    with _make_client(db, caught_up_user) as client:
        assert _issue(client)["questions"] == baseline["questions"]

    last_cutoff = last_submission + timedelta(hours=24)
    db.rollback()
    assert run_common_question_experiments(db, now=last_cutoff) is True
    assert run_common_question_experiments(db, now=last_cutoff) is False
    with _make_client(db, caught_up_user) as client:
        next_questions = _issue(client)["questions"]
    assert (variant_id in {question["id"] for question in next_questions}) is adopted


def test_existing_issuance_and_legacy_submission_are_ineligible_for_first_sheet_randomization(
    db, adopted_question, monkeypatch,
):
    baseline, _ = adopted_question
    previous_issuer = _add_user(db, 3)
    legacy_user = _add_user(db, 4)
    with _make_client(db, previous_issuer) as client:
        _issue(client)
    with _make_client(db, legacy_user) as client:
        _submit(client, {**baseline, "order_sheet_id": None})
    db.rollback()
    assert run_common_question_experiments(db) is True
    monkeypatch.setattr("app.recommend.common_question_experiments.random.choice", lambda choices: choices[1])

    for user in (previous_issuer, legacy_user):
        with _make_client(db, user) as client:
            assert _question_ids(_issue(client)) == _question_ids(baseline)

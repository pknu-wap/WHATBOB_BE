"""Personal question experiments through order-sheet issuance and reaction APIs."""

import pytest

from app.core.config import settings
from app.menus.models import OrderSheetQuestion, UserFoodExclusion
from app.recommend.question_review import run_pending_question_reviews
from app.users.models import User
from tests.test_order_sheet_issuance import _seed_diverse_menus_and_full_pool
from tests.test_order_sheet_issuance_http import _make_client, db


@pytest.fixture
def user(db):
    _seed_diverse_menus_and_full_pool(db)
    user = User(kakao_id=909001, nickname="personal experiment")
    db.add(user)
    db.commit()
    return user


def _issue(client, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    response = client.get("/api/recommendations/order-sheet/questions", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _submit(client, sheet, questions=None):
    response = client.post(
        "/api/recommendations/order-sheet",
        json={
            "order_sheet_id": sheet["order_sheet_id"],
            "answers": [
                {"question_id": question["id"], "option_id": "any"}
                for question in (sheet["questions"] if questions is None else questions)
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _reject_first_two_lists(client):
    for _ in range(2):
        sheet = _issue(client)
        _reject(client, _submit(client, sheet))
    return sheet


def _question_ids(sheet):
    return [question["id"] for question in sheet["questions"]]


def _reject(client, recommendation):
    response = client.post(f"/api/recommendations/{recommendation['recommendation_id']}/all-menu-reject")
    assert response.status_code == 200, response.text


def test_retries_keep_assignment_and_every_two_new_issuances_get_both_questions(db, user):
    with _make_client(db, user) as client:
        baseline = _reject_first_two_lists(client)
        assert run_pending_question_reviews(db) == 1
        first = _issue(client, "experiment-screen-1")
        assert _issue(client, "experiment-screen-1") == first
        second = _issue(client, "experiment-screen-2")
        third = _issue(client)
        fourth = _issue(client)

    assert len({sheet["order_sheet_id"] for sheet in (first, second, third, fourth)}) == 4
    different_positions = [
        index
        for index, questions in enumerate(zip(first["questions"], second["questions"]))
        if questions[0] != questions[1]
    ]
    assert len(different_positions) == 1
    assert baseline["questions"][different_positions[0]]["cluster_key"] == "flavor_intensity"
    assert {
        tuple(_question_ids(first)), tuple(_question_ids(second)),
    } == {
        tuple(_question_ids(third)), tuple(_question_ids(fourth)),
    }
    assert _question_ids(baseline) in [_question_ids(first), _question_ids(second)]
    assert all(
        next(arm["questions"] for arm in (first, second) if _question_ids(arm) == _question_ids(sheet))
        == sheet["questions"]
        for sheet in (third, fourth)
    )


def test_an_unfit_frozen_sheet_skips_that_issuance_and_keeps_the_pending_arm(db, user):
    with _make_client(db, user) as client:
        _reject_first_two_lists(client)
        assert run_pending_question_reviews(db) == 1
        first = _issue(client, "experiment-before-allergy")

        wheat = UserFoodExclusion(
            user_id=user.id, exclusion_type="ALLERGY", keyword="wheat", catalog_code="wheat",
        )
        db.add(wheat)
        db.commit()
        skipped = _issue(client)
        assert len(skipped["questions"]) == 9
        assert "cooking_style" not in {question["cluster_key"] for question in skipped["questions"]}
        assert _issue(client, "experiment-before-allergy") == first

        db.delete(wheat)
        db.commit()
        second = _issue(client)

    assert sum(left != right for left, right in zip(first["questions"], second["questions"])) == 1


def test_an_alternate_with_identical_menu_matches_does_not_start_an_experiment(db, user):
    for question in db.query(OrderSheetQuestion).all():
        if question.cluster_key not in settings.ORDER_SHEET_INITIAL_CLUSTERS:
            question.is_active = False
    identical_rules = [{"field": "food_code", "op": "eq", "value": "S001"}]
    db.add(OrderSheetQuestion(
        question_code="same_menu_in_both_answers",
        cluster_key="distinct_label_with_same_menu",
        question_text="Which ingredient sounds better?",
        options_json=[
            {"option_id": "left", "label": "A", "rules": identical_rules},
            {"option_id": "right", "label": "B", "rules": identical_rules},
            {"option_id": "any", "label": "Either", "rules": []},
        ],
        initial_score=1.0,
        importance_weight=1.0,
        is_active=True,
    ))
    db.commit()

    with _make_client(db, user) as client:
        baseline = _reject_first_two_lists(client)
        assert run_pending_question_reviews(db) == 1
        assert _question_ids(_issue(client)) == _question_ids(baseline)
        assert _question_ids(_issue(client)) == _question_ids(baseline)


def test_only_the_first_full_submission_of_an_owned_experiment_sheet_is_its_initial_list(db, user):
    other_user = User(kakao_id=909002, nickname="another experiment user")
    db.add(other_user)
    db.commit()
    with _make_client(db, user) as client:
        _reject_first_two_lists(client)
        assert run_pending_question_reviews(db) == 1
        sheet = _issue(client)
        with _make_client(db, other_user) as other_client:
            _reject(other_client, _submit(other_client, sheet))
        _reject(client, _submit(client, sheet, sheet["questions"][:1]))
        assert run_pending_question_reviews(db) == 0

        _reject(client, _submit(client, sheet))
        assert run_pending_question_reviews(db) == 1
        _reject(client, _submit(client, sheet))
        assert run_pending_question_reviews(db) == 0

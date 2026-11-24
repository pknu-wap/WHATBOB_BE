"""Personal question experiments through order-sheet issuance and reaction APIs."""

import pytest

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
        recommendation = _submit(client, sheet)
        response = client.post(
            f"/api/recommendations/{recommendation['recommendation_id']}/reject"
        )
        assert response.status_code == 200, response.text
    return sheet


def _question_ids(sheet):
    return [question["id"] for question in sheet["questions"]]


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

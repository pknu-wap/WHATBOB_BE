"""Personal question decisions through order-sheet HTTP and worker entry points."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.recommend.personal_experiment_decisions import run_personal_experiment_decision_batch
from app.recommend.question_review import run_pending_question_reviews
from app.users.models import User
from tests.test_order_sheet_issuance import _seed_diverse_menus_and_full_pool
from tests.test_order_sheet_issuance_http import _make_client, db


@pytest.fixture
def user(db):
    _seed_diverse_menus_and_full_pool(db)
    user = User(kakao_id=910001, nickname="personal question decision")
    db.add(user)
    db.commit()
    return user


def _issue(client, key=None):
    response = client.get(
        "/api/recommendations/order-sheet/questions",
        headers={"Idempotency-Key": key} if key else {},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _submit(client, sheet, *, options=None, partial=False):
    questions = sheet["questions"][:1] if partial else sheet["questions"]
    options = options or {}
    response = client.post(
        "/api/recommendations/order-sheet",
        json={
            "order_sheet_id": sheet["order_sheet_id"],
            "answers": [
                {"question_id": question["id"], "option_id": options.get(question["cluster_key"], "any")}
                for question in questions
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _menu_feedback(client, recommendation, *, liked):
    response = client.post(
        f"/api/recommendations/{recommendation['recommendation_id']}/feedback",
        json={
            "selected_menu_id": recommendation["recommended_menus"][0]["menu_id"],
            "is_satisfied": liked,
        },
    )
    assert response.status_code == 200, response.text


def _reject(client, recommendation):
    response = client.post(f"/api/recommendations/{recommendation['recommendation_id']}/reject")
    assert response.status_code == 200, response.text


def _question_ids(sheet):
    return tuple(question["id"] for question in sheet["questions"])


def _start_review(client, db: Session):
    for _ in range(2):
        baseline = _issue(client)
        _reject(client, _submit(client, baseline))
    with Session(db.get_bind()) as worker_db:
        assert run_pending_question_reviews(worker_db) == 1
    return _question_ids(baseline)


def _issue_pair(client, control_ids, *, key=None):
    first = _issue(client, key)
    second = _issue(client)
    control, variant = (first, second) if _question_ids(first) == control_ids else (second, first)
    assert _question_ids(control) == control_ids
    assert sum(a != b for a, b in zip(control_ids, _question_ids(variant))) == 1
    return control, variant


def test_personal_question_adoption_requires_fifteen_explicit_lists_in_each_arm(db, user):
    session_factory = sessionmaker(bind=db.get_bind())
    with _make_client(db, user) as client:
        control_ids = _start_review(client, db)
        assert run_personal_experiment_decision_batch(session_factory) == 0

        for order in range(14):
            control, variant = _issue_pair(client, control_ids)
            control_rec = _submit(client, control)
            variant_rec = _submit(client, variant)
            if order < 9:
                _menu_feedback(client, control_rec, liked=True)
            else:
                _reject(client, control_rec)
            if order < 11:
                _menu_feedback(client, variant_rec, liked=True)
            else:
                _reject(client, variant_rec)

        control, variant_no_response = _issue_pair(client, control_ids)
        _reject(client, _submit(client, control))
        _submit(client, variant_no_response)

        _, variant_zero = _issue_pair(client, control_ids)
        zero_rec = _submit(client, variant_zero, options={"cooking_style": "noodle", "main_ingredient": "meat"})
        assert zero_rec["recommended_menus"] == []
        _menu_feedback(client, _submit(client, variant_zero), liked=True)

        _, variant = _issue_pair(client, control_ids, key="final-pair-entry")
        frozen_issuance = _issue(client, "final-pair-entry")
        _menu_feedback(client, _submit(client, variant, partial=True), liked=True)
        assert run_personal_experiment_decision_batch(session_factory) == 0

        variant_rec = _submit(client, variant)
        _menu_feedback(client, variant_rec, liked=True)
        _menu_feedback(client, variant_rec, liked=False)

        assert run_personal_experiment_decision_batch(session_factory) == 1
        variant_ids = _question_ids(variant)
        changed_position = next(i for i, question_id in enumerate(control_ids) if question_id != variant_ids[i])
        assert _question_ids(_issue(client))[changed_position] == variant_ids[changed_position]
        assert run_personal_experiment_decision_batch(session_factory) == 0
        assert _question_ids(_issue(client))[changed_position] == variant_ids[changed_position]
        assert _issue(client, "final-pair-entry") == frozen_issuance


def test_personal_question_retention_when_list_rejection_rises_despite_more_likes(db, user):
    session_factory = sessionmaker(bind=db.get_bind())
    with _make_client(db, user) as client:
        control_ids = _start_review(client, db)
        for order in range(15):
            control, variant = _issue_pair(client, control_ids)
            control_rec = _submit(client, control)
            variant_rec = _submit(client, variant)
            if order < 9:
                _menu_feedback(client, control_rec, liked=True)
            else:
                _reject(client, control_rec)
            if order < 12:
                _menu_feedback(client, variant_rec, liked=True)
            if order >= 8:
                _reject(client, variant_rec)

        assert run_personal_experiment_decision_batch(session_factory) == 1
        variant_ids = _question_ids(variant)
        changed_position = next(i for i, question_id in enumerate(control_ids) if question_id != variant_ids[i])
        next_slot_ids = {_question_ids(_issue(client))[changed_position] for _ in range(2)}
        assert control_ids[changed_position] in next_slot_ids
        assert variant_ids[changed_position] not in next_slot_ids
        assert run_personal_experiment_decision_batch(session_factory) == 0

"""Personal order-sheet question selection and paired assignments."""

from collections.abc import Mapping
from dataclasses import dataclass
import random

from sqlalchemy.orm import Session

from app.menus.models import (
    CommonOrderSheetQuestionExperiment,
    OrderSheetQuestion,
    PersonalOrderSheetQuestionAssignment,
    PersonalOrderSheetQuestionExperiment,
    PersonalOrderSheetQuestionOverride,
)
from app.recommend.common import evaluate_option_rules
from app.recommend.order_sheet import OrderSheetQuestionPool, _question_options, _to_question_dto
from app.recommend.question_review import load_ranked_question_ids
from app.recommend.ranking import _is_neutral_option, resolve_question_role
from app.recommend.schemas import OrderSheetQuestionDTO, OrderSheetQuestionsResponse

CONTROL = "control"
VARIANT = "variant"


@dataclass(frozen=True)
class PersonalQuestionAssignment:
    experiment_id: int
    pair_number: int
    arm: str
    selection: OrderSheetQuestionsResponse
    question_roles: dict[str, str]


def load_personal_question_overrides(db: Session, user_id: int) -> dict[str, int]:
    rows = (
        db.query(PersonalOrderSheetQuestionOverride)
        .filter_by(user_id=user_id)
        .with_for_update()
        .all()
    )
    return {row.slot_key: row.question_id for row in rows}


def merge_personal_question_state(db: Session, origin_id: int, target_id: int) -> None:
    """Move experiment history and overrides when the two users are locked for linking."""
    active_target = (
        db.query(PersonalOrderSheetQuestionExperiment.id)
        .filter_by(user_id=target_id, active=True)
        .with_for_update()
        .scalar()
    )
    origin_experiments = (
        db.query(PersonalOrderSheetQuestionExperiment)
        .filter_by(user_id=origin_id)
        .with_for_update()
        .all()
    )
    for experiment in origin_experiments:
        experiment.user_id = target_id
        if active_target is not None:
            experiment.active = None
            experiment.decision_requested_at = None

    target_slots = load_personal_question_overrides(db, target_id)
    origin_overrides = (
        db.query(PersonalOrderSheetQuestionOverride)
        .filter_by(user_id=origin_id)
        .with_for_update()
        .all()
    )
    for override in origin_overrides:
        if override.slot_key in target_slots:
            db.delete(override)
        else:
            override.user_id = target_id


def _axes_by_question_id(question_pool: OrderSheetQuestionPool) -> dict[int, frozenset[str]]:
    return {question.id: axes for question, axes, _ in question_pool.valid_candidates}


def apply_question_overrides(
    selection: OrderSheetQuestionsResponse,
    slot_keys: tuple[str, ...],
    question_pool: OrderSheetQuestionPool,
    overrides: Mapping[str, int],
) -> OrderSheetQuestionsResponse:
    """Apply eligible score-slot overrides while preserving distinct topics."""
    if not overrides:
        return selection
    candidates = {question.id: question for question, _, _ in question_pool.valid_candidates}
    axes_by_id = _axes_by_question_id(question_pool)
    questions = list(selection.questions)
    for index, slot_key in enumerate(slot_keys):
        original = questions[index]
        replacement = candidates.get(overrides.get(slot_key))
        if replacement is None or (
            resolve_question_role(original.id, original.cluster_key) != "score"
            or resolve_question_role(replacement.id, replacement.cluster_key) != "score"
        ):
            continue
        other_axes = frozenset().union(*(
            axes_by_id.get(question.id, frozenset())
            for position, question in enumerate(questions) if position != index
        ))
        if axes_by_id[replacement.id].isdisjoint(other_axes):
            questions[index] = _to_question_dto(replacement, is_exploration=original.is_exploration)
    return selection.model_copy(update={"questions": questions})


def distinguishes_available_menus(
    question: OrderSheetQuestion,
    question_pool: OrderSheetQuestionPool,
) -> bool:
    matched_groups = set()
    for option in _question_options(question):
        if _is_neutral_option(option):
            continue
        matches = frozenset(
            menu.id for menu in question_pool.available_menus
            if evaluate_option_rules(menu, option["rules"])
        )
        if matches:
            matched_groups.add(matches)
            if len(matched_groups) >= 2:
                return True
    return False


def _snapshot(questions: list[OrderSheetQuestionDTO]) -> list[dict]:
    return [question.model_dump(mode="json", by_alias=True) for question in questions]


def _start_experiment(
    db: Session,
    user_id: int,
    selection: OrderSheetQuestionsResponse,
    slot_keys: tuple[str, ...],
    question_pool: OrderSheetQuestionPool,
) -> PersonalOrderSheetQuestionExperiment | None:
    ranked_question_ids = load_ranked_question_ids(db, user_id)
    if not ranked_question_ids:
        return None
    axes_by_id = _axes_by_question_id(question_pool)
    completed = (
        db.query(PersonalOrderSheetQuestionExperiment)
        .filter_by(user_id=user_id)
        .filter(PersonalOrderSheetQuestionExperiment.completed_at.is_not(None))
        .with_for_update()
        .all()
    )
    tested = {
        (row.slot_key, frozenset((row.control_question_id, row.variant_question_id)))
        for row in completed
    }
    positions = {question.id: index for index, question in enumerate(selection.questions)}
    occupied_axes = frozenset().union(*(
        axes_by_id[question.id] for question in selection.questions
    ))
    ranked_candidates = question_pool.ranked_candidates()
    eligible_candidates = [
        question
        for question, axes, _ in ranked_candidates
        if axes.isdisjoint(occupied_axes)
        and resolve_question_role(question.id, question.cluster_key) == "score"
        and distinguishes_available_menus(question, question_pool)
    ]
    for question_id in ranked_question_ids:
        index = positions.get(question_id)
        if index is None:
            continue
        control = selection.questions[index]
        if resolve_question_role(control.id, control.cluster_key) != "score":
            continue
        slot_key = slot_keys[index]
        variant = next((
            question for question in eligible_candidates
            if (slot_key, frozenset((control.id, question.id))) not in tested
        ), None)
        if variant is None:
            continue
        variant_questions = list(selection.questions)
        variant_questions[index] = _to_question_dto(variant, is_exploration=control.is_exploration)
        question_roles = {
            str(question.id): resolve_question_role(question.id, question.cluster_key)
            for question in selection.questions
        }
        question_roles[str(variant.id)] = "score"
        return PersonalOrderSheetQuestionExperiment(
            user_id=user_id,
            slot_key=slot_key,
            control_question_id=control.id,
            variant_question_id=variant.id,
            control_questions_json=_snapshot(selection.questions),
            variant_questions_json=_snapshot(variant_questions),
            question_roles_json=question_roles,
        )
    return None


def frozen_experiment_questions_fit(
    experiment: PersonalOrderSheetQuestionExperiment | CommonOrderSheetQuestionExperiment,
    question_pool: OrderSheetQuestionPool,
    target_count: int,
) -> bool:
    if (
        len(experiment.control_questions_json) != target_count
        or len(experiment.variant_questions_json) != target_count
    ):
        return False
    candidates = {question.id: (question, axes) for question, axes, _ in question_pool.valid_candidates}
    if experiment.variant_question_id not in candidates or not distinguishes_available_menus(
        candidates[experiment.variant_question_id][0], question_pool,
    ):
        return False
    for sheet in (experiment.control_questions_json, experiment.variant_questions_json):
        used_axes: set[str] = set()
        for data in sheet:
            question_id = data["id"]
            if question_id not in candidates:
                return False
            question, axes = candidates[question_id]
            if used_axes.intersection(axes):
                return False
            current_options = _to_question_dto(question).model_dump(mode="json")["options"]
            frozen_rules = [(option["option_id"], option["rules"]) for option in data["options"]]
            if frozen_rules != [(option["option_id"], option["rules"]) for option in current_options]:
                return False
            used_axes.update(axes)
    return True


def _next_pair_arm(
    db: Session,
    experiment_id: int,
) -> tuple[int, str]:
    last_pair = (
        db.query(PersonalOrderSheetQuestionAssignment)
        .filter_by(experiment_id=experiment_id)
        .order_by(
            PersonalOrderSheetQuestionAssignment.pair_number.desc(),
            PersonalOrderSheetQuestionAssignment.issuance_id.desc(),
        )
        .limit(2)
        .with_for_update()
        .all()
    )
    if last_pair and (len(last_pair) == 1 or last_pair[0].pair_number != last_pair[1].pair_number):
        latest = last_pair[0]
        return latest.pair_number, VARIANT if latest.arm == CONTROL else CONTROL
    return (last_pair[0].pair_number + 1 if last_pair else 1), random.choice((CONTROL, VARIANT))


def pick_personal_question_assignment(
    db: Session,
    user_id: int,
    selection: OrderSheetQuestionsResponse,
    slot_keys: tuple[str, ...],
    question_pool: OrderSheetQuestionPool,
    target_count: int,
) -> PersonalQuestionAssignment | None:
    """Pick an arm for a fresh issuance while the issuing transaction holds the actor lock."""
    experiment = (
        db.query(PersonalOrderSheetQuestionExperiment)
        .filter_by(user_id=user_id, active=True)
        .with_for_update()
        .one_or_none()
    )
    if experiment is None:
        if selection.total_count != target_count:
            return None
        experiment = _start_experiment(db, user_id, selection, slot_keys, question_pool)
        if experiment is None:
            return None
        db.add(experiment)
        db.flush()
    elif not frozen_experiment_questions_fit(experiment, question_pool, target_count):
        return None

    pair_number, arm = _next_pair_arm(db, experiment.id)
    snapshot = experiment.control_questions_json if arm == CONTROL else experiment.variant_questions_json
    questions = [OrderSheetQuestionDTO.model_validate(data) for data in snapshot]
    frozen_selection = OrderSheetQuestionsResponse(
        questions=questions,
        total_count=len(questions),
        exploitation_count=sum(not question.is_exploration for question in questions),
        exploration_count=sum(question.is_exploration for question in questions),
    )
    return PersonalQuestionAssignment(
        experiment.id,
        pair_number,
        arm,
        frozen_selection,
        {str(question.id): experiment.question_roles_json[str(question.id)] for question in questions},
    )

"""Shared trials of score questions already adopted in personal experiments."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import random

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.sessions import as_utc_naive, utc_now
from app.core.config import settings
from app.menus.models import (
    GROUP_ORDER_SHEET_LIST,
    PERSONAL_INITIAL_LIST,
    CommonOrderSheetQuestionAssignment,
    CommonOrderSheetQuestionExperiment,
    OrderSheetIssuance,
    PersonalOrderSheetQuestionExperiment,
    Recommendation,
)
from app.recommend.common import QuestionPoolEmptyException
from app.recommend.experiment_metrics import ArmMetrics, decide_common_experiment
from app.recommend.feedback import summarize_first_list_reactions
from app.recommend.order_sheet import (
    OrderSheetQuestionPool,
    load_order_sheet_question_pool,
    select_order_sheet_questions,
)
from app.recommend.question_experiments import (
    CONTROL,
    VARIANT,
    apply_question_overrides,
    distinguishes_available_menus,
    frozen_experiment_questions_fit,
)
from app.recommend.ranking import resolve_question_role, resolve_score_weight
from app.recommend.schemas import OrderSheetQuestionDTO, OrderSheetQuestionsResponse


@dataclass(frozen=True)
class CommonQuestionAssignment:
    experiment_id: int
    arm: str
    selection: OrderSheetQuestionsResponse
    question_roles: dict[str, str]


def load_common_question_overrides(db: Session) -> dict[str, int]:
    experiment = CommonOrderSheetQuestionExperiment
    latest_ids = (
        db.query(func.max(experiment.id))
        .filter(experiment.adopted.is_(True))
        .group_by(experiment.slot_key)
        .statement
    )
    return dict(
        db.query(experiment.slot_key, experiment.variant_question_id)
        .filter(experiment.id.in_(latest_ids))
        .all()
    )


def pick_common_question_assignment(
    db: Session,
    user_id: int,
    question_pool: OrderSheetQuestionPool,
    target_count: int,
) -> CommonQuestionAssignment | None:
    """Assign only users whose first issuance can support either frozen form."""
    experiment = db.query(CommonOrderSheetQuestionExperiment).filter_by(active=True).one_or_none()
    if experiment is None or len(experiment.cohorts_json) == 2:
        return None
    if db.query(OrderSheetIssuance.id).filter_by(user_id=user_id).first() is not None:
        return None
    # Older clients could submit directly without first creating an issuance.
    if db.query(Recommendation.id).filter(
        Recommendation.user_id == user_id,
        or_(
            Recommendation.order_sheet_list_scope.is_(None),
            Recommendation.order_sheet_list_scope != GROUP_ORDER_SHEET_LIST,
        ),
    ).first() is not None:
        return None
    if not frozen_experiment_questions_fit(experiment, question_pool, target_count):
        return None
    arm = random.choice((CONTROL, VARIANT))
    snapshot = experiment.control_questions_json if arm == CONTROL else experiment.variant_questions_json
    questions = [OrderSheetQuestionDTO.model_validate(data) for data in snapshot]
    return CommonQuestionAssignment(
        experiment.id,
        arm,
        OrderSheetQuestionsResponse(
            questions=questions,
            total_count=len(questions),
            exploitation_count=len(questions),
            exploration_count=0,
        ),
        {str(question.id): experiment.question_roles_json[str(question.id)] for question in questions},
    )


def _open_experiment(db: Session, now: datetime) -> bool:
    personal = PersonalOrderSheetQuestionExperiment
    common = CommonOrderSheetQuestionExperiment
    previously_tested = db.query(common.id).filter(
        common.slot_key == personal.slot_key,
        common.variant_question_id == personal.variant_question_id,
    ).exists()
    candidates = db.query(personal).filter(
        personal.active.is_(None),
        personal.adopted.is_(True),
        personal.completed_at <= now,
        ~previously_tested,
    )
    if candidates.with_entities(personal.id).first() is None:
        return False

    try:
        pool = load_order_sheet_question_pool(db)
    except QuestionPoolEmptyException:
        return False
    baseline = select_order_sheet_questions(db, commit=False, question_pool=pool)
    if baseline.total_count != settings.ORDER_SHEET_QUESTION_COUNT:
        return False
    slot_keys = tuple(question.cluster_key for question in baseline.questions)
    control = apply_question_overrides(baseline, slot_keys, pool, load_common_question_overrides(db))
    control_positions = {slot_key: index for index, slot_key in enumerate(slot_keys)}
    eligible_variants = {
        question.id: axes
        for question, axes, _ in pool.valid_candidates
        if resolve_question_role(question.id, question.cluster_key) == "score"
        and distinguishes_available_menus(question, pool)
    }
    if not eligible_variants:
        return False

    supported_slots = {
        slot_key: question.id for slot_key, question in zip(slot_keys, control.questions)
        if resolve_question_role(question.id, question.cluster_key) == "score"
    }
    trials = (
        candidates.with_entities(personal.slot_key, personal.variant_question_id)
        .filter(
            personal.slot_key.in_(supported_slots),
            personal.variant_question_id.in_(eligible_variants),
        )
        .group_by(personal.slot_key, personal.variant_question_id)
        .order_by(func.min(personal.completed_at), func.min(personal.id))
        .all()
    )
    axes_by_id = {question.id: axes for question, axes, _ in pool.valid_candidates}
    for slot, variant_id in trials:
        control_id = supported_slots[slot]
        if axes_by_id[control_id] & eligible_variants[variant_id]:
            continue
        variant = apply_question_overrides(control, slot_keys, pool, {slot: variant_id})
        if variant.questions[control_positions[slot]].id != variant_id:
            continue
        roles = {
            str(question.id): resolve_question_role(question.id, question.cluster_key)
            for question in control.questions
        }
        weights = {
            str(question.id): resolve_score_weight(question.id, question.cluster_key, question.question_code)
            for question in control.questions if roles[str(question.id)] == "score"
        }
        roles[str(variant_id)] = "score"
        weights[str(variant_id)] = weights[str(control_id)]
        experiment = common(
            slot_key=slot,
            control_question_id=control_id,
            variant_question_id=variant_id,
            control_questions_json=[question.model_dump(mode="json", by_alias=True) for question in control.questions],
            variant_questions_json=[question.model_dump(mode="json", by_alias=True) for question in variant.questions],
            question_roles_json=roles,
            score_weights_json=weights,
            ranking_rule_version=settings.ORDER_SHEET_RANKING_RULE_VERSION,
            submissions_per_arm=settings.ORDER_SHEET_COMMON_SUBMISSIONS_PER_ARM,
            reaction_window_hours=settings.ORDER_SHEET_COMMON_REACTION_WINDOW_HOURS,
            created_at=now,
        )
        try:
            with db.begin_nested():
                db.add(experiment)
                db.flush()
        except IntegrityError:
            # Another worker can win the unique active slot before this insert.
            if db.query(common.id).filter_by(active=True).with_for_update().scalar() is None:
                raise
            return False
        return True
    return False


def _capture_cohort(db: Session, experiment: CommonOrderSheetQuestionExperiment, arm: str) -> dict | None:
    assignment = CommonOrderSheetQuestionAssignment
    submissions = (
        db.query(OrderSheetIssuance.recommendation_id, OrderSheetIssuance.completed_at)
        .join(assignment, assignment.issuance_id == OrderSheetIssuance.id)
        .join(Recommendation, Recommendation.id == OrderSheetIssuance.recommendation_id)
        .filter(
            assignment.experiment_id == experiment.id,
            assignment.arm == arm,
            OrderSheetIssuance.status == "COMPLETED",
            OrderSheetIssuance.completed_at.is_not(None),
            Recommendation.order_sheet_list_scope == PERSONAL_INITIAL_LIST,
        )
        .order_by(OrderSheetIssuance.completed_at, OrderSheetIssuance.id)
        .limit(experiment.submissions_per_arm)
        .all()
    )
    if len(submissions) != experiment.submissions_per_arm:
        return None
    reaction_window = timedelta(hours=experiment.reaction_window_hours)
    return {
        str(rec_id): (as_utc_naive(submitted_at) + reaction_window).isoformat()
        for rec_id, submitted_at in submissions
    }


def _check_experiment(db: Session, experiment: CommonOrderSheetQuestionExperiment, now: datetime) -> bool:
    cohorts = dict(experiment.cohorts_json)
    for arm in (CONTROL, VARIANT):
        if arm not in cohorts and (cohort := _capture_cohort(db, experiment, arm)) is not None:
            cohorts[arm] = cohort
    changed = cohorts != experiment.cohorts_json
    if changed:
        experiment.cohorts_json = cohorts
    if len(cohorts) != 2:
        return changed

    cutoffs = {
        arm: {int(rec_id): datetime.fromisoformat(end) for rec_id, end in cohort.items()}
        for arm, cohort in cohorts.items()
    }
    if any(end > now for deadlines in cutoffs.values() for end in deadlines.values()):
        return changed
    metrics = {
        arm: ArmMetrics.from_summary(summarize_first_list_reactions(
            db, recommendation_ids=deadlines, reaction_cutoffs=deadlines, for_update=True,
        ))
        for arm, deadlines in cutoffs.items()
    }
    adopted = decide_common_experiment(
        metrics[CONTROL], metrics[VARIANT], submissions_per_arm=experiment.submissions_per_arm,
    )
    experiment.decision_metrics_json = {arm: asdict(metric) for arm, metric in metrics.items()}
    experiment.adopted = adopted is True
    experiment.completed_at = now
    experiment.active = None
    return True


def run_common_question_experiments(db: Session, *, now: datetime | None = None) -> bool:
    """Advance the single shared trial or start the next eligible candidate."""
    decision_time = as_utc_naive(now or utc_now())
    with db.begin():
        experiment = (
            db.query(CommonOrderSheetQuestionExperiment)
            .filter_by(active=True)
            .with_for_update()
            .one_or_none()
        )
        if experiment is None:
            return _open_experiment(db, decision_time)
        return _check_experiment(db, experiment, decision_time)

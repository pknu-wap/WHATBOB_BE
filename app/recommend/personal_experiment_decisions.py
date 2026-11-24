"""Evaluate personal score-question experiments off the request path."""

from collections.abc import Callable
from dataclasses import asdict
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.sessions import utc_now
from app.menus.models import (
    OrderSheetIssuance,
    PersonalOrderSheetQuestionAssignment,
    PersonalOrderSheetQuestionExperiment,
    PersonalOrderSheetQuestionOverride,
)
from app.recommend.experiment_metrics import ArmMetrics, decide_personal_experiment
from app.recommend.feedback import summarize_first_list_reactions
from app.recommend.question_experiments import CONTROL, VARIANT
from app.users.models import User


logger = logging.getLogger(__name__)
PERSONAL_EXPERIMENTS_PER_BATCH = 25


def run_personal_experiment_decision_batch(
    session_factory: Callable[[], Session], *, limit: int = PERSONAL_EXPERIMENTS_PER_BATCH,
) -> int:
    """Resolve a bounded batch of experiments with new explicit first-list feedback."""
    if limit <= 0:
        return 0
    experiment = PersonalOrderSheetQuestionExperiment
    with session_factory() as db:
        pending = (
            db.query(experiment.id, experiment.user_id, experiment.decision_revision)
            .filter(experiment.decision_requested_at.is_not(None), experiment.active.is_(True))
            .order_by(experiment.decision_requested_at, experiment.id)
            .limit(limit)
            .all()
        )

    decided = 0
    for experiment_id, user_id, revision in pending:
        try:
            with session_factory() as db:
                metrics = _summarize_experiment(db, experiment_id)
            adopted = decide_personal_experiment(metrics[CONTROL], metrics[VARIANT])
            with session_factory() as db:
                decided += _publish_decision(db, experiment_id, user_id, revision, adopted, metrics)
        except SQLAlchemyError:
            logger.exception("Personal question decision failed for experiment_id=%s", experiment_id)
    return decided


def _summarize_experiment(db: Session, experiment_id: int) -> dict[str, ArmMetrics]:
    assigned_recommendations = (
        db.query(PersonalOrderSheetQuestionAssignment.arm, OrderSheetIssuance.recommendation_id)
        .join(OrderSheetIssuance, OrderSheetIssuance.id == PersonalOrderSheetQuestionAssignment.issuance_id)
        .filter(
            PersonalOrderSheetQuestionAssignment.experiment_id == experiment_id,
            OrderSheetIssuance.status == "COMPLETED",
            OrderSheetIssuance.recommendation_id.is_not(None),
        )
        .all()
    )
    recommendation_ids: dict[str, list[int]] = {CONTROL: [], VARIANT: []}
    for arm, recommendation_id in assigned_recommendations:
        recommendation_ids[arm].append(recommendation_id)

    return {
        arm: ArmMetrics.from_summary(summarize_first_list_reactions(db, recommendation_ids=ids))
        for arm, ids in recommendation_ids.items()
    }


def _publish_decision(
    db: Session,
    experiment_id: int,
    user_id: int,
    revision: int,
    adopted: bool | None,
    metrics: dict[str, ArmMetrics],
) -> bool:
    # Issuance and authenticated feedback take the user lock before writing experiment state.
    if db.query(User.id).filter(User.id == user_id).with_for_update().scalar() is None:
        return False
    experiment = (
        db.query(PersonalOrderSheetQuestionExperiment)
        .filter_by(id=experiment_id, user_id=user_id, active=True, decision_revision=revision)
        .with_for_update()
        .one_or_none()
    )
    if experiment is None or experiment.decision_requested_at is None:
        return False

    experiment.decision_requested_at = None
    if adopted is not None:
        now = utc_now()
        experiment.active = None
        experiment.adopted = adopted
        experiment.completed_at = now
        experiment.decision_metrics_json = {arm: asdict(values) for arm, values in metrics.items()}
        if adopted:
            override = db.get(PersonalOrderSheetQuestionOverride, (user_id, experiment.slot_key))
            if override is None:
                override = PersonalOrderSheetQuestionOverride(user_id=user_id, slot_key=experiment.slot_key)
                db.add(override)
            override.question_id = experiment.variant_question_id
            override.adopted_at = now
    db.commit()
    return adopted is not None

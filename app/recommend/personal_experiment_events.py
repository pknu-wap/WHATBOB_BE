"""Keep pending personal decisions in sync with committed order-sheet events."""

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from app.auth.sessions import utc_now
from app.menus.models import (
    OrderSheetIssuance,
    PersonalOrderSheetQuestionAssignment,
    PersonalOrderSheetQuestionExperiment,
)


def record_personal_experiment_reaction(
    db: Session, recommendation_id: int, *, explicit: bool,
) -> None:
    experiment_id = (
        select(PersonalOrderSheetQuestionAssignment.experiment_id)
        .join(OrderSheetIssuance, OrderSheetIssuance.id == PersonalOrderSheetQuestionAssignment.issuance_id)
        .where(
            OrderSheetIssuance.recommendation_id == recommendation_id,
            OrderSheetIssuance.status == "COMPLETED",
        )
        .limit(1)
    )
    _record_decision_event(db, experiment_id, request_decision=explicit)


def record_personal_experiment_completion(db: Session, issuance_id: int) -> None:
    experiment_id = select(PersonalOrderSheetQuestionAssignment.experiment_id).where(
        PersonalOrderSheetQuestionAssignment.issuance_id == issuance_id,
    )
    _record_decision_event(db, experiment_id, request_decision=False)


def _record_decision_event(db: Session, experiment_id_query: Select, *, request_decision: bool) -> None:
    experiment = PersonalOrderSheetQuestionExperiment
    statement = update(experiment).where(
        experiment.id == experiment_id_query.scalar_subquery(), experiment.active.is_(True),
    )
    values = {experiment.decision_revision: experiment.decision_revision + 1}
    if request_decision:
        values[experiment.decision_requested_at] = func.coalesce(experiment.decision_requested_at, utc_now())
    else:
        statement = statement.where(experiment.decision_requested_at.is_not(None))
    db.execute(statement.values(values).execution_options(synchronize_session=False))

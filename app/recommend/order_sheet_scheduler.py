"""Run bounded order-sheet question work independently of HTTP requests."""

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.recommend.common_question_experiments import run_common_question_experiments
from app.recommend.personal_experiment_decisions import run_personal_experiment_decision_batch
from app.recommend.question_review import run_pending_question_reviews


logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 30


def _run_question_reviews(session_factory: Callable[[], Session]) -> None:
    with session_factory() as db:
        run_pending_question_reviews(db)


def _run_common_question_experiments(session_factory: Callable[[], Session]) -> None:
    with session_factory() as db:
        run_common_question_experiments(db)


async def order_sheet_experiment_loop(session_factory: Callable[[], Session]) -> None:
    jobs = (
        _run_question_reviews,
        run_personal_experiment_decision_batch,
        _run_common_question_experiments,
    )
    while True:
        for run in jobs:
            try:
                await asyncio.to_thread(run, session_factory)
            except Exception:
                logger.exception("Order-sheet background job %s failed", run.__name__)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

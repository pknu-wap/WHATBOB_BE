"""Run bounded order-sheet question work independently of HTTP requests."""

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.recommend.question_review import run_pending_question_reviews


logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 30


def _run_question_reviews(session_factory: Callable[[], Session]) -> None:
    with session_factory() as db:
        run_pending_question_reviews(db)


async def order_sheet_experiment_loop(session_factory: Callable[[], Session]) -> None:
    while True:
        try:
            await asyncio.to_thread(_run_question_reviews, session_factory)
        except Exception:
            logger.exception("Order-sheet question review worker failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
import logging

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.menus.popular import (
    PopularAggregationResult,
    aggregate_popular_menus_for_kst_date,
    get_seoul_tz,
)

logger = logging.getLogger("uvicorn")


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_next_kst_midnight(
    now: datetime,
    *,
    after_kst_date: date | None = None,
) -> tuple[date, float]:
    """현재 시각 기준으로 다음 Asia/Seoul 자정 날짜와 대기 시간(초)을 계산한다."""
    seoul_tz = get_seoul_tz()
    now_aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    now_kst = now_aware.astimezone(seoul_tz)

    next_kst_date = now_kst.date() + timedelta(days=1)
    if after_kst_date is not None and next_kst_date <= after_kst_date:
        next_kst_date = after_kst_date + timedelta(days=1)

    next_midnight_kst = datetime(
        next_kst_date.year,
        next_kst_date.month,
        next_kst_date.day,
        0,
        0,
        0,
        tzinfo=seoul_tz,
    )
    delay_seconds = max(0.0, (next_midnight_kst - now_kst).total_seconds())
    return next_kst_date, delay_seconds


async def run_popular_aggregation_once(
    target_kst_date: date,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    aggregate_fn: Callable[[Session, date], PopularAggregationResult] = aggregate_popular_menus_for_kst_date,
) -> PopularAggregationResult:
    """동기 DB 인기 순위 집계를 워커 스레드에서 실행해 이벤트 루프 차단을 방지한다."""

    def _execute() -> PopularAggregationResult:
        with session_factory() as db:
            return aggregate_fn(db, target_kst_date)

    return await asyncio.to_thread(_execute)


async def popular_ranking_scheduler_loop(
    *,
    now_fn: Callable[[], datetime] = _default_utc_now,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session_factory: Callable[[], Session] = SessionLocal,
    aggregate_fn: Callable[[Session, date], PopularAggregationResult] = aggregate_popular_menus_for_kst_date,
) -> None:
    """매일 Asia/Seoul 자정까지 대기한 뒤 전날 인기 순위 집계를 실행한다."""
    last_scheduled_kst_date: date | None = None
    while True:
        target_kst_date, delay_seconds = compute_next_kst_midnight(
            now_fn(),
            after_kst_date=last_scheduled_kst_date,
        )
        await sleep_fn(delay_seconds)
        last_scheduled_kst_date = target_kst_date
        try:
            await run_popular_aggregation_once(
                target_kst_date,
                session_factory=session_factory,
                aggregate_fn=aggregate_fn,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "자정 인기 메뉴 순위 집계 작업에 실패했습니다. (target_kst_date=%s)",
                target_kst_date.isoformat(),
            )

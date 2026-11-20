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
    recover_missed_popular_menu_ranking,
)

logger = logging.getLogger("uvicorn")

DEFAULT_RETRY_DELAY_SECONDS: float = 60.0
DEFAULT_MAX_RETRIES_PER_DAY: int = 3


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _kst_midnight_utc(kst_date: date) -> datetime:
    seoul_tz = get_seoul_tz()
    return datetime(
        kst_date.year,
        kst_date.month,
        kst_date.day,
        0,
        0,
        0,
        tzinfo=seoul_tz,
    ).astimezone(timezone.utc)


def compute_next_kst_midnight(
    now: datetime,
    *,
    after_kst_date: date | None = None,
) -> tuple[date, float]:
    """현재 시각 기준으로 다음 Asia/Seoul 자정 날짜와 대기 시간(초)을 계산한다."""
    seoul_tz = get_seoul_tz()
    now_utc = _as_utc(now)
    now_kst = now_utc.astimezone(seoul_tz)

    next_kst_date = now_kst.date() + timedelta(days=1)
    if after_kst_date is not None and next_kst_date <= after_kst_date:
        next_kst_date = after_kst_date + timedelta(days=1)

    next_midnight_utc = _kst_midnight_utc(next_kst_date)
    delay_seconds = max(0.0, (next_midnight_utc - now_utc).total_seconds())
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


async def run_popular_recovery_once(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    recover_fn: Callable[..., PopularAggregationResult] = recover_missed_popular_menu_ranking,
) -> PopularAggregationResult:
    """서버 시작 시 놓친 자정 인기 순위 집계를 워커 스레드에서 비차단 실행한다."""

    def _execute() -> PopularAggregationResult:
        with session_factory() as db:
            if now is None:
                return recover_fn(db)
            return recover_fn(db, now)

    return await asyncio.to_thread(_execute)


async def _sleep_until_utc(
    target_utc: datetime,
    *,
    initial_delay_seconds: float,
    now_fn: Callable[[], datetime],
    sleep_fn: Callable[[float], Awaitable[None]],
) -> None:
    """조기 기상(early wake)으로 목표 자정 전에 집계가 실행되지 않도록 목표 시각까지 재대기한다."""
    delay_seconds = initial_delay_seconds
    while delay_seconds > 0.0:
        await sleep_fn(delay_seconds)
        delay_seconds = (target_utc - _as_utc(now_fn())).total_seconds()


async def _run_aggregation_with_retries(
    target_kst_date: date,
    *,
    sleep_fn: Callable[[float], Awaitable[None]],
    session_factory: Callable[[], Session],
    aggregate_fn: Callable[[Session, date], PopularAggregationResult],
    retry_delay_seconds: float,
    max_retries_per_day: int,
) -> bool:
    """일시 오류 발생 시 같은 날짜 집계를 최대 max_retries_per_day회까지 재시도한다."""
    max_retries = max(0, max_retries_per_day)
    for attempt in range(max_retries + 1):
        try:
            await run_popular_aggregation_once(
                target_kst_date,
                session_factory=session_factory,
                aggregate_fn=aggregate_fn,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "자정 인기 메뉴 순위 집계 작업에 실패했습니다. (target_kst_date=%s, attempt=%d/%d)",
                target_kst_date.isoformat(),
                attempt + 1,
                max_retries + 1,
            )
            if attempt < max_retries:
                await sleep_fn(max(0.0, retry_delay_seconds))
    return False


async def popular_ranking_scheduler_loop(
    *,
    now_fn: Callable[[], datetime] = _default_utc_now,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session_factory: Callable[[], Session] = SessionLocal,
    aggregate_fn: Callable[[Session, date], PopularAggregationResult] = aggregate_popular_menus_for_kst_date,
    recover_fn: Callable[..., PopularAggregationResult] = recover_missed_popular_menu_ranking,
    run_startup_recovery: bool = False,
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    max_retries_per_day: int = DEFAULT_MAX_RETRIES_PER_DAY,
) -> None:
    """선택적 비차단 시작 복구 후 매일 Asia/Seoul 자정에 전날 인기 순위 집계를 실행한다."""
    last_completed_kst_date: date | None = None

    if run_startup_recovery:
        startup_now_utc = _as_utc(now_fn())
        startup_kst_date = startup_now_utc.astimezone(get_seoul_tz()).date()
        try:
            recovery_result = await run_popular_recovery_once(
                now=startup_now_utc,
                session_factory=session_factory,
                recover_fn=recover_fn,
            )
            last_completed_kst_date = (
                recovery_result.target_kst_date
                if isinstance(recovery_result, PopularAggregationResult)
                else startup_kst_date
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("서버 시작 시 놓친 인기 메뉴 자정 집계 복구에 실패했습니다.")

        post_recovery_kst_date = _as_utc(now_fn()).astimezone(get_seoul_tz()).date()
        reference_kst_date = (
            last_completed_kst_date
            if last_completed_kst_date is not None
            else startup_kst_date
        )
        if post_recovery_kst_date > reference_kst_date:
            succeeded = await _run_aggregation_with_retries(
                post_recovery_kst_date,
                sleep_fn=sleep_fn,
                session_factory=session_factory,
                aggregate_fn=aggregate_fn,
                retry_delay_seconds=retry_delay_seconds,
                max_retries_per_day=max_retries_per_day,
            )
            if succeeded:
                last_completed_kst_date = post_recovery_kst_date

    while True:
        target_kst_date, delay_seconds = compute_next_kst_midnight(
            now_fn(),
            after_kst_date=last_completed_kst_date,
        )
        target_midnight_utc = _kst_midnight_utc(target_kst_date)
        await _sleep_until_utc(
            target_midnight_utc,
            initial_delay_seconds=delay_seconds,
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
        succeeded = await _run_aggregation_with_retries(
            target_kst_date,
            sleep_fn=sleep_fn,
            session_factory=session_factory,
            aggregate_fn=aggregate_fn,
            retry_delay_seconds=retry_delay_seconds,
            max_retries_per_day=max_retries_per_day,
        )
        if succeeded:
            last_completed_kst_date = target_kst_date

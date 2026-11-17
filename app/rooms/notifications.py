"""Single-process durable delivery of immutable room state snapshots.

DB work is short and synchronous; no Session survives a network await or memory
lock wait. The same record lock/context serves immediate delivery and polling.
An observed result stays here until its DB write is confirmed (D12).
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import time
from weakref import WeakKeyDictionary

from sqlalchemy.orm import Session

from app.core.config import settings
from app.rooms.models import Room, RoomMember, RoomNotification

logger = logging.getLogger(__name__)
RETRY_SECONDS = (1, 3, 5, 10, 30)
POLL_SECONDS = 1
MAX_BATCH = 100
MAX_CONCURRENT = 16
MAX_CONTEXTS = 100


def naive(value):
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


class SystemClock:
    def utcnow(self):
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def monotonic(self):
        return time.monotonic()


@dataclass
class Observation:
    started_at: datetime
    phase: str = 'starting'
    first_failed_at: datetime | None = None
    deadline_at: datetime | None = None
    deadline_tick: float | None = None
    next_attempt_at: datetime | None = None
    attempt_count: int = 0
    terminal_reason: str | None = None
    finished_at: datetime | None = None


class NotificationDispatcher:
    def __init__(self, bind, manager, *, clock=None, session_factory=None):
        self.bind = getattr(bind, 'engine', bind)
        self.manager = manager
        self.clock = clock or SystemClock()
        self.session_factory = session_factory or (lambda: Session(self.bind, expire_on_commit=False))
        self._locks = {}
        self._observations = {}
        self._workers = asyncio.Semaphore(MAX_CONCURRENT)

    def _read(self, notification_id):
        with self.session_factory() as db:
            row = db.get(RoomNotification, notification_id)
            if row is not None:
                db.expunge(row)
            return row

    def _eligible(self, row):
        # Fresh room-wide authorization for EVERY attempt. A DB error propagates;
        # it must never turn into the successful "no receivers" result.
        with self.session_factory() as db:
            room = db.get(Room, row.room_id) if row.room_id is not None else None
            now = self.clock.utcnow()
            if room is None or room.status == 'CLOSED':
                return 'ROOM_CLOSED', set()
            if naive(room.expires_at) <= now:
                return 'EXPIRED', set()
            if room.host_disconnected_at is not None and naive(room.host_disconnected_at) <= (
                now - timedelta(seconds=settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS)
            ):
                return 'ROOM_CLOSED', set()
            cutoff = now - timedelta(seconds=settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS)
            ids = {uid for uid, role, disconnected in db.query(
                RoomMember.user_id, RoomMember.role, RoomMember.disconnected_at
            ).filter(RoomMember.room_id == room.id).all()
                if role == 'HOST' or disconnected is None or naive(disconnected) > cutoff}
            return None, ids

    def _confirm_start(self, notification_id, observation):
        with self.session_factory() as db:
            row = (db.query(RoomNotification).filter_by(id=notification_id)
                   .with_for_update().one_or_none())
            if row is None or row.finished_at is not None:
                return False
            if row.started_at is None:
                row.started_at = observation.started_at
                db.commit()
            # A lost commit response is retried via a fresh Session and the same
            # live observation; socket calls happen only after confirmation.
            return True

    def _save(self, notification_id, result):
        with self.session_factory() as db:
            row = (db.query(RoomNotification).filter_by(id=notification_id)
                   .with_for_update().one_or_none())
            if row is None or row.finished_at is not None:
                return  # Closure / previously committed result wins.
            row.attempt_count = result.attempt_count
            if row.first_failed_at is None and result.first_failed_at is not None:
                row.first_failed_at = result.first_failed_at
                row.deadline_at = result.deadline_at
            row.next_attempt_at = result.next_attempt_at
            if result.terminal_reason:
                row.terminal_reason = result.terminal_reason
                row.finished_at = result.finished_at
                row.payload_json = None  # JSON(none_as_null=True): SQL NULL.
                row.next_attempt_at = None
            db.commit()

    def _terminal(self, row, reason):
        return Observation(started_at=naive(row.started_at) or self.clock.utcnow(), phase='result',
                           first_failed_at=naive(row.first_failed_at), deadline_at=naive(row.deadline_at),
                           attempt_count=row.attempt_count, terminal_reason=reason,
                           finished_at=self.clock.utcnow())

    async def _send(self, row, connections, result):
        failed = False

        def fail():
            nonlocal failed
            failed = True
            if result.first_failed_at is None:
                result.first_failed_at = self.clock.utcnow()
                result.deadline_at = result.first_failed_at + timedelta(seconds=60)
                result.deadline_tick = self.clock.monotonic() + 60

        def remaining():
            if result.deadline_at is None:
                return 2.0
            wall = (result.deadline_at - self.clock.utcnow()).total_seconds()
            monotonic = result.deadline_tick - self.clock.monotonic()
            return max(0, min(2.0, wall, monotonic))

        async def send(socket):
            try:
                budget = remaining()
                if budget <= 0:
                    fail()
                    return
                await asyncio.wait_for(self.manager.send_connection(socket, row.payload_json), budget)
            except Exception:
                fail()

        tasks = [asyncio.create_task(send(socket)) for socket in connections]
        try:
            while tasks:
                # Once the first failure is observed, the same overall deadline
                # bounds all outstanding waits as well as per-connection waits.
                timeout = None if result.deadline_at is None else max(0, min(
                    (result.deadline_at - self.clock.utcnow()).total_seconds(),
                    result.deadline_tick - self.clock.monotonic()))
                done, pending = await asyncio.wait(tasks, timeout=timeout,
                                                   return_when=asyncio.FIRST_COMPLETED)
                tasks = list(pending)
                if not done:
                    for task in tasks:
                        task.cancel()
                    # Do not await a transport that refuses cancellation beyond
                    # the room deadline. Consume its eventual exception.
                    for task in tasks:
                        task.add_done_callback(_consume_task)
                    tasks = []
                    failed = True
                    break
            result.attempt_count += 1
            if result.deadline_at is not None and remaining() <= 0:
                result.terminal_reason = 'DEADLINE_EXCEEDED'
            elif not failed:
                result.terminal_reason = 'SUCCESS'
            else:
                delay = RETRY_SECONDS[min(result.attempt_count - 1, len(RETRY_SECONDS) - 1)]
                result.next_attempt_at = self.clock.utcnow() + timedelta(seconds=delay)
            if result.terminal_reason:
                result.finished_at = self.clock.utcnow()
            result.phase = 'result'
        finally:
            for task in tasks:
                task.cancel()
                task.add_done_callback(_consume_task)

    async def dispatch(self, notification_id):
        # Reserve context capacity BEFORE sending. DB failures cannot accumulate
        # unbounded observations. Existing observations always remain repairable.
        if notification_id not in self._observations and len(self._observations) >= MAX_CONTEXTS:
            return
        lock, users = self._locks.setdefault(notification_id, [asyncio.Lock(), 0])
        self._locks[notification_id][1] += 1
        try:
            async with lock, self._workers:
                if notification_id not in self._observations and len(self._observations) >= MAX_CONTEXTS:
                    return
                observation = self._observations.get(notification_id)
                if observation is not None and observation.phase == 'result':
                    await asyncio.to_thread(self._save, notification_id, observation)
                    if observation.terminal_reason:
                        self._observations.pop(notification_id, None)
                    else:
                        observation.phase = 'waiting'
                    return  # Storage recovery never consumes a socket retry.
                row = await asyncio.to_thread(self._read, notification_id)
                if row is None or row.finished_at is not None:
                    self._observations.pop(notification_id, None)
                    return
                now = self.clock.utcnow()
                terminal, eligible_ids = await asyncio.to_thread(self._eligible, row)
                if terminal is None and row.deadline_at is not None and naive(row.deadline_at) <= now:
                    terminal = 'DEADLINE_EXCEEDED'
                if terminal is None and row.started_at is not None and row.deadline_at is None and observation is None:
                    terminal = 'UNKNOWN_AFTER_START'
                if terminal:
                    result = self._terminal(row, terminal)
                    self._observations[notification_id] = result
                    await asyncio.to_thread(self._save, notification_id, result)
                    self._observations.pop(notification_id, None)
                    return
                if row.next_attempt_at is not None and naive(row.next_attempt_at) > now:
                    return
                connections = self.manager.eligible_connections(row.historical_public_id, eligible_ids)
                if not connections:
                    result = self._terminal(row, 'NO_RECEIVERS')
                    self._observations[notification_id] = result
                    await asyncio.to_thread(self._save, notification_id, result)
                    self._observations.pop(notification_id, None)
                    return
                if observation is None:
                    observation = Observation(started_at=naive(row.started_at) or now,
                                              first_failed_at=naive(row.first_failed_at),
                                              deadline_at=naive(row.deadline_at), attempt_count=row.attempt_count)
                    self._observations[notification_id] = observation
                if not await asyncio.to_thread(self._confirm_start, notification_id, observation):
                    self._observations.pop(notification_id, None)
                    return
                if observation.deadline_at is not None and observation.deadline_tick is None:
                    observation.deadline_tick = self.clock.monotonic() + max(0, (
                        observation.deadline_at - self.clock.utcnow()).total_seconds())
                observation.phase = 'sending'
                await self._send(row, connections, observation)
                await asyncio.to_thread(self._save, notification_id, observation)
                if observation.terminal_reason:
                    self._observations.pop(notification_id, None)
                else:
                    observation.phase = 'waiting'
        finally:
            entry = self._locks[notification_id]
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(notification_id, None)

    def _due_ids(self, room_public_id):
        with self.session_factory() as db:
            query = db.query(RoomNotification.id).filter(RoomNotification.finished_at.is_(None))
            if room_public_id is not None:
                query = query.filter(RoomNotification.historical_public_id == room_public_id)
            now = self.clock.utcnow()
            query = query.filter((RoomNotification.next_attempt_at <= now)
                                 | RoomNotification.next_attempt_at.is_(None)
                                 | (RoomNotification.deadline_at <= now))
            ids = [r[0] for r in query.order_by(RoomNotification.id).limit(MAX_BATCH).all()]
        return ids

    async def run_due(self, room_public_id=None):
        ids = await asyncio.to_thread(self._due_ids, room_public_id)
        # Prioritize unsaved observed results even when their scheduled send is
        # in the future. Retained observations are bounded to MAX_CONTEXTS.
        ids = list(dict.fromkeys([*self._observations, *ids]))[:MAX_BATCH]
        results = await asyncio.gather(*(self.dispatch(i) for i in ids), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.error('Room notification processing failed: %s', type(result).__name__)


def _consume_task(task):
    if not task.cancelled():
        task.exception()


_dispatchers = WeakKeyDictionary()


def dispatcher_for(bind):
    from app.rooms.websocket import room_connection_manager
    bind = getattr(bind, 'engine', bind)
    if bind not in _dispatchers:
        _dispatchers[bind] = NotificationDispatcher(bind, room_connection_manager)
    return _dispatchers[bind]


async def dispatch_room(bind, room_public_id=None):
    await dispatcher_for(bind).run_due(room_public_id)


async def notification_loop(bind):
    while True:
        try:
            await dispatch_room(bind)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error('Room notification poll failed')
        await asyncio.sleep(POLL_SECONDS)


def cleanup_summaries_once(bind, *, now=None, session_factory=None):
    session_factory = session_factory or (lambda: Session(bind))
    cutoff = (now or SystemClock().utcnow()) - timedelta(days=3)
    deleted = 0
    for _ in range(10):
        with session_factory() as db:
            ids = [r[0] for r in db.query(RoomNotification.id).filter(
                RoomNotification.finished_at <= cutoff).order_by(
                    RoomNotification.finished_at, RoomNotification.id).limit(100).all()]
            if not ids:
                break
            db.query(RoomNotification).filter(RoomNotification.id.in_(ids),
                RoomNotification.finished_at <= cutoff).delete(synchronize_session=False)
            db.commit()
            deleted += len(ids)
    return deleted


async def summary_cleanup_loop(bind):
    while True:
        try:
            await asyncio.to_thread(cleanup_summaries_once, bind)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error('Room notification summary cleanup failed')
        await asyncio.sleep(3600)

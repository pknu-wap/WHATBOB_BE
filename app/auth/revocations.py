"""Single-process durable Kakao token refresh and revocation worker.

DB work is short and synchronous; no Session survives a network await or memory
lock wait. Per-credential locks serialize token refresh and logout revocation.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import time
from weakref import WeakKeyDictionary

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from app.auth.credentials import (
    decrypt_kakao_token,
    encrypt_kakao_token,
    shred_kakao_credential_tokens,
)
from app.auth.kakao import (
    KakaoPermanentError,
    KakaoTokenExpiredError,
    KakaoTransientError,
    logout_kakao_access_token,
    refresh_kakao_tokens,
)
from app.auth.models import AccountTransition, KakaoSessionCredential

logger = logging.getLogger(__name__)
RETRY_SECONDS = (1, 3, 5, 10, 30)
POLL_SECONDS = 1
DEADLINE_SECONDS = 60
MAX_BATCH = 100
MAX_CONCURRENT = 16
MAX_CONTEXTS = 100


def naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


class SystemClock:
    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class RevocationObservation:
    started_at: datetime
    phase: str = "starting"
    first_failed_at: datetime | None = None
    deadline_at: datetime | None = None
    deadline_tick: float | None = None
    next_attempt_at: datetime | None = None
    attempt_count: int = 0
    terminal_status: str | None = None
    terminal_reason: str | None = None
    finished_at: datetime | None = None


class KakaoRevocationDispatcher:
    def __init__(self, bind, *, clock=None, session_factory=None):
        self.bind = getattr(bind, "engine", bind)
        self.clock = clock or SystemClock()
        self.session_factory = session_factory or (
            lambda: Session(self.bind, expire_on_commit=False)
        )
        self._locks: dict[int, list] = {}
        self._observations: dict[int, RevocationObservation] = {}
        self._workers = asyncio.Semaphore(MAX_CONCURRENT)

    def _read(self, credential_id: int) -> KakaoSessionCredential | None:
        with self.session_factory() as db:
            row = db.get(KakaoSessionCredential, credential_id)
            if row is not None:
                db.expunge(row)
            return row

    def _confirm_start(self, credential_id: int, observation: RevocationObservation) -> bool:
        with self.session_factory() as db:
            row = (
                db.query(KakaoSessionCredential)
                .filter_by(id=credential_id)
                .with_for_update()
                .one_or_none()
            )
            if (
                row is None
                or row.finished_at is not None
                or row.status != "PENDING_REVOCATION"
            ):
                return False
            if row.started_at is None:
                row.started_at = observation.started_at
                row.updated_at = self.clock.utcnow()
                db.commit()
            return True

    def _persist_refreshed_tokens(
        self,
        credential_id: int,
        refreshed: dict,
        now: datetime,
    ) -> None:
        new_access = refreshed.get("access_token")
        new_refresh = refreshed.get("refresh_token")
        exp_in = refreshed.get("expires_in")
        ref_exp_in = refreshed.get("refresh_token_expires_in")

        enc_access = encrypt_kakao_token(new_access)
        enc_refresh = encrypt_kakao_token(new_refresh) if new_refresh else None

        with self.session_factory() as db:
            row = (
                db.query(KakaoSessionCredential)
                .filter_by(id=credential_id)
                .with_for_update()
                .one_or_none()
            )
            if row is None or row.finished_at is not None:
                return
            if enc_access:
                row.encrypted_access_token = enc_access
            if exp_in is not None:
                row.access_token_expires_at = now + timedelta(seconds=int(exp_in))
            # If Kakao returns a rotated refresh_token, replace it; if omitted, preserve existing!
            if enc_refresh is not None:
                row.encrypted_refresh_token = enc_refresh
                if ref_exp_in is not None:
                    row.refresh_token_expires_at = now + timedelta(seconds=int(ref_exp_in))
            elif ref_exp_in is not None:
                row.refresh_token_expires_at = now + timedelta(seconds=int(ref_exp_in))
            row.updated_at = now
            db.commit()

    def _save(self, credential_id: int, result: RevocationObservation) -> None:
        with self.session_factory() as db:
            row = (
                db.query(KakaoSessionCredential)
                .filter_by(id=credential_id)
                .with_for_update()
                .one_or_none()
            )
            if row is None or row.finished_at is not None:
                return
            row.attempt_count = result.attempt_count
            if row.first_failed_at is None and result.first_failed_at is not None:
                row.first_failed_at = result.first_failed_at
                row.deadline_at = result.deadline_at
            row.next_attempt_at = result.next_attempt_at
            row.updated_at = self.clock.utcnow()
            if result.terminal_status:
                shred_kakao_credential_tokens(
                    row,
                    status=result.terminal_status,
                    now=result.finished_at or self.clock.utcnow(),
                    terminal_reason=result.terminal_reason,
                )
            db.commit()

    def _terminal(
        self,
        row: KakaoSessionCredential,
        status: str,
        reason: str,
    ) -> RevocationObservation:
        now = self.clock.utcnow()
        return RevocationObservation(
            started_at=naive(row.started_at) or now,
            phase="result",
            first_failed_at=naive(row.first_failed_at),
            deadline_at=naive(row.deadline_at),
            attempt_count=row.attempt_count,
            terminal_status=status,
            terminal_reason=reason,
            finished_at=now,
        )

    async def _refresh_and_persist(
        self,
        credential_id: int,
        refresh_token: str | None,
        budget: float,
    ) -> str:
        if not refresh_token:
            raise KakaoPermanentError("missing_refresh_token")
        refreshed = await refresh_kakao_tokens(refresh_token, timeout=budget)
        now = self.clock.utcnow()
        await asyncio.to_thread(
            self._persist_refreshed_tokens,
            credential_id,
            refreshed,
            now,
        )
        return refreshed["access_token"]

    async def _revoke_once(
        self,
        row: KakaoSessionCredential,
        result: RevocationObservation,
    ) -> None:
        def mark_transient_failure():
            if result.first_failed_at is None:
                result.first_failed_at = self.clock.utcnow()
                result.deadline_at = result.first_failed_at + timedelta(seconds=DEADLINE_SECONDS)
                result.deadline_tick = self.clock.monotonic() + DEADLINE_SECONDS

        def remaining_budget() -> float:
            if result.deadline_at is None:
                return 2.0
            wall = (result.deadline_at - self.clock.utcnow()).total_seconds()
            mono = (
                (result.deadline_tick - self.clock.monotonic())
                if result.deadline_tick is not None
                else wall
            )
            return max(0.0, min(2.0, wall, mono))

        try:
            access_token = decrypt_kakao_token(row.encrypted_access_token)
            refresh_token = decrypt_kakao_token(row.encrypted_refresh_token)
        except (InvalidToken, ValueError):
            result.attempt_count += 1
            result.terminal_status = "FAILED"
            result.terminal_reason = "INVALID_ENCRYPTED_TOKEN"
            result.finished_at = self.clock.utcnow()
            result.phase = "result"
            return

        now = self.clock.utcnow()
        access_expired = (
            not access_token
            or (
                row.access_token_expires_at is not None
                and naive(row.access_token_expires_at) <= now
            )
        )

        try:
            budget = remaining_budget()
            if result.deadline_at is not None and budget <= 0:
                raise KakaoTransientError("deadline_budget_exhausted")

            refreshed_in_this_attempt = False
            if access_expired:
                access_token = await self._refresh_and_persist(
                    int(row.id),
                    refresh_token,
                    max(0.1, budget),
                )
                refreshed_in_this_attempt = True

            try:
                await logout_kakao_access_token(
                    access_token,
                    timeout=max(0.1, remaining_budget()),
                )
            except KakaoTokenExpiredError:
                if refreshed_in_this_attempt or not refresh_token:
                    raise KakaoPermanentError("unauthorized_after_refresh")
                access_token = await self._refresh_and_persist(
                    int(row.id),
                    refresh_token,
                    max(0.1, remaining_budget()),
                )
                try:
                    await logout_kakao_access_token(
                        access_token,
                        timeout=max(0.1, remaining_budget()),
                    )
                except KakaoTokenExpiredError as exc:
                    raise KakaoPermanentError("unauthorized_after_refresh") from exc

            result.attempt_count += 1
            result.terminal_status = "REVOKED"
            result.terminal_reason = "SUCCESS"
            result.finished_at = self.clock.utcnow()
            result.phase = "result"

        except KakaoPermanentError as exc:
            result.attempt_count += 1
            result.terminal_status = "FAILED"
            result.terminal_reason = f"PERMANENT_ERROR:{exc}"[:64]
            result.finished_at = self.clock.utcnow()
            result.phase = "result"

        except (KakaoTransientError, Exception):
            mark_transient_failure()
            result.attempt_count += 1
            if result.deadline_at is not None and remaining_budget() <= 0:
                result.terminal_status = "FAILED"
                result.terminal_reason = "DEADLINE_EXCEEDED"
                result.finished_at = self.clock.utcnow()
            else:
                delay = RETRY_SECONDS[min(result.attempt_count - 1, len(RETRY_SECONDS) - 1)]
                next_at = self.clock.utcnow() + timedelta(seconds=delay)
                if result.deadline_at is not None and next_at > result.deadline_at:
                    next_at = result.deadline_at
                result.next_attempt_at = next_at
            result.phase = "result"

    async def dispatch(self, credential_id: int) -> None:
        if credential_id not in self._observations and len(self._observations) >= MAX_CONTEXTS:
            return
        lock_entry = self._locks.setdefault(credential_id, [asyncio.Lock(), 0])
        lock_entry[1] += 1
        try:
            async with lock_entry[0], self._workers:
                if credential_id not in self._observations and len(self._observations) >= MAX_CONTEXTS:
                    return
                observation = self._observations.get(credential_id)
                if observation is not None and observation.phase == "result":
                    await asyncio.to_thread(self._save, credential_id, observation)
                    if observation.terminal_status:
                        self._observations.pop(credential_id, None)
                    else:
                        observation.phase = "waiting"
                    return

                row = await asyncio.to_thread(self._read, credential_id)
                if (
                    row is None
                    or row.finished_at is not None
                    or row.status != "PENDING_REVOCATION"
                ):
                    self._observations.pop(credential_id, None)
                    return

                if not row.encrypted_access_token and not row.encrypted_refresh_token:
                    term = self._terminal(row, "SKIPPED_NO_CREDENTIALS", "NO_CREDENTIALS")
                    self._observations[credential_id] = term
                    await asyncio.to_thread(self._save, credential_id, term)
                    self._observations.pop(credential_id, None)
                    return

                now = self.clock.utcnow()
                if row.deadline_at is not None and naive(row.deadline_at) <= now:
                    term = self._terminal(row, "FAILED", "DEADLINE_EXCEEDED")
                    self._observations[credential_id] = term
                    await asyncio.to_thread(self._save, credential_id, term)
                    self._observations.pop(credential_id, None)
                    return

                if row.next_attempt_at is not None and naive(row.next_attempt_at) > now:
                    return

                if observation is None:
                    observation = RevocationObservation(
                        started_at=naive(row.started_at) or now,
                        first_failed_at=naive(row.first_failed_at),
                        deadline_at=naive(row.deadline_at),
                        attempt_count=row.attempt_count,
                    )
                    self._observations[credential_id] = observation

                if not await asyncio.to_thread(self._confirm_start, credential_id, observation):
                    self._observations.pop(credential_id, None)
                    return

                if observation.deadline_at is not None and observation.deadline_tick is None:
                    observation.deadline_tick = self.clock.monotonic() + max(
                        0.0,
                        (observation.deadline_at - self.clock.utcnow()).total_seconds(),
                    )

                observation.phase = "sending"
                await self._revoke_once(row, observation)
                await asyncio.to_thread(self._save, credential_id, observation)
                if observation.terminal_status:
                    self._observations.pop(credential_id, None)
                else:
                    observation.phase = "waiting"
        finally:
            entry = self._locks[credential_id]
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(credential_id, None)

    async def refresh_credential(self, credential_id: int) -> str:
        """동일 자격 증명 잠금 아래에서 카카오 액세스 토큰을 갱신하고 회전된 리프레시 토큰을 반영한다."""
        lock_entry = self._locks.setdefault(credential_id, [asyncio.Lock(), 0])
        lock_entry[1] += 1
        try:
            async with lock_entry[0], self._workers:
                row = await asyncio.to_thread(self._read, credential_id)
                if row is None or row.finished_at is not None:
                    raise KakaoPermanentError("credential_unavailable")
                refresh_token = decrypt_kakao_token(row.encrypted_refresh_token)
                return await self._refresh_and_persist(credential_id, refresh_token, 2.0)
        finally:
            entry = self._locks[credential_id]
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(credential_id, None)

    def _due_ids(self, logout_transition_id: str | None = None) -> list[int]:
        with self.session_factory() as db:
            query = db.query(KakaoSessionCredential.id).filter(
                KakaoSessionCredential.status == "PENDING_REVOCATION",
                KakaoSessionCredential.finished_at.is_(None),
            )
            if logout_transition_id is not None:
                query = query.filter(
                    KakaoSessionCredential.logout_transition_id == logout_transition_id
                )
            now = self.clock.utcnow()
            query = query.filter(
                (KakaoSessionCredential.next_attempt_at <= now)
                | KakaoSessionCredential.next_attempt_at.is_(None)
                | (KakaoSessionCredential.deadline_at <= now)
            )
            return [
                int(r[0])
                for r in query.order_by(KakaoSessionCredential.id).limit(MAX_BATCH).all()
            ]

    async def run_due(self, logout_transition_id: str | None = None) -> None:
        ids = await asyncio.to_thread(self._due_ids, logout_transition_id)
        if logout_transition_id is None:
            ids = list(dict.fromkeys([*self._observations, *ids]))[:MAX_BATCH]
        else:
            ids = list(dict.fromkeys(ids))[:MAX_BATCH]
        results = await asyncio.gather(
            *(self.dispatch(i) for i in ids),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.error(
                    "Kakao revocation processing failed: %s",
                    type(result).__name__,
                )


_dispatchers: WeakKeyDictionary = WeakKeyDictionary()


def revocation_dispatcher_for(bind) -> KakaoRevocationDispatcher:
    bind = getattr(bind, "engine", bind)
    if bind not in _dispatchers:
        _dispatchers[bind] = KakaoRevocationDispatcher(bind)
    return _dispatchers[bind]


async def dispatch_kakao_revocation(
    bind,
    logout_transition_id: str | None = None,
) -> None:
    await revocation_dispatcher_for(bind).run_due(logout_transition_id)


def cleanup_expired_prepared_credentials_once(
    bind,
    *,
    now: datetime | None = None,
    session_factory=None,
) -> int:
    bind = getattr(bind, "engine", bind)
    session_factory = session_factory or (lambda: Session(bind))
    current_now = now or SystemClock().utcnow()
    shredded = 0
    with session_factory() as db:
        rows = (
            db.query(KakaoSessionCredential)
            .join(
                AccountTransition,
                AccountTransition.transition_id == KakaoSessionCredential.transition_id,
            )
            .filter(
                KakaoSessionCredential.status == "PREPARED",
                (AccountTransition.status.in_(("EXPIRED", "CANCELLED")))
                | (AccountTransition.prepare_expires_at <= current_now),
            )
            .with_for_update()
            .limit(MAX_BATCH)
            .all()
        )
        for cred in rows:
            shred_kakao_credential_tokens(
                cred,
                status="EXPIRED",
                now=current_now,
                terminal_reason="PREPARE_EXPIRED",
            )
            shredded += 1
        if shredded:
            db.commit()
    return shredded


async def kakao_revocation_loop(bind) -> None:
    while True:
        try:
            await dispatch_kakao_revocation(bind)
            await asyncio.to_thread(cleanup_expired_prepared_credentials_once, bind)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Kakao revocation poll failed")
        await asyncio.sleep(POLL_SECONDS)

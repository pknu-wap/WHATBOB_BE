"""Bounded retries at room-operation transaction owners, never inside cleanup."""
from functools import wraps
from inspect import signature
from types import SimpleNamespace
from contextlib import contextmanager
from threading import Lock, RLock

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.auth.operations import get_user_session_id

_COMMIT_PHASE = 'room_operation_commit_phase'
_READ_POOL_ATTRIBUTE = '_whatbob_current_read_pool'
_read_pool_lock = RLock()


class _CurrentReadPool:
    def __init__(self, owner: Engine):
        # recreate preserves connect_args, creator, connection events and
        # recycling/ping settings. No owner-pool checkout is involved.
        self.engine = Engine(owner.pool.recreate(), owner.dialect, owner.url,
                             execution_options=owner.get_execution_options())
        # Only one read session may use the independent pool: at most one
        # extra connection per owner Engine, irrespective of owner pool size.
        self.lock = Lock()
        self.retired = False


@event.listens_for(Engine, 'engine_disposed')
def _dispose_current_read_pool(owner):
    with _read_pool_lock:
        reader = getattr(owner, _READ_POOL_ATTRIBUTE, None)
        if reader is not None:
            # Keep creation serialized until the active read and its connection
            # close, so disposal cannot briefly create a second reader pool.
            with reader.lock:
                reader.retired = True
                reader.engine.dispose()
                delattr(owner, _READ_POOL_ATTRIBUTE)


@contextmanager
def current_read_session(db: Session):
    """Short non-locking discovery/profile reads outside an old MySQL snapshot.

    Eligibility and aggregate state still require the owner's current locking
    reads. This never commits or releases the owner's transaction/locks. MySQL
    uses one independent reader connection per owner Engine so a saturated
    owner pool cannot block the Room lock holder's non-locking reads.
    """
    bind = db.get_bind()
    if bind.dialect.name == 'mysql':
        owner = bind.engine
        while True:
            with _read_pool_lock:
                reader = getattr(owner, _READ_POOL_ATTRIBUTE, None)
                if reader is None:
                    reader = _CurrentReadPool(owner)
                    setattr(owner, _READ_POOL_ATTRIBUTE, reader)
            with reader.lock:
                if reader.retired:
                    continue  # owner disposal raced with this checkout
                with Session(bind=reader.engine) as read_db:
                    yield read_db
                break
    else:
        yield db


def is_lock_conflict(exc: OperationalError) -> bool:
    args = getattr(exc.orig, 'args', ())
    return bool(args) and args[0] in (1205, 1213)


def commit_room_transaction(db):
    # A failed or successful commit must never replay an already persisted or
    # outcome-unknown operation. Only pre-commit lock conflicts are retried.
    db.flush()  # flush conflicts still have a known, rollback-able outcome
    db.info[_COMMIT_PHASE] = 'committing'
    db.commit()
    db.info[_COMMIT_PHASE] = 'committed'


def room_transaction(*, http: bool = False):
    def decorate(operation):
        operation_signature = signature(operation)

        @wraps(operation)
        def run(*args, **kwargs):
            bound = operation_signature.bind(*args, **kwargs)
            db = bound.arguments['db']
            # Rollback expires ORM state; retain only immutable credentials and
            # reacquire/verify the actual User and AuthSession on every attempt.
            for name in ('user', 'host'):
                if bound.arguments.get(name) is not None:
                    user = bound.arguments[name]
                    bound.arguments[name] = SimpleNamespace(
                        id=user.id, _auth_session_id=get_user_session_id(user),
                    )
            try:
                for attempt in range(3):
                    db.info.pop(_COMMIT_PHASE, None)
                    try:
                        result = operation(*bound.args, **bound.kwargs)
                        if db.in_transaction():
                            db.rollback()  # release no-op/read-only locks
                        return result
                    except OperationalError as exc:
                        may_retry = is_lock_conflict(exc) and not db.info.get(_COMMIT_PHASE)
                        db.rollback()
                        if not may_retry:
                            raise
                        if attempt < 2:
                            continue
                        if http:
                            raise HTTPException(503, detail={
                                'code': 'ROOM_TEMPORARILY_UNAVAILABLE',
                                'message': '일시적으로 방 요청을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.',
                            }) from None
                        raise
                    except Exception:
                        db.rollback()
                        raise
            finally:
                db.info.pop(_COMMIT_PHASE, None)
        return run
    return decorate

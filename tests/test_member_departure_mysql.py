"""Departure behavior against independent InnoDB REPEATABLE READ sessions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.auth.sessions import authenticate_access_token
from app.core.database import Base, get_db
from app.main import app
from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet
from app.rooms.service import (complete_room_order_sheet, expire_room_member_after_grace,
                               join_room, mark_room_member_online)
from app.users.models import User
from tests.auth_helpers import session_token
from tests.test_issue39_mysql_concurrency import mysql_env


@pytest.fixture
def departure_mysql(mysql_env):
    Base.metadata.create_all(mysql_env['engine'])
    with mysql_env['SessionLocal']() as db:
        users = [User(nickname=f'departure-{i}') for i in range(3)]
        db.add_all(users)
        db.commit()
        tokens = [session_token(db, u) for u in users]
        ids = [u.id for u in users]
    headers = [{'Authorization': f'Bearer {t}'} for t in tokens]
    client = TestClient(app)
    room_id = client.post('/api/rooms', headers=headers[0], json={'max_members': 4}).json()['room_id']
    assert client.post(f'/api/rooms/{room_id}/join', headers=headers[1]).status_code == 201
    return {**mysql_env, 'client': client, 'room_id': room_id, 'ids': ids, 'tokens': tokens, 'headers': headers}


def test_mysql_lazy_departure_ignores_old_auth_snapshot_and_preloaded_state(departure_mysql):
    env = departure_mysql
    c, room_id = env['client'], env['room_id']
    assert c.post(f'/api/rooms/{room_id}/join', headers=env['headers'][2]).status_code == 201
    with env['SessionLocal']() as setup:
        host = setup.query(RoomMember).filter_by(user_id=env['ids'][0]).one()
        setup.add(RoomMemberOrderSheet(room_member_id=host.id, is_completed=False))
        setup.commit()
    with env['SessionLocal']() as stale:
        authenticate_access_token(stale, env['tokens'][0])
        old_room = stale.query(Room).filter_by(public_id=room_id).one()
        old_members = stale.query(RoomMember).filter_by(room_id=old_room.id).all()
        old_sheets = stale.query(RoomMemberOrderSheet).all()
        assert old_sheets[0].is_completed is False
        with env['SessionLocal']() as writer:
            member = writer.query(RoomMember).filter_by(room_id=old_room.id, user_id=env['ids'][1]).one()
            member.disconnected_at = datetime.now(timezone.utc) - timedelta(seconds=61)
            writer.query(RoomMemberOrderSheet).one().is_completed = True
            writer.commit()
        app.dependency_overrides[get_db] = lambda: stale
        detail = c.get(f'/api/rooms/{room_id}', headers=env['headers'][0]).json()
        assert (detail['member_count'], detail['completed_member_count'], detail['all_members_completed']) == (2, 1, False)
        assert {m['user_id'] for m in detail['members']} == {env['ids'][0], env['ids'][2]}


def test_mysql_explicit_and_automatic_departure_do_not_double_remove(departure_mysql):
    env = departure_mysql
    c, room_id = env['client'], env['room_id']
    assert c.post(f'/api/rooms/{room_id}/order-sheet/complete', headers=env['headers'][0]).status_code == 200
    now = datetime.now(timezone.utc)
    with env['SessionLocal']() as db:
        db.query(RoomMember).filter_by(user_id=env['ids'][1]).update({'disconnected_at': now - timedelta(seconds=59)})
        db.commit()
    ready = threading.Barrier(2)

    def explicit():
        ready.wait(timeout=10)
        return c.delete(f'/api/rooms/{room_id}/members/{env["ids"][1]}', headers=env['headers'][1]).status_code

    def automatic():
        with env['SessionLocal']() as db:
            db.query(RoomMember).all()  # pre-existing snapshot and identity map
            ready.wait(timeout=10)
            return expire_room_member_after_grace(db, room_id, env['ids'][1], now=now + timedelta(seconds=2))

    with ThreadPoolExecutor(max_workers=2) as pool:
        explicit_result = pool.submit(explicit)
        auto_result = pool.submit(automatic)
        code, event = explicit_result.result(timeout=20), auto_result.result(timeout=20)
    assert code in (200, 404)
    assert (event is None) == (code == 200)
    detail = c.get(f'/api/rooms/{room_id}', headers=env['headers'][0]).json()
    assert (detail['member_count'], detail['completed_member_count'], detail['all_members_completed']) == (1, 1, True)


def test_mysql_expiry_and_reconnect_use_current_disconnect_state(departure_mysql):
    env = departure_mysql
    now = datetime.now(timezone.utc)
    with env['SessionLocal']() as db:
        db.query(RoomMember).filter_by(user_id=env['ids'][1]).update({'disconnected_at': now - timedelta(seconds=59)})
        db.commit()
    ready = threading.Barrier(2)

    def attempt(reconnect):
        with env['SessionLocal']() as db:
            authenticate_access_token(db, env['tokens'][1])
            db.query(RoomMember).all()
            ready.wait(timeout=10)
            if reconnect:
                return mark_room_member_online(db, env['room_id'], env['ids'][1])
            return expire_room_member_after_grace(db, env['room_id'], env['ids'][1], now=now + timedelta(seconds=2))

    with ThreadPoolExecutor(max_workers=2) as pool:
        reconnect = pool.submit(attempt, True)
        expiry = pool.submit(attempt, False)
        connected, departed = reconnect.result(timeout=20), expiry.result(timeout=20)
    detail = env['client'].get(f'/api/rooms/{env["room_id"]}', headers=env['headers'][0]).json()
    assert detail['member_count'] == (2 if connected else 1)
    assert (departed is None) == connected


def test_mysql_departure_join_and_completion_serialize_with_old_snapshots(departure_mysql):
    env = departure_mysql
    now = datetime.now(timezone.utc)
    with env['SessionLocal']() as db:
        db.query(RoomMember).filter_by(user_id=env['ids'][1]).update({'disconnected_at': now - timedelta(seconds=59)})
        db.commit()
    ready = threading.Barrier(3)

    def attempt(kind):
        with env['SessionLocal']() as db:
            user, _ = authenticate_access_token(db, env['tokens'][2 if kind == 'join' else 0])
            db.execute(select(RoomMember)).all()
            db.execute(select(RoomMemberOrderSheet)).all()
            ready.wait(timeout=10)
            if kind == 'join':
                return join_room(db, env['room_id'], user)
            if kind == 'complete':
                return complete_room_order_sheet(db, env['room_id'], user)
            return expire_room_member_after_grace(db, env['room_id'], env['ids'][1], now=now + timedelta(seconds=2))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(attempt, kind) for kind in ('join', 'complete', 'depart')]
        assert all(f.result(timeout=20) is not None for f in futures)
    detail = env['client'].get(f'/api/rooms/{env["room_id"]}', headers=env['headers'][0]).json()
    assert (detail['member_count'], detail['completed_member_count'], detail['all_members_completed']) == (2, 1, False)


def test_mysql_account_transition_detects_join_committed_after_auth_snapshot(departure_mysql):
    from app.auth.operations import lock_user_and_verify_session
    from app.rooms.service import has_active_or_pending_room_activity

    env = departure_mysql
    with env['SessionLocal']() as stale:
        user, _ = authenticate_access_token(stale, env['tokens'][2])
        assert stale.query(RoomMember).filter_by(user_id=user.id).all() == []
        assert env['client'].post(f'/api/rooms/{env["room_id"]}/join', headers=env['headers'][2]).status_code == 201
        lock_user_and_verify_session(stale, user)
        assert has_active_or_pending_room_activity(stale, [user.id]) is True
        stale.rollback()


def test_mysql_retry_rechecks_authentication_after_full_rollback(departure_mysql):
    from types import SimpleNamespace
    from sqlalchemy import event, update
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session
    from app.auth.models import AuthSession

    env = departure_mysql
    attempts = []
    revoked = []

    def fail_first_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('DELETE FROM room_members'):
            attempts.append(1)
            if len(attempts) == 1:
                raise OperationalError(statement, parameters, SimpleNamespace(args=(1213, 'injected')))

    def revoke_after_rollback(session, transaction):
        if attempts and not revoked:
            revoked.append(1)
            # Independent commit after the failed operation released its locks.
            with env['engine'].begin() as conn:
                conn.execute(update(AuthSession).where(AuthSession.user_id == env['ids'][1]).values(
                    revoked_at=datetime.now(timezone.utc).replace(tzinfo=None)
                ))

    event.listen(env['engine'], 'before_cursor_execute', fail_first_delete)
    event.listen(Session, 'after_soft_rollback', revoke_after_rollback)
    try:
        response = env['client'].delete(f'/api/rooms/{env["room_id"]}/members/{env["ids"][1]}', headers=env['headers'][1])
    finally:
        event.remove(env['engine'], 'before_cursor_execute', fail_first_delete)
        event.remove(Session, 'after_soft_rollback', revoke_after_rollback)
    assert response.status_code == 401
    assert response.json()['detail']['code'] == 'SESSION_REVOKED'
    assert len(attempts) == 1
    detail = env['client'].get(f'/api/rooms/{env["room_id"]}', headers=env['headers'][0]).json()
    assert detail['member_count'] == 2


def test_mysql_room_detail_includes_user_created_after_auth_snapshot(departure_mysql):
    env = departure_mysql
    with env['SessionLocal']() as stale:
        authenticate_access_token(stale, env['tokens'][0])
        stale.query(User).all()
        with env['SessionLocal']() as writer:
            newcomer = User(nickname='new-after-snapshot')
            writer.add(newcomer)
            writer.commit()
            token = session_token(writer, newcomer)
            newcomer_id = newcomer.id
        response = env['client'].post(f'/api/rooms/{env["room_id"]}/join', headers={'Authorization': f'Bearer {token}'})
        assert response.status_code == 201
        app.dependency_overrides[get_db] = lambda: stale
        detail = env['client'].get(f'/api/rooms/{env["room_id"]}', headers=env['headers'][0]).json()
        assert detail['member_count'] == 3
        assert newcomer_id in {m['user_id'] for m in detail['members']}

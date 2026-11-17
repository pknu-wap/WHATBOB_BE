from datetime import datetime, timedelta, timezone

import pytest

from app.rooms.models import Room, RoomMember
from tests.test_t03_rooms import room_env


def populate_room(env):
    c = env.client
    room_id = c.post('/api/rooms', headers=env.host_headers, json={'max_members': 4}).json()['room_id']
    for headers in (env.m1_headers, env.m2_headers):
        assert c.post(f'/api/rooms/{room_id}/join', headers=headers).status_code == 201
    assert c.post(f'/api/rooms/{room_id}/order-sheet/complete', headers=env.host_headers).status_code == 200
    return room_id


def test_shared_departure_batches_duplicates_and_reports_final_completion(room_env, caplog):
    from app.rooms.departures import lock_room, lock_room_members, remove_locked_members

    room_id = populate_room(room_env)
    db = room_env.db
    room = lock_room(db, room_id)
    members = [m for m in lock_room_members(db, room.id) if m.role == 'MEMBER']
    result = remove_locked_members(db, room, [members[1], members[0], members[1]])
    assert result.user_ids == (room_env.member1.id, room_env.member2.id)
    assert (result.member_count, result.completed_member_count, result.all_members_completed) == (1, 1, True)
    assert 'duplicate' in caplog.text.lower()
    db.commit()
    detail = room_env.client.get(f'/api/rooms/{room_id}', headers=room_env.host_headers).json()
    assert (detail['member_count'], detail['completed_member_count'], detail['all_members_completed']) == (1, 1, True)


def test_lazy_departure_refreshes_preloaded_members(room_env):
    from sqlalchemy import update

    room_id = populate_room(room_env)
    db = room_env.db
    member = db.query(RoomMember).filter_by(user_id=room_env.member1.id).one()
    assert member.disconnected_at is None
    db.execute(update(RoomMember).where(RoomMember.id == member.id).values(
        disconnected_at=datetime.now(timezone.utc) - timedelta(seconds=61)
    ).execution_options(synchronize_session=False))
    # Reproduce a stale identity-map object from an earlier read.
    assert member.disconnected_at is None
    detail = room_env.client.get(f'/api/rooms/{room_id}', headers=room_env.host_headers).json()
    assert detail['member_count'] == 2
    assert {m['user_id'] for m in detail['members']} == {room_env.host.id, room_env.member2.id}


@pytest.mark.parametrize('path', ['scheduled', 'periodic', 'reconnect', 'transition'])
def test_expiration_paths_refresh_state_and_remove_completion(room_env, path):
    from sqlalchemy import update
    from app.rooms.service import (expire_room_member_after_grace, expire_due_room_members,
                                   mark_room_member_online, has_active_or_pending_room_activity)

    room_id = populate_room(room_env)
    assert room_env.client.post(f'/api/rooms/{room_id}/order-sheet/complete', headers=room_env.m1_headers).status_code == 200
    db = room_env.db
    member = db.query(RoomMember).filter_by(user_id=room_env.member1.id).one()
    db.execute(update(RoomMember).where(RoomMember.id == member.id).values(
        disconnected_at=datetime.now(timezone.utc) - timedelta(seconds=61)
    ).execution_options(synchronize_session=False))
    if path == 'scheduled':
        result = expire_room_member_after_grace(db, room_id, room_env.member1.id)
        assert result is not None
        assert (result.member_count, result.completed_member_count) == (2, 1)
    elif path == 'periodic':
        results = expire_due_room_members(db)
        assert [(r.user_id, r.member_count, r.completed_member_count) for r in results] == [(room_env.member1.id, 2, 1)]
    elif path == 'reconnect':
        assert mark_room_member_online(db, room_id, room_env.member1.id) is False
    else:
        assert has_active_or_pending_room_activity(db, [room_env.member1.id]) is False
        db.commit()
    detail = room_env.client.get(f'/api/rooms/{room_id}', headers=room_env.host_headers).json()
    assert (detail['member_count'], detail['completed_member_count']) == (2, 1)


@pytest.mark.parametrize('code', [1205, 1213])
@pytest.mark.parametrize('failures', [1, 3])
def test_http_departure_retries_only_lock_conflicts_and_rolls_back(room_env, code, failures):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace

    room_id = populate_room(room_env)
    c = room_env.client
    assert c.post(f'/api/rooms/{room_id}/order-sheet/complete', headers=room_env.m1_headers).status_code == 200
    attempts = []

    def fail_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('DELETE FROM room_members'):
            attempts.append(statement)
            if len(attempts) <= failures:
                raise OperationalError(statement, parameters, SimpleNamespace(args=(code, 'injected lock conflict')))

    engine = room_env.db.get_bind()
    event.listen(engine, 'before_cursor_execute', fail_delete)
    try:
        response = c.delete(f'/api/rooms/{room_id}/members/{room_env.member1.id}', headers=room_env.m1_headers)
    finally:
        event.remove(engine, 'before_cursor_execute', fail_delete)
    if failures == 1:
        assert response.status_code == 200
        assert len(attempts) == 2
        assert (response.json()['member_count'], response.json()['completed_member_count']) == (2, 1)
    else:
        assert response.status_code == 503
        assert response.json() == {'detail': {
            'code': 'ROOM_TEMPORARILY_UNAVAILABLE',
            'message': '일시적으로 방 요청을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.',
        }}
        assert len(attempts) == 3
        detail = c.get(f'/api/rooms/{room_id}', headers=room_env.host_headers).json()
        assert (detail['member_count'], detail['completed_member_count']) == (3, 2)


def test_periodic_departure_preserves_other_rooms_after_lock_retry_exhaustion(room_env, caplog):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace
    from app.rooms.service import mark_due_room_members_offline

    rooms = [populate_room(room_env) for _ in range(3)]
    db = room_env.db
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=61)
    targets = db.query(RoomMember).filter_by(user_id=room_env.member1.id).order_by(RoomMember.id).all()
    for target in targets:
        target.disconnected_at = cutoff
    db.commit()
    blocked_id = targets[1].id
    failures = []

    def fail_middle_room(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('DELETE FROM room_members') and parameters == (blocked_id,):
            failures.append(1)
            raise OperationalError(statement, parameters, SimpleNamespace(args=(1213, 'injected')))

    engine = db.get_bind()
    event.listen(engine, 'before_cursor_execute', fail_middle_room)
    try:
        results = mark_due_room_members_offline(db)
    finally:
        event.remove(engine, 'before_cursor_execute', fail_middle_room)
    assert [(r.room_id, r.member_count, r.completed_member_count) for r in results] == [(rooms[0], 2, 1), (rooms[2], 2, 1)]
    assert len(failures) == 3
    assert 'failed' in caplog.text.lower()
    # Remove the failed room's expiry only for observation so GET cannot repair it.
    db.query(RoomMember).filter_by(id=blocked_id).update({'disconnected_at': None})
    db.commit()
    details = [room_env.client.get(f'/api/rooms/{r}', headers=room_env.host_headers).json() for r in rooms]
    assert [d['member_count'] for d in details] == [2, 3, 2]


@pytest.mark.parametrize('invalid', ['host', 'foreign'])
def test_shared_departure_rejects_entire_input_before_mutation(room_env, invalid):
    from app.rooms.departures import lock_room, lock_room_members, remove_locked_members

    room_id = populate_room(room_env)
    foreign_id = populate_room(room_env)
    db = room_env.db
    room = lock_room(db, room_id)
    members = lock_room_members(db, room.id)
    good = next(m for m in members if m.user_id == room_env.member1.id)
    bad = next(m for m in members if m.role == 'HOST')
    if invalid == 'foreign':
        other = lock_room(db, foreign_id)
        bad = next(m for m in lock_room_members(db, other.id) if m.role == 'MEMBER')
    with pytest.raises(ValueError):
        remove_locked_members(db, room, [good, bad])
    # Even a caller mistakenly committing after rejection cannot partially delete.
    db.commit()
    detail = room_env.client.get(f'/api/rooms/{room_id}', headers=room_env.host_headers).json()
    assert detail['member_count'] == 3


def test_shared_departure_empty_input_does_not_query_or_change_state(room_env):
    from sqlalchemy import event
    from app.rooms.departures import lock_room, remove_locked_members

    room_id = populate_room(room_env)
    db = room_env.db
    room = lock_room(db, room_id)
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', record)
    try:
        result = remove_locked_members(db, room, [])
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', record)
    assert result.changed is False and result.user_ids == ()
    assert statements == []
    db.rollback()


def test_account_transition_cleanup_rolls_back_with_its_owner(room_env):
    from app.rooms.service import has_active_or_pending_room_activity

    room_id = populate_room(room_env)
    c, db = room_env.client, room_env.db
    assert c.post(f'/api/rooms/{room_id}/order-sheet/complete', headers=room_env.m1_headers).status_code == 200
    member = db.query(RoomMember).filter_by(user_id=room_env.member1.id).one()
    member.disconnected_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    db.commit()
    assert has_active_or_pending_room_activity(db, [room_env.member1.id]) is False
    # A failure later in account transition must undo participation and completion.
    db.rollback()
    restored = db.query(RoomMember).filter_by(user_id=room_env.member1.id).one()
    restored.disconnected_at = None  # disable lazy repair while observing rollback
    db.commit()
    detail = c.get(f'/api/rooms/{room_id}', headers=room_env.host_headers).json()
    assert (detail['member_count'], detail['completed_member_count']) == (3, 2)


@pytest.mark.parametrize('code', [2006, 2013])
def test_general_database_outage_is_not_retried_or_reported_as_lock_conflict(room_env, code):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace

    room_id = populate_room(room_env)
    attempts = []
    def fail_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('DELETE FROM room_members'):
            attempts.append(1)
            raise OperationalError(statement, parameters, SimpleNamespace(args=(code, 'unavailable')))
    engine = room_env.db.get_bind()
    event.listen(engine, 'before_cursor_execute', fail_delete)
    try:
        with pytest.raises(OperationalError):
            room_env.client.delete(f'/api/rooms/{room_id}/members/{room_env.member1.id}', headers=room_env.m1_headers)
    finally:
        event.remove(engine, 'before_cursor_execute', fail_delete)
    assert len(attempts) == 1


def test_actual_websocket_rejects_reconnect_after_committed_grace_expiry(room_env):
    from starlette.websockets import WebSocketDisconnect

    room_id = populate_room(room_env)
    member = room_env.db.query(RoomMember).filter_by(user_id=room_env.member1.id).one()
    member.disconnected_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    room_env.db.commit()
    with room_env.client.websocket_connect(f'/ws/rooms/{room_id}', headers={'Origin': 'http://localhost:8081'}) as ws:
        ws.send_json({'type': 'auth', 'access_token': room_env.m1_token})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4403
    detail = room_env.client.get(f'/api/rooms/{room_id}', headers=room_env.host_headers).json()
    assert detail['member_count'] == 2


def test_failed_departure_never_sends_confirmed_leave_on_actual_websocket(room_env):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace

    room_id = populate_room(room_env)
    member_id = room_env.member1.id
    engine = room_env.db.get_bind()
    def fail_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('DELETE FROM room_members'):
            raise OperationalError(statement, parameters, SimpleNamespace(args=(1213, 'injected')))
    with room_env.client.websocket_connect(f'/ws/rooms/{room_id}', headers={'Origin': 'http://localhost:8081'}) as ws:
        ws.send_json({'type': 'auth', 'access_token': room_env.host_token})
        assert ws.receive_json()['type'] == 'connection.ready'
        event.listen(engine, 'before_cursor_execute', fail_delete)
        try:
            response = room_env.client.delete(f'/api/rooms/{room_id}/members/{member_id}', headers=room_env.m1_headers)
        finally:
            event.remove(engine, 'before_cursor_execute', fail_delete)
        assert response.status_code == 503
        ws.send_json({'type': 'ping'})
        assert ws.receive_json() == {'type': 'pong'}


def test_periodic_global_outage_propagates_after_yielding_committed_results(room_env):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace
    from app.rooms.service import iter_due_room_member_departures

    rooms = [populate_room(room_env) for _ in range(3)]
    db = room_env.db
    targets = db.query(RoomMember).filter_by(user_id=room_env.member1.id).order_by(RoomMember.id).all()
    for target in targets:
        target.disconnected_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    db.commit()
    blocked_id = targets[1].id
    def outage(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('DELETE FROM room_members') and parameters == (blocked_id,):
            raise OperationalError(statement, parameters, SimpleNamespace(args=(2006, 'unavailable')))
    engine = db.get_bind()
    batches = iter_due_room_member_departures(db)
    event.listen(engine, 'before_cursor_execute', outage)
    try:
        first = next(batches)
        assert [(r.room_id, r.member_count) for r in first] == [(rooms[0], 2)]
        with pytest.raises(OperationalError):
            next(batches)
    finally:
        event.remove(engine, 'before_cursor_execute', outage)


def test_commit_failure_is_never_replayed_as_a_lock_retry(room_env):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace

    room_id = populate_room(room_env)
    member_id = room_env.member1.id
    engine = room_env.db.get_bind()
    attempts = []
    def fail_commit(connection):
        attempts.append(1)
        raise OperationalError('COMMIT', {}, SimpleNamespace(args=(1213, 'commit outcome unknown')))
    event.listen(engine, 'commit', fail_commit)
    try:
        with pytest.raises(OperationalError):
            room_env.client.delete(f'/api/rooms/{room_id}/members/{member_id}', headers=room_env.m1_headers)
    finally:
        event.remove(engine, 'commit', fail_commit)
    assert len(attempts) == 1

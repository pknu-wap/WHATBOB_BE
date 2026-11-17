"""Approved REST/WS, clock, transport and persistence boundaries for #45."""
from tests.test_rooms import build_client
import asyncio
from datetime import datetime, timedelta, timezone
import pytest
import time
from sqlalchemy.orm import Session
from app.rooms.models import Room, RoomMember, RoomNotification
from tests.test_t04_t05_profile import profile_env, _make_image_bytes


class Clock:
    def __init__(self):
        self.value = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
        self.ticks = 0.0
        self.real_start = time.monotonic()

    def utcnow(self):
        return self.value

    def monotonic(self):
        return self.ticks + time.monotonic() - self.real_start

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
        self.ticks += seconds


class Socket:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def send_json(self, payload):
        if self.fail:
            raise OSError('controlled transport failure')
        self.messages.append(payload)


def test_partial_failure_retries_original_snapshot_to_all_current_members():
    from app.rooms.notifications import NotificationDispatcher
    from app.rooms.websocket import RoomConnectionManager
    client, db, host = build_client()
    created = client.post('/api/rooms', json={}).json()
    from app.rooms.service import complete_room_order_sheet
    complete_room_order_sheet(db, created['room_id'], host)
    manager = RoomConnectionManager()
    good, bad = Socket(), Socket(fail=True)
    manager.connect(created['room_id'], host.id, good)
    manager.connect(created['room_id'], host.id, bad)
    clock = Clock()
    sender = NotificationDispatcher(db.get_bind(), manager, clock=clock)

    async def scenario():
        await sender.run_due()
        assert len(good.messages) == 1
        clock.advance(0.99)
        await sender.run_due()
        assert len(good.messages) == 1
        bad.fail = False
        clock.advance(0.01)
        await sender.run_due()
        assert good.messages == [good.messages[0], good.messages[0]]
        assert bad.messages == [good.messages[0]]

    asyncio.run(scenario())
    with Session(db.get_bind()) as read:
        row = read.query(RoomNotification).order_by(RoomNotification.id.desc()).first()
        assert row.terminal_reason == 'SUCCESS'
        assert row.payload_json is None
        assert row.attempt_count == 2


def test_command_and_member_query_share_complete_versioned_state():
    client, db, host = build_client()
    created = client.post('/api/rooms', json={}).json()
    state = created['common_state']
    assert state['state_version'] == 1
    assert state['members'][0]['user_id'] == host.id
    detail = client.get('/api/rooms/' + created['room_id']).json()
    assert detail['common_state'] == state
    completed = client.post('/api/rooms/' + created['room_id'] + '/order-sheet/complete').json()
    assert completed['common_state']['state_version'] == 2
    assert completed['common_state']['completed_member_count'] == 1
    replay = client.post('/api/rooms/' + created['room_id'] + '/order-sheet/complete').json()
    assert replay['common_state'] == completed['common_state']


def notification_env():
    from app.rooms.service import complete_room_order_sheet
    from app.rooms.websocket import RoomConnectionManager
    client, db, host = build_client()
    room_id = client.post('/api/rooms', json={}).json()['room_id']
    complete_room_order_sheet(db, room_id, host)
    notification_id = db.query(RoomNotification.id).order_by(RoomNotification.id.desc()).first()[0]
    db.rollback()
    manager = RoomConnectionManager()
    socket = Socket()
    manager.connect(room_id, host.id, socket)
    return client, db, host, room_id, notification_id, manager, socket


class FaultStorage:
    """A controlled commit boundary: fail before or after one actual commit."""
    def __init__(self, bind, fail_at=2, after_commit=False):
        self.bind, self.fail_at, self.after_commit = bind, fail_at, after_commit
        self.commits = 0

    def session(self):
        storage = self
        class FaultSession(Session):
            def commit(self):
                storage.commits += 1
                failing = storage.commits == storage.fail_at
                if failing and not storage.after_commit:
                    raise OSError('controlled persistence failure')
                super().commit()
                if failing:
                    raise OSError('controlled lost commit response')
        return FaultSession(self.bind, expire_on_commit=False)


@pytest.mark.parametrize('after_commit', [False, True])
@pytest.mark.parametrize('transport_failure', [False, True])
def test_live_observed_result_is_repaired_before_any_resend(after_commit, transport_failure):
    from app.rooms.notifications import NotificationDispatcher
    _, db, _, _, ident, manager, socket = notification_env()
    socket.fail = transport_failure
    storage = FaultStorage(db.get_bind(), after_commit=after_commit)
    sender = NotificationDispatcher(db.get_bind(), manager, clock=Clock(), session_factory=storage.session)

    async def scenario():
        with pytest.raises(OSError):
            await sender.dispatch(ident)
        with Session(db.get_bind()) as check:
            original = check.get(RoomNotification, ident)
            assert original.started_at is not None
        await sender.dispatch(ident)  # confirms/repairs the known result only
        assert len(socket.messages) == (0 if transport_failure else 1)
    asyncio.run(scenario())
    with Session(db.get_bind()) as check:
        saved = check.get(RoomNotification, ident)
        assert saved.attempt_count == 1
        if transport_failure:
            assert saved.deadline_at - saved.first_failed_at == timedelta(seconds=60)
            assert saved.next_attempt_at == saved.first_failed_at + timedelta(seconds=1)
        else:
            assert saved.terminal_reason == 'SUCCESS'
            assert saved.payload_json is None


@pytest.mark.parametrize('after_commit', [False, True])
def test_initial_start_must_be_confirmed_before_socket_and_live_resume_is_safe(after_commit):
    from app.rooms.notifications import NotificationDispatcher
    _, db, _, _, ident, manager, socket = notification_env()
    storage = FaultStorage(db.get_bind(), fail_at=1, after_commit=after_commit)
    sender = NotificationDispatcher(db.get_bind(), manager, clock=Clock(), session_factory=storage.session)
    async def scenario():
        with pytest.raises(OSError):
            await sender.dispatch(ident)
        assert not socket.messages
        await sender.dispatch(ident)
        assert len(socket.messages) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('transport_failure', [False, True])
def test_restart_after_unsaved_initial_result_is_unknown_without_resend(transport_failure):
    from app.rooms.notifications import NotificationDispatcher
    _, db, _, _, ident, manager, socket = notification_env()
    socket.fail = transport_failure
    storage = FaultStorage(db.get_bind())
    clock = Clock()
    async def scenario():
        live = NotificationDispatcher(db.get_bind(), manager, clock=clock, session_factory=storage.session)
        with pytest.raises(OSError):
            await live.dispatch(ident)
        restarted = NotificationDispatcher(db.get_bind(), manager, clock=clock)
        await restarted.dispatch(ident)
        assert len(socket.messages) == (0 if transport_failure else 1)
    asyncio.run(scenario())
    with Session(db.get_bind()) as check:
        row = check.get(RoomNotification, ident)
        assert row.terminal_reason == 'UNKNOWN_AFTER_START'
        assert row.first_failed_at is None
        assert row.payload_json is None


def test_saved_failure_deadline_survives_restart_and_cancels_last_half_second_send():
    from app.rooms.notifications import NotificationDispatcher
    _, db, host, room_id, ident, manager, socket = notification_env()
    clock = Clock()
    socket.fail = True
    cancelled = asyncio.Event()
    class HangingSocket(Socket):
        async def send_json(self, payload):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
    hanging = HangingSocket()
    async def scenario():
        await NotificationDispatcher(db.get_bind(), manager, clock=clock).dispatch(ident)
        clock.advance(59.5)
        manager.disconnect(room_id, host.id, socket)
        manager.connect(room_id, host.id, hanging)
        start = time.monotonic()
        restarted = NotificationDispatcher(db.get_bind(), manager, clock=clock)
        await restarted.dispatch(ident)
        assert time.monotonic() - start < 0.8
        await asyncio.wait_for(cancelled.wait(), 0.2)
        clock.advance(1)
        await restarted.dispatch(ident)  # no new call after deadline
    asyncio.run(scenario())
    with Session(db.get_bind()) as check:
        row = check.get(RoomNotification, ident)
        assert row.terminal_reason == 'DEADLINE_EXCEEDED'
        assert row.deadline_at - row.first_failed_at == timedelta(seconds=60)
        assert row.payload_json is None


def test_socket_metadata_never_authorizes_a_removed_or_outside_user():
    from app.rooms.notifications import NotificationDispatcher
    _, db, host, room_id, ident, manager, socket = notification_env()
    outsider = Socket()
    manager.connect(room_id, host.id + 500, outsider)
    asyncio.run(NotificationDispatcher(db.get_bind(), manager, clock=Clock()).dispatch(ident))
    assert len(socket.messages) == 1
    assert outsider.messages == []


def test_closure_wins_over_late_success_and_keeps_original_terminal_time():
    from app.rooms.notifications import NotificationDispatcher
    from app.rooms.service import close_room_by_host
    _, db, host, room_id, ident, manager, socket = notification_env()
    class ClosingSocket(Socket):
        async def send_json(self, payload):
            with Session(db.get_bind()) as closing:
                close_room_by_host(closing, room_id, closing.get(__import__('app.users.models', fromlist=['User']).User, host.id))
            self.messages.append(payload)
    manager.disconnect(room_id, host.id, socket)
    manager.connect(room_id, host.id, ClosingSocket())
    sender = NotificationDispatcher(db.get_bind(), manager, clock=Clock())
    asyncio.run(sender.dispatch(ident))
    with Session(db.get_bind()) as check:
        row = check.get(RoomNotification, ident)
        finished = row.finished_at
        assert row.terminal_reason == 'ROOM_CLOSED'
        assert row.payload_json is None
    asyncio.run(sender.dispatch(ident))
    with Session(db.get_bind()) as check:
        assert check.get(RoomNotification, ident).finished_at == finished


def test_authenticated_profile_changes_are_blocked_during_participation_and_grace(profile_env):
    env = profile_env
    client = env['client']
    headers = {'Authorization': 'Bearer ' + env['t1']}
    room_id = client.post('/api/rooms', json={}, headers=headers).json()['room_id']
    for disconnected in (False, True):
        if disconnected:
            from app.rooms.service import mark_room_member_disconnected
            with env['SessionLocal']() as db:
                mark_room_member_disconnected(db, room_id, env['u1_id'])
        nickname = client.patch('/api/users/me/nickname', headers=headers, json={'nickname': 'ChangedName'})
        replace = client.put('/api/users/me/profile-image', headers=headers,
                             files={'file': ('image.png', _make_image_bytes(), 'image/png')})
        reset = client.delete('/api/users/me/profile-image', headers=headers)
        assert [r.status_code for r in (nickname, replace, reset)] == [409, 409, 409]
        assert not list(env['upload_dir'].iterdir())
    assert client.get('/api/users/me', headers=headers).json()['nickname'] == '기본유저01'


def test_pending_only_request_does_not_block_profile_and_outsider_get_has_no_common_state(profile_env):
    from app.auth.models import RequestOperation, AuthSession
    env = profile_env
    headers = {'Authorization': 'Bearer ' + env['t1']}
    with env['SessionLocal']() as db:
        session_id = db.query(AuthSession.id).filter_by(user_id=env['u1_id']).first()[0]
        db.add(RequestOperation(scope='ROOM_JOIN', operation_key='pending-only', user_id=env['u1_id'],
            auth_session_id=session_id, status='PENDING', payload_hash='x',
            created_at=datetime.now(), updated_at=datetime.now()))
        db.commit()
    assert env['client'].patch('/api/users/me/nickname', headers=headers,
                               json={'nickname': 'AllowedName'}).status_code == 200
    room_id = env['client'].post('/api/rooms', headers={'Authorization': 'Bearer ' + env['t2']}, json={}).json()['room_id']
    outside = env['client'].get('/api/rooms/' + room_id, headers=headers).json()
    assert outside['members'] == []
    assert outside['common_state'] is None


def test_connection_changes_version_once_and_return_with_only_timestamp_change_is_visible():
    from app.rooms.service import mark_room_member_online, mark_room_member_disconnected, recover_rooms_after_restart
    client, db, host = build_client()
    room_id = client.post('/api/rooms', json={}).json()['room_id']
    mark_room_member_online(db, room_id, host.id)
    assert client.get('/api/rooms/' + room_id).json()['state_version'] == 2
    mark_room_member_online(db, room_id, host.id)
    assert client.get('/api/rooms/' + room_id).json()['state_version'] == 2
    mark_room_member_disconnected(db, room_id, host.id)
    disconnected = client.get('/api/rooms/' + room_id).json()['common_state']
    assert disconnected['state_version'] == 3
    assert disconnected['members'][0]['is_online'] is True
    assert disconnected['members'][0]['disconnected_at'] is not None
    mark_room_member_online(db, room_id, host.id)
    returned = client.get('/api/rooms/' + room_id).json()['common_state']
    assert returned['state_version'] == 4
    assert returned['members'][0]['disconnected_at'] is None
    recover_rooms_after_restart(db)
    assert client.get('/api/rooms/' + room_id).json()['state_version'] == 5
    recover_rooms_after_restart(db)
    assert client.get('/api/rooms/' + room_id).json()['state_version'] == 5


def test_retry_intervals_do_not_extend_first_failure_deadline():
    from app.rooms.notifications import NotificationDispatcher
    _, db, _, _, ident, manager, socket = notification_env()
    socket.fail = True
    clock = Clock()
    sender = NotificationDispatcher(db.get_bind(), manager, clock=clock)
    async def scenario():
        await sender.dispatch(ident)
        first = clock.utcnow()
        for attempt, delay in enumerate([1, 3, 5, 10, 30], start=1):
            with Session(db.get_bind()) as read:
                row = read.get(RoomNotification, ident)
                assert row.next_attempt_at - clock.utcnow() == timedelta(seconds=delay)
                assert row.deadline_at == first + timedelta(seconds=60)
                assert row.attempt_count == attempt
            clock.advance(delay)
            await sender.dispatch(ident)
        clock.advance(11)
        await sender.dispatch(ident)
    asyncio.run(scenario())
    with Session(db.get_bind()) as read:
        assert read.get(RoomNotification, ident).terminal_reason == 'DEADLINE_EXCEEDED'


def test_summary_retention_exact_boundary_and_thousand_row_run_cap():
    from app.rooms.notifications import cleanup_summaries_once
    _, db, _, _, ident, _, _ = notification_env()
    now = datetime(2026, 11, 17)
    cutoff = now - timedelta(days=3)
    with Session(db.get_bind()) as setup:
        for version in range(1102):
            setup.add(RoomNotification(room_id=None, historical_room_id=9000, historical_public_id='retention',
                state_version=version, created_at=cutoff, payload_json=None, terminal_reason='SUCCESS',
                finished_at=cutoff if version < 1101 else cutoff + timedelta(microseconds=1)))
        setup.commit()
    assert cleanup_summaries_once(db.get_bind(), now=now) == 1000
    assert cleanup_summaries_once(db.get_bind(), now=now) == 101
    assert cleanup_summaries_once(db.get_bind(), now=now) == 0
    with Session(db.get_bind()) as read:
        assert read.query(RoomNotification).filter_by(historical_room_id=9000).count() == 1
        assert read.get(RoomNotification, ident).payload_json is not None


def test_restart_grace_commits_each_room_independently():
    from app.rooms.service import recover_rooms_after_restart, mark_room_member_online
    client, db, host = build_client()
    room_ids = [client.post('/api/rooms', json={}).json()['room_id'] for _ in range(2)]
    for room_id in room_ids:
        mark_room_member_online(db, room_id, host.id)
    storage = FaultStorage(db.get_bind(), fail_at=4)
    with storage.session() as restarting:
        with pytest.raises(OSError):
            recover_rooms_after_restart(restarting)
    with Session(db.get_bind()) as check:
        rooms = check.query(Room).order_by(Room.id).all()
        assert [r.state_version for r in rooms] == [3, 2]
        assert [r.host_disconnected_at is not None for r in rooms] == [True, False]


def test_same_record_dispatch_and_poll_share_lock_while_other_room_can_send():
    from app.rooms.notifications import NotificationDispatcher
    from app.rooms.service import create_room
    from app.rooms.schemas import RoomCreateRequest
    _, db, host, room_id, ident, manager, socket = notification_env()
    other = create_room(db, host, RoomCreateRequest())
    other_ident = db.query(RoomNotification.id).order_by(RoomNotification.id.desc()).first()[0]
    db.rollback()
    async def scenario():
        both = asyncio.Event()
        calls = []
        class BarrierSocket(Socket):
            async def send_json(self, payload):
                calls.append(payload['room_id'])
                if len(calls) == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), 1)
        manager.disconnect(room_id, host.id, socket)
        manager.connect(room_id, host.id, BarrierSocket())
        manager.connect(other.room_id, host.id, BarrierSocket())
        sender = NotificationDispatcher(db.get_bind(), manager, clock=Clock())
        await asyncio.gather(sender.dispatch(ident), sender.dispatch(ident), sender.dispatch(other_ident))
        assert sorted(calls) == sorted([room_id, other.room_id])
    asyncio.run(scenario())


def test_actual_websocket_common_state_matches_command_response_and_multi_tab_heartbeat_is_noop():
    from tests.auth_helpers import session_token
    from tests.test_rooms import add_user
    from app.auth.dependencies import get_current_user
    client, db, host = build_client()
    room_id = client.post('/api/rooms', json={}).json()['room_id']
    token = session_token(db, host)
    with client.websocket_connect('/ws/rooms/' + room_id) as first:
        first.send_json({'type': 'auth', 'access_token': token})
        assert first.receive_json()['type'] == 'connection.ready'
        version = client.get('/api/rooms/' + room_id).json()['state_version']
        with client.websocket_connect('/ws/rooms/' + room_id) as second:
            second.send_json({'type': 'auth', 'access_token': token})
            assert second.receive_json()['type'] == 'connection.ready'
            second.send_json({'type': 'ping'})
            assert second.receive_json() == {'type': 'pong'}
        assert client.get('/api/rooms/' + room_id).json()['state_version'] == version
        participant = add_user(db, 777777, 'new-member')
        client.app.dependency_overrides[get_current_user] = lambda: participant
        response = client.post('/api/rooms/' + room_id + '/join').json()
        event = first.receive_json()
        assert event['common_state'] == response['common_state']
        assert event['state_version'] == version + 1


def test_no_current_receivers_terminates_without_treating_stale_metadata_as_membership():
    from app.rooms.notifications import NotificationDispatcher
    _, db, host, room_id, ident, manager, socket = notification_env()
    manager.disconnect(room_id, host.id, socket)
    outsider = Socket()
    manager.connect(room_id, host.id + 100, outsider)
    asyncio.run(NotificationDispatcher(db.get_bind(), manager, clock=Clock()).dispatch(ident))
    assert not outsider.messages
    with Session(db.get_bind()) as read:
        row = read.get(RoomNotification, ident)
        assert row.terminal_reason == 'NO_RECEIVERS'
        assert row.started_at is None
        assert row.payload_json is None


def test_summary_cleanup_failure_keeps_previous_batch_and_resumes():
    from app.rooms.notifications import cleanup_summaries_once
    _, db, _, _, _, _, _ = notification_env()
    now = datetime(2026, 11, 17)
    with Session(db.get_bind()) as setup:
        setup.add_all([RoomNotification(historical_room_id=9000, historical_public_id='retention',
            state_version=v, created_at=now - timedelta(days=4), payload_json=None,
            terminal_reason='SUCCESS', finished_at=now - timedelta(days=4)) for v in range(201)])
        setup.commit()
    storage = FaultStorage(db.get_bind(), fail_at=2)
    with pytest.raises(OSError):
        cleanup_summaries_once(db.get_bind(), now=now, session_factory=storage.session)
    assert cleanup_summaries_once(db.get_bind(), now=now) == 101


def test_success_replay_does_not_expose_common_state_after_participant_leaves():
    from tests.test_rooms import add_user
    from app.auth.dependencies import get_current_user
    from app.rooms.router import operations_router
    client, db, _ = build_client()
    client.app.include_router(operations_router)
    room_id = client.post('/api/rooms', json={}).json()['room_id']
    participant = add_user(db, 777777, 'leaving-member')
    client.app.dependency_overrides[get_current_user] = lambda: participant
    headers = {'Idempotency-Key': 'join-privacy'}
    original = client.post('/api/rooms/' + room_id + '/join', headers=headers)
    assert original.status_code == 201
    assert client.delete(f'/api/rooms/{room_id}/members/{participant.id}').status_code == 200
    replay = client.post('/api/rooms/' + room_id + '/join', headers=headers)
    assert replay.status_code == 201
    assert replay.json()['common_state'] is None
    operation = client.get('/api/room-operations/join-privacy').json()
    assert operation['result']['common_state'] is None


def test_authorization_database_failure_is_not_saved_as_no_receivers():
    from app.rooms.notifications import NotificationDispatcher
    _, db, _, _, ident, manager, socket = notification_env()
    class Unavailable(Session):
        def get(self, entity, *args, **kwargs):
            if entity is Room:
                raise OSError('controlled authorization DB outage')
            return super().get(entity, *args, **kwargs)
    sender = NotificationDispatcher(db.get_bind(), manager, clock=Clock(),
                                    session_factory=lambda: Unavailable(db.get_bind(), expire_on_commit=False))
    with pytest.raises(OSError):
        asyncio.run(sender.dispatch(ident))
    with Session(db.get_bind()) as read:
        row = read.get(RoomNotification, ident)
        assert row.finished_at is None and row.started_at is None and row.payload_json is not None
    assert not socket.messages


def test_restart_immediately_after_start_marker_never_sends_unknown_initial_attempt():
    from app.rooms.notifications import NotificationDispatcher
    _, db, _, _, ident, manager, socket = notification_env()
    storage = FaultStorage(db.get_bind(), fail_at=1, after_commit=True)
    async def scenario():
        with pytest.raises(OSError):
            await NotificationDispatcher(db.get_bind(), manager, clock=Clock(),
                session_factory=storage.session).dispatch(ident)
        await NotificationDispatcher(db.get_bind(), manager, clock=Clock()).dispatch(ident)
    asyncio.run(scenario())
    assert not socket.messages
    with Session(db.get_bind()) as read:
        assert read.get(RoomNotification, ident).terminal_reason == 'UNKNOWN_AFTER_START'


def test_summary_job_runs_at_start_and_waits_one_hour_after_completed_run(monkeypatch):
    from app.rooms.notifications import summary_cleanup_loop
    _, db, _, _, _, _, _ = notification_env()
    with Session(db.get_bind()) as setup:
        setup.add(RoomNotification(historical_room_id=9000, historical_public_id='old', state_version=1,
            created_at=datetime(2020, 1, 1), finished_at=datetime(2020, 1, 1), terminal_reason='SUCCESS'))
        setup.commit()
    async def sleep(seconds):
        assert seconds == 3600
        with Session(db.get_bind()) as read:
            assert read.query(RoomNotification).filter_by(historical_room_id=9000).count() == 0
        raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(summary_cleanup_loop(db.get_bind()))


def test_lazy_departure_plus_join_produces_only_one_final_version():
    from tests.test_rooms import add_user
    from app.auth.dependencies import get_current_user
    client, db, host = build_client()
    room_id = client.post('/api/rooms', json={}).json()['room_id']
    old = add_user(db, 700001, 'old-member')
    newcomer = add_user(db, 700002, 'new-member')
    client.app.dependency_overrides[get_current_user] = lambda: old
    assert client.post('/api/rooms/' + room_id + '/join').status_code == 201
    db.query(RoomMember).filter_by(user_id=old.id).update({
        'disconnected_at': datetime.now(timezone.utc) - timedelta(seconds=61)})
    db.commit()
    client.app.dependency_overrides[get_current_user] = lambda: newcomer
    response = client.post('/api/rooms/' + room_id + '/join').json()['common_state']
    assert response['state_version'] == 3
    assert [m['user_id'] for m in response['members']] == [host.id, newcomer.id]
    assert response['member_count'] == 2
    assert db.query(RoomNotification).count() == 3

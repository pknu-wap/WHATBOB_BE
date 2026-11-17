"""Isolated MySQL 8 migration, FK and independent-session transaction checks."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.database import Base
from app.rooms.models import Room, RoomMember, RoomNotification
from app.rooms.schemas import RoomCreateRequest
from app.rooms.service import create_room, join_room, close_room_by_host, delete_closed_room
from app.rooms.service import get_room, complete_room_order_sheet
from app.users.models import User
from tests.test_issue39_mysql_concurrency import mysql_env


def test_mysql_migration_check_apply_resume_verify_preserves_legacy_rows(mysql_env, tmp_path):
    from scripts import migrate_room_notifications as migration
    engine = mysql_env['engine']
    Base.metadata.create_all(engine)
    with mysql_env['SessionLocal']() as db:
        host = User(nickname='migration-user')
        db.add(host)
        db.commit()
        response = create_room(db, host, RoomCreateRequest())
        assert response.common_state['state_version'] == 1
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE room_notifications'))
        conn.execute(text('ALTER TABLE rooms DROP COLUMN state_version'))
    manifest_path = tmp_path / 'manifest.json'
    environment = {**os.environ, 'DATABASE_URL': engine.url.render_as_string(hide_password=False)}
    def command(*args):
        return subprocess.run([sys.executable, '-m', 'scripts.migrate_room_notifications', *args,
                               '--manifest', str(manifest_path)], env=environment,
                              capture_output=True, text=True, timeout=30)
    assert command('--check').returncode == 0
    assert command('--apply').returncode != 0
    assert not manifest_path.exists()
    with engine.connect() as conn:
        baseline = migration.create_manifest(conn)
    manifest_path.write_text(json.dumps(baseline), encoding='utf-8')
    # A DDL interruption can leave just the additive column committed.
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE rooms ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0'))
    first = command('--apply', '--maintenance-confirmed')
    assert first.returncode == 0, first.stderr
    assert command('--apply', '--maintenance-confirmed').returncode == 0
    assert command('--verify').returncode == 0
    with engine.connect() as conn:
        migration.verify_preservation(conn, baseline)
        assert conn.execute(text('SELECT state_version FROM rooms')).scalar_one() == 0
        assert inspect(conn).get_foreign_keys('room_notifications')[0]['options']['ondelete'] == 'SET NULL'


def test_mysql_fk_summary_survives_physical_delete_and_rejects_missing_room(mysql_env):
    engine = mysql_env['engine']
    Base.metadata.create_all(engine)
    with mysql_env['SessionLocal']() as db:
        host = User(nickname='fk-user')
        db.add(host)
        db.commit()
        room_id = create_room(db, host, RoomCreateRequest()).room_id
        room = db.query(Room).filter_by(public_id=room_id).one()
        historical_id = room.id
        close_room_by_host(db, room_id, host)
        original = db.query(RoomNotification).one().finished_at
        delete_closed_room(db, room_id)
    with mysql_env['SessionLocal']() as db:
        summary = db.query(RoomNotification).one()
        assert (summary.room_id, summary.historical_room_id, summary.historical_public_id) == (None, historical_id, room_id)
        assert (summary.terminal_reason, summary.finished_at, summary.payload_json) == ('ROOM_CLOSED', original, None)
        assert db.execute(text('SELECT payload_json IS NULL FROM room_notifications')).scalar_one() == 1
        db.add(RoomNotification(room_id=999999, historical_room_id=999999, historical_public_id='missing',
                                state_version=1, payload_json={}, created_at=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.commit()


def test_mysql_concurrent_joins_get_distinct_final_versions_and_rollback_leaves_no_outbox(mysql_env):
    from app.rooms.departures import lock_room, remove_locked_members, lock_room_members
    from app.rooms.state import prepare_room_notifications
    engine = mysql_env['engine']
    Base.metadata.create_all(engine)
    factory = mysql_env['SessionLocal']
    with factory() as db:
        users = [User(nickname=f'version-{n}') for n in range(3)]
        db.add_all(users)
        db.commit()
        ids = [u.id for u in users]
        room_id = create_room(db, users[0], RoomCreateRequest(max_members=3)).room_id
    ready = threading.Barrier(2)
    def participate(uid):
        with factory() as db:
            user = db.get(User, uid)  # establish old RR authentication snapshot
            ready.wait(timeout=10)
            return join_room(db, room_id, user).common_state
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(participate, ids[1:]))
    assert sorted((s['state_version'], s['member_count']) for s in states) == [(2, 2), (3, 3)]
    with factory() as db:
        room = lock_room(db, room_id)
        member = next(m for m in lock_room_members(db, room.id) if m.user_id == ids[1])
        remove_locked_members(db, room, [member])
        prepare_room_notifications(db)
        db.rollback()
    with factory() as db:
        assert db.query(Room).one().state_version == 3
        assert db.query(RoomMember).count() == 3
        rows = db.query(RoomNotification).order_by(RoomNotification.state_version).all()
        assert [r.state_version for r in rows] == [1, 2, 3]
        assert [r.payload_json['common_state']['member_count'] for r in rows] == [1, 2, 3]


def test_mysql_success_responses_queries_and_outbox_share_persisted_timestamp_precision(mysql_env):
    Base.metadata.create_all(mysql_env['engine'])
    with mysql_env['SessionLocal']() as db:
        host, participant = User(nickname='precision-host'), User(nickname='precision-member')
        db.add_all([host, participant])
        db.commit()
        created = create_room(db, host, RoomCreateRequest())
        assert created.common_state == get_room(db, created.room_id, host).common_state
        for response, viewer in [(created, host), (join_room(db, created.room_id, participant), participant)]:
            # The first response was version 1; query its immutable saved payload
            # even after the second command. GET for the latest version follows.
            version = response.common_state['state_version']
            payload = db.query(RoomNotification).filter_by(state_version=version).one().payload_json
            assert response.common_state == payload['common_state']
        detail = get_room(db, created.room_id, participant)
        assert detail.common_state == db.query(RoomNotification).filter_by(state_version=2).one().payload_json['common_state']
        response, _ = complete_room_order_sheet(db, created.room_id, participant)
        assert response.common_state == get_room(db, created.room_id, participant).common_state
        assert response.common_state == db.query(RoomNotification).filter_by(state_version=3).one().payload_json['common_state']


def test_mysql_profile_edit_and_join_serialize_on_actual_participation(mysql_env):
    from fastapi import HTTPException
    from app.users.service import update_my_nickname
    from app.users.schemas import NicknameUpdateRequest
    Base.metadata.create_all(mysql_env['engine'])
    factory = mysql_env['SessionLocal']
    with factory() as db:
        host, member = User(nickname='profile-race-host'), User(nickname='profile-race-member')
        db.add_all([host, member])
        db.commit()
        member_id = member.id
        room_id = create_room(db, host, RoomCreateRequest()).room_id
    ready = threading.Barrier(2)
    def join():
        with factory() as db:
            user = db.get(User, member_id)
            ready.wait(timeout=10)
            return join_room(db, room_id, user).common_state
    def edit():
        with factory() as db:
            user = db.get(User, member_id)
            ready.wait(timeout=10)
            try:
                update_my_nickname(db, user, NicknameUpdateRequest(nickname='RaceEdited'))
                return 200
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        participating, editing = pool.submit(join), pool.submit(edit)
        state, code = participating.result(timeout=20), editing.result(timeout=20)
    assert code in (200, 409)
    displayed = next(m for m in state['members'] if m['user_id'] == member_id)['nickname']
    assert displayed == ('RaceEdited' if code == 200 else 'profile-race-member')
    with factory() as db:
        assert db.get(User, member_id).nickname == displayed

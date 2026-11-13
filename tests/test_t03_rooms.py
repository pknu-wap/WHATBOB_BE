from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.rooms.cleanup import cleanup_due_rooms_once
from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet
from app.rooms.service import (
    mark_host_disconnected,
    mark_room_member_disconnected,
    mark_room_member_online,
    recover_rooms_after_restart,
)
from app.users.models import User
from tests.auth_helpers import session_token


@pytest.fixture
def room_env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    overrides = app.dependency_overrides.copy()
    try:
        with Session(engine) as db:
            host = User(nickname="방장유저", nickname_key="방장유저".encode("utf-8"))
            member1 = User(nickname="참가자일", nickname_key="참가자일".encode("utf-8"))
            member2 = User(nickname="참가자이", nickname_key="참가자이".encode("utf-8"))
            db.add_all([host, member1, member2])
            db.commit()

            app.dependency_overrides[get_db] = lambda: db
            yield SimpleNamespace(
                client=TestClient(app),
                db=db,
                host=host,
                member1=member1,
                member2=member2,
                host_token=session_token(db, host),
                m1_token=session_token(db, member1),
                m2_token=session_token(db, member2),
                host_headers={"Authorization": f"Bearer {session_token(db, host)}"},
                m1_headers={"Authorization": f"Bearer {session_token(db, member1)}"},
                m2_headers={"Authorization": f"Bearer {session_token(db, member2)}"},
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        engine.dispose()


def test_t03_multi_websocket_last_close_starts_grace_and_lazy_deadline_cleans_up(room_env):
    c = room_env.client
    create_res = c.post("/api/rooms", headers=room_env.host_headers, json={"max_members": 2})
    assert create_res.status_code == 201
    room_id = create_res.json()["room_id"]

    join_res = c.post(f"/api/rooms/{room_id}/join", headers=room_env.m1_headers)
    assert join_res.status_code == 201

    # 호스트만 주문서 완료, member1은 미완료 상태 -> all_members_completed == False
    assert c.post(f"/api/rooms/{room_id}/order-sheet/complete", headers=room_env.host_headers).status_code == 200
    detail0 = c.get(f"/api/rooms/{room_id}", headers=room_env.host_headers).json()
    assert detail0["member_count"] == 2
    assert detail0["completed_member_count"] == 1
    assert detail0["all_members_completed"] is False

    # 동일 참가자(member1)의 다중 WebSocket 연결: 첫 번째 연결만 닫혀도 두 번째 연결이 남아 있으면 disconnected_at은 NULL 유지
    ws_headers = {"Origin": "http://localhost:8081"}
    with c.websocket_connect(f"/ws/rooms/{room_id}", headers=ws_headers) as ws1:
        ws1.send_json({"type": "auth", "access_token": room_env.m1_token})
        assert ws1.receive_json()["type"] == "connection.ready"
        with c.websocket_connect(f"/ws/rooms/{room_id}", headers=ws_headers) as ws2:
            ws2.send_json({"type": "auth", "access_token": room_env.m1_token})
            assert ws2.receive_json()["type"] == "connection.ready"
        # ws2가 종료되었지만 ws1이 살아 있으므로 disconnected_at은 여전히 None이어야 함
        m1_row = room_env.db.query(RoomMember).filter_by(user_id=room_env.member1.id).one()
        assert m1_row.is_online is True
        assert m1_row.disconnected_at is None

    # 마지막 연결(ws1)까지 종료되면 disconnected_at이 기록됨
    room_env.db.expire_all()
    m1_row = room_env.db.query(RoomMember).filter_by(user_id=room_env.member1.id).one()
    assert m1_row.disconnected_at is not None

    # 59초 경과 시점: 정원(2명) 가득 참 유지, member2 참여 불가(409)
    now = datetime.now(timezone.utc)
    m1_row.disconnected_at = now - timedelta(seconds=59)
    room_env.db.commit()
    assert c.post(f"/api/rooms/{room_id}/join", headers=room_env.m2_headers).status_code == 409

    # 60초 경과 시점: 백그라운드 태스크가 돌지 않아도 GET /api/rooms/{room_id} 지연 정리로
    # member1이 자동 퇴장되고 호스트 1명 기준 all_members_completed == True로 재계산됨!
    m1_row.disconnected_at = now - timedelta(seconds=60)
    room_env.db.commit()
    detail_after_60s = c.get(f"/api/rooms/{room_id}", headers=room_env.host_headers).json()
    assert detail_after_60s["member_count"] == 1
    assert detail_after_60s["completed_member_count"] == 1
    assert detail_after_60s["all_members_completed"] is True

    # 이제 빈자리가 생겼으므로 member2가 즉시 참여 가능
    join2 = c.post(f"/api/rooms/{room_id}/join", headers=room_env.m2_headers)
    assert join2.status_code == 201
    assert join2.json()["member_count"] == 2


def test_t03_restart_recovery_starts_grace_without_extending_existing_disconnects(room_env):
    c = room_env.client
    r1_id = c.post("/api/rooms", headers=room_env.host_headers, json={"max_members": 4}).json()["room_id"]
    c.post(f"/api/rooms/{r1_id}/join", headers=room_env.m1_headers)
    c.post(f"/api/rooms/{r1_id}/join", headers=room_env.m2_headers)

    now = datetime.now(timezone.utc)
    # host는 온라인 상태였고(disconnected_at=None), member1은 30초 전에 끊겼고, member2는 61초 전에 끊긴 상태
    mark_room_member_online(room_env.db, r1_id, room_env.host.id)
    mark_room_member_online(room_env.db, r1_id, room_env.member1.id)
    t_minus_30 = now - timedelta(seconds=30)
    mark_room_member_disconnected(room_env.db, r1_id, room_env.member1.id, disconnected_at=t_minus_30)
    mark_room_member_online(room_env.db, r1_id, room_env.member2.id)
    mark_room_member_disconnected(room_env.db, r1_id, room_env.member2.id, disconnected_at=now - timedelta(seconds=61))

    summary = recover_rooms_after_restart(room_env.db, now=now)
    assert summary["expired_members"] == 1
    assert summary["grace_started_members"] == 1

    room_env.db.expire_all()
    room_row = room_env.db.query(Room).filter_by(public_id=r1_id).one()
    host_member = room_env.db.query(RoomMember).filter_by(room_id=room_row.id, user_id=room_env.host.id).one()
    m1_member = room_env.db.query(RoomMember).filter_by(room_id=room_row.id, user_id=room_env.member1.id).one()
    m2_member = room_env.db.query(RoomMember).filter_by(room_id=room_row.id, user_id=room_env.member2.id).one_or_none()

    # 61초 지난 member2는 퇴장됨
    assert m2_member is None
    # 온라인이던 host는 재시작 시각(now)부터 60초 유예가 시작됨
    assert host_member.disconnected_at is not None
    assert room_row.host_disconnected_at is not None
    # 이미 30초 전에 끊겼던 member1의 disconnected_at은 재시작 시각으로 연장되지 않고 원래 시각 유지
    assert m1_member.disconnected_at.replace(tzinfo=timezone.utc) == t_minus_30


def test_t03_room_operations_idempotency_and_cancellation_tombstone(room_env):
    c = room_env.client
    # 1) 아직 없는 작업 키 조회 -> UNKNOWN (단순 미참여 조회로 실패 단정하지 않음)
    st0 = c.get("/api/room-operations/op-join-1", headers=room_env.m1_headers)
    assert st0.status_code == 200 and st0.json()["status"] == "UNKNOWN"

    # 2) 참여 요청이 서버에 도착하기 전에 취소(cancellation)가 먼저 기록된 경우 -> tombstone 생성 후 늦은 참여 409 거절
    room_id = c.post(
        "/api/rooms",
        headers={**room_env.host_headers, "Idempotency-Key": "op-create-1"},
        json={"max_members": 4},
    ).json()["room_id"]
    # 같은 키로 방 생성 재시도 -> 새 방을 만들지 않고 동일 room_id 반환
    replay_create = c.post(
        "/api/rooms",
        headers={**room_env.host_headers, "Idempotency-Key": "op-create-1"},
        json={"max_members": 4},
    )
    assert replay_create.status_code == 201 and replay_create.json()["room_id"] == room_id
    # 같은 키에 다른 max_members -> 409
    assert c.post(
        "/api/rooms",
        headers={**room_env.host_headers, "Idempotency-Key": "op-create-1"},
        json={"max_members": 6},
    ).status_code == 409

    cancel_first = c.post("/api/room-operations/op-join-1/cancellation", headers=room_env.m1_headers)
    assert cancel_first.status_code == 200 and cancel_first.json()["status"] == "CANCELLED"

    late_join = c.post(
        f"/api/rooms/{room_id}/join",
        headers={**room_env.m1_headers, "Idempotency-Key": "op-join-1"},
    )
    assert late_join.status_code == 409
    assert room_env.db.query(RoomMember).filter_by(user_id=room_env.member1.id).one_or_none() is None

    # 3) 이미 참여가 성공한 뒤 도착한 취소 요청 -> 방/참여를 삭제하지 않고 SUCCEEDED 상태 그대로 반환
    ok_join = c.post(
        f"/api/rooms/{room_id}/join",
        headers={**room_env.m1_headers, "Idempotency-Key": "op-join-2"},
    )
    assert ok_join.status_code == 201
    # 같은 키 재전송은 중복 참여 409 대신 201과 동일 응답 반환
    replay_join = c.post(
        f"/api/rooms/{room_id}/join",
        headers={**room_env.m1_headers, "Idempotency-Key": "op-join-2"},
    )
    assert replay_join.status_code == 201 and replay_join.json() == ok_join.json()

    cancel_after_success = c.post("/api/room-operations/op-join-2/cancellation", headers=room_env.m1_headers)
    assert cancel_after_success.status_code == 200
    assert cancel_after_success.json()["status"] == "SUCCEEDED"
    assert cancel_after_success.json()["room_id"] == room_id
    assert room_env.db.query(RoomMember).filter_by(user_id=room_env.member1.id).one_or_none() is not None

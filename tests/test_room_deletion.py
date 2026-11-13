from datetime import datetime, timedelta, timezone

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet
from app.rooms.service import (
    ROOM_CLOSE_REASON_EXPIRED,
    ROOM_CLOSE_REASON_HOST_DISCONNECTED,
    clear_host_disconnected,
    close_due_rooms,
    delete_closed_room,
    mark_due_room_members_offline,
    mark_host_disconnected,
    mark_room_member_disconnected,
    mark_room_member_offline_after_grace,
    mark_room_member_online,
)
from tests.test_rooms import add_user, build_client


def test_room_creation_sets_configured_expiration(monkeypatch):
    monkeypatch.setattr(settings, "ROOM_TTL_SECONDS", 3600)
    client, db, _ = build_client()

    body = client.post("/api/rooms", json={"max_members": 4}).json()
    room = db.query(Room).filter(Room.public_id == body["room_id"]).one()

    assert body["expires_at"] is not None
    assert abs((room.expires_at - room.created_at).total_seconds() - 3600) < 1


def test_host_can_delete_room_and_dependent_rows():
    client, db, _ = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 3}).json()["room_id"]
    assert client.post(f"/api/rooms/{room_id}/order-sheet/complete").status_code == 200

    response = client.delete(f"/api/rooms/{room_id}")

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert response.json()["close_reason"] == "HOST_LEFT"
    assert db.query(Room).count() == 0
    assert db.query(RoomMember).count() == 0
    assert db.query(RoomMemberOrderSheet).count() == 0
    assert client.get(f"/api/rooms/{room_id}").status_code == 404


def test_non_host_cannot_delete_room():
    client, db, _ = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 3}).json()["room_id"]
    participant = add_user(db, 222222222, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201

    response = client.delete(f"/api/rooms/{room_id}")

    assert response.status_code == 403
    assert response.json()["detail"] == "Only the room host can delete the room"
    assert db.query(Room).count() == 1


def test_expired_room_is_closed_and_requests_are_blocked():
    client, db, _ = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    room = db.query(Room).filter(Room.public_id == room_id).one()
    room.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    response = client.get(f"/api/rooms/{room_id}")

    assert response.status_code == 410
    assert response.json()["detail"] == "Room has expired"
    db.expire_all()
    stored = db.query(Room).filter(Room.public_id == room_id).one()
    assert stored.status == "CLOSED"
    assert stored.close_reason == ROOM_CLOSE_REASON_EXPIRED


def test_cleanup_closes_and_deletes_expired_room_with_children():
    client, db, _ = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    assert client.post(f"/api/rooms/{room_id}/order-sheet/complete").status_code == 200
    room = db.query(Room).filter(Room.public_id == room_id).one()
    room.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    closed = close_due_rooms(db, now=datetime.now(timezone.utc))

    assert [(item.room_id, item.reason) for item in closed] == [
        (room_id, ROOM_CLOSE_REASON_EXPIRED)
    ]
    assert delete_closed_room(db, room_id) is True
    assert db.query(Room).count() == 0
    assert db.query(RoomMember).count() == 0
    assert db.query(RoomMemberOrderSheet).count() == 0


def test_host_reconnect_cancels_disconnect_cleanup(monkeypatch):
    monkeypatch.setattr(settings, "ROOM_HOST_DISCONNECT_GRACE_SECONDS", 60)
    client, db, host = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    disconnected_at = datetime.now(timezone.utc) - timedelta(seconds=30)

    assert mark_host_disconnected(db, room_id, host.id, disconnected_at) is True
    assert clear_host_disconnected(db, room_id, host.id) is True
    assert close_due_rooms(db, now=datetime.now(timezone.utc) + timedelta(minutes=2)) == []
    assert db.query(Room).filter(Room.public_id == room_id).one().status == "WAITING"


def test_host_reconnect_after_grace_deadline_closes_room(monkeypatch):
    monkeypatch.setattr(settings, "ROOM_HOST_DISCONNECT_GRACE_SECONDS", 60)
    client, db, host = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    disconnected_at = datetime.now(timezone.utc) - timedelta(minutes=2)

    assert mark_host_disconnected(db, room_id, host.id, disconnected_at) is True
    assert clear_host_disconnected(db, room_id, host.id) is False
    room = db.query(Room).filter(Room.public_id == room_id).one()
    assert room.status == "CLOSED"
    assert room.close_reason == ROOM_CLOSE_REASON_HOST_DISCONNECTED


def test_host_disconnect_past_grace_period_is_cleanup_target(monkeypatch):
    monkeypatch.setattr(settings, "ROOM_HOST_DISCONNECT_GRACE_SECONDS", 30)
    client, db, host = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    disconnected_at = datetime.now(timezone.utc) - timedelta(seconds=31)
    assert mark_host_disconnected(db, room_id, host.id, disconnected_at) is True

    closed = close_due_rooms(db, now=datetime.now(timezone.utc))

    assert [(item.room_id, item.reason) for item in closed] == [
        (room_id, ROOM_CLOSE_REASON_HOST_DISCONNECTED)
    ]


def test_all_members_completed_does_not_delete_or_close_room():
    client, db, _ = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]

    response = client.post(f"/api/rooms/{room_id}/order-sheet/complete")

    assert response.status_code == 200
    assert response.json()["all_members_completed"] is True
    room = db.query(Room).filter(Room.public_id == room_id).one()
    assert room.status == "WAITING"


def test_member_can_leave_and_completion_is_recalculated():
    client, db, host = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 3}).json()["room_id"]
    assert client.post(f"/api/rooms/{room_id}/order-sheet/complete").status_code == 200

    participant = add_user(db, 222222222, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201
    assert db.query(RoomMember).count() == 2

    response = client.delete(f"/api/rooms/{room_id}/members/{participant.id}")

    assert response.status_code == 200
    assert response.json() == {
        "room_id": room_id,
        "user_id": participant.id,
        "deleted": True,
        "member_count": 1,
        "completed_member_count": 1,
        "all_members_completed": True,
    }
    assert db.query(RoomMember).count() == 1
    assert db.query(RoomMemberOrderSheet).count() == 1

    client.app.dependency_overrides[get_current_user] = lambda: host
    detail = client.get(f"/api/rooms/{room_id}").json()
    assert detail["member_count"] == 1
    assert detail["all_members_completed"] is True


def test_member_leave_deletes_own_order_sheet():
    client, db, _ = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    participant = add_user(db, 222222222, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201
    assert client.post(f"/api/rooms/{room_id}/order-sheet/complete").status_code == 200

    response = client.delete(f"/api/rooms/{room_id}/members/{participant.id}")

    assert response.status_code == 200
    assert db.query(RoomMemberOrderSheet).count() == 0


def test_member_cannot_leave_as_another_user():
    client, db, host = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    participant = add_user(db, 222222222, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201

    response = client.delete(f"/api/rooms/{room_id}/members/{host.id}")

    assert response.status_code == 403
    assert db.query(RoomMember).count() == 2


def test_host_cannot_leave_through_member_endpoint():
    client, db, host = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]

    response = client.delete(f"/api/rooms/{room_id}/members/{host.id}")

    assert response.status_code == 403
    assert response.json()["detail"] == "The room host must delete the room instead"
    assert db.query(Room).count() == 1
    assert db.query(RoomMember).count() == 1


def test_member_presence_grace_preserves_within_60s_and_removes_after_60s(monkeypatch):
    monkeypatch.setattr(settings, "ROOM_MEMBER_DISCONNECT_GRACE_SECONDS", 60)
    client, db, host = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    assert client.post(f"/api/rooms/{room_id}/order-sheet/complete").status_code == 200
    participant = add_user(db, 222222222, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201
    assert client.post(f"/api/rooms/{room_id}/order-sheet/complete").status_code == 200

    assert mark_room_member_online(db, room_id, participant.id) is True
    now = datetime.now(timezone.utc)
    disconnected_at = now - timedelta(seconds=59)
    assert mark_room_member_disconnected(
        db, room_id, participant.id, disconnected_at
    ) is True
    # 59초 시점에는 유예 기간 내이므로 퇴장되지 않고 주문서와 참여가 유지된다.
    assert mark_room_member_offline_after_grace(
        db, room_id, participant.id, now=now
    ) is False
    assert db.query(RoomMember).count() == 2
    assert db.query(RoomMemberOrderSheet).count() == 2

    # 60초 경과 시점에는 해당 참가자만 자동 퇴장되고 주문서가 삭제되며 남은 인원 기준 통계가 재계산된다.
    left = mark_room_member_offline_after_grace(
        db, room_id, participant.id, now=disconnected_at + timedelta(seconds=60)
    )
    assert left is not False
    assert left.user_id == participant.id
    assert left.member_count == 1
    assert left.completed_member_count == 1
    assert left.all_members_completed is True
    assert db.query(RoomMember).filter(RoomMember.user_id == participant.id).one_or_none() is None
    assert db.query(RoomMember).count() == 1
    assert db.query(RoomMemberOrderSheet).count() == 1
    assert host.id == db.query(RoomMember).one().user_id


def test_member_reconnect_cancels_offline_transition(monkeypatch):
    monkeypatch.setattr(settings, "ROOM_MEMBER_DISCONNECT_GRACE_SECONDS", 60)
    client, db, _ = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    participant = add_user(db, 222222222, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201

    assert mark_room_member_online(db, room_id, participant.id) is True
    assert mark_room_member_disconnected(db, room_id, participant.id) is True
    assert mark_room_member_online(db, room_id, participant.id) is False
    assert mark_room_member_offline_after_grace(
        db,
        room_id,
        participant.id,
        now=datetime.now(timezone.utc) + timedelta(minutes=2),
    ) is False

    member = db.query(RoomMember).filter(RoomMember.user_id == participant.id).one()
    assert member.is_online is True
    assert member.disconnected_at is None


def test_cleanup_marks_overdue_members_offline(monkeypatch):
    monkeypatch.setattr(settings, "ROOM_MEMBER_DISCONNECT_GRACE_SECONDS", 60)
    client, db, _ = build_client()
    room_id = client.post("/api/rooms", json={"max_members": 2}).json()["room_id"]
    participant = add_user(db, 222222222, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201
    assert mark_room_member_online(db, room_id, participant.id) is True
    assert mark_room_member_disconnected(
        db,
        room_id,
        participant.id,
        datetime.now(timezone.utc) - timedelta(seconds=61),
    ) is True

    offline = mark_due_room_members_offline(db, now=datetime.now(timezone.utc))

    assert [(item.room_id, item.user_id) for item in offline] == [
        (room_id, participant.id)
    ]

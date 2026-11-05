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
    mark_host_disconnected,
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
    disconnected_at = datetime.now(timezone.utc) - timedelta(minutes=2)

    assert mark_host_disconnected(db, room_id, host.id, disconnected_at) is True
    assert clear_host_disconnected(db, room_id, host.id) is True
    assert close_due_rooms(db, now=datetime.now(timezone.utc)) == []
    assert db.query(Room).filter(Room.public_id == room_id).one().status == "WAITING"


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

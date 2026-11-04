from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet
from app.rooms.router import router
from app.rooms.websocket import router as websocket_router
from app.users.models import User
from starlette.websockets import WebSocketDisconnect


def build_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = testing_session()
    user = User(kakao_id=123456789, nickname="방장")
    db.add(user)
    db.commit()
    db.refresh(user)

    app = FastAPI()
    app.include_router(router)
    app.include_router(websocket_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), db, user


def test_create_room_and_register_host(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://frontend.example/")
    client, db, user = build_client()

    response = client.post("/api/rooms", json={"max_members": 6})

    assert response.status_code == 201
    body = response.json()
    assert body["host_user_id"] == user.id
    assert body["status"] == "WAITING"
    assert body["max_members"] == 6
    assert body["join_url"] == (
        f"https://frontend.example/rooms/{body['room_id']}/join"
    )

    room = db.query(Room).filter(Room.public_id == body["room_id"]).one()
    member = db.query(RoomMember).filter(RoomMember.room_id == room.id).one()
    assert member.user_id == user.id
    assert member.role == "HOST"


def test_create_room_rejects_invalid_capacity():
    client, _, _ = build_client()

    response = client.post("/api/rooms", json={"max_members": 1})

    assert response.status_code == 422


def test_create_room_requires_login():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post("/api/rooms", json={"max_members": 8})

    assert response.status_code == 401


def add_user(db, kakao_id, nickname):
    user = User(kakao_id=kakao_id, nickname=nickname)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_join_room_registers_authenticated_user_as_member():
    client, db, _ = build_client()
    create_response = client.post("/api/rooms", json={"max_members": 3})
    room_id = create_response.json()["room_id"]
    participant = add_user(db, 987654321, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant

    response = client.post(f"/api/rooms/{room_id}/join")

    assert response.status_code == 201
    body = response.json()
    assert body["room_id"] == room_id
    assert body["user_id"] == participant.id
    assert body["role"] == "MEMBER"
    assert body["room_status"] == "WAITING"
    assert body["member_count"] == 2
    assert body["max_members"] == 3

    room = db.query(Room).filter(Room.public_id == room_id).one()
    member = (
        db.query(RoomMember)
        .filter(
            RoomMember.room_id == room.id,
            RoomMember.user_id == participant.id,
        )
        .one()
    )
    assert member.role == "MEMBER"


def test_join_room_rejects_duplicate_member():
    client, db, _ = build_client()
    create_response = client.post("/api/rooms", json={"max_members": 3})
    room_id = create_response.json()["room_id"]
    participant = add_user(db, 987654321, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant

    first_response = client.post(f"/api/rooms/{room_id}/join")
    second_response = client.post(f"/api/rooms/{room_id}/join")

    assert first_response.status_code == 201
    assert second_response.status_code == 409
    assert second_response.json()["detail"] == "User is already a room member"


def test_join_room_rejects_full_room():
    client, db, _ = build_client()
    create_response = client.post("/api/rooms", json={"max_members": 2})
    room_id = create_response.json()["room_id"]

    first_participant = add_user(db, 987654321, "participant-1")
    client.app.dependency_overrides[get_current_user] = lambda: first_participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201

    second_participant = add_user(db, 192837465, "participant-2")
    client.app.dependency_overrides[get_current_user] = lambda: second_participant
    response = client.post(f"/api/rooms/{room_id}/join")

    assert response.status_code == 409
    assert response.json()["detail"] == "Room is full"


def test_join_room_rejects_unknown_room():
    client, _, _ = build_client()

    response = client.post("/api/rooms/missing-room-id/join")

    assert response.status_code == 404
    assert response.json()["detail"] == "Room not found"


def test_join_room_rejects_non_waiting_room():
    client, db, _ = build_client()
    create_response = client.post("/api/rooms", json={"max_members": 3})
    room_id = create_response.json()["room_id"]
    room = db.query(Room).filter(Room.public_id == room_id).one()
    room.status = "IN_PROGRESS"
    db.commit()

    participant = add_user(db, 987654321, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    response = client.post(f"/api/rooms/{room_id}/join")

    assert response.status_code == 409
    assert response.json()["detail"] == "Room is not accepting members"


def test_join_room_requires_login():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post("/api/rooms/room-id/join")

    assert response.status_code == 401


def test_get_room_returns_details_and_members_to_joined_user():
    client, db, host = build_client()
    create_response = client.post("/api/rooms", json={"max_members": 3})
    room_id = create_response.json()["room_id"]
    participant = add_user(db, 987654321, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201

    response = client.get(f"/api/rooms/{room_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["room_id"] == room_id
    assert body["host_user_id"] == host.id
    assert body["status"] == "WAITING"
    assert body["max_members"] == 3
    assert body["member_count"] == 2
    assert body["is_full"] is False
    assert body["is_member"] is True
    assert body["my_role"] == "MEMBER"
    assert [member["role"] for member in body["members"]] == [
        "HOST",
        "MEMBER",
    ]
    assert body["members"][1]["nickname"] == "participant"
    assert body["completed_member_count"] == 0
    assert body["all_members_completed"] is False
    assert all(
        member["order_sheet_completed"] is False
        for member in body["members"]
    )


def test_get_room_hides_members_from_non_member():
    client, db, _ = build_client()
    create_response = client.post("/api/rooms", json={"max_members": 2})
    room_id = create_response.json()["room_id"]
    non_member = add_user(db, 987654321, "non-member")
    client.app.dependency_overrides[get_current_user] = lambda: non_member

    response = client.get(f"/api/rooms/{room_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["member_count"] == 1
    assert body["is_full"] is False
    assert body["is_member"] is False
    assert body["my_role"] is None
    assert body["members"] == []


def test_get_room_returns_full_state():
    client, db, _ = build_client()
    create_response = client.post("/api/rooms", json={"max_members": 2})
    room_id = create_response.json()["room_id"]
    participant = add_user(db, 987654321, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201

    response = client.get(f"/api/rooms/{room_id}")

    assert response.status_code == 200
    assert response.json()["member_count"] == 2
    assert response.json()["is_full"] is True


def test_get_room_rejects_unknown_room():
    client, _, _ = build_client()

    response = client.get("/api/rooms/missing-room-id")

    assert response.status_code == 404
    assert response.json()["detail"] == "Room not found"


def test_get_room_requires_login():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/rooms/room-id")

    assert response.status_code == 401


def test_complete_order_sheet_updates_room_member_progress():
    client, db, host = build_client()
    room_id = client.post(
        "/api/rooms",
        json={"max_members": 3},
    ).json()["room_id"]
    participant = add_user(db, 987654321, "participant")
    client.app.dependency_overrides[get_current_user] = lambda: participant
    assert client.post(f"/api/rooms/{room_id}/join").status_code == 201

    complete_response = client.post(
        f"/api/rooms/{room_id}/order-sheet/complete"
    )

    assert complete_response.status_code == 200
    body = complete_response.json()
    assert body["user_id"] == participant.id
    assert body["order_sheet_completed"] is True
    assert body["order_sheet_completed_at"] is not None
    assert body["completed_member_count"] == 1
    assert body["total_member_count"] == 2
    assert body["all_members_completed"] is False

    room_response = client.get(f"/api/rooms/{room_id}")
    room_body = room_response.json()
    participant_status = next(
        member
        for member in room_body["members"]
        if member["user_id"] == participant.id
    )
    assert participant_status["order_sheet_completed"] is True
    assert participant_status["order_sheet_completed_at"] is not None
    assert room_body["completed_member_count"] == 1
    assert room_body["all_members_completed"] is False

    client.app.dependency_overrides[get_current_user] = lambda: host
    host_response = client.post(
        f"/api/rooms/{room_id}/order-sheet/complete"
    )
    assert host_response.status_code == 200
    assert host_response.json()["completed_member_count"] == 2
    assert host_response.json()["all_members_completed"] is True


def test_complete_order_sheet_is_idempotent():
    client, db, _ = build_client()
    room_id = client.post(
        "/api/rooms",
        json={"max_members": 2},
    ).json()["room_id"]

    first_response = client.post(
        f"/api/rooms/{room_id}/order-sheet/complete"
    )
    second_response = client.post(
        f"/api/rooms/{room_id}/order-sheet/complete"
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["order_sheet_completed_at"] == (
        second_response.json()["order_sheet_completed_at"]
    )
    assert db.query(RoomMemberOrderSheet).count() == 1


def test_complete_order_sheet_requires_room_membership():
    client, db, _ = build_client()
    room_id = client.post(
        "/api/rooms",
        json={"max_members": 2},
    ).json()["room_id"]
    non_member = add_user(db, 987654321, "non-member")
    client.app.dependency_overrides[get_current_user] = lambda: non_member

    response = client.post(
        f"/api/rooms/{room_id}/order-sheet/complete"
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Only room members can complete an order sheet"
    )


def test_complete_order_sheet_rejects_closed_room():
    client, db, _ = build_client()
    room_id = client.post(
        "/api/rooms",
        json={"max_members": 2},
    ).json()["room_id"]
    room = db.query(Room).filter(Room.public_id == room_id).one()
    room.status = "CLOSED"
    db.commit()

    response = client.post(
        f"/api/rooms/{room_id}/order-sheet/complete"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Room is closed"


def test_complete_order_sheet_rejects_unknown_room():
    client, _, _ = build_client()

    response = client.post(
        "/api/rooms/missing-room-id/order-sheet/complete"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Room not found"


def test_complete_order_sheet_requires_login():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/rooms/room-id/order-sheet/complete"
    )

    assert response.status_code == 401


def test_room_websocket_broadcasts_join_and_completion_events():
    client, db, host = build_client()
    room_id = client.post(
        "/api/rooms",
        json={"max_members": 3},
    ).json()["room_id"]
    host_token = create_access_token({"sub": str(host.id)})

    with client.websocket_connect(f"/ws/rooms/{room_id}") as websocket:
        websocket.send_json(
            {
                "type": "auth",
                "access_token": host_token,
            }
        )
        ready_message = websocket.receive_json()
        assert ready_message == {
            "type": "connection.ready",
            "room_id": room_id,
            "user_id": host.id,
        }

        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}

        participant = add_user(db, 987654321, "participant")
        client.app.dependency_overrides[get_current_user] = lambda: participant
        join_response = client.post(f"/api/rooms/{room_id}/join")
        assert join_response.status_code == 201

        join_event = websocket.receive_json()
        assert join_event["type"] == "room.updated"
        assert join_event["room_id"] == room_id
        assert join_event["reason"] == "MEMBER_JOINED"
        assert join_event["user_id"] == participant.id
        assert join_event["occurred_at"]

        complete_response = client.post(
            f"/api/rooms/{room_id}/order-sheet/complete"
        )
        assert complete_response.status_code == 200

        complete_event = websocket.receive_json()
        assert complete_event["type"] == "room.updated"
        assert complete_event["room_id"] == room_id
        assert complete_event["reason"] == "ORDER_SHEET_COMPLETED"
        assert complete_event["user_id"] == participant.id
        assert complete_event["occurred_at"]


def test_room_websocket_rejects_non_member():
    client, db, _ = build_client()
    room_id = client.post(
        "/api/rooms",
        json={"max_members": 2},
    ).json()["room_id"]
    non_member = add_user(db, 987654321, "non-member")
    non_member_token = create_access_token({"sub": str(non_member.id)})

    with client.websocket_connect(f"/ws/rooms/{room_id}") as websocket:
        websocket.send_json(
            {
                "type": "auth",
                "access_token": non_member_token,
            }
        )
        try:
            websocket.receive_json()
            assert False, "Expected WebSocketDisconnect"
        except WebSocketDisconnect as exc:
            assert exc.code == 4403


def test_room_websocket_rejects_invalid_token():
    client, _, _ = build_client()

    with client.websocket_connect("/ws/rooms/room-id") as websocket:
        websocket.send_json(
            {
                "type": "auth",
                "access_token": "invalid-token",
            }
        )
        try:
            websocket.receive_json()
            assert False, "Expected WebSocketDisconnect"
        except WebSocketDisconnect as exc:
            assert exc.code == 4401

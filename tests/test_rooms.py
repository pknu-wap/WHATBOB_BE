from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import Base, get_db
from app.rooms.models import Room, RoomMember
from app.rooms.router import router
from app.users.models import User


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

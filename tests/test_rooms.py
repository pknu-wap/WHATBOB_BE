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

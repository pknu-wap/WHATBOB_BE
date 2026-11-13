import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import AsyncClient
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import kakao, service
from app.core.database import Base, get_db
from tests.auth_helpers import session_token
from app.core.security import create_access_token, decode_access_token
from app.main import app
from app.users.models import User


def mock_kakao_response(monkeypatch, payload):
    def handle_request(request):
        assert request.url.path == "/v2/user/me"
        assert request.url.params["secure_resource"] == "true"
        assert request.headers["Authorization"] == "Bearer test-kakao-token"
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(kakao.httpx, "AsyncClient", lambda: AsyncClient(
        transport=httpx.MockTransport(handle_request),
    ))


@pytest.mark.parametrize("payload,nickname,image", [
    ({"kakao_account": {"profile": {
        "nickname": "테스트닉네임", "profile_image_url": "https://example.test/profile.jpg",
    }}}, "테스트닉네임", "https://example.test/profile.jpg"),
    ({"properties": {"nickname": "이전형식닉네임", "profile_image": "https://example.test/legacy.jpg"}},
     "이전형식닉네임", "https://example.test/legacy.jpg"),
    ({"kakao_account": {"profile": {
        "nickname": "썸네일닉네임", "thumbnail_image_url": "https://example.test/thumb.jpg",
    }}}, "썸네일닉네임", "https://example.test/thumb.jpg"),
    ({"kakao_account": {"profile": {"nickname": "최신닉네임"}}, "properties": {
        "nickname": "이전닉네임", "profile_image": "https://example.test/legacy.jpg",
    }}, "최신닉네임", "https://example.test/legacy.jpg"),
    ({"kakao_account": {"profile": {"nickname": "최신닉네임", "profile_image_url": "https://example.test/new.jpg"}},
      "properties": {"nickname": "이전닉네임", "profile_image": "https://example.test/old.jpg"}},
     "최신닉네임", "https://example.test/new.jpg"),
    ({"kakao_account": {"profile": None}, "properties": None}, None, None),
    ({"kakao_account": None}, None, None),
    ({}, None, None),
])
def test_kakao_profile_extraction(monkeypatch, payload, nickname, image):
    mock_kakao_response(monkeypatch, {"id": 900001, **payload})
    result = asyncio.run(kakao.get_kakao_user_info("test-kakao-token"))
    assert result["kakao_id"] == 900001
    assert result["nickname"] == nickname
    assert result["profile_image"] == image
    assert result["email"] is None


@pytest.mark.parametrize("identifier", [None, 0, True, "not-an-id"])
def test_missing_kakao_identifier_is_rejected(monkeypatch, identifier):
    mock_kakao_response(monkeypatch, {"id": identifier})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(kakao.get_kakao_user_info("test-kakao-token"))
    assert exc.value.status_code == 502


@pytest.fixture
def profile_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    overrides = app.dependency_overrides.copy()
    try:
        with Session(engine) as db:
            app.dependency_overrides[get_db] = lambda: db
            # 실제 DB에 연결하는 lifespan 없이 앱에 등록된 경로를 검증한다.
            yield TestClient(app), db
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        engine.dispose()


def test_login_and_me_return_same_profile_and_preserve_login_contract(profile_client, monkeypatch):
    client, db = profile_client
    monkeypatch.setattr(service, "get_kakao_access_token", AsyncMock(return_value="test-kakao-token"))
    mock_kakao_response(monkeypatch, {"id": 900001, "kakao_account": {"profile": {
        "nickname": "로그인테스터", "profile_image_url": "https://example.test/profile.jpg",
    }}})
    response = client.post("/api/auth/kakao/login", json={"code": "test-auth-code"})
    assert response.status_code == 200
    login = response.json()
    assert set(login) == {"access_token", "token_type", "expires_in", "user", "is_new_user"}
    assert login["is_new_user"] is True
    assert login["token_type"] == "bearer"
    user = login["user"]
    assert user["nickname"] == "로그인테스터"
    assert user["profile_image"] == "https://example.test/profile.jpg"
    assert user["kakao_id"] == 900001
    assert user["id"] != user["kakao_id"]
    assert decode_access_token(login["access_token"])["sub"] == str(user["id"])
    me = client.get("/api/users/me", headers={
        "Authorization": f'Bearer {login["access_token"]}',
        "Origin": "https://whatbobfe-app.vercel.app",
    })
    assert me.status_code == 200
    assert me.headers["access-control-allow-origin"] == "https://whatbobfe-app.vercel.app"
    assert me.json() == user
    assert db.get(User, user["id"]).nickname == "로그인테스터"
    # 쿼리에 다른 사용자 ID를 넣어도 인증된 본인만 조회한다.
    other = User(kakao_id=900002, nickname="다른사용자")
    db.add(other)
    db.commit()
    me = client.get(f"/api/users/me?user_id={other.id}", headers={
        "Authorization": f'Bearer {login["access_token"]}',
    })
    assert me.json() == user


def test_existing_login_preserves_whatbap_profile(profile_client, monkeypatch):
    client, db = profile_client
    existing = User(kakao_id=900001, nickname="이전닉네임", profile_image="https://example.test/old.jpg")
    db.add(existing)
    db.commit()
    identifier = existing.id
    monkeypatch.setattr(service, "get_kakao_access_token", AsyncMock(return_value="test-kakao-token"))
    mock_kakao_response(monkeypatch, {"id": 900001, "properties": {
        "nickname": "갱신닉네임", "profile_image": "https://example.test/new.jpg",
    }})
    updated = client.post("/api/auth/kakao/login", json={"code": "test-code"}).json()
    assert updated["is_new_user"] is False
    assert updated["user"]["id"] == identifier
    assert updated["user"]["nickname"] == "이전닉네임"
    assert updated["user"]["profile_image"] == "https://example.test/old.jpg"
    mock_kakao_response(monkeypatch, {"id": 900001})
    retained = client.post("/api/auth/kakao/login", json={"code": "test-code"}).json()
    assert retained["user"] == updated["user"]
    assert db.query(User).count() == 1


@pytest.mark.parametrize("token_type", ["missing", "invalid", "expired", "unknown_user", "bad_subject", "missing_subject", "wrong_scheme"])
def test_me_requires_valid_service_login(profile_client, token_type):
    client, db = profile_client
    user = User(kakao_id=900001)
    db.add(user)
    db.commit()
    headers = {}
    if token_type == "invalid":
        headers["Authorization"] = "Bearer invalid-token"
    elif token_type != "missing":
        payload = {"sub": str(user.id)}
        if token_type == "unknown_user":
            payload["sub"] = str(user.id + 1000)
        elif token_type == "bad_subject":
            payload["sub"] = "not-an-integer"
        elif token_type == "missing_subject":
            payload = {}
        expiry = timedelta(seconds=-1) if token_type == "expired" else timedelta(minutes=5)
        token = create_access_token(payload, expires_delta=expiry)
        scheme = "Basic" if token_type == "wrong_scheme" else "Bearer"
        headers["Authorization"] = f"{scheme} {token}"
    response = client.get("/api/users/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "nickname" not in response.json()


def test_me_does_not_fabricate_missing_profile(profile_client):
    client, db = profile_client
    user = User(kakao_id=900001)
    db.add(user)
    db.commit()
    token = session_token(db, user)
    response = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["nickname"] is None
    assert response.json()["profile_image"] is None

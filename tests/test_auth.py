import asyncio
from importlib import import_module
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import kakao
from app.core.config import settings
from app.core.database import get_db

auth_router = import_module("app.auth.router")
FRONTEND_CALLBACK = "https://whatbobfe-app.vercel.app/auth/callback"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "KAKAO_REST_API_KEY", "test-rest-api-key")
    monkeypatch.setattr(settings, "KAKAO_REDIRECT_URI", FRONTEND_CALLBACK)
    app = FastAPI()
    app.include_router(auth_router.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as test_client:
        yield test_client


def test_login_url_preserves_state_and_frontend_callback(client):
    state = "random+state&value=/테스트"
    response = client.get("/api/auth/kakao/login/url", params={"state": state})

    assert response.status_code == 200
    url = urlparse(response.json()["login_url"])
    assert (url.scheme, url.netloc, url.path) == (
        "https", "kauth.kakao.com", "/oauth/authorize",
    )
    assert parse_qs(url.query) == {
        "client_id": ["test-rest-api-key"],
        "redirect_uri": [FRONTEND_CALLBACK],
        "response_type": ["code"],
        "state": [state],
    }


@pytest.mark.parametrize("params", [{}, {"state": ""}])
def test_login_url_rejects_missing_or_empty_state(client, params):
    response = client.get("/api/auth/kakao/login/url", params=params)
    assert response.status_code == 422


def test_frontend_code_is_forwarded_to_login_service(client, monkeypatch):
    login = AsyncMock(return_value={
        "access_token": "test-service-jwt",
        "token_type": "bearer",
        "user": {"id": 1, "kakao_id": 123},
        "is_new_user": True,
    })
    monkeypatch.setattr(auth_router, "process_kakao_login", login)

    response = client.post("/api/auth/kakao/login", json={"code": "test-code"})

    assert response.status_code == 200
    login.assert_awaited_once()
    assert login.await_args.args[0] == "test-code"
    assert response.json()["access_token"] == "test-service-jwt"


def test_token_exchange_uses_same_frontend_callback(monkeypatch):
    monkeypatch.setattr(settings, "KAKAO_REST_API_KEY", "test-rest-api-key")
    monkeypatch.setattr(settings, "KAKAO_REDIRECT_URI", FRONTEND_CALLBACK)
    monkeypatch.setattr(settings, "KAKAO_CLIENT_SECRET", None)

    def handle_request(request):
        assert str(request.url) == kakao.KAKAO_TOKEN_URL
        assert request.method == "POST"
        body = parse_qs(request.content.decode())
        assert body["redirect_uri"] == [FRONTEND_CALLBACK]
        assert body["code"] == ["test-code"]
        return httpx.Response(200, json={"access_token": "test-kakao-token"})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        kakao.httpx,
        "AsyncClient",
        lambda: async_client(transport=httpx.MockTransport(handle_request)),
    )

    token = asyncio.run(kakao.get_kakao_access_token("test-code"))
    assert token == "test-kakao-token"

from datetime import timedelta
import hashlib
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import repository, sessions
from app.auth.models import AuthSession
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.security import create_access_token, decode_access_token
from app.main import app
from app.users.models import User

AUTH = "/api/auth"
WEB_HEADERS = {"Origin": "https://whatbobfe-app.vercel.app", "X-Whatbob-Request": "1"}


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    overrides = app.dependency_overrides.copy()
    with Session(engine) as db:
        app.dependency_overrides[get_db] = lambda: db
        client = TestClient(app, base_url="https://testserver", headers=WEB_HEADERS)
        try:
            yield SimpleNamespace(client=client, db=db, engine=engine)
        finally:
            client.close()
            app.dependency_overrides.clear()
            app.dependency_overrides.update(overrides)
    engine.dispose()


def start_user(api):
    assert api.client.post(f"{AUTH}/preparations").status_code == 200
    response = api.client.post(f"{AUTH}/anonymous-users")
    assert response.status_code == 201, response.text
    return response.json()


def bearer(result):
    return {"Authorization": f'Bearer {result["access_token"]}'}


def test_first_visit_recovery_refresh_and_profile_are_one_user(api):
    response = api.client.post(f"{AUTH}/preparations")
    assert response.json() == {"next_action": "CREATE_USER", "notice": None}
    cookie = response.headers["set-cookie"]
    for attribute in ("HttpOnly", "Secure", "SameSite=none", "Path=/api/auth", "Max-Age=2592300"):
        assert attribute in cookie
    assert "Domain=" not in cookie
    secret = api.client.cookies.get(settings.AUTH_COOKIE_NAME)
    record = api.db.query(AuthSession).one()
    assert record.secret_hash == hashlib.sha256(secret.encode()).digest()
    assert len(record.secret_hash) == 32 and record.user_id is None
    assert api.db.query(User).count() == 0
    repeat = api.client.post(f"{AUTH}/preparations")
    assert "set-cookie" not in repeat.headers
    created = api.client.post(f"{AUTH}/anonymous-users", json={"user_id": 999}).json()
    assert created["is_new_user"] is True and created["expires_in"] == 3600
    assert UUID(created["user"]["uuid"]).version == 4
    assert created["user"]["kakao_id"] is None
    assert 2 <= len(created["user"]["nickname"]) <= 20
    assert api.client.get(created["user"]["profile_image"]).status_code == 200
    claims = decode_access_token(created["access_token"])
    assert set(claims) == {"sub", "sid", "iat", "exp", "iss", "aud"}
    assert claims["exp"] - claims["iat"] == 3600
    assert claims["sid"] == str(record.id)
    assert secret not in str(created)
    # Ignore the first response, then retry with the already stored preparation cookie.
    recovered = api.client.post(f"{AUTH}/anonymous-users")
    assert recovered.status_code == 200 and recovered.json()["is_new_user"] is False
    assert recovered.json()["user"] == created["user"]
    prepared = api.client.post(f"{AUTH}/preparations")
    assert prepared.json()["next_action"] == "ISSUE_TOKEN" and "set-cookie" not in prepared.headers
    refreshed = api.client.post(f"{AUTH}/tokens")
    assert refreshed.status_code == 200 and refreshed.json()["user"] == created["user"]
    assert refreshed.headers["Cache-Control"] == "no-store"
    assert refreshed.headers["Pragma"] == "no-cache"
    assert "Max-Age=2592000" in refreshed.headers["set-cookie"]
    assert api.client.get("/api/users/me?user_id=999", headers=bearer(created)).json() == created["user"]
    assert api.db.query(User).count() == api.db.query(AuthSession).count() == 1


def test_independent_cookies_create_independent_users_and_cookie_is_not_bearer(api):
    first = start_user(api)
    api.client.cookies.clear()
    second = start_user(api)
    assert first["user"]["id"] != second["user"]["id"]
    assert first["user"]["uuid"] != second["user"]["uuid"]
    assert api.client.get("/api/users/me").status_code == 401
    assert api.client.get("/api/users/me", headers=bearer(first)).json() == first["user"]
    assert api.client.get("/api/users/me", headers=bearer(second)).json() == second["user"]


@pytest.mark.parametrize("endpoint", ["preparations", "anonymous-users", "tokens"])
@pytest.mark.parametrize("origin,marker", [(None, "1"), ("null", "1"), ("https://evil.test", "1"),
    (WEB_HEADERS["Origin"], None), (WEB_HEADERS["Origin"], "0")])
def test_csrf_rejection_has_no_side_effects(api, endpoint, origin, marker):
    api.client.headers.clear()
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if marker is not None:
        headers["X-Whatbob-Request"] = marker
    response = api.client.post(f"{AUTH}/{endpoint}", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CSRF_REJECTED"
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert api.db.query(User).count() == api.db.query(AuthSession).count() == 0


def test_preflight_needs_no_secret_or_marker_value(api):
    response = api.client.options(f"{AUTH}/tokens", headers={
        "Origin": WEB_HEADERS["Origin"], "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-Whatbob-Request",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-credentials"] == "true"
    assert api.db.query(AuthSession).count() == 0


@pytest.mark.parametrize("endpoint", ["anonymous-users", "tokens"])
def test_missing_cookie_does_not_create_a_user(api, endpoint):
    response = api.client.post(f"{AUTH}/{endpoint}")
    assert response.status_code == 401 and response.json()["detail"]["code"] == "AUTH_COOKIE_MISSING"
    assert "set-cookie" not in response.headers
    assert api.db.query(User).count() == 0


@pytest.mark.parametrize("secret", ["", "abc", "x" * 44, "!" * 43, "A" * 42 + "B"])
@pytest.mark.parametrize("endpoint", ["preparations", "anonymous-users", "tokens"])
def test_malformed_cookie_is_rejected_without_replacement(api, secret, endpoint):
    api.client.cookies.set(settings.AUTH_COOKIE_NAME, secret)
    response = api.client.post(f"{AUTH}/{endpoint}")
    assert response.status_code == 400 and response.json()["detail"]["code"] == "INVALID_AUTH_COOKIE"
    assert "set-cookie" not in response.headers
    assert api.db.query(AuthSession).count() == 0


def test_unready_preparation_does_not_issue_token_or_extend_lifetime(api):
    api.client.post(f"{AUTH}/preparations")
    expires = api.db.query(AuthSession).one().expires_at
    response = api.client.post(f"{AUTH}/tokens")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "AUTHENTICATION_NOT_READY"
    assert api.db.query(User).count() == 0
    assert api.db.query(AuthSession).one().expires_at == expires


@pytest.mark.parametrize("formal", [False, True])
def test_expired_preparation_or_session_never_revives_and_records_remain(api, monkeypatch, formal):
    if formal:
        start_user(api)
    else:
        api.client.post(f"{AUTH}/preparations")
    record = api.db.query(AuthSession).one()
    expiry = record.expires_at
    monkeypatch.setattr(sessions, "utc_now", lambda: expiry)
    expected = "SESSION_EXPIRED" if formal else "PREPARATION_EXPIRED"
    for endpoint in ("tokens", "anonymous-users"):
        response = api.client.post(f"{AUTH}/{endpoint}")
        assert response.status_code == 401 and response.json()["detail"]["code"] == expected
        assert "set-cookie" not in response.headers
    response = api.client.post(f"{AUTH}/preparations")
    assert response.status_code == 200 and response.json()["next_action"] == "CREATE_USER"
    assert response.json()["notice"] == ("PREVIOUS_SESSION_UNAVAILABLE" if formal else None)
    assert api.db.query(User).count() == int(formal)
    assert api.db.query(AuthSession).count() == 2


def test_missing_record_only_preparation_replaces_secret(api):
    original = start_user(api)
    api.db.query(AuthSession).delete()
    api.db.commit()
    for endpoint in ("tokens", "anonymous-users"):
        response = api.client.post(f"{AUTH}/{endpoint}")
        assert response.json()["detail"]["code"] == "SESSION_NOT_FOUND"
        assert "set-cookie" not in response.headers
    response = api.client.post(f"{AUTH}/preparations")
    assert response.json()["notice"] == "PREVIOUS_SESSION_UNAVAILABLE"
    assert api.db.get(User, original["user"]["id"]) is not None


def test_revocation_precedes_expiry_and_rejects_cookie_and_unexpired_jwt(api):
    result = start_user(api)
    record = api.db.query(AuthSession).one()
    record.revoked_at = record.expires_at = sessions.utc_now() - timedelta(seconds=1)
    api.db.commit()
    for endpoint in ("preparations", "anonymous-users", "tokens"):
        response = api.client.post(f"{AUTH}/{endpoint}")
        assert response.json()["detail"]["code"] == "SESSION_REVOKED"
        assert "set-cookie" not in response.headers
    response = api.client.get("/api/users/me", headers=bearer(result))
    assert response.status_code == 401 and response.json()["detail"]["code"] == "SESSION_REVOKED"


def test_regular_requests_and_preparation_do_not_extend_but_token_issuance_does(api, monkeypatch):
    result = start_user(api)
    record = api.db.query(AuthSession).one()
    expiry = record.expires_at
    future = sessions.utc_now() + timedelta(days=1)
    monkeypatch.setattr(sessions, "utc_now", lambda: future)
    assert api.client.get("/api/users/me", headers=bearer(result)).status_code == 200
    assert api.client.post(f"{AUTH}/preparations").status_code == 200
    assert api.db.query(AuthSession).one().expires_at == expiry
    assert api.client.post(f"{AUTH}/tokens").status_code == 200
    assert api.db.query(AuthSession).one().expires_at == future + timedelta(days=30)


def test_database_outage_never_becomes_a_new_user_or_cookie(api, monkeypatch):
    result = start_user(api)
    def unavailable(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("private database failure"))
    monkeypatch.setattr(repository, "find_session_by_hash", unavailable)
    for endpoint in ("preparations", "anonymous-users", "tokens"):
        response = api.client.post(f"{AUTH}/{endpoint}")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "AUTH_TEMPORARILY_UNAVAILABLE"
        assert "private" not in response.text and "set-cookie" not in response.headers
    assert api.db.query(User).count() == 1
    assert api.db.get(User, result["user"]["id"]) is not None


def test_user_creation_commit_failure_rolls_back_user_and_preparation_link(api, monkeypatch):
    api.client.post(f"{AUTH}/preparations")
    def fail_commit():
        raise OperationalError("COMMIT", {}, Exception("unavailable"))
    monkeypatch.setattr(api.db, "commit", fail_commit)
    response = api.client.post(f"{AUTH}/anonymous-users")
    assert response.status_code == 503 and "set-cookie" not in response.headers
    assert api.db.query(User).count() == 0
    assert api.db.query(AuthSession).one().user_id is None


def test_nickname_collision_is_retried_and_existing_name_is_preserved(api, monkeypatch):
    existing = User(kakao_id=123, nickname="행복한김밥12345", nickname_key="행복한김밥12345".encode())
    api.db.add(existing)
    api.db.commit()
    names = iter([existing.nickname, "즐거운만두54321"])
    monkeypatch.setattr(sessions, "generate_nickname", lambda: next(names))
    result = start_user(api)
    assert result["user"]["nickname"] == "즐거운만두54321"
    assert api.db.get(User, existing.id).nickname == "행복한김밥12345"


def test_new_jwt_requires_matching_user_session_and_all_claims(api):
    result = start_user(api)
    claims = decode_access_token(result["access_token"])
    wrong = create_access_token({"sub": str(int(claims["sub"]) + 1), "sid": claims["sid"]})
    response = api.client.get("/api/users/me", headers={"Authorization": f"Bearer {wrong}"})
    assert response.json()["detail"]["code"] == "ACCESS_TOKEN_INVALID"
    missing = create_access_token({"sub": claims["sub"], "sid": "999999"})
    response = api.client.get("/api/users/me", headers={"Authorization": f"Bearer {missing}"})
    assert response.json()["detail"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.parametrize("change", ["sid", "iss", "aud", "sub", "iat", "expired", "expired_and_invalid", "forged", "legacy"])
def test_invalid_expired_forged_and_legacy_tokens_are_distinguished(api, change):
    result = start_user(api)
    claims = decode_access_token(result["access_token"])
    key = settings.JWT_SECRET
    expected = "ACCESS_TOKEN_INVALID"
    if change == "sid":
        claims["sid"] = True
    elif change in {"iss", "aud"}:
        claims[change] = "wrong"
    elif change == "sub":
        claims["sub"] = "01"
    elif change == "iat":
        claims["iat"] += 1000
    elif change.startswith("expired"):
        claims["exp"] = claims["iat"] - 1
        expected = "ACCESS_TOKEN_EXPIRED"
        if change == "expired_and_invalid":
            claims["aud"] = "wrong"
            expected = "ACCESS_TOKEN_INVALID"
    elif change == "forged":
        key += "wrong"
    else:
        claims = {"sub": claims["sub"], "exp": claims["exp"], "kakao_id": 123}
        expected = "REAUTHENTICATION_REQUIRED"
    token = jwt.encode(claims, key, algorithm="HS256")
    response = api.client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401 and response.json()["detail"]["code"] == expected


@pytest.mark.parametrize("count", [0, 99, 100, 101, 210])
def test_cleanup_has_one_total_cap_and_never_deletes_user_data(api, count):
    result = start_user(api)
    valid_id = api.db.query(AuthSession).one().id
    for index in range(count):
        record, _ = sessions.new_session()
        record.expires_at = sessions.utc_now() - timedelta(days=1)
        if index % 2:
            record.revoked_at = sessions.utc_now() - timedelta(days=2)
        api.db.add(record)
    api.db.commit()
    assert sessions.cleanup_auth_sessions_once(api.db) == min(count, 100)
    assert api.db.query(AuthSession).count() == 1 + max(0, count - 100)
    assert api.db.get(AuthSession, valid_id) is not None
    assert api.db.get(User, result["user"]["id"]) is not None


def test_session_foreign_key_prevents_turning_a_formal_session_into_a_preparation(api):
    result = start_user(api)
    api.db.delete(api.db.get(User, result["user"]["id"]))
    with pytest.raises(IntegrityError):
        api.db.commit()
    api.db.rollback()
    assert api.db.query(AuthSession).one().user_id == result["user"]["id"]


def test_standalone_kakao_login_cannot_silently_discard_uuid_context(api, monkeypatch):
    from unittest.mock import AsyncMock
    from importlib import import_module
    router = import_module("app.auth.router")
    result = start_user(api)
    login = AsyncMock()
    monkeypatch.setattr(router, "process_kakao_login", login)
    response = api.client.post("/api/auth/kakao/login", json={"code": "unused-code"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ACCOUNT_LINKING_NOT_AVAILABLE"
    login.assert_not_awaited()
    assert api.client.get("/api/users/me", headers=bearer(result)).status_code == 200


def test_creation_response_loss_after_preparation_window_still_restores_formal_user(api, monkeypatch):
    result = start_user(api)
    future = sessions.utc_now() + timedelta(minutes=6)
    monkeypatch.setattr(sessions, "utc_now", lambda: future)
    recovered = api.client.post(f"{AUTH}/anonymous-users")
    assert recovered.status_code == 200 and recovered.json()["user"] == result["user"]
    assert api.db.query(User).count() == 1

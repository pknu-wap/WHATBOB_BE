from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.auth.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.rooms.models  # noqa: F401
from app.auth.models import AccountTransition, AuthSession, KakaoSessionCredential
from app.auth.credentials import decrypt_kakao_token
from app.auth.sessions import create_uuid_user, new_session, utc_now
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.main import app
from app.users.models import User

CSRF_HEADERS = {
    "Origin": "http://localhost:8081",
    "X-Whatbob-Request": "1",
}


def _issue_session(db, user: User) -> tuple[AuthSession, str, str]:
    session_record, secret = new_session(user_id=user.id)
    db.add(session_record)
    db.commit()
    db.refresh(session_record)
    token = create_access_token({"sub": str(user.id), "sid": str(session_record.id)})
    return session_record, secret, token


@pytest.fixture()
def t01_env(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        bind=engine,
    )
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, base_url="https://testserver")
    try:
        yield {
            "client": client,
            "SessionLocal": TestingSessionLocal,
            "engine": engine,
        }
    finally:
        client.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_t01_d01_kakao_link_encrypts_credentials_binds_session_and_shreds_expired_prepare(
    t01_env,
    monkeypatch: pytest.MonkeyPatch,
):
    client: TestClient = t01_env["client"]
    SessionLocal = t01_env["SessionLocal"]

    raw_access_token = "kakao-raw-access-token-secret-12345"
    raw_refresh_token = "kakao-raw-refresh-token-secret-67890"

    def handle_kakao_http(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://kauth.kakao.com/oauth/token":
            body = parse_qs(request.content.decode("utf-8"))
            assert body["grant_type"] == ["authorization_code"]
            return httpx.Response(
                200,
                json={
                    "token_type": "bearer",
                    "access_token": raw_access_token,
                    "expires_in": 21599,
                    "refresh_token": raw_refresh_token,
                    "refresh_token_expires_in": 5183999,
                },
            )
        if str(request.url).startswith("https://kapi.kakao.com/v2/user/me"):
            assert request.headers.get("Authorization") == f"Bearer {raw_access_token}"
            return httpx.Response(
                200,
                json={
                    "id": 910001,
                    "kakao_account": {
                        "email": "kakao910001@example.com",
                        "profile": {
                            "nickname": "카카오테스터",
                            "profile_image_url": "https://k.kakaocdn.net/dn/test.jpg",
                        },
                    },
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.auth.kakao.httpx.AsyncClient",
        lambda *args, **kwargs: real_async_client(
            transport=httpx.MockTransport(handle_kakao_http)
        ),
    )

    with SessionLocal() as db:
        origin_user = create_uuid_user(db)
        db.commit()
        db.refresh(origin_user)
        origin_session, origin_secret, origin_jwt = _issue_session(db, origin_user)

    auth_headers = {**CSRF_HEADERS, "Authorization": f"Bearer {origin_jwt}"}
    client.cookies.set(settings.AUTH_COOKIE_NAME, origin_secret, domain="testserver.local", path="/api/auth")

    # 1. Prepare KAKAO_LINK -> stores encrypted credential bound to transition_id
    prep_res = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "valid-auth-code-1"},
        headers=auth_headers,
    )
    assert prep_res.status_code == 200
    assert raw_access_token not in prep_res.text
    assert raw_refresh_token not in prep_res.text
    transition_id = prep_res.json()["transition_id"]

    with SessionLocal() as db:
        tr = db.query(AccountTransition).filter_by(transition_id=transition_id).one()
        assert raw_access_token not in str(tr.provider_context_json)
        assert raw_refresh_token not in str(tr.provider_context_json)

        cred = db.query(KakaoSessionCredential).filter_by(transition_id=transition_id).one()
        assert cred.status == "PREPARED"
        assert cred.auth_session_id is None
        assert cred.kakao_id == 910001
        assert cred.encrypted_access_token is not None
        assert cred.encrypted_refresh_token is not None
        assert raw_access_token not in cred.encrypted_access_token
        assert raw_refresh_token not in cred.encrypted_refresh_token
        assert decrypt_kakao_token(cred.encrypted_access_token) == raw_access_token
        assert decrypt_kakao_token(cred.encrypted_refresh_token) == raw_refresh_token
        assert cred.access_token_expires_at is not None
        assert cred.refresh_token_expires_at is not None

    # 2. Commit KAKAO_LINK -> binds credential to new_auth_session.id with status ACTIVE
    commit_res = client.post(
        f"/api/auth/transitions/{transition_id}/commit",
        headers={**CSRF_HEADERS, "X-Whatbob-Auth-Context": str(origin_session.id)},
    )
    assert commit_res.status_code == 200
    assert raw_access_token not in commit_res.text
    assert raw_refresh_token not in commit_res.text

    with SessionLocal() as db:
        tr = db.query(AccountTransition).filter_by(transition_id=transition_id).one()
        cred = db.query(KakaoSessionCredential).filter_by(transition_id=transition_id).one()
        assert cred.status == "ACTIVE"
        assert cred.auth_session_id == tr.result_session_id
        assert decrypt_kakao_token(cred.encrypted_access_token) == raw_access_token
        assert decrypt_kakao_token(cred.encrypted_refresh_token) == raw_refresh_token

    # 3. Expired PREPARED transition shreds temporary encrypted tokens upon expiry check
    with SessionLocal() as db:
        u2 = create_uuid_user(db)
        db.commit()
        db.refresh(u2)
        s2, sec2, jwt2 = _issue_session(db, u2)

    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, sec2, domain="testserver.local", path="/api/auth")
    prep2 = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "valid-auth-code-2"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {jwt2}"},
    )
    assert prep2.status_code == 200
    tid2 = prep2.json()["transition_id"]

    with SessionLocal() as db:
        tr2 = db.query(AccountTransition).filter_by(transition_id=tid2).one()
        tr2.prepare_expires_at = utc_now() - timedelta(seconds=5)
        db.commit()

    exp_commit = client.post(f"/api/auth/transitions/{tid2}/commit", headers=CSRF_HEADERS)
    assert exp_commit.status_code == 401
    assert exp_commit.json()["detail"]["code"] == "TRANSITION_PREPARATION_EXPIRED"

    with SessionLocal() as db:
        cred2 = db.query(KakaoSessionCredential).filter_by(transition_id=tid2).one()
        assert cred2.status == "EXPIRED"
        assert cred2.encrypted_access_token is None
        assert cred2.encrypted_refresh_token is None


def test_t01_d02_logout_revokes_only_current_session_shreds_tokens_and_skips_legacy_session(
    t01_env,
    monkeypatch: pytest.MonkeyPatch,
):
    client: TestClient = t01_env["client"]
    SessionLocal = t01_env["SessionLocal"]

    logout_calls: list[str] = []
    tokens_by_code = {
        "code-device-a": ("access-tok-device-a", "refresh-tok-device-a"),
        "code-device-b": ("access-tok-device-b", "refresh-tok-device-b"),
    }

    def handle_kakao_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://kauth.kakao.com/oauth/token":
            body = parse_qs(request.content.decode("utf-8"))
            code = body["code"][0]
            acc, ref = tokens_by_code[code]
            return httpx.Response(
                200,
                json={
                    "token_type": "bearer",
                    "access_token": acc,
                    "expires_in": 21599,
                    "refresh_token": ref,
                    "refresh_token_expires_in": 5183999,
                },
            )
        if url.startswith("https://kapi.kakao.com/v2/user/me"):
            return httpx.Response(
                200,
                json={
                    "id": 920002,
                    "kakao_account": {
                        "email": "multi@example.com",
                        "profile": {"nickname": "멀티디바이스유저"},
                    },
                },
            )
        if url == "https://kapi.kakao.com/v1/user/logout":
            auth_hdr = request.headers.get("Authorization", "")
            logout_calls.append(auth_hdr)
            return httpx.Response(200, json={"id": 920002})
        raise AssertionError(f"Unexpected URL: {url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.auth.kakao.httpx.AsyncClient",
        lambda *args, **kwargs: real_async_client(
            transport=httpx.MockTransport(handle_kakao_http)
        ),
    )

    with SessionLocal() as db:
        u_a = create_uuid_user(db)
        u_b = create_uuid_user(db)
        db.commit()
        db.refresh(u_a)
        db.refresh(u_b)
        _, sec_a, jwt_a = _issue_session(db, u_a)
        _, sec_b, jwt_b = _issue_session(db, u_b)

    # Link Device A
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, sec_a, domain="testserver.local", path="/api/auth")
    prep_a = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "code-device-a"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {jwt_a}"},
    )
    assert prep_a.status_code == 200
    commit_a = client.post(
        f"/api/auth/transitions/{prep_a.json()['transition_id']}/commit",
        headers=CSRF_HEADERS,
    )
    assert commit_a.status_code == 200
    kakao_jwt_a = commit_a.json()["access_token"]
    kakao_sec_a = client.cookies.get(settings.AUTH_COOKIE_NAME, domain="testserver.local", path="/api/auth")
    kakao_user_id = commit_a.json()["user"]["id"]

    # Link Device B to the same Kakao account
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, sec_b, domain="testserver.local", path="/api/auth")
    prep_b = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "code-device-b"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {jwt_b}"},
    )
    assert prep_b.status_code == 200
    commit_b = client.post(
        f"/api/auth/transitions/{prep_b.json()['transition_id']}/commit",
        headers=CSRF_HEADERS,
    )
    assert commit_b.status_code == 200
    kakao_jwt_b = commit_b.json()["access_token"]
    kakao_sec_b = client.cookies.get(settings.AUTH_COOKIE_NAME, domain="testserver.local", path="/api/auth")

    # Also create Device C as a legacy session on the same Kakao user (no KakaoSessionCredential row)
    with SessionLocal() as db:
        kakao_user = db.get(User, kakao_user_id)
        _, kakao_sec_c, kakao_jwt_c = _issue_session(db, kakao_user)

    # 1. Logout Device A -> revokes only Device A's Kakao token pair, shreds Device A's tokens
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, kakao_sec_a, domain="testserver.local", path="/api/auth")
    out_prep_a = client.post(
        "/api/auth/transitions",
        json={"action": "LOGOUT"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {kakao_jwt_a}"},
    )
    assert out_prep_a.status_code == 200
    out_tid_a = out_prep_a.json()["transition_id"]
    out_prepare_secret_a = client.cookies.get(
        settings.TRANSITION_PREPARE_COOKIE_NAME, domain="testserver.local", path="/api/auth"
    )

    out_commit_a = client.post(
        f"/api/auth/transitions/{out_tid_a}/commit",
        headers=CSRF_HEADERS,
    )
    assert out_commit_a.status_code == 200
    assert out_commit_a.json()["kakao_logout_status"] in ("PENDING", "REVOKED")
    new_uuid_jwt_a = out_commit_a.json()["access_token"]

    # Background task ran after commit response: verify Kakao logout was called ONLY for Device A's token
    assert logout_calls == ["Bearer access-tok-device-a"]

    # Status and recovery endpoints report REVOKED
    status_a = client.get(
        f"/api/auth/transitions/{out_tid_a}",
        headers={"Authorization": f"Bearer {new_uuid_jwt_a}"},
    )
    assert status_a.status_code == 200
    assert status_a.json()["kakao_logout_status"] == "REVOKED"

    client.cookies.set(
        settings.TRANSITION_PREPARE_COOKIE_NAME,
        out_prepare_secret_a,
        domain="testserver.local",
        path="/api/auth",
    )
    recov_a = client.post(f"/api/auth/transitions/{out_tid_a}/recovery", headers=CSRF_HEADERS)
    assert recov_a.status_code == 200
    assert recov_a.json()["kakao_logout_status"] == "REVOKED"

    # Verify Device A's encrypted tokens are shredded (NULL), while Device B's session & tokens remain ACTIVE
    with SessionLocal() as db:
        cred_a = db.query(KakaoSessionCredential).filter_by(logout_transition_id=out_tid_a).one()
        assert cred_a.status == "REVOKED"
        assert cred_a.encrypted_access_token is None
        assert cred_a.encrypted_refresh_token is None
        assert cred_a.finished_at is not None

        cred_b = (
            db.query(KakaoSessionCredential)
            .filter_by(transition_id=prep_b.json()["transition_id"])
            .one()
        )
        assert cred_b.status == "ACTIVE"
        assert decrypt_kakao_token(cred_b.encrypted_access_token) == "access-tok-device-b"
        assert decrypt_kakao_token(cred_b.encrypted_refresh_token) == "refresh-tok-device-b"

    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {kakao_jwt_b}"}).status_code == 200

    # 2. Logout Device C (legacy session without stored Kakao credentials) -> SKIPPED_NO_CREDENTIALS
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, kakao_sec_c, domain="testserver.local", path="/api/auth")
    out_prep_c = client.post(
        "/api/auth/transitions",
        json={"action": "LOGOUT"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {kakao_jwt_c}"},
    )
    assert out_prep_c.status_code == 200
    out_tid_c = out_prep_c.json()["transition_id"]

    out_commit_c = client.post(
        f"/api/auth/transitions/{out_tid_c}/commit",
        headers=CSRF_HEADERS,
    )
    assert out_commit_c.status_code == 200
    assert out_commit_c.json()["kakao_logout_status"] == "SKIPPED_NO_CREDENTIALS"
    new_uuid_jwt_c = out_commit_c.json()["access_token"]

    status_c = client.get(
        f"/api/auth/transitions/{out_tid_c}",
        headers={"Authorization": f"Bearer {new_uuid_jwt_c}"},
    )
    assert status_c.status_code == 200
    assert status_c.json()["kakao_logout_status"] == "SKIPPED_NO_CREDENTIALS"
    assert len(logout_calls) == 1  # No extra Kakao API call for legacy session


def test_t01_d02_expired_access_token_refreshes_with_and_without_rotation_and_serializes_refresh(
    t01_env,
    monkeypatch: pytest.MonkeyPatch,
):
    import asyncio
    from app.auth.revocations import KakaoRevocationDispatcher

    client: TestClient = t01_env["client"]
    SessionLocal = t01_env["SessionLocal"]
    engine = t01_env["engine"]

    events: list[tuple[str, str]] = []
    rotate_mode = {"rotate": False, "fail_logout_once": False}

    def handle_kakao_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://kauth.kakao.com/oauth/token":
            body = parse_qs(request.content.decode("utf-8"))
            grant_type = body["grant_type"][0]
            if grant_type == "authorization_code":
                code = body["code"][0]
                return httpx.Response(
                    200,
                    json={
                        "token_type": "bearer",
                        "access_token": f"initial-access-{code}",
                        "expires_in": 3600,
                        "refresh_token": f"initial-refresh-{code}",
                        "refresh_token_expires_in": 500000,
                    },
                )
            if grant_type == "refresh_token":
                used_refresh = body["refresh_token"][0]
                events.append(("refresh", used_refresh))
                if rotate_mode["rotate"]:
                    return httpx.Response(
                        200,
                        json={
                            "token_type": "bearer",
                            "access_token": "rotated-access-token",
                            "expires_in": 7200,
                            "refresh_token": "rotated-refresh-token",
                            "refresh_token_expires_in": 999999,
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "token_type": "bearer",
                        "access_token": "refreshed-access-no-rotation",
                        "expires_in": 7200,
                    },
                )
        if url.startswith("https://kapi.kakao.com/v2/user/me"):
            auth_hdr = request.headers.get("Authorization", "")
            kid = 930001 if "code-1" in auth_hdr else 930002
            return httpx.Response(
                200,
                json={
                    "id": kid,
                    "kakao_account": {
                        "email": f"u{kid}@example.com",
                        "profile": {"nickname": f"유저{kid}"},
                    },
                },
            )
        if url == "https://kapi.kakao.com/v1/user/logout":
            auth_hdr = request.headers.get("Authorization", "")
            events.append(("logout", auth_hdr))
            if auth_hdr.startswith("Bearer initial-access-"):
                return httpx.Response(401, json={"msg": "expired_or_invalid", "code": -401})
            if rotate_mode["fail_logout_once"]:
                rotate_mode["fail_logout_once"] = False
                return httpx.Response(503, json={"msg": "temporary outage"})
            return httpx.Response(200, json={"id": 930001})
        raise AssertionError(f"Unexpected URL: {url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.auth.kakao.httpx.AsyncClient",
        lambda *args, **kwargs: real_async_client(
            transport=httpx.MockTransport(handle_kakao_http)
        ),
    )

    # Case 1: 401 on logout triggers refresh WITHOUT refresh_token rotation -> preserves refresh token, then logs out
    with SessionLocal() as db:
        u1 = create_uuid_user(db)
        db.commit()
        db.refresh(u1)
        _, sec1, jwt1 = _issue_session(db, u1)

    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, sec1, domain="testserver.local", path="/api/auth")
    p1 = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "code-1"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"},
    )
    c1 = client.post(f"/api/auth/transitions/{p1.json()['transition_id']}/commit", headers=CSRF_HEADERS)
    assert c1.status_code == 200
    k_jwt1 = c1.json()["access_token"]
    k_sec1 = client.cookies.get(settings.AUTH_COOKIE_NAME, domain="testserver.local", path="/api/auth")

    # Test refresh without rotation on active session first to verify existing refresh_token & expiry are preserved
    with SessionLocal() as db:
        cred1 = db.query(KakaoSessionCredential).filter_by(transition_id=p1.json()["transition_id"]).one()
        cred1_id = int(cred1.id)
        orig_refresh_exp = cred1.refresh_token_expires_at

    dispatcher = KakaoRevocationDispatcher(engine)
    asyncio.run(dispatcher.refresh_credential(cred1_id))

    with SessionLocal() as db:
        cred1 = db.get(KakaoSessionCredential, cred1_id)
        assert decrypt_kakao_token(cred1.encrypted_access_token) == "refreshed-access-no-rotation"
        assert decrypt_kakao_token(cred1.encrypted_refresh_token) == "initial-refresh-code-1"
        assert cred1.refresh_token_expires_at == orig_refresh_exp
        # Force access_token back to initial-access-code-1 to test 401-triggered refresh during logout
        from app.auth.credentials import encrypt_kakao_token
        cred1.encrypted_access_token = encrypt_kakao_token("initial-access-code-1")
        db.commit()

    events.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, k_sec1, domain="testserver.local", path="/api/auth")
    out_p1 = client.post(
        "/api/auth/transitions",
        json={"action": "LOGOUT"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {k_jwt1}"},
    )
    out_c1 = client.post(f"/api/auth/transitions/{out_p1.json()['transition_id']}/commit", headers=CSRF_HEADERS)
    assert out_c1.status_code == 200
    assert events == [
        ("logout", "Bearer initial-access-code-1"),
        ("refresh", "initial-refresh-code-1"),
        ("logout", "Bearer refreshed-access-no-rotation"),
    ]

    # Case 2: Expired access_token_expires_at + refresh_token rotation + transient logout failure preserves rotated tokens for next retry
    with SessionLocal() as db:
        u2 = create_uuid_user(db)
        db.commit()
        db.refresh(u2)
        _, sec2, jwt2 = _issue_session(db, u2)

    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, sec2, domain="testserver.local", path="/api/auth")
    p2 = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "code-2"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {jwt2}"},
    )
    c2 = client.post(f"/api/auth/transitions/{p2.json()['transition_id']}/commit", headers=CSRF_HEADERS)
    assert c2.status_code == 200
    k_jwt2 = c2.json()["access_token"]
    k_sec2 = client.cookies.get(settings.AUTH_COOKIE_NAME, domain="testserver.local", path="/api/auth")

    with SessionLocal() as db:
        cred2 = db.query(KakaoSessionCredential).filter_by(transition_id=p2.json()["transition_id"]).one()
        cred2.access_token_expires_at = utc_now() - timedelta(seconds=10)
        db.commit()

    events.clear()
    rotate_mode["rotate"] = True
    rotate_mode["fail_logout_once"] = True

    client.cookies.set(settings.AUTH_COOKIE_NAME, k_sec2, domain="testserver.local", path="/api/auth")
    out_p2 = client.post(
        "/api/auth/transitions",
        json={"action": "LOGOUT"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {k_jwt2}"},
    )
    out_tid2 = out_p2.json()["transition_id"]
    out_c2 = client.post(f"/api/auth/transitions/{out_tid2}/commit", headers=CSRF_HEADERS)
    assert out_c2.status_code == 200

    # Attempt 1 refreshed and rotated the token, then logout got 503 -> rotated tokens are persisted in DB while PENDING_REVOCATION!
    with SessionLocal() as db:
        cred2 = db.query(KakaoSessionCredential).filter_by(logout_transition_id=out_tid2).one()
        assert cred2.status == "PENDING_REVOCATION"
        assert decrypt_kakao_token(cred2.encrypted_access_token) == "rotated-access-token"
        assert decrypt_kakao_token(cred2.encrypted_refresh_token) == "rotated-refresh-token"


def test_t01_d03_retry_backoff_60s_deadline_restart_recovery_permanent_failure_and_shredding(
    t01_env,
    monkeypatch: pytest.MonkeyPatch,
):
    import asyncio
    from app.auth.revocations import KakaoRevocationDispatcher

    client: TestClient = t01_env["client"]
    SessionLocal = t01_env["SessionLocal"]
    engine = t01_env["engine"]

    class FakeClock:
        def __init__(self, now: datetime):
            self.current = now
            self.mono = 1000.0

        def utcnow(self) -> datetime:
            return self.current

        def monotonic(self) -> float:
            return self.mono

        def advance(self, seconds: float) -> None:
            self.current = self.current + timedelta(seconds=seconds)
            self.mono += seconds

    clock = FakeClock(utc_now())
    monkeypatch.setattr("app.auth.revocations.revocation_dispatcher_for", lambda bind: KakaoRevocationDispatcher(bind, clock=clock))

    mode = {"behavior": "503"}
    logout_attempts: list[datetime] = []

    def handle_kakao_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://kauth.kakao.com/oauth/token":
            body = parse_qs(request.content.decode("utf-8"))
            if body["grant_type"] == ["authorization_code"]:
                code = body["code"][0]
                return httpx.Response(
                    200,
                    json={
                        "token_type": "bearer",
                        "access_token": f"acc-{code}",
                        "expires_in": 21599,
                        "refresh_token": f"ref-{code}",
                        "refresh_token_expires_in": 5183999,
                    },
                )
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh token expired"})
        if url.startswith("https://kapi.kakao.com/v2/user/me"):
            auth_hdr = request.headers.get("Authorization", "")
            kid = 940001 if "outage" in auth_hdr else 940002
            return httpx.Response(
                200,
                json={
                    "id": kid,
                    "kakao_account": {"email": f"k{kid}@example.com", "profile": {"nickname": f"닉{kid}"}},
                },
            )
        if url == "https://kapi.kakao.com/v1/user/logout":
            logout_attempts.append(clock.utcnow())
            if mode["behavior"] == "503":
                return httpx.Response(503, json={"msg": "service unavailable"})
            if mode["behavior"] == "400_permanent":
                return httpx.Response(400, json={"msg": "bad request"})
            return httpx.Response(200, json={"id": 940001})
        raise AssertionError(f"Unexpected URL: {url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.auth.kakao.httpx.AsyncClient",
        lambda *args, **kwargs: real_async_client(
            transport=httpx.MockTransport(handle_kakao_http)
        ),
    )

    # 1. Outage scenario: 1, 3, 5, 10, 30s backoff up to 60s deadline, surviving dispatcher restart
    with SessionLocal() as db:
        u1 = create_uuid_user(db)
        db.commit()
        db.refresh(u1)
        _, sec1, jwt1 = _issue_session(db, u1)

    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, sec1, domain="testserver.local", path="/api/auth")
    p1 = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "outage"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"},
    )
    c1 = client.post(f"/api/auth/transitions/{p1.json()['transition_id']}/commit", headers=CSRF_HEADERS)
    k_jwt1 = c1.json()["access_token"]
    k_sec1 = client.cookies.get(settings.AUTH_COOKIE_NAME, domain="testserver.local", path="/api/auth")

    client.cookies.set(settings.AUTH_COOKIE_NAME, k_sec1, domain="testserver.local", path="/api/auth")
    out_p1 = client.post(
        "/api/auth/transitions",
        json={"action": "LOGOUT"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {k_jwt1}"},
    )
    out_tid1 = out_p1.json()["transition_id"]
    out_prep_sec1 = client.cookies.get(
        settings.TRANSITION_PREPARE_COOKIE_NAME, domain="testserver.local", path="/api/auth"
    )
    out_c1 = client.post(f"/api/auth/transitions/{out_tid1}/commit", headers=CSRF_HEADERS)
    assert out_c1.status_code == 200
    assert out_c1.json()["kakao_logout_status"] == "PENDING"
    new_uuid_jwt1 = out_c1.json()["access_token"]

    # Attempt 1 ran immediately in background task and failed with 503
    assert len(logout_attempts) == 1
    st1 = client.get(f"/api/auth/transitions/{out_tid1}", headers={"Authorization": f"Bearer {new_uuid_jwt1}"})
    assert st1.json()["kakao_logout_status"] == "PENDING"

    # Recovery works during Kakao outage and reports PENDING
    client.cookies.set(
        settings.TRANSITION_PREPARE_COOKIE_NAME, out_prep_sec1, domain="testserver.local", path="/api/auth"
    )
    rec1 = client.post(f"/api/auth/transitions/{out_tid1}/recovery", headers=CSRF_HEADERS)
    assert rec1.status_code == 200
    assert rec1.json()["kakao_logout_status"] == "PENDING"

    # Verify exact retry schedule: 1s, 3s, 5s, 10s, 30s (total 49s), then remaining 11s hits 60s deadline
    for idx, expected_delay in enumerate((1, 3, 5, 10, 30), start=2):
        # Before delay elapsed, run_due does not trigger a new attempt
        clock.advance(expected_delay - 0.5)
        restarted_dispatcher = KakaoRevocationDispatcher(engine, clock=clock)
        asyncio.run(restarted_dispatcher.run_due())
        assert len(logout_attempts) == idx - 1

        # At exact delay, attempt fires
        clock.advance(0.5)
        asyncio.run(restarted_dispatcher.run_due())
        assert len(logout_attempts) == idx

    # Now 49 seconds have elapsed since first_failed_at. Next backoff capped at deadline (60s total -> +11s)
    clock.advance(11.0)
    restarted_dispatcher = KakaoRevocationDispatcher(engine, clock=clock)
    asyncio.run(restarted_dispatcher.run_due())

    with SessionLocal() as db:
        cred1 = db.query(KakaoSessionCredential).filter_by(logout_transition_id=out_tid1).one()
        assert cred1.status == "FAILED"
        assert cred1.terminal_reason == "DEADLINE_EXCEEDED"
        assert cred1.encrypted_access_token is None
        assert cred1.encrypted_refresh_token is None
        assert cred1.finished_at is not None

    st1_final = client.get(f"/api/auth/transitions/{out_tid1}", headers={"Authorization": f"Bearer {new_uuid_jwt1}"})
    assert st1_final.json()["kakao_logout_status"] == "FAILED"

    # 2. Permanent error scenario: fails immediately on Attempt 1 without retry and shreds tokens
    logout_attempts.clear()
    mode["behavior"] = "400_permanent"

    with SessionLocal() as db:
        u2 = create_uuid_user(db)
        db.commit()
        db.refresh(u2)
        _, sec2, jwt2 = _issue_session(db, u2)

    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, sec2, domain="testserver.local", path="/api/auth")
    p2 = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "perm"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {jwt2}"},
    )
    c2 = client.post(f"/api/auth/transitions/{p2.json()['transition_id']}/commit", headers=CSRF_HEADERS)
    k_jwt2 = c2.json()["access_token"]
    k_sec2 = client.cookies.get(settings.AUTH_COOKIE_NAME, domain="testserver.local", path="/api/auth")

    client.cookies.set(settings.AUTH_COOKIE_NAME, k_sec2, domain="testserver.local", path="/api/auth")
    out_p2 = client.post(
        "/api/auth/transitions",
        json={"action": "LOGOUT"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {k_jwt2}"},
    )
    out_tid2 = out_p2.json()["transition_id"]
    out_c2 = client.post(f"/api/auth/transitions/{out_tid2}/commit", headers=CSRF_HEADERS)
    assert out_c2.status_code == 200
    new_uuid_jwt2 = out_c2.json()["access_token"]

    assert len(logout_attempts) == 1
    with SessionLocal() as db:
        cred2 = db.query(KakaoSessionCredential).filter_by(logout_transition_id=out_tid2).one()
        assert cred2.status == "FAILED"
        assert cred2.attempt_count == 1
        assert cred2.next_attempt_at is None
        assert cred2.encrypted_access_token is None
        assert cred2.encrypted_refresh_token is None

    st2_final = client.get(f"/api/auth/transitions/{out_tid2}", headers={"Authorization": f"Bearer {new_uuid_jwt2}"})
    assert st2_final.json()["kakao_logout_status"] == "FAILED"


def test_t01_d01_superseded_prepare_and_expired_session_cleanup_shred_credentials(
    t01_env,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.auth.sessions import cleanup_auth_sessions_once

    client: TestClient = t01_env["client"]
    SessionLocal = t01_env["SessionLocal"]

    def handle_kakao_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://kauth.kakao.com/oauth/token":
            body = parse_qs(request.content.decode("utf-8"))
            code = body["code"][0]
            return httpx.Response(
                200,
                json={
                    "token_type": "bearer",
                    "access_token": f"acc-{code}",
                    "expires_in": 21599,
                    "refresh_token": f"ref-{code}",
                    "refresh_token_expires_in": 5183999,
                },
            )
        if url.startswith("https://kapi.kakao.com/v2/user/me"):
            return httpx.Response(
                200,
                json={
                    "id": 950001,
                    "kakao_account": {"email": "k950@example.com", "profile": {"nickname": "닉950"}},
                },
            )
        raise AssertionError(f"Unexpected URL: {url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.auth.kakao.httpx.AsyncClient",
        lambda *args, **kwargs: real_async_client(
            transport=httpx.MockTransport(handle_kakao_http)
        ),
    )

    with SessionLocal() as db:
        u1 = create_uuid_user(db)
        db.commit()
        db.refresh(u1)
        _, sec1, jwt1 = _issue_session(db, u1)

    auth_headers = {**CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"}
    client.cookies.set(settings.AUTH_COOKIE_NAME, sec1, domain="testserver.local", path="/api/auth")

    # 1. Prepare first KAKAO_LINK
    prep_first = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "first-attempt"},
        headers=auth_headers,
    )
    assert prep_first.status_code == 200
    tid_first = prep_first.json()["transition_id"]

    # 2. Prepare second KAKAO_LINK in the same UUID session -> supersedes first prepare and shreds its tokens
    prep_second = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "second-attempt"},
        headers=auth_headers,
    )
    assert prep_second.status_code == 200
    tid_second = prep_second.json()["transition_id"]

    with SessionLocal() as db:
        cred_first = db.query(KakaoSessionCredential).filter_by(transition_id=tid_first).one()
        assert cred_first.status == "CANCELLED"
        assert cred_first.encrypted_access_token is None
        assert cred_first.encrypted_refresh_token is None

        cred_second = db.query(KakaoSessionCredential).filter_by(transition_id=tid_second).one()
        assert cred_second.status == "PREPARED"
        assert decrypt_kakao_token(cred_second.encrypted_access_token) == "acc-second-attempt"

    # 3. Commit second KAKAO_LINK, then expire its AuthSession and run cleanup_auth_sessions_once -> shreds expired session's tokens
    commit_second = client.post(f"/api/auth/transitions/{tid_second}/commit", headers=CSRF_HEADERS)
    assert commit_second.status_code == 200

    with SessionLocal() as db:
        tr_second = db.query(AccountTransition).filter_by(transition_id=tid_second).one()
        active_sess = db.get(AuthSession, int(tr_second.result_session_id))
        active_sess.expires_at = utc_now() - timedelta(seconds=10)
        db.commit()

        deleted = cleanup_auth_sessions_once(db)
        assert deleted >= 1

        cred_second = db.query(KakaoSessionCredential).filter_by(transition_id=tid_second).one()
        assert cred_second.status == "EXPIRED"
        assert cred_second.encrypted_access_token is None
        assert cred_second.encrypted_refresh_token is None

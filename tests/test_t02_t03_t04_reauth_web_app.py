from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.auth.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.rooms.models  # noqa: F401
from app.auth.models import AccountTransition, AuthSession, KakaoSessionCredential
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.menus.models import Menu, UserMeal
from app.users.models import User

WEB_CSRF_HEADERS = {
    "Origin": "http://localhost:8081",
    "X-Whatbob-Request": "1",
}

APP_CSRF_HEADERS = {
    "X-Whatbob-Client": "app",
    "X-Whatbob-Request": "1",
}


@pytest.fixture()
def auth_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    upload_dir = tmp_path / "profile-images"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROFILE_IMAGE_UPLOAD_DIR", str(upload_dir))

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

    with TestingSessionLocal() as db:
        m1 = Menu(
            food_code="M001",
            name="김치찌개",
            base_name="김치찌개",
            category_large="찌개/전골류",
            meal_type="lunch",
            has_soup=True,
            cooking_style="stew",
            main_ingredient="pork",
            flavor_intensity="spicy",
            temperature="hot",
            heaviness="medium",
            allergen_tags=["대두", "돼지고기"],
        )
        db.add(m1)
        db.commit()
        db.refresh(m1)
        menu_id = m1.id

    def override_get_db():
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, base_url="https://testserver")
    try:
        yield {
            "client": client,
            "engine": engine,
            "SessionLocal": TestingSessionLocal,
            "menu_id": menu_id,
        }
    finally:
        client.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _bootstrap_web_uuid(client: TestClient) -> dict:
    prep = client.post("/api/auth/preparations", headers=WEB_CSRF_HEADERS)
    assert prep.status_code == 200
    created = client.post("/api/auth/anonymous-users", headers=WEB_CSRF_HEADERS)
    assert created.status_code == 201
    return created.json()


def test_t02_logout_sets_requires_reauth_and_enforces_prompt_login_and_server_state(
    auth_env, monkeypatch: pytest.MonkeyPatch
):
    """
    T02-D01:
    1. 최초 UUID 세션은 requires_reauth=False이며 카카오 로그인 URL에 prompt=login이 붙지 않는다.
    2. 카카오 연결 후 LOGOUT을 확정하면 새 UUID 세션의 requires_reauth=True가 되며,
       세션 복원(POST /api/auth/tokens)과 로그인 URL 발급(GET /api/auth/kakao/login/url)에서
       requires_reauth=True 및 prompt=login이 서버에 의해 강제된다.
    3. 서버가 세션에 발급한 최신 1회용 state만 KAKAO_LINK prepare를 통과하며,
       이전 탭의 구형 state나 재사용된 state는 400 INVALID_OAUTH_STATE로 거절된다.
    """
    async def fake_exchange(code: str, **kwargs):
        return {
            "access_token": f"access-{code}",
            "refresh_token": f"refresh-{code}",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400,
        }

    async def fake_user_info(access_token: str):
        return {
            "kakao_id": 900101,
            "nickname": "길동",
            "profile_image": None,
            "email": "yungil@kakao.com",
        }

    async def fake_logout(access_token: str, **kwargs):
        return None

    monkeypatch.setattr("app.auth.transitions.exchange_kakao_authorization_code", fake_exchange)
    monkeypatch.setattr("app.auth.transitions.get_kakao_user_info", fake_user_info)
    monkeypatch.setattr("app.auth.revocations.logout_kakao_access_token", fake_logout)

    client: TestClient = auth_env["client"]
    if True:
        boot = _bootstrap_web_uuid(client)
        assert boot["requires_reauth"] is False
        jwt1 = boot["access_token"]

        # 1. 최초 연결 시 로그인 URL 요청 -> requires_reauth=False, prompt 없음
        url_res1 = client.get(
            "/api/auth/kakao/login/url",
            headers={"Authorization": f"Bearer {jwt1}"},
        )
        assert url_res1.status_code == 200
        url_data1 = url_res1.json()
        assert url_data1["requires_reauth"] is False
        parsed1 = urlparse(url_data1["login_url"])
        qs1 = parse_qs(parsed1.query)
        assert "prompt" not in qs1
        state1 = url_data1["state"]
        assert qs1["state"] == [state1]

        # 2. KAKAO_LINK prepare & commit
        prep_link = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"},
            json={"action": "KAKAO_LINK", "code": "code-initial", "state": state1},
        )
        assert prep_link.status_code == 200
        prep_link_body = prep_link.json()
        assert prep_link_body["target_account"] == {
            "nickname": "길동",
            "masked_email": "yu***@kakao.com",
        }

        commit_link = client.post(
            f"/api/auth/transitions/{prep_link_body['transition_id']}/commit",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"},
        )
        assert commit_link.status_code == 200
        kakao_jwt = commit_link.json()["access_token"]
        assert commit_link.json()["requires_reauth"] is False

        # 3. LOGOUT prepare & commit -> 새 UUID 세션은 requires_reauth=True
        prep_logout = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {kakao_jwt}"},
            json={"action": "LOGOUT"},
        )
        assert prep_logout.status_code == 200
        commit_logout = client.post(
            f"/api/auth/transitions/{prep_logout.json()['transition_id']}/commit",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {kakao_jwt}"},
        )
        assert commit_logout.status_code == 200
        logout_body = commit_logout.json()
        assert logout_body["requires_reauth"] is True
        reauth_jwt = logout_body["access_token"]

        # 세션 복원(POST /api/auth/tokens)에서도 requires_reauth=True 유지
        restored = client.post("/api/auth/tokens", headers=WEB_CSRF_HEADERS)
        assert restored.status_code == 200
        assert restored.json()["requires_reauth"] is True

        # 4. 로그아웃 후 재연결 시 두 번 연속 로그인 URL 발급 -> 최신 state만 유효, prompt=login 강제
        first_attempt = client.get(
            "/api/auth/kakao/login/url",
            headers={"Authorization": f"Bearer {reauth_jwt}"},
        ).json()
        assert first_attempt["requires_reauth"] is True
        assert first_attempt["prompt"] == "login"
        assert parse_qs(urlparse(first_attempt["login_url"]).query)["prompt"] == ["login"]
        old_state = first_attempt["state"]

        second_attempt = client.get(
            "/api/auth/kakao/login/url",
            headers={"Authorization": f"Bearer {reauth_jwt}"},
        ).json()
        latest_state = second_attempt["state"]
        assert latest_state != old_state

        # 이전 탭의 old_state로 prepare 요청 시 400 INVALID_OAUTH_STATE 거절
        stale_prep = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {reauth_jwt}"},
            json={"action": "KAKAO_LINK", "code": "code-stale", "state": old_state},
        )
        assert stale_prep.status_code == 400
        assert stale_prep.json()["detail"]["code"] == "INVALID_OAUTH_STATE"

        # 최신 latest_state로 prepare 요청 시 성공 및 state 1회용 소진
        valid_prep = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {reauth_jwt}"},
            json={"action": "KAKAO_LINK", "code": "code-fresh", "state": latest_state},
        )
        assert valid_prep.status_code == 200

        # 이미 소진된 latest_state를 다시 쓰면(새 로그인 URL을 발급받지 않은 경우) 400 또는 새 state 필요
        # 다시 로그인 URL 발급 후 이전 latest_state 재사용 시도 -> 거절
        client.get(
            "/api/auth/kakao/login/url",
            headers={"Authorization": f"Bearer {reauth_jwt}"},
        )
        replay_prep = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {reauth_jwt}"},
            json={"action": "KAKAO_LINK", "code": "code-replay", "state": latest_state},
        )
        assert replay_prep.status_code == 400
        assert replay_prep.json()["detail"]["code"] == "INVALID_OAUTH_STATE"


def test_t02_unverifiable_target_stops_prepare_and_cancel_endpoint_preserves_uuid_records(
    auth_env, monkeypatch: pytest.MonkeyPatch
):
    """
    T02:
    1. 카카오 계정이 닉네임과 이메일을 둘 다 제공하지 않으면 KAKAO_LINK prepare를 즉시 중단(422 KAKAO_TARGET_UNVERIFIABLE)하고
       임시 토큰을 남기지 않으며 사용자의 UUID 세션·기록을 보존한다.
    2. 확인 화면에서 취소(POST /api/auth/transitions/{id}/cancel)하면 PREPARED 전이가 CANCELLED로 바뀌고
       임시 토큰이 파기되며, 이후 해당 전환으로 commit을 호출하면 409 TRANSITION_NOT_PREPARED로 거절된다.
    3. 취소 후 같은 UUID 세션에서 다른 카카오 계정을 선택해 연결하면 현재 UUID 기록만 선택한 계정으로 이관되고,
       이전 카카오 계정의 기록은 그대로 유지된다.
    """
    profiles_by_code = {
        "code-empty": {
            "kakao_id": 900201,
            "nickname": "   ",
            "profile_image": None,
            "email": None,
        },
        "code-wrong-account": {
            "kakao_id": 900202,
            "nickname": "잘못고른계정",
            "profile_image": None,
            "email": "wrong@kakao.com",
        },
        "code-right-account": {
            "kakao_id": 900203,
            "nickname": None,
            "profile_image": None,
            "email": "right_user@kakao.com",
        },
    }

    async def fake_exchange(code: str, **kwargs):
        return {
            "access_token": f"access-{code}",
            "refresh_token": f"refresh-{code}",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400,
        }

    async def fake_user_info(access_token: str):
        code = access_token.replace("access-", "", 1)
        return profiles_by_code[code]

    monkeypatch.setattr("app.auth.transitions.exchange_kakao_authorization_code", fake_exchange)
    monkeypatch.setattr("app.auth.transitions.get_kakao_user_info", fake_user_info)

    client: TestClient = auth_env["client"]
    if True:
        boot = _bootstrap_web_uuid(client)
        jwt = boot["access_token"]
        uuid_val = boot["user"]["uuid"]

        # UUID 상태에서 식사 기록 1건 저장
        with auth_env["SessionLocal"]() as db:
            u = db.query(User).filter(User.uuid == uuid_val).one()
            from app.auth.sessions import utc_now
            db.add(
                UserMeal(
                    user_id=u.id,
                    menu_id=auth_env["menu_id"],
                    menu_name="김치찌개",
                    eaten_at=utc_now(),
                )
            )
            db.commit()

        # 1. 닉네임·이메일 둘 다 없는 계정 -> 422 KAKAO_TARGET_UNVERIFIABLE
        s1 = client.get("/api/auth/kakao/login/url", headers={"Authorization": f"Bearer {jwt}"}).json()["state"]
        res_empty = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
            json={"action": "KAKAO_LINK", "code": "code-empty", "state": s1},
        )
        assert res_empty.status_code == 422
        assert res_empty.json()["detail"]["code"] == "KAKAO_TARGET_UNVERIFIABLE"

        with auth_env["SessionLocal"]() as db:
            assert db.query(KakaoSessionCredential).count() == 0

        # 2. 잘못 고른 계정 prepare -> 확인 화면에서 cancel 호출
        s2 = client.get("/api/auth/kakao/login/url", headers={"Authorization": f"Bearer {jwt}"}).json()["state"]
        res_wrong = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
            json={"action": "KAKAO_LINK", "code": "code-wrong-account", "state": s2},
        )
        assert res_wrong.status_code == 200
        wrong_tid = res_wrong.json()["transition_id"]
        assert res_wrong.json()["target_account"] == {
            "nickname": "잘못고른계정",
            "masked_email": "wr***@kakao.com",
        }

        cancel_res = client.post(
            f"/api/auth/transitions/{wrong_tid}/cancel",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
        )
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "CANCELLED"

        # 중복 cancel은 멱등(200 CANCELLED)
        cancel_again = client.post(
            f"/api/auth/transitions/{wrong_tid}/cancel",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
        )
        assert cancel_again.status_code == 200
        assert cancel_again.json()["status"] == "CANCELLED"

        # 취소된 전환의 임시 카카오 토큰 원문은 즉시 파기됨
        with auth_env["SessionLocal"]() as db:
            cred = db.query(KakaoSessionCredential).filter_by(transition_id=wrong_tid).one()
            assert cred.status == "CANCELLED"
            assert cred.encrypted_access_token is None
            assert cred.encrypted_refresh_token is None

        # 취소된 전환에 대해 commit 시도 -> 409 TRANSITION_NOT_PREPARED
        commit_cancelled = client.post(
            f"/api/auth/transitions/{wrong_tid}/commit",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
        )
        assert commit_cancelled.status_code in (401, 409)

        # 3. 같은 UUID 세션에서 올바른 계정(닉네임은 없고 이메일만 있는 계정)으로 다시 prepare & commit
        s3 = client.get("/api/auth/kakao/login/url", headers={"Authorization": f"Bearer {jwt}"}).json()["state"]
        res_right = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
            json={"action": "KAKAO_LINK", "code": "code-right-account", "state": s3},
        )
        assert res_right.status_code == 200
        assert res_right.json()["target_account"] == {
            "nickname": None,
            "masked_email": "ri***@kakao.com",
        }
        right_tid = res_right.json()["transition_id"]

        commit_right = client.post(
            f"/api/auth/transitions/{right_tid}/commit",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
        )
        assert commit_right.status_code == 200
        committed_user_id = commit_right.json()["user"]["id"]

        with auth_env["SessionLocal"]() as db:
            meals = db.query(UserMeal).filter(UserMeal.user_id == committed_user_id).all()
            assert len(meals) == 1
            assert db.query(User).filter(User.kakao_id == 900202).first() is None


def test_t03_kakaotalk_inapp_browser_detection_and_same_origin_proxy_flow(
    auth_env, monkeypatch: pytest.MonkeyPatch
):
    """
    T03:
    1. 로그아웃 후 재연결 세션(requires_reauth=True)에서 카카오톡 인앱 브라우저(User-Agent에 KAKAOTALK 포함)로
       /api/auth/kakao/login/url을 호출하면 kakao_in_app_browser=True, requires_external_browser=True를 반환해
       인가 요청 전에 외부 브라우저 열기 안내를 띄울 수 있게 한다.
    2. 일반 브라우저에서는 requires_external_browser=False를 반환한다.
    """
    async def fake_exchange(code: str, **kwargs):
        return {"access_token": f"access-{code}", "refresh_token": f"refresh-{code}", "expires_in": 3600}

    async def fake_user_info(access_token: str):
        return {"kakao_id": 900301, "nickname": "웹유저", "profile_image": None, "email": "web@kakao.com"}

    async def fake_logout(access_token: str, **kwargs):
        return None

    monkeypatch.setattr("app.auth.transitions.exchange_kakao_authorization_code", fake_exchange)
    monkeypatch.setattr("app.auth.transitions.get_kakao_user_info", fake_user_info)
    monkeypatch.setattr("app.auth.revocations.logout_kakao_access_token", fake_logout)

    client: TestClient = auth_env["client"]
    if True:
        boot = _bootstrap_web_uuid(client)
        jwt = boot["access_token"]
        state = client.get("/api/auth/kakao/login/url", headers={"Authorization": f"Bearer {jwt}"}).json()["state"]

        prep = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
            json={"action": "KAKAO_LINK", "code": "c1", "state": state},
        ).json()
        kakao_jwt = client.post(
            f"/api/auth/transitions/{prep['transition_id']}/commit",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt}"},
        ).json()["access_token"]

        prep_out = client.post(
            "/api/auth/transitions",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {kakao_jwt}"},
            json={"action": "LOGOUT"},
        ).json()
        reauth_jwt = client.post(
            f"/api/auth/transitions/{prep_out['transition_id']}/commit",
            headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {kakao_jwt}"},
        ).json()["access_token"]

        # 카카오톡 인앱 브라우저 User-Agent
        inapp_res = client.get(
            "/api/auth/kakao/login/url",
            headers={
                "Authorization": f"Bearer {reauth_jwt}",
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) KAKAOTALK 10.5.0",
            },
        )
        assert inapp_res.status_code == 200
        inapp_body = inapp_res.json()
        assert inapp_body["requires_reauth"] is True
        assert inapp_body["kakao_in_app_browser"] is True
        assert inapp_body["requires_external_browser"] is True

        # 일반 사파리 브라우저 User-Agent
        safari_res = client.get(
            "/api/auth/kakao/login/url",
            headers={
                "Authorization": f"Bearer {reauth_jwt}",
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile/15E148 Safari/604.1",
            },
        )
        assert safari_res.status_code == 200
        safari_body = safari_res.json()
        assert safari_body["requires_reauth"] is True
        assert safari_body["kakao_in_app_browser"] is False
        assert safari_body["requires_external_browser"] is False


def test_t04_expo_app_dual_transport_session_restore_and_system_browser_bridge_roundtrip(
    auth_env, monkeypatch: pytest.MonkeyPatch
):
    """
    T04-D01:
    1. Expo 모바일 앱은 브라우저 쿠키나 Origin 헤더 없이 X-Whatbob-Client: app, X-Whatbob-Request: 1 헤더로
       UUID 생성·세션 복원(X-Whatbob-Auth-Secret)·계정 전환(X-Whatbob-Transition-Secret)을 수행한다.
    2. 앱을 재시작해 쿠키가 전혀 없는 새 클라이언트에서도 SecureStore에 저장한 X-Whatbob-Auth-Secret 헤더만으로
       POST /api/auth/tokens를 호출해 기존 세션을 복원한다.
    3. GET /api/auth/kakao/app-callback 브릿지는 카카오가 보낸 code와 state를 즉시
       whatbob://auth/kakao/callback?code=...&state=... 로 302 Redirect하여 시스템 인증 창을 닫고 앱으로 복귀시킨다.
    4. 웹 브라우저 요청에는 X-Whatbob-Auth-Secret / X-Whatbob-Transition-Secret 헤더가 절대 노출되지 않는다.
    """
    captured_redirect_uris: list[str | None] = []

    async def fake_exchange(code: str, *, redirect_uri: str | None = None):
        captured_redirect_uris.append(redirect_uri)
        return {
            "access_token": f"app-access-{code}",
            "refresh_token": f"app-refresh-{code}",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400,
        }

    async def fake_user_info(access_token: str):
        return {
            "kakao_id": 900401,
            "nickname": "앱사용자",
            "profile_image": None,
            "email": "expo_app@kakao.com",
        }

    async def fake_logout(access_token: str, **kwargs):
        return None

    monkeypatch.setattr("app.auth.transitions.exchange_kakao_authorization_code", fake_exchange)
    monkeypatch.setattr("app.auth.transitions.get_kakao_user_info", fake_user_info)
    monkeypatch.setattr("app.auth.revocations.logout_kakao_access_token", fake_logout)

    # 웹 요청에서는 비밀열쇠 헤더가 노출되지 않는지 먼저 확인
    web_client = TestClient(app, base_url="https://testserver")
    try:
        web_prep = web_client.post("/api/auth/preparations", headers=WEB_CSRF_HEADERS)
        assert web_prep.status_code == 200
        assert "x-whatbob-auth-secret" not in {k.lower() for k in web_prep.headers.keys()}
    finally:
        web_client.close()

    # 1. Expo 앱 최초 실행: 쿠키 저장소를 쓰지 않고 헤더로만 비밀열쇠 수신
    app_client = TestClient(app, base_url="https://testserver")
    try:
        prep = app_client.post("/api/auth/preparations", headers=APP_CSRF_HEADERS)
        assert prep.status_code == 200
        auth_secret = prep.headers.get("X-Whatbob-Auth-Secret")
        assert auth_secret and len(auth_secret) == 43

        app_client.cookies.clear()
        created = app_client.post(
            "/api/auth/anonymous-users",
            headers={**APP_CSRF_HEADERS, "X-Whatbob-Auth-Secret": auth_secret},
        )
        assert created.status_code == 201
        app_uuid = created.json()["user"]["uuid"]
        app_jwt = created.json()["access_token"]
    finally:
        app_client.close()

    # 2. 앱 완전 재시작 시뮬레이션 (새 TestClient, 쿠키 0개, SecureStore에서 꺼낸 auth_secret만 전달)
    restarted_app = TestClient(app, base_url="https://testserver")
    try:
        restored = restarted_app.post(
            "/api/auth/tokens",
            headers={**APP_CSRF_HEADERS, "X-Whatbob-Auth-Secret": auth_secret},
        )
        assert restored.status_code == 200
        assert restored.json()["user"]["uuid"] == app_uuid
        app_jwt = restored.json()["access_token"]

        # 3. 앱에서 카카오 로그인 URL 요청 -> redirect_uri가 앱 브릿지 콜백 주소로 설정됨
        login_url_res = restarted_app.get(
            "/api/auth/kakao/login/url",
            headers={
                "X-Whatbob-Client": "app",
                "X-Whatbob-Auth-Secret": auth_secret,
            },
        )
        assert login_url_res.status_code == 200
        login_url_body = login_url_res.json()
        app_state = login_url_body["state"]
        login_qs = parse_qs(urlparse(login_url_body["login_url"]).query)
        assert login_qs["redirect_uri"] == [settings.KAKAO_APP_REDIRECT_URI]

        # 4. 시스템 인증 창에서 카카오가 https:// 브릿지(/api/auth/kakao/app-callback)로 리다이렉트 -> whatbob:// 스킴으로 302
        bridge_res = restarted_app.get(
            f"/api/auth/kakao/app-callback?code=app-code-1&state={app_state}",
            follow_redirects=False,
        )
        assert bridge_res.status_code == 302
        assert bridge_res.headers.get("cache-control") == "no-store"
        deep_link = bridge_res.headers["location"]
        assert deep_link.startswith("whatbob://auth/kakao/callback?")
        dl_qs = parse_qs(urlparse(deep_link).query)
        assert dl_qs["code"] == ["app-code-1"]
        assert dl_qs["state"] == [app_state]

        # 5. 앱으로 복귀 후 KAKAO_LINK prepare -> X-Whatbob-Transition-Secret 헤더 수신
        restarted_app.cookies.clear()
        prep_link = restarted_app.post(
            "/api/auth/transitions",
            headers={
                **APP_CSRF_HEADERS,
                "Authorization": f"Bearer {app_jwt}",
                "X-Whatbob-Auth-Secret": auth_secret,
            },
            json={
                "action": "KAKAO_LINK",
                "code": dl_qs["code"][0],
                "state": dl_qs["state"][0],
            },
        )
        assert prep_link.status_code == 200
        assert captured_redirect_uris[-1] == settings.KAKAO_APP_REDIRECT_URI
        transition_secret = prep_link.headers.get("X-Whatbob-Transition-Secret")
        assert transition_secret and len(transition_secret) == 43
        tid = prep_link.json()["transition_id"]
        assert prep_link.json()["target_account"] == {
            "nickname": "앱사용자",
            "masked_email": "ex***@kakao.com",
        }

        # 6. 쿠키 없이 X-Whatbob-Transition-Secret 헤더만으로 commit 확정
        restarted_app.cookies.clear()
        commit_link = restarted_app.post(
            f"/api/auth/transitions/{tid}/commit",
            headers={
                **APP_CSRF_HEADERS,
                "X-Whatbob-Transition-Secret": transition_secret,
            },
        )
        assert commit_link.status_code == 200
        new_app_auth_secret = commit_link.headers.get("X-Whatbob-Auth-Secret")
        assert new_app_auth_secret == transition_secret
        kakao_app_jwt = commit_link.json()["access_token"]

        # 7. 앱에서 LOGOUT prepare & commit -> 새 UUID 세션 열쇠 수신 및 requires_reauth=True
        restarted_app.cookies.clear()
        prep_logout = restarted_app.post(
            "/api/auth/transitions",
            headers={
                **APP_CSRF_HEADERS,
                "Authorization": f"Bearer {kakao_app_jwt}",
                "X-Whatbob-Auth-Secret": new_app_auth_secret,
            },
            json={"action": "LOGOUT"},
        )
        assert prep_logout.status_code == 200
        logout_trans_secret = prep_logout.headers.get("X-Whatbob-Transition-Secret")
        logout_tid = prep_logout.json()["transition_id"]

        restarted_app.cookies.clear()
        commit_logout = restarted_app.post(
            f"/api/auth/transitions/{logout_tid}/commit",
            headers={
                **APP_CSRF_HEADERS,
                "X-Whatbob-Transition-Secret": logout_trans_secret,
            },
        )
        assert commit_logout.status_code == 200
        assert commit_logout.json()["requires_reauth"] is True
        assert commit_logout.json()["kakao_logout_status"] in ("PENDING", "REVOKED")
        post_logout_secret = commit_logout.headers.get("X-Whatbob-Auth-Secret")
        assert post_logout_secret == logout_trans_secret

        # 8. 다시 앱 재시작 후 복원 시 requires_reauth=True 유지
        restarted_app.cookies.clear()
        restored_after_logout = restarted_app.post(
            "/api/auth/tokens",
            headers={**APP_CSRF_HEADERS, "X-Whatbob-Auth-Secret": post_logout_secret},
        )
        assert restored_after_logout.status_code == 200
        assert restored_after_logout.json()["requires_reauth"] is True

        # 9. 앱에서 확정 응답 유실 복구(POST /api/auth/transitions/{id}/recovery) 호출 시 헤더로 복구 동작
        recovered = restarted_app.post(
            f"/api/auth/transitions/{logout_tid}/recovery",
            headers={**APP_CSRF_HEADERS, "X-Whatbob-Transition-Secret": logout_trans_secret},
        )
        assert recovered.status_code == 200
        assert recovered.json()["requires_reauth"] is True
        assert recovered.headers.get("X-Whatbob-Auth-Secret") == post_logout_secret
    finally:
        restarted_app.close()


def test_t02_superseded_prepare_rejected_and_same_vs_different_account_reconnection(
    auth_env, monkeypatch: pytest.MonkeyPatch
):
    """
    User Stories 14, 15, 16:
    1. 동일 UUID 세션에서 두 번 연속 prepare(T1 -> T2)하면 이전 T1은 자동 CANCELLED 처리 및 토큰 파기되어
       늦게 도착한 T1 commit 요청이 409 TRANSITION_NOT_PREPARED로 거절된다.
    2. 로그아웃 후 같은 카카오 계정(Account A)으로 재연결하면 새 UUID 활동이 Account A의 기존 기록과 병합된다.
    3. 다시 로그아웃 후 다른 카카오 계정(Account B)으로 재연결하면 현재 UUID 활동만 Account B로 이관되고
       Account A의 기록은 그대로 보존된다.
    """
    profiles = {
        "code-a1": {"kakao_id": 900501, "nickname": "계정A", "profile_image": None, "email": "acc_a@kakao.com"},
        "code-a2": {"kakao_id": 900501, "nickname": "계정A", "profile_image": None, "email": "acc_a@kakao.com"},
        "code-b1": {"kakao_id": 900502, "nickname": "계정B", "profile_image": None, "email": "acc_b@kakao.com"},
    }

    async def fake_exchange(code: str, **kwargs):
        return {
            "access_token": f"access-{code}",
            "refresh_token": f"refresh-{code}",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400,
        }

    async def fake_user_info(access_token: str):
        code = access_token.replace("access-", "", 1)
        return profiles[code]

    async def fake_logout(access_token: str, **kwargs):
        return None

    monkeypatch.setattr("app.auth.transitions.exchange_kakao_authorization_code", fake_exchange)
    monkeypatch.setattr("app.auth.transitions.get_kakao_user_info", fake_user_info)
    monkeypatch.setattr("app.auth.revocations.logout_kakao_access_token", fake_logout)

    client: TestClient = auth_env["client"]
    boot = _bootstrap_web_uuid(client)
    jwt1 = boot["access_token"]
    uuid1 = boot["user"]["uuid"]

    from app.auth.sessions import utc_now
    with auth_env["SessionLocal"]() as db:
        u1 = db.query(User).filter(User.uuid == uuid1).one()
        db.add(UserMeal(user_id=u1.id, menu_id=auth_env["menu_id"], menu_name="김치찌개", eaten_at=utc_now()))
        db.commit()

    # 1. T1 prepare 후 곧바로 T2 prepare -> T1은 CANCELLED 및 토큰 파기
    s1 = client.get("/api/auth/kakao/login/url", headers={"Authorization": f"Bearer {jwt1}"}).json()["state"]
    prep_t1 = client.post(
        "/api/auth/transitions",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"},
        json={"action": "KAKAO_LINK", "code": "code-b1", "state": s1},
    ).json()
    t1_secret = client.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME)

    s2 = client.get("/api/auth/kakao/login/url", headers={"Authorization": f"Bearer {jwt1}"}).json()["state"]
    prep_t2 = client.post(
        "/api/auth/transitions",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"},
        json={"action": "KAKAO_LINK", "code": "code-a1", "state": s2},
    ).json()
    t2_secret = client.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME)

    # T1의 준비 쿠키로 뒤늦게 commit 시도 -> 409 TRANSITION_NOT_PREPARED 거절
    client.cookies.set(settings.TRANSITION_PREPARE_COOKIE_NAME, t1_secret, domain="testserver.local", path="/api/auth")
    late_commit = client.post(
        f"/api/auth/transitions/{prep_t1['transition_id']}/commit",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"},
    )
    assert late_commit.status_code == 409
    assert late_commit.json()["detail"]["code"] == "TRANSITION_NOT_PREPARED"

    # T2로 Account A 확정 -> 식사 기록 1건
    client.cookies.set(settings.TRANSITION_PREPARE_COOKIE_NAME, t2_secret, domain="testserver.local", path="/api/auth")
    commit_a1 = client.post(
        f"/api/auth/transitions/{prep_t2['transition_id']}/commit",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt1}"},
    ).json()
    user_a_id = commit_a1["user"]["id"]
    jwt_a1 = commit_a1["access_token"]

    # 2. 로그아웃 후 새 UUID에서 식사 1건 추가 -> 같은 계정(Account A) 재연결 시 총 2건으로 병합
    out1 = client.post(
        "/api/auth/transitions",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt_a1}"},
        json={"action": "LOGOUT"},
    ).json()
    out1_commit = client.post(
        f"/api/auth/transitions/{out1['transition_id']}/commit",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt_a1}"},
    ).json()
    jwt_uuid2 = out1_commit["access_token"]
    uuid2 = out1_commit["user"]["uuid"]

    with auth_env["SessionLocal"]() as db:
        u2 = db.query(User).filter(User.uuid == uuid2).one()
        db.add(UserMeal(user_id=u2.id, menu_id=auth_env["menu_id"], menu_name="김치찌개", eaten_at=utc_now()))
        db.commit()

    sa2 = client.get("/api/auth/kakao/login/url", headers={"Authorization": f"Bearer {jwt_uuid2}"}).json()["state"]
    prep_a2 = client.post(
        "/api/auth/transitions",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt_uuid2}"},
        json={"action": "KAKAO_LINK", "code": "code-a2", "state": sa2},
    ).json()
    commit_a2 = client.post(
        f"/api/auth/transitions/{prep_a2['transition_id']}/commit",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt_uuid2}"},
    ).json()
    assert commit_a2["user"]["id"] == user_a_id
    jwt_a2 = commit_a2["access_token"]

    with auth_env["SessionLocal"]() as db:
        assert db.query(UserMeal).filter(UserMeal.user_id == user_a_id).count() == 2

    # 3. 다시 로그아웃 후 새 UUID에서 식사 1건 추가 -> 다른 계정(Account B)으로 연결 시 Account A는 2건 유지, Account B는 1건만 보유
    out2 = client.post(
        "/api/auth/transitions",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt_a2}"},
        json={"action": "LOGOUT"},
    ).json()
    out2_commit = client.post(
        f"/api/auth/transitions/{out2['transition_id']}/commit",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt_a2}"},
    ).json()
    jwt_uuid3 = out2_commit["access_token"]
    uuid3 = out2_commit["user"]["uuid"]

    with auth_env["SessionLocal"]() as db:
        u3 = db.query(User).filter(User.uuid == uuid3).one()
        db.add(UserMeal(user_id=u3.id, menu_id=auth_env["menu_id"], menu_name="김치찌개", eaten_at=utc_now()))
        db.commit()

    sb1 = client.get("/api/auth/kakao/login/url", headers={"Authorization": f"Bearer {jwt_uuid3}"}).json()["state"]
    prep_b1 = client.post(
        "/api/auth/transitions",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt_uuid3}"},
        json={"action": "KAKAO_LINK", "code": "code-b1", "state": sb1},
    ).json()
    commit_b1 = client.post(
        f"/api/auth/transitions/{prep_b1['transition_id']}/commit",
        headers={**WEB_CSRF_HEADERS, "Authorization": f"Bearer {jwt_uuid3}"},
    ).json()
    user_b_id = commit_b1["user"]["id"]
    assert user_b_id != user_a_id

    with auth_env["SessionLocal"]() as db:
        assert db.query(UserMeal).filter(UserMeal.user_id == user_a_id).count() == 2
        assert db.query(UserMeal).filter(UserMeal.user_id == user_b_id).count() == 1

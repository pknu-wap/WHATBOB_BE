from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.auth.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.rooms.models  # noqa: F401
from app.auth.models import AccountTransition, AuthSession
from app.auth.sessions import DEFAULT_PROFILE_IMAGE, create_uuid_user, new_session, utc_now
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.main import app
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    QuestionSelectionState,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
    UserFoodExclusion,
    UserMeal,
)
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


def _make_png_bytes(color: tuple[int, int, int] = (80, 140, 220)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (96, 96), color=color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def transition_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
            food_code="TR001",
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
        m2 = Menu(
            food_code="TR002",
            name="비빔밥",
            base_name="비빔밥",
            category_large="밥류",
            meal_type="lunch",
            has_soup=False,
            cooking_style="mixed",
            main_ingredient="rice_grain",
            flavor_intensity="mild",
            temperature="warm",
            heaviness="light",
            allergen_tags=["대두", "참깨"],
        )
        db.add_all([m1, m2])
        q1 = OrderSheetQuestion(
            question_code="Q1",
            question_text="따뜻한 국물이 있는 메뉴가 좋으신가요?",
            cluster_key="soup",
            options_json=[
                {"option_id": "OPT_Y", "label": "좋아요", "rules": [{"field": "has_soup", "op": "eq", "value": True}]},
                {"option_id": "OPT_N", "label": "상관없음", "rules": []},
            ],
            is_active=True,
        )
        db.add(q1)
        db.commit()
        db.refresh(m1)
        db.refresh(m2)
        db.refresh(q1)
        m1_id, m2_id, q1_id = m1.id, m2.id, q1.id

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, base_url="https://testserver")
    try:
        yield {
            "client": client,
            "SessionLocal": TestingSessionLocal,
            "upload_dir": upload_dir,
            "m1_id": m1_id,
            "m2_id": m2_id,
            "q1_id": q1_id,
        }
    finally:
        client.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_t06_first_kakao_link_moves_all_records_deletes_origin_and_supports_recovery(
    transition_env,
    monkeypatch: pytest.MonkeyPatch,
):
    client: TestClient = transition_env["client"]
    SessionLocal = transition_env["SessionLocal"]
    upload_dir: Path = transition_env["upload_dir"]
    m1_id = transition_env["m1_id"]
    m2_id = transition_env["m2_id"]

    with SessionLocal() as db:
        origin = create_uuid_user(db)
        db.commit()
        db.refresh(origin)
        origin_id = origin.id
        origin_uuid = origin.uuid
        origin_session, origin_secret, origin_token = _issue_session(db, origin)
        origin_sid = origin_session.id
        # guest 질문 순환 이력이 영향받지 않는지 확인하기 위해 guest 행 추가
        db.add(
            QuestionSelectionState(
                scope_key="guest:keep-me",
                topic_counts={"soup": 3},
                last_clusters=["soup"],
                updated_at=utc_now(),
            )
        )
        db.commit()

    auth_headers = {**CSRF_HEADERS, "Authorization": f"Bearer {origin_token}"}
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, origin_secret, domain="testserver.local", path="/api/auth")

    # 1. 원본 UUID 상태에서 닉네임 수정, 프로필 사진 업로드, 알레르기/섭취불가/비선호, 식사 2건(동일 메뉴·시각), 추천·피드백·질문 상태 생성
    assert client.patch("/api/users/me/nickname", json={"nickname": "원본닉네임01"}, headers=auth_headers).status_code == 200
    up = client.put(
        "/api/users/me/profile-image",
        files={"file": ("origin.png", _make_png_bytes(), "image/png")},
        headers=auth_headers,
    )
    assert up.status_code == 200
    uploaded_url = up.json()["profile_image"]
    uploaded_fname = uploaded_url.rsplit("/", 1)[-1]
    assert (upload_dir / uploaded_fname).is_file()

    assert client.put("/api/users/me/allergies", json={"codes": ["egg"]}, headers=auth_headers).status_code == 200
    assert client.put("/api/users/me/unables", json={"codes": ["beef"]}, headers=auth_headers).status_code == 200
    assert client.put("/api/users/me/dislikes", json={"keywords": ["고수", "민트초코"]}, headers=auth_headers).status_code == 200

    same_time = "2026-11-13T12:00:00Z"
    meal1 = client.post("/api/users/me/meals", json={"menu_id": m1_id, "eaten_at": same_time}, headers=auth_headers)
    meal2 = client.post("/api/users/me/meals", json={"menu_id": m1_id, "eaten_at": same_time}, headers=auth_headers)
    assert meal1.status_code == 201 and meal2.status_code == 201
    meal1_id = meal1.json()["data"]["id"]
    meal2_id = meal2.json()["data"]["id"]

    q_res = client.get("/api/recommendations/order-sheet/questions", headers=auth_headers)
    assert q_res.status_code == 200
    rec = client.post(
        "/api/recommendations/order-sheet",
        json={"answers": [{"question_id": transition_env["q1_id"], "option_id": "OPT_Y"}], "top_k": 2},
        headers=auth_headers,
    )
    assert rec.status_code == 201
    rec_id = rec.json()["data"]["recommendation_id"]
    rec_menu_id = rec.json()["data"]["recommended_menus"][0]["menu_id"]

    fb = client.post(
        f"/api/recommendations/{rec_id}/feedback",
        json={"selected_menu_id": rec_menu_id, "is_satisfied": True},
        headers=auth_headers,
    )
    assert fb.status_code == 200
    fb_id = fb.json()["data"]["feedback_id"]

    # 2. 외부 카카오 인증 실패 시 원본 사용자·기록 그대로 유지 (User Story 20)
    kakao_call_count = {"count": 0}

    async def fake_resolve_kakao(code: str) -> dict:
        kakao_call_count["count"] += 1
        if code == "bad-code":
            from app.auth.errors import AuthError
            raise AuthError("KAKAO_AUTH_FAILED", "카카오 인가 코드 검증 실패", 400)
        return {
            "kakao_id": 777001,
            "nickname": "카카오기본이름",
            "profile_image": "https://k.kakaocdn.net/dn/default.jpg",
            "email": "user777001@example.com",
        }

    monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_resolve_kakao)

    fail_prep = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "bad-code"},
        headers=auth_headers,
    )
    assert fail_prep.status_code == 400
    assert client.get("/api/users/me", headers=auth_headers).status_code == 200

    # 3. 정상 준비(PREPARED) 및 준비 응답 유실 시 인가 코드 재소비 없는 재준비 확인
    op_key = "11111111-2222-4333-8444-555555555555"
    prep1 = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "one-time-code", "operation_key": op_key},
        headers=auth_headers,
    )
    assert prep1.status_code == 200
    transition_id = prep1.json()["transition_id"]
    assert kakao_call_count["count"] == 2  # bad-code 1회 + one-time-code 1회

    # 준비 응답이 유실되었다고 가정하고 동일 operation_key로 재요청 → 외부 카카오 호출 없이 새 준비 쿠키 발급
    prep2 = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "operation_key": op_key},
        headers=auth_headers,
    )
    assert prep2.status_code == 200
    assert prep2.json()["transition_id"] == transition_id
    assert kakao_call_count["count"] == 2  # 인가 코드 재소비 없음!
    prepare_secret = client.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME)
    assert prepare_secret is not None

    # 준비 단계에서는 원본 사용자와 기록이 아직 그대로 유지됨
    assert client.get("/api/users/me", headers=auth_headers).json()["uuid"] == origin_uuid

    # 4. 확정 중 DB 실패 주입 시 전체 롤백되어 원본 사용자·기록이 보존되는지 확인
    def fail_rebuild(*args, **kwargs):
        raise RuntimeError("Simulated mid-migration failure")

    monkeypatch.setattr("app.auth.transitions._rebuild_feedback_states_for_recommendations", fail_rebuild)
    with pytest.raises(RuntimeError, match="Simulated mid-migration failure"):
        client.post(f"/api/auth/transitions/{transition_id}/commit", headers=CSRF_HEADERS)

    # 롤백 후 원본 사용자와 식사·추천·파일이 그대로 살아있어야 함
    assert client.get("/api/users/me", headers=auth_headers).status_code == 200
    assert (upload_dir / uploaded_fname).is_file()
    monkeypatch.undo()
    # 다시 fake_resolve_kakao 복원
    monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_resolve_kakao)

    # 5. 정상 commit 실행
    commit_res = client.post(
        f"/api/auth/transitions/{transition_id}/commit",
        headers={**CSRF_HEADERS, "X-Whatbob-Auth-Context": str(origin_sid)},
    )
    assert commit_res.status_code == 200
    body = commit_res.json()
    assert body["status"] == "COMMITTED"
    assert body["is_new_user"] is True
    target_user_dto = body["user"]
    target_id = target_user_dto["id"]
    target_token = body["access_token"]
    assert target_id != origin_id
    assert target_user_dto["kakao_id"] == 777001
    # 서비스 프로필(닉네임·업로드 사진)이 카카오 기본 프로필로 덮어써지지 않고 보존됨 (User Story 30)
    assert target_user_dto["nickname"] == "원본닉네임01"
    assert target_user_dto["profile_image"] == uploaded_url
    assert (upload_dir / uploaded_fname).is_file()

    # 6. 원본 User 및 원본 AuthSession이 물리 삭제되었고(R3A), 이전 JWT는 즉시 401 거절됨
    with SessionLocal() as db:
        assert db.get(User, origin_id) is None
        assert db.get(AuthSession, origin_sid) is None
        assert (
            db.query(QuestionSelectionState)
            .filter(QuestionSelectionState.scope_key == f"user:{origin_id}")
            .first()
            is None
        )
        assert (
            db.query(QuestionSelectionState)
            .filter(QuestionSelectionState.scope_key == f"user:{target_id}")
            .first()
            is not None
        )
        assert (
            db.query(QuestionSelectionState)
            .filter(QuestionSelectionState.scope_key == "guest:keep-me")
            .first()
            is not None
        )

    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {origin_token}"}).status_code == 401

    # 7. 대상 계정으로 모든 기록(알레르기·섭취불가·비선호·식사 2건 원래 ID·추천·피드백 원래 ID) 조회 확인
    target_headers = {**CSRF_HEADERS, "Authorization": f"Bearer {target_token}"}
    assert client.get("/api/users/me/allergies", headers=target_headers).json()["data"]["codes"] == ["egg"]
    assert client.get("/api/users/me/unables", headers=target_headers).json()["data"]["codes"] == ["beef"]
    assert len(client.get("/api/users/me/dislikes", headers=target_headers).json()["data"]["items"]) == 2
    meals_data = client.get("/api/users/me/meals", headers=target_headers).json()["data"]
    assert meals_data["total_count"] == 2
    assert {m["id"] for m in meals_data["items"]} == {meal1_id, meal2_id}

    with SessionLocal() as db:
        assert db.get(Recommendation, rec_id).user_id == target_id
        assert db.get(RecommendationFeedback, fb_id).user_id == target_id
        state = (
            db.query(RecommendationFeedbackState)
            .filter(RecommendationFeedbackState.recommendation_id == rec_id)
            .one()
        )
        assert state.user_id == target_id
        assert state.latest_feedback_id == fb_id

    # 8. commit 성공 후 응답 유실 복구(원본 물리 삭제 후에도 동작) 및 비밀 쿠키 없는 요청 차단 확인
    # 비밀 쿠키/토큰 없이 transition_id만 아는 클라이언트는 조회·복구 모두 401 거절
    unauth_client = TestClient(app, base_url="https://testserver")
    try:
        assert unauth_client.get(f"/api/auth/transitions/{transition_id}").status_code == 401
        assert (
            unauth_client.post(f"/api/auth/transitions/{transition_id}/recovery", headers=CSRF_HEADERS).status_code
            == 401
        )
    finally:
        unauth_client.close()

    # 비밀 쿠키를 보유한 브라우저는 recovery 호출로 동일한 대상 계정과 토큰을 복구함 (중복 생성 없음)
    recov = client.post(f"/api/auth/transitions/{transition_id}/recovery", headers=CSRF_HEADERS)
    assert recov.status_code == 200
    assert recov.json()["user"]["id"] == target_id
    assert recov.json()["user"]["uuid"] == target_user_dto["uuid"]


def test_t06_room_participation_blocks_origin_and_target_transitions(
    transition_env,
    monkeypatch: pytest.MonkeyPatch,
):
    client: TestClient = transition_env["client"]
    SessionLocal = transition_env["SessionLocal"]

    with SessionLocal() as db:
        u_origin = create_uuid_user(db)
        u_kakao = User(
            kakao_id=888001,
            nickname="타기기카카오",
            nickname_key="타기기카카오".encode("utf-8"),
            profile_image=DEFAULT_PROFILE_IMAGE,
        )
        db.add(u_kakao)
        db.commit()
        db.refresh(u_origin)
        db.refresh(u_kakao)
        _, s_origin, t_origin = _issue_session(db, u_origin)
        _, s_kakao, t_kakao = _issue_session(db, u_kakao)

    async def fake_kakao(code: str) -> dict:
        return {
            "kakao_id": 888001,
            "nickname": "타기기카카오",
            "profile_image": None,
            "email": "k888@example.com",
        }

    monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_kakao)

    h_origin = {**CSRF_HEADERS, "Authorization": f"Bearer {t_origin}"}
    h_kakao = {**CSRF_HEADERS, "Authorization": f"Bearer {t_kakao}"}

    # 1. 대상 카카오 계정이 다른 기기에서 방에 참여 중일 때 원본 UUID의 KAKAO_LINK 거절 (User Story 45)
    room_res = client.post("/api/rooms", json={"max_members": 4}, headers=h_kakao)
    assert room_res.status_code == 201
    kakao_room_id = room_res.json()["room_id"]

    client.cookies.set(settings.AUTH_COOKIE_NAME, s_origin)
    blocked_target = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "valid"},
        headers=h_origin,
    )
    assert blocked_target.status_code == 409
    assert blocked_target.json()["detail"]["code"] == "ROOM_PARTICIPATION_ACTIVE"

    # 대상 카카오 계정의 방장 60초 유예 만료 후에는 방이 닫히므로 제한 해제
    with SessionLocal() as db:
        from app.rooms.models import Room
        r = db.query(Room).filter(Room.public_id == kakao_room_id).one()
        r.host_disconnected_at = utc_now() - timedelta(seconds=65)
        db.commit()

    # 2. 이제 원본 사용자가 직접 방을 만든 상태(참여/유예 중)에서 전환 시도 → 409 ROOM_PARTICIPATION_ACTIVE (User Story 44, 53)
    r2 = client.post("/api/rooms", json={"max_members": 4}, headers=h_origin)
    assert r2.status_code == 201
    origin_room_id = r2.json()["room_id"]

    blocked_origin = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "valid"},
        headers=h_origin,
    )
    assert blocked_origin.status_code == 409
    assert blocked_origin.json()["detail"]["code"] == "ROOM_PARTICIPATION_ACTIVE"

    # 3. 유예 시간 경과로 참여 종료 후 다시 전환 가능 (User Story 54)
    with SessionLocal() as db:
        from app.rooms.models import Room
        r_orig = db.query(Room).filter(Room.public_id == origin_room_id).one()
        r_orig.host_disconnected_at = utc_now() - timedelta(seconds=65)
        db.commit()

    allowed = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "valid"},
        headers=h_origin,
    )
    assert allowed.status_code == 200


def test_t07_reverse_order_uuid_profile_priority_direct_edit_protection_and_over_100_dislikes_merge(
    transition_env,
    monkeypatch: pytest.MonkeyPatch,
):
    client: TestClient = transition_env["client"]
    SessionLocal = transition_env["SessionLocal"]

    t_early = datetime(2026, 11, 1, 10, 0, 0)
    t_late = datetime(2026, 11, 5, 10, 0, 0)

    with SessionLocal() as db:
        u_early = User(
            kakao_id=None,
            nickname="오래된UUID닉",
            nickname_key="오래된UUID닉".encode("utf-8"),
            profile_image="/uploads/profile-images/" + "1" * 32 + ".png",
            created_at=t_early,
            nickname_source_id=10,
            nickname_source_created_at=t_early,
            profile_image_source_id=10,
            profile_image_source_created_at=t_early,
        )
        u_late = User(
            kakao_id=None,
            nickname="최근UUID닉",
            nickname_key="최근UUID닉".encode("utf-8"),
            profile_image="/uploads/profile-images/" + "2" * 32 + ".png",
            created_at=t_late,
            nickname_source_id=20,
            nickname_source_created_at=t_late,
            profile_image_source_id=20,
            profile_image_source_created_at=t_late,
        )
        db.add_all([u_early, u_late])
        db.commit()
        db.refresh(u_early)
        db.refresh(u_late)
        u_early.nickname_source_uuid = u_early.uuid
        u_early.profile_image_source_uuid = u_early.uuid
        u_late.nickname_source_uuid = u_late.uuid
        u_late.profile_image_source_uuid = u_late.uuid

        # 각 UUID에 70개씩 서로 다른 비선호 음식 저장 (병합 시 130개 > 100개)
        for i in range(70):
            db.add(UserFoodExclusion(user_id=u_early.id, exclusion_type="DISLIKE", keyword=f"비선호음식_{i:03d}"))
        for i in range(60, 130):
            db.add(UserFoodExclusion(user_id=u_late.id, exclusion_type="DISLIKE", keyword=f"비선호음식_{i:03d}"))
        db.commit()

        _, s_early, tok_early = _issue_session(db, u_early)
        _, s_late, tok_late = _issue_session(db, u_late)

    async def fake_kakao(code: str) -> dict:
        return {
            "kakao_id": 999001,
            "nickname": "카카오프로필",
            "profile_image": None,
            "email": "merge999@example.com",
        }

    monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_kakao)

    # 1. 더 늦게 생성된 u_late가 먼저 카카오 연결
    client.cookies.set(settings.AUTH_COOKIE_NAME, s_late)
    p_late = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "code-late"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_late}"},
    )
    assert p_late.status_code == 200
    c_late = client.post(
        f"/api/auth/transitions/{p_late.json()['transition_id']}/commit",
        headers=CSRF_HEADERS,
    )
    assert c_late.status_code == 200
    assert c_late.json()["user"]["nickname"] == "최근UUID닉"

    # 2. 더 먼저 생성된 u_early가 나중에 같은 카카오 계정으로 연결 → 가장 오래된 UUID인 u_early 프로필로 갱신됨 (User Story 28)
    client.cookies.set(settings.AUTH_COOKIE_NAME, s_early)
    p_early = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "code-early"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_early}"},
    )
    assert p_early.status_code == 200
    c_early = client.post(
        f"/api/auth/transitions/{p_early.json()['transition_id']}/commit",
        headers=CSRF_HEADERS,
    )
    assert c_early.status_code == 200
    merged_user = c_early.json()["user"]
    merged_token = c_early.json()["access_token"]
    assert merged_user["nickname"] == "오래된UUID닉"
    assert merged_user["profile_image"] == "/uploads/profile-images/" + "1" * 32 + ".png"

    # 3. 중복 10개가 제거되고 130개 비선호가 무손실 보존되며, 100개 초과 상태에서도 조회 및 축소 편집 가능 (User Story 24, 25)
    h_merged = {**CSRF_HEADERS, "Authorization": f"Bearer {merged_token}"}
    dislikes_res = client.get("/api/users/me/dislikes", headers=h_merged)
    assert dislikes_res.status_code == 200
    assert dislikes_res.json()["data"]["total_count"] == 130

    reduced_keywords = [f"비선호음식_{i:03d}" for i in range(115)]
    edit_res = client.put("/api/users/me/dislikes", json={"keywords": reduced_keywords}, headers=h_merged)
    assert edit_res.status_code == 200
    assert edit_res.json()["data"]["total_count"] == 115

    # 4. 카카오 계정에서 닉네임을 직접 수정한 뒤에는 더 오래된 UUID가 병합되어도 카카오 편집 닉네임이 유지됨 (User Story 29)
    assert client.patch("/api/users/me/nickname", json={"nickname": "카카오직접수정닉"}, headers=h_merged).status_code == 200

    with SessionLocal() as db:
        t_oldest = datetime(2026, 10, 1, 10, 0, 0)
        u_oldest = User(
            kakao_id=None,
            nickname="최초UUID닉",
            nickname_key="최초UUID닉".encode("utf-8"),
            profile_image="/uploads/profile-images/" + "0" * 32 + ".png",
            created_at=t_oldest,
            nickname_source_id=1,
            nickname_source_created_at=t_oldest,
            profile_image_source_id=1,
            profile_image_source_created_at=t_oldest,
        )
        db.add(u_oldest)
        db.commit()
        db.refresh(u_oldest)
        _, s_oldest, tok_oldest = _issue_session(db, u_oldest)

    client.cookies.set(settings.AUTH_COOKIE_NAME, s_oldest)
    p_oldest = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "code-oldest"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_oldest}"},
    )
    c_oldest = client.post(
        f"/api/auth/transitions/{p_oldest.json()['transition_id']}/commit",
        headers=CSRF_HEADERS,
    )
    assert c_oldest.status_code == 200
    # 닉네임은 카카오에서 직접 편집했으므로 유지되고, 사진은 직접 편집하지 않았으므로 더 오래된 u_oldest 사진이 선택됨!
    assert c_oldest.json()["user"]["nickname"] == "카카오직접수정닉"
    assert c_oldest.json()["user"]["profile_image"] == "/uploads/profile-images/" + "0" * 32 + ".png"


def test_t08_logout_creates_new_uuid_preserves_other_device_session_and_remerges_on_relogin(
    transition_env,
    monkeypatch: pytest.MonkeyPatch,
):
    client: TestClient = transition_env["client"]
    SessionLocal = transition_env["SessionLocal"]
    m1_id = transition_env["m1_id"]
    m2_id = transition_env["m2_id"]

    with SessionLocal() as db:
        kakao_user = User(
            kakao_id=555001,
            nickname="기존카카오유저",
            nickname_key="기존카카오유저".encode("utf-8"),
            profile_image=DEFAULT_PROFILE_IMAGE,
            is_legacy_kakao=True,
        )
        db.add(kakao_user)
        db.commit()
        db.refresh(kakao_user)
        kakao_user_id = kakao_user.id
        _, secret_device_a, token_device_a = _issue_session(db, kakao_user)
        _, secret_device_b, token_device_b = _issue_session(db, kakao_user)

    h_a = {**CSRF_HEADERS, "Authorization": f"Bearer {token_device_a}"}
    h_b = {**CSRF_HEADERS, "Authorization": f"Bearer {token_device_b}"}

    # 카카오 계정에서 식사 1건 저장
    assert client.post("/api/users/me/meals", json={"menu_id": m1_id}, headers=h_a).status_code == 201

    # 1. 기기 A에서 LOGOUT 준비 및 확정 (User Story 41, 42)
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, secret_device_a, domain="testserver.local", path="/api/auth")
    prep_out = client.post("/api/auth/transitions", json={"action": "LOGOUT"}, headers=h_a)
    assert prep_out.status_code == 200
    commit_out = client.post(
        f"/api/auth/transitions/{prep_out.json()['transition_id']}/commit",
        headers=CSRF_HEADERS,
    )
    assert commit_out.status_code == 200
    new_uuid_user = commit_out.json()["user"]
    new_uuid_token = commit_out.json()["access_token"]
    new_uuid_secret = client.cookies.get(settings.AUTH_COOKIE_NAME, domain="testserver.local", path="/api/auth")

    assert new_uuid_user["id"] != kakao_user_id
    assert new_uuid_user["kakao_id"] is None
    assert new_uuid_user["profile_image"] == DEFAULT_PROFILE_IMAGE

    # 기기 A의 이전 토큰은 폐기됨(401), 반면 기기 B의 세션과 카카오 식사 기록은 그대로 유지됨!
    assert client.get("/api/users/me", headers=h_a).status_code == 401
    assert client.get("/api/users/me", headers=h_b).status_code == 200
    assert client.get("/api/users/me/meals", headers=h_b).json()["data"]["total_count"] == 1

    # 2. 기기 A의 새 UUID로 새 식사(m2_id)와 비선호 저장
    h_new_uuid = {**CSRF_HEADERS, "Authorization": f"Bearer {new_uuid_token}"}
    assert client.post("/api/users/me/meals", json={"menu_id": m2_id}, headers=h_new_uuid).status_code == 201
    assert client.put("/api/users/me/dislikes", json={"keywords": ["오이"]}, headers=h_new_uuid).status_code == 200

    # 3. 기기 A에서 다시 같은 카카오 계정으로 KAKAO_LINK → 새 UUID 활동이 기존 카카오 계정에 합쳐짐 (User Story 43)
    async def fake_kakao(code: str) -> dict:
        return {
            "kakao_id": 555001,
            "nickname": "카카오기본",
            "profile_image": None,
            "email": "k555@example.com",
        }

    monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_kakao)

    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, new_uuid_secret, domain="testserver.local", path="/api/auth")
    prep_in = client.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "relogin-code"},
        headers=h_new_uuid,
    )
    assert prep_in.status_code == 200
    commit_in = client.post(
        f"/api/auth/transitions/{prep_in.json()['transition_id']}/commit",
        headers=CSRF_HEADERS,
    )
    assert commit_in.status_code == 200
    relogged_user = commit_in.json()["user"]
    relogged_token = commit_in.json()["access_token"]
    assert relogged_user["id"] == kakao_user_id
    assert relogged_user["nickname"] == "기존카카오유저"

    h_relogged = {**CSRF_HEADERS, "Authorization": f"Bearer {relogged_token}"}
    meals_after = client.get("/api/users/me/meals", headers=h_relogged).json()["data"]
    assert meals_after["total_count"] == 2
    assert {m["menu_id"] for m in meals_after["items"]} == {m1_id, m2_id}
    dislikes_after = client.get("/api/users/me/dislikes", headers=h_relogged).json()["data"]
    assert [item["keyword"] for item in dislikes_after["items"]] == ["오이"]


def test_t06_t08_recovery_expiry_revoked_target_session_and_auth_context_mismatch(
    transition_env,
    monkeypatch: pytest.MonkeyPatch,
):
    client: TestClient = transition_env["client"]
    SessionLocal = transition_env["SessionLocal"]

    with SessionLocal() as db:
        u1 = create_uuid_user(db)
        db.commit()
        db.refresh(u1)
        u1_id = u1.id
        _, s1, t1 = _issue_session(db, u1)

    h1 = {**CSRF_HEADERS, "Authorization": f"Bearer {t1}"}
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, s1, domain="testserver.local", path="/api/auth")

    # 1. X-Whatbob-Auth-Context 불일치 시 409 AUTH_CONTEXT_CHANGED
    mismatch = client.post(
        "/api/auth/transitions",
        json={"action": "LOGOUT"},
        headers={**h1, "X-Whatbob-Auth-Context": "999999"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "AUTH_CONTEXT_CHANGED"

    # 2. 정상 LOGOUT 준비 및 확정
    prep = client.post("/api/auth/transitions", json={"action": "LOGOUT"}, headers=h1)
    assert prep.status_code == 200
    tid = prep.json()["transition_id"]
    commit = client.post(f"/api/auth/transitions/{tid}/commit", headers=CSRF_HEADERS)
    assert commit.status_code == 200

    # 3. 대상 세션이 폐기된 경우 recovery는 401 SESSION_REVOKED로 거절하며 원본을 재생성하지 않음
    with SessionLocal() as db:
        tr = db.query(AccountTransition).filter(AccountTransition.transition_id == tid).one()
        target_session = db.get(AuthSession, int(tr.result_session_id))
        target_session.revoked_at = utc_now()
        db.commit()

    revoked_recov = client.post(f"/api/auth/transitions/{tid}/recovery", headers=CSRF_HEADERS)
    assert revoked_recov.status_code == 401
    assert revoked_recov.json()["detail"]["code"] == "SESSION_REVOKED"

    # 4. 복구 기한(recovery_expires_at)이 지난 경우 401 TRANSITION_RECOVERY_EXPIRED 거절
    with SessionLocal() as db:
        tr = db.query(AccountTransition).filter(AccountTransition.transition_id == tid).one()
        target_session = db.get(AuthSession, int(tr.result_session_id))
        target_session.revoked_at = None
        tr.recovery_expires_at = utc_now() - timedelta(seconds=10)
        db.commit()

    expired_recov = client.post(f"/api/auth/transitions/{tid}/recovery", headers=CSRF_HEADERS)
    assert expired_recov.status_code == 401
    assert expired_recov.json()["detail"]["code"] == "TRANSITION_RECOVERY_EXPIRED"

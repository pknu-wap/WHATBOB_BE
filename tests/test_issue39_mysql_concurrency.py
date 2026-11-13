"""Real MySQL 8.4 InnoDB concurrency, migration, and end-to-end verification for Issue #39 (T01~T09).

Requires MYSQL_TEST_DATABASE_URL pointing to a dedicated MySQL test database.
Each test creates and drops its own isolated child schema.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

import app.auth.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.rooms.models  # noqa: F401
import app.users.models  # noqa: F401
from app.auth.models import AccountTransition, AuthSession, KakaoLinkGuard
from app.auth.sessions import (
    DEFAULT_PROFILE_IMAGE,
    cleanup_auth_sessions_once,
    create_uuid_user,
    new_session,
    utc_now,
)
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.main import app
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
    UserFoodExclusion,
    UserMeal,
)
from app.rooms.models import Room, RoomMember
from app.rooms.service import (
    expire_room_member_after_grace,
    mark_room_member_disconnected,
    mark_room_member_online,
)
from app.users.models import User
from scripts import migrate_issue39

CSRF_HEADERS = {
    "Origin": "http://localhost:8081",
    "X-Whatbob-Request": "1",
}


@pytest.fixture()
def mysql_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = os.getenv("MYSQL_TEST_DATABASE_URL")
    if not value:
        pytest.skip("MYSQL_TEST_DATABASE_URL is required for real MySQL tests")
    url = make_url(value)
    if not url.drivername.startswith("mysql") or not url.database or "test" not in url.database.lower():
        pytest.fail("MYSQL_TEST_DATABASE_URL must point to a dedicated MySQL test database")

    schema = f"{url.database[:28]}_i39_{uuid4().hex[:10]}"
    admin = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"))

    engine = create_engine(
        url.set(database=schema),
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        bind=engine,
    )
    upload_dir = tmp_path / "profile-images"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROFILE_IMAGE_UPLOAD_DIR", str(upload_dir))

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

    overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db
    try:
        yield {
            "engine": engine,
            "SessionLocal": TestingSessionLocal,
            "upload_dir": upload_dir,
            "schema": schema,
        }
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE `{schema}`"))
        admin.dispose()


def _issue_session(db: Session, user: User) -> tuple[AuthSession, str, str]:
    rec, secret = new_session(user_id=user.id)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    tok = create_access_token({"sub": str(user.id), "sid": str(rec.id)})
    return rec, secret, tok


def _new_client(cookie_secret: str | None = None, prepare_secret: str | None = None) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    if cookie_secret:
        c.cookies.set(settings.AUTH_COOKIE_NAME, cookie_secret, domain="testserver.local", path="/api/auth")
    if prepare_secret:
        c.cookies.set(
            settings.TRANSITION_PREPARE_COOKIE_NAME,
            prepare_secret,
            domain="testserver.local",
            path="/api/auth",
        )
    return c


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), color=color).save(buf, format="PNG")
    return buf.getvalue()


def test_mysql_01_02_03_04_concurrent_prep_nickname_exclusions_and_feedback(mysql_env):
    engine = mysql_env["engine"]
    SessionLocal = mysql_env["SessionLocal"]
    Base.metadata.create_all(engine)

    with SessionLocal() as db:
        prep_rec, prep_secret = new_session()
        db.add(prep_rec)
        u1 = create_uuid_user(db)
        u2 = create_uuid_user(db)
        m1 = Menu(
            food_code="MY001",
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
        q1 = OrderSheetQuestion(
            question_code="Q_MY1",
            question_text="국물이 좋으신가요?",
            cluster_key="soup",
            options_json=[
                {"option_id": "OPT_Y", "label": "좋아요", "rules": [{"field": "has_soup", "op": "eq", "value": True}]}
            ],
            is_active=True,
        )
        db.add_all([m1, q1])
        db.commit()
        _, s1, t1 = _issue_session(db, u1)
        _, s2, t2 = _issue_session(db, u2)
        u1_id, u2_id, m1_id = u1.id, u2.id, m1.id

        rec = Recommendation(
            user_id=u1_id,
            shown_question_ids=[q1.id],
            answers_json=[{"question_id": q1.id, "option_id": "OPT_Y"}],
            recommended_menu_ids=[m1_id],
        )
        db.add(rec)
        db.commit()
        rec_id = rec.id

    # 1. 같은 준비 쿠키로 동시 익명 사용자 생성: 정확히 1명만 생성 (201 + 200)
    b1 = threading.Barrier(2)

    def create_anon():
        c = _new_client(cookie_secret=prep_secret)
        try:
            b1.wait(timeout=10)
            r = c.post("/api/auth/anonymous-users", headers=CSRF_HEADERS)
            return r.status_code, r.json()["user"]["id"]
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        res1 = list(pool.map(lambda _: create_anon(), range(2)))
    assert sorted(code for code, _ in res1) == [200, 201]
    assert res1[0][1] == res1[1][1]

    # 2. 서로 다른 두 사용자가 동시에 같은 닉네임 저장: 하나는 200, 다른 하나는 409 NICKNAME_DUPLICATE
    b2 = threading.Barrier(2)

    def save_nick(token: str):
        c = _new_client()
        try:
            b2.wait(timeout=10)
            r = c.patch(
                "/api/users/me/nickname",
                json={"nickname": "동시경합닉네임"},
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        nick_codes = sorted(pool.map(save_nick, [t1, t2]))
    assert nick_codes == [200, 409]
    with SessionLocal() as db:
        matched = db.query(User).filter(User.nickname == "동시경합닉네임").all()
        assert len(matched) == 1
        assert matched[0].nickname_key == "동시경합닉네임".encode("utf-8")

    # 3. 같은 사용자의 제외 전체 교체 2개 동시 실행 + 다른 타입 보존
    with _new_client() as c:
        assert (
            c.put(
                "/api/users/me/allergies",
                json={"codes": ["egg"]},
                headers={"Authorization": f"Bearer {t1}"},
            ).status_code
            == 200
        )

    b3 = threading.Barrier(2)
    payloads = [["고수", "민트초코"], ["오이", "당근", "셀러리"]]

    def replace_dislikes(idx: int):
        c = _new_client()
        try:
            b3.wait(timeout=10)
            r = c.put(
                "/api/users/me/dislikes",
                json={"keywords": payloads[idx]},
                headers={"Authorization": f"Bearer {t1}"},
            )
            return r.status_code, [item["keyword"] for item in r.json()["data"]["items"]]
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        exc_results = list(pool.map(replace_dislikes, [0, 1]))
    assert [code for code, _ in exc_results] == [200, 200]
    with _new_client() as c:
        final_dislikes = [
            item["keyword"]
            for item in c.get("/api/users/me/dislikes", headers={"Authorization": f"Bearer {t1}"}).json()["data"]["items"]
        ]
        assert final_dislikes in payloads
        # 다른 타입(ALLERGY)은 그대로 보존됨
        assert (
            c.get("/api/users/me/allergies", headers={"Authorization": f"Bearer {t1}"}).json()["data"]["codes"]
            == ["egg"]
        )

    # 4. 같은 메뉴 평가 수정 2개 동시 실행: 이력은 2개 쌓여도 유효 상태(RecommendationFeedbackState)는 정확히 1표
    b4 = threading.Barrier(2)

    def send_feedback(is_sat: bool):
        c = _new_client()
        try:
            b4.wait(timeout=10)
            r = c.post(
                f"/api/recommendations/{rec_id}/feedback",
                json={"selected_menu_id": m1_id, "is_satisfied": is_sat},
                headers={"Authorization": f"Bearer {t1}"},
            )
            return r.status_code
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fb_codes = list(pool.map(send_feedback, [True, False]))
    assert fb_codes == [200, 200]

    with SessionLocal() as db:
        fb_rows = (
            db.query(RecommendationFeedback)
            .filter(RecommendationFeedback.recommendation_id == rec_id)
            .order_by(RecommendationFeedback.id.asc())
            .all()
        )
        state_rows = (
            db.query(RecommendationFeedbackState)
            .filter(RecommendationFeedbackState.recommendation_id == rec_id)
            .all()
        )
        assert len(fb_rows) == 2
        assert len(state_rows) == 1
        assert state_rows[0].latest_feedback_id == fb_rows[-1].id
        assert state_rows[0].is_satisfied == fb_rows[-1].is_satisfied


def test_mysql_05_06_07_08_09_10_11_transitions_rooms_records_and_sessions_races(
    mysql_env,
    monkeypatch: pytest.MonkeyPatch,
):
    engine = mysql_env["engine"]
    SessionLocal = mysql_env["SessionLocal"]
    Base.metadata.create_all(engine)

    with SessionLocal() as db:
        m1 = Menu(
            food_code="MY002",
            name="된장찌개",
            base_name="된장찌개",
            category_large="찌개/전골류",
            meal_type="lunch",
            has_soup=True,
            cooking_style="stew",
            main_ingredient="veggie_tofu",
            flavor_intensity="mild",
            temperature="hot",
            heaviness="light",
            allergen_tags=["대두"],
        )
        db.add(m1)
        db.commit()
        m1_id = m1.id

    # --- Scenario 7: 서로 다른 2개의 UUID가 같은 신규 kakao_id(700700)로 동시에 KAKAO_LINK commit ---
    with SessionLocal() as db:
        u_a = create_uuid_user(db)
        u_b = create_uuid_user(db)
        db.commit()
        db.add(UserMeal(user_id=u_a.id, menu_id=m1_id, menu_name="된장찌개"))
        db.add(UserMeal(user_id=u_b.id, menu_id=m1_id, menu_name="된장찌개"))
        db.add(UserFoodExclusion(user_id=u_a.id, exclusion_type="DISLIKE", keyword="가지"))
        db.add(UserFoodExclusion(user_id=u_b.id, exclusion_type="DISLIKE", keyword="피망"))
        db.commit()
        _, s_a, tok_a = _issue_session(db, u_a)
        _, s_b, tok_b = _issue_session(db, u_b)
        ua_id, ub_id = u_a.id, u_b.id

    async def fake_kakao_700(code: str) -> dict:
        return {
            "kakao_id": 700700,
            "nickname": "카카오700",
            "profile_image": None,
            "email": "k700@example.com",
        }

    monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_kakao_700)

    c_a = _new_client(cookie_secret=s_a)
    c_b = _new_client(cookie_secret=s_b)
    p_a = c_a.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "c-a"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_a}"},
    )
    p_b = c_b.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "c-b"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_b}"},
    )
    assert p_a.status_code == 200 and p_b.status_code == 200
    tid_a = p_a.json()["transition_id"]
    tid_b = p_b.json()["transition_id"]
    prep_sec_a = c_a.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME, domain="testserver.local", path="/api/auth")
    prep_sec_b = c_b.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME, domain="testserver.local", path="/api/auth")
    c_a.close()
    c_b.close()

    b7 = threading.Barrier(2)

    def commit_link(args):
        tid, psec = args
        client = _new_client(prepare_secret=psec)
        try:
            b7.wait(timeout=10)
            r = client.post(f"/api/auth/transitions/{tid}/commit", headers=CSRF_HEADERS)
            return r.status_code, r.json()
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        res7 = list(pool.map(commit_link, [(tid_a, prep_sec_a), (tid_b, prep_sec_b)]))
    assert [code for code, _ in res7] == [200, 200]
    assert res7[0][1]["user"]["id"] == res7[1][1]["user"]["id"]
    target_700_id = res7[0][1]["user"]["id"]

    with SessionLocal() as db:
        assert db.query(User).filter(User.kakao_id == 700700).count() == 1
        assert db.get(User, ua_id) is None
        assert db.get(User, ub_id) is None
        assert db.query(UserMeal).filter(UserMeal.user_id == target_700_id).count() == 2
        assert db.query(UserFoodExclusion).filter(UserFoodExclusion.user_id == target_700_id).count() == 2

    # --- Scenario 5 & 8: 방 생성/참여와 계정 전환(원본 및 대상) 동시 경합 ---
    with SessionLocal() as db:
        u_room_orig = create_uuid_user(db)
        db.commit()
        _, s_ro, tok_ro = _issue_session(db, u_room_orig)

    c_ro = _new_client(cookie_secret=s_ro)
    p_ro = c_ro.post(
        "/api/auth/transitions",
        json={"action": "LOGOUT"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_ro}"},
    )
    assert p_ro.status_code == 200
    tid_ro = p_ro.json()["transition_id"]
    psec_ro = c_ro.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME, domain="testserver.local", path="/api/auth")
    c_ro.close()

    b5 = threading.Barrier(2)

    def create_room_task():
        c = _new_client()
        try:
            b5.wait(timeout=10)
            return c.post(
                "/api/rooms",
                json={"max_members": 4},
                headers={"Authorization": f"Bearer {tok_ro}"},
            ).status_code
        finally:
            c.close()

    def commit_logout_task():
        c = _new_client(prepare_secret=psec_ro)
        try:
            b5.wait(timeout=10)
            return c.post(f"/api/auth/transitions/{tid_ro}/commit", headers=CSRF_HEADERS).status_code
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_room = pool.submit(create_room_task)
        f_out = pool.submit(commit_logout_task)
        room_code = f_room.result(timeout=15)
        out_code = f_out.result(timeout=15)

    # 둘 중 먼저 잠금을 획득한 쪽만 성공: 방 생성이 먼저면 전환은 409, 전환이 먼저면 방 생성은 401
    assert (room_code == 201 and out_code == 409) or (out_code == 200 and room_code == 401)

    # --- Scenario 6 & 9 & 11: 원본 식사 저장·토큰 갱신·타인 동일 닉네임 변경과 KAKAO_LINK 이관 동시 경합 ---
    with SessionLocal() as db:
        u_src = create_uuid_user(db)
        u_src.nickname = "이전될고유닉"
        u_src.nickname_key = "이전될고유닉".encode("utf-8")
        u_other = create_uuid_user(db)
        db.commit()
        _, s_src, tok_src = _issue_session(db, u_src)
        _, s_oth, tok_oth = _issue_session(db, u_other)
        u_src_id = u_src.id

    async def fake_kakao_900(code: str) -> dict:
        return {
            "kakao_id": 900900,
            "nickname": "카카오900",
            "profile_image": None,
            "email": "k900@example.com",
        }

    monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_kakao_900)

    c_src = _new_client(cookie_secret=s_src)
    p_src = c_src.post(
        "/api/auth/transitions",
        json={"action": "KAKAO_LINK", "code": "c-900"},
        headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_src}"},
    )
    assert p_src.status_code == 200
    tid_src = p_src.json()["transition_id"]
    psec_src = c_src.cookies.get(settings.TRANSITION_PREPARE_COOKIE_NAME, domain="testserver.local", path="/api/auth")
    c_src.close()

    b6 = threading.Barrier(4)

    def task_commit_migration():
        c = _new_client(prepare_secret=psec_src)
        try:
            b6.wait(timeout=10)
            r = c.post(f"/api/auth/transitions/{tid_src}/commit", headers=CSRF_HEADERS)
            return r.status_code, r.json()
        finally:
            c.close()

    def task_save_meal():
        c = _new_client()
        try:
            b6.wait(timeout=10)
            r = c.post(
                "/api/users/me/meals",
                json={"menu_id": m1_id},
                headers={"Authorization": f"Bearer {tok_src}"},
            )
            return r.status_code
        finally:
            c.close()

    def task_refresh_token():
        c = _new_client(cookie_secret=s_src)
        try:
            b6.wait(timeout=10)
            return c.post("/api/auth/tokens", headers=CSRF_HEADERS).status_code
        finally:
            c.close()

    def task_steal_nickname():
        c = _new_client()
        try:
            b6.wait(timeout=10)
            return c.patch(
                "/api/users/me/nickname",
                json={"nickname": "이전될고유닉"},
                headers={"Authorization": f"Bearer {tok_oth}"},
            ).status_code
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_mig = pool.submit(task_commit_migration)
        f_meal = pool.submit(task_save_meal)
        f_ref = pool.submit(task_refresh_token)
        f_nick = pool.submit(task_steal_nickname)
        mig_code, mig_body = f_mig.result(timeout=15)
        meal_code = f_meal.result(timeout=15)
        ref_code = f_ref.result(timeout=15)
        nick_code = f_nick.result(timeout=15)

    assert mig_code == 200
    target_900_id = mig_body["user"]["id"]
    assert meal_code in {201, 401}
    assert ref_code in {200, 401}
    assert nick_code == 409  # 원본->대상 닉네임 이전이 한 트랜잭션 안에서 수행되어 타인이 가로챌 수 없음!

    with SessionLocal() as db:
        assert db.get(User, u_src_id) is None
        meals_count = db.query(UserMeal).filter(UserMeal.user_id == target_900_id).count()
        assert meals_count == (1 if meal_code == 201 else 0)
        # 고아 UserMeal 없음
        assert db.query(UserMeal).filter(UserMeal.user_id == u_src_id).count() == 0

    # --- Scenario 10: 마지막 연결 종료·재접속·정리 작업 경합 ---
    with SessionLocal() as db:
        u_host = create_uuid_user(db)
        u_mem = create_uuid_user(db)
        db.commit()
        _, _, tok_host = _issue_session(db, u_host)
        _, _, tok_mem = _issue_session(db, u_mem)
        u_mem_id = u_mem.id

    with _new_client() as c:
        rm = c.post("/api/rooms", json={"max_members": 4}, headers={"Authorization": f"Bearer {tok_host}"})
        assert rm.status_code == 201
        pub_room_id = rm.json()["room_id"]
        assert c.post(f"/api/rooms/{pub_room_id}/join", headers={"Authorization": f"Bearer {tok_mem}"}).status_code == 201

    with SessionLocal() as db:
        assert mark_room_member_online(db, pub_room_id, u_mem_id) is True
        past_disc = datetime.now(timezone.utc) - timedelta(seconds=61)
        assert mark_room_member_disconnected(db, pub_room_id, u_mem_id, past_disc) is True

    b10 = threading.Barrier(2)

    def reconnect_member():
        with SessionLocal() as db:
            b10.wait(timeout=10)
            return mark_room_member_online(db, pub_room_id, u_mem_id)

    def expire_member():
        with SessionLocal() as db:
            b10.wait(timeout=10)
            return expire_room_member_after_grace(
                db, pub_room_id, u_mem_id, now=datetime.now(timezone.utc)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_rec = pool.submit(reconnect_member)
        f_exp = pool.submit(expire_member)
        rec_ok = f_rec.result(timeout=15)
        exp_res = f_exp.result(timeout=15)

    # 이미 61초가 지났으므로 재접속 시점에 지연 만료 정리되거나 정리 작업이 먼저 퇴장시킴 (둘 다 퇴장 처리, 중복 퇴장 없음)
    assert rec_ok is False


def test_mysql_migration_issue39_preserves_legacy_data_resumes_and_verifies(
    mysql_env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    engine = mysql_env["engine"]
    monkeypatch.setattr(migrate_issue39, "engine", engine)
    monkeypatch.setattr(migrate_issue39.base_migration, "engine", engine)

    # T01 이전 레거시 스키마 + 메뉴 삭제된 피드백 + 소유자 없는 피드백 + 미매핑 제외 조건 생성
    with engine.connect() as conn:
        conn.execute(
            text(
                """CREATE TABLE users (
                    id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    kakao_id BIGINT NOT NULL,
                    nickname VARCHAR(100) NULL,
                    email VARCHAR(255) NULL,
                    profile_image VARCHAR(500) NULL,
                    created_at DATETIME(6) NULL,
                    updated_at DATETIME(6) NULL ON UPDATE CURRENT_TIMESTAMP(6),
                    UNIQUE INDEX ix_users_kakao_id (kakao_id)
                ) ENGINE=InnoDB"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE menus (
                    id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    food_code VARCHAR(50) NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    base_name VARCHAR(100) NOT NULL,
                    main_ingredient VARCHAR(30) NOT NULL
                ) ENGINE=InnoDB"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE user_food_exclusions (
                    id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    exclusion_type VARCHAR(20) NOT NULL,
                    keyword VARCHAR(100) NOT NULL,
                    created_at DATETIME(6) NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                ) ENGINE=InnoDB"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE recommendations (
                    id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    user_id INTEGER NULL,
                    shown_question_ids JSON NOT NULL,
                    answers_json JSON NOT NULL,
                    recommended_menu_ids JSON NOT NULL,
                    created_at DATETIME(6) NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                ) ENGINE=InnoDB"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE recommendation_feedbacks (
                    id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    recommendation_id INTEGER NOT NULL,
                    user_id INTEGER NULL,
                    selected_menu_id INTEGER NULL,
                    is_satisfied BOOLEAN NOT NULL,
                    feature_match_vector JSON NOT NULL,
                    created_at DATETIME(6) NULL,
                    FOREIGN KEY (recommendation_id) REFERENCES recommendations(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (selected_menu_id) REFERENCES menus(id) ON DELETE SET NULL
                ) ENGINE=InnoDB"""
            )
        )
        conn.execute(
            text(
                "INSERT INTO users (id, kakao_id, nickname, email, profile_image, created_at, updated_at) "
                "VALUES (10, 10001, '레거시유저', 'leg@example.com', '/leg.png', '2025-01-01', '2025-01-02')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO menus (id, food_code, name, base_name, main_ingredient) "
                "VALUES (5, 'M005', '불고기', '불고기', 'beef')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO user_food_exclusions (id, user_id, exclusion_type, keyword, created_at) "
                "VALUES (1, 10, 'ALLERGY', '미확인알레르기키워드', '2025-01-03')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO recommendations (id, user_id, shown_question_ids, answers_json, recommended_menu_ids, created_at) "
                "VALUES (20, 10, '[1]', '[]', '[5]', '2025-01-04'), "
                "       (21, NULL, '[1]', '[]', '[5]', '2025-01-05')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO recommendation_feedbacks "
                "(id, recommendation_id, user_id, selected_menu_id, is_satisfied, feature_match_vector, created_at) "
                "VALUES (30, 20, 10, 5, 0, '{}', '2025-01-04 12:00:00'), "
                "       (31, 20, 10, 5, 1, '{}', '2025-01-04 12:05:00'), "
                "       (32, 20, 10, NULL, 0, '{}', '2025-01-04 12:10:00'), "
                "       (33, 21, NULL, 5, 1, '{}', '2025-01-05 12:00:00')"
            )
        )
        conn.commit()

    env = {**os.environ, "DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    manifest_path = str(tmp_path / "issue39-manifest.json")

    def run_cli(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "scripts.migrate_issue39", *args, "--manifest", manifest_path],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    # 1. --check 성공 및 미매핑 제외/소유자 없는 피드백 카운트 확인
    chk = run_cli("--check")
    assert chk.returncode == 0, chk.stdout + chk.stderr
    chk_data = json.loads(chk.stdout)
    assert chk_data["unmapped_catalog_exclusions"] == 1
    assert chk_data["ownerless_feedbacks"] == 1

    # 2. --apply 시 --maintenance-confirmed 필수
    assert run_cli("--apply").returncode == 2

    # 3. 부분 적용(예: users.nickname_edited만 먼저 추가된 상태) 후 --apply --maintenance-confirmed 재실행 및 --verify
    with engine.connect() as conn:
        manifest = migrate_issue39.create_issue39_manifest(conn)
        Path(manifest_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        conn.execute(text("ALTER TABLE users ADD COLUMN nickname_edited BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.commit()

    app_res = run_cli("--apply", "--maintenance-confirmed")
    assert app_res.returncode == 0, app_res.stdout + app_res.stderr

    ver_res = run_cli("--verify")
    assert ver_res.returncode == 0, ver_res.stdout + ver_res.stderr

    # 멱등 재적용 확인
    reapp_res = run_cli("--apply", "--maintenance-confirmed")
    assert reapp_res.returncode == 0, reapp_res.stdout + reapp_res.stderr

    # 4. 파생 상태(recommendation_feedback_states)와 원본 보존 검증
    with engine.connect() as conn:
        states = (
            conn.execute(
                text(
                    "SELECT recommendation_id, menu_key, latest_feedback_id, is_satisfied "
                    "FROM recommendation_feedback_states ORDER BY id ASC"
                )
            )
            .mappings()
            .all()
        )
        # 추천 20의 메뉴 5('불고기')는 피드백 30->31 중 최신인 31(만족=1)로 1표만 생성되고, 메뉴 삭제(NULL)인 32는 'legacy:unknown'으로 1표 생성됨.
        # 소유자 없는 피드백 33(user_id=NULL)은 원본 테이블에 그대로 보존됨.
        assert len(states) == 2
        by_key = {s["menu_key"]: s for s in states}
        assert by_key["불고기"]["latest_feedback_id"] == 31
        assert bool(by_key["불고기"]["is_satisfied"]) is True
        assert by_key["legacy:unknown"]["latest_feedback_id"] == 32
        assert conn.execute(text("SELECT COUNT(*) FROM recommendation_feedbacks")).scalar_one() == 4


def test_mysql_end_to_end_user_journey_t01_to_t08(
    mysql_env,
    monkeypatch: pytest.MonkeyPatch,
):
    engine = mysql_env["engine"]
    SessionLocal = mysql_env["SessionLocal"]
    Base.metadata.create_all(engine)

    with SessionLocal() as db:
        m_bibim = Menu(
            food_code="E2E01",
            name="비빔밥",
            base_name="비빔밥",
            category_large="밥류",
            meal_type="lunch",
            has_soup=False,
            cooking_style="mixed",
            main_ingredient="beef",
            flavor_intensity="medium",
            temperature="hot",
            heaviness="medium",
            allergen_tags=["계란", "쇠고기", "참깨"],
        )
        m_kong = Menu(
            food_code="E2E02",
            name="콩나물국밥",
            base_name="콩나물국밥",
            category_large="국/탕류",
            meal_type="lunch",
            has_soup=True,
            cooking_style="soup",
            main_ingredient="veggie_tofu",
            flavor_intensity="mild",
            temperature="hot",
            heaviness="light",
            allergen_tags=["대두"],
        )
        q_e2e = OrderSheetQuestion(
            question_code="Q_E2E_1",
            question_text="국물이 좋으신가요?",
            cluster_key="soup",
            options_json=[
                {"option_id": "OPT_Y", "label": "좋아요", "rules": [{"field": "has_soup", "op": "eq", "value": True}]},
                {"option_id": "OPT_N", "label": "싫어요", "rules": [{"field": "has_soup", "op": "eq", "value": False}]},
            ],
            is_active=True,
        )
        db.add_all([m_bibim, m_kong, q_e2e])
        db.commit()
        m_kong_id = m_kong.id
        q_e2e_id = q_e2e.id

    client = _new_client()
    try:
        # 1. T01: UUID 세션 부트스트랩 (준비 쿠키 발급 -> 익명 사용자 생성)
        prep_boot = client.post("/api/auth/preparations", headers=CSRF_HEADERS)
        assert prep_boot.status_code == 200
        boot = client.post("/api/auth/anonymous-users", headers=CSRF_HEADERS)
        assert boot.status_code == 201
        tok1 = boot.json()["access_token"]
        uuid1 = boot.json()["user"]["uuid"]

        # 2. T02: 제외 조건 등록 + 식사 기록 + 추천 생성 및 피드백
        r_exc = client.put(
            "/api/users/me/allergies",
            json={"codes": ["egg"]},
            headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok1}"},
        )
        assert r_exc.status_code == 200
        assert r_exc.json()["data"]["items"][0]["code"] == "egg"

        r_meal = client.post(
            "/api/users/me/meals",
            json={"menu_id": m_kong_id},
            headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok1}"},
        )
        assert r_meal.status_code == 201

        r_rec = client.post(
            "/api/recommendations/order-sheet",
            json={"answers": [{"question_id": q_e2e_id, "option_id": "OPT_Y"}]},
            headers={"Authorization": f"Bearer {tok1}"},
        )
        assert r_rec.status_code == 201
        rec_data = r_rec.json()["data"]
        rec_id = rec_data["recommendation_id"]
        rec_menus = rec_data["recommended_menus"]
        assert all(m["name"] != "비빔밥" for m in rec_menus)

        r_fb = client.post(
            f"/api/recommendations/{rec_id}/feedback",
            json={
                "selected_menu_id": rec_menus[0]["menu_id"],
                "is_satisfied": True,
            },
            headers={"Authorization": f"Bearer {tok1}", "Idempotency-Key": "e2e-fb-1"},
        )
        assert r_fb.status_code == 200

        # 3. T04 & T05: 닉네임 변경 및 프로필 이미지 업로드
        r_nick = client.patch(
            "/api/users/me/nickname",
            json={"nickname": "마이SQL유저"},
            headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok1}"},
        )
        assert r_nick.status_code == 200
        assert r_nick.json()["nickname"] == "마이SQL유저"

        r_img = client.put(
            "/api/users/me/profile-image",
            files={"file": ("avatar.png", _png_bytes((10, 120, 220)), "image/png")},
            headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok1}"},
        )
        assert r_img.status_code == 200
        uploaded_img_url = r_img.json()["profile_image"]
        assert uploaded_img_url.startswith("/uploads/profile-images/")

        # 4. T03: 방 생성 후 자발적 퇴장
        r_room = client.post(
            "/api/rooms",
            json={"max_members": 2},
            headers={"Authorization": f"Bearer {tok1}"},
        )
        assert r_room.status_code == 201
        room_id = r_room.json()["room_id"]
        r_leave = client.delete(
            f"/api/rooms/{room_id}",
            headers={"Authorization": f"Bearer {tok1}"},
        )
        assert r_leave.status_code == 200

        # 5. T06 & T07: 카카오 연동(prepare -> commit)
        async def fake_kakao_e2e(code: str) -> dict:
            return {
                "kakao_id": 777888,
                "nickname": "카카오기본닉",
                "profile_image": None,
                "email": "e2e@example.com",
            }

        monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_kakao_e2e)
        prep_link = client.post(
            "/api/auth/transitions",
            json={"action": "KAKAO_LINK", "code": "e2e-code-1"},
            headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok1}"},
        )
        assert prep_link.status_code == 200
        tid_link = prep_link.json()["transition_id"]
        com_link = client.post(
            f"/api/auth/transitions/{tid_link}/commit",
            headers=CSRF_HEADERS,
        )
        assert com_link.status_code == 200
        tok_kakao = com_link.json()["access_token"]
        kakao_user = com_link.json()["user"]
        assert kakao_user["kakao_id"] == 777888
        assert kakao_user["nickname"] == "마이SQL유저"
        assert kakao_user["profile_image"] == uploaded_img_url

        # 6. T08: 로그아웃(prepare -> commit) 후 신규 UUID 발급 및 기존 카카오 계정 재진입 확인
        prep_out = client.post(
            "/api/auth/transitions",
            json={"action": "LOGOUT"},
            headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_kakao}"},
        )
        assert prep_out.status_code == 200
        tid_out = prep_out.json()["transition_id"]
        com_out = client.post(
            f"/api/auth/transitions/{tid_out}/commit",
            headers=CSRF_HEADERS,
        )
        assert com_out.status_code == 200
        tok_uuid2 = com_out.json()["access_token"]
        uuid2 = com_out.json()["user"]["uuid"]
        assert uuid2 != uuid1
        assert com_out.json()["user"]["kakao_id"] is None

        # 신규 UUID 상태에서 다시 동일 카카오 계정(777888)으로 진입 시 기존 데이터 보존 확인
        prep_re = client.post(
            "/api/auth/transitions",
            json={"action": "KAKAO_LINK", "code": "e2e-code-2"},
            headers={**CSRF_HEADERS, "Authorization": f"Bearer {tok_uuid2}"},
        )
        assert prep_re.status_code == 200
        tid_re = prep_re.json()["transition_id"]
        com_re = client.post(
            f"/api/auth/transitions/{tid_re}/commit",
            headers=CSRF_HEADERS,
        )
        assert com_re.status_code == 200
        tok_re = com_re.json()["access_token"]
        assert com_re.json()["user"]["uuid"] == kakao_user["uuid"]
        assert com_re.json()["user"]["nickname"] == "마이SQL유저"
        assert com_re.json()["user"]["profile_image"] == uploaded_img_url

        r_meals_after = client.get(
            "/api/users/me/meals",
            headers={"Authorization": f"Bearer {tok_re}"},
        )
        assert r_meals_after.status_code == 200
        meal_items = r_meals_after.json()["data"]["items"]
        assert len(meal_items) == 1
        assert meal_items[0]["menu_name"] == "콩나물국밥"
    finally:
        client.close()

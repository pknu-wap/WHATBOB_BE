from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.sessions import create_uuid_user
from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.main import app
from app.menus.food_kinds import DEFAULT_MENU_ILLUSTRATION_URL, classify_food_kind
from app.menus.models import FoodKind, Menu, UserMeal
from app.users.models import User
from scripts import migrate_issue62_food_kinds
from scripts.seed_menus import parse_and_clean_csv, seed_database
from tests.auth_helpers import session_token

RECENT_HOME_CARD_URL = "/api/users/me/home-card-recently-eaten-food-max-6"
MEALS_URL = "/api/users/me/meals"


@pytest.fixture
def sqlite_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("base_name", "expected_key", "expected_name"),
    [
        ("떡볶이", "tteokbokki", "떡볶이"),
        ("돈가스", "tonkatsu", "돈까스"),
        ("돈까스", "tonkatsu", "돈까스"),
        ("버거", "burger", "햄버거"),
        ("햄버거", "burger", "햄버거"),
        ("마라탕", "malatang", "마라탕"),
        ("스파게티", "pasta", "파스타"),
        ("파스타", "pasta", "파스타"),
        ("국밥", "gukbap", "국밥"),
        ("자장면", "jjajangmyeon", "짜장면"),
        ("짜장면", "jjajangmyeon", "짜장면"),
        ("제육볶음", "jeyuk_bokkeum", "제육볶음"),
        ("돼지고기볶음(제육볶음)", "jeyuk_bokkeum", "제육볶음"),
        ("김치찌개", "김치찌개", "김치찌개"),
        ("비빔밥", "비빔밥", "비빔밥"),
    ],
)
def test_classify_food_kind_merges_agreed_aliases_and_falls_back_to_base_name(
    base_name: str,
    expected_key: str,
    expected_name: str,
):
    kind_key, display_name = classify_food_kind(base_name=base_name)
    assert kind_key == expected_key
    assert display_name == expected_name


def test_seed_database_links_food_kinds_and_preserves_manual_mapping_and_meals_on_reseed(
    sqlite_db: Session,
    tmp_path: Path,
):
    csv_path = tmp_path / "sample_nutrition.csv"
    csv_path.write_text(
        "식품코드,식품명,식품대분류명,식품중분류명,식품소분류명,식품기원명,업체명,에너지(kcal),단백질(g),지방(g),탄수화물(g),당류(g),나트륨(mg),식품중량\n"
        "F-BURGER-1,버거_치즈버거,빵 및 과자류,버거,치즈버거,외식(분석함량),해당없음,260,13,11,27,4,520,220g\n"
        "F-BURGER-2,햄버거_불고기버거,빵 및 과자류,햄버거,불고기버거,외식(분석함량),해당없음,275,14,12,28,5,540,230g\n"
        "F-JJ-1,자장면_삼선자장면,면 및 만두류,자장면,삼선자장면,외식(분석함량),해당없음,185,7,5,31,3,480,600g\n"
        "F-JJ-2,짜장면_간짜장,면 및 만두류,짜장면,간짜장,외식(분석함량),해당없음,195,8,6,32,3,510,600g\n"
        "F-JEYUK-1,돼지고기볶음(제육볶음)_제육덮밥,볶음류,돼지고기볶음,제육볶음,외식(분석함량),해당없음,175,11,8,14,4,460,350g\n"
        "F-GUKBAP-1,국밥_돼지국밥,국 및 탕류,국밥,돼지국밥,외식(분석함량),해당없음,115,8,4,11,1,320,550g\n",
        encoding="utf-8-sig",
    )

    first_summary = seed_database(
        db=sqlite_db,
        csv_path=csv_path,
        include_beverages_and_bakery=False,
        use_llm=False,
    )
    assert first_summary["total_menus_in_db"] == 6
    assert first_summary["inserted_menus"] == 6

    menus_by_code = {m.food_code: m for m in sqlite_db.query(Menu).all()}
    assert all(m.food_kind_id is not None for m in menus_by_code.values())

    burger_kind = sqlite_db.get(FoodKind, menus_by_code["F-BURGER-1"].food_kind_id)
    assert burger_kind is not None
    assert burger_kind.kind_key == "burger"
    assert burger_kind.name == "햄버거"
    assert menus_by_code["F-BURGER-2"].food_kind_id == burger_kind.id

    jj_kind = sqlite_db.get(FoodKind, menus_by_code["F-JJ-1"].food_kind_id)
    assert jj_kind is not None
    assert jj_kind.kind_key == "jjajangmyeon"
    assert jj_kind.name == "짜장면"
    assert menus_by_code["F-JJ-2"].food_kind_id == jj_kind.id

    jeyuk_kind = sqlite_db.get(FoodKind, menus_by_code["F-JEYUK-1"].food_kind_id)
    assert jeyuk_kind is not None
    assert jeyuk_kind.kind_key == "jeyuk_bokkeum"
    assert jeyuk_kind.name == "제육볶음"

    # 수동 분류 변경 및 사용자 식사 기록 생성 후 재시드해도 보존되는지 검증
    custom_kind = FoodKind(kind_key="custom_special_burger", name="수제버거특선")
    user = User(kakao_id=12345, nickname="시드보존유저")
    sqlite_db.add_all([custom_kind, user])
    sqlite_db.flush()

    original_burger_menu_id = menus_by_code["F-BURGER-1"].id
    menus_by_code["F-BURGER-1"].food_kind_id = custom_kind.id
    meal = UserMeal(
        user_id=user.id,
        menu_id=original_burger_menu_id,
        menu_name="치즈버거",
    )
    sqlite_db.add(meal)
    sqlite_db.commit()

    second_summary = seed_database(
        db=sqlite_db,
        csv_path=csv_path,
        include_beverages_and_bakery=False,
        use_llm=False,
    )
    assert second_summary["total_menus_in_db"] == 6
    assert second_summary["inserted_menus"] == 0

    sqlite_db.expire_all()
    reloaded_menu = sqlite_db.query(Menu).filter(Menu.food_code == "F-BURGER-1").one()
    assert reloaded_menu.id == original_burger_menu_id
    assert reloaded_menu.food_kind_id == custom_kind.id

    reloaded_meal = sqlite_db.get(UserMeal, meal.id)
    assert reloaded_meal is not None
    assert reloaded_meal.menu_id == original_burger_menu_id
    assert reloaded_meal.menu_name == "치즈버거"


@pytest.fixture
def recent_card_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    overrides = app.dependency_overrides.copy()
    try:
        with Session(engine) as db:
            kakao_user = User(kakao_id=88001, nickname="카카오첫카드")
            uuid_user = create_uuid_user(db)
            other_user = User(kakao_id=88002, nickname="다른계정유저")

            menu_cheese_burger = Menu(
                food_code="RC-BURGER-01",
                name="버거_치즈버거",
                base_name="버거",
                category_large="빵 및 과자류",
                category_medium="버거",
            )
            menu_bulgogi_burger = Menu(
                food_code="RC-BURGER-02",
                name="햄버거_불고기버거",
                base_name="햄버거",
                category_large="빵 및 과자류",
                category_medium="햄버거",
            )
            menu_gukbap = Menu(
                food_code="RC-GUKBAP-01",
                name="국밥_돼지국밥",
                base_name="국밥",
                category_large="국 및 탕류",
                category_medium="국밥",
            )
            menu_tteokbokki = Menu(
                food_code="RC-TTEOK-01",
                name="떡볶이_로제떡볶이",
                base_name="떡볶이",
                category_large="볶음류",
                category_medium="떡볶이",
            )
            db.add_all(
                [
                    kakao_user,
                    other_user,
                    menu_cheese_burger,
                    menu_bulgogi_burger,
                    menu_gukbap,
                    menu_tteokbokki,
                ]
            )
            db.commit()

            app.dependency_overrides[get_db] = lambda: db
            yield SimpleNamespace(
                client=TestClient(app),
                db=db,
                kakao_user=kakao_user,
                uuid_user=uuid_user,
                other_user=other_user,
                menus={
                    "cheese_burger": menu_cheese_burger,
                    "bulgogi_burger": menu_bulgogi_burger,
                    "gukbap": menu_gukbap,
                    "tteokbokki": menu_tteokbokki,
                },
                kakao_headers={"Authorization": f"Bearer {session_token(db, kakao_user)}"},
                uuid_headers={"Authorization": f"Bearer {session_token(db, uuid_user)}"},
                other_headers={"Authorization": f"Bearer {session_token(db, other_user)}"},
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        engine.dispose()


def test_default_menu_illustration_static_file_is_served_with_http_200(recent_card_api):
    resp = recent_card_api.client.get(DEFAULT_MENU_ILLUSTRATION_URL)
    assert resp.status_code == 200
    assert "svg" in resp.headers.get("content-type", "").lower()


def test_recent_home_card_returns_empty_items_with_no_store_when_no_meals(recent_card_api):
    resp = recent_card_api.client.get(RECENT_HOME_CARD_URL, headers=recent_card_api.kakao_headers)
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.json() == {
        "success": True,
        "data": {"items": []},
        "message": None,
    }


def test_recent_home_card_returns_latest_valid_first_card_and_filters_deleted_and_future_meals(
    recent_card_api,
):
    cheese_burger = recent_card_api.menus["cheese_burger"]
    bulgogi_burger = recent_card_api.menus["bulgogi_burger"]
    gukbap = recent_card_api.menus["gukbap"]
    tteokbokki = recent_card_api.menus["tteokbokki"]

    # 버거와 햄버거는 동일한 food_kind_id에 연결되어야 한다.
    assert isinstance(cheese_burger.food_kind_id, int)
    assert cheese_burger.food_kind_id == bulgogi_burger.food_kind_id

    now = datetime.now(timezone.utc)
    t_past_1 = (now - timedelta(hours=3)).isoformat()
    t_past_2 = (now - timedelta(hours=2)).isoformat()
    t_deleted = (now - timedelta(hours=1)).isoformat()
    t_future = (now + timedelta(hours=5)).isoformat()

    # 1) 3시간 전 치즈버거
    r1 = recent_card_api.client.post(
        MEALS_URL,
        headers=recent_card_api.kakao_headers,
        json={"menu_id": cheese_burger.id, "eaten_at": t_past_1},
    )
    # 2) 2시간 전 불고기버거 (유효한 기록 중 가장 최신)
    r2 = recent_card_api.client.post(
        MEALS_URL,
        headers=recent_card_api.kakao_headers,
        json={"menu_id": bulgogi_burger.id, "eaten_at": t_past_2},
    )
    # 3) 1시간 전 떡볶이 (이후 카탈로그에서 삭제될 메뉴)
    r3 = recent_card_api.client.post(
        MEALS_URL,
        headers=recent_card_api.kakao_headers,
        json={"menu_id": tteokbokki.id, "eaten_at": t_deleted},
    )
    # 4) 5시간 뒤 국밥 (미래 식사 기록)
    r4 = recent_card_api.client.post(
        MEALS_URL,
        headers=recent_card_api.kakao_headers,
        json={"menu_id": gukbap.id, "eaten_at": t_future},
    )
    assert all(r.status_code == 201 for r in (r1, r2, r3, r4))

    # 카탈로그에서 떡볶이 메뉴 삭제
    recent_card_api.db.delete(tteokbokki)
    recent_card_api.db.commit()
    recent_card_api.db.expire_all()

    resp = recent_card_api.client.get(RECENT_HOME_CARD_URL, headers=recent_card_api.kakao_headers)
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    payload = resp.json()
    assert payload["success"] is True
    assert payload["message"] is None

    items = payload["data"]["items"]
    assert len(items) == 1
    card = items[0]
    assert card["food_kind_id"] == bulgogi_burger.food_kind_id
    assert card["menu_name"] == r2.json()["data"]["menu_name"]
    assert card["eaten_at"].endswith("Z") or card["eaten_at"].endswith("+00:00")
    parsed_eaten_at = datetime.fromisoformat(card["eaten_at"].replace("Z", "+00:00"))
    assert parsed_eaten_at == datetime.fromisoformat(t_past_2)
    assert card["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL

    # 반환된 상대 image_url이 실제로 HTTP 200으로 조회되는지 확인
    img_resp = recent_card_api.client.get(card["image_url"])
    assert img_resp.status_code == 200


def test_recent_home_card_breaks_ties_by_meal_id_desc(recent_card_api):
    cheese_burger = recent_card_api.menus["cheese_burger"]
    bulgogi_burger = recent_card_api.menus["bulgogi_burger"]
    same_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

    r_first = recent_card_api.client.post(
        MEALS_URL,
        headers=recent_card_api.kakao_headers,
        json={"menu_id": cheese_burger.id, "eaten_at": same_ts},
    )
    r_second = recent_card_api.client.post(
        MEALS_URL,
        headers=recent_card_api.kakao_headers,
        json={"menu_id": bulgogi_burger.id, "eaten_at": same_ts},
    )
    assert r_first.status_code == 201 and r_second.status_code == 201
    assert r_second.json()["data"]["id"] > r_first.json()["data"]["id"]

    resp = recent_card_api.client.get(RECENT_HOME_CARD_URL, headers=recent_card_api.kakao_headers)
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["menu_name"] == r_second.json()["data"]["menu_name"]


def test_recent_home_card_supports_uuid_user_and_isolates_accounts(recent_card_api):
    gukbap = recent_card_api.menus["gukbap"]
    cheese_burger = recent_card_api.menus["cheese_burger"]
    t_uuid = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    t_other = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    r_uuid = recent_card_api.client.post(
        MEALS_URL,
        headers=recent_card_api.uuid_headers,
        json={"menu_id": gukbap.id, "eaten_at": t_uuid},
    )
    r_other = recent_card_api.client.post(
        MEALS_URL,
        headers=recent_card_api.other_headers,
        json={"menu_id": cheese_burger.id, "eaten_at": t_other},
    )
    assert r_uuid.status_code == 201 and r_other.status_code == 201

    # UUID 사용자는 더 최신인 다른 사용자의 기록이 있어도 본인의 국밥 기록만 조회한다.
    uuid_resp = recent_card_api.client.get(
        f"{RECENT_HOME_CARD_URL}?user_id={recent_card_api.other_user.id}",
        headers=recent_card_api.uuid_headers,
    )
    assert uuid_resp.status_code == 200
    uuid_items = uuid_resp.json()["data"]["items"]
    assert len(uuid_items) == 1
    assert uuid_items[0]["food_kind_id"] == gukbap.food_kind_id
    assert uuid_items[0]["menu_name"] == r_uuid.json()["data"]["menu_name"]

    # 식사 기록이 없는 카카오 사용자는 빈 배열을 받는다.
    kakao_resp = recent_card_api.client.get(
        RECENT_HOME_CARD_URL,
        headers=recent_card_api.kakao_headers,
    )
    assert kakao_resp.status_code == 200
    assert kakao_resp.json()["data"]["items"] == []


@pytest.mark.parametrize(
    "token_kind",
    ["missing", "invalid", "expired", "unknown_user", "bad_subject", "wrong_scheme"],
)
def test_recent_home_card_requires_valid_bearer_token(recent_card_api, token_kind):
    headers = {}
    if token_kind == "invalid":
        headers["Authorization"] = "Bearer invalid"
    elif token_kind != "missing":
        subject = str(recent_card_api.kakao_user.id)
        if token_kind == "unknown_user":
            subject = "999999"
        elif token_kind == "bad_subject":
            subject = "not-an-id"
        expiry = timedelta(seconds=-1) if token_kind == "expired" else timedelta(minutes=5)
        token = create_access_token({"sub": subject}, expires_delta=expiry)
        headers["Authorization"] = f'{"Basic" if token_kind == "wrong_scheme" else "Bearer"} {token}'

    resp = recent_card_api.client.get(RECENT_HOME_CARD_URL, headers=headers)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_catalog_csv_backfill_links_all_7503_meal_menus_and_merges_385_burger_aliases():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            text(
                """CREATE TABLE food_kinds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind_key VARCHAR(100) NOT NULL UNIQUE,
                    name VARCHAR(100) NOT NULL,
                    representative_image_url VARCHAR(500) NULL,
                    created_at DATETIME NOT NULL
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE menus (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    food_code VARCHAR(50) NOT NULL UNIQUE,
                    name VARCHAR(200) NOT NULL,
                    base_name VARCHAR(100) NOT NULL,
                    food_kind_id INTEGER NULL
                )"""
            )
        )
        parsed = parse_and_clean_csv(include_beverages_and_bakery=False)
        assert len(parsed) == 7503
        conn.execute(
            text("INSERT INTO menus (food_code, name, base_name) VALUES (:food_code, :name, :base_name)"),
            [{"food_code": m["food_code"], "name": m["name"], "base_name": m["base_name"]} for m in parsed],
        )
        conn.commit()

        summary = migrate_issue62_food_kinds.backfill_food_kinds(conn)
        assert summary["backfilled_menus"] == 7503
        assert summary["preserved_menus"] == 0

        stats = migrate_issue62_food_kinds.inspect_issue62_data(conn, final=True)
        assert stats["total_menus"] == 7503
        assert stats["unlinked_menus"] == 0
        assert stats["invalid_linked_menus"] == 0

        counts_by_key = dict(
            conn.execute(
                text(
                    "SELECT fk.kind_key, COUNT(m.id) "
                    "FROM food_kinds fk "
                    "LEFT JOIN menus m ON m.food_kind_id = fk.id "
                    "GROUP BY fk.kind_key"
                )
            ).all()
        )
        assert counts_by_key["burger"] == 385
        assert counts_by_key["tteokbokki"] == 42
        assert counts_by_key["tonkatsu"] == 8
        assert counts_by_key["malatang"] == 2
        assert counts_by_key["pasta"] == 49
        assert counts_by_key["gukbap"] == 7
        assert counts_by_key["jjajangmyeon"] == 3
        assert counts_by_key["jeyuk_bokkeum"] == 4
    engine.dispose()


@pytest.fixture
def mysql_engine():
    value = os.getenv("MYSQL_TEST_DATABASE_URL")
    if not value:
        pytest.skip("MYSQL_TEST_DATABASE_URL is required for real MySQL tests")
    url = make_url(value)
    if not url.drivername.startswith("mysql") or not url.database or "test" not in url.database.lower():
        pytest.fail("MYSQL_TEST_DATABASE_URL must point to a dedicated MySQL test database")
    schema = f"{url.database[:28]}_i62_{uuid4().hex[:10]}"
    admin = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    engine = create_engine(url.set(database=schema), pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE `{schema}`"))
        admin.dispose()


def _create_pre_issue62_mysql_schema(conn) -> None:
    conn.execute(
        text(
            """CREATE TABLE users (
                id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
                kakao_id BIGINT NULL,
                nickname VARCHAR(100) NULL
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
                category_large VARCHAR(50) NOT NULL DEFAULT '기타',
                category_medium VARCHAR(50) NULL,
                category_small VARCHAR(50) NULL,
                origin_type VARCHAR(50) NOT NULL DEFAULT 'restaurant',
                meal_type VARCHAR(30) NOT NULL DEFAULT 'main_meal',
                has_soup BOOLEAN NOT NULL DEFAULT FALSE,
                cooking_style VARCHAR(30) NOT NULL DEFAULT 'other',
                main_ingredient VARCHAR(30) NOT NULL DEFAULT 'other',
                flavor_intensity VARCHAR(30) NOT NULL DEFAULT 'mild_clean',
                temperature VARCHAR(20) NOT NULL DEFAULT 'hot',
                heaviness VARCHAR(20) NOT NULL DEFAULT 'moderate',
                kcal FLOAT NULL,
                protein_g FLOAT NULL,
                fat_g FLOAT NULL,
                carb_g FLOAT NULL,
                sugar_g FLOAT NULL,
                sodium_mg FLOAT NULL,
                allergen_tags JSON NOT NULL,
                created_at DATETIME NULL,
                UNIQUE INDEX ix_menus_food_code (food_code)
            ) ENGINE=InnoDB"""
        )
    )
    conn.execute(
        text(
            """CREATE TABLE user_meals (
                id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                menu_id INTEGER NULL,
                menu_name VARCHAR(200) NOT NULL,
                eaten_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (menu_id) REFERENCES menus(id) ON DELETE SET NULL
            ) ENGINE=InnoDB"""
        )
    )
    conn.execute(text("INSERT INTO users (id, kakao_id, nickname) VALUES (1, 1001, '기존식사유저')"))
    conn.execute(
        text(
            "INSERT INTO menus (id, food_code, name, base_name, category_large, allergen_tags, created_at) VALUES "
            "(101, 'M-BG-01', '버거_치즈버거', '버거', '빵 및 과자류', '[]', '2026-11-01 00:00:00'), "
            "(102, 'M-BG-02', '햄버거_불고기버거', '햄버거', '빵 및 과자류', '[]', '2026-11-01 00:00:00'), "
            "(103, 'M-JJ-01', '자장면_삼선자장면', '자장면', '면 및 만두류', '[]', '2026-11-01 00:00:00'), "
            "(104, 'M-JJ-02', '짜장면_간짜장', '짜장면', '면 및 만두류', '[]', '2026-11-01 00:00:00'), "
            "(105, 'M-JY-01', '돼지고기볶음(제육볶음)_제육덮밥', '돼지고기볶음(제육볶음)', '볶음류', '[]', '2026-11-01 00:00:00'), "
            "(106, 'M-KC-01', '김치찌개_돼지고기', '김치찌개', '찌개 및 전골류', '[]', '2026-11-01 00:00:00')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO user_meals (id, user_id, menu_id, menu_name, eaten_at, created_at) VALUES "
            "(501, 1, 101, '치즈버거', '2026-11-18 12:00:00', '2026-11-18 12:00:00'), "
            "(502, 1, NULL, '삭제된옛메뉴', '2026-11-17 12:00:00', '2026-11-17 12:00:00')"
        )
    )
    conn.commit()


def test_mysql_issue62_migration_backfills_by_food_code_preserves_manual_links_meals_and_enforces_constraints(
    mysql_engine,
    tmp_path: Path,
):
    with mysql_engine.connect() as conn:
        _create_pre_issue62_mysql_schema(conn)

    env = {
        **os.environ,
        "DATABASE_URL": mysql_engine.url.render_as_string(hide_password=False),
        "PYTHONPATH": str(Path(sys.executable).resolve().parent.parent.parent / ".tools" / "venv" / "Lib" / "site-packages"),
    }
    manifest_path = str(tmp_path / "issue62-manifest.json")

    def run_cli(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "scripts.migrate_issue62_food_kinds", *args, "--manifest", manifest_path],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    # 1. --check 성공
    chk = run_cli("--check")
    assert chk.returncode == 0, chk.stdout + chk.stderr
    chk_data = json.loads(chk.stdout)
    assert chk_data["total_menus"] == 6
    assert chk_data["unlinked_menus"] == 6
    assert chk_data["total_user_meals"] == 2

    # 2. --apply 시 --maintenance-confirmed 필수
    assert run_cli("--apply").returncode == 2

    # 3. 부분 중단 및 수동 매핑 시뮬레이션 후 --apply --maintenance-confirmed 실행
    with mysql_engine.connect() as conn:
        FoodKind.__table__.create(conn)
        conn.execute(text("ALTER TABLE menus ADD COLUMN food_kind_id INTEGER NULL"))
        conn.execute(
            text(
                "INSERT INTO food_kinds (id, kind_key, name, representative_image_url, created_at) "
                "VALUES (77, 'manual_special_stew', '특선김치전골', NULL, '2026-11-19 00:00:00')"
            )
        )
        conn.execute(text("UPDATE menus SET food_kind_id = 77 WHERE food_code = 'M-KC-01'"))
        conn.commit()
        manifest = migrate_issue62_food_kinds.create_issue62_manifest(conn)
        Path(manifest_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    app_res = run_cli("--apply", "--maintenance-confirmed")
    assert app_res.returncode == 0, app_res.stdout + app_res.stderr

    ver_res = run_cli("--verify")
    assert ver_res.returncode == 0, ver_res.stdout + ver_res.stderr

    # 4. 멱등 재적용 및 재시드 후에도 음식 종류 ID, 수동 매핑, 식사 기록이 보존되는지 검증
    reapp_res = run_cli("--apply", "--maintenance-confirmed")
    assert reapp_res.returncode == 0, reapp_res.stdout + reapp_res.stderr

    with mysql_engine.connect() as conn:
        menu_rows = {
            r["food_code"]: r
            for r in conn.execute(
                text(
                    "SELECT m.id, m.food_code, m.food_kind_id, fk.kind_key, fk.name AS kind_name "
                    "FROM menus m JOIN food_kinds fk ON fk.id = m.food_kind_id"
                )
            ).mappings()
        }
        assert menu_rows["M-BG-01"]["kind_key"] == "burger"
        assert menu_rows["M-BG-01"]["kind_name"] == "햄버거"
        assert menu_rows["M-BG-02"]["food_kind_id"] == menu_rows["M-BG-01"]["food_kind_id"]

        assert menu_rows["M-JJ-01"]["kind_key"] == "jjajangmyeon"
        assert menu_rows["M-JJ-02"]["food_kind_id"] == menu_rows["M-JJ-01"]["food_kind_id"]

        assert menu_rows["M-JY-01"]["kind_key"] == "jeyuk_bokkeum"
        # 수동 매핑 보존 확인
        assert menu_rows["M-KC-01"]["food_kind_id"] == 77
        assert menu_rows["M-KC-01"]["kind_key"] == "manual_special_stew"

        # 식사 기록 보존 확인
        meals = conn.execute(text("SELECT id, user_id, menu_id, menu_name FROM user_meals ORDER BY id")).mappings().all()
        assert [dict(m) for m in meals] == [
            {"id": 501, "user_id": 1, "menu_id": 101, "menu_name": "치즈버거"},
            {"id": 502, "user_id": 1, "menu_id": None, "menu_name": "삭제된옛메뉴"},
        ]

        # NOT NULL 및 외래 키 제약 강제 확인
        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE menus SET food_kind_id = NULL WHERE food_code = 'M-BG-01'"))
        conn.rollback()

        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE menus SET food_kind_id = 999999 WHERE food_code = 'M-BG-01'"))
        conn.rollback()

    # 5. MySQL 스키마에서 seed_database 재실행 후에도 음식 종류 ID, 수동 매핑, 식사 기록 보존 확인
    Base.metadata.create_all(mysql_engine)
    reseed_csv = tmp_path / "mysql_reseed.csv"
    reseed_csv.write_text(
        "식품코드,식품명,식품대분류명,식품중분류명,식품소분류명,식품기원명,업체명,에너지(kcal),단백질(g),지방(g),탄수화물(g),당류(g),나트륨(mg),식품중량\n"
        "M-BG-01,버거_치즈버거,빵 및 과자류,버거,치즈버거,외식(분석함량),해당없음,260,13,11,27,4,520,220g\n"
        "M-KC-01,김치찌개_돼지고기,찌개 및 전골류,김치찌개,돼지고기,외식(분석함량),해당없음,140,9,6,8,2,510,450g\n",
        encoding="utf-8-sig",
    )
    burger_fk_id_before = menu_rows["M-BG-01"]["food_kind_id"]
    with Session(mysql_engine) as mysql_db:
        seed_database(
            db=mysql_db,
            csv_path=reseed_csv,
            include_beverages_and_bakery=False,
            use_llm=False,
        )
        m_bg = mysql_db.query(Menu).filter(Menu.food_code == "M-BG-01").one()
        m_kc = mysql_db.query(Menu).filter(Menu.food_code == "M-KC-01").one()
        assert m_bg.food_kind_id == burger_fk_id_before
        assert m_kc.food_kind_id == 77
        assert mysql_db.query(UserMeal).count() == 2


def test_mysql_issue62_migration_halts_before_constraints_when_invalid_food_kind_link_exists(
    mysql_engine,
):
    with mysql_engine.connect() as conn:
        _create_pre_issue62_mysql_schema(conn)
        FoodKind.__table__.create(conn)
        conn.execute(text("ALTER TABLE menus ADD COLUMN food_kind_id INTEGER NULL"))
        conn.execute(text("UPDATE menus SET food_kind_id = 999999 WHERE food_code = 'M-BG-01'"))
        conn.commit()

        manifest = migrate_issue62_food_kinds.create_issue62_manifest(conn)
        with pytest.raises(migrate_issue62_food_kinds.MigrationError, match="invalid"):
            migrate_issue62_food_kinds.apply_issue62_migration(conn, manifest)

        # 제약 적용이 보류되어 menus.food_kind_id가 여전히 nullable이고 FK가 추가되지 않았는지 확인
        col_info = {c["name"]: c for c in inspect(conn).get_columns("menus")}["food_kind_id"]
        assert col_info["nullable"] is True
        assert inspect(conn).get_foreign_keys("menus") == []



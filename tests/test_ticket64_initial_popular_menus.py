from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.sessions import create_uuid_user
from app.core.database import Base, get_db
from app.main import app
from app.menus.food_kinds import DEFAULT_MENU_ILLUSTRATION_URL
from app.menus.models import FoodKind, InitialPopularFoodKind, Menu, UserMeal
from app.menus.popular import (
    DEFAULT_INITIAL_POPULAR_KIND_KEYS,
    ensure_initial_popular_food_kinds,
    seed_initial_popular_food_kinds,
)
from app.users.models import User
from scripts import migrate_issue62_food_kinds
from scripts.seed_menus import seed_database
from tests.auth_helpers import session_token

POPULAR_MENUS_URL = "/api/menus/popular"
RECENT_HOME_CARD_URL = "/api/users/me/home-card-recently-eaten-food-max-6"

EXPECTED_INITIAL_POPULAR_SPECS: list[tuple[str, str]] = [
    ("tteokbokki", "떡볶이"),
    ("tonkatsu", "돈까스"),
    ("burger", "햄버거"),
    ("malatang", "마라탕"),
    ("pasta", "파스타"),
    ("gukbap", "국밥"),
    ("jjajangmyeon", "짜장면"),
    ("jeyuk_bokkeum", "제육볶음"),
]


@pytest.fixture
def sqlite_engine():
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
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def popular_api(sqlite_engine):
    overrides = dict(app.dependency_overrides)
    try:
        with Session(sqlite_engine) as db:
            app.dependency_overrides[get_db] = lambda: db
            yield SimpleNamespace(
                client=TestClient(app),
                db=db,
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def test_public_popular_menus_returns_initial_eight_in_agreed_order_even_with_scrambled_food_kind_ids(
    popular_api,
):
    db = popular_api.db
    # 의도적으로 합의 순서와 다른 순서로 FoodKind 행을 삽입해 ID 순서에 의존하지 않음을 검증한다.
    scrambled_specs = [
        ("jeyuk_bokkeum", "제육볶음"),
        ("gukbap", "국밥"),
        ("burger", "햄버거"),
        ("jjajangmyeon", "짜장면"),
        ("pasta", "파스타"),
        ("tteokbokki", "떡볶이"),
        ("malatang", "마라탕"),
        ("tonkatsu", "돈까스"),
    ]
    kind_id_by_key: dict[str, int] = {}
    for kind_key, name in scrambled_specs:
        fk = FoodKind(kind_key=kind_key, name=name)
        db.add(fk)
        db.flush()
        kind_id_by_key[kind_key] = fk.id
    db.commit()
    ensure_initial_popular_food_kinds(db)

    resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp.status_code == 200

    body = resp.json()
    assert body["success"] is True
    assert body["message"] is None

    data = body["data"]
    assert data["source"] == "initial"
    assert data["as_of"] is None

    items = data["items"]
    assert len(items) == 8
    assert [item["name"] for item in items] == [
        "떡볶이",
        "돈까스",
        "햄버거",
        "마라탕",
        "파스타",
        "국밥",
        "짜장면",
        "제육볶음",
    ]
    assert [item["food_kind_id"] for item in items] == [
        kind_id_by_key[key] for key, _ in EXPECTED_INITIAL_POPULAR_SPECS
    ]

    for item in items:
        assert set(item.keys()) == {"food_kind_id", "name", "image_url"}
        assert isinstance(item["food_kind_id"], int)
        assert item["image_url"].startswith("/static/")
        assert item["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
        asset_resp = popular_api.client.get(item["image_url"])
        assert asset_resp.status_code == 200


def test_public_popular_menus_is_strictly_read_only_when_initial_table_is_empty(
    popular_api,
    sqlite_engine,
):
    db = popular_api.db
    commits: list[str] = []
    write_statements: list[str] = []

    def _record_commit(_session):
        commits.append("commit")

    def _record_write_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lstrip().upper()
        if normalized.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            write_statements.append(statement)

    event.listen(db, "before_commit", _record_commit)
    event.listen(sqlite_engine, "before_cursor_execute", _record_write_sql)
    try:
        resp = popular_api.client.get(POPULAR_MENUS_URL)
    finally:
        event.remove(db, "before_commit", _record_commit)
        event.remove(sqlite_engine, "before_cursor_execute", _record_write_sql)

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"] == {"source": "initial", "as_of": None, "items": []}
    assert commits == []
    assert write_statements == []
    assert db.query(InitialPopularFoodKind).count() == 0
    assert db.query(FoodKind).count() == 0


def test_public_popular_menus_returns_initial_eight_after_startup_seed_without_token(popular_api):
    ensure_initial_popular_food_kinds(popular_api.db)

    resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["source"] == "initial"
    assert body["data"]["as_of"] is None
    assert [item["name"] for item in body["data"]["items"]] == [
        name for _, name in EXPECTED_INITIAL_POPULAR_SPECS
    ]


def test_public_popular_menus_exposes_no_user_or_meal_data_and_is_independent_from_recent_card(
    popular_api,
):
    db = popular_api.db
    kakao_user = User(
        kakao_id=90001,
        nickname="비공개유저닉네임",
        email="secret-user@example.com",
    )
    uuid_user = create_uuid_user(db)
    uuid_user.nickname = "익명비공개닉네임"

    secret_menu = Menu(
        food_code="P64-SECRET-BURGER",
        name="버거_시크릿트러플버거",
        base_name="버거",
        category_large="빵 및 과자류",
        category_medium="버거",
    )
    db.add_all([kakao_user, secret_menu])
    db.flush()

    db.add_all(
        [
            UserMeal(
                user_id=kakao_user.id,
                menu_id=secret_menu.id,
                menu_name="시크릿트러플버거",
                eaten_at=datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=uuid_user.id,
                menu_id=secret_menu.id,
                menu_name="익명식사메뉴기록",
                eaten_at=datetime(2026, 11, 19, 13, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    db.commit()
    ensure_initial_popular_food_kinds(db)

    # 인증 없이 최근 메뉴는 401이지만 인기 메뉴는 200으로 독립 조회된다.
    recent_unauth = popular_api.client.get(RECENT_HOME_CARD_URL)
    assert recent_unauth.status_code == 401

    popular_resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert popular_resp.status_code == 200
    payload = popular_resp.json()
    raw_json = json.dumps(payload, ensure_ascii=False)

    for forbidden in (
        "비공개유저닉네임",
        "익명비공개닉네임",
        "secret-user@example.com",
        "시크릿트러플버거",
        "익명식사메뉴기록",
        "eaten_at",
        "user_id",
        "nickname",
    ):
        assert forbidden not in raw_json

    assert set(payload["data"].keys()) == {"source", "as_of", "items"}
    assert len(payload["data"]["items"]) == 8

    # 로그인 사용자의 최근 메뉴 조회와 인기 메뉴 조회는 서로 영향을 주지 않고 독립 동작한다.
    headers = {"Authorization": f"Bearer {session_token(db, kakao_user)}"}
    recent_auth = popular_api.client.get(RECENT_HOME_CARD_URL, headers=headers)
    assert recent_auth.status_code == 200
    assert len(recent_auth.json()["data"]["items"]) == 1
    assert recent_auth.json()["data"]["items"][0]["menu_name"] == "시크릿트러플버거"

    popular_after = popular_api.client.get(POPULAR_MENUS_URL)
    assert popular_after.status_code == 200
    assert popular_after.json() == payload


def test_public_popular_menus_resolves_valid_representative_image_and_falls_back_when_missing(
    popular_api,
):
    db = popular_api.db
    seed_initial_popular_food_kinds(db)

    tteokbokki = db.query(FoodKind).filter(FoodKind.kind_key == "tteokbokki").one()
    tonkatsu = db.query(FoodKind).filter(FoodKind.kind_key == "tonkatsu").one()
    tteokbokki.representative_image_url = "/static/default-profile.svg"
    tonkatsu.representative_image_url = "/static/menu-illustrations/not-deployed-yet.png"
    db.commit()

    resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]

    assert items[0]["name"] == "떡볶이"
    assert items[0]["image_url"] == "/static/default-profile.svg"
    assert popular_api.client.get(items[0]["image_url"]).status_code == 200

    assert items[1]["name"] == "돈까스"
    assert items[1]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
    assert popular_api.client.get(items[1]["image_url"]).status_code == 200


def test_initial_popular_food_kinds_db_constraints_reject_invalid_rows(sqlite_engine):
    with Session(sqlite_engine) as db:
        fk1 = FoodKind(kind_key="tteokbokki", name="떡볶이")
        fk2 = FoodKind(kind_key="tonkatsu", name="돈까스")
        db.add_all([fk1, fk2])
        db.commit()

        # 정상 1건 등록
        db.add(InitialPopularFoodKind(position=1, food_kind_id=fk1.id))
        db.commit()

        # 1) 중복 position 거부
        db.add(InitialPopularFoodKind(position=1, food_kind_id=fk2.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 2) 중복 food_kind_id 거부
        db.add(InitialPopularFoodKind(position=2, food_kind_id=fk1.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 3) 범위 밖 position (0, 9, -1) 거부
        for bad_pos in (0, 9, -1):
            db.add(InitialPopularFoodKind(position=bad_pos, food_kind_id=fk2.id))
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

        # 4) 존재하지 않는 food_kind_id FK 거부
        db.add(InitialPopularFoodKind(position=2, food_kind_id=999999))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 5) NULL position / NULL food_kind_id 거부
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO initial_popular_food_kinds (position, food_kind_id, created_at, updated_at) "
                    "VALUES (NULL, :fk_id, '2026-11-20 00:00:00', '2026-11-20 00:00:00')"
                ),
                {"fk_id": fk2.id},
            )
            db.commit()
        db.rollback()

        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO initial_popular_food_kinds (position, food_kind_id, created_at, updated_at) "
                    "VALUES (2, NULL, '2026-11-20 00:00:00', '2026-11-20 00:00:00')"
                )
            )
            db.commit()
        db.rollback()


def test_transactional_reorder_replaces_all_eight_atomically_and_rolls_back_on_invalid_input(
    popular_api,
):
    db = popular_api.db
    seed_initial_popular_food_kinds(db)

    # 1) 위치 맞바꾸기를 포함한 전체 8종 순서 교체가 UNIQUE 충돌 없이 원자적으로 반영된다.
    reordered_keys = (
        "jeyuk_bokkeum",
        "jjajangmyeon",
        "gukbap",
        "pasta",
        "malatang",
        "burger",
        "tonkatsu",
        "tteokbokki",
    )
    seed_initial_popular_food_kinds(db, kind_keys=reordered_keys)

    resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp.status_code == 200
    assert [item["name"] for item in resp.json()["data"]["items"]] == [
        "제육볶음",
        "짜장면",
        "국밥",
        "파스타",
        "마라탕",
        "햄버거",
        "돈까스",
        "떡볶이",
    ]

    # 2) 존재하지 않는 음식 종류 키가 포함되면 실패하고 기존 8종 순서가 보존된다.
    with pytest.raises(ValueError, match="등록되지 않은"):
        seed_initial_popular_food_kinds(
            db,
            kind_keys=(
                "tteokbokki",
                "tonkatsu",
                "burger",
                "malatang",
                "pasta",
                "gukbap",
                "jjajangmyeon",
                "nonexistent_kind_key",
            ),
        )

    # 3) 중복 음식 종류 키가 포함되면 실패하고 기존 8종 순서가 보존된다.
    with pytest.raises(ValueError, match="중복"):
        seed_initial_popular_food_kinds(
            db,
            kind_keys=(
                "tteokbokki",
                "tteokbokki",
                "burger",
                "malatang",
                "pasta",
                "gukbap",
                "jjajangmyeon",
                "jeyuk_bokkeum",
            ),
        )

    # 4) 개수가 8개가 아니면 실패하고 기존 8종 순서가 보존된다.
    with pytest.raises(ValueError, match="8개"):
        seed_initial_popular_food_kinds(
            db,
            kind_keys=DEFAULT_INITIAL_POPULAR_KIND_KEYS[:7],
        )

    # 기존 순서가 그대로 유지되었는지 확인
    rows = (
        db.query(InitialPopularFoodKind, FoodKind)
        .join(FoodKind, InitialPopularFoodKind.food_kind_id == FoodKind.id)
        .order_by(InitialPopularFoodKind.position.asc())
        .all()
    )
    assert [(entry.position, fk.kind_key) for entry, fk in rows] == list(
        enumerate(reordered_keys, start=1)
    )


def test_transactional_reorder_rolls_back_mid_write_failure_preserving_previous_rows(
    sqlite_engine,
):
    with Session(sqlite_engine) as db:
        seed_initial_popular_food_kinds(db)
        before_snapshot = [
            (r.position, r.food_kind_id)
            for r in db.query(InitialPopularFoodKind)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        ]
        assert len(before_snapshot) == 8

        # DELETE 직후 INSERT 단계에서 DB 오류가 발생하는 상황을 시뮬레이션해 롤백을 검증한다.
        def _abort_on_insert(session, _flush_context, _instances):
            if any(isinstance(obj, InitialPopularFoodKind) for obj in session.new):
                raise RuntimeError("simulated mid-transaction insert failure")

        event.listen(db, "before_flush", _abort_on_insert)
        try:
            with pytest.raises(RuntimeError, match="simulated mid-transaction insert failure"):
                seed_initial_popular_food_kinds(
                    db,
                    kind_keys=tuple(reversed(DEFAULT_INITIAL_POPULAR_KIND_KEYS)),
                )
        finally:
            event.remove(db, "before_flush", _abort_on_insert)

        # 실패 후 같은 세션에서 커밋이 발생하더라도 롤백된 삭제가 반영되지 않아야 한다.
        db.commit()

        # 새 세션에서 조회해도 기존 8건이 그대로 보존되어 있어야 한다.
    with Session(sqlite_engine) as verify_db:
        after_snapshot = [
            (r.position, r.food_kind_id)
            for r in verify_db.query(InitialPopularFoodKind)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        ]
        assert after_snapshot == before_snapshot


def test_seed_database_populates_initial_popular_food_kinds_idempotently_and_preserves_custom_order(
    sqlite_engine,
    tmp_path: Path,
):
    csv_path = tmp_path / "sample_popular_seed.csv"
    csv_path.write_text(
        "식품코드,식품명,식품대분류명,식품중분류명,식품소분류명,식품기원명,업체명,에너지(kcal),단백질(g),지방(g),탄수화물(g),당류(g),나트륨(mg),식품중량\n"
        "P64-01,버거_치즈버거,빵 및 과자류,버거,치즈버거,외식(분석함량),해당없음,260,13,11,27,4,520,220g\n"
        "P64-02,국밥_돼지국밥,국 및 탕류,국밥,돼지국밥,외식(분석함량),해당없음,115,8,4,11,1,320,550g\n",
        encoding="utf-8-sig",
    )

    with Session(sqlite_engine) as db:
        summary1 = seed_database(
            db=db,
            csv_path=csv_path,
            include_beverages_and_bakery=False,
            use_llm=False,
        )
        assert summary1["initial_popular_food_kinds"] == 8

        rows_after_first = (
            db.query(InitialPopularFoodKind, FoodKind)
            .join(FoodKind, InitialPopularFoodKind.food_kind_id == FoodKind.id)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        )
        assert [(entry.position, fk.kind_key, fk.name) for entry, fk in rows_after_first] == [
            (pos, key, name)
            for pos, (key, name) in enumerate(EXPECTED_INITIAL_POPULAR_SPECS, start=1)
        ]

        # 재실행 시 멱등 유지 확인
        first_ids = [entry.id for entry, _ in rows_after_first]
        summary2 = seed_database(
            db=db,
            csv_path=csv_path,
            include_beverages_and_bakery=False,
            use_llm=False,
        )
        assert summary2["initial_popular_food_kinds"] == 8
        rows_after_second = (
            db.query(InitialPopularFoodKind)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        )
        assert [row.id for row in rows_after_second] == first_ids

        # 운영자가 합의 순서를 변경한 뒤 재시드해도 변경된 초기 순서를 덮어쓰지 않고 보존한다.
        custom_order = tuple(reversed(DEFAULT_INITIAL_POPULAR_KIND_KEYS))
        seed_initial_popular_food_kinds(db, kind_keys=custom_order)

        seed_database(
            db=db,
            csv_path=csv_path,
            include_beverages_and_bakery=False,
            use_llm=False,
        )
        rows_after_custom_reseed = (
            db.query(InitialPopularFoodKind, FoodKind)
            .join(FoodKind, InitialPopularFoodKind.food_kind_id == FoodKind.id)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        )
        assert [fk.kind_key for _, fk in rows_after_custom_reseed] == list(custom_order)


def test_migrate_issue62_backfill_creates_and_seeds_initial_popular_food_kinds_idempotently():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        with engine.connect() as conn:
            FoodKind.__table__.create(conn)
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
            conn.execute(
                text(
                    "INSERT INTO menus (food_code, name, base_name) VALUES "
                    "('MIG-1', '버거_치즈버거', '버거'), "
                    "('MIG-2', '국밥_순대국밥', '국밥')"
                )
            )
            conn.commit()

            migrate_issue62_food_kinds.backfill_food_kinds(conn)
            first_result = migrate_issue62_food_kinds.backfill_initial_popular_food_kinds(conn)
            assert first_result == {"seeded_initial_popular": 8, "total_initial_popular": 8}

            assert "initial_popular_food_kinds" in set(inspect(conn).get_table_names())
            rows = (
                conn.execute(
                    text(
                        "SELECT ip.position, fk.kind_key, fk.name "
                        "FROM initial_popular_food_kinds ip "
                        "JOIN food_kinds fk ON fk.id = ip.food_kind_id "
                        "ORDER BY ip.position ASC"
                    )
                )
                .mappings()
                .all()
            )
            assert [(r["position"], r["kind_key"], r["name"]) for r in rows] == [
                (pos, key, name)
                for pos, (key, name) in enumerate(EXPECTED_INITIAL_POPULAR_SPECS, start=1)
            ]

            # 재적용 시 멱등성 확인 (기존 8건을 덮어쓰지 않고 보존한다)
            second_result = migrate_issue62_food_kinds.backfill_initial_popular_food_kinds(conn)
            assert second_result == {"seeded_initial_popular": 0, "total_initial_popular": 8}
    finally:
        engine.dispose()


@pytest.fixture
def mysql_engine():
    raw_url = os.environ.get("MYSQL_TEST_DATABASE_URL", "").strip()
    if not raw_url:
        pytest.skip("Set MYSQL_TEST_DATABASE_URL to run MySQL Ticket #64 integration tests.")

    url = make_url(raw_url)
    assert url.drivername.startswith("mysql"), "MYSQL_TEST_DATABASE_URL must use a MySQL driver."
    assert url.database and "test" in url.database.lower(), (
        "MYSQL_TEST_DATABASE_URL must point to an isolated test database."
    )

    schema = f"whatbob_t64_{uuid4().hex[:12]}"
    admin = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE `{schema}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    engine = create_engine(url.set(database=schema), pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE `{schema}`"))
        admin.dispose()


def test_mysql_initial_popular_food_kinds_constraints_and_atomic_reorder(mysql_engine):
    Base.metadata.create_all(mysql_engine)

    with Session(mysql_engine) as db:
        seed_initial_popular_food_kinds(db)

        rows = (
            db.query(InitialPopularFoodKind, FoodKind)
            .join(FoodKind, InitialPopularFoodKind.food_kind_id == FoodKind.id)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        )
        assert [(r.position, fk.kind_key) for r, fk in rows] == list(
            enumerate(DEFAULT_INITIAL_POPULAR_KIND_KEYS, start=1)
        )

        # MySQL 8.4 CHECK / UNIQUE / FK 제약 확인
        extra_kind = FoodKind(kind_key="mysql_extra_kind", name="비빔밥")
        db.add(extra_kind)
        db.commit()

        db.add(InitialPopularFoodKind(position=9, food_kind_id=extra_kind.id))
        with pytest.raises((IntegrityError, OperationalError)) as exc_info:
            db.commit()
        assert "ck_initial_popular_food_kinds_position_range" in str(exc_info.value)
        db.rollback()

        db.add(InitialPopularFoodKind(position=1, food_kind_id=extra_kind.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        first_fk_id = rows[0][1].id
        db.query(InitialPopularFoodKind).filter(InitialPopularFoodKind.position == 8).delete()
        db.flush()
        db.add(InitialPopularFoodKind(position=8, food_kind_id=first_fk_id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.query(InitialPopularFoodKind).filter(InitialPopularFoodKind.position == 8).delete()
        db.flush()
        db.add(InitialPopularFoodKind(position=8, food_kind_id=999999))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 원자적 역순 교체 성공
        reversed_keys = tuple(reversed(DEFAULT_INITIAL_POPULAR_KIND_KEYS))
        seed_initial_popular_food_kinds(db, kind_keys=reversed_keys)

        # 잘못된 키 입력 시 롤백 및 기존 순서 유지
        with pytest.raises(ValueError, match="등록되지 않은"):
            seed_initial_popular_food_kinds(
                db,
                kind_keys=(*DEFAULT_INITIAL_POPULAR_KIND_KEYS[:7], "unknown_mysql_kind"),
            )

        reloaded = (
            db.query(InitialPopularFoodKind, FoodKind)
            .join(FoodKind, InitialPopularFoodKind.food_kind_id == FoodKind.id)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        )
        assert [fk.kind_key for _, fk in reloaded] == list(reversed_keys)

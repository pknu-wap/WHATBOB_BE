from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4
from xml.etree import ElementTree

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.menus import food_kinds
from app.menus.food_kinds import (
    CANONICAL_FOOD_KINDS,
    DEFAULT_MENU_ILLUSTRATION_URL,
    STATIC_DIR,
    classify_food_kind,
    resolve_menu_illustration_url,
)
from app.menus.illustrations import (
    DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES,
    FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY,
    FOOD_KIND_ILLUSTRATION_SPECS,
    MISSING_ILLUSTRATION_KIND_KEYS_FOR_ISSUE68,
    PENDING_VERIFICATION_ILLUSTRATION_KIND_KEYS,
    REPRESENTATIVE_ILLUSTRATION_HEIGHT,
    REPRESENTATIVE_ILLUSTRATION_WIDTH,
    REUSABLE_ILLUSTRATION_KIND_KEYS,
    FoodKindNotFoundException,
    IllustrationAuditStatus,
    InvalidMenuIllustrationException,
    load_food_kind_illustration_urls,
    register_food_kind_illustration,
    seed_menu_illustration_exceptions,
    sync_food_kind_representative_illustrations,
    validate_menu_illustration_png,
)
from app.menus.models import FoodKind, Menu, MenuIllustrationException, UserMeal
from app.menus.popular import (
    aggregate_popular_menus_for_kst_date,
    ensure_initial_popular_food_kinds,
    get_seoul_tz,
)
from app.users.models import User
from app.users.router import get_utc_now
from scripts import migrate_issue67_illustrations
from scripts.seed_menus import seed_database
from tests.auth_helpers import session_token

RECENT_HOME_CARD_URL = "/api/users/me/home-card-recently-eaten-food-max-6"
POPULAR_MENUS_URL = "/api/menus/popular"
MEALS_URL = "/api/users/me/meals"


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
def sqlite_db(sqlite_engine):
    with Session(sqlite_engine) as db:
        yield db


def _make_menu(
    *,
    food_code: str,
    name: str,
    base_name: str,
    category_large: str = "면 및 만두류",
) -> Menu:
    return Menu(
        food_code=food_code,
        name=name,
        base_name=base_name,
        category_large=category_large,
    )


def test_figma_illustration_manifest_records_reusable_pending_and_missing_for_issue68():
    # 8 canonical food kinds must all be tracked in agreed order
    assert [spec.kind_key for spec in FOOD_KIND_ILLUSTRATION_SPECS] == [
        spec.kind_key for spec in CANONICAL_FOOD_KINDS
    ]

    # Confirmed reusable named Figma originals (5 kinds)
    assert REUSABLE_ILLUSTRATION_KIND_KEYS == (
        "tonkatsu",
        "burger",
        "malatang",
        "pasta",
        "jeyuk_bokkeum",
    )
    assert FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY["tonkatsu"].figma_source_name == "돈까스"
    assert FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY["burger"].figma_source_name == "햄버거"
    assert FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY["malatang"].figma_source_name == "마라탕"
    assert FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY["pasta"].figma_source_name == "스파게티"
    assert FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY["jeyuk_bokkeum"].figma_source_name == "제육볶음"

    for key in REUSABLE_ILLUSTRATION_KIND_KEYS:
        spec = FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY[key]
        assert spec.usable is True
        assert spec.status is IllustrationAuditStatus.CONFIRMED_REUSABLE
        assert spec.expected_image_url == f"/static/menu-illustrations/{key}.png"

    # Pending verification candidate (tteokbokki from Firefly (2) 1 — distinguished from confirmed & missing)
    assert PENDING_VERIFICATION_ILLUSTRATION_KIND_KEYS == ("tteokbokki",)
    tteok_spec = FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY["tteokbokki"]
    assert tteok_spec.usable is False
    assert tteok_spec.status is IllustrationAuditStatus.PENDING_VERIFICATION
    assert tteok_spec.figma_source_name is not None
    assert "Firefly (2) 1" in tteok_spec.figma_source_name
    assert tteok_spec.expected_image_url == "/static/menu-illustrations/tteokbokki.png"

    # Missing originals handed off to #68
    assert MISSING_ILLUSTRATION_KIND_KEYS_FOR_ISSUE68 == ("gukbap", "jjajangmyeon")
    for key in MISSING_ILLUSTRATION_KIND_KEYS_FOR_ISSUE68:
        spec = FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY[key]
        assert spec.usable is False
        assert spec.status is IllustrationAuditStatus.MISSING_ORIGINAL
        assert spec.figma_source_name is None
        assert spec.expected_image_url == f"/static/menu-illustrations/{key}.png"


def test_default_menu_illustration_exception_food_codes_cover_non_tomato_pasta_catalog():
    # 29 unique non-tomato pasta (cream, carbonara, toowoomba, rose, oil, gnocchi) codes
    assert len(DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES) == 29
    assert len(set(DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES)) == 29
    # Cream & rose & oil pasta included
    assert "D203-161000000-0002" in DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES  # 베이컨 크림 파스타
    assert "D203-161000000-0003" in DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES  # 쉬림프 로제 파스타
    assert "D303-161000000-0008" in DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES  # 알리오올리오파스타
    # Tomato pasta NOT excluded
    assert "D203-161000000-0001" not in DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES  # 미트 토마토 파스타
    assert "D203-161000000-0023" not in DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES  # 토마토스파게티


def test_resolve_menu_illustration_url_handles_common_fallback_and_file_existence(tmp_path: Path):
    illustrations_dir = tmp_path / "menu-illustrations"
    illustrations_dir.mkdir(parents=True)
    (illustrations_dir / "pasta.png").write_bytes(b"fake-png-bytes")

    # Deployed file resolves to its exact relative static path
    assert (
        resolve_menu_illustration_url(
            "/static/menu-illustrations/pasta.png",
            static_dir=tmp_path,
        )
        == "/static/menu-illustrations/pasta.png"
    )
    assert (
        resolve_menu_illustration_url("/static/menu-illustrations/pasta.svg", static_dir=tmp_path)
        == "/static/menu-illustrations/pasta.png"
    )
    (illustrations_dir / "pasta.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert (
        resolve_menu_illustration_url("/static/menu-illustrations/pasta.png", static_dir=tmp_path)
        == "/static/menu-illustrations/pasta.png"
    )
    assert (
        resolve_menu_illustration_url("/static/menu-illustrations/pasta.svg", static_dir=tmp_path)
        == "/static/menu-illustrations/pasta.svg"
    )
    # Forced common fallback always returns common image even if representative file exists
    assert (
        resolve_menu_illustration_url(
            "/static/menu-illustrations/pasta.png",
            use_common_fallback=True,
            static_dir=tmp_path,
        )
        == DEFAULT_MENU_ILLUSTRATION_URL
    )
    # Missing file, unset URL, external URL, and path traversal all fall back to common image
    assert resolve_menu_illustration_url(None, static_dir=tmp_path) == DEFAULT_MENU_ILLUSTRATION_URL
    assert resolve_menu_illustration_url("", static_dir=tmp_path) == DEFAULT_MENU_ILLUSTRATION_URL
    assert (
        resolve_menu_illustration_url("/static/menu-illustrations/missing.png", static_dir=tmp_path)
        == DEFAULT_MENU_ILLUSTRATION_URL
    )
    assert (
        resolve_menu_illustration_url("https://cdn.example.com/pasta.png", static_dir=tmp_path)
        == DEFAULT_MENU_ILLUSTRATION_URL
    )
    assert (
        resolve_menu_illustration_url("/static/../secret.txt", static_dir=tmp_path)
        == DEFAULT_MENU_ILLUSTRATION_URL
    )
    assert (
        resolve_menu_illustration_url("/static//menu-illustrations/pasta.png", static_dir=tmp_path)
        == DEFAULT_MENU_ILLUSTRATION_URL
    )


def test_menu_illustration_exception_pk_fk_and_cascade_delete_preserve_user_meal_name(
    sqlite_db: Session,
):
    user = User(kakao_id=67001, nickname="파스타유저")
    cream_pasta = _make_menu(
        food_code="D203-161000000-0002",
        name="스파게티_베이컨 크림 파스타",
        base_name="스파게티",
    )
    sqlite_db.add_all([user, cream_pasta])
    sqlite_db.commit()

    exc = MenuIllustrationException(menu_id=cream_pasta.id, reason="크림 파스타 예외")
    sqlite_db.add(exc)
    sqlite_db.commit()

    # Duplicate menu_id rejected by PK
    sqlite_db.expunge(exc)
    sqlite_db.add(MenuIllustrationException(menu_id=cream_pasta.id, reason="중복"))
    with pytest.raises(IntegrityError):
        sqlite_db.commit()
    sqlite_db.rollback()

    # Non-existent menu_id rejected by FK
    sqlite_db.add(MenuIllustrationException(menu_id=999999, reason="없는 메뉴"))
    with pytest.raises(IntegrityError):
        sqlite_db.commit()
    sqlite_db.rollback()

    # Record a meal for the excepted menu, then delete the menu
    meal = UserMeal(
        user_id=user.id,
        menu_id=cream_pasta.id,
        menu_name="베이컨 크림 파스타",
        eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
    )
    sqlite_db.add(meal)
    sqlite_db.commit()

    sqlite_db.delete(cream_pasta)
    sqlite_db.commit()

    # Exception row is removed by ON DELETE CASCADE while UserMeal.menu_name is preserved
    assert (
        sqlite_db.query(MenuIllustrationException)
        .filter(MenuIllustrationException.menu_id == cream_pasta.id)
        .one_or_none()
        is None
    )
    reloaded_meal = sqlite_db.get(UserMeal, meal.id)
    assert reloaded_meal is not None
    assert reloaded_meal.menu_id is None
    assert reloaded_meal.menu_name == "베이컨 크림 파스타"


def test_seed_menu_illustration_exceptions_is_idempotent_and_tolerates_partial_catalog(
    sqlite_db: Session,
):
    tomato = _make_menu(
        food_code="D203-161000000-0001",
        name="스파게티_미트 토마토 파스타",
        base_name="스파게티",
    )
    cream = _make_menu(
        food_code="D203-161000000-0002",
        name="스파게티_베이컨 크림 파스타",
        base_name="스파게티",
    )
    rose = _make_menu(
        food_code="D203-161000000-0003",
        name="스파게티_쉬림프 로제 파스타",
        base_name="스파게티",
    )
    oil = _make_menu(
        food_code="D303-161000000-0008",
        name="스파게티_간편조리세트_알리오올리오파스타",
        base_name="스파게티",
    )
    sqlite_db.add_all([tomato, cream, rose, oil])
    sqlite_db.commit()

    # First run inserts only existing non-tomato codes and ignores the rest of the 29 codes
    first = seed_menu_illustration_exceptions(sqlite_db)
    assert first["inserted"] == 3
    assert first["already_present"] == 0
    assert first["unmatched_food_codes"] == len(DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES) - 3
    assert first["total_exceptions"] == 3

    excepted_ids = {
        row.menu_id for row in sqlite_db.query(MenuIllustrationException).all()
    }
    assert excepted_ids == {cream.id, rose.id, oil.id}
    assert tomato.id not in excepted_ids

    # Custom reason is preserved on idempotent re-run
    cream_exc = sqlite_db.get(MenuIllustrationException, cream.id)
    assert cream_exc is not None
    cream_exc.reason = "운영 수동 사유"
    sqlite_db.commit()

    second = seed_menu_illustration_exceptions(sqlite_db)
    assert second["inserted"] == 0
    assert second["already_present"] == 3
    assert second["total_exceptions"] == 3
    sqlite_db.expire_all()
    assert sqlite_db.get(MenuIllustrationException, cream.id).reason == "운영 수동 사유"


def test_sync_and_register_food_kind_illustrations_verify_files_and_preserve_custom_paths(
    sqlite_db: Session,
    tmp_path: Path,
):
    illustrations_dir = tmp_path / "menu-illustrations"
    illustrations_dir.mkdir(parents=True)
    (illustrations_dir / "burger.png").write_bytes(b"fake-burger-png")
    (illustrations_dir / "pasta.png").write_bytes(b"fake-pasta-png")
    (illustrations_dir / "gukbap-v2.png").write_bytes(b"fake-gukbap-png")

    burger = FoodKind(kind_key="burger", name="햄버거")
    pasta = FoodKind(kind_key="pasta", name="파스타")
    gukbap = FoodKind(kind_key="gukbap", name="국밥")
    malatang = FoodKind(kind_key="malatang", name="마라탕")
    sqlite_db.add_all([burger, pasta, gukbap, malatang])
    sqlite_db.commit()

    # Sync registers only files that exist on disk; never points DB at missing files
    summary = sync_food_kind_representative_illustrations(sqlite_db, static_dir=tmp_path)
    assert summary["updated"] == 2
    deployed_urls = {
        "/static/menu-illustrations/burger.png",
        "/static/menu-illustrations/pasta.png",
    }
    assert summary["skipped_missing_file"] == sum(
        url not in deployed_urls for url in load_food_kind_illustration_urls().values()
    )
    sqlite_db.expire_all()
    assert sqlite_db.get(FoodKind, burger.id).representative_image_url == "/static/menu-illustrations/burger.png"
    assert sqlite_db.get(FoodKind, pasta.id).representative_image_url == "/static/menu-illustrations/pasta.png"
    assert sqlite_db.get(FoodKind, malatang.id).representative_image_url is None
    assert sqlite_db.get(FoodKind, gukbap.id).representative_image_url is None

    # Registering a designer PNG for gukbap succeeds when deployed file exists
    updated = register_food_kind_illustration(
        sqlite_db,
        kind_key="gukbap",
        image_url="/static/menu-illustrations/gukbap-v2.png",
        static_dir=tmp_path,
    )
    assert updated.representative_image_url == "/static/menu-illustrations/gukbap-v2.png"

    # A matching SVG upgrades the mapped path while preserving a separately named custom path.
    (illustrations_dir / "gukbap.png").write_bytes(b"fake-gukbap-default-png")
    (illustrations_dir / "gukbap.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    second_sync = sync_food_kind_representative_illustrations(sqlite_db, static_dir=tmp_path)
    assert second_sync["updated"] == 0
    assert second_sync["already_registered"] == 3
    sqlite_db.expire_all()
    assert sqlite_db.get(FoodKind, gukbap.id).representative_image_url == "/static/menu-illustrations/gukbap-v2.png"

    # Reject missing disk file when verify_file_exists=True
    with pytest.raises(FileNotFoundError):
        register_food_kind_illustration(
            sqlite_db,
            kind_key="malatang",
            image_url="/static/menu-illustrations/not-deployed.png",
            static_dir=tmp_path,
            verify_file_exists=True,
        )

    # Reject unknown kind_key and invalid URL format with domain exceptions
    with pytest.raises(FoodKindNotFoundException, match="찾을 수 없습니다"):
        register_food_kind_illustration(
            sqlite_db,
            kind_key="nonexistent_kind",
            image_url="/static/menu-illustrations/burger.png",
            static_dir=tmp_path,
        )
    with pytest.raises(InvalidMenuIllustrationException):
        register_food_kind_illustration(
            sqlite_db,
            kind_key="burger",
            image_url="https://evil.example.com/burger.png",
            static_dir=tmp_path,
        )


@contextmanager
def _deployed_temp_static_file(filename: str, content: bytes = b"<svg xmlns='http://www.w3.org/2000/svg'/>"):
    target_dir = STATIC_DIR / "menu-illustrations"
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / filename
    assert not file_path.exists(), f"Test temp file already exists: {file_path}"
    file_path.write_bytes(content)
    try:
        yield f"/static/menu-illustrations/{filename}"
    finally:
        if file_path.exists():
            file_path.unlink()


@pytest.fixture
def home_api(sqlite_engine):
    overrides = dict(app.dependency_overrides)
    fixed_now = datetime(2026, 11, 20, 15, 0, 0, tzinfo=timezone.utc)
    try:
        with Session(sqlite_engine) as db:
            app.dependency_overrides[get_db] = lambda: db
            app.dependency_overrides[get_utc_now] = lambda: fixed_now
            yield SimpleNamespace(
                client=TestClient(app),
                db=db,
                now=fixed_now,
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def test_shared_representative_illustration_is_served_on_both_recent_and_popular_home_apis(
    home_api,
):
    db = home_api.db
    client = home_api.client
    ensure_initial_popular_food_kinds(db)

    user_a = User(kakao_id=67101, nickname="버거유저A")
    user_b = User(kakao_id=67102, nickname="버거유저B")
    cheeseburger = _make_menu(
        food_code="T67-BURGER-1",
        name="버거_치즈버거",
        base_name="버거",
        category_large="빵 및 과자류",
    )
    bulgogiburger = _make_menu(
        food_code="T67-BURGER-2",
        name="햄버거_불고기버거",
        base_name="햄버거",
        category_large="빵 및 과자류",
    )
    db.add_all([user_a, user_b, cheeseburger, bulgogiburger])
    db.flush()

    db.add_all(
        [
            UserMeal(
                user_id=user_a.id,
                menu_id=cheeseburger.id,
                menu_name="치즈버거",
                eaten_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=user_a.id,
                menu_id=bulgogiburger.id,
                menu_name="불고기버거",
                eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=user_b.id,
                menu_id=cheeseburger.id,
                menu_name="치즈버거",
                eaten_at=datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc),
            ),
        ]
    )
    db.commit()

    token_a = session_token(db, user_a)
    token_b = session_token(db, user_b)

    temp_name = f"_test_t67_burger_{uuid4().hex[:8]}.svg"
    with _deployed_temp_static_file(temp_name) as burger_url:
        register_food_kind_illustration(db, kind_key="burger", image_url=burger_url)

        recent_a = client.get(
            RECENT_HOME_CARD_URL,
            headers={"Authorization": f"Bearer {token_a}"},
        )
        recent_b = client.get(
            RECENT_HOME_CARD_URL,
            headers={"Authorization": f"Bearer {token_b}"},
        )
        popular = client.get(POPULAR_MENUS_URL)

        assert recent_a.status_code == 200
        assert recent_b.status_code == 200
        assert popular.status_code == 200

        item_a = recent_a.json()["data"]["items"][0]
        item_b = recent_b.json()["data"]["items"][0]
        popular_burger = next(
            item for item in popular.json()["data"]["items"] if item["name"] == "햄버거"
        )

        # Both burger variants and popular burger share the same food_kind_id and representative image URL
        assert item_a["menu_name"] == "불고기버거"
        assert item_b["menu_name"] == "치즈버거"
        assert item_a["food_kind_id"] == item_b["food_kind_id"] == popular_burger["food_kind_id"]
        assert item_a["image_url"] == burger_url
        assert item_b["image_url"] == burger_url
        assert popular_burger["image_url"] == burger_url

        # Deployed static file serves HTTP 200
        assert client.get(burger_url).status_code == 200


def test_missing_registration_and_missing_disk_file_fallback_to_common_image_on_both_apis(
    home_api,
):
    db = home_api.db
    client = home_api.client
    ensure_initial_popular_food_kinds(db)

    # gukbap has representative_image_url=None; jjajangmyeon points to a missing file on disk
    jj_kind = db.query(FoodKind).filter(FoodKind.kind_key == "jjajangmyeon").one()
    jj_kind.representative_image_url = "/static/menu-illustrations/does-not-exist-t67.png"
    gukbap_kind = db.query(FoodKind).filter(FoodKind.kind_key == "gukbap").one()
    gukbap_kind.representative_image_url = None

    user = User(kakao_id=67201, nickname="폴백유저")
    gukbap_menu = _make_menu(
        food_code="T67-GUKBAP-1",
        name="국밥_돼지국밥",
        base_name="국밥",
        category_large="국 및 탕류",
    )
    jj_menu = _make_menu(
        food_code="T67-JJ-1",
        name="짜장면_간짜장",
        base_name="짜장면",
        category_large="면 및 만두류",
    )
    db.add_all([user, gukbap_menu, jj_menu])
    db.flush()

    db.add_all(
        [
            UserMeal(
                user_id=user.id,
                menu_id=gukbap_menu.id,
                menu_name="돼지국밥",
                eaten_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=user.id,
                menu_id=jj_menu.id,
                menu_name="간짜장",
                eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    db.commit()

    token = session_token(db, user)
    recent_resp = client.get(RECENT_HOME_CARD_URL, headers={"Authorization": f"Bearer {token}"})
    popular_resp = client.get(POPULAR_MENUS_URL)

    assert recent_resp.status_code == 200
    assert popular_resp.status_code == 200

    recent_items = recent_resp.json()["data"]["items"]
    assert [(i["food_kind_id"], i["menu_name"], i["image_url"]) for i in recent_items] == [
        (jj_kind.id, "간짜장", DEFAULT_MENU_ILLUSTRATION_URL),
        (gukbap_kind.id, "돼지국밥", DEFAULT_MENU_ILLUSTRATION_URL),
    ]

    popular_items = popular_resp.json()["data"]["items"]
    assert [i["name"] for i in popular_items] == [
        "떡볶이",
        "돈까스",
        "햄버거",
        "마라탕",
        "파스타",
        "국밥",
        "짜장면",
        "제육볶음",
    ]
    pop_by_name = {i["name"]: i for i in popular_items}
    assert pop_by_name["국밥"]["food_kind_id"] == gukbap_kind.id
    assert pop_by_name["국밥"]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
    assert pop_by_name["짜장면"]["food_kind_id"] == jj_kind.id
    assert pop_by_name["짜장면"]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL

    assert client.get(DEFAULT_MENU_ILLUSTRATION_URL).status_code == 200


def test_menu_illustration_exception_uses_common_image_on_recent_card_without_affecting_kind_order_or_popular(
    home_api,
):
    db = home_api.db
    client = home_api.client
    ensure_initial_popular_food_kinds(db)

    user_cream_latest = User(kakao_id=67301, nickname="크림최신유저")
    user_tomato_latest = User(kakao_id=67302, nickname="토마토최신유저")
    tomato_pasta = _make_menu(
        food_code="D203-161000000-0001",
        name="스파게티_미트 토마토 파스타",
        base_name="스파게티",
    )
    cream_pasta = _make_menu(
        food_code="D203-161000000-0002",
        name="스파게티_베이컨 크림 파스타",
        base_name="스파게티",
    )
    burger_menu = _make_menu(
        food_code="T67-BG-ORDER",
        name="버거_치즈버거",
        base_name="버거",
        category_large="빵 및 과자류",
    )
    gukbap_menu = _make_menu(
        food_code="T67-GB-ORDER",
        name="국밥_돼지국밥",
        base_name="국밥",
        category_large="국 및 탕류",
    )
    db.add_all([user_cream_latest, user_tomato_latest, tomato_pasta, cream_pasta, burger_menu, gukbap_menu])
    db.flush()

    # Both pastas share the same pasta food_kind_id
    assert tomato_pasta.food_kind_id == cream_pasta.food_kind_id
    pasta_kind_id = tomato_pasta.food_kind_id

    # User 1: gukbap (10:00) -> tomato pasta (11:00) -> burger (11:30) -> cream pasta (12:00)
    db.add_all(
        [
            UserMeal(
                user_id=user_cream_latest.id,
                menu_id=gukbap_menu.id,
                menu_name="돼지국밥",
                eaten_at=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=user_cream_latest.id,
                menu_id=tomato_pasta.id,
                menu_name="미트 토마토 파스타",
                eaten_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=user_cream_latest.id,
                menu_id=burger_menu.id,
                menu_name="치즈버거",
                eaten_at=datetime(2026, 11, 20, 11, 30, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=user_cream_latest.id,
                menu_id=cream_pasta.id,
                menu_name="베이컨 크림 파스타",
                eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            ),
            # User 2: cream pasta (11:00) -> tomato pasta (12:00)
            UserMeal(
                user_id=user_tomato_latest.id,
                menu_id=cream_pasta.id,
                menu_name="베이컨 크림 파스타",
                eaten_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=user_tomato_latest.id,
                menu_id=tomato_pasta.id,
                menu_name="미트 토마토 파스타",
                eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    db.commit()

    token_cream = session_token(db, user_cream_latest)
    token_tomato = session_token(db, user_tomato_latest)

    temp_pasta_file = f"_test_t67_pasta_{uuid4().hex[:8]}.svg"
    with _deployed_temp_static_file(temp_pasta_file) as pasta_url:
        register_food_kind_illustration(db, kind_key="pasta", image_url=pasta_url)

        # Before exception: order and food_kind_id baseline
        before_items = client.get(
            RECENT_HOME_CARD_URL,
            headers={"Authorization": f"Bearer {token_cream}"},
        ).json()["data"]["items"]
        assert [(i["food_kind_id"], i["menu_name"]) for i in before_items] == [
            (pasta_kind_id, "베이컨 크림 파스타"),
            (burger_menu.food_kind_id, "치즈버거"),
            (gukbap_menu.food_kind_id, "돼지국밥"),
        ]
        assert before_items[0]["image_url"] == pasta_url

        # Now seed default pasta exceptions (includes cream pasta, excludes tomato pasta)
        seed_menu_illustration_exceptions(db)

        after_cream_items = client.get(
            RECENT_HOME_CARD_URL,
            headers={"Authorization": f"Bearer {token_cream}"},
        ).json()["data"]["items"]
        # Same food_kind_id, same deduplication (3 cards), same ordering — only cream pasta image switches to common!
        assert [(i["food_kind_id"], i["menu_name"]) for i in after_cream_items] == [
            (pasta_kind_id, "베이컨 크림 파스타"),
            (burger_menu.food_kind_id, "치즈버거"),
            (gukbap_menu.food_kind_id, "돼지국밥"),
        ]
        assert after_cream_items[0]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL

        # User 2 whose latest pasta is tomato pasta STILL gets pasta representative illustration
        after_tomato_items = client.get(
            RECENT_HOME_CARD_URL,
            headers={"Authorization": f"Bearer {token_tomato}"},
        ).json()["data"]["items"]
        assert len(after_tomato_items) == 1
        assert after_tomato_items[0]["food_kind_id"] == pasta_kind_id
        assert after_tomato_items[0]["menu_name"] == "미트 토마토 파스타"
        assert after_tomato_items[0]["image_url"] == pasta_url

        # Popular API is food-kind level and STILL serves pasta representative illustration
        popular_items = client.get(POPULAR_MENUS_URL).json()["data"]["items"]
        pop_pasta = next(i for i in popular_items if i["food_kind_id"] == pasta_kind_id)
        assert pop_pasta["name"] == "파스타"
        assert pop_pasta["image_url"] == pasta_url
        assert client.get(pasta_url).status_code == 200
        assert client.get(DEFAULT_MENU_ILLUSTRATION_URL).status_code == 200

        # Deleting the excepted cream pasta cascades exception row, recent falls back to tomato pasta,
        # and GET /api/users/me/meals still preserves the deleted cream pasta's saved menu_name!
        db.delete(cream_pasta)
        db.commit()

        assert (
            db.query(MenuIllustrationException)
            .filter(MenuIllustrationException.menu_id == cream_pasta.id)
            .one_or_none()
            is None
        )
        recent_after_delete = client.get(
            RECENT_HOME_CARD_URL,
            headers={"Authorization": f"Bearer {token_cream}"},
        ).json()["data"]["items"]
        assert [(i["food_kind_id"], i["menu_name"], i["image_url"]) for i in recent_after_delete] == [
            (burger_menu.food_kind_id, "치즈버거", DEFAULT_MENU_ILLUSTRATION_URL),
            (pasta_kind_id, "미트 토마토 파스타", pasta_url),
            (gukbap_menu.food_kind_id, "돼지국밥", DEFAULT_MENU_ILLUSTRATION_URL),
        ]

        meals_after_delete = client.get(
            MEALS_URL,
            headers={"Authorization": f"Bearer {token_cream}"},
        ).json()["data"]["items"]
        assert meals_after_delete[0]["menu_id"] is None
        assert meals_after_delete[0]["menu_name"] == "베이컨 크림 파스타"


def test_catalog_illustration_sync_registers_all_136_images_and_preserves_food_kinds(home_api):
    db = home_api.db
    catalog = Path(__file__).resolve().parent.parent / "data" / "menus_meals_only.csv"
    with catalog.open(encoding="utf-8-sig", newline="") as source:
        catalog_kinds = dict(
            classify_food_kind(base_name=row["base_name"], name=row["name"])
            for row in csv.DictReader(source)
        )

    examples = {
        "김치우동": "udon.svg",
        "백우동": "udon.svg",
        "볶음 우동": "yaki_udon.svg",
        "치킨까스": "tonkatsu.svg",
        "치킨가스": "tonkatsu.svg",
        "생선까스": "fish_cutlet.svg",
    }
    catalog_kinds.update({key: key for key in examples.keys() - catalog_kinds.keys()})
    db.add_all(FoodKind(kind_key=key, name=name) for key, name in catalog_kinds.items())
    db.commit()
    original_kinds = {
        kind.kind_key: (kind.id, kind.name) for kind in db.query(FoodKind).all()
    }

    legacy_kinds = db.query(FoodKind).filter(FoodKind.kind_key.in_(examples)).all()
    for kind in legacy_kinds:
        filename = examples[kind.kind_key].removesuffix(".svg")
        kind.representative_image_url = f"/static/menu-illustrations/{filename}.png"
    db.commit()

    first = sync_food_kind_representative_illustrations(db)
    assert first["updated"] == len(catalog_kinds)
    assert first["skipped_missing_file"] == 0
    assert first["skipped_missing_kind"] == 0
    kinds = {kind.kind_key: kind for kind in db.query(FoodKind).all()}
    assert {key: (kind.id, kind.name) for key, kind in kinds.items()} == original_kinds
    illustrations_dir = STATIC_DIR / "menu-illustrations"
    png_files = list(illustrations_dir.glob("*.png"))
    svg_files = [file for file in illustrations_dir.glob("*.svg") if file.stem != "default"]
    expected_urls = {f"/static/menu-illustrations/{file.name}" for file in svg_files}
    assert len(expected_urls) == 136
    assert {file.stem for file in svg_files} == {file.stem for file in png_files}
    for png_file in png_files:
        asset = validate_menu_illustration_png(png_file)
        assert (asset.width, asset.height) == (
            REPRESENTATIVE_ILLUSTRATION_WIDTH,
            REPRESENTATIVE_ILLUSTRATION_HEIGHT,
        )
        assert asset.has_alpha is True
    namespace = "{http://www.w3.org/2000/svg}"
    for svg_file in svg_files:
        svg = ElementTree.parse(svg_file).getroot()
        assert svg.tag == namespace + "svg"
        assert svg.attrib["width"] == str(REPRESENTATIVE_ILLUSTRATION_WIDTH)
        assert svg.attrib["height"] == str(REPRESENTATIVE_ILLUSTRATION_HEIGHT)
        assert svg.attrib["viewBox"] == "0 0 450 405"
        assert len(svg) and all(path.tag == namespace + "path" for path in svg)
        assert any("C" in path.attrib["d"] for path in svg)
    assert {kind.representative_image_url for kind in kinds.values()} == expected_urls
    for key, filename in examples.items():
        assert kinds[key].representative_image_url == f"/static/menu-illustrations/{filename}"

    second = sync_food_kind_representative_illustrations(db)
    assert second["updated"] == 0
    assert second["already_registered"] == len(catalog_kinds)

    udon = _make_menu(food_code="T72-UDON", name="김치우동", base_name="김치우동")
    users = [User(nickname=f"우동 사용자 {number}") for number in range(5)]
    db.add_all([udon, *users])
    db.flush()
    eaten_at = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    db.add_all(
        UserMeal(user_id=user.id, menu_id=udon.id, menu_name=udon.name, eaten_at=eaten_at)
        for user in users
    )
    db.commit()
    aggregation_date = home_api.now.astimezone(get_seoul_tz()).date()
    assert aggregate_popular_menus_for_kst_date(db, aggregation_date).published
    token = session_token(db, users[0])
    recent = home_api.client.get(RECENT_HOME_CARD_URL, headers={"Authorization": f"Bearer {token}"})
    popular = home_api.client.get(POPULAR_MENUS_URL)
    assert recent.status_code == popular.status_code == 200
    assert popular.json()["data"]["source"] == "aggregated"
    for response in (recent, popular):
        item = response.json()["data"]["items"][0]
        assert item["food_kind_id"] == udon.food_kind_id
        assert item["image_url"] == "/static/menu-illustrations/udon.svg"
    svg_response = home_api.client.get("/static/menu-illustrations/udon.svg")
    assert svg_response.status_code == 200
    assert svg_response.headers["content-type"] == "image/svg+xml"
    assert home_api.client.get("/static/menu-illustrations/udon.png").status_code == 200


def test_default_exception_food_codes_all_exist_under_spaghetti_in_meals_catalog_csv():
    repo_root = Path(__file__).resolve().parent.parent
    csv_path = repo_root / "data" / "menus_meals_only.csv"
    with csv_path.open("r", encoding="utf-8-sig") as f:
        spaghetti_codes = {
            row["food_code"].strip(): row["name"].strip()
            for row in csv.DictReader(f)
            if row["base_name"].strip() == "스파게티"
        }

    missing_from_catalog = [
        code
        for code in DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES
        if code not in spaghetti_codes
    ]
    assert missing_from_catalog == []


def test_seed_database_seeds_illustration_exceptions_and_syncs_deployed_illustrations_idempotently(
    sqlite_db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    static_root = tmp_path / "static"
    illus_dir = static_root / "menu-illustrations"
    illus_dir.mkdir(parents=True)
    (illus_dir / "burger.png").write_bytes(b"fake-burger-png")
    (illus_dir / "pasta.png").write_bytes(b"fake-pasta-png")
    monkeypatch.setattr(food_kinds, "STATIC_DIR", static_root)

    csv_path = tmp_path / "sample_t67.csv"
    csv_path.write_text(
        "식품코드,식품명,식품대분류명,식품중분류명,식품소분류명,식품기원명,업체명,에너지(kcal),단백질(g),지방(g),탄수화물(g),당류(g),나트륨(mg),식품중량\n"
        "D203-161000000-0001,스파게티_미트 토마토 파스타,면 및 만두류,스파게티,토마토,외식(분석함량),해당없음,170,7,4,26,4,420,450g\n"
        "D203-161000000-0002,스파게티_베이컨 크림 파스타,면 및 만두류,스파게티,크림,외식(분석함량),해당없음,210,8,9,24,3,480,450g\n"
        "D203-161000000-0003,스파게티_쉬림프 로제 파스타,면 및 만두류,스파게티,로제,외식(분석함량),해당없음,195,8,7,25,4,460,450g\n"
        "D303-161000000-0008,스파게티_간편조리세트_알리오올리오파스타,면 및 만두류,스파게티,오일,외식(분석함량),해당없음,185,6,6,27,2,410,450g\n"
        "F-BG-67,버거_치즈버거,빵 및 과자류,버거,치즈버거,외식(분석함량),해당없음,260,13,11,27,4,520,220g\n"
        "F-GB-67,국밥_돼지국밥,국 및 탕류,국밥,돼지국밥,외식(분석함량),해당없음,115,8,4,11,1,320,550g\n",
        encoding="utf-8-sig",
    )

    first = seed_database(
        db=sqlite_db,
        csv_path=csv_path,
        include_beverages_and_bakery=False,
        use_llm=False,
    )
    assert first["synced_food_kind_illustrations"] == 2
    assert first["inserted_illustration_exceptions"] == 3
    assert first["total_illustration_exceptions"] == 3

    burger_fk = sqlite_db.query(FoodKind).filter(FoodKind.kind_key == "burger").one()
    pasta_fk = sqlite_db.query(FoodKind).filter(FoodKind.kind_key == "pasta").one()
    gukbap_fk = sqlite_db.query(FoodKind).filter(FoodKind.kind_key == "gukbap").one()
    assert burger_fk.representative_image_url == "/static/menu-illustrations/burger.png"
    assert pasta_fk.representative_image_url == "/static/menu-illustrations/pasta.png"
    assert gukbap_fk.representative_image_url is None

    cream = sqlite_db.query(Menu).filter(Menu.food_code == "D203-161000000-0002").one()
    tomato = sqlite_db.query(Menu).filter(Menu.food_code == "D203-161000000-0001").one()
    assert sqlite_db.get(MenuIllustrationException, cream.id) is not None
    assert sqlite_db.get(MenuIllustrationException, tomato.id) is None

    # Customize representative URL and exception reason, then re-seed idempotently
    (illus_dir / "burger-custom.png").write_bytes(b"custom-burger-png")
    burger_fk.representative_image_url = "/static/menu-illustrations/burger-custom.png"
    sqlite_db.get(MenuIllustrationException, cream.id).reason = "수동 유지 사유"
    sqlite_db.commit()

    second = seed_database(
        db=sqlite_db,
        csv_path=csv_path,
        include_beverages_and_bakery=False,
        use_llm=False,
    )
    assert second["synced_food_kind_illustrations"] == 0
    assert second["inserted_illustration_exceptions"] == 0
    assert second["total_illustration_exceptions"] == 3
    sqlite_db.expire_all()
    assert (
        sqlite_db.query(FoodKind).filter(FoodKind.kind_key == "burger").one().representative_image_url
        == "/static/menu-illustrations/burger-custom.png"
    )
    assert sqlite_db.get(MenuIllustrationException, cream.id).reason == "수동 유지 사유"


def test_migrate_issue67_creates_exception_table_seeds_exceptions_and_syncs_idempotently(
    tmp_path: Path,
):
    static_root = tmp_path / "static"
    illus_dir = static_root / "menu-illustrations"
    illus_dir.mkdir(parents=True)
    (illus_dir / "pasta.png").write_bytes(b"fake-pasta-png")

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

    try:
        with engine.connect() as conn:
            FoodKind.__table__.create(conn)
            Menu.__table__.create(conn)
            conn.execute(
                text(
                    "INSERT INTO food_kinds (id, kind_key, name, representative_image_url, created_at) "
                    "VALUES (1, 'pasta', '파스타', NULL, '2026-11-20 00:00:00'), "
                    "       (2, 'gukbap', '국밥', NULL, '2026-11-20 00:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO menus (id, food_code, name, base_name, food_kind_id, category_large, origin_type, "
                    "meal_type, has_soup, cooking_style, main_ingredient, flavor_intensity, temperature, heaviness, allergen_tags) "
                    "VALUES "
                    "(10, 'D203-161000000-0001', '스파게티_미트 토마토 파스타', '스파게티', 1, '면 및 만두류', '외식', 'main_meal', 0, 'noodle', 'other', 'mild_clean', 'hot', 'moderate', '[]'), "
                    "(11, 'D203-161000000-0002', '스파게티_베이컨 크림 파스타', '스파게티', 1, '면 및 만두류', '외식', 'main_meal', 0, 'noodle', 'other', 'mild_clean', 'hot', 'moderate', '[]'), "
                    "(12, 'D203-161000000-0003', '스파게티_쉬림프 로제 파스타', '스파게티', 1, '면 및 만두류', '외식', 'main_meal', 0, 'noodle', 'other', 'mild_clean', 'hot', 'moderate', '[]')"
                )
            )
            conn.commit()

            assert "menu_illustration_exceptions" not in set(inspect(conn).get_table_names())

            manifest = migrate_issue67_illustrations.create_issue67_manifest(conn)
            first = migrate_issue67_illustrations.apply_issue67_migration(
                conn,
                manifest,
                static_dir=static_root,
            )
            assert first["inserted_exceptions"] == 2
            assert first["synced_illustrations"] == 1
            assert "menu_illustration_exceptions" in set(inspect(conn).get_table_names())

            exc_ids = [
                int(r[0])
                for r in conn.execute(
                    text("SELECT menu_id FROM menu_illustration_exceptions ORDER BY menu_id")
                ).all()
            ]
            assert exc_ids == [11, 12]

            pasta_url = conn.execute(
                text("SELECT representative_image_url FROM food_kinds WHERE kind_key = 'pasta'")
            ).scalar_one()
            gukbap_url = conn.execute(
                text("SELECT representative_image_url FROM food_kinds WHERE kind_key = 'gukbap'")
            ).scalar_one()
            assert pasta_url == "/static/menu-illustrations/pasta.png"
            assert gukbap_url is None

            # Idempotent re-apply and verify
            second = migrate_issue67_illustrations.apply_issue67_migration(
                conn,
                manifest,
                static_dir=static_root,
            )
            assert second["inserted_exceptions"] == 0
            assert second["synced_illustrations"] == 0
            migrate_issue67_illustrations.validate_issue67_schema(conn, final=True)
            migrate_issue67_illustrations.verify_issue67_preservation(conn, manifest)
    finally:
        engine.dispose()


@pytest.fixture
def mysql_engine():
    raw_url = os.environ.get("MYSQL_TEST_DATABASE_URL", "").strip()
    if not raw_url:
        pytest.skip("Set MYSQL_TEST_DATABASE_URL to run MySQL Ticket #67 integration tests.")

    url = make_url(raw_url)
    assert url.drivername.startswith("mysql"), "MYSQL_TEST_DATABASE_URL must use a MySQL driver."
    assert url.database and "test" in url.database.lower(), (
        "MYSQL_TEST_DATABASE_URL must point to an isolated test database."
    )

    schema = f"whatbob_t67_{uuid4().hex[:12]}"
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


def test_mysql_issue67_migration_cli_and_cascade_constraints(mysql_engine, tmp_path: Path):
    # Create pre-#67 schema (users, food_kinds, menus, user_meals — without menu_illustration_exceptions)
    with mysql_engine.connect() as conn:
        User.__table__.create(conn)
        FoodKind.__table__.create(conn)
        Menu.__table__.create(conn)
        UserMeal.__table__.create(conn)
        conn.commit()

    with Session(mysql_engine) as seed_db:
        seed_db.add(User(id=1, kakao_id=96701, nickname="MySQL파스타유저"))
        seed_db.add_all(
            [
                FoodKind(id=1, kind_key="pasta", name="파스타", representative_image_url=None),
                FoodKind(
                    id=2,
                    kind_key="burger",
                    name="햄버거",
                    representative_image_url="/static/menu-illustrations/custom-burger.png",
                ),
            ]
        )
        seed_db.flush()
        seed_db.add_all(
            [
                Menu(
                    id=101,
                    food_code="D203-161000000-0001",
                    name="스파게티_미트 토마토 파스타",
                    base_name="스파게티",
                    food_kind_id=1,
                    category_large="면 및 만두류",
                ),
                Menu(
                    id=102,
                    food_code="D203-161000000-0002",
                    name="스파게티_베이컨 크림 파스타",
                    base_name="스파게티",
                    food_kind_id=1,
                    category_large="면 및 만두류",
                ),
            ]
        )
        seed_db.flush()
        seed_db.add(
            UserMeal(
                id=501,
                user_id=1,
                menu_id=102,
                menu_name="베이컨 크림 파스타",
                eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            )
        )
        seed_db.commit()

    repo_root = Path(__file__).resolve().parent.parent
    manifest_path = tmp_path / "issue67-manifest.json"
    env = {
        **os.environ,
        "DATABASE_URL": mysql_engine.url.render_as_string(hide_password=False),
        "JWT_SECRET": "whatbob-mysql-issue67-migration-test-key",
        "KAKAO_REST_API_KEY": "test-kakao-key",
        "PYTHONPATH": str(repo_root),
    }

    def run_cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "migrate_issue67_illustrations.py"),
                "--manifest",
                str(manifest_path),
                *args,
            ],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    check_res = run_cli("--check")
    assert check_res.returncode == 0, check_res.stdout + check_res.stderr
    assert json.loads(check_res.stdout)["stage"] == "check"

    apply_res = run_cli("--apply", "--maintenance-confirmed")
    assert apply_res.returncode == 0, apply_res.stdout + apply_res.stderr
    apply_payload = json.loads(apply_res.stdout)
    assert apply_payload["stage"] == "apply"
    assert apply_payload["total_exceptions"] == 1

    verify_res = run_cli("--verify")
    assert verify_res.returncode == 0, verify_res.stdout + verify_res.stderr

    # Idempotent re-apply
    reapply_res = run_cli("--apply", "--maintenance-confirmed")
    assert reapply_res.returncode == 0, reapply_res.stdout + reapply_res.stderr

    with mysql_engine.connect() as conn:
        # Custom representative URL on burger preserved
        burger_url = conn.execute(
            text("SELECT representative_image_url FROM food_kinds WHERE kind_key = 'burger'")
        ).scalar_one()
        assert burger_url == "/static/menu-illustrations/custom-burger.png"

        # Duplicate menu_id rejected by PK
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO menu_illustration_exceptions (menu_id, reason, created_at) "
                    "VALUES (102, 'dup', '2026-11-20 12:00:00')"
                )
            )
        conn.rollback()

        # Non-existent menu_id rejected by FK
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO menu_illustration_exceptions (menu_id, reason, created_at) "
                    "VALUES (999999, 'orphan', '2026-11-20 12:00:00')"
                )
            )
        conn.rollback()

        # Deleting menu 102 cascades menu_illustration_exceptions and preserves user_meals.menu_name
        conn.execute(text("DELETE FROM menus WHERE id = 102"))
        conn.commit()

        remaining_exc = conn.execute(
            text("SELECT COUNT(*) FROM menu_illustration_exceptions WHERE menu_id = 102")
        ).scalar_one()
        assert remaining_exc == 0

        meal_row = (
            conn.execute(text("SELECT menu_id, menu_name FROM user_meals WHERE id = 501"))
            .mappings()
            .one()
        )
        assert meal_row["menu_id"] is None
        assert meal_row["menu_name"] == "베이컨 크림 파스타"


def test_mysql_recent_and_popular_home_apis_respect_representative_and_exception(mysql_engine):
    Base.metadata.create_all(mysql_engine)
    overrides = dict(app.dependency_overrides)
    fixed_now = datetime(2026, 11, 20, 15, 0, 0, tzinfo=timezone.utc)
    temp_pasta_file = f"_test_t67_mysql_pasta_{uuid4().hex[:8]}.svg"

    try:
        with _deployed_temp_static_file(temp_pasta_file) as pasta_url:
            with Session(mysql_engine) as db:
                app.dependency_overrides[get_db] = lambda: db
                app.dependency_overrides[get_utc_now] = lambda: fixed_now
                client = TestClient(app)

                ensure_initial_popular_food_kinds(db)
                register_food_kind_illustration(db, kind_key="pasta", image_url=pasta_url)

                user = User(kakao_id=96702, nickname="MySQL홈유저")
                tomato = _make_menu(
                    food_code="D203-161000000-0001",
                    name="스파게티_미트 토마토 파스타",
                    base_name="스파게티",
                )
                cream = _make_menu(
                    food_code="D203-161000000-0002",
                    name="스파게티_베이컨 크림 파스타",
                    base_name="스파게티",
                )
                db.add_all([user, tomato, cream])
                db.flush()

                db.add_all(
                    [
                        UserMeal(
                            user_id=user.id,
                            menu_id=tomato.id,
                            menu_name="미트 토마토 파스타",
                            eaten_at=datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc),
                        ),
                        UserMeal(
                            user_id=user.id,
                            menu_id=cream.id,
                            menu_name="베이컨 크림 파스타",
                            eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
                        ),
                    ]
                )
                db.commit()
                seed_menu_illustration_exceptions(db)

                token = session_token(db, user)
                recent_items = client.get(
                    RECENT_HOME_CARD_URL,
                    headers={"Authorization": f"Bearer {token}"},
                ).json()["data"]["items"]
                assert len(recent_items) == 1
                assert recent_items[0]["food_kind_id"] == cream.food_kind_id
                assert recent_items[0]["menu_name"] == "베이컨 크림 파스타"
                assert recent_items[0]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL

                popular_items = client.get(POPULAR_MENUS_URL).json()["data"]["items"]
                pop_pasta = next(i for i in popular_items if i["name"] == "파스타")
                assert pop_pasta["food_kind_id"] == cream.food_kind_id
                assert pop_pasta["image_url"] == pasta_url
                assert client.get(pasta_url).status_code == 200
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)





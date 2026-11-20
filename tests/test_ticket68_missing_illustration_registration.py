from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.auth.models  # noqa: F401
from app.core.database import Base, get_db
from app.main import app
from app.menus import illustrations
from app.menus.food_kinds import (
    CANONICAL_FOOD_KINDS,
    DEFAULT_MENU_ILLUSTRATION_URL,
    STATIC_DIR,
)
from app.menus.illustrations import (
    MenuIllustrationAssetCheck,
    deploy_and_register_food_kind_png,
    inspect_food_kind_illustration_status,
    seed_menu_illustration_exceptions,
    validate_menu_illustration_png,
)
from app.menus.models import FoodKind, Menu, UserMeal
from app.menus.popular import (
    aggregate_popular_menus_for_kst_date,
    ensure_initial_popular_food_kinds,
)
from app.users.models import User
from app.users.router import get_utc_now
from tests.auth_helpers import session_token

RECENT_HOME_CARD_URL = "/api/users/me/home-card-recently-eaten-food-max-6"
POPULAR_MENUS_URL = "/api/menus/popular"


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


def _write_png(
    path: Path,
    *,
    mode: str = "RGBA",
    size: tuple[int, int] = (128, 128),
    color: tuple[int, ...] | int = (240, 120, 40, 220),
    transparency: int | None = None,
) -> Path:
    img = Image.new(mode, size, color)
    save_kwargs: dict[str, object] = {}
    if transparency is not None:
        save_kwargs["transparency"] = transparency
    img.save(path, format="PNG", **save_kwargs)
    return path


def test_validate_menu_illustration_png_accepts_rgba_rgb_and_palette_transparency(
    tmp_path: Path,
):
    rgba_path = _write_png(
        tmp_path / "gukbap_rgba.png",
        mode="RGBA",
        size=(240, 180),
        color=(220, 90, 40, 200),
    )
    rgba_check = validate_menu_illustration_png(rgba_path)
    assert isinstance(rgba_check, MenuIllustrationAssetCheck)
    assert rgba_check.format == "PNG"
    assert rgba_check.width == 240
    assert rgba_check.height == 180
    assert rgba_check.mode == "RGBA"
    assert rgba_check.has_alpha is True
    assert rgba_check.size_bytes == rgba_path.stat().st_size

    rgb_path = _write_png(
        tmp_path / "gukbap_rgb.png",
        mode="RGB",
        size=(160, 160),
        color=(220, 90, 40),
    )
    rgb_check = validate_menu_illustration_png(rgb_path)
    assert rgb_check.width == 160
    assert rgb_check.height == 160
    assert rgb_check.mode == "RGB"
    assert rgb_check.has_alpha is False

    palette_path = _write_png(
        tmp_path / "jjajangmyeon_palette.png",
        mode="P",
        size=(96, 96),
        color=0,
        transparency=0,
    )
    palette_check = validate_menu_illustration_png(palette_path)
    assert palette_check.width == 96
    assert palette_check.height == 96
    assert palette_check.mode == "P"
    assert palette_check.has_alpha is True


def test_validate_menu_illustration_png_rejects_missing_empty_non_png_and_corrupted_files(
    tmp_path: Path,
):
    # Missing file
    with pytest.raises(FileNotFoundError):
        validate_menu_illustration_png(tmp_path / "missing.png")

    # Directory path
    sub_dir = tmp_path / "folder.png"
    sub_dir.mkdir()
    with pytest.raises(ValueError):
        validate_menu_illustration_png(sub_dir)

    # Empty file
    empty_file = tmp_path / "empty.png"
    empty_file.write_bytes(b"")
    with pytest.raises(ValueError):
        validate_menu_illustration_png(empty_file)

    # JPEG saved with .png extension
    fake_jpeg = tmp_path / "jpeg_disguised.png"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(fake_jpeg, format="JPEG")
    with pytest.raises(ValueError, match="PNG"):
        validate_menu_illustration_png(fake_jpeg)

    # SVG text saved with .png extension
    fake_svg = tmp_path / "svg_disguised.png"
    fake_svg.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'/>", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_menu_illustration_png(fake_svg)

    # Random bytes
    random_bytes = tmp_path / "random.png"
    random_bytes.write_bytes(b"\x01\x02\x03not-a-png-payload\xff\xfe")
    with pytest.raises(ValueError):
        validate_menu_illustration_png(random_bytes)

    # Corrupted / truncated PNG (valid header + truncated body)
    valid_png = _write_png(tmp_path / "source.png", size=(128, 128))
    raw = valid_png.read_bytes()
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(ValueError):
        validate_menu_illustration_png(truncated)

    # Corrupted PNG payload bytes in IDAT chunk
    corrupted_idat = tmp_path / "corrupted_idat.png"
    tampered_idat = bytearray(raw)
    tampered_idat[len(tampered_idat) // 2] ^= 0xFF
    corrupted_idat.write_bytes(bytes(tampered_idat))
    with pytest.raises(ValueError):
        validate_menu_illustration_png(corrupted_idat)

    # Corrupted PNG trailing IEND chunk
    corrupted_iend = tmp_path / "corrupted_iend.png"
    tampered_iend = bytearray(raw)
    tampered_iend[-10] ^= 0xFF
    corrupted_iend.write_bytes(bytes(tampered_iend))
    with pytest.raises(ValueError):
        validate_menu_illustration_png(corrupted_iend)


def test_deploy_and_register_food_kind_png_deploys_atomically_and_updates_db(
    sqlite_db: Session,
    tmp_path: Path,
):
    ensure_initial_popular_food_kinds(sqlite_db)
    static_root = tmp_path / "static"
    source_dir = tmp_path / "designer_drop"
    source_dir.mkdir()

    gukbap_src = _write_png(source_dir / "designer-gukbap-final.png", mode="RGBA", size=(256, 256))
    jj_src = _write_png(source_dir / "designer-jjajang-v2.png", mode="RGBA", size=(300, 300))

    # Default filename (<kind_key>.png)
    result = deploy_and_register_food_kind_png(
        sqlite_db,
        kind_key="gukbap",
        source_png=gukbap_src,
        static_dir=static_root,
        require_transparency=True,
    )
    deployed_gukbap = static_root / "menu-illustrations" / "gukbap.png"
    assert deployed_gukbap.is_file()
    assert deployed_gukbap.read_bytes() == gukbap_src.read_bytes()
    assert result["kind_key"] == "gukbap"
    assert result["name"] == "국밥"
    assert result["image_url"] == "/static/menu-illustrations/gukbap.png"
    assert result["previous_image_url"] is None
    assert result["width"] == 256
    assert result["height"] == 256
    assert result["mode"] == "RGBA"
    assert result["has_alpha"] is True
    assert Path(result["deployed_path"]).resolve() == deployed_gukbap.resolve()

    sqlite_db.expire_all()
    gukbap_fk = sqlite_db.query(FoodKind).filter(FoodKind.kind_key == "gukbap").one()
    assert gukbap_fk.representative_image_url == "/static/menu-illustrations/gukbap.png"

    # Custom filename + re-registering already-deployed file in place
    jj_result = deploy_and_register_food_kind_png(
        sqlite_db,
        kind_key="jjajangmyeon",
        source_png=jj_src,
        filename="jjajangmyeon-v2.png",
        static_dir=static_root,
        require_transparency=True,
    )
    deployed_jj = static_root / "menu-illustrations" / "jjajangmyeon-v2.png"
    assert deployed_jj.is_file()
    assert jj_result["image_url"] == "/static/menu-illustrations/jjajangmyeon-v2.png"

    inplace_result = deploy_and_register_food_kind_png(
        sqlite_db,
        kind_key="jjajangmyeon",
        source_png=deployed_jj,
        filename="jjajangmyeon-v2.png",
        static_dir=static_root,
    )
    assert inplace_result["image_url"] == "/static/menu-illustrations/jjajangmyeon-v2.png"
    assert inplace_result["previous_image_url"] == "/static/menu-illustrations/jjajangmyeon-v2.png"
    assert deployed_jj.read_bytes() == jj_src.read_bytes()


def test_deploy_and_register_food_kind_png_rejects_invalid_inputs_without_side_effects(
    sqlite_db: Session,
    tmp_path: Path,
):
    ensure_initial_popular_food_kinds(sqlite_db)
    static_root = tmp_path / "static"
    illus_dir = static_root / "menu-illustrations"
    illus_dir.mkdir(parents=True)

    valid_rgba = _write_png(tmp_path / "valid_rgba.png", mode="RGBA", size=(128, 128))
    opaque_rgb = _write_png(tmp_path / "opaque_rgb.png", mode="RGB", size=(128, 128), color=(10, 20, 30))
    fake_jpeg = tmp_path / "fake.png"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(fake_jpeg, format="JPEG")

    # Unknown kind_key -> rejected BEFORE writing disk
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        deploy_and_register_food_kind_png(
            sqlite_db,
            kind_key="nonexistent_kind",
            source_png=valid_rgba,
            static_dir=static_root,
        )

    # Path traversal and non-.png filenames
    for bad_filename in (
        "../evil.png",
        "..\\evil.png",
        "sub/evil.png",
        "sub\\evil.png",
        "gukbap..png",
        ".hidden.png",
        ".png",
        "gukbap.jpg",
        "gukbap.svg",
        "gukbap.PNG",
        "",
    ):
        with pytest.raises(ValueError):
            deploy_and_register_food_kind_png(
                sqlite_db,
                kind_key="gukbap",
                source_png=valid_rgba,
                filename=bad_filename,
                static_dir=static_root,
            )

    # JPEG disguised as PNG
    with pytest.raises(ValueError, match="PNG"):
        deploy_and_register_food_kind_png(
            sqlite_db,
            kind_key="gukbap",
            source_png=fake_jpeg,
            static_dir=static_root,
        )

    # Opaque RGB rejected when require_transparency=True
    with pytest.raises(ValueError, match="투명도|알파"):
        deploy_and_register_food_kind_png(
            sqlite_db,
            kind_key="gukbap",
            source_png=opaque_rgb,
            static_dir=static_root,
            require_transparency=True,
        )

    # Disk and DB remain completely unmodified
    assert list(illus_dir.iterdir()) == []
    sqlite_db.expire_all()
    assert sqlite_db.query(FoodKind).filter(FoodKind.kind_key == "gukbap").one().representative_image_url is None


def test_deploy_and_register_food_kind_png_cleans_up_or_restores_disk_on_db_failure(
    sqlite_db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    ensure_initial_popular_food_kinds(sqlite_db)
    static_root = tmp_path / "static"
    illus_dir = static_root / "menu-illustrations"
    illus_dir.mkdir(parents=True)

    first_png = _write_png(tmp_path / "gukbap_v1.png", mode="RGBA", size=(64, 64), color=(10, 20, 30, 255))
    second_png = _write_png(tmp_path / "gukbap_v2.png", mode="RGBA", size=(96, 96), color=(200, 100, 50, 255))

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated DB failure")

    # Failure when deploying brand-new file -> new file is cleaned up
    monkeypatch.setattr(illustrations, "register_food_kind_illustration", _boom)
    with pytest.raises(RuntimeError, match="simulated DB failure"):
        deploy_and_register_food_kind_png(
            sqlite_db,
            kind_key="gukbap",
            source_png=first_png,
            static_dir=static_root,
        )
    assert not (illus_dir / "gukbap.png").exists()
    assert list(illus_dir.iterdir()) == []

    # Failure when overwriting existing file -> previous file bytes are restored
    existing_target = illus_dir / "gukbap.png"
    existing_target.write_bytes(first_png.read_bytes())
    with pytest.raises(RuntimeError, match="simulated DB failure"):
        deploy_and_register_food_kind_png(
            sqlite_db,
            kind_key="gukbap",
            source_png=second_png,
            static_dir=static_root,
        )
    assert existing_target.is_file()
    assert existing_target.read_bytes() == first_png.read_bytes()


def test_inspect_food_kind_illustration_status_reports_audit_disk_and_live_effective_url(
    sqlite_db: Session,
    tmp_path: Path,
):
    ensure_initial_popular_food_kinds(sqlite_db)
    static_root = tmp_path / "static"
    illus_dir = static_root / "menu-illustrations"
    illus_dir.mkdir(parents=True)

    # State 1: nothing deployed yet
    initial_report = inspect_food_kind_illustration_status(sqlite_db, static_dir=static_root)
    assert [row["kind_key"] for row in initial_report] == [spec.kind_key for spec in CANONICAL_FOOD_KINDS]

    by_key = {row["kind_key"]: row for row in initial_report}
    assert by_key["gukbap"] == {
        "kind_key": "gukbap",
        "food_kind_id": by_key["gukbap"]["food_kind_id"],
        "name": "국밥",
        "audit_status": "missing_for_#68",
        "expected_image_url": "/static/menu-illustrations/gukbap.png",
        "registered_image_url": None,
        "file_exists_on_disk": False,
        "effective_image_url": DEFAULT_MENU_ILLUSTRATION_URL,
        "using_common_fallback": True,
    }
    assert by_key["jjajangmyeon"]["audit_status"] == "missing_for_#68"
    assert by_key["jjajangmyeon"]["effective_image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
    assert by_key["pasta"]["audit_status"] == "reusable_figma"

    # State 2: burger.png exists on disk, not yet registered in DB
    _write_png(illus_dir / "burger.png", mode="RGBA", size=(128, 128))
    # State 3: gukbap.png deployed and registered
    gukbap_src = _write_png(tmp_path / "designer_gukbap.png", mode="RGBA", size=(128, 128))
    deploy_and_register_food_kind_png(
        sqlite_db,
        kind_key="gukbap",
        source_png=gukbap_src,
        static_dir=static_root,
    )
    # State 4: pasta.png registered in DB, then removed from disk
    pasta_src = _write_png(tmp_path / "designer_pasta.png", mode="RGBA", size=(128, 128))
    deploy_and_register_food_kind_png(
        sqlite_db,
        kind_key="pasta",
        source_png=pasta_src,
        static_dir=static_root,
    )
    (illus_dir / "pasta.png").unlink()

    updated_by_key = {
        row["kind_key"]: row
        for row in inspect_food_kind_illustration_status(sqlite_db, static_dir=static_root)
    }
    assert updated_by_key["burger"]["registered_image_url"] is None
    assert updated_by_key["burger"]["file_exists_on_disk"] is True
    assert updated_by_key["burger"]["effective_image_url"] == DEFAULT_MENU_ILLUSTRATION_URL

    assert updated_by_key["gukbap"]["registered_image_url"] == "/static/menu-illustrations/gukbap.png"
    assert updated_by_key["gukbap"]["file_exists_on_disk"] is True
    assert updated_by_key["gukbap"]["effective_image_url"] == "/static/menu-illustrations/gukbap.png"
    assert updated_by_key["gukbap"]["using_common_fallback"] is False

    assert updated_by_key["pasta"]["registered_image_url"] == "/static/menu-illustrations/pasta.png"
    assert updated_by_key["pasta"]["file_exists_on_disk"] is False
    assert updated_by_key["pasta"]["effective_image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
    assert updated_by_key["pasta"]["using_common_fallback"] is True


def test_register_menu_illustration_cli_supports_status_deploy_existing_url_and_sync(
    tmp_path: Path,
):
    repo_root = Path(__file__).resolve().parent.parent
    db_file = tmp_path / "cli_t68.sqlite3"
    db_url = f"sqlite:///{db_file.as_posix()}"

    cli_engine = create_engine(db_url)
    Base.metadata.create_all(cli_engine)
    with Session(cli_engine) as seed_db:
        ensure_initial_popular_food_kinds(seed_db)
    cli_engine.dispose()

    static_root = tmp_path / "static"
    illus_dir = static_root / "menu-illustrations"
    illus_dir.mkdir(parents=True)

    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "JWT_SECRET": "whatbob-cli-t68-test-signing-key",
        "KAKAO_REST_API_KEY": "test-kakao-key",
        "PYTHONPATH": str(repo_root),
    }

    def run_cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.register_menu_illustration",
                "--static-dir",
                str(static_root),
                *args,
            ],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    # 1. --status reports all 8 kinds and highlights missing_for_#68 (gukbap, jjajangmyeon)
    status_before = run_cli("--status")
    assert status_before.returncode == 0, status_before.stdout + status_before.stderr
    before_payload = json.loads(status_before.stdout)
    assert before_payload["action"] == "status"
    assert before_payload["default_illustration_url"] == DEFAULT_MENU_ILLUSTRATION_URL
    assert before_payload["missing_for_issue68"] == ["gukbap", "jjajangmyeon"]
    assert before_payload["pending_for_issue68"] == ["gukbap", "jjajangmyeon"]
    assert len(before_payload["items"]) == 8
    assert all(
        item["effective_image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
        for item in before_payload["items"]
    )

    # 2. --kind-key gukbap --source-png ... --require-transparency
    gukbap_src = _write_png(tmp_path / "designer_gukbap.png", mode="RGBA", size=(256, 256))
    deploy_gukbap = run_cli(
        "--kind-key",
        "gukbap",
        "--source-png",
        str(gukbap_src),
        "--require-transparency",
    )
    assert deploy_gukbap.returncode == 0, deploy_gukbap.stdout + deploy_gukbap.stderr
    gukbap_payload = json.loads(deploy_gukbap.stdout)
    assert gukbap_payload["action"] == "deploy_and_register"
    assert gukbap_payload["kind_key"] == "gukbap"
    assert gukbap_payload["image_url"] == "/static/menu-illustrations/gukbap.png"
    assert gukbap_payload["has_alpha"] is True
    assert (illus_dir / "gukbap.png").is_file()

    # 3. --kind-key jjajangmyeon --source-png ... --filename jjajangmyeon-v2.png
    jj_src = _write_png(tmp_path / "designer_jj.png", mode="RGBA", size=(256, 256))
    deploy_jj = run_cli(
        "--kind-key",
        "jjajangmyeon",
        "--source-png",
        str(jj_src),
        "--filename",
        "jjajangmyeon-v2.png",
    )
    assert deploy_jj.returncode == 0, deploy_jj.stdout + deploy_jj.stderr
    jj_payload = json.loads(deploy_jj.stdout)
    assert jj_payload["image_url"] == "/static/menu-illustrations/jjajangmyeon-v2.png"
    assert (illus_dir / "jjajangmyeon-v2.png").is_file()

    # 4. --kind-key pasta --image-url ... fails when file is missing, succeeds after file exists
    missing_url_res = run_cli(
        "--kind-key",
        "pasta",
        "--image-url",
        "/static/menu-illustrations/pasta.png",
    )
    assert missing_url_res.returncode != 0

    _write_png(illus_dir / "pasta.png", mode="RGBA", size=(200, 200))
    existing_url_res = run_cli(
        "--kind-key",
        "pasta",
        "--image-url",
        "/static/menu-illustrations/pasta.png",
    )
    assert existing_url_res.returncode == 0, existing_url_res.stdout + existing_url_res.stderr
    pasta_payload = json.loads(existing_url_res.stdout)
    assert pasta_payload["action"] == "register_existing"
    assert pasta_payload["kind_key"] == "pasta"
    assert pasta_payload["image_url"] == "/static/menu-illustrations/pasta.png"

    # 5. --sync-deployed picks up newly placed burger.png without overwriting custom jjajangmyeon-v2.png
    _write_png(illus_dir / "burger.png", mode="RGBA", size=(200, 200))
    sync_res = run_cli("--sync-deployed")
    assert sync_res.returncode == 0, sync_res.stdout + sync_res.stderr
    sync_payload = json.loads(sync_res.stdout)
    assert sync_payload["action"] == "sync_deployed"
    assert sync_payload["updated"] == 1

    # 6. --status reflects live state after all operations
    status_after = run_cli("--status")
    assert status_after.returncode == 0, status_after.stdout + status_after.stderr
    after_payload = json.loads(status_after.stdout)
    assert after_payload["pending_for_issue68"] == []
    after_by_key = {item["kind_key"]: item for item in after_payload["items"]}
    assert after_by_key["gukbap"]["effective_image_url"] == "/static/menu-illustrations/gukbap.png"
    assert (
        after_by_key["jjajangmyeon"]["effective_image_url"]
        == "/static/menu-illustrations/jjajangmyeon-v2.png"
    )
    assert after_by_key["pasta"]["effective_image_url"] == "/static/menu-illustrations/pasta.png"
    assert after_by_key["burger"]["effective_image_url"] == "/static/menu-illustrations/burger.png"
    assert after_by_key["malatang"]["effective_image_url"] == DEFAULT_MENU_ILLUSTRATION_URL

    # 7. Invalid CLI usage and bad PNG fail cleanly without touching DB or disk
    bad_png = tmp_path / "not_a_png.png"
    bad_png.write_bytes(b"definitely-not-a-png")
    bad_png_res = run_cli("--kind-key", "malatang", "--source-png", str(bad_png))
    assert bad_png_res.returncode != 0
    assert not (illus_dir / "malatang.png").exists()

    bad_args_res = run_cli("--kind-key", "malatang")
    assert bad_args_res.returncode != 0
    conflict_args_res = run_cli(
        "--kind-key",
        "malatang",
        "--source-png",
        str(gukbap_src),
        "--image-url",
        "/static/menu-illustrations/malatang.png",
    )
    assert conflict_args_res.returncode != 0


@contextmanager
def _guarded_static_cleanup(*filenames: str):
    """Guarantees app/static/menu-illustrations/ is restored to its exact prior state after test."""
    illus_dir = STATIC_DIR / "menu-illustrations"
    illus_dir.mkdir(parents=True, exist_ok=True)
    backups: dict[Path, bytes | None] = {}
    for name in filenames:
        path = illus_dir / name
        backups[path] = path.read_bytes() if path.is_file() else None
    try:
        yield illus_dir
    finally:
        for path, prior_bytes in backups.items():
            if prior_bytes is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes(prior_bytes)


def _make_menu(*, food_code: str, name: str, base_name: str, category_large: str = "면 및 만두류") -> Menu:
    return Menu(
        food_code=food_code,
        name=name,
        base_name=base_name,
        category_large=category_large,
    )


def _seed_five_user_previous_day_meals(
    db: Session,
    *,
    gukbap_menu: Menu,
    jj_menu: Menu,
    tomato_pasta_menu: Menu,
    cream_pasta_menu: Menu,
) -> list[User]:
    """Seeds 5 distinct users eating on KST 2026-11-19 so midnight 2026-11-20 KST publishes:
    1. gukbap (3 users)
    2. jjajangmyeon (2 users, latest 11:30 UTC)
    3. pasta (2 users: tomato + cream, latest 10:30 UTC)
    """
    users = [User(kakao_id=68100 + i, nickname=f"집계유저{i}") for i in range(1, 6)]
    db.add_all(users)
    db.flush()

    # KST 2026-11-19 is [2026-11-18T15:00:00Z, 2026-11-19T15:00:00Z)
    db.add_all(
        [
            # 3 users ate gukbap
            UserMeal(
                user_id=users[0].id,
                menu_id=gukbap_menu.id,
                menu_name="돼지국밥",
                eaten_at=datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=users[1].id,
                menu_id=gukbap_menu.id,
                menu_name="돼지국밥",
                eaten_at=datetime(2026, 11, 19, 4, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=users[2].id,
                menu_id=gukbap_menu.id,
                menu_name="돼지국밥",
                eaten_at=datetime(2026, 11, 19, 5, 0, tzinfo=timezone.utc),
            ),
            # 2 users ate jjajangmyeon (later max eaten_at than pasta -> rank 2)
            UserMeal(
                user_id=users[3].id,
                menu_id=jj_menu.id,
                menu_name="간짜장",
                eaten_at=datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=users[4].id,
                menu_id=jj_menu.id,
                menu_name="간짜장",
                eaten_at=datetime(2026, 11, 19, 11, 30, tzinfo=timezone.utc),
            ),
            # 2 users ate pasta (1 cream, 1 tomato -> rank 3)
            UserMeal(
                user_id=users[0].id,
                menu_id=cream_pasta_menu.id,
                menu_name="베이컨 크림 파스타",
                eaten_at=datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc),
            ),
            UserMeal(
                user_id=users[1].id,
                menu_id=tomato_pasta_menu.id,
                menu_name="미트 토마토 파스타",
                eaten_at=datetime(2026, 11, 19, 10, 30, tzinfo=timezone.utc),
            ),
        ]
    )
    db.commit()
    return users


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


def test_missing_illustrations_fallback_to_common_image_across_recent_initial_and_aggregated_popular(
    home_api,
):
    db = home_api.db
    client = home_api.client
    ensure_initial_popular_food_kinds(db)

    gukbap_menu = _make_menu(
        food_code="T68-GB-1",
        name="국밥_돼지국밥",
        base_name="국밥",
        category_large="국 및 탕류",
    )
    jj_menu = _make_menu(
        food_code="T68-JJ-1",
        name="짜장면_간짜장",
        base_name="짜장면",
    )
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
    db.add_all([gukbap_menu, jj_menu, tomato_pasta, cream_pasta])
    db.flush()

    users = _seed_five_user_previous_day_meals(
        db,
        gukbap_menu=gukbap_menu,
        jj_menu=jj_menu,
        tomato_pasta_menu=tomato_pasta,
        cream_pasta_menu=cream_pasta,
    )
    # Give users[0] a recent meal of jjajangmyeon too so recent cards cover gukbap + jjajangmyeon + pasta
    db.add(
        UserMeal(
            user_id=users[0].id,
            menu_id=jj_menu.id,
            menu_name="간짜장",
            eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
        )
    )
    db.commit()

    gukbap_fk = db.query(FoodKind).filter(FoodKind.kind_key == "gukbap").one()
    jj_fk = db.query(FoodKind).filter(FoodKind.kind_key == "jjajangmyeon").one()
    pasta_fk = db.query(FoodKind).filter(FoodKind.kind_key == "pasta").one()
    token_u0 = session_token(db, users[0])

    # Before aggregation: initial popular 8 items & recent cards all fall back to common image
    initial_pop = client.get(POPULAR_MENUS_URL)
    assert initial_pop.status_code == 200
    initial_data = initial_pop.json()["data"]
    assert initial_data["source"] == "initial"
    assert initial_data["as_of"] is None
    assert [item["name"] for item in initial_data["items"]] == [
        "떡볶이",
        "돈까스",
        "햄버거",
        "마라탕",
        "파스타",
        "국밥",
        "짜장면",
        "제육볶음",
    ]
    initial_by_name = {item["name"]: item for item in initial_data["items"]}
    assert initial_by_name["국밥"]["food_kind_id"] == gukbap_fk.id
    assert initial_by_name["국밥"]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
    assert initial_by_name["짜장면"]["food_kind_id"] == jj_fk.id
    assert initial_by_name["짜장면"]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL

    recent_resp = client.get(RECENT_HOME_CARD_URL, headers={"Authorization": f"Bearer {token_u0}"})
    assert recent_resp.status_code == 200
    assert [(i["food_kind_id"], i["menu_name"], i["image_url"]) for i in recent_resp.json()["data"]["items"]] == [
        (jj_fk.id, "간짜장", DEFAULT_MENU_ILLUSTRATION_URL),
        (pasta_fk.id, "베이컨 크림 파스타", DEFAULT_MENU_ILLUSTRATION_URL),
        (gukbap_fk.id, "돼지국밥", DEFAULT_MENU_ILLUSTRATION_URL),
    ]

    # Publish aggregated ranking for 2026-11-20 KST midnight
    agg_result = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert agg_result.published is True

    agg_pop = client.get(POPULAR_MENUS_URL)
    assert agg_pop.status_code == 200
    agg_data = agg_pop.json()["data"]
    assert agg_data["source"] == "aggregated"
    assert agg_data["as_of"] == "2026-11-19T15:00:00Z"
    assert [(i["food_kind_id"], i["name"], i["image_url"]) for i in agg_data["items"]] == [
        (gukbap_fk.id, "국밥", DEFAULT_MENU_ILLUSTRATION_URL),
        (jj_fk.id, "짜장면", DEFAULT_MENU_ILLUSTRATION_URL),
        (pasta_fk.id, "파스타", DEFAULT_MENU_ILLUSTRATION_URL),
    ]

    # Common fallback image opens with HTTP 200
    default_img_resp = client.get(DEFAULT_MENU_ILLUSTRATION_URL)
    assert default_img_resp.status_code == 200


def test_deploying_designer_pngs_updates_recent_initial_and_aggregated_popular_immediately_preserving_exceptions_and_fallback(
    tmp_path: Path,
):
    repo_root = Path(__file__).resolve().parent.parent
    db_file = tmp_path / "e2e_t68.sqlite3"
    db_url = f"sqlite:///{db_file.as_posix()}"
    e2e_engine = create_engine(db_url, connect_args={"check_same_thread": False})

    @event.listens_for(e2e_engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(e2e_engine)

    overrides = dict(app.dependency_overrides)
    fixed_now = datetime(2026, 11, 20, 15, 0, 0, tzinfo=timezone.utc)

    # Prepare temporary designer RGBA PNGs outside static dir
    gukbap_src = _write_png(tmp_path / "designer_gukbap.png", mode="RGBA", size=(256, 256), color=(200, 90, 50, 255))
    jj_src = _write_png(tmp_path / "designer_jjajangmyeon.png", mode="RGBA", size=(256, 256), color=(40, 30, 30, 255))
    pasta_src = _write_png(tmp_path / "designer_pasta.png", mode="RGBA", size=(256, 256), color=(210, 50, 40, 255))

    cli_env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "JWT_SECRET": "whatbob-e2e-t68-test-signing-key",
        "KAKAO_REST_API_KEY": "test-kakao-key",
        "PYTHONPATH": str(repo_root),
    }

    try:
        with _guarded_static_cleanup("gukbap.png", "jjajangmyeon.png", "pasta.png") as illus_dir:
            with Session(e2e_engine) as db:
                ensure_initial_popular_food_kinds(db)
                gukbap_menu = _make_menu(
                    food_code="T68-E2E-GB",
                    name="국밥_돼지국밥",
                    base_name="국밥",
                    category_large="국 및 탕류",
                )
                jj_menu = _make_menu(
                    food_code="T68-E2E-JJ",
                    name="짜장면_간짜장",
                    base_name="짜장면",
                )
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
                db.add_all([gukbap_menu, jj_menu, tomato_pasta, cream_pasta])
                db.flush()

                users = _seed_five_user_previous_day_meals(
                    db,
                    gukbap_menu=gukbap_menu,
                    jj_menu=jj_menu,
                    tomato_pasta_menu=tomato_pasta,
                    cream_pasta_menu=cream_pasta,
                )
                seed_menu_illustration_exceptions(db)

                # User 0 has cream pasta (excepted) + gukbap + jjajangmyeon
                db.add(
                    UserMeal(
                        user_id=users[0].id,
                        menu_id=jj_menu.id,
                        menu_name="간짜장",
                        eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
                    )
                )
                db.commit()

                gukbap_id = gukbap_menu.food_kind_id
                jj_id = jj_menu.food_kind_id
                pasta_id = tomato_pasta.food_kind_id
                token_cream_user = session_token(db, users[0])
                token_tomato_user = session_token(db, users[1])

            def _get_fresh_db():
                with Session(e2e_engine) as session:
                    yield session

            app.dependency_overrides[get_db] = _get_fresh_db
            app.dependency_overrides[get_utc_now] = lambda: fixed_now
            client = TestClient(app)

            # 1. Deploy gukbap.png via service while popular is still in source="initial" mode
            with Session(e2e_engine) as op_db:
                deploy_and_register_food_kind_png(
                    op_db,
                    kind_key="gukbap",
                    source_png=gukbap_src,
                    require_transparency=True,
                )

            initial_pop = client.get(POPULAR_MENUS_URL).json()["data"]
            assert initial_pop["source"] == "initial"
            assert initial_pop["as_of"] is None
            initial_by_name = {item["name"]: item for item in initial_pop["items"]}
            assert initial_by_name["국밥"]["image_url"] == "/static/menu-illustrations/gukbap.png"
            assert initial_by_name["짜장면"]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL

            gukbap_static_resp = client.get("/static/menu-illustrations/gukbap.png")
            assert gukbap_static_resp.status_code == 200
            assert gukbap_static_resp.headers["content-type"].startswith("image/png")

            # 2. Publish aggregated popular ranking BEFORE jjajangmyeon.png & pasta.png are deployed
            with Session(e2e_engine) as op_db:
                agg = aggregate_popular_menus_for_kst_date(op_db, date(2026, 11, 20))
                assert agg.published is True

            pop_before_cli = client.get(POPULAR_MENUS_URL).json()["data"]
            assert pop_before_cli["source"] == "aggregated"
            baseline_as_of = pop_before_cli["as_of"]
            assert baseline_as_of == "2026-11-19T15:00:00Z"
            assert [(i["food_kind_id"], i["name"], i["image_url"]) for i in pop_before_cli["items"]] == [
                (gukbap_id, "국밥", "/static/menu-illustrations/gukbap.png"),
                (jj_id, "짜장면", DEFAULT_MENU_ILLUSTRATION_URL),
                (pasta_id, "파스타", DEFAULT_MENU_ILLUSTRATION_URL),
            ]

            # 3. Deploy jjajangmyeon.png via CLI and pasta.png via service WITHOUT re-running aggregation!
            cli_res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.register_menu_illustration",
                    "--kind-key",
                    "jjajangmyeon",
                    "--source-png",
                    str(jj_src),
                    "--require-transparency",
                ],
                cwd=str(repo_root),
                env=cli_env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert cli_res.returncode == 0, cli_res.stdout + cli_res.stderr
            assert json.loads(cli_res.stdout)["image_url"] == "/static/menu-illustrations/jjajangmyeon.png"

            with Session(e2e_engine) as op_db:
                deploy_and_register_food_kind_png(
                    op_db,
                    kind_key="pasta",
                    source_png=pasta_src,
                    require_transparency=True,
                )

            # Aggregated popular immediately serves all 3 PNGs with UNCHANGED as_of and card order!
            pop_after_deploy = client.get(POPULAR_MENUS_URL).json()["data"]
            assert pop_after_deploy["source"] == "aggregated"
            assert pop_after_deploy["as_of"] == baseline_as_of
            assert [(i["food_kind_id"], i["name"], i["image_url"]) for i in pop_after_deploy["items"]] == [
                (gukbap_id, "국밥", "/static/menu-illustrations/gukbap.png"),
                (jj_id, "짜장면", "/static/menu-illustrations/jjajangmyeon.png"),
                (pasta_id, "파스타", "/static/menu-illustrations/pasta.png"),
            ]

            # Static PNGs all return HTTP 200 image/png
            for url in (
                "/static/menu-illustrations/gukbap.png",
                "/static/menu-illustrations/jjajangmyeon.png",
                "/static/menu-illustrations/pasta.png",
            ):
                img_resp = client.get(url)
                assert img_resp.status_code == 200
                assert img_resp.headers["content-type"].startswith("image/png")

            # Recent cards:
            # - Cream pasta (excepted) STILL returns DEFAULT_MENU_ILLUSTRATION_URL!
            # - jjajangmyeon & gukbap immediately return their new PNG URLs!
            recent_cream = client.get(
                RECENT_HOME_CARD_URL,
                headers={"Authorization": f"Bearer {token_cream_user}"},
            ).json()["data"]["items"]
            assert [(i["food_kind_id"], i["menu_name"], i["image_url"]) for i in recent_cream] == [
                (jj_id, "간짜장", "/static/menu-illustrations/jjajangmyeon.png"),
                (pasta_id, "베이컨 크림 파스타", DEFAULT_MENU_ILLUSTRATION_URL),
                (gukbap_id, "돼지국밥", "/static/menu-illustrations/gukbap.png"),
            ]

            # - Tomato pasta (NOT excepted) immediately returns /static/menu-illustrations/pasta.png!
            recent_tomato = client.get(
                RECENT_HOME_CARD_URL,
                headers={"Authorization": f"Bearer {token_tomato_user}"},
            ).json()["data"]["items"]
            assert [(i["food_kind_id"], i["menu_name"], i["image_url"]) for i in recent_tomato] == [
                (pasta_id, "미트 토마토 파스타", "/static/menu-illustrations/pasta.png"),
                (gukbap_id, "돼지국밥", "/static/menu-illustrations/gukbap.png"),
            ]

            # 4. Removing a registered PNG file (gukbap.png) from disk gracefully falls back on BOTH APIs!
            (illus_dir / "gukbap.png").unlink()

            pop_after_unlink = client.get(POPULAR_MENUS_URL)
            assert pop_after_unlink.status_code == 200
            pop_unlink_data = pop_after_unlink.json()["data"]
            assert pop_unlink_data["as_of"] == baseline_as_of
            assert [(i["food_kind_id"], i["name"], i["image_url"]) for i in pop_unlink_data["items"]] == [
                (gukbap_id, "국밥", DEFAULT_MENU_ILLUSTRATION_URL),
                (jj_id, "짜장면", "/static/menu-illustrations/jjajangmyeon.png"),
                (pasta_id, "파스타", "/static/menu-illustrations/pasta.png"),
            ]

            recent_after_unlink = client.get(
                RECENT_HOME_CARD_URL,
                headers={"Authorization": f"Bearer {token_cream_user}"},
            )
            assert recent_after_unlink.status_code == 200
            assert [(i["food_kind_id"], i["menu_name"], i["image_url"]) for i in recent_after_unlink.json()["data"]["items"]] == [
                (jj_id, "간짜장", "/static/menu-illustrations/jjajangmyeon.png"),
                (pasta_id, "베이컨 크림 파스타", DEFAULT_MENU_ILLUSTRATION_URL),
                (gukbap_id, "돼지국밥", DEFAULT_MENU_ILLUSTRATION_URL),
            ]
            assert client.get(DEFAULT_MENU_ILLUSTRATION_URL).status_code == 200
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        e2e_engine.dispose()


@pytest.fixture
def mysql_engine():
    raw_url = os.environ.get("MYSQL_TEST_DATABASE_URL", "").strip()
    if not raw_url:
        pytest.skip("Set MYSQL_TEST_DATABASE_URL to run MySQL Ticket #68 integration tests.")

    url = make_url(raw_url)
    assert url.drivername.startswith("mysql"), "MYSQL_TEST_DATABASE_URL must use a MySQL driver."
    assert url.database and "test" in url.database.lower(), (
        "MYSQL_TEST_DATABASE_URL must point to an isolated test database."
    )

    schema = f"whatbob_t68_{uuid4().hex[:12]}"
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


def test_mysql_issue68_cli_registration_and_immediate_home_api_reflection(
    mysql_engine,
    tmp_path: Path,
):
    Base.metadata.create_all(mysql_engine)
    overrides = dict(app.dependency_overrides)
    fixed_now = datetime(2026, 11, 20, 15, 0, 0, tzinfo=timezone.utc)
    repo_root = Path(__file__).resolve().parent.parent

    gukbap_src = _write_png(tmp_path / "mysql_gukbap.png", mode="RGBA", size=(256, 256), color=(200, 90, 50, 255))
    jj_src = _write_png(tmp_path / "mysql_jjajangmyeon.png", mode="RGBA", size=(256, 256), color=(40, 30, 30, 255))
    pasta_src = _write_png(tmp_path / "mysql_pasta.png", mode="RGBA", size=(256, 256), color=(210, 50, 40, 255))

    env = {
        **os.environ,
        "DATABASE_URL": mysql_engine.url.render_as_string(hide_password=False),
        "JWT_SECRET": "whatbob-mysql-t68-test-signing-key",
        "KAKAO_REST_API_KEY": "test-kakao-key",
        "PYTHONPATH": str(repo_root),
    }

    def run_cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "scripts.register_menu_illustration", *args],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    try:
        with _guarded_static_cleanup("gukbap.png", "jjajangmyeon.png", "pasta.png") as illus_dir:
            with Session(mysql_engine) as seed_db:
                ensure_initial_popular_food_kinds(seed_db)
                gukbap_menu = _make_menu(
                    food_code="T68-MYSQL-GB",
                    name="국밥_돼지국밥",
                    base_name="국밥",
                    category_large="국 및 탕류",
                )
                jj_menu = _make_menu(
                    food_code="T68-MYSQL-JJ",
                    name="짜장면_간짜장",
                    base_name="짜장면",
                )
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
                seed_db.add_all([gukbap_menu, jj_menu, tomato_pasta, cream_pasta])
                seed_db.flush()

                users = _seed_five_user_previous_day_meals(
                    seed_db,
                    gukbap_menu=gukbap_menu,
                    jj_menu=jj_menu,
                    tomato_pasta_menu=tomato_pasta,
                    cream_pasta_menu=cream_pasta,
                )
                seed_menu_illustration_exceptions(seed_db)
                seed_db.add(
                    UserMeal(
                        user_id=users[0].id,
                        menu_id=jj_menu.id,
                        menu_name="간짜장",
                        eaten_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
                    )
                )
                seed_db.commit()

                agg = aggregate_popular_menus_for_kst_date(seed_db, date(2026, 11, 20))
                assert agg.published is True

                gukbap_id = gukbap_menu.food_kind_id
                jj_id = jj_menu.food_kind_id
                pasta_id = tomato_pasta.food_kind_id
                token_cream_user = session_token(seed_db, users[0])
                token_tomato_user = session_token(seed_db, users[1])

            # 1. CLI --status before registration
            status_before = run_cli("--status")
            assert status_before.returncode == 0, status_before.stdout + status_before.stderr
            before_json = json.loads(status_before.stdout)
            assert before_json["missing_for_issue68"] == ["gukbap", "jjajangmyeon"]
            assert before_json["pending_for_issue68"] == ["gukbap", "jjajangmyeon"]

            # 2. Register gukbap via --source-png, jjajangmyeon via --image-url, pasta via --sync-deployed
            res_gb = run_cli(
                "--kind-key",
                "gukbap",
                "--source-png",
                str(gukbap_src),
                "--require-transparency",
            )
            assert res_gb.returncode == 0, res_gb.stdout + res_gb.stderr
            assert json.loads(res_gb.stdout)["image_url"] == "/static/menu-illustrations/gukbap.png"

            (illus_dir / "jjajangmyeon.png").write_bytes(jj_src.read_bytes())
            res_jj = run_cli(
                "--kind-key",
                "jjajangmyeon",
                "--image-url",
                "/static/menu-illustrations/jjajangmyeon.png",
            )
            assert res_jj.returncode == 0, res_jj.stdout + res_jj.stderr
            assert json.loads(res_jj.stdout)["image_url"] == "/static/menu-illustrations/jjajangmyeon.png"

            (illus_dir / "pasta.png").write_bytes(pasta_src.read_bytes())
            res_sync = run_cli("--sync-deployed")
            assert res_sync.returncode == 0, res_sync.stdout + res_sync.stderr
            assert json.loads(res_sync.stdout)["updated"] == 1

            # 3. CLI --status confirms DB and live effective URLs
            status_after = run_cli("--status")
            assert status_after.returncode == 0, status_after.stdout + status_after.stderr
            after_json = json.loads(status_after.stdout)
            assert after_json["pending_for_issue68"] == []
            after_by_key = {item["kind_key"]: item for item in after_json["items"]}
            assert after_by_key["gukbap"]["effective_image_url"] == "/static/menu-illustrations/gukbap.png"
            assert (
                after_by_key["jjajangmyeon"]["effective_image_url"]
                == "/static/menu-illustrations/jjajangmyeon.png"
            )
            assert after_by_key["pasta"]["effective_image_url"] == "/static/menu-illustrations/pasta.png"

            # Verify directly in MySQL DB
            with mysql_engine.connect() as conn:
                rows = dict(
                    conn.execute(
                        text(
                            "SELECT kind_key, representative_image_url FROM food_kinds "
                            "WHERE kind_key IN ('gukbap', 'jjajangmyeon', 'pasta')"
                        )
                    ).all()
                )
            assert rows == {
                "gukbap": "/static/menu-illustrations/gukbap.png",
                "jjajangmyeon": "/static/menu-illustrations/jjajangmyeon.png",
                "pasta": "/static/menu-illustrations/pasta.png",
            }

            # 4. Both Home APIs immediately reflect registered PNGs without re-running aggregation
            def _get_mysql_db():
                with Session(mysql_engine) as session:
                    yield session

            app.dependency_overrides[get_db] = _get_mysql_db
            app.dependency_overrides[get_utc_now] = lambda: fixed_now
            client = TestClient(app)

            pop_resp = client.get(POPULAR_MENUS_URL)
            assert pop_resp.status_code == 200
            pop_data = pop_resp.json()["data"]
            assert pop_data["source"] == "aggregated"
            assert pop_data["as_of"] == "2026-11-19T15:00:00Z"
            assert [(i["food_kind_id"], i["name"], i["image_url"]) for i in pop_data["items"]] == [
                (gukbap_id, "국밥", "/static/menu-illustrations/gukbap.png"),
                (jj_id, "짜장면", "/static/menu-illustrations/jjajangmyeon.png"),
                (pasta_id, "파스타", "/static/menu-illustrations/pasta.png"),
            ]

            recent_cream = client.get(
                RECENT_HOME_CARD_URL,
                headers={"Authorization": f"Bearer {token_cream_user}"},
            ).json()["data"]["items"]
            assert [(i["food_kind_id"], i["menu_name"], i["image_url"]) for i in recent_cream] == [
                (jj_id, "간짜장", "/static/menu-illustrations/jjajangmyeon.png"),
                (pasta_id, "베이컨 크림 파스타", DEFAULT_MENU_ILLUSTRATION_URL),
                (gukbap_id, "돼지국밥", "/static/menu-illustrations/gukbap.png"),
            ]

            recent_tomato = client.get(
                RECENT_HOME_CARD_URL,
                headers={"Authorization": f"Bearer {token_tomato_user}"},
            ).json()["data"]["items"]
            assert [(i["food_kind_id"], i["menu_name"], i["image_url"]) for i in recent_tomato] == [
                (pasta_id, "미트 토마토 파스타", "/static/menu-illustrations/pasta.png"),
                (gukbap_id, "돼지국밥", "/static/menu-illustrations/gukbap.png"),
            ]

            gb_http = client.get("/static/menu-illustrations/gukbap.png")
            assert gb_http.status_code == 200
            assert gb_http.headers["content-type"].startswith("image/png")

            # 5. Removing gukbap.png from disk falls back gracefully on both MySQL-backed APIs
            (illus_dir / "gukbap.png").unlink()
            pop_after_unlink = client.get(POPULAR_MENUS_URL).json()["data"]
            assert pop_after_unlink["items"][0]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
            recent_after_unlink = client.get(
                RECENT_HOME_CARD_URL,
                headers={"Authorization": f"Bearer {token_cream_user}"},
            ).json()["data"]["items"]
            assert recent_after_unlink[2]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)

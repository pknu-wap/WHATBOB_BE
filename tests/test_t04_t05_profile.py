from io import BytesIO
import os
from pathlib import Path
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.sessions import DEFAULT_PROFILE_IMAGE, new_session
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.security import create_access_token
import app.auth.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.rooms.models  # noqa: F401
from app.main import app
from app.users.models import User
from app.users.profile_image import cleanup_orphan_profile_images


def _issue_session_token(db, user: User) -> str:
    session_record, _ = new_session(user_id=user.id)
    db.add(session_record)
    db.commit()
    db.refresh(session_record)
    return create_access_token({"sub": str(user.id), "sid": str(session_record.id)})


def _make_image_bytes(
    fmt: str = "PNG",
    size: tuple[int, int] = (200, 150),
    color: tuple[int, int, int] = (120, 180, 240),
    exif_bytes: bytes | None = None,
) -> bytes:
    buf = BytesIO()
    img = Image.new("RGB", size, color=color)
    save_kwargs: dict[str, object] = {}
    if exif_bytes is not None:
        save_kwargs["exif"] = exif_bytes
    img.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def _make_animated_webp_bytes() -> bytes:
    buf = BytesIO()
    frame1 = Image.new("RGB", (64, 64), (255, 0, 0))
    frame2 = Image.new("RGB", (64, 64), (0, 255, 0))
    frame1.save(buf, format="WEBP", save_all=True, append_images=[frame2], duration=100, loop=0)
    return buf.getvalue()


@pytest.fixture()
def profile_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    upload_dir = tmp_path / "profile-images"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROFILE_IMAGE_UPLOAD_DIR", str(upload_dir))

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    with TestingSessionLocal() as db:
        u1 = User(
            kakao_id=None,
            nickname="기본유저01",
            nickname_key="기본유저01".encode("utf-8"),
            profile_image=DEFAULT_PROFILE_IMAGE,
        )
        u2 = User(
            kakao_id=None,
            nickname="다른유저02",
            nickname_key="다른유저02".encode("utf-8"),
            profile_image=DEFAULT_PROFILE_IMAGE,
        )
        db.add_all([u1, u2])
        db.commit()
        db.refresh(u1)
        db.refresh(u2)
        t1 = _issue_session_token(db, u1)
        t2 = _issue_session_token(db, u2)
        u1_id, u2_id = u1.id, u2.id

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

    monkeypatch.setattr("app.rooms.cleanup.SessionLocal", TestingSessionLocal)
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield {
                "client": client,
                "SessionLocal": TestingSessionLocal,
                "upload_dir": upload_dir,
                "u1_id": u1_id,
                "u2_id": u2_id,
                "t1": t1,
                "t2": t2,
            }
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_t04_nickname_validation_trimming_uniqueness_and_provenance(profile_env):
    client: TestClient = profile_env["client"]
    h1 = {"Authorization": f"Bearer {profile_env['t1']}"}
    h2 = {"Authorization": f"Bearer {profile_env['t2']}"}

    # 1. 미인증 시 401
    assert client.patch("/api/users/me/nickname", json={"nickname": "새닉네임"}).status_code == 401

    # 2. 길이/문자 검증 (1자, 21자, 특수문자/이모지/자모 단독 거절)
    for bad in ["가", "가" * 21, "닉네임!", "ㄱㄴㄷ", "줄바꿈\n닉네임", "   "]:
        res = client.patch("/api/users/me/nickname", json={"nickname": bad}, headers=h1)
        assert res.status_code == 400
        assert res.json()["detail"]["code"] == "INVALID_NICKNAME"

    # 3. 앞뒤 공백 제거 후 정상 저장 (한글/영문/숫자/내부 공백 허용)
    ok = client.patch("/api/users/me/nickname", json={"nickname": "  맛집 탐험가 01  "}, headers=h1)
    assert ok.status_code == 200
    assert ok.json()["nickname"] == "맛집 탐험가 01"

    # 4. 자기 자신의 현재 닉네임으로 다시 저장 시 성공 (35번 스토리)
    same = client.patch("/api/users/me/nickname", json={"nickname": "맛집 탐험가 01"}, headers=h1)
    assert same.status_code == 200
    assert same.json()["nickname"] == "맛집 탐험가 01"

    # 5. 다른 사용자가 이미 사용 중인 닉네임 저장 시 409 NICKNAME_DUPLICATE (34번 스토리)
    dup = client.patch("/api/users/me/nickname", json={"nickname": "  맛집 탐험가 01 "}, headers=h2)
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "NICKNAME_DUPLICATE"

    # 대소문자 구분 UTF-8 binary 키 확인 (영문 대소문자가 다르면 별개 키로 허용)
    case1 = client.patch("/api/users/me/nickname", json={"nickname": "FoodLover 1"}, headers=h1)
    assert case1.status_code == 200
    case2 = client.patch("/api/users/me/nickname", json={"nickname": "foodlover 1"}, headers=h2)
    assert case2.status_code == 200

    # DB 직접 편집 플래그 및 출처 보존 확인
    with profile_env["SessionLocal"]() as db:
        u1 = db.get(User, profile_env["u1_id"])
        assert u1.nickname == "FoodLover 1"
        assert u1.nickname_key == b"FoodLover 1"
        assert u1.nickname_edited is True
        assert u1.nickname_source_edited is True
        assert u1.nickname_source_uuid == u1.uuid
        assert u1.nickname_source_id == u1.id
        assert u1.nickname_source_created_at is not None


def test_t04_nickname_reflected_in_room_participants(profile_env):
    client: TestClient = profile_env["client"]
    h1 = {"Authorization": f"Bearer {profile_env['t1']}"}

    client.patch("/api/users/me/nickname", json={"nickname": "방장닉네임99"}, headers=h1)
    room_res = client.post("/api/rooms", json={"max_members": 4}, headers=h1)
    assert room_res.status_code == 201
    room_id = room_res.json()["room_id"]

    detail = client.get(f"/api/rooms/{room_id}", headers=h1)
    assert detail.status_code == 200
    members = detail.json()["members"]
    assert len(members) == 1
    assert members[0]["nickname"] == "방장닉네임99"


def test_t05_profile_image_upload_resize_exif_strip_replace_delete_and_serve(profile_env):
    client: TestClient = profile_env["client"]
    upload_dir: Path = profile_env["upload_dir"]
    h1 = {"Authorization": f"Bearer {profile_env['t1']}"}

    # 1. 큰 이미지(1200x900 JPEG, EXIF 포함) 업로드 → 긴 변 512px(512x384)로 리사이즈 및 EXIF 제거 확인
    img = Image.new("RGB", (1200, 900), color=(200, 100, 50))
    exif = img.getexif()
    exif[0x010E] = "Secret camera description"
    raw_jpeg = BytesIO()
    img.save(raw_jpeg, format="JPEG", exif=exif.tobytes())

    up1 = client.put(
        "/api/users/me/profile-image",
        files={"file": ("photo.jpg", raw_jpeg.getvalue(), "image/jpeg")},
        headers=h1,
    )
    assert up1.status_code == 200
    url1 = up1.json()["profile_image"]
    assert url1.startswith("/uploads/profile-images/")
    assert url1.endswith(".jpg")
    fname1 = url1.rsplit("/", 1)[-1]
    assert (upload_dir / fname1).is_file()

    # R5A: 인증 없이 이미지 URL 직접 조회 가능 및 리사이즈/EXIF 제거 확인
    serve1 = client.get(url1)
    assert serve1.status_code == 200
    assert serve1.headers["content-type"] == "image/jpeg"
    with Image.open(BytesIO(serve1.content)) as saved_img:
        assert saved_img.size == (512, 384)
        assert not saved_img.getexif()

    # 2. 새 이미지(WebP)로 교체 시 이전 파일 자동 삭제 확인
    webp_bytes = _make_image_bytes("WEBP", size=(300, 300))
    up2 = client.put(
        "/api/users/me/profile-image",
        files={"file": ("avatar.webp", webp_bytes, "image/webp")},
        headers=h1,
    )
    assert up2.status_code == 200
    url2 = up2.json()["profile_image"]
    assert url2 != url1
    assert url2.endswith(".webp")
    fname2 = url2.rsplit("/", 1)[-1]
    assert not (upload_dir / fname1).exists()
    assert (upload_dir / fname2).is_file()

    # 3. DELETE 시 기본 SVG 복원 및 로컬 파일 삭제
    del_res = client.delete("/api/users/me/profile-image", headers=h1)
    assert del_res.status_code == 200
    assert del_res.json()["profile_image"] == DEFAULT_PROFILE_IMAGE
    assert not (upload_dir / fname2).exists()


def test_t05_invalid_oversized_animated_and_path_traversal_preserve_previous_image(profile_env, monkeypatch: pytest.MonkeyPatch):
    client: TestClient = profile_env["client"]
    upload_dir: Path = profile_env["upload_dir"]
    h1 = {"Authorization": f"Bearer {profile_env['t1']}"}

    # 먼저 유효한 PNG 업로드
    png_bytes = _make_image_bytes("PNG", size=(128, 128))
    up = client.put(
        "/api/users/me/profile-image",
        files={"file": ("valid.png", png_bytes, "image/png")},
        headers=h1,
    )
    assert up.status_code == 200
    valid_url = up.json()["profile_image"]
    valid_fname = valid_url.rsplit("/", 1)[-1]

    # 1. 빈 파일 거절 (400 EMPTY_IMAGE_FILE)
    r_empty = client.put(
        "/api/users/me/profile-image",
        files={"file": ("empty.png", b"", "image/png")},
        headers=h1,
    )
    assert r_empty.status_code == 400
    assert r_empty.json()["detail"]["code"] == "EMPTY_IMAGE_FILE"

    # 2. 깨진/위장된 파일 거절 (400 INVALID_IMAGE_FILE)
    r_corrupt = client.put(
        "/api/users/me/profile-image",
        files={"file": ("fake.jpg", b"not-an-image-at-all", "image/jpeg")},
        headers=h1,
    )
    assert r_corrupt.status_code == 400
    assert r_corrupt.json()["detail"]["code"] == "INVALID_IMAGE_FILE"

    # 3. 허용되지 않는 포맷(BMP) 거절 (400 UNSUPPORTED_IMAGE_FORMAT)
    bmp_bytes = _make_image_bytes("BMP", size=(64, 64))
    r_bmp = client.put(
        "/api/users/me/profile-image",
        files={"file": ("test.bmp", bmp_bytes, "image/bmp")},
        headers=h1,
    )
    assert r_bmp.status_code == 400
    assert r_bmp.json()["detail"]["code"] == "UNSUPPORTED_IMAGE_FORMAT"

    # 4. 애니메이션 WebP 거절 (400 ANIMATED_IMAGE_NOT_ALLOWED)
    r_anim = client.put(
        "/api/users/me/profile-image",
        files={"file": ("anim.webp", _make_animated_webp_bytes(), "image/webp")},
        headers=h1,
    )
    assert r_anim.status_code == 400
    assert r_anim.json()["detail"]["code"] == "ANIMATED_IMAGE_NOT_ALLOWED"

    # 5. 픽셀 상한(1,200만 픽셀) 초과 거절 (400 IMAGE_DIMENSIONS_EXCEEDED)
    monkeypatch.setattr(settings, "PROFILE_IMAGE_MAX_PIXELS", 10_000)
    big_dim_bytes = _make_image_bytes("PNG", size=(200, 100))  # 20,000 px > 10,000 px
    r_dim = client.put(
        "/api/users/me/profile-image",
        files={"file": ("big.png", big_dim_bytes, "image/png")},
        headers=h1,
    )
    assert r_dim.status_code == 400
    assert r_dim.json()["detail"]["code"] == "IMAGE_DIMENSIONS_EXCEEDED"
    monkeypatch.setattr(settings, "PROFILE_IMAGE_MAX_PIXELS", 12_000_000)

    # 6. 5MB 바이트 경계: 정확히 상한과 같으면 통과(유효 이미지일 때), 1바이트 초과 시 413 IMAGE_TOO_LARGE
    exact_bytes = _make_image_bytes("PNG", size=(64, 64))
    monkeypatch.setattr(settings, "PROFILE_IMAGE_MAX_BYTES", len(exact_bytes))
    r_exact = client.put(
        "/api/users/me/profile-image",
        files={"file": ("exact.png", exact_bytes, "image/png")},
        headers=h1,
    )
    assert r_exact.status_code == 200
    valid_url = r_exact.json()["profile_image"]
    valid_fname = valid_url.rsplit("/", 1)[-1]

    r_over = client.put(
        "/api/users/me/profile-image",
        files={"file": ("over.png", exact_bytes + b"\x00", "image/png")},
        headers=h1,
    )
    assert r_over.status_code == 413
    assert r_over.json()["detail"]["code"] == "IMAGE_TOO_LARGE"
    monkeypatch.setattr(settings, "PROFILE_IMAGE_MAX_BYTES", 5 * 1024 * 1024)

    # 이전 사진 및 프로필 참조가 그대로 유지되었는지 확인
    me = client.get("/api/users/me", headers=h1)
    assert me.status_code == 200
    assert me.json()["profile_image"] == valid_url
    assert (upload_dir / valid_fname).is_file()

    # 7. 경로 조작 조회 차단
    assert client.get("/uploads/profile-images/..%2F..%2Fmain.py").status_code == 404
    assert client.get("/uploads/profile-images/not_hex_name.png").status_code == 404


def test_t05_db_failure_cleans_new_file_and_shared_reference_is_protected(profile_env, monkeypatch: pytest.MonkeyPatch):
    client: TestClient = profile_env["client"]
    upload_dir: Path = profile_env["upload_dir"]
    h1 = {"Authorization": f"Bearer {profile_env['t1']}"}
    h2 = {"Authorization": f"Bearer {profile_env['t2']}"}

    # u1이 사진 업로드
    up1 = client.put(
        "/api/users/me/profile-image",
        files={"file": ("u1.png", _make_image_bytes("PNG", size=(100, 100)), "image/png")},
        headers=h1,
    )
    assert up1.status_code == 200
    shared_url = up1.json()["profile_image"]
    shared_fname = shared_url.rsplit("/", 1)[-1]

    # u2가 동일한 파일 URL을 참조 중인 상태(예: 이관 중 또는 공유 참조) 시뮬레이션
    with profile_env["SessionLocal"]() as db:
        u2 = db.get(User, profile_env["u2_id"])
        u2.profile_image = shared_url
        db.commit()

    # u1이 기본 이미지로 복원하더라도 u2가 아직 참조 중이므로 파일이 삭제되지 않아야 함!
    del1 = client.delete("/api/users/me/profile-image", headers=h1)
    assert del1.status_code == 200
    assert (upload_dir / shared_fname).is_file()

    # u2도 기본 이미지로 복원하면 이제 참조가 0개이므로 파일이 삭제됨
    del2 = client.delete("/api/users/me/profile-image", headers=h2)
    assert del2.status_code == 200
    assert not (upload_dir / shared_fname).exists()

    # DB 잠금/검증 단계에서 예외 발생 시 새로 생성한 파일이 고아로 남지 않고 즉시 정리되는지 확인
    def fail_lock(*args, **kwargs):
        raise RuntimeError("Simulated DB failure after file move")

    monkeypatch.setattr("app.users.profile_image.lock_user_and_verify_session", fail_lock)
    with pytest.raises(RuntimeError, match="Simulated DB failure"):
        client.put(
            "/api/users/me/profile-image",
            files={"file": ("fail.png", _make_image_bytes("PNG", size=(80, 80)), "image/png")},
            headers=h1,
        )
    assert list(upload_dir.iterdir()) == []


def test_t05_orphan_profile_image_cleanup_respects_grace_period(profile_env):
    upload_dir: Path = profile_env["upload_dir"]
    old_orphan = upload_dir / ("a" * 32 + ".png")
    recent_orphan = upload_dir / ("b" * 32 + ".png")
    old_tmp = upload_dir / (".tmp_" + "c" * 32 + ".png")

    old_orphan.write_bytes(b"old")
    recent_orphan.write_bytes(b"recent")
    old_tmp.write_bytes(b"tmp")

    now = time.time()
    old_time = now - (settings.PROFILE_IMAGE_ORPHAN_GRACE_SECONDS + 60)
    os.utime(old_orphan, (old_time, old_time))
    os.utime(old_tmp, (old_time, old_time))

    with profile_env["SessionLocal"]() as db:
        removed = cleanup_orphan_profile_images(db, now_epoch=now)
        assert removed == 2

    assert not old_orphan.exists()
    assert not old_tmp.exists()
    assert recent_orphan.exists()

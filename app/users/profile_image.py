from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.errors import AuthError
from app.auth.operations import lock_user_and_verify_session
from app.auth.sessions import DEFAULT_PROFILE_IMAGE, as_utc_naive, utc_now
from app.core.config import settings
from app.core.database import get_db
from app.users.models import User
from app.users.schemas import UserResponse

SAFE_PROFILE_IMAGE_FILENAME_RE = re.compile(r"^[a-f0-9]{32}\.(jpg|png|webp)$")
FORMAT_TO_EXT_AND_MIME: dict[str, tuple[str, str, str]] = {
    "JPEG": ("jpg", "JPEG", "image/jpeg"),
    "PNG": ("png", "PNG", "image/png"),
    "WEBP": ("webp", "WEBP", "image/webp"),
}

# 1GB 운영 환경 보호를 위해 동시 이미지 디코딩·정규화를 1개로 제한
_IMAGE_DECODE_SEMAPHORE = threading.BoundedSemaphore(1)

uploads_router = APIRouter(tags=["ProfileImages"])


def get_profile_image_upload_dir() -> Path:
    upload_dir = Path(settings.PROFILE_IMAGE_UPLOAD_DIR).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _read_upload_stream_bounded(upload_file: UploadFile, max_bytes: int) -> bytes:
    chunk_size = 64 * 1024
    chunks: list[bytes] = []
    total = 0
    file_obj = upload_file.file
    file_obj.seek(0)
    while True:
        chunk = file_obj.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise AuthError(
                "IMAGE_TOO_LARGE",
                f"프로필 사진은 {max_bytes // (1024 * 1024)}MB 이하만 업로드할 수 있습니다.",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        chunks.append(chunk)
    if total == 0:
        raise AuthError(
            "EMPTY_IMAGE_FILE",
            "빈 파일은 프로필 사진으로 업로드할 수 없습니다.",
            status.HTTP_400_BAD_REQUEST,
        )
    return b"".join(chunks)


def decode_and_normalize_profile_image(raw_bytes: bytes) -> tuple[bytes, str]:
    """
    JPG/PNG/WebP 실제 바이트를 검증하고, 애니메이션 및 1,200만 픽셀 초과를 거절한 뒤,
    EXIF 회전 반영·메타데이터 제거·긴 변 512px 이하 정지 이미지로 정규화한다.
    """
    max_pixels = int(settings.PROFILE_IMAGE_MAX_PIXELS)
    max_side = int(settings.PROFILE_IMAGE_MAX_SIDE)

    with _IMAGE_DECODE_SEMAPHORE:
        previous_max_pixels = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            try:
                with Image.open(BytesIO(raw_bytes)) as img:
                    fmt = (img.format or "").upper()
                    if fmt not in FORMAT_TO_EXT_AND_MIME:
                        raise AuthError(
                            "UNSUPPORTED_IMAGE_FORMAT",
                            "JPG, PNG, WebP 형식의 이미지만 업로드할 수 있습니다.",
                            status.HTTP_400_BAD_REQUEST,
                        )
                    width, height = img.size
                    if width <= 0 or height <= 0 or (width * height) > max_pixels:
                        raise AuthError(
                            "IMAGE_DIMENSIONS_EXCEEDED",
                            f"이미지 해상도가 허용 상한({max_pixels:,} 픽셀)을 초과했습니다.",
                            status.HTTP_400_BAD_REQUEST,
                        )
                    is_animated = bool(getattr(img, "is_animated", False)) or int(getattr(img, "n_frames", 1)) > 1
                    if is_animated:
                        raise AuthError(
                            "ANIMATED_IMAGE_NOT_ALLOWED",
                            "움직이는 이미지는 프로필 사진으로 사용할 수 없습니다.",
                            status.HTTP_400_BAD_REQUEST,
                        )
                    img.load()
                    transposed = ImageOps.exif_transpose(img)
                    ext, pil_format, _ = FORMAT_TO_EXT_AND_MIME[fmt]

                    if pil_format == "JPEG":
                        if transposed.mode in ("RGBA", "LA") or (
                            transposed.mode == "P" and "transparency" in transposed.info
                        ):
                            rgba = transposed.convert("RGBA")
                            background = Image.new("RGB", rgba.size, (255, 255, 255))
                            background.paste(rgba, mask=rgba.split()[3])
                            clean_source = background
                        else:
                            clean_source = transposed.convert("RGB")
                    else:
                        if transposed.mode in ("RGBA", "LA") or (
                            transposed.mode == "P" and "transparency" in transposed.info
                        ):
                            clean_source = transposed.convert("RGBA")
                        else:
                            clean_source = transposed.convert("RGB")

                    if max(clean_source.size) > max_side:
                        clean_source.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

                    # EXIF/ICC/주석 등 모든 메타데이터를 제거한 새 이미지 픽셀 버퍼 생성 (C 레벨 복사)
                    stripped = Image.new(clean_source.mode, clean_source.size)
                    stripped.paste(clean_source)

                    out_buf = BytesIO()
                    save_kwargs: dict[str, object] = {}
                    if pil_format == "JPEG":
                        save_kwargs = {"quality": 88, "optimize": True}
                    elif pil_format == "WEBP":
                        save_kwargs = {"quality": 88}
                    elif pil_format == "PNG":
                        save_kwargs = {"optimize": True}
                    stripped.save(out_buf, format=pil_format, **save_kwargs)
                    return out_buf.getvalue(), ext
            except AuthError:
                raise
            except Image.DecompressionBombError as exc:
                raise AuthError(
                    "IMAGE_DIMENSIONS_EXCEEDED",
                    f"이미지 해상도가 허용 상한({max_pixels:,} 픽셀)을 초과했습니다.",
                    status.HTTP_400_BAD_REQUEST,
                ) from exc
            except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
                raise AuthError(
                    "INVALID_IMAGE_FILE",
                    "손상되었거나 읽을 수 없는 이미지 파일입니다.",
                    status.HTTP_400_BAD_REQUEST,
                ) from exc
        finally:
            Image.MAX_IMAGE_PIXELS = previous_max_pixels


def extract_managed_profile_filename(profile_image_url: str | None) -> str | None:
    if not profile_image_url:
        return None
    prefix = settings.PROFILE_IMAGE_URL_PREFIX.rstrip("/") + "/"
    if not profile_image_url.startswith(prefix):
        return None
    filename = profile_image_url[len(prefix):]
    if not SAFE_PROFILE_IMAGE_FILENAME_RE.fullmatch(filename):
        return None
    return filename


def delete_profile_image_if_unreferenced(db: Session, profile_image_url: str | None) -> bool:
    """
    로컬 관리 대상 프로필 이미지 URL이 더 이상 어떤 User 행에서도 참조되지 않을 때만 파일을 삭제한다.
    기본 SVG나 외부 URL, 경로 조작 문자열은 절대 삭제하지 않는다.
    """
    filename = extract_managed_profile_filename(profile_image_url)
    if filename is None:
        return False
    ref_count = db.query(User).filter(User.profile_image == profile_image_url).count()
    if ref_count > 0:
        return False
    upload_dir = get_profile_image_upload_dir()
    file_path = (upload_dir / filename).resolve()
    if file_path.parent != upload_dir:
        return False
    try:
        if file_path.is_file():
            file_path.unlink()
            return True
    except OSError:
        return False
    return False


def replace_my_profile_image(
    db: Session,
    current_user: User,
    upload_file: UploadFile,
) -> UserResponse:
    """
    DB 잠금 밖에서 스트리밍 크기 제한·실제 이미지 디코딩·정규화·임시 파일 저장·원자 이동을 수행한 뒤,
    User 잠금과 세션 재검증 아래 프로필 참조를 교체한다. DB 실패 시 새 파일을 즉시 삭제하고 기존 프로필을 보존한다.
    """
    raw_bytes = _read_upload_stream_bounded(upload_file, int(settings.PROFILE_IMAGE_MAX_BYTES))
    normalized_bytes, ext = decode_and_normalize_profile_image(raw_bytes)

    upload_dir = get_profile_image_upload_dir()
    file_token = uuid4().hex
    tmp_path = (upload_dir / f".tmp_{file_token}.{ext}").resolve()
    final_filename = f"{file_token}.{ext}"
    final_path = (upload_dir / final_filename).resolve()
    if tmp_path.parent != upload_dir or final_path.parent != upload_dir:
        raise AuthError("INVALID_IMAGE_PATH", "허용되지 않는 파일 경로입니다.", status.HTTP_400_BAD_REQUEST)

    try:
        with open(tmp_path, "wb") as f:
            f.write(normalized_bytes)
            f.flush()
        os.replace(tmp_path, final_path)
    except OSError as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="프로필 사진을 저장할 수 없습니다.",
        ) from exc

    new_url = f"{settings.PROFILE_IMAGE_URL_PREFIX.rstrip('/')}/{final_filename}"
    old_url: str | None = None
    try:
        locked_user = lock_user_and_verify_session(db, current_user)
        old_url = locked_user.profile_image
        locked_user.profile_image = new_url
        locked_user.profile_image_edited = True
        locked_user.profile_image_source_edited = True
        if locked_user.kakao_id is None:
            locked_user.profile_image_source_uuid = locked_user.uuid
            locked_user.profile_image_source_id = locked_user.id
            locked_user.profile_image_source_created_at = (
                as_utc_naive(locked_user.created_at) if locked_user.created_at else utc_now()
            )
        db.flush()
        resp = UserResponse.model_validate(locked_user)
        db.commit()
    except Exception:
        db.rollback()
        try:
            if final_path.is_file():
                final_path.unlink()
        except OSError:
            pass
        raise

    if old_url and old_url != new_url:
        delete_profile_image_if_unreferenced(db, old_url)

    return resp


def reset_my_profile_image(
    db: Session,
    current_user: User,
) -> UserResponse:
    """본인의 프로필 사진을 기본 이미지(/static/default-profile.svg)로 복원하고 미참조 이전 로컬 파일을 정리한다."""
    old_url: str | None = None
    try:
        locked_user = lock_user_and_verify_session(db, current_user)
        old_url = locked_user.profile_image
        locked_user.profile_image = DEFAULT_PROFILE_IMAGE
        locked_user.profile_image_edited = True
        locked_user.profile_image_source_edited = True
        if locked_user.kakao_id is None:
            locked_user.profile_image_source_uuid = locked_user.uuid
            locked_user.profile_image_source_id = locked_user.id
            locked_user.profile_image_source_created_at = (
                as_utc_naive(locked_user.created_at) if locked_user.created_at else utc_now()
            )
        db.flush()
        resp = UserResponse.model_validate(locked_user)
        db.commit()
    except (SQLAlchemyError, HTTPException):
        db.rollback()
        raise

    if old_url and old_url != DEFAULT_PROFILE_IMAGE:
        delete_profile_image_if_unreferenced(db, old_url)

    return resp


def cleanup_orphan_profile_images(db: Session, now_epoch: float | None = None) -> int:
    """
    유예 시간(PROFILE_IMAGE_ORPHAN_GRACE_SECONDS)이 지난 미참조 로컬 프로필 이미지 및 임시 파일을 정리한다.
    """
    upload_dir = get_profile_image_upload_dir()
    current_epoch = time.time() if now_epoch is None else float(now_epoch)
    grace = float(settings.PROFILE_IMAGE_ORPHAN_GRACE_SECONDS)
    referenced_urls = {
        row[0]
        for row in db.query(User.profile_image).filter(User.profile_image.isnot(None)).all()
        if row[0]
    }
    prefix = settings.PROFILE_IMAGE_URL_PREFIX.rstrip("/")
    removed = 0

    for entry in upload_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if (current_epoch - mtime) < grace:
            continue
        name = entry.name
        if name.startswith(".tmp_"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
            continue
        if not SAFE_PROFILE_IMAGE_FILENAME_RE.fullmatch(name):
            continue
        url = f"{prefix}/{name}"
        if url not in referenced_urls:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


@uploads_router.get("/uploads/profile-images/{filename}")
def serve_profile_image_endpoint(filename: str):
    """R5A: 임의 파일명 기반 프로필 이미지를 인증 없이 조회한다. 경로 조작 문자열은 거절한다."""
    if not SAFE_PROFILE_IMAGE_FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="프로필 이미지를 찾을 수 없습니다.")
    upload_dir = get_profile_image_upload_dir()
    target = (upload_dir / filename).resolve()
    if target.parent != upload_dir or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="프로필 이미지를 찾을 수 없습니다.")
    ext = filename.rsplit(".", 1)[-1].lower()
    media_type = {
        "jpg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return FileResponse(path=target, media_type=media_type)

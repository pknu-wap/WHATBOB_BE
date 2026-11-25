from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass
from enum import Enum
import logging
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
import zlib

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.menus import food_kinds
from app.menus.food_kinds import MENU_ILLUSTRATIONS_URL_PREFIX
from app.menus.models import FoodKind, Menu, MenuIllustrationException

logger = logging.getLogger(__name__)

MENU_ILLUSTRATIONS_SUBDIR = "menu-illustrations"
MENU_ILLUSTRATION_MAPPINGS_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "menu_illustration_mappings.csv"
)
DEFAULT_NON_TOMATO_PASTA_EXCEPTION_REASON = (
    "대표 파스타(토마토 스파게티) 일러스트와 소스·외형이 크게 다른 메뉴"
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_SIGNATURE_LENGTH = len(PNG_SIGNATURE)
PNG_CHUNK_LENGTH_FIELD_BYTES = 4
PNG_CHUNK_TYPE_FIELD_BYTES = 4
PNG_CHUNK_HEADER_BYTES = PNG_CHUNK_LENGTH_FIELD_BYTES + PNG_CHUNK_TYPE_FIELD_BYTES
PNG_CHUNK_CRC_BYTES = 4
PNG_CHUNK_MIN_BYTES = PNG_CHUNK_HEADER_BYTES + PNG_CHUNK_CRC_BYTES
PNG_CRC_MASK = 0xFFFFFFFF
PNG_IEND_CHUNK_TYPE = b"IEND"
PNG_FORMAT_NAME = "PNG"
PNG_FILE_EXTENSION = ".png"
MIN_PNG_DIMENSION = 1
REPRESENTATIVE_ILLUSTRATION_WIDTH = 150
REPRESENTATIVE_ILLUSTRATION_HEIGHT = 135
MIN_PRINTABLE_ASCII = 32
HTTP_VERIFY_TIMEOUT_SECONDS = 5.0
ALLOWED_VERIFY_HTTP_SCHEMES = frozenset({"http", "https"})

_FORBIDDEN_FILENAME_CHARS = frozenset('/\\:*?"<>|\x00')


class FoodKindNotFoundException(ValueError):
    """요청한 음식 종류 키(kind_key)를 food_kinds에서 찾을 수 없을 때 발생하는 도메인 예외."""


class InvalidMenuIllustrationException(ValueError):
    """메뉴 일러스트 경로·파일명·PNG 형식이 유효하지 않을 때 발생하는 도메인 예외."""


class MenuIllustrationVerificationError(RuntimeError):
    """배포된 정적 일러스트 URL의 HTTP 200 응답 검증에 실패했을 때 발생하는 도메인 예외."""


class IllustrationAuditStatus(str, Enum):
    CONFIRMED_REUSABLE = "confirmed_reusable"
    PENDING_VERIFICATION = "pending_verification"
    MISSING_ORIGINAL = "missing_original"


@dataclass(frozen=True)
class MenuIllustrationAssetCheck:
    path: Path
    format: str
    width: int
    height: int
    mode: str
    has_alpha: bool
    size_bytes: int


@dataclass(frozen=True)
class FoodKindIllustrationSpec:
    kind_key: str
    name: str
    expected_image_url: str
    status: IllustrationAuditStatus
    figma_source_name: str | None
    usable: bool
    notes: str


@dataclass(frozen=True)
class FoodKindIllustrationStatus:
    kind_key: str
    food_kind_id: int | None
    name: str
    audit_status: IllustrationAuditStatus
    expected_image_url: str
    registered_image_url: str | None
    file_exists_on_disk: bool
    effective_image_url: str
    using_common_fallback: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "kind_key": self.kind_key,
            "food_kind_id": self.food_kind_id,
            "name": self.name,
            "audit_status": self.audit_status.value,
            "expected_image_url": self.expected_image_url,
            "registered_image_url": self.registered_image_url,
            "file_exists_on_disk": self.file_exists_on_disk,
            "effective_image_url": self.effective_image_url,
            "using_common_fallback": self.using_common_fallback,
        }


@dataclass(frozen=True)
class IllustrationDeploymentResult:
    kind_key: str
    food_kind_id: int
    name: str
    image_url: str
    deployed_path: str
    previous_image_url: str | None
    width: int
    height: int
    mode: str
    has_alpha: bool
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "kind_key": self.kind_key,
            "food_kind_id": self.food_kind_id,
            "name": self.name,
            "image_url": self.image_url,
            "deployed_path": self.deployed_path,
            "previous_image_url": self.previous_image_url,
            "width": self.width,
            "height": self.height,
            "mode": self.mode,
            "has_alpha": self.has_alpha,
            "size_bytes": self.size_bytes,
        }


# Figma 원본('음식일러스트' 페이지) 대조 결과 및 정적 파일 경로 매핑 (이슈 #54 / #67 / #68)
FOOD_KIND_ILLUSTRATION_SPECS: tuple[FoodKindIllustrationSpec, ...] = (
    FoodKindIllustrationSpec(
        kind_key="tteokbokki",
        name="떡볶이",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}tteokbokki.png",
        status=IllustrationAuditStatus.PENDING_VERIFICATION,
        figma_source_name="Firefly (2) 1 (왼쪽 위 후보)",
        usable=False,
        notes="Firefly (2) 1 왼쪽 위에서 떡볶이 후보를 확인했으나 개별 이름이 없어 확인 대기 상태다.",
    ),
    FoodKindIllustrationSpec(
        kind_key="tonkatsu",
        name="돈까스",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}tonkatsu.png",
        status=IllustrationAuditStatus.CONFIRMED_REUSABLE,
        figma_source_name="돈까스",
        usable=True,
        notes="Figma 원본 '돈까스' 그림 재사용.",
    ),
    FoodKindIllustrationSpec(
        kind_key="burger",
        name="햄버거",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}burger.png",
        status=IllustrationAuditStatus.CONFIRMED_REUSABLE,
        figma_source_name="햄버거",
        usable=True,
        notes="Figma 원본 '햄버거' 그림 재사용.",
    ),
    FoodKindIllustrationSpec(
        kind_key="malatang",
        name="마라탕",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}malatang.png",
        status=IllustrationAuditStatus.CONFIRMED_REUSABLE,
        figma_source_name="마라탕",
        usable=True,
        notes="Figma 원본 '마라탕' 그림 재사용 (우선 대조 확인).",
    ),
    FoodKindIllustrationSpec(
        kind_key="pasta",
        name="파스타",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}pasta.png",
        status=IllustrationAuditStatus.CONFIRMED_REUSABLE,
        figma_source_name="스파게티",
        usable=True,
        notes="Figma 원본 '스파게티' 그림 재사용. 크림·로제·오일·뇨끼 메뉴는 공통 이미지 예외로 처리한다.",
    ),
    FoodKindIllustrationSpec(
        kind_key="gukbap",
        name="국밥",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}gukbap.png",
        status=IllustrationAuditStatus.MISSING_ORIGINAL,
        figma_source_name=None,
        usable=False,
        notes="Figma 원본에서 국밥으로 확실히 식별되는 그림이 없어 #68 제작·등록 대상으로 전달한다.",
    ),
    FoodKindIllustrationSpec(
        kind_key="jjajangmyeon",
        name="짜장면",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}jjajangmyeon.png",
        status=IllustrationAuditStatus.MISSING_ORIGINAL,
        figma_source_name=None,
        usable=False,
        notes="Figma 원본에서 짜장면·자장면으로 확실히 식별되는 그림이 없어 #68 제작·등록 대상으로 전달한다.",
    ),
    FoodKindIllustrationSpec(
        kind_key="jeyuk_bokkeum",
        name="제육볶음",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}jeyuk_bokkeum.png",
        status=IllustrationAuditStatus.CONFIRMED_REUSABLE,
        figma_source_name="제육볶음",
        usable=True,
        notes="Figma 원본 '제육볶음' 그림 재사용 (우선 대조 확인).",
    ),
)

FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY: dict[str, FoodKindIllustrationSpec] = {
    spec.kind_key: spec for spec in FOOD_KIND_ILLUSTRATION_SPECS
}

REUSABLE_ILLUSTRATION_KIND_KEYS: tuple[str, ...] = tuple(
    spec.kind_key
    for spec in FOOD_KIND_ILLUSTRATION_SPECS
    if spec.status is IllustrationAuditStatus.CONFIRMED_REUSABLE
)

PENDING_VERIFICATION_ILLUSTRATION_KIND_KEYS: tuple[str, ...] = tuple(
    spec.kind_key
    for spec in FOOD_KIND_ILLUSTRATION_SPECS
    if spec.status is IllustrationAuditStatus.PENDING_VERIFICATION
)

MISSING_ILLUSTRATION_KIND_KEYS_FOR_ISSUE68: tuple[str, ...] = tuple(
    spec.kind_key
    for spec in FOOD_KIND_ILLUSTRATION_SPECS
    if spec.status is IllustrationAuditStatus.MISSING_ORIGINAL
)

# 대표 파스타(토마토 스파게티) 일러스트와 크게 다른 크림·까르보나라·투움바·로제·오일·뇨끼 메뉴 food_code 목록
DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES: tuple[str, ...] = (
    # 크림 / 까르보나라 / 투움바 (18)
    "D203-161000000-0002",  # 스파게티_베이컨 크림 파스타
    "D203-161000000-0005",  # 스파게티_까르보나라 파스타
    "D203-161000000-0006",  # 스파게티_까르보나라스파게티
    "D203-161000000-0009",  # 스파게티_명란크림스파게티
    "D203-161000000-0014",  # 스파게티_옥수수바질치즈크림뇨끼
    "D203-161000000-0017",  # 스파게티_치즈오븐베이컨에멘탈크림
    "D203-161000000-0020",  # 스파게티_크림감베로니스파게티
    "D203-161000000-0021",  # 스파게티_크림스파게티
    "D203-161000000-0024",  # 스파게티_투움바 스파게티
    "D303-161000000-0002",  # 스파게티_간편조리세트_바질크림 빠네 파스타
    "D303-161000000-0003",  # 스파게티_간편조리세트_베이컨까르보나라
    "D303-161000000-0007",  # 스파게티_간편조리세트_쉬림프 크림파스타
    "D303-161000000-0011",  # 스파게티_간편조리세트_톡톡크림 빠네 파스타
    "D303-161000000-0012",  # 스파게티_간편조리세트_트러플 크림 파스타
    "D303-161000000-0014",  # 스파게티_간편조리세트_핫매콤크림파스타
    "D303-161080000-0001",  # 스파게티_까르보나라소스
    "D303-161480000-0001",  # 스파게티_크림소스
    "D303-161504800-0001",  # 스파게티_해물_크림소스
    # 로제 / 오일 / 뇨끼 (11)
    "D203-161000000-0003",  # 스파게티_쉬림프 로제 파스타
    "D203-161000000-0004",  # 스파게티_뇨끼
    "D203-161000000-0007",  # 스파게티_나폴리로제파스타
    "D203-161000000-0011",  # 스파게티_베이컨 로제 파스타
    "D203-161000000-0016",  # 스파게티_치즈오븐마라로제파스타
    "D203-161000000-0018",  # 스파게티_치즈오븐쉬림프로제파스타
    "D303-161000000-0001",  # 스파게티_간편조리세트_감바스알리오올리오
    "D303-161000000-0006",  # 스파게티_간편조리세트_쉬림프 로제파스타
    "D303-161000000-0008",  # 스파게티_간편조리세트_알리오올리오파스타
    "D303-161150000-0001",  # 스파게티_로제소스
    "D303-161370000-0001",  # 스파게티_오일소스
)


def _resolve_static_url_or_raise(
    image_url: str,
    *,
    static_dir: Path | None = None,
) -> tuple[str, Path]:
    try:
        return food_kinds.resolve_safe_static_file_path(image_url, static_dir=static_dir)
    except ValueError as exc:
        raise InvalidMenuIllustrationException(str(exc)) from exc


def validate_static_illustration_url(
    image_url: str,
    *,
    static_dir: Path | None = None,
    verify_file_exists: bool = True,
) -> str:
    """상대 정적 일러스트 URL 형식과(선택적으로) 실제 배포 파일 존재 여부를 검증한다."""
    cleaned, candidate = _resolve_static_url_or_raise(image_url, static_dir=static_dir)
    if verify_file_exists and not candidate.is_file():
        raise FileNotFoundError(
            f"배포된 정적 일러스트 파일을 찾을 수 없습니다: {cleaned} ({candidate})"
        )
    return cleaned


def _verify_live_http_200(verify_base_url: str, image_url: str) -> None:
    parsed = urlparse(verify_base_url.strip())
    if parsed.scheme not in ALLOWED_VERIFY_HTTP_SCHEMES or not parsed.netloc:
        raise MenuIllustrationVerificationError(
            f"HTTP 검증 기준 URL은 http(s) 스킴과 호스트를 포함해야 합니다: {verify_base_url!r}"
        )

    target_url = f"{verify_base_url.strip().rstrip('/')}{image_url}"
    request = Request(target_url, method="GET")
    try:
        with urlopen(request, timeout=HTTP_VERIFY_TIMEOUT_SECONDS) as response:
            status_code = getattr(response, "status", response.getcode())
            body = response.read()
    except HTTPError as exc:
        raise MenuIllustrationVerificationError(
            f"정적 일러스트 HTTP 검증에 실패했습니다 (status={exc.code}, url={target_url})"
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise MenuIllustrationVerificationError(
            f"정적 일러스트 HTTP 검증 요청에 실패했습니다 (url={target_url}): {exc}"
        ) from exc

    if status_code != 200 or not body:
        raise MenuIllustrationVerificationError(
            f"정적 일러스트가 HTTP 200 비어 있지 않은 본문으로 응답하지 않았습니다 "
            f"(status={status_code}, bytes={len(body)}, url={target_url})"
        )


def _verify_static_files_route_http_200(static_root: Path, image_url: str) -> None:
    probe_app = FastAPI()
    probe_app.mount("/static", StaticFiles(directory=static_root), name="static")
    with TestClient(probe_app) as client:
        response = client.get(image_url)

    if response.status_code != 200 or not response.content:
        raise MenuIllustrationVerificationError(
            f"정적 파일 라우트 검증에서 HTTP 200 비어 있지 않은 응답을 받지 못했습니다 "
            f"(status={response.status_code}, bytes={len(response.content)}, url={image_url})"
        )


def verify_static_illustration_http_200(
    image_url: str,
    *,
    static_dir: Path | None = None,
    verify_base_url: str | None = None,
) -> str:
    """정적 일러스트 URL이 실제 HTTP 200과 비어 있지 않은 본문으로 응답하는지 검증한다."""
    effective_static_dir = food_kinds.STATIC_DIR if static_dir is None else Path(static_dir)
    cleaned, _candidate = _resolve_static_url_or_raise(image_url, static_dir=effective_static_dir)

    if verify_base_url is not None:
        _verify_live_http_200(verify_base_url, cleaned)
    else:
        _verify_static_files_route_http_200(effective_static_dir.resolve(), cleaned)
    return cleaned


def _verify_png_chunks(raw_bytes: bytes, resolved_path: Path) -> None:
    if len(raw_bytes) < PNG_SIGNATURE_LENGTH or raw_bytes[:PNG_SIGNATURE_LENGTH] != PNG_SIGNATURE:
        raise InvalidMenuIllustrationException(f"유효한 PNG 시그니처가 아닙니다: {resolved_path}")

    offset = PNG_SIGNATURE_LENGTH
    total = len(raw_bytes)
    found_iend = False
    while offset + PNG_CHUNK_MIN_BYTES <= total:
        length = int.from_bytes(
            raw_bytes[offset : offset + PNG_CHUNK_LENGTH_FIELD_BYTES],
            "big",
        )
        type_start = offset + PNG_CHUNK_LENGTH_FIELD_BYTES
        data_start = offset + PNG_CHUNK_HEADER_BYTES
        chunk_type = raw_bytes[type_start:data_start]
        data_end = data_start + length
        crc_end = data_end + PNG_CHUNK_CRC_BYTES
        if crc_end > total:
            raise InvalidMenuIllustrationException(
                f"손상되었거나 잘린 PNG 청크가 포함되어 있습니다: {resolved_path}"
            )

        expected_crc = int.from_bytes(raw_bytes[data_end:crc_end], "big")
        actual_crc = zlib.crc32(raw_bytes[type_start:data_end]) & PNG_CRC_MASK
        if actual_crc != expected_crc:
            raise InvalidMenuIllustrationException(
                f"손상된 PNG 체크섬이 감지되었습니다 ({chunk_type!r}): {resolved_path}"
            )

        offset = crc_end
        if chunk_type == PNG_IEND_CHUNK_TYPE:
            if length != 0:
                raise InvalidMenuIllustrationException(
                    f"손상된 PNG 종료 청크(IEND)입니다: {resolved_path}"
                )
            found_iend = True
            break

    if not found_iend:
        raise InvalidMenuIllustrationException(
            f"PNG 종료 청크(IEND)가 누락되었습니다: {resolved_path}"
        )


def _load_validated_png_metadata(resolved_path: Path) -> tuple[int, int, str, bool]:
    with Image.open(resolved_path) as image:
        width, height = image.size
        if width < MIN_PNG_DIMENSION or height < MIN_PNG_DIMENSION:
            raise InvalidMenuIllustrationException(
                f"PNG 이미지 크기는 {MIN_PNG_DIMENSION}x{MIN_PNG_DIMENSION} 이상이어야 합니다 "
                f"({width}x{height}): {resolved_path}"
            )
        mode = image.mode
        has_alpha = ("A" in image.getbands()) or ("transparency" in image.info)
        image.load()
        return int(width), int(height), mode, bool(has_alpha)


def validate_menu_illustration_png(png_path: Path) -> MenuIllustrationAssetCheck:
    """디자이너에게 수령한 PNG 일러스트 파일의 형식·무결성·해상도·투명도를 검증한다 (#68)."""
    resolved_path = Path(png_path)
    if not resolved_path.exists():
        raise FileNotFoundError(f"PNG 일러스트 파일을 찾을 수 없습니다: {resolved_path}")
    if not resolved_path.is_file():
        raise InvalidMenuIllustrationException(
            f"일반 파일이 아닌 경로는 PNG로 검증할 수 없습니다: {resolved_path}"
        )

    size_bytes = resolved_path.stat().st_size
    if size_bytes <= 0:
        raise InvalidMenuIllustrationException(
            f"빈 파일은 PNG 일러스트로 등록할 수 없습니다: {resolved_path}"
        )

    try:
        with Image.open(resolved_path) as image:
            if (image.format or "").upper() != PNG_FORMAT_NAME:
                raise InvalidMenuIllustrationException(
                    f"PNG 형식의 이미지 파일만 등록할 수 있습니다 "
                    f"(감지된 형식: {image.format or 'UNKNOWN'}): {resolved_path}"
                )
            image.verify()

        _verify_png_chunks(resolved_path.read_bytes(), resolved_path)
        width, height, mode, has_alpha = _load_validated_png_metadata(resolved_path)
    except InvalidMenuIllustrationException:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise InvalidMenuIllustrationException(
            f"손상되었거나 유효한 PNG 파일이 아닙니다: {resolved_path}"
        ) from exc

    return MenuIllustrationAssetCheck(
        path=resolved_path.resolve(),
        format=PNG_FORMAT_NAME,
        width=width,
        height=height,
        mode=mode,
        has_alpha=has_alpha,
        size_bytes=int(size_bytes),
    )


def _require_food_kind_by_key(db: Session, kind_key: str) -> FoodKind:
    cleaned_key = (kind_key or "").strip()
    if not cleaned_key:
        raise InvalidMenuIllustrationException("음식 종류 키(kind_key)는 비어 있을 수 없습니다.")
    food_kind = db.query(FoodKind).filter(FoodKind.kind_key == cleaned_key).one_or_none()
    if food_kind is None:
        raise FoodKindNotFoundException(f"음식 종류를 찾을 수 없습니다: {cleaned_key}")
    return food_kind


def register_food_kind_illustration(
    db: Session,
    *,
    kind_key: str,
    image_url: str,
    static_dir: Path | None = None,
    verify_file_exists: bool = True,
    verify_http: bool = True,
    verify_base_url: str | None = None,
    commit: bool = True,
    _food_kind: FoodKind | None = None,
) -> FoodKind:
    """음식 종류 키(kind_key)에 대표 일러스트 정적 경로를 검증·등록한다 (#67/#68 공용)."""
    food_kind = _require_food_kind_by_key(db, kind_key) if _food_kind is None else _food_kind

    validated_url = validate_static_illustration_url(
        image_url,
        static_dir=static_dir,
        verify_file_exists=verify_file_exists,
    )
    if verify_http and (verify_file_exists or verify_base_url is not None):
        verify_static_illustration_http_200(
            validated_url,
            static_dir=static_dir,
            verify_base_url=verify_base_url,
        )

    food_kind.representative_image_url = validated_url
    db.flush()
    if commit:
        db.commit()
    return food_kind


def _validate_png_filename(filename: str) -> str:
    cleaned = (filename or "").strip()
    if not cleaned:
        raise InvalidMenuIllustrationException("배포할 PNG 파일명은 비어 있을 수 없습니다.")
    if any(ch in _FORBIDDEN_FILENAME_CHARS or ord(ch) < MIN_PRINTABLE_ASCII for ch in cleaned):
        raise InvalidMenuIllustrationException(
            f"허용되지 않는 문자가 포함된 파일명입니다: {filename!r}"
        )
    if ".." in cleaned or cleaned.startswith("."):
        raise InvalidMenuIllustrationException(
            f"상위 경로 탐색 또는 숨김 파일명은 허용되지 않습니다: {filename!r}"
        )
    if not cleaned.endswith(PNG_FILE_EXTENSION) or len(cleaned) <= len(PNG_FILE_EXTENSION):
        raise InvalidMenuIllustrationException(
            f"배포 파일명은 소문자 '{PNG_FILE_EXTENSION}' 확장자를 가져야 합니다: {filename!r}"
        )
    if Path(cleaned).name != cleaned:
        raise InvalidMenuIllustrationException(
            f"파일명에는 경로 구분자를 포함할 수 없습니다: {filename!r}"
        )
    return cleaned


def _write_bytes_atomically(target_path: Path, payload: bytes) -> None:
    tmp_path = target_path.parent / f".tmp_{uuid4().hex}{PNG_FILE_EXTENSION}"
    try:
        with tmp_path.open("wb") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError as cleanup_exc:
            logger.warning(
                "임시 PNG 파일 정리에 실패했습니다 (path=%s): %s",
                tmp_path,
                cleanup_exc,
            )
        raise


def _rollback_deployed_file(
    target_path: Path,
    *,
    existed_before: bool,
    previous_bytes: bytes | None,
) -> None:
    try:
        if not existed_before:
            if target_path.exists():
                target_path.unlink()
        elif previous_bytes is not None:
            _write_bytes_atomically(target_path, previous_bytes)
    except OSError as restore_exc:
        logger.warning(
            "배포 롤백 중 정적 일러스트 파일 정리/복원에 실패했습니다 (path=%s): %s",
            target_path,
            restore_exc,
        )


def deploy_and_register_food_kind_png(
    db: Session,
    *,
    kind_key: str,
    source_png: Path,
    filename: str | None = None,
    static_dir: Path | None = None,
    require_transparency: bool = False,
    verify_http: bool = True,
    verify_base_url: str | None = None,
    commit: bool = True,
) -> IllustrationDeploymentResult:
    """디자이너 PNG를 검증해 정적 디렉터리에 원자 배포하고 HTTP 200 확인 후 DB에 등록한다 (#68)."""
    food_kind = _require_food_kind_by_key(db, kind_key)
    cleaned_key = food_kind.kind_key
    previous_url = food_kind.representative_image_url

    target_filename = f"{cleaned_key}{PNG_FILE_EXTENSION}" if filename is None else filename
    safe_filename = _validate_png_filename(target_filename)

    asset_check = validate_menu_illustration_png(Path(source_png))
    if require_transparency and not asset_check.has_alpha:
        raise InvalidMenuIllustrationException(
            f"배경 투명도(알파 채널)가 없는 PNG 파일은 등록할 수 없습니다: {source_png}"
        )

    effective_static_dir = food_kinds.STATIC_DIR if static_dir is None else Path(static_dir)
    illustrations_dir = (effective_static_dir.resolve() / MENU_ILLUSTRATIONS_SUBDIR).resolve()
    illustrations_dir.mkdir(parents=True, exist_ok=True)
    target_path = (illustrations_dir / safe_filename).resolve()
    if target_path.parent != illustrations_dir:
        raise InvalidMenuIllustrationException(f"허용되지 않는 배포 파일 경로입니다: {safe_filename!r}")

    source_resolved = asset_check.path
    image_url = f"{MENU_ILLUSTRATIONS_URL_PREFIX}{safe_filename}"
    existed_before = target_path.is_file()
    need_copy = source_resolved != target_path
    previous_bytes = target_path.read_bytes() if (existed_before and need_copy) else None

    if need_copy:
        _write_bytes_atomically(target_path, source_resolved.read_bytes())

    try:
        updated_kind = register_food_kind_illustration(
            db,
            kind_key=cleaned_key,
            image_url=image_url,
            static_dir=effective_static_dir,
            verify_file_exists=True,
            verify_http=verify_http,
            verify_base_url=verify_base_url,
            commit=commit,
            _food_kind=food_kind,
        )
    except Exception:
        if commit:
            db.rollback()
        if need_copy:
            _rollback_deployed_file(
                target_path,
                existed_before=existed_before,
                previous_bytes=previous_bytes,
            )
        raise

    return IllustrationDeploymentResult(
        kind_key=updated_kind.kind_key,
        food_kind_id=int(updated_kind.id),
        name=updated_kind.name,
        image_url=str(updated_kind.representative_image_url),
        deployed_path=str(target_path),
        previous_image_url=previous_url,
        width=asset_check.width,
        height=asset_check.height,
        mode=asset_check.mode,
        has_alpha=asset_check.has_alpha,
        size_bytes=asset_check.size_bytes,
    )


def inspect_food_kind_illustration_status(
    db: Session,
    *,
    static_dir: Path | None = None,
) -> list[FoodKindIllustrationStatus]:
    """초기 8종 음식 종류의 원본 대조 상태, DB 등록 경로, 디스크 배포 여부, 실조회 URL을 보고한다 (#68)."""
    effective_static_dir = food_kinds.STATIC_DIR if static_dir is None else Path(static_dir)
    spec_keys = [spec.kind_key for spec in FOOD_KIND_ILLUSTRATION_SPECS]
    kinds_by_key = {
        fk.kind_key: fk
        for fk in db.query(FoodKind).filter(FoodKind.kind_key.in_(spec_keys)).all()
    }

    statuses: list[FoodKindIllustrationStatus] = []
    for spec in FOOD_KIND_ILLUSTRATION_SPECS:
        food_kind = kinds_by_key.get(spec.kind_key)
        registered_url = food_kind.representative_image_url if food_kind is not None else None
        expected_url = (
            food_kinds.find_deployed_menu_illustration_url(
                spec.expected_image_url, static_dir=effective_static_dir, prefer_svg=True,
            )
            or spec.expected_image_url
        )
        target_url_to_check = (registered_url or "").strip() or expected_url
        try:
            _cleaned, candidate = food_kinds.resolve_safe_static_file_path(
                target_url_to_check,
                static_dir=effective_static_dir,
            )
            file_exists_on_disk = candidate.is_file()
        except ValueError:
            file_exists_on_disk = False

        effective_url = food_kinds.resolve_menu_illustration_url(
            registered_url,
            static_dir=effective_static_dir,
        )
        statuses.append(
            FoodKindIllustrationStatus(
                kind_key=spec.kind_key,
                food_kind_id=int(food_kind.id) if food_kind is not None else None,
                name=food_kind.name if food_kind is not None else spec.name,
                audit_status=spec.status,
                expected_image_url=expected_url,
                registered_image_url=registered_url,
                file_exists_on_disk=file_exists_on_disk,
                effective_image_url=effective_url,
                using_common_fallback=(effective_url == food_kinds.DEFAULT_MENU_ILLUSTRATION_URL),
            )
        )
    return statuses


def load_food_kind_illustration_urls() -> dict[str, str]:
    urls_by_key = {
        spec.kind_key: spec.expected_image_url for spec in FOOD_KIND_ILLUSTRATION_SPECS
    }
    with MENU_ILLUSTRATION_MAPPINGS_PATH.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            kind_key = row["kind_key"].strip()
            if not kind_key or kind_key in urls_by_key:
                raise ValueError(f"Invalid or duplicate illustration kind_key: {kind_key!r}")
            filename = _validate_png_filename(row["filename"])
            urls_by_key[kind_key] = f"{MENU_ILLUSTRATIONS_URL_PREFIX}{filename}"
    return urls_by_key


def sync_food_kind_representative_illustrations(
    db: Session,
    *,
    static_dir: Path | None = None,
    overwrite_existing: bool = False,
    commit: bool = True,
) -> dict[str, int]:
    """정적 디렉터리에 실제로 배포된 대표 일러스트 파일만 food_kinds.representative_image_url에 반영한다."""
    urls_by_key = load_food_kind_illustration_urls()
    kinds_by_key = {
        fk.kind_key: fk
        for fk in db.query(FoodKind).filter(FoodKind.kind_key.in_(urls_by_key)).all()
    }
    deployed_urls = {
        legacy_url: food_kinds.find_deployed_menu_illustration_url(
            legacy_url, static_dir=static_dir, prefer_svg=True,
        )
        for legacy_url in set(urls_by_key.values())
    }

    updated = 0
    already_registered = 0
    skipped_missing_file = 0
    skipped_missing_kind = 0

    for kind_key, legacy_url in urls_by_key.items():
        image_url = deployed_urls[legacy_url]
        if image_url is None:
            skipped_missing_file += 1
            continue

        food_kind = kinds_by_key.get(kind_key)
        if food_kind is None:
            skipped_missing_kind += 1
            continue

        current_url = (food_kind.representative_image_url or "").strip()
        if current_url == image_url or (
            current_url and current_url != legacy_url and not overwrite_existing
        ):
            already_registered += 1
            continue

        food_kind.representative_image_url = image_url
        updated += 1

    if updated > 0:
        db.flush()
        if commit:
            db.commit()

    return {
        "updated": updated,
        "already_registered": already_registered,
        "skipped_missing_file": skipped_missing_file,
        "skipped_missing_kind": skipped_missing_kind,
    }


def seed_menu_illustration_exceptions(
    db: Session,
    food_codes: Sequence[str] | Mapping[str, str | None] | None = None,
    *,
    default_reason: str | None = DEFAULT_NON_TOMATO_PASTA_EXCEPTION_REASON,
    commit: bool = True,
) -> dict[str, int]:
    """food_code 기준으로 기존 메뉴를 찾아 menu_illustration_exceptions에 멱등 적재한다."""
    code_reason_map: dict[str, str | None] = {}
    if food_codes is None:
        for code in DEFAULT_MENU_ILLUSTRATION_EXCEPTION_FOOD_CODES:
            code_reason_map[code] = default_reason
    elif isinstance(food_codes, Mapping):
        for raw_code, reason in food_codes.items():
            cleaned = (raw_code or "").strip()
            if cleaned and cleaned not in code_reason_map:
                code_reason_map[cleaned] = reason
    else:
        for raw_code in food_codes:
            cleaned = (raw_code or "").strip()
            if cleaned and cleaned not in code_reason_map:
                code_reason_map[cleaned] = default_reason

    if not code_reason_map:
        return {
            "inserted": 0,
            "already_present": 0,
            "unmatched_food_codes": 0,
            "total_exceptions": db.query(MenuIllustrationException).count(),
        }

    matched_menus = (
        db.query(Menu)
        .filter(Menu.food_code.in_(list(code_reason_map.keys())))
        .all()
    )
    matched_menu_ids = [menu.id for menu in matched_menus]
    existing_exception_ids = (
        {
            row.menu_id
            for row in db.query(MenuIllustrationException)
            .filter(MenuIllustrationException.menu_id.in_(matched_menu_ids))
            .all()
        }
        if matched_menu_ids
        else set()
    )

    inserted = 0
    already_present = 0
    for menu in matched_menus:
        if menu.id in existing_exception_ids:
            already_present += 1
            continue
        db.add(
            MenuIllustrationException(
                menu_id=menu.id,
                reason=code_reason_map[menu.food_code],
            )
        )
        inserted += 1

    if inserted > 0:
        db.flush()
        if commit:
            db.commit()

    return {
        "inserted": inserted,
        "already_present": already_present,
        "unmatched_food_codes": len(code_reason_map) - len(matched_menus),
        "total_exceptions": db.query(MenuIllustrationException).count(),
    }

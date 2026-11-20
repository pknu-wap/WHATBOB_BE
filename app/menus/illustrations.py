from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4
import zlib

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.menus import food_kinds
from app.menus.models import FoodKind, Menu, MenuIllustrationException

MENU_ILLUSTRATIONS_URL_PREFIX = "/static/menu-illustrations/"
DEFAULT_NON_TOMATO_PASTA_EXCEPTION_REASON = (
    "대표 파스타(토마토 스파게티) 일러스트와 소스·외형이 크게 다른 메뉴"
)


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
    status: Literal["reusable_figma", "missing_for_#68"]
    figma_source_name: str | None
    usable: bool
    notes: str


# Figma 원본('음식일러스트' 페이지) 대조 결과 및 정적 파일 경로 매핑 (이슈 #54 / #67 / #68)
FOOD_KIND_ILLUSTRATION_SPECS: tuple[FoodKindIllustrationSpec, ...] = (
    FoodKindIllustrationSpec(
        kind_key="tteokbokki",
        name="떡볶이",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}tteokbokki.png",
        status="reusable_figma",
        figma_source_name="Firefly (2) 1 (왼쪽 위 후보)",
        usable=True,
        notes="Firefly (2) 1 왼쪽 위에서 떡볶이 후보를 확인했다. 개별 내보내기 후 배포한다.",
    ),
    FoodKindIllustrationSpec(
        kind_key="tonkatsu",
        name="돈까스",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}tonkatsu.png",
        status="reusable_figma",
        figma_source_name="돈까스",
        usable=True,
        notes="Figma 원본 '돈까스' 그림 재사용.",
    ),
    FoodKindIllustrationSpec(
        kind_key="burger",
        name="햄버거",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}burger.png",
        status="reusable_figma",
        figma_source_name="햄버거",
        usable=True,
        notes="Figma 원본 '햄버거' 그림 재사용.",
    ),
    FoodKindIllustrationSpec(
        kind_key="malatang",
        name="마라탕",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}malatang.png",
        status="reusable_figma",
        figma_source_name="마라탕",
        usable=True,
        notes="Figma 원본 '마라탕' 그림 재사용 (우선 대조 확인).",
    ),
    FoodKindIllustrationSpec(
        kind_key="pasta",
        name="파스타",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}pasta.png",
        status="reusable_figma",
        figma_source_name="스파게티",
        usable=True,
        notes="Figma 원본 '스파게티' 그림 재사용. 크림·로제·오일·뇨끼 메뉴는 공통 이미지 예외로 처리한다.",
    ),
    FoodKindIllustrationSpec(
        kind_key="gukbap",
        name="국밥",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}gukbap.png",
        status="missing_for_#68",
        figma_source_name=None,
        usable=False,
        notes="Figma 원본에서 국밥으로 확실히 식별되는 그림이 없어 #68 제작·등록 대상으로 전달한다.",
    ),
    FoodKindIllustrationSpec(
        kind_key="jjajangmyeon",
        name="짜장면",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}jjajangmyeon.png",
        status="missing_for_#68",
        figma_source_name=None,
        usable=False,
        notes="Figma 원본에서 짜장면·자장면으로 확실히 식별되는 그림이 없어 #68 제작·등록 대상으로 전달한다.",
    ),
    FoodKindIllustrationSpec(
        kind_key="jeyuk_bokkeum",
        name="제육볶음",
        expected_image_url=f"{MENU_ILLUSTRATIONS_URL_PREFIX}jeyuk_bokkeum.png",
        status="reusable_figma",
        figma_source_name="제육볶음",
        usable=True,
        notes="Figma 원본 '제육볶음' 그림 재사용 (우선 대조 확인).",
    ),
)

FOOD_KIND_ILLUSTRATION_SPEC_BY_KEY: dict[str, FoodKindIllustrationSpec] = {
    spec.kind_key: spec for spec in FOOD_KIND_ILLUSTRATION_SPECS
}

REUSABLE_ILLUSTRATION_KIND_KEYS: tuple[str, ...] = tuple(
    spec.kind_key for spec in FOOD_KIND_ILLUSTRATION_SPECS if spec.usable
)

MISSING_ILLUSTRATION_KIND_KEYS_FOR_ISSUE68: tuple[str, ...] = tuple(
    spec.kind_key
    for spec in FOOD_KIND_ILLUSTRATION_SPECS
    if spec.status == "missing_for_#68"
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


def _resolve_static_candidate(
    image_url: str,
    *,
    static_dir: Path | None = None,
) -> tuple[str, Path]:
    cleaned = (image_url or "").strip()
    if not cleaned.startswith("/static/") or "\\" in cleaned:
        raise ValueError(
            f"일러스트 경로는 '/static/'으로 시작하는 백엔드 상대 경로여야 합니다: {image_url!r}"
        )
    rel_path = cleaned.removeprefix("/static/")
    if not rel_path or rel_path.startswith("/"):
        raise ValueError(f"유효하지 않은 정적 일러스트 경로입니다: {image_url!r}")

    effective_static_dir = food_kinds.STATIC_DIR if static_dir is None else static_dir
    static_root = effective_static_dir.resolve()
    candidate = (static_root / rel_path).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError as exc:
        raise ValueError(f"정적 파일 루트 밖의 경로는 허용되지 않습니다: {image_url!r}") from exc
    return cleaned, candidate


def validate_static_illustration_url(
    image_url: str,
    *,
    static_dir: Path | None = None,
    verify_file_exists: bool = True,
) -> str:
    """상대 정적 일러스트 URL 형식과(선택적으로) 실제 배포 파일 존재 여부를 검증한다."""
    cleaned, candidate = _resolve_static_candidate(image_url, static_dir=static_dir)
    if verify_file_exists and not candidate.is_file():
        raise FileNotFoundError(
            f"배포된 정적 일러스트 파일을 찾을 수 없습니다: {cleaned} ({candidate})"
        )
    return cleaned


def _verify_png_chunks(raw_bytes: bytes, resolved_path: Path) -> None:
    if len(raw_bytes) < 8 or raw_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"유효한 PNG 시그니처가 아닙니다: {resolved_path}")
    offset = 8
    total = len(raw_bytes)
    found_iend = False
    while offset + 12 <= total:
        length = int.from_bytes(raw_bytes[offset : offset + 4], "big")
        chunk_type = raw_bytes[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > total:
            raise ValueError(f"손상되었거나 잘린 PNG 청크가 포함되어 있습니다: {resolved_path}")
        expected_crc = int.from_bytes(raw_bytes[data_end:crc_end], "big")
        actual_crc = zlib.crc32(raw_bytes[offset + 4 : data_end]) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError(
                f"손상된 PNG 체크섬이 감지되었습니다 ({chunk_type!r}): {resolved_path}"
            )
        offset = crc_end
        if chunk_type == b"IEND":
            if length != 0:
                raise ValueError(f"손상된 PNG 종료 청크(IEND)입니다: {resolved_path}")
            found_iend = True
            break
    if not found_iend:
        raise ValueError(f"PNG 종료 청크(IEND)가 누락되었습니다: {resolved_path}")


def validate_menu_illustration_png(png_path: Path) -> MenuIllustrationAssetCheck:
    """디자이너에게 수령한 PNG 일러스트 파일의 형식·무결성·해상도·투명도를 검증한다 (#68)."""
    resolved_path = Path(png_path)
    if not resolved_path.exists():
        raise FileNotFoundError(f"PNG 일러스트 파일을 찾을 수 없습니다: {resolved_path}")
    if not resolved_path.is_file():
        raise ValueError(f"일반 파일이 아닌 경로는 PNG로 검증할 수 없습니다: {resolved_path}")

    size_bytes = resolved_path.stat().st_size
    if size_bytes <= 0:
        raise ValueError(f"빈 파일은 PNG 일러스트로 등록할 수 없습니다: {resolved_path}")

    try:
        with Image.open(resolved_path) as image:
            detected_format = (image.format or "").upper()
            if detected_format != "PNG":
                raise ValueError(
                    f"PNG 형식의 이미지 파일만 등록할 수 있습니다 (감지된 형식: {image.format or 'UNKNOWN'}): {resolved_path}"
                )
            image.verify()

        _verify_png_chunks(resolved_path.read_bytes(), resolved_path)

        with Image.open(resolved_path) as image:
            detected_format = (image.format or "").upper()
            if detected_format != "PNG":
                raise ValueError(
                    f"PNG 형식의 이미지 파일만 등록할 수 있습니다 (감지된 형식: {image.format or 'UNKNOWN'}): {resolved_path}"
                )
            width, height = image.size
            if width < 1 or height < 1:
                raise ValueError(
                    f"PNG 이미지 크기는 1x1 이상이어야 합니다 ({width}x{height}): {resolved_path}"
                )
            mode = image.mode
            has_alpha = ("A" in image.getbands()) or ("transparency" in image.info)
            image.load()
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError(
            f"손상되었거나 유효한 PNG 파일이 아닙니다: {resolved_path}"
        ) from exc

    return MenuIllustrationAssetCheck(
        path=resolved_path.resolve(),
        format="PNG",
        width=int(width),
        height=int(height),
        mode=mode,
        has_alpha=bool(has_alpha),
        size_bytes=int(size_bytes),
    )


def register_food_kind_illustration(
    db: Session,
    *,
    kind_key: str,
    image_url: str,
    static_dir: Path | None = None,
    verify_file_exists: bool = True,
    commit: bool = True,
) -> FoodKind:
    """음식 종류 키(kind_key)에 대표 일러스트 정적 경로를 검증·등록한다 (#67/#68 공용)."""
    cleaned_key = (kind_key or "").strip()
    if not cleaned_key:
        raise ValueError("음식 종류 키(kind_key)는 비어 있을 수 없습니다.")

    validated_url = validate_static_illustration_url(
        image_url,
        static_dir=static_dir,
        verify_file_exists=verify_file_exists,
    )

    food_kind = db.query(FoodKind).filter(FoodKind.kind_key == cleaned_key).one_or_none()
    if food_kind is None:
        raise ValueError(f"음식 종류를 찾을 수 없습니다: {cleaned_key}")

    food_kind.representative_image_url = validated_url
    db.flush()
    if commit:
        db.commit()
    return food_kind


_FORBIDDEN_FILENAME_CHARS = set('/\\:*?"<>|\x00')


def _validate_png_filename(filename: str) -> str:
    cleaned = (filename or "").strip()
    if not cleaned:
        raise ValueError("배포할 PNG 파일명은 비어 있을 수 없습니다.")
    if any(ch in _FORBIDDEN_FILENAME_CHARS or ord(ch) < 32 for ch in cleaned):
        raise ValueError(f"허용되지 않는 문자가 포함된 파일명입니다: {filename!r}")
    if ".." in cleaned or cleaned.startswith("."):
        raise ValueError(f"상위 경로 탐색 또는 숨김 파일명은 허용되지 않습니다: {filename!r}")
    if not cleaned.endswith(".png") or len(cleaned) <= len(".png"):
        raise ValueError(f"배포 파일명은 소문자 '.png' 확장자를 가져야 합니다: {filename!r}")
    if Path(cleaned).name != cleaned:
        raise ValueError(f"파일명에는 경로 구분자를 포함할 수 없습니다: {filename!r}")
    return cleaned


def _write_bytes_atomically(target_path: Path, payload: bytes) -> None:
    tmp_path = target_path.parent / f".tmp_{uuid4().hex}.png"
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
        except OSError:
            pass
        raise


def deploy_and_register_food_kind_png(
    db: Session,
    *,
    kind_key: str,
    source_png: Path,
    filename: str | None = None,
    static_dir: Path | None = None,
    require_transparency: bool = False,
    commit: bool = True,
) -> dict[str, object]:
    """디자이너 PNG를 검증해 정적 디렉터리에 원자 배포하고 food_kinds.representative_image_url을 등록한다 (#68)."""
    cleaned_key = (kind_key or "").strip()
    if not cleaned_key:
        raise ValueError("음식 종류 키(kind_key)는 비어 있을 수 없습니다.")

    food_kind = db.query(FoodKind).filter(FoodKind.kind_key == cleaned_key).one_or_none()
    if food_kind is None:
        raise ValueError(f"음식 종류를 찾을 수 없습니다: {cleaned_key}")
    previous_url = food_kind.representative_image_url

    target_filename = f"{cleaned_key}.png" if filename is None else filename
    safe_filename = _validate_png_filename(target_filename)

    asset_check = validate_menu_illustration_png(Path(source_png))
    if require_transparency and not asset_check.has_alpha:
        raise ValueError(
            f"배경 투명도(알파 채널)가 없는 PNG 파일은 등록할 수 없습니다: {source_png}"
        )

    effective_static_dir = food_kinds.STATIC_DIR if static_dir is None else Path(static_dir)
    illustrations_dir = (effective_static_dir.resolve() / "menu-illustrations").resolve()
    illustrations_dir.mkdir(parents=True, exist_ok=True)
    target_path = (illustrations_dir / safe_filename).resolve()
    if target_path.parent != illustrations_dir:
        raise ValueError(f"허용되지 않는 배포 파일 경로입니다: {safe_filename!r}")

    source_resolved = asset_check.path
    image_url = f"{MENU_ILLUSTRATIONS_URL_PREFIX}{safe_filename}"
    existed_before = target_path.is_file()
    need_copy = source_resolved != target_path
    previous_bytes = target_path.read_bytes() if (existed_before and need_copy) else None

    if need_copy:
        _write_bytes_atomically(target_path, source_resolved.read_bytes())

    try:
        validate_static_illustration_url(
            image_url,
            static_dir=effective_static_dir,
            verify_file_exists=True,
        )
        updated_kind = register_food_kind_illustration(
            db,
            kind_key=cleaned_key,
            image_url=image_url,
            static_dir=effective_static_dir,
            verify_file_exists=True,
            commit=commit,
        )
    except Exception:
        if commit:
            db.rollback()
        if need_copy:
            try:
                if not existed_before:
                    if target_path.exists():
                        target_path.unlink()
                elif previous_bytes is not None:
                    _write_bytes_atomically(target_path, previous_bytes)
            except OSError:
                pass
        raise

    return {
        "kind_key": updated_kind.kind_key,
        "food_kind_id": int(updated_kind.id),
        "name": updated_kind.name,
        "image_url": updated_kind.representative_image_url,
        "deployed_path": str(target_path),
        "previous_image_url": previous_url,
        "width": asset_check.width,
        "height": asset_check.height,
        "mode": asset_check.mode,
        "has_alpha": asset_check.has_alpha,
        "size_bytes": asset_check.size_bytes,
    }


def inspect_food_kind_illustration_status(
    db: Session,
    *,
    static_dir: Path | None = None,
) -> list[dict[str, object]]:
    """초기 8종 음식 종류의 원본 대조 상태, DB 등록 경로, 디스크 배포 여부, 실조회 URL을 보고한다 (#68)."""
    effective_static_dir = food_kinds.STATIC_DIR if static_dir is None else Path(static_dir)
    spec_keys = [spec.kind_key for spec in FOOD_KIND_ILLUSTRATION_SPECS]
    kinds_by_key = {
        fk.kind_key: fk
        for fk in db.query(FoodKind).filter(FoodKind.kind_key.in_(spec_keys)).all()
    }

    statuses: list[dict[str, object]] = []
    for spec in FOOD_KIND_ILLUSTRATION_SPECS:
        food_kind = kinds_by_key.get(spec.kind_key)
        registered_url = food_kind.representative_image_url if food_kind is not None else None
        target_url_to_check = (registered_url or "").strip() or spec.expected_image_url
        try:
            _cleaned, candidate = _resolve_static_candidate(
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
            {
                "kind_key": spec.kind_key,
                "food_kind_id": int(food_kind.id) if food_kind is not None else None,
                "name": food_kind.name if food_kind is not None else spec.name,
                "audit_status": spec.status,
                "expected_image_url": spec.expected_image_url,
                "registered_image_url": registered_url,
                "file_exists_on_disk": file_exists_on_disk,
                "effective_image_url": effective_url,
                "using_common_fallback": effective_url == food_kinds.DEFAULT_MENU_ILLUSTRATION_URL,
            }
        )
    return statuses


def sync_food_kind_representative_illustrations(
    db: Session,
    *,
    static_dir: Path | None = None,
    overwrite_existing: bool = False,
    commit: bool = True,
) -> dict[str, int]:
    """정적 디렉터리에 실제로 배포된 대표 일러스트 파일만 food_kinds.representative_image_url에 반영한다."""
    spec_keys = [spec.kind_key for spec in FOOD_KIND_ILLUSTRATION_SPECS]
    kinds_by_key = {
        fk.kind_key: fk
        for fk in db.query(FoodKind).filter(FoodKind.kind_key.in_(spec_keys)).all()
    }

    updated = 0
    already_registered = 0
    skipped_missing_file = 0
    skipped_missing_kind = 0

    for spec in FOOD_KIND_ILLUSTRATION_SPECS:
        _cleaned, candidate = _resolve_static_candidate(
            spec.expected_image_url,
            static_dir=static_dir,
        )
        if not candidate.is_file():
            skipped_missing_file += 1
            continue

        food_kind = kinds_by_key.get(spec.kind_key)
        if food_kind is None:
            skipped_missing_kind += 1
            continue

        current_url = (food_kind.representative_image_url or "").strip()
        if current_url and not overwrite_existing:
            already_registered += 1
            continue

        if current_url != spec.expected_image_url:
            food_kind.representative_image_url = spec.expected_image_url
            updated += 1
        else:
            already_registered += 1

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

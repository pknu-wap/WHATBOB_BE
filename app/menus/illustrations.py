from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from app.menus import food_kinds
from app.menus.models import FoodKind, Menu, MenuIllustrationException

MENU_ILLUSTRATIONS_URL_PREFIX = "/static/menu-illustrations/"
DEFAULT_NON_TOMATO_PASTA_EXCEPTION_REASON = (
    "대표 파스타(토마토 스파게티) 일러스트와 소스·외형이 크게 다른 메뉴"
)


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

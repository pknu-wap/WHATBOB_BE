from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.menus.food_kinds import CANONICAL_FOOD_KINDS, resolve_menu_illustration_url
from app.menus.models import FoodKind, InitialPopularFoodKind
from app.menus.schemas import PopularMenuItemResponse, PopularMenusResponse

INITIAL_POPULAR_MENU_COUNT = 8

# 이슈 #54 댓글 합의 순서: 떡볶이 → 돈까스 → 햄버거 → 마라탕 → 파스타 → 국밥 → 짜장면 → 제육볶음
DEFAULT_INITIAL_POPULAR_KIND_KEYS: tuple[str, ...] = (
    "tteokbokki",
    "tonkatsu",
    "burger",
    "malatang",
    "pasta",
    "gukbap",
    "jjajangmyeon",
    "jeyuk_bokkeum",
)

_CANONICAL_NAME_BY_KEY: dict[str, str] = {
    spec.kind_key: spec.name for spec in CANONICAL_FOOD_KINDS
}


def _normalize_kind_keys(kind_keys: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_key in kind_keys:
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("초기 인기 메뉴의 음식 종류 키는 비어 있을 수 없습니다.")
        key = raw_key.strip()
        if key in seen:
            raise ValueError(f"초기 인기 메뉴에 중복된 음식 종류 키가 포함되어 있습니다: {key}")
        seen.add(key)
        normalized.append(key)

    if len(normalized) != INITIAL_POPULAR_MENU_COUNT:
        raise ValueError(
            f"초기 인기 메뉴는 정확히 {INITIAL_POPULAR_MENU_COUNT}개여야 합니다. "
            f"(입력: {len(normalized)}개)"
        )
    return normalized


def seed_initial_popular_food_kinds(
    db: Session,
    kind_keys: Sequence[str] | None = None,
    *,
    ensure_canonical_kinds: bool | None = None,
    commit: bool = True,
) -> list[InitialPopularFoodKind]:
    """초기 인기 메뉴 8종의 위치(1..8)와 음식 종류 연결을 한 트랜잭션으로 검증·반영한다."""
    use_default = kind_keys is None
    target_keys = _normalize_kind_keys(
        DEFAULT_INITIAL_POPULAR_KIND_KEYS if use_default else kind_keys
    )
    should_ensure_canonical = use_default if ensure_canonical_kinds is None else ensure_canonical_kinds

    with db.begin_nested():
        existing_kinds = {
            fk.kind_key: fk
            for fk in db.query(FoodKind).filter(FoodKind.kind_key.in_(target_keys)).all()
        }

        if should_ensure_canonical:
            created_any = False
            for key in target_keys:
                if key not in existing_kinds and key in _CANONICAL_NAME_BY_KEY:
                    fk = FoodKind(kind_key=key, name=_CANONICAL_NAME_BY_KEY[key])
                    db.add(fk)
                    existing_kinds[key] = fk
                    created_any = True
            if created_any:
                db.flush()

        missing_keys = [key for key in target_keys if key not in existing_kinds]
        if missing_keys:
            raise ValueError(
                f"등록되지 않은 음식 종류 키가 포함되어 있습니다: {', '.join(missing_keys)}"
            )

        desired_pairs = [
            (position, existing_kinds[key].id)
            for position, key in enumerate(target_keys, start=1)
        ]

        current_rows = (
            db.query(InitialPopularFoodKind)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        )
        current_pairs = [(row.position, row.food_kind_id) for row in current_rows]
        if current_pairs == desired_pairs:
            result_rows = current_rows
        else:
            # position과 food_kind_id 모두 UNIQUE 제약이 있으므로 동일 트랜잭션 내에서 비우고 재적재한다.
            for row in current_rows:
                db.delete(row)
            db.flush()

            result_rows = [
                InitialPopularFoodKind(position=position, food_kind_id=food_kind_id)
                for position, food_kind_id in desired_pairs
            ]
            db.add_all(result_rows)
            db.flush()

    if commit:
        db.commit()
    return result_rows


def ensure_initial_popular_food_kinds(
    db: Session,
    *,
    commit: bool = True,
) -> list[InitialPopularFoodKind]:
    """초기 인기 메뉴 테이블이 비어 있을 때만 기본 8종을 등록하고, 기존 순서는 보존한다."""
    existing_rows = (
        db.query(InitialPopularFoodKind)
        .order_by(InitialPopularFoodKind.position.asc())
        .all()
    )
    if existing_rows:
        return existing_rows
    return seed_initial_popular_food_kinds(db, commit=commit)


def _fetch_initial_popular_rows(
    db: Session,
) -> list[tuple[InitialPopularFoodKind, FoodKind]]:
    return (
        db.query(InitialPopularFoodKind, FoodKind)
        .join(FoodKind, InitialPopularFoodKind.food_kind_id == FoodKind.id)
        .order_by(InitialPopularFoodKind.position.asc())
        .all()
    )


def get_popular_menus(db: Session) -> PopularMenusResponse:
    """요즘 인기 메뉴를 조회한다. 실제 집계 순위 발행 전에는 DB의 초기 인기 8종을 위치순으로 반환한다."""
    rows = _fetch_initial_popular_rows(db)
    if not rows:
        try:
            ensure_initial_popular_food_kinds(db, commit=True)
        except IntegrityError:
            # 최초 동시 요청이 겹친 경우 다른 트랜잭션이 채운 초기 목록을 다시 조회한다.
            db.rollback()
        rows = _fetch_initial_popular_rows(db)

    items = [
        PopularMenuItemResponse(
            food_kind_id=food_kind.id,
            name=food_kind.name,
            image_url=resolve_menu_illustration_url(food_kind.representative_image_url),
        )
        for _entry, food_kind in rows
    ]
    return PopularMenusResponse(
        source="initial",
        as_of=None,
        items=items,
    )

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import Session

STATIC_URL_PREFIX = "/static/"
MENU_ILLUSTRATIONS_URL_PREFIX = f"{STATIC_URL_PREFIX}menu-illustrations/"
DEFAULT_MENU_ILLUSTRATION_URL = f"{MENU_ILLUSTRATIONS_URL_PREFIX}default.svg"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@dataclass(frozen=True)
class FoodKindSpec:
    kind_key: str
    name: str
    aliases: tuple[str, ...]


# 이슈 #54 댓글 합의 순서대로 정의한 초기 인기 음식 종류 8종 및 대표 별칭 규칙
CANONICAL_FOOD_KINDS: tuple[FoodKindSpec, ...] = (
    FoodKindSpec(kind_key="tteokbokki", name="떡볶이", aliases=("떡볶이",)),
    FoodKindSpec(kind_key="tonkatsu", name="돈까스", aliases=("돈가스", "돈까스")),
    FoodKindSpec(kind_key="burger", name="햄버거", aliases=("버거", "햄버거")),
    FoodKindSpec(kind_key="malatang", name="마라탕", aliases=("마라탕",)),
    FoodKindSpec(kind_key="pasta", name="파스타", aliases=("스파게티", "파스타")),
    FoodKindSpec(kind_key="gukbap", name="국밥", aliases=("국밥",)),
    FoodKindSpec(kind_key="jjajangmyeon", name="짜장면", aliases=("자장면", "짜장면")),
    FoodKindSpec(
        kind_key="jeyuk_bokkeum",
        name="제육볶음",
        aliases=("제육볶음", "돼지고기볶음(제육볶음)"),
    ),
)

FOOD_KIND_ALIAS_MAP: dict[str, FoodKindSpec] = {
    alias: spec
    for spec in CANONICAL_FOOD_KINDS
    for alias in spec.aliases
}


def classify_food_kind(
    *,
    base_name: str | None = None,
    name: str | None = None,
) -> tuple[str, str]:
    """메뉴의 base_name(또는 원본 name)으로부터 (kind_key, 표시명)을 결정한다."""
    cleaned_base = (base_name or "").strip()
    if not cleaned_base and name:
        cleaned_base = name.split("_")[0].strip()
    if not cleaned_base:
        raise ValueError("음식 종류를 분류하려면 base_name 또는 name이 필요합니다.")

    spec = FOOD_KIND_ALIAS_MAP.get(cleaned_base)
    if spec is not None:
        return spec.kind_key, spec.name
    return cleaned_base, cleaned_base


def resolve_safe_static_file_path(
    image_url: str,
    *,
    static_dir: Path | None = None,
) -> tuple[str, Path]:
    """'/static/' 상대 URL 형식을 검증하고 정적 루트 내부의 안전한 파일 경로를 반환한다."""
    cleaned = (image_url or "").strip()
    if not cleaned.startswith(STATIC_URL_PREFIX) or "\\" in cleaned:
        raise ValueError(
            f"일러스트 경로는 '{STATIC_URL_PREFIX}'으로 시작하는 백엔드 상대 경로여야 합니다: {image_url!r}"
        )
    rel_path = cleaned.removeprefix(STATIC_URL_PREFIX)
    if not rel_path or rel_path.startswith("/"):
        raise ValueError(f"유효하지 않은 정적 일러스트 경로입니다: {image_url!r}")

    effective_static_dir = STATIC_DIR if static_dir is None else Path(static_dir)
    static_root = effective_static_dir.resolve()
    candidate = (static_root / rel_path).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError as exc:
        raise ValueError(f"정적 파일 루트 밖의 경로는 허용되지 않습니다: {image_url!r}") from exc
    return cleaned, candidate


def find_deployed_menu_illustration_url(
    image_url: str,
    *,
    static_dir: Path | None = None,
    prefer_svg: bool = False,
) -> str | None:
    """등록된 형식을 우선하고, 같은 이름의 대체 파일을 찾는다."""
    cleaned = image_url.strip()
    stem, _, extension = cleaned.rpartition(".")
    urls = (cleaned,)
    if cleaned.startswith(MENU_ILLUSTRATIONS_URL_PREFIX) and (
        extension == "svg" or (extension == "png" and prefer_svg)
    ):
        urls = (f"{stem}.svg", f"{stem}.png")

    for url in urls:
        try:
            _, candidate = resolve_safe_static_file_path(url, static_dir=static_dir)
        except ValueError:
            continue
        if candidate.is_file():
            return url
    return None


def resolve_menu_illustration_url(
    representative_image_url: str | None,
    *,
    use_common_fallback: bool = False,
    static_dir: Path | None = None,
) -> str:
    """대표 일러스트 경로가 등록되어 있고 정적 파일이 존재하면 해당 상대 경로를, 아니면 공통 이미지를 반환한다."""
    if use_common_fallback or not representative_image_url:
        return DEFAULT_MENU_ILLUSTRATION_URL

    return (
        find_deployed_menu_illustration_url(representative_image_url, static_dir=static_dir)
        or DEFAULT_MENU_ILLUSTRATION_URL
    )


def get_menu_illustration_urls(db: Session, menu_ids: list[int]) -> dict[int, str]:
    """메뉴들의 대표 일러스트와 메뉴별 예외를 한 번에 조회한다."""
    if not menu_ids:
        return {}

    from app.menus.models import FoodKind, Menu, MenuIllustrationException

    illustrations = (
        db.query(Menu.id, FoodKind.representative_image_url, MenuIllustrationException.menu_id)
        .select_from(Menu)
        .outerjoin(FoodKind, Menu.food_kind_id == FoodKind.id)
        .outerjoin(MenuIllustrationException, Menu.id == MenuIllustrationException.menu_id)
        .filter(Menu.id.in_(menu_ids))
        .all()
    )
    urls_by_id = dict.fromkeys(menu_ids, DEFAULT_MENU_ILLUSTRATION_URL)
    for menu_id, representative_url, exception_menu_id in illustrations:
        urls_by_id[menu_id] = resolve_menu_illustration_url(
            representative_url,
            use_common_fallback=exception_menu_id is not None,
        )
    return urls_by_id


def get_menu_illustration_url(db: Session, menu_id: int) -> str:
    return get_menu_illustration_urls(db, [menu_id])[menu_id]


def _ensure_new_menu_food_kind_on_flush(
    session: Session,
    _flush_context,
    _instances,
) -> None:
    """food_kind_id 없이 새로 삽입되는 Menu에만 음식 종류를 자동 연결한다.

    기존 행 수정(session.dirty)은 건드리지 않아 menus.food_kind_id NOT NULL 제약을 가리지 않는다.
    """
    from app.menus.models import FoodKind, Menu

    pending_menus = [
        obj
        for obj in session.new
        if isinstance(obj, Menu) and obj.food_kind_id is None and obj.food_kind is None
    ]
    if not pending_menus:
        return

    with session.no_autoflush:
        specs_by_menu: list[tuple[Menu, str, str]] = []
        needed_keys: set[str] = set()
        for menu in pending_menus:
            raw_base = (menu.base_name or "").strip() or (menu.name or "").split("_")[0].strip()
            if not raw_base:
                continue
            kind_key, display_name = classify_food_kind(base_name=menu.base_name, name=menu.name)
            specs_by_menu.append((menu, kind_key, display_name))
            needed_keys.add(kind_key)

        if not specs_by_menu:
            return

        kinds_by_key: dict[str, FoodKind] = {}
        for obj in (*session.new, *session.identity_map.values()):
            if isinstance(obj, FoodKind) and obj.kind_key in needed_keys:
                kinds_by_key[obj.kind_key] = obj

        missing_keys = needed_keys - kinds_by_key.keys()
        if missing_keys:
            for existing in session.query(FoodKind).filter(FoodKind.kind_key.in_(missing_keys)).all():
                kinds_by_key[existing.kind_key] = existing

        for menu, kind_key, display_name in specs_by_menu:
            fk = kinds_by_key.get(kind_key)
            if fk is None:
                fk = FoodKind(kind_key=kind_key, name=display_name)
                session.add(fk)
                kinds_by_key[kind_key] = fk
            menu.food_kind = fk
            if fk.id is not None:
                menu.food_kind_id = fk.id


def register_new_menu_food_kind_listener() -> None:
    """신규 Menu 자동 음식 종류 연결 리스너를 Session에 멱등 등록한다."""
    if not event.contains(Session, "before_flush", _ensure_new_menu_food_kind_on_flush):
        event.listen(Session, "before_flush", _ensure_new_menu_food_kind_on_flush)


register_new_menu_food_kind_listener()

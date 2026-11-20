from dataclasses import dataclass
from pathlib import Path

DEFAULT_MENU_ILLUSTRATION_URL = "/static/menu-illustrations/default.svg"
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


def resolve_menu_illustration_url(
    representative_image_url: str | None,
    *,
    use_common_fallback: bool = False,
    static_dir: Path | None = None,
) -> str:
    """대표 일러스트 경로가 등록되어 있고 정적 파일이 존재하면 해당 상대 경로를, 아니면 공통 이미지를 반환한다."""
    if use_common_fallback:
        return DEFAULT_MENU_ILLUSTRATION_URL

    cleaned = (representative_image_url or "").strip()
    if cleaned.startswith("/static/") and "\\" not in cleaned:
        rel_path = cleaned.removeprefix("/static/")
        if rel_path and not rel_path.startswith("/"):
            effective_static_dir = STATIC_DIR if static_dir is None else static_dir
            static_root = effective_static_dir.resolve()
            candidate = (static_root / rel_path).resolve()
            try:
                candidate.relative_to(static_root)
            except ValueError:
                return DEFAULT_MENU_ILLUSTRATION_URL
            if candidate.is_file():
                return cleaned
    return DEFAULT_MENU_ILLUSTRATION_URL

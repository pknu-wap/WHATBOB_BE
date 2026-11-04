"""원본 음식명을 보존하면서 화면에 표시할 이름을 만든다."""

COMPOUND_DISH_NAMES = frozenset({
    "김밥", "볶음밥", "비빔밥", "덮밥", "주먹밥", "초밥", "국밥",
    "라면", "칼국수", "우동", "냉면", "스파게티",
})

# 원본 분류명과 실제 상품명에서 사용하는 음식 이름이 다른 경우.
DISH_NAME_ALIASES = {
    "닭튀김": ("치킨",),
    "스파게티": ("파스타",),
    "스프": ("수프",),
    "리소토/리조또": ("리소토", "리조또"),
    "닭볶음(닭갈비)": ("닭갈비",),
    "돈가스": ("돈까스", "카츠"),
}

PREPARATION_LABELS = frozenset({"간편조리세트"})


def format_menu_name(name: str) -> str:
    """재료형 이름은 조합하고, 완성된 상품명은 중복 분류 없이 표시한다."""
    parts = [" ".join(part.split()) for part in name.split("_") if part.strip()]
    if not parts:
        return ""

    base, *details = parts
    if not details:
        return base

    is_prepared_product = any(part in PREPARATION_LABELS for part in details)
    details = [part for part in details if part not in PREPARATION_LABELS]
    if not details:
        return base

    detail = "·".join(details)
    if is_prepared_product:
        return detail

    # '국밥_순대국밥', '닭튀김_땡초 치킨' 등은 뒤에 이미 음식명이 있다.
    compact_detail = "".join(detail.split())
    dish_names = (base, *DISH_NAME_ALIASES.get(base, ()))
    if any("".join(dish.split()) in compact_detail for dish in dish_names):
        return detail

    if base in COMPOUND_DISH_NAMES and len(details) == 1 and " " not in detail and "(" not in detail:
        return f"{detail}{base}"

    # 재료·양념·조리 상태를 무조건 앞에 붙이면 뜻이 어색해지므로 함께 표시한다.
    return f"{base} ({detail})"

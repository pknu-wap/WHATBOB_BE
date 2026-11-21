import re

# 메뉴 데이터의 base_name은 식약처 분류명이라 카카오 장소 검색에서 실제 상호·업종명과 다를 수 있다.
# 검색 결과가 크게 달라지는 메뉴만 명시적으로 바꾸고, 나머지는 base_name을 그대로 사용한다.
SEARCH_KEYWORD_ALIASES: dict[str, str] = {
    "닭튀김": "치킨",
    "닭다리튀김": "치킨",
    "스파게티": "파스타",
    "오븐스파게티": "파스타",
    "닭볶음(닭갈비)": "닭갈비",
    "리소토/리조또": "리조또",
    "쌀죽(흰죽)": "죽",
    "어묵국(어묵탕)": "어묵탕",
}

_PARENTHESIS_PATTERN = re.compile(r"\(.*?\)")


def build_place_search_keyword(base_name: str | None, menu_name: str | None = None) -> str:
    """메뉴의 base_name으로 카카오 장소 검색어를 만든다."""
    raw = (base_name or "").strip() or (menu_name or "").strip()
    if raw in SEARCH_KEYWORD_ALIASES:
        return SEARCH_KEYWORD_ALIASES[raw]
    # 별칭에 없는 표기("A(B)", "A/B")는 첫 표기만 남겨 검색어가 장소명과 어긋나지 않게 한다.
    keyword = _PARENTHESIS_PATTERN.sub("", raw).split("/")[0].strip()
    return keyword or raw

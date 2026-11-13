from typing import Any, Iterable, Optional
from sqlalchemy.orm import Session
from app.menus.models import Menu, UserFoodExclusion


class RecommendationDomainException(Exception):
    """추천 도메인 기본 예외"""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NoAvailableFoodException(RecommendationDomainException):
    """제외 조건 적용 후 추천 가능한 음식 후보가 없을 때 발생하는 예외"""
    def __init__(self, message: str = "알레르기 및 제외 조건을 만족하는 추천 가능 메뉴가 없습니다."):
        super().__init__(message=message, status_code=404)


class QuestionPoolEmptyException(RecommendationDomainException):
    """활성화된 주문서 질문 풀이 비어 있을 때 발생하는 예외"""
    def __init__(self, message: str = "적재된 주문서 질문이 없습니다. 시드 스크립트를 먼저 실행해주세요."):
        super().__init__(message=message, status_code=404)


class RecommendationNotFoundException(RecommendationDomainException):
    """추천 이력을 찾을 수 없을 때 발생하는 예외"""
    def __init__(self, recommendation_id: int):
        super().__init__(
            message=f"추천 기록(id={recommendation_id})을 찾을 수 없습니다.",
            status_code=404,
        )


class FoodNotFoundException(RecommendationDomainException):
    """요청한 메뉴(음식)를 찾을 수 없을 때 발생하는 예외"""
    def __init__(self, menu_id: int):
        super().__init__(
            message=f"메뉴(id={menu_id})를 찾을 수 없습니다.",
            status_code=404,
        )


class InvalidOrderSheetException(RecommendationDomainException):
    """잘못된 주문서 응답 입력 시 발생하는 예외"""
    def __init__(self, message: str = "유효하지 않은 주문서 응답입니다."):
        super().__init__(message=message, status_code=400)


# 식품 등의 표시·광고에 관한 법률 시행규칙 [별표 2] 기준 식약처 고시 법정 19대 알레르기 유발물질
OFFICIAL_ALLERGEN_ITEMS: list[dict[str, Any]] = [
    {
        "id": "egg",
        "name": "알류(달걀)",
        "legal_name": "알류(가금류만 해당한다)",
        "description": "달걀, 계란, 메추리알, 마요네즈 등",
    },
    {
        "id": "milk",
        "name": "우유",
        "legal_name": "우유",
        "description": "우유, 치즈, 버터, 크림, 요거트, 라떼 등",
    },
    {
        "id": "buckwheat",
        "name": "메밀",
        "legal_name": "메밀",
        "description": "메밀국수, 모밀, 메밀전병 등",
    },
    {
        "id": "peanut",
        "name": "땅콩",
        "legal_name": "땅콩",
        "description": "땅콩, 땅콩버터 등",
    },
    {
        "id": "soybean",
        "name": "대두(콩)",
        "legal_name": "대두",
        "description": "두부, 된장, 청국장, 순두부, 콩나물 등",
    },
    {
        "id": "wheat",
        "name": "밀",
        "legal_name": "밀",
        "description": "밀가루, 빵, 면, 라면, 파스타, 튀김 등",
    },
    {
        "id": "mackerel",
        "name": "고등어",
        "legal_name": "고등어",
        "description": "고등어구이, 조림 등",
    },
    {
        "id": "crab",
        "name": "게",
        "legal_name": "게",
        "description": "꽃게, 간장게장, 킹크랩, 게살 등",
    },
    {
        "id": "shrimp",
        "name": "새우",
        "legal_name": "새우",
        "description": "새우튀김, 대하, 칵테일새우 등",
    },
    {
        "id": "pork",
        "name": "돼지고기",
        "legal_name": "돼지고기",
        "description": "삼겹살, 제육, 돈까스, 보쌈, 햄, 소시지 등",
    },
    {
        "id": "peach",
        "name": "복숭아",
        "legal_name": "복숭아",
        "description": "복숭아, 복숭아 에이드/디저트 등",
    },
    {
        "id": "tomato",
        "name": "토마토",
        "legal_name": "토마토",
        "description": "토마토, 케첩, 스파게티 토마토소스 등",
    },
    {
        "id": "sulfite",
        "name": "아황산류",
        "legal_name": "아황산류",
        "description": "건과일류, 와인 가공식품 등",
    },
    {
        "id": "walnut",
        "name": "호두",
        "legal_name": "호두",
        "description": "호두, 월넛 등",
    },
    {
        "id": "chicken",
        "name": "닭고기",
        "legal_name": "닭고기",
        "description": "치킨, 닭볶음탕, 삼계탕 등",
    },
    {
        "id": "beef",
        "name": "쇠고기(소고기)",
        "legal_name": "쇠고기",
        "description": "소고기, 한우, 불고기, 갈비, 설렁탕 등",
    },
    {
        "id": "squid",
        "name": "오징어",
        "legal_name": "오징어",
        "description": "오징어볶음, 오징어순대 등",
    },
    {
        "id": "shellfish",
        "name": "조개류(굴/전복/홍합 포함)",
        "legal_name": "조개류(굴, 전복, 홍합을 포함한다)",
        "description": "바지락, 굴, 전복, 홍합, 가리비, 조개탕 등",
    },
    {
        "id": "pine_nut",
        "name": "잣",
        "legal_name": "잣",
        "description": "잣, 잣죽 등",
    },
]

for _item in OFFICIAL_ALLERGEN_ITEMS:
    _item["code"] = _item["id"]
    _item["label"] = _item["name"]

ALLERGEN_CATALOG_VERSION = 1
UNABLE_CATALOG_VERSION = 1

ALLERGEN_BY_CODE: dict[str, dict[str, Any]] = {item["id"]: item for item in OFFICIAL_ALLERGEN_ITEMS}

UNABLE_INGREDIENT_ITEMS: list[dict[str, Any]] = [
    {
        "id": "pork",
        "code": "pork",
        "name": "돼지고기",
        "label": "돼지고기",
        "category": "육류",
        "description": "삼겹살, 제육, 돈까스, 보쌈, 족발, 햄, 베이컨, 소시지, 순대 등",
    },
    {
        "id": "beef",
        "code": "beef",
        "name": "소고기",
        "label": "소고기",
        "category": "육류",
        "description": "소고기, 한우, 불고기, 갈비, 육회, 설렁탕, 곰탕, 차돌박이 등",
    },
    {
        "id": "poultry",
        "code": "poultry",
        "name": "닭·가금류",
        "label": "닭·가금류",
        "category": "육류",
        "description": "닭고기, 치킨, 삼계탕, 닭갈비, 백숙, 오리고기, 칠면조 등",
    },
    {
        "id": "seafood",
        "code": "seafood",
        "name": "해산물·어패류",
        "label": "해산물·어패류",
        "category": "수산",
        "description": "생선, 회, 초밥, 새우, 게, 오징어, 낙지, 문어, 조개, 굴, 전복, 홍합, 어묵 등",
    },
    {
        "id": "egg",
        "code": "egg",
        "name": "달걀(난류)",
        "label": "달걀(난류)",
        "category": "난류·유제품",
        "description": "달걀, 계란, 메추리알, 오믈렛, 에그, 마요네즈, 지단 등",
    },
    {
        "id": "dairy",
        "code": "dairy",
        "name": "유제품",
        "label": "유제품",
        "category": "난류·유제품",
        "description": "우유, 치즈, 버터, 크림, 요거트, 연유, 라떼 등",
    },
    {
        "id": "wheat",
        "code": "wheat",
        "name": "밀·글루텐",
        "label": "밀·글루텐",
        "category": "곡류·면·빵",
        "description": "밀가루, 빵, 면, 국수, 라면, 파스타, 우동, 만두, 피자, 튀김, 전 등",
    },
    {
        "id": "soybean",
        "code": "soybean",
        "name": "대두·콩류",
        "label": "대두·콩류",
        "category": "콩·두부",
        "description": "두부, 순두부, 콩, 된장, 청국장, 콩나물, 유부, 두유, 콩비지 등",
    },
]

UNABLE_BY_CODE: dict[str, dict[str, Any]] = {item["id"]: item for item in UNABLE_INGREDIENT_ITEMS}

OFFICIAL_ALLERGEN_RULES: dict[str, tuple[str, ...]] = {
    "알류(달걀)": ("달걀", "계란", "메추리알", "에그", "마요"),
    "우유": ("우유", "치즈", "크림", "버터", "요거트", "라떼", "유제품"),
    "메밀": ("메밀", "모밀"),
    "땅콩": ("땅콩", "피넛"),
    "대두(콩)": ("두부", "콩", "된장", "청국장", "순두부", "콩비지", "콩나물"),
    "밀": ("빵", "면", "국수", "파스타", "스파게티", "라면", "우동", "만두", "튀김", "전", "피자", "케이크", "밀가루"),
    "고등어": ("고등어",),
    "게": ("꽃게", "대게", "킹크랩", "게살", "간장게장", "양념게장", "게장"),
    "새우": ("새우", "대하", "칵테일새우", "새우튀김"),
    "돼지고기": ("돼지", "삼겹", "제육", "돈까스", "돈가스", "족발", "보쌈", "햄", "베이컨", "소시지", "탕수육", "순대", "수육"),
    "복숭아": ("복숭아",),
    "토마토": ("토마토", "케첩", "케찹"),
    "아황산류": ("아황산", "이산화황"),
    "호두": ("호두", "월넛"),
    "닭고기": ("닭", "치킨", "삼계", "칠면조", "백숙", "닭갈비", "닭볶음"),
    "쇠고기(소고기)": ("소고기", "한우", "불고기", "갈비", "차돌", "육회", "설렁탕", "곰탕", "우삼겹", "소갈비"),
    "오징어": ("오징어",),
    "조개류(굴/전복/홍합 포함)": ("조개", "홍합", "바지락", "굴", "전복", "키조개", "가리비", "꼬막", "재첩"),
    "잣": ("잣",),
    # 기존 도메인 상위 호환 유지
    "해산물": (
        "해물", "새우", "오징어", "조개", "홍합", "바지락", "굴", "전복", "게", "생선", "참치",
        "고등어", "갈치", "황태", "어묵", "회", "홍어", "가오리", "연어", "낙지", "쭈꾸미",
        "주꾸미", "아귀", "코다리", "명태", "동태", "조기", "장어"
    ),
}


def derive_allergens_from_text(text: str) -> list[str]:
    """식약처 고시 19대 기준 및 주요 식품 키워드 기반으로 알레르기 태그를 추출합니다."""
    text_no_stingray = text.replace("가오리", "")
    tags: list[str] = []
    for allergen_name, keywords in OFFICIAL_ALLERGEN_RULES.items():
        target = text_no_stingray if allergen_name == "닭고기" else text
        if any(kw in target for kw in keywords):
            tags.append(allergen_name)
    return tags


def _menu_search_blob(menu: Menu) -> tuple[str, str, set[str]]:
    fields = [
        menu.name or "",
        menu.base_name or "",
        menu.category_large or "",
        menu.category_medium or "",
        menu.category_small or "",
    ]
    allergen_list = menu.allergen_tags if isinstance(menu.allergen_tags, list) else []
    allergen_set = {str(t).strip() for t in allergen_list if str(t).strip()}
    raw_text = " ".join(fields + list(allergen_set)).lower()
    no_stingray = raw_text.replace("가오리", "")
    return raw_text, no_stingray, allergen_set


ALLERGEN_MAIN_INGREDIENT_MAP: dict[str, set[str]] = {
    "pork": {"pork"},
    "beef": {"beef"},
    "chicken": {"chicken"},
    "wheat": {"flour_wheat", "flour"},
}

UNABLE_RULE_SPEC: dict[str, dict[str, Any]] = {
    "pork": {
        "main_ingredients": {"pork"},
        "allergen_tags": {"돼지고기"},
        "keywords": OFFICIAL_ALLERGEN_RULES["돼지고기"],
    },
    "beef": {
        "main_ingredients": {"beef"},
        "allergen_tags": {"쇠고기(소고기)", "쇠고기", "소고기"},
        "keywords": OFFICIAL_ALLERGEN_RULES["쇠고기(소고기)"] + ("쇠고기", "스테이크", "사골"),
    },
    "poultry": {
        "main_ingredients": {"chicken"},
        "allergen_tags": {"닭고기"},
        "keywords": OFFICIAL_ALLERGEN_RULES["닭고기"] + ("오리고기", "오리", "가금류"),
        "strip_stingray": True,
    },
    "seafood": {
        "main_ingredients": {"seafood"},
        "allergen_tags": {"고등어", "게", "새우", "오징어", "조개류(굴/전복/홍합 포함)", "조개류", "해산물"},
        "keywords": OFFICIAL_ALLERGEN_RULES["해산물"] + ("해산물", "어패류", "초밥", "문어", "멸치", "북어", "복어", "추어", "메기"),
    },
    "egg": {
        "main_ingredients": set(),
        "allergen_tags": {"알류(달걀)", "알류", "난류", "달걀", "계란"},
        "keywords": OFFICIAL_ALLERGEN_RULES["알류(달걀)"] + ("난류", "오믈렛", "스크램블", "지단", "수란"),
    },
    "dairy": {
        "main_ingredients": set(),
        "allergen_tags": {"우유"},
        "keywords": OFFICIAL_ALLERGEN_RULES["우유"] + ("요구르트", "연유", "생크림", "모짜렐라", "체다", "파르메산", "밀크"),
    },
    "wheat": {
        "main_ingredients": {"flour_wheat", "flour"},
        "allergen_tags": {"밀"},
        "keywords": OFFICIAL_ALLERGEN_RULES["밀"] + ("밀", "글루텐", "짜장면", "짬뽕", "칼국수", "수제비", "버거", "햄버거", "샌드위치", "토스트", "베이글", "도넛", "쿠키", "부침개"),
    },
    "soybean": {
        "main_ingredients": set(),
        "allergen_tags": {"대두(콩)", "대두"},
        "keywords": OFFICIAL_ALLERGEN_RULES["대두(콩)"] + ("대두", "유부", "두유", "비지", "낫또", "춘장", "짜장"),
    },
}


def matches_allergen_code(menu: Menu, code: str) -> bool:
    item = ALLERGEN_BY_CODE.get(code)
    if item is None:
        return False
    allergen_name = item["name"]
    raw_text, no_stingray, allergen_set = _menu_search_blob(menu)
    if allergen_name in allergen_set or item["legal_name"] in allergen_set:
        return True
    main_ings = ALLERGEN_MAIN_INGREDIENT_MAP.get(code)
    if main_ings and (menu.main_ingredient or "") in main_ings:
        return True
    keywords = OFFICIAL_ALLERGEN_RULES.get(allergen_name, ())
    target_text = no_stingray if code == "chicken" else raw_text
    return any(kw.lower() in target_text for kw in keywords)


def matches_unable_code(menu: Menu, code: str) -> bool:
    spec = UNABLE_RULE_SPEC.get(code)
    if spec is None:
        return False
    raw_text, no_stingray, allergen_set = _menu_search_blob(menu)
    if (menu.main_ingredient or "") in spec["main_ingredients"]:
        return True
    if allergen_set & spec["allergen_tags"]:
        return True
    target_text = no_stingray if spec.get("strip_stingray") else raw_text
    return any(kw.lower() in target_text for kw in spec["keywords"])


def inspect_menu_catalog_coverage(menus: Iterable[Menu]) -> dict[str, Any]:
    """
    [M2A] 7,503개 메뉴 데이터에 대해 19종 알레르기와 8종 섭취불가 규칙의 매칭 분포와
    main_ingredient='other' 및 빈 allergen_tags 미검증 메뉴 건수를 집계한다.
    """
    menu_list = list(menus)
    allergen_counts = {code: 0 for code in ALLERGEN_BY_CODE}
    unable_counts = {code: 0 for code in UNABLE_BY_CODE}
    unverified_other_menus = 0

    for menu in menu_list:
        matched_any = False
        for code in ALLERGEN_BY_CODE:
            if matches_allergen_code(menu, code):
                allergen_counts[code] += 1
                matched_any = True
        for code in UNABLE_BY_CODE:
            if matches_unable_code(menu, code):
                unable_counts[code] += 1
                matched_any = True
        tags = menu.allergen_tags if isinstance(menu.allergen_tags, list) else []
        if not matched_any and (menu.main_ingredient or "other") == "other" and not tags:
            unverified_other_menus += 1

    return {
        "total_menus": len(menu_list),
        "allergen_code_counts": allergen_counts,
        "unable_code_counts": unable_counts,
        "unverified_other_menus": unverified_other_menus,
    }


def collect_user_exclusion_keywords(
    db: Session,
    user_id: Optional[int] = None,
    extra_exclusions: Optional[Iterable[str]] = None,
) -> set[str]:
    """DB에 저장된 사용자의 알레르기/섭취불가/비선호 코드·키워드와 요청의 추가 제외 키워드를 통합합니다."""
    keywords: set[str] = set()
    if user_id is not None:
        rows = (
            db.query(UserFoodExclusion)
            .filter(UserFoodExclusion.user_id == user_id)
            .all()
        )
        for row in rows:
            exc_type = (row.exclusion_type or "").upper()
            code = (row.catalog_code or "").strip()
            if exc_type == "ALLERGY" and code in ALLERGEN_BY_CODE:
                keywords.add(f"allergy_code:{code}")
                continue
            if exc_type == "UNABLE" and code in UNABLE_BY_CODE:
                keywords.add(f"unable_code:{code}")
                continue
            cleaned = (row.keyword or "").strip()
            if cleaned:
                keywords.add(cleaned)

    if extra_exclusions:
        for item in extra_exclusions:
            cleaned = (item or "").strip()
            if not cleaned:
                continue
            if cleaned in ALLERGEN_BY_CODE:
                keywords.add(f"allergy_code:{cleaned}")
            if cleaned in UNABLE_BY_CODE:
                keywords.add(f"unable_code:{cleaned}")
            keywords.add(cleaned)

    return keywords


def is_menu_excluded(menu: Menu, exclusion_keywords: set[str]) -> bool:
    """메뉴가 사용자의 알레르기/비선호/섭취불가 코드 또는 키워드에 해당하는지 판별합니다."""
    if not exclusion_keywords:
        return False

    searchable_fields = [
        menu.name or "",
        menu.base_name or "",
        menu.category_large or "",
        menu.category_medium or "",
        menu.category_small or "",
        menu.main_ingredient or "",
    ]
    allergen_list = menu.allergen_tags if isinstance(menu.allergen_tags, list) else []
    searchable_text = " ".join(searchable_fields + [str(t) for t in allergen_list]).lower()

    for kw in exclusion_keywords:
        if kw.startswith("allergy_code:"):
            code = kw.split(":", 1)[1]
            if matches_allergen_code(menu, code):
                return True
            continue
        if kw.startswith("unable_code:"):
            code = kw.split(":", 1)[1]
            if matches_unable_code(menu, code):
                return True
            continue
        kw_lower = kw.lower()
        if kw_lower in searchable_text:
            return True
    return False


def filter_available_menus(
    menus: list[Menu],
    exclusion_keywords: set[str],
) -> tuple[list[Menu], int]:
    """
    [AGENTS.md 도메인 규칙]
    알레르기 / 섭취 불가능 / 비선호 음식을 기본 추천 후보에서 가장 먼저 제거합니다.
    후보가 없을 때 제외 조건을 임의로 해제하지 않고 NoAvailableFoodException을 발생시킵니다.
    """
    if not menus:
        raise NoAvailableFoodException("등록된 메뉴 데이터가 없습니다.")

    available: list[Menu] = []
    excluded_count = 0

    for menu in menus:
        if is_menu_excluded(menu, exclusion_keywords):
            excluded_count += 1
        else:
            available.append(menu)

    if not available:
        raise NoAvailableFoodException()

    return available, excluded_count


def evaluate_single_rule(menu: Menu, rule: dict[str, Any]) -> bool:
    """단일 조건식(field, op, value)이 해당 Menu 엔티티를 만족하는지 평가합니다."""
    field = rule.get("field")
    op = rule.get("op", "eq")
    target = rule.get("value")

    if not field or not hasattr(menu, field):
        return False

    actual = getattr(menu, field)
    if actual is None:
        return False

    if op == "eq":
        return actual == target
    if op == "neq":
        return actual != target
    if op == "in":
        return isinstance(target, (list, tuple, set)) and actual in target
    if op == "not_in":
        return isinstance(target, (list, tuple, set)) and actual not in target
    if op == "gte":
        return float(actual) >= float(target)
    if op == "lte":
        return float(actual) <= float(target)
    if op == "gt":
        return float(actual) > float(target)
    if op == "lt":
        return float(actual) < float(target)
    if op == "between":
        if isinstance(target, (list, tuple)) and len(target) == 2:
            return float(target[0]) <= float(actual) <= float(target[1])
        return False
    if op == "contains":
        return str(target).lower() in str(actual).lower()

    return False


def evaluate_option_rules(menu: Menu, rules: list[dict[str, Any]]) -> bool:
    """
    선택지에 연결된 모든 조건식(rules)을 평가합니다.
    rules가 빈 리스트([])이면 '상관없음(any)' 선택지로 간주하여 True를 반환합니다.
    """
    if not rules:
        return True
    return all(evaluate_single_rule(menu, rule) for rule in rules)


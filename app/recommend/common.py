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


def collect_user_exclusion_keywords(
    db: Session,
    user_id: Optional[int] = None,
    extra_exclusions: Optional[Iterable[str]] = None,
) -> set[str]:
    """DB에 저장된 사용자의 알레르기/섭취불가/비선호 키워드와 요청의 추가 제외 키워드를 통합합니다."""
    keywords: set[str] = set()
    if user_id is not None:
        rows = (
            db.query(UserFoodExclusion)
            .filter(UserFoodExclusion.user_id == user_id)
            .all()
        )
        for row in rows:
            cleaned = (row.keyword or "").strip()
            if cleaned:
                keywords.add(cleaned)

    if extra_exclusions:
        for item in extra_exclusions:
            cleaned = (item or "").strip()
            if cleaned:
                keywords.add(cleaned)

    return keywords


def is_menu_excluded(menu: Menu, exclusion_keywords: set[str]) -> bool:
    """메뉴가 사용자의 알레르기/비선호/섭취불가 키워드에 해당하는지 판별합니다."""
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


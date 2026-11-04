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


class InvalidOrderSheetException(RecommendationDomainException):
    """잘못된 주문서 응답 입력 시 발생하는 예외"""
    def __init__(self, message: str = "유효하지 않은 주문서 응답입니다."):
        super().__init__(message=message, status_code=400)


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


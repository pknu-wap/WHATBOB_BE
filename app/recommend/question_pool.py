"""두 취향 선택지와 상관없음, 24자 이내 문구를 사용하는 주문서 질문 후보."""

from typing import Any
from functools import lru_cache

MAX_QUESTION_TEXT_LENGTH = 24
QUESTION_OPTION_COUNT = 3
QUESTION_CODE_PREFIX = "q_binary_v1_"


def build_candidate_question_pool() -> list[dict[str, Any]]:
    # 두 번째 선택지는 첫 번째 조건의 반대다. 기존 재료·조리 분류를 버리지 않는다.
    specs = [
        ("meal_type", "든든한 식사를", "meal_type", "eq", "main_meal",
         ("main_meal", "든든한 식사"), ("snack_drink", "간식·디저트·음료")),
        ("has_soup", "국물 있는 요리를", "has_soup", "eq", True,
         ("soup_yes", "국물 있는 요리"), ("soup_no", "국물 없는 요리")),
        ("cooking_style", "밥이나 면 요리를", "cooking_style", "in", ["rice", "noodle"],
         ("rice_noodle", "밥·면 요리"), ("other_style", "국·구이·튀김 등")),
        ("main_ingredient", "고기 요리를", "main_ingredient", "in", ["beef", "pork", "poultry"],
         ("meat", "소·돼지·닭·오리고기"), ("non_meat", "해산물·채소 등")),
        ("flavor_intensity", "매콤한 음식을", "flavor_intensity", "eq", "spicy_bold",
         ("spicy_bold", "매콤·얼큰한 맛"), ("not_spicy", "담백·달콤·고소한 맛")),
        ("temperature", "따뜻한 음식을", "temperature", "eq", "hot",
         ("hot", "따뜻한 요리"), ("cold", "차가운 요리")),
        ("heaviness", "가벼운 식사를", "heaviness", "eq", "light",
         ("light", "가볍게 (700kcal 미만)"), ("filling", "든든하게 (700kcal 이상)")),
        ("protein_level", "고단백 식사를", "protein_g", "gte", 10.0,
         ("high_protein", "단백질 10g 이상"), ("normal_protein", "단백질 10g 미만")),
        ("carb_level", "저탄수화물 식사를", "carb_g", "lte", 15.0,
         ("low_carb", "탄수화물 15g 이하"), ("rich_carb", "탄수화물 15g 초과")),
        ("sodium_level", "싱거운 음식을", "sodium_mg", "lte", 250.0,
         ("low_sodium", "나트륨 250mg 이하"), ("savory_sodium", "나트륨 250mg 초과")),
        ("sugar_level", "당이 적은 음식을", "sugar_g", "lte", 3.0,
         ("low_sugar", "당류 3g 이하"), ("with_sugar", "당류 3g 초과")),
        ("origin_type", "집밥 스타일 음식을", "origin_type", "in", ["home_style", "cafeteria"],
         ("home_style", "집밥·급식 스타일"), ("eating_out", "외식·프랜차이즈")),
    ]
    templates = [
        "{topic} 먹고 싶나요?",
        "오늘은 {topic} 드실래요?",
        "지금 {topic} 먹고 싶나요?",
        "이번엔 {topic} 드실래요?",
        "{topic} 먹으면 좋겠나요?",
        "오늘 {topic} 고를까요?",
        "{topic} 먹을까요?",
        "이번 식사는 {topic} 고를까요?",
        "{topic} 먹어볼까요?",
        "오늘은 {topic} 원하나요?",
        "지금은 {topic} 원하나요?",
        "이번에는 {topic} 먹을까요?",
        "오늘 {topic} 먹고 싶나요?",
        "{topic} 드시고 싶나요?",
        "{topic} 골라볼까요?",
        "지금 {topic} 드실래요?",
        "이번에 {topic} 먹어볼까요?",
    ]
    opposite_ops = {"eq": "neq", "in": "not_in", "gte": "lt", "lte": "gt"}
    pool = []
    for cluster, topic, field, op, value, positive, negative in specs:
        options = [
            {"option_id": positive[0], "label": "예",
             "rules": [{"field": field, "op": op, "value": value}]},
            {"option_id": negative[0], "label": "아니요",
             "rules": [{"field": field, "op": opposite_ops[op], "value": value}]},
            {"option_id": "any", "label": "상관없어요", "rules": []},
        ]
        for idx, template in enumerate(templates, start=1):
            question_text = template.format(topic=topic)
            for before, after in (
                ("식사를 먹고", "식사를 하고"), ("식사를 먹으면", "식사를 하면"),
                ("식사를 먹을까요", "식사를 할까요"), ("식사를 먹어볼까요", "식사를 해볼까요"),
            ):
                question_text = question_text.replace(before, after)
            if not 1 <= len(question_text) <= MAX_QUESTION_TEXT_LENGTH:
                raise ValueError("주문서 질문은 공백 포함 1~24자여야 합니다.")
            pool.append({
                "question_code": f"{QUESTION_CODE_PREFIX}{cluster}_{idx:02d}",
                "cluster_key": cluster,
                "question_text": question_text,
                "options": options,
            })
    return pool


@lru_cache(maxsize=1)
def _question_display_definitions() -> tuple[dict, dict]:
    questions = build_candidate_question_pool()
    by_code = {question["question_code"]: question for question in questions}
    by_cluster = {}
    for question in questions:
        by_cluster.setdefault(question["cluster_key"], question)
    return by_code, by_cluster


def get_question_display(
    question_code: str, cluster_key: str, options: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """기존 DB도 재적재 없이 표시를 갱신한다. ID와 조건이 같은 질문에만 적용한다."""
    if not question_code.startswith(QUESTION_CODE_PREFIX):
        return None
    by_code, by_cluster = _question_display_definitions()
    definition = by_code.get(question_code)
    if definition is None and question_code.startswith(f"{QUESTION_CODE_PREFIX}llm_"):
        definition = by_cluster.get(cluster_key)
    if definition is None or definition["cluster_key"] != cluster_key:
        return None
    actual = {option["option_id"]: option.get("rules", []) for option in options}
    expected = {option["option_id"]: option["rules"] for option in definition["options"]}
    if actual != expected:
        # 수동으로 조건을 바꾼 질문은 원래 문구를 유지해 예/아니요 의미가 뒤집히지 않게 한다.
        return None
    return definition

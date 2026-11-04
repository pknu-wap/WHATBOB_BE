"""버튼 두 개와 24자 이내 문구를 사용하는 주문서 질문 후보."""

from typing import Any

MAX_QUESTION_TEXT_LENGTH = 24
QUESTION_OPTION_COUNT = 2
QUESTION_CODE_PREFIX = "q_binary_v1_"


def build_candidate_question_pool() -> list[dict[str, Any]]:
    # 두 번째 선택지는 첫 번째 조건의 반대다. 기존 재료·조리 분류를 버리지 않는다.
    specs = [
        ("meal_type", "든든한 식사", "meal_type", "eq", "main_meal",
         ("main_meal", "든든한 식사"), ("snack_drink", "간식·디저트·음료")),
        ("has_soup", "국물 요리", "has_soup", "eq", True,
         ("soup_yes", "국물 있는 요리"), ("soup_no", "국물 없는 요리")),
        ("cooking_style", "밥·면 요리", "cooking_style", "in", ["rice", "noodle"],
         ("rice_noodle", "밥·면 요리"), ("other_style", "국·구이·튀김 등")),
        ("main_ingredient", "고기 요리", "main_ingredient", "in", ["beef", "pork", "poultry"],
         ("meat", "소·돼지·닭·오리고기"), ("non_meat", "해산물·채소 등")),
        ("flavor_intensity", "매콤한 요리", "flavor_intensity", "eq", "spicy_bold",
         ("spicy_bold", "매콤·얼큰한 맛"), ("not_spicy", "담백·달콤·고소한 맛")),
        ("temperature", "따뜻한 요리", "temperature", "eq", "hot",
         ("hot", "따뜻한 요리"), ("cold", "차가운 요리")),
        ("heaviness", "가벼운 식사", "heaviness", "eq", "light",
         ("light", "가볍게 (700kcal 미만)"), ("filling", "든든하게 (700kcal 이상)")),
        ("protein_level", "고단백 메뉴", "protein_g", "gte", 10.0,
         ("high_protein", "단백질 10g 이상"), ("normal_protein", "단백질 10g 미만")),
        ("carb_level", "저탄수 메뉴", "carb_g", "lte", 15.0,
         ("low_carb", "탄수화물 15g 이하"), ("rich_carb", "탄수화물 15g 초과")),
        ("sodium_level", "저염 메뉴", "sodium_mg", "lte", 250.0,
         ("low_sodium", "나트륨 250mg 이하"), ("savory_sodium", "나트륨 250mg 초과")),
        ("sugar_level", "저당 메뉴", "sugar_g", "lte", 3.0,
         ("low_sugar", "당류 3g 이하"), ("with_sugar", "당류 3g 초과")),
        ("origin_type", "집밥 스타일", "origin_type", "in", ["home_style", "cafeteria"],
         ("home_style", "집밥·급식 스타일"), ("eating_out", "외식·프랜차이즈")),
    ]
    templates = [
        "오늘은 {topic} 어때요?",
        "{topic}, 지금 당기나요?",
        "{topic}, 먹고 싶나요?",
        "{topic}, 오늘 끌리나요?",
        "{topic}, 지금 괜찮나요?",
        "{topic} 쪽으로 고를까요?",
        "{topic} 메뉴가 좋나요?",
        "{topic} 메뉴로 할까요?",
        "{topic}, 이번엔 어때요?",
        "{topic} 메뉴를 원하나요?",
        "{topic}, 입맛에 맞나요?",
        "{topic} 쪽이 더 좋나요?",
        "{topic}, 생각나나요?",
        "{topic}, 한 끼로 어때요?",
        "{topic} 메뉴로 정할까요?",
        "{topic}, 지금 끌리나요?",
        "{topic}, 선택할까요?",
    ]
    opposite_ops = {"eq": "neq", "in": "not_in", "gte": "lt", "lte": "gt"}
    pool = []
    for cluster, topic, field, op, value, positive, negative in specs:
        options = [
            {"option_id": positive[0], "label": positive[1],
             "rules": [{"field": field, "op": op, "value": value}]},
            {"option_id": negative[0], "label": negative[1],
             "rules": [{"field": field, "op": opposite_ops[op], "value": value}]},
        ]
        for idx, template in enumerate(templates, start=1):
            question_text = template.format(topic=topic)
            if not 1 <= len(question_text) <= MAX_QUESTION_TEXT_LENGTH:
                raise ValueError("주문서 질문은 공백 포함 1~24자여야 합니다.")
            pool.append({
                "question_code": f"{QUESTION_CODE_PREFIX}{cluster}_{idx:02d}",
                "cluster_key": cluster,
                "question_text": question_text,
                "options": options,
            })
    return pool

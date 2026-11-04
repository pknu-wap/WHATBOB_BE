import argparse
import csv
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import httpx
import pymysql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import app.users.models  # noqa: F401
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.menus.models import Menu, OrderSheetQuestion
from app.recommend.common import derive_allergens_from_text
from app.recommend.order_sheet import rank_and_score_question_pool

logger = logging.getLogger(__name__)

DEFAULT_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "foods_nutrition.csv"

ORIGIN_PRIORITY = {
    "외식(분석함량)": 1,
    "가정식(분석 함량)": 2,
    "외식(재료량 기반 산출함량)": 3,
    "외식(프랜차이즈 등 업체 제공 영양정보)": 4,
    "산업체급식(재료량 기반 산출 함량)": 5,
    "중고등학교급식(재료량 기반 산출함량)": 6,
    "초등학교급식(재료량 기반 산출 함량)": 7,
}


def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    text = str(val).strip().replace(",", "")
    if not text:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _derive_origin_type(origin_raw: str, company_name: str) -> str:
    if "프랜차이즈" in origin_raw or (company_name and company_name != "해당없음"):
        return "franchise"
    if "가정식" in origin_raw:
        return "home_style"
    if "급식" in origin_raw:
        return "cafeteria"
    return "restaurant"


def _derive_meal_type(cat_large: str) -> str:
    if cat_large in ("음료 및 차류",):
        return "beverage"
    if cat_large in ("빵 및 과자류", "유제품류 및 빙과류", "과일류"):
        return "bakery_snack"
    return "main_meal"


def _derive_has_soup(cat_large: str, name: str) -> bool:
    if cat_large in ("국 및 탕류", "찌개 및 전골류", "죽 및 스프류"):
        return True
    soup_keywords = ("국밥", "해장국", "설렁탕", "곰탕", "갈비탕", "삼계탕", "라면", "칼국수", "짬뽕", "우동", "쌀국수", "전골", "훠궈", "마라탕", "수제비", "냉면", "물냉면")
    if "비빔냉면" in name or "회냉면" in name or "볶음" in name:
        return False
    return any(kw in name for kw in soup_keywords)


def _derive_cooking_style(cat_large: str) -> str:
    mapping = {
        "밥류": "rice",
        "죽 및 스프류": "rice",
        "면 및 만두류": "noodle",
        "국 및 탕류": "soup_stew",
        "찌개 및 전골류": "soup_stew",
        "구이류": "grill_stir",
        "볶음류": "grill_stir",
        "찜류": "steamed_braised",
        "조림류": "steamed_braised",
        "튀김류": "fried_pan",
        "전·적 및 부침류": "fried_pan",
        "생채·무침류": "raw_salad",
        "나물·숙채류": "raw_salad",
        "빵 및 과자류": "bakery",
        "음료 및 차류": "beverage",
        "유제품류 및 빙과류": "dessert",
    }
    return mapping.get(cat_large, "other")


def _derive_main_ingredient(cat_medium: str, cat_small: str, name: str) -> str:
    combined = f"{cat_medium} {cat_small} {name}"
    if any(k in combined for k in ("해물", "참치", "바지락", "오징어", "낙지", "쭈꾸미", "주꾸미", "새우", "홍합", "황태", "동태", "생선", "고등어", "갈치", "조기", "장어", "어묵", "회", "게", "굴", "조개", "전복", "아귀", "코다리", "연어", "홍어", "가오리", "서대", "멸치", "북어", "명태", "꽁치", "삼치", "다슬기", "재첩", "골뱅이", "문어", "꽃게", "대하", "바닷가재")):
        return "seafood"
    if any(k in combined for k in ("소고기", "한우", "차돌", "불고기", "갈비", "육회", "스테이크", "설렁탕", "곰탕", "도가니", "꼬리", "육개장", "선지", "곱창")):
        return "beef"
    if any(k in combined for k in ("돼지", "삼겹", "제육", "돈까스", "돈가스", "족발", "보쌈", "탕수육", "햄", "베이컨", "소시지", "페퍼로니", "순대", "감자탕", "뼈해장국", "부대찌개", "폭찹", "수육")):
        return "pork"
    if any(k in combined.replace("가오리", "") for k in ("닭", "치킨", "오리", "삼계", "백숙", "칠면조", "윙", "봉")):
        return "poultry"
    if any(k in combined for k in ("채소", "두부", "콩나물", "버섯", "호박", "무", "감자", "나물", "비빔", "시금치", "미역", "부추", "오이", "당근", "연근", "우엉", "가지", "팥", "흑임자", "현미", "흑미", "고구마", "옥수수", "양배추", "브로콜리", "토마토", "샐러드")):
        return "veggie_tofu"
    if any(k in combined for k in ("치즈", "달걀", "계란", "메추리알", "우유", "크림", "요거트")):
        return "egg_dairy"
    return "other"


def _derive_flavor_intensity(cat_medium: str, name: str, sodium_mg: Optional[float], sugar_g: Optional[float]) -> str:
    combined = f"{cat_medium} {name}"
    spicy_keywords = ("고추장", "고춧가루", "김치", "배추김치", "매운", "짬뽕", "훠궈", "마라", "탄탄면", "쫄면", "떡볶이", "불닭", "육개장", "해물찜", "아귀찜", "양념", "비빔", "회무침", "제육", "닭발", "쭈꾸미")
    if any(k in combined for k in spicy_keywords) or (sodium_mg is not None and sodium_mg >= 450):
        return "spicy_bold"
    sweet_keywords = ("치즈", "크림", "데리야끼", "탕수", "강정", "하이라이스", "돈까스", "초코", "카라멜", "바닐라", "딸기", "허니", "고구마", "콘치즈")
    if any(k in combined for k in sweet_keywords) or (sugar_g is not None and sugar_g >= 8.0):
        return "sweet_savory"
    return "mild_clean"


def _derive_temperature(cat_large: str, name: str) -> str:
    cold_keywords = ("아이스", "ICED", "냉면", "물냉면", "비빔냉면", "회냉면", "콩국수", "쫄면", "회무침", "회덮밥", "물회", "샐러드", "빙수", "아이스크림", "식혜", "냉국")
    if any(k in name for k in cold_keywords) or cat_large in ("생채·무침류", "유제품류 및 빙과류"):
        return "cold"
    if cat_large in ("빵 및 과자류", "김치류", "장아찌·절임류", "젓갈류"):
        return "neutral"
    return "hot"


def _estimate_meal_kcal(
    kcal_100g: Optional[float],
    fat_100g: Optional[float],
    weight_str: str,
    cat_large: str,
) -> float:
    """
    식약처 CSV의 100g(ml)당 열량과 1인분 중량(식품중량), 조리 분류를 바탕으로
    실제 한 끼 식사(1인분 정식/단품, 일반 700~1100kcal 중심) 기준 열량(kcal)을 산출합니다.
    """
    k100 = kcal_100g if kcal_100g is not None else 130.0
    f100 = fat_100g if fat_100g is not None else 3.0
    match = re.search(r"([0-9.]+)", weight_str or "")
    serving_w = float(match.group(1)) if match else 400.0

    if cat_large in ("음료 및 차류",):
        eff_w = max(250.0, min(550.0, serving_w))
        return round(k100 * (eff_w / 100.0), 1)
    if cat_large in ("빵 및 과자류", "유제품류 및 빙과류", "과일류"):
        eff_w = max(150.0, min(350.0, serving_w))
        return round(k100 * (eff_w / 100.0), 1)
    if cat_large in ("밥류", "면 및 만두류"):
        eff_w = max(500.0, min(720.0, serving_w))
        base = k100 * (eff_w / 100.0) + 120.0
    elif cat_large in ("죽 및 스프류", "생채·무침류", "나물·숙채류", "김치류", "장아찌·절임류"):
        eff_w = max(350.0, min(500.0, serving_w))
        base = k100 * (eff_w / 100.0) + 260.0
    elif cat_large in ("국 및 탕류", "찌개 및 전골류"):
        eff_w = max(450.0, min(650.0, serving_w))
        base = k100 * (eff_w / 100.0) + 450.0
    else:
        # 구이, 볶음, 튀김, 찜, 전 등 메인요리 + 공기밥 및 기본 반찬(420kcal)
        eff_w = max(280.0, min(450.0, serving_w))
        base = k100 * (eff_w / 100.0) + 420.0 + (f100 * 4.0)

    return round(max(380.0, min(1650.0, base)), 1)


def _derive_heaviness(meal_kcal: Optional[float], fat_g: Optional[float], cat_large: str) -> str:
    """
    한 끼 식사 열량(일반 700~1100kcal 기준)으로 칼로리 무게감을 분류합니다.
    - light: 700kcal 미만 (가벼운 다이어트·저열량 한 끼)
    - moderate: 700 ~ 950kcal 미만 (일반적인 든든한 한 끼)
    - heavy: 950kcal 이상 또는 고지방 튀김/구이류 (묵직한 고칼로리·치팅데이 한 끼)
    """
    if meal_kcal is not None and meal_kcal >= 950.0:
        return "heavy"
    if cat_large in ("튀김류", "구이류") and (fat_g is not None and fat_g >= 14.0):
        return "heavy"
    if meal_kcal is not None and meal_kcal < 700.0:
        return "light"
    return "moderate"


def _derive_allergens(cat_medium: str, cat_small: str, name: str, cat_large: str) -> list[str]:
    combined = f"{cat_large} {cat_medium} {cat_small} {name}"
    return derive_allergens_from_text(combined)


def parse_and_clean_csv(
    csv_path: Path = DEFAULT_CSV_PATH,
    max_menus: Optional[int] = None,
    include_beverages_and_bakery: bool = True,
) -> list[dict[str, Any]]:
    """
    식약처 영양성분 CSV 파일을 읽어 급식/중복 데이터를 대표 메뉴 기준으로 정제하고,
    주문서 조건식(rules) 매칭에 필요한 12개 정규화 속성을 자동 추출합니다.
    """
    with open(csv_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        best_by_name: dict[str, tuple[int, dict[str, Any]]] = {}

        for row in reader:
            food_code = (row.get("식품코드") or "").strip()
            name = (row.get("식품명") or "").strip()
            cat_large = (row.get("식품대분류명") or "").strip()
            if not food_code or not name or not cat_large:
                continue

            if not include_beverages_and_bakery and cat_large in ("음료 및 차류", "빵 및 과자류", "유제품류 및 빙과류"):
                continue

            origin_raw = (row.get("식품기원명") or "").strip()
            priority = ORIGIN_PRIORITY.get(origin_raw, 9)

            existing = best_by_name.get(name)
            if existing is None or priority < existing[0]:
                best_by_name[name] = (priority, row)

    parsed_menus: list[dict[str, Any]] = []
    for name, (_, row) in best_by_name.items():
        food_code = row["식품코드"].strip()
        cat_large = row["식품대분류명"].strip()
        cat_medium = (row.get("식품중분류명") or "").strip()
        cat_small = (row.get("식품소분류명") or "").strip()
        origin_raw = (row.get("식품기원명") or "").strip()
        company = (row.get("업체명") or "").strip()

        base_name = name.split("_")[0].strip()
        kcal_100g = _to_float(row.get("에너지(kcal)"))
        protein_g = _to_float(row.get("단백질(g)"))
        fat_g = _to_float(row.get("지방(g)"))
        carb_g = _to_float(row.get("탄수화물(g)"))
        sugar_g = _to_float(row.get("당류(g)"))
        sodium_mg = _to_float(row.get("나트륨(mg)"))
        weight_str = (row.get("식품중량") or "").strip()
        meal_kcal = _estimate_meal_kcal(kcal_100g, fat_g, weight_str, cat_large)

        parsed_menus.append(
            {
                "food_code": food_code,
                "name": name,
                "base_name": base_name,
                "category_large": cat_large,
                "category_medium": None if cat_medium == "해당없음" else cat_medium,
                "category_small": None if cat_small == "해당없음" else cat_small,
                "origin_type": _derive_origin_type(origin_raw, company),
                "meal_type": _derive_meal_type(cat_large),
                "has_soup": _derive_has_soup(cat_large, name),
                "cooking_style": _derive_cooking_style(cat_large),
                "main_ingredient": _derive_main_ingredient(cat_medium, cat_small, name),
                "flavor_intensity": _derive_flavor_intensity(cat_medium, name, sodium_mg, sugar_g),
                "temperature": _derive_temperature(cat_large, name),
                "heaviness": _derive_heaviness(meal_kcal, fat_g, cat_large),
                "kcal": meal_kcal,
                "protein_g": protein_g,
                "fat_g": fat_g,
                "carb_g": carb_g,
                "sugar_g": sugar_g,
                "sodium_mg": sodium_mg,
                "allergen_tags": _derive_allergens(cat_medium, cat_small, name, cat_large),
            }
        )

    parsed_menus.sort(key=lambda m: (0 if m["meal_type"] == "main_meal" else 1, m["food_code"]))
    if max_menus is not None:
        return parsed_menus[:max_menus]
    return parsed_menus


def build_candidate_question_pool() -> list[dict[str, Any]]:
    """
    [2단계 질문 후보 풀 대량 생성 (200+ 질문)]
    12개의 핵심 의미 클러스터(cluster_key)와 상황/컨텍스트 변형을 조합하여
    각 선택지가 CSV 컬럼 조건식(rules)과 100% 매칭되는 200개 이상의 질문 후보 풀을 생성합니다.
    """
    cluster_Specs: list[dict[str, Any]] = [
        {
            "cluster_key": "meal_type",
            "questions": [
                "오늘 찾으시는 메뉴는 든든한 정규 식사인가요, 가벼운 간식·디저트인가요?",
                "지금 출출한 정도가 한 끼 식사 수준인가요, 아니면 베이커리·디저트 정도인가요?",
                "식사 메뉴를 고르고 계신가요, 카페 음료나 빵류를 찾으시나요?",
                "제대로 된 밥/면/요리 한 끼가 좋으신가요, 가볍게 곁들일 간식이 좋으신가요?",
                "오늘의 주문 목적은 식사인가요, 티타임·디저트인가요?",
                "배를 든든히 채울 정식 메뉴와 간단한 스낵 중 어느 쪽이 끌리시나요?",
                "점심·저녁 식사용 메뉴를 원하시나요, 아니면 빵·과자·음료가 당기시나요?",
                "본격적인 식사 자리인가요, 가볍게 즐길 간식 시간인가요?",
                "든든하게 배 채우는 요리 vs 달콤·가벼운 베이커리 중 골라주세요!",
                "오늘 입맛은 정식 한 끼 쪽인가요, 간편한 빵·음료 쪽인가요?",
                "식탁에서 먹는 든든한 요리와 가벼운 디저트 중 무엇을 추천해 드릴까요?",
                "허기를 확실히 달랠 식사 메뉴를 원하시나요?",
                "지금 당장 필요한 건 든든한 밥상인가요, 카페 메뉴인가요?",
                "한 끼 식사용 메뉴만 볼까요, 간식과 음료까지 함께 볼까요?",
                "오늘 메뉴 고민은 식사인가요, 디저트인가요?",
                "출출함을 달랠 메인 요리와 가벼운 주전부리 중 선택해 주세요.",
                "제대로 차려 먹는 식사와 간편한 베이커리 중 어느 쪽이 좋으세요?",
                "지금 시간대에 어울리는 든든한 식사 vs 가벼운 간식!",
            ],
            "options": [
                {"option_id": "main_meal", "label": "든든한 한 끼 정규 식사", "rules": [{"field": "meal_type", "op": "eq", "value": "main_meal"}]},
                {"option_id": "bakery_snack", "label": "가벼운 빵·과자·디저트", "rules": [{"field": "meal_type", "op": "eq", "value": "bakery_snack"}]},
                {"option_id": "beverage", "label": "시원하거나 따뜻한 음료·차", "rules": [{"field": "meal_type", "op": "eq", "value": "beverage"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "has_soup",
            "questions": [
                "숟가락으로 떠먹을 수 있는 뜨끈한 국물 요리가 당기시나요?",
                "오늘 식사에 국물이 꼭 있어야 할까요, 없어도 괜찮을까요?",
                "속을 확 풀어줄 국물 있는 음식 vs 깔끔하게 집어 먹는 국물 없는 음식!",
                "국물 자작한 탕·찌개·면 요리가 좋으세요, 볶음·구이처럼 국물 없는 요리가 좋으세요?",
                "어제 피로나 숙취를 풀어줄 해장용 국물이 필요하신가요?",
                "비 오거나 선선한 날 생각나는 진한 국물 요리가 끌리시나요?",
                "밥 말아 먹기 좋은 국물 메뉴를 선호하시나요?",
                "깔끔하게 딱 떨어지는 국물 없는 요리와 넉넉한 국물 요리 중 골라주세요.",
                "국물이 있는 든든한 메뉴가 좋으신가요, 뻑뻑하지 않은 단품 요리가 좋으신가요?",
                "오늘 컨디션에는 국물 있는 요리와 없는 요리 중 어느 쪽이 맞으세요?",
                "뜨거운 국물을 후루룩 마시고 싶으신가요?",
                "국물 유무 중 오늘 절대 포기할 수 없는 쪽은 무엇인가요?",
                "시원·칼칼한 국물 요리 vs 바삭·담백한 비국물 요리!",
                "국물 있는 메뉴로 속을 데우고 싶으신가요?",
                "오늘 식탁에 국물이 있는 편이 좋으실까요?",
                "국물 요리(탕·찌개·국밥)와 비국물 요리(구이·덮밥·볶음) 중 선택해 주세요.",
                "속이 든든해지는 국물 요리가 생각나시나요?",
                "국물 없이 깔끔하게 먹는 메뉴를 더 선호하시나요?",
            ],
            "options": [
                {"option_id": "soup_yes", "label": "네, 국물이 있는 요리가 좋아요", "rules": [{"field": "has_soup", "op": "eq", "value": True}]},
                {"option_id": "soup_no", "label": "아니요, 국물 없는 요리가 좋아요", "rules": [{"field": "has_soup", "op": "eq", "value": False}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "cooking_style",
            "questions": [
                "오늘 가장 끌리는 조리 방식이나 주식 형태는 무엇인가요?",
                "밥, 면, 국·찌개, 구이·볶음, 튀김 중 지금 가장 입에 당기는 스타일은?",
                "한국인은 밥심! 밥류가 좋으세요, 호로록 면류가 좋으세요, 아니면 고기·요리류가 좋으세요?",
                "지글지글 구이·볶음, 보글보글 찌개·탕, 바삭한 튀김·전 중 골라주세요!",
                "오늘 식사의 주인공은 밥인가요, 면인가요, 아니면 일품 요리인가요?",
                "어떤 조리법으로 만든 음식이 가장 먹고 싶으신가요?",
                "덮밥·국밥 같은 밥류 vs 칼국수·파스타 같은 면류 vs 구이·볶음 요리!",
                "바삭하게 튀기거나 부친 요리가 끌리세요, 아니면 든든한 밥·찌개가 끌리세요?",
                "식감이 살아있는 구이·볶음과 부드러운 국·탕·찜 중 어느 쪽이 좋으세요?",
                "오늘 점심/저녁 메뉴 카테고리로 가장 끌리는 조리 스타일은?",
                "면치기가 당기는 날인가요, 든든한 밥 한 공기가 당기는 날인가요?",
                "불향 가득한 구이·볶음 요리와 깊은 맛의 탕·찌개 중 선택해 주세요.",
                "입안에서 바삭거리는 튀김·전류는 어떠신가요?",
                "가볍고 신선한 무침·생채 스타일과 든든한 조리 요리 중 골라주세요.",
                "오늘 당신의 미각을 깨울 조리 스타일은 무엇인가요?",
                "밥·죽류, 면·만두류, 구이·볶음류, 탕·찌개류 중 1순위는?",
                "한 그릇 뚝딱 먹는 덮밥/면 요리와 메인 디쉬(구이/튀김) 중 어느 쪽이 좋으세요?",
                "지금 머릿속에 떠오르는 음식의 조리 형태를 골라주세요!",
            ],
            "options": [
                {"option_id": "rice", "label": "든든한 밥·죽류 (덮밥·비빔밥·죽)", "rules": [{"field": "cooking_style", "op": "eq", "value": "rice"}]},
                {"option_id": "noodle", "label": "호로록 면·만두류 (국수·파스타·냉면)", "rules": [{"field": "cooking_style", "op": "eq", "value": "noodle"}]},
                {"option_id": "soup_stew", "label": "보글보글 국·탕·찌개·전골류", "rules": [{"field": "cooking_style", "op": "eq", "value": "soup_stew"}]},
                {"option_id": "grill_stir", "label": "노릇노릇 구이·볶음·찜·조림류", "rules": [{"field": "cooking_style", "op": "in", "value": ["grill_stir", "steamed_braised"]}]},
                {"option_id": "fried_pan", "label": "바삭바삭 튀김·전·부침류", "rules": [{"field": "cooking_style", "op": "eq", "value": "fried_pan"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "main_ingredient",
            "questions": [
                "메인 재료로 어떤 것이 들어간 음식을 가장 드시고 싶나요?",
                "소고기, 돼지고기, 닭·오리, 해산물, 채소·두부 중 오늘의 주재료는?",
                "육즙 가득한 고기 요리가 좋으세요, 바다 향 가득한 해산물 요리가 좋으세요?",
                "오늘 식탁의 주인공 재료를 하나만 고른다면 무엇인가요?",
                "담백한 닭·오리고기 vs 고소한 돼지·소고기 vs 시원한 해물 중 끌리는 것은?",
                "속이 편안한 채소·두부·버섯 위주 식단과 육류/해산물 중 골라주세요!",
                "오늘 몸이 원하는 핵심 재료는 육류인가요, 해물인가요, 채소인가요?",
                "쫄깃한 해물·생선 요리와 든든한 고기 요리 중 어느 쪽이 당기세요?",
                "소고기 요리, 돼지고기 요리, 닭 요리, 해산물 요리 중 가장 먹고 싶은 것은?",
                "비건·채소·두부 중심의 깔끔한 재료를 원하시나요, 고기·해물을 원하시나요?",
                "오늘 메뉴에서 가장 중요하게 생각하는 주재료를 선택해 주세요.",
                "고기가 듬뿍 들어간 메뉴가 좋으신가요, 신선한 해산물이 들어간 메뉴가 좋으신가요?",
                "돼지고기(제육·돈까스·삼겹) vs 소고기(불고기·갈비·전골) vs 해물!",
                "가벼운 채소·두부 요리부터 묵직한 육류까지, 오늘의 원픽 재료는?",
                "입맛을 돋워줄 메인 식재료를 골라주세요.",
                "육류파, 해산물파, 채소·두부파 중 오늘은 어느 쪽이신가요?",
                "닭·오리 같은 가금류 요리와 소·돼지 적색육 요리 중 선호하시는 쪽은?",
                "오늘 메뉴 선정의 핵심 식재료 기준을 알려주세요!",
            ],
            "options": [
                {"option_id": "beef", "label": "풍미 깊은 소고기", "rules": [{"field": "main_ingredient", "op": "eq", "value": "beef"}]},
                {"option_id": "pork", "label": "고소하고 든든한 돼지고기·햄", "rules": [{"field": "main_ingredient", "op": "eq", "value": "pork"}]},
                {"option_id": "poultry", "label": "담백한 닭고기·오리고기", "rules": [{"field": "main_ingredient", "op": "eq", "value": "poultry"}]},
                {"option_id": "seafood", "label": "신선한 해산물·생선·조개", "rules": [{"field": "main_ingredient", "op": "eq", "value": "seafood"}]},
                {"option_id": "veggie_tofu", "label": "속 편한 채소·두부·버섯·나물", "rules": [{"field": "main_ingredient", "op": "eq", "value": "veggie_tofu"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "flavor_intensity",
            "questions": [
                "스트레스 풀리는 매콤·얼큰한 맛과 속 편한 담백한 맛 중 어느 쪽이 끌리세요?",
                "오늘 입맛은 칼칼하고 자극적인 쪽인가요, 깔끔하고 순한 쪽인가요?",
                "매콤한 고추장·김치·마라 베이스 vs 담백한 간장·소금·된장 베이스 vs 달콤·고소한 치즈·크림!",
                "자극 없이 순하고 슴슴한 음식이 좋으세요, 입맛 확 도는 매콤한 음식이 좋으세요?",
                "달콤짭짤하거나 치즈처럼 고소한 풍미를 원하시나요?",
                "스트레스를 날려줄 매운맛이 필요한 날인가요?",
                "위장에 부담 없는 순하고 담백한 맛을 찾으시나요?",
                "얼큰·매콤 vs 담백·깔끔 vs 달콤·고소 중 오늘의 맛 방향성은?",
                "혀끝이 짜릿한 매콤함과 재료 본연의 담백함 중 골라주세요!",
                "아이들도 먹기 좋은 달콤·고소한 맛과 어른들의 얼큰한 맛 중 어느 쪽인가요?",
                "양념이 진하고 매콤한 요리 vs 국물이나 간이 맑고 깔끔한 요리!",
                "오늘 컨디션에 맞는 맛의 강도(자극도)를 선택해 주세요.",
                "매운 음식을 잘 드시나요, 아니면 안 매운 음식을 원하시나요?",
                "칼칼한 감칠맛이 당기시나요, 부드럽고 고소한 맛이 당기시나요?",
                "속이 쓰리지 않은 순한 맛과 땀이 송골송골 맺히는 매운맛 중 선택해 주세요.",
                "고추장/김치의 칼칼함, 소금/육수의 담백함, 치즈/소스의 달콤고소함 중 1순위는?",
                "자극적인 맛으로 기분 전환하고 싶으신가요?",
                "오늘 식사에서 가장 원하는 맛의 톤(Tone)을 알려주세요!",
            ],
            "options": [
                {"option_id": "spicy_bold", "label": "스트레스 풀리는 매콤·얼큰·자극적인 맛", "rules": [{"field": "flavor_intensity", "op": "eq", "value": "spicy_bold"}]},
                {"option_id": "mild_clean", "label": "속 편하고 깔끔한 담백·순한 맛", "rules": [{"field": "flavor_intensity", "op": "eq", "value": "mild_clean"}]},
                {"option_id": "sweet_savory", "label": "달콤짭짤하거나 치즈·크림처럼 고소한 맛", "rules": [{"field": "flavor_intensity", "op": "eq", "value": "sweet_savory"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "temperature",
            "questions": [
                "김이 모락모락 나는 따뜻한 요리가 좋으세요, 가슴까지 시원해지는 차가운 요리가 좋으세요?",
                "오늘 날씨나 기분에는 온기 있는 음식과 시원한 냉기 음식 중 어느 쪽이 어울리나요?",
                "몸을 따뜻하게 데워줄 핫(HOT) 메뉴 vs 더위를 식혀줄 아이스·냉(COLD) 메뉴!",
                "시원한 냉면·회덮밥·샐러드 스타일과 따끈하게 조리한 음식 중 골라주세요.",
                "갓 조리해 뜨거운 음식이 당기시나요, 시원하고 상큼한 음식이 당기시나요?",
                "음식의 온도감은 따뜻한 쪽과 차가운 쪽 중 어디가 좋으세요?",
                "이열치열 뜨거운 요리 vs 속까지 시원한 냉요리!",
                "따뜻한 온기가 있는 식사와 차갑게 즐기는 별미 중 선택해 주세요.",
                "오늘 체온과 컨디션에 맞는 음식 온도는 무엇인가요?",
                "호호 불어 먹는 뜨거운 음식 vs 아삭하고 시원한 음식!",
                "따뜻하게 데워 나오는 메뉴를 선호하시나요?",
                "시원하게 입맛을 돋우는 차가운 메뉴가 생각나시나요?",
                "온도 기준(따뜻함 vs 시원함)으로 오늘 메뉴를 좁혀볼까요?",
                "따끈따끈한 정식 요리와 차갑고 상쾌한 메뉴 중 골라주세요.",
                "지금 당장 먹고 싶은 음식의 온도는 어느 쪽인가요?",
                "몸이 으슬으슬할 때 좋은 따뜻한 요리 vs 답답할 때 좋은 시원한 요리!",
                "열기가 느껴지는 조리 음식과 차가운 생채·냉류 중 선호하는 쪽은?",
                "음식 온도(온기/냉기)에 대한 선호를 알려주세요!",
            ],
            "options": [
                {"option_id": "hot", "label": "김이 모락모락 나는 따뜻한 요리", "rules": [{"field": "temperature", "op": "eq", "value": "hot"}]},
                {"option_id": "cold", "label": "속까지 시원하고 상쾌한 차가운 요리", "rules": [{"field": "temperature", "op": "eq", "value": "cold"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "heaviness",
            "questions": [
                "식사 후 포만감이나 칼로리 부담은 어느 정도가 적당할까요?",
                "다이어트·식단 관리 중이라 가벼운 저칼로리 메뉴를 찾으시나요, 아니면 치팅데이처럼 묵직한 메뉴를 원하시나요?",
                "속이 부대끼지 않는 가벼운 식사 vs 적당히 든든한 일반 식사 vs 기름지고 헤비한 고칼로리!",
                "오늘 칼로리 걱정 없이 헤비하게 즐기고 싶으신가요, 가볍고 클린하게 드시고 싶으신가요?",
                "100g당 열량과 지방 함량 기준으로 가벼운 음식과 묵직한 음식 중 골라주세요.",
                "식사 후 졸리지 않게 가벼운 메뉴가 좋으세요, 배가 빵빵해지는 헤비한 메뉴가 좋으세요?",
                "오늘 운동이나 활동량에 맞춰 식사 무게감을 골라볼까요?",
                "저열량·저지방 라이트 메뉴 vs 고열량·고풍미 헤비 메뉴!",
                "야식이나 늦은 저녁이라 부담 없는 칼로리를 원하시나요?",
                "제대로 기름칠하는 고칼로리 요리와 산뜻한 저칼로리 요리 중 선택해 주세요.",
                "체중 관리 중이신가요, 아니면 든든한 에너지 보충이 필요하신가요?",
                "소화가 잘되는 가벼운 음식과 포만감이 오래가는 묵직한 음식 중 어느 쪽이 좋으세요?",
                "오늘 식사의 무게감(Light / Moderate / Heavy)을 정해주세요!",
                "가볍게 비우는 식단 vs 적당한 한 끼 vs 제대로 달리는 고칼로리!",
                "기름기 적은 깔끔한 음식과 고소하고 기름진 음식 중 끌리는 쪽은?",
                "한 끼 칼로리 기준(700kcal 미만 가벼운 식사 vs 700~950kcal 일반 식사 vs 950kcal 이상 헤비 식사) 선호도는?",
                "부담 없이 가벼운 한 끼를 원하시나요, 묵직한 만족감을 원하시나요?",
                "오늘 내 몸이 원하는 칼로리 밸런스를 선택해 주세요.",
            ],
            "options": [
                {"option_id": "light", "label": "가볍고 부담 없는 저칼로리 한 끼 (700kcal 미만)", "rules": [{"field": "heaviness", "op": "eq", "value": "light"}]},
                {"option_id": "moderate", "label": "적당히 든든한 일반 한 끼 식사 (700~950kcal)", "rules": [{"field": "heaviness", "op": "eq", "value": "moderate"}]},
                {"option_id": "heavy", "label": "묵직하고 기름진 고칼로리·치팅데이 한 끼 (950~1,100kcal 이상)", "rules": [{"field": "heaviness", "op": "eq", "value": "heavy"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "protein_level",
            "questions": [
                "운동 전후나 근손실 방지를 위해 고단백 메뉴를 찾고 계신가요?",
                "단백질 함량이 높은(100g당 10g 이상) 든든한 보양·단백질 식단이 필요하신가요?",
                "오늘 식사에서 단백질 보충이 얼마나 중요하신가요?",
                "헬스·운동 후 챙겨 먹기 좋은 고단백 메뉴를 우선 추천해 드릴까요?",
                "단백질이 풍부한 고기·생선·콩 위주 식단을 원하시나요?",
                "영양 성분 중 단백질 비중이 높은 메뉴를 선호하시나요?",
                "고단백(High-Protein) 식단 여부로 후보를 추려볼까요?",
                "근육 회복과 포만감을 위한 고단백 음식이 당기시나요?",
                "일반 단백질 함량 메뉴와 고단백 집중 메뉴 중 골라주세요!",
                "오늘 단백질 섭취 목표치를 채워줄 메뉴를 찾으시나요?",
                "단백질 함량(10g/100g 이상)을 중요하게 보시나요?",
                "체력 보충을 위해 단백질이 꽉 찬 요리를 추천해 드릴까요?",
                "고단백 식단 vs 가벼운 일반 식단 중 어느 쪽이 좋으세요?",
                "단백질 위주의 든든한 구성을 원하시나요?",
                "영양학적으로 단백질이 풍부한 메뉴를 우선순위에 둘까요?",
                "운동하는 날 먹기 좋은 고단백 메뉴를 찾으시나요?",
                "단백질 함량 기준으로 메뉴를 필터링해 드릴까요?",
                "고단백 영양 포인트가 오늘 메뉴 선택에 중요한가요?",
            ],
            "options": [
                {"option_id": "high_protein", "label": "네, 단백질이 풍부한 고단백 메뉴(10g 이상)가 좋아요", "rules": [{"field": "protein_g", "op": "gte", "value": 10.0}]},
                {"option_id": "normal_protein", "label": "일반적인 단백질 함량(10g 미만)이면 충분해요", "rules": [{"field": "protein_g", "op": "lt", "value": 10.0}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "carb_level",
            "questions": [
                "저탄수화물(키토·당질 제한) 식단 중이신가요, 아니면 탄수화물이 든든하게 들어가야 하나요?",
                "탄수화물 함량이 낮은 요리 위주가 좋으세요, 밥·면처럼 탄수화물이 풍부한 메뉴가 좋으세요?",
                "오늘 식단에서 탄수화물 비중은 어떻게 맞추고 싶으신가요?",
                "혈당 관리나 저탄수 식단을 위해 탄수화물(15g 이하)을 줄인 메뉴를 찾으시나요?",
                "뇌 회전과 에너지를 위해 탄수화물 충전이 필요하신가요?",
                "저탄수 요리(고기·해물·채소 위주) vs 탄수화물 든든 메뉴(밥·면·빵)!",
                "탄수화물 함량을 낮춘 깔끔한 메뉴를 선호하시나요?",
                "든든한 탄수화물 한 끼와 저탄수 식단 중 골라주세요.",
                "당질·탄수화물 제한 식단을 실천 중이신가요?",
                "오늘 몸이 원하는 건 탄수화물 충전인가요, 저탄수 클린 식단인가요?",
                "탄수화물 수치(100g당 15g 기준)로 후보를 나눠볼까요?",
                "밥이나 면 없이도 든든한 저탄수 요리를 원하시나요?",
                "탄수화물이 넉넉한 메뉴로 에너지를 빠르게 채우고 싶으신가요?",
                "식후 혈당 스파이크를 피할 저탄수 메뉴가 좋으신가요?",
                "탄수화물 함량이 높은 메뉴와 낮은 메뉴 중 어느 쪽이 끌리세요?",
                "오늘 탄수화물 섭취량 기준을 선택해 주세요!",
                "저탄수 고단백 스타일 vs 든든한 탄수화물 중심 스타일!",
                "탄수화물 비중에 대한 선호를 알려주세요.",
            ],
            "options": [
                {"option_id": "low_carb", "label": "탄수화물 부담 적은 저탄수 메뉴 (15g 이하)", "rules": [{"field": "carb_g", "op": "lte", "value": 15.0}]},
                {"option_id": "rich_carb", "label": "탄수화물이 든든하게 들어간 메뉴 (15g 초과)", "rules": [{"field": "carb_g", "op": "gt", "value": 15.0}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "sodium_level",
            "questions": [
                "붓기 관리나 건강을 위해 저염·슴슴한 음식을 찾으시나요, 간이 딱 맞는 짭짤한 음식이 좋으세요?",
                "나트륨 함량이 낮은 순한 간(250mg 이하)과 감칠맛 나는 짭조름한 간 중 골라주세요!",
                "내일 아침 붓기가 걱정되어 나트륨이 적은 메뉴를 원하시나요?",
                "간이 슴슴하고 건강한 음식 vs 밥도둑처럼 짭짤하고 진한 양념 음식!",
                "나트륨 수치 기준으로 저염식과 일반/고염식 중 어느 쪽을 선호하시나요?",
                "짜지 않고 담백한 저나트륨 메뉴를 우선 추천해 드릴까요?",
                "입맛 없을 때 확 당기는 짭조름한 감칠맛 요리가 좋으신가요?",
                "오늘 식사에서 나트륨(염분) 조절이 필요하신가요?",
                "속이 편안한 저염 스타일과 간이 배어 있는 외식 스타일 중 선택해 주세요.",
                "나트륨 250mg 이하의 착한 메뉴들을 모아볼까요?",
                "간이 세지 않은 음식을 선호하시나요, 진한 간을 선호하시나요?",
                "염분 부담 없는 식단과 짭짤한 별미 중 오늘의 선택은?",
                "나트륨 함량에 민감하신 편인가요?",
                "슴슴하고 깔끔한 맛 vs 짭조름하고 진한 맛!",
                "건강 관리용 저염 메뉴를 찾고 계신가요?",
                "오늘 음식의 간 세기(저염 vs 짭짤함)를 골라주세요.",
                "짜지 않은 음식과 간이 확실한 음식 중 어느 쪽이 끌리시나요?",
                "나트륨 기준 선호도를 선택해 주세요!",
            ],
            "options": [
                {"option_id": "low_sodium", "label": "붓기 걱정 없는 저염·슴슴한 메뉴 (나트륨 250mg 이하)", "rules": [{"field": "sodium_mg", "op": "lte", "value": 250.0}]},
                {"option_id": "savory_sodium", "label": "감칠맛 도는 짭조름·진한 간 메뉴 (나트륨 250mg 초과)", "rules": [{"field": "sodium_mg", "op": "gt", "value": 250.0}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "sugar_level",
            "questions": [
                "당류(설탕·단맛)가 거의 없는 로우슈거(Low-Sugar) 메뉴를 원하시나요?",
                "혈당과 당류를 낮춘 무당·저당 메뉴(당류 3g 이하)가 좋으신가요?",
                "단맛이 없는 깔끔한 식사와 적당히 달콤한 양념·디저트 중 골라주세요!",
                "제로슈거·저당 식단을 위해 당류 함량이 낮은 음식만 추려볼까요?",
                "오늘 당분이 당기는 날인가요, 당류를 피하고 싶은 날인가요?",
                "당류 3g 이하의 저당 클린 메뉴를 선호하시나요?",
                "달콤한 소스나 당류가 들어간 메뉴도 괜찮으신가요?",
                "당류 함량(3g 기준)으로 후보를 구분해 드릴까요?",
                "단맛 없이 담백한 요리 vs 은은하거나 달콤한 당류 포함 메뉴!",
                "식단에서 당류(Sugar) 섭취를 제한하고 계신가요?",
                "저당(Low-Sugar) 기준을 오늘 추천에 적용할까요?",
                "기분 전환용 달콤한 맛과 깔끔한 무당·저당 맛 중 선택해 주세요.",
            ],
            "options": [
                {"option_id": "low_sugar", "label": "당류 부담 없는 저당 메뉴 (당류 3g 이하)", "rules": [{"field": "sugar_g", "op": "lte", "value": 3.0}]},
                {"option_id": "with_sugar", "label": "달콤한 풍미가 있는 메뉴 (당류 3g 초과)", "rules": [{"field": "sugar_g", "op": "gt", "value": 3.0}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
        {
            "cluster_key": "origin_type",
            "questions": [
                "오늘 찾으시는 건 정겨운 집밥·백반 스타일인가요, 외식 전문점 요리인가요, 아니면 프랜차이즈 메뉴인가요?",
                "엄마 손맛 같은 가정식 스타일 vs 식당 외식 메뉴 vs 익숙한 브랜드 프랜차이즈!",
                "깔끔한 집밥 느낌의 메뉴가 끌리세요, 밖에서 사 먹는 외식·프랜차이즈 메뉴가 끌리세요?",
                "프랜차이즈 브랜드 메뉴와 일반 식당/가정식 요리 중 어느 쪽을 원하시나요?",
                "백반집·집밥 감성의 편안한 음식 vs 외식 일품 요리!",
                "오늘 식사 분위기에 어울리는 메뉴 출처 스타일을 골라주세요.",
                "익숙한 프랜차이즈 메뉴가 좋으세요, 정통 외식·집밥 요리가 좋으세요?",
                "가정식 백반 스타일과 전문 식당 외식 메뉴 중 선택해 주세요.",
                "집에서 해 먹는 듯한 편안한 요리와 외식 전용 별미 중 끌리는 쪽은?",
                "식당 외식 요리, 가정식 요리, 프랜차이즈 메뉴 중 오늘의 선호는?",
                "부담 없는 집밥·급식 백반 스타일 vs 화려한 외식·브랜드 메뉴!",
                "메뉴의 스타일(가정식 / 일반 외식 / 프랜차이즈)을 선택해 주세요.",
            ],
            "options": [
                {"option_id": "restaurant", "label": "전문 식당에서 즐기는 일반 외식 요리", "rules": [{"field": "origin_type", "op": "eq", "value": "restaurant"}]},
                {"option_id": "home_style", "label": "편안하고 정겨운 집밥·백반 스타일", "rules": [{"field": "origin_type", "op": "in", "value": ["home_style", "cafeteria"]}]},
                {"option_id": "franchise", "label": "익숙한 프랜차이즈·브랜드 메뉴", "rules": [{"field": "origin_type", "op": "eq", "value": "franchise"}]},
                {"option_id": "any", "label": "상관없어요", "rules": []},
            ],
        },
    ]

    pool: list[dict[str, Any]] = []
    for spec in cluster_Specs:
        cluster_key = spec["cluster_key"]
        options = spec["options"]
        for idx, q_text in enumerate(spec["questions"], start=1):
            pool.append(
                {
                    "question_code": f"q_{cluster_key}_{idx:02d}",
                    "cluster_key": cluster_key,
                    "question_text": q_text,
                    "options": options,
                }
            )
    return pool


def fetch_llm_generated_questions(base_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    settings.LLM_API_KEY가 설정되어 있을 경우, LLM API를 호출하여
    추가 상황별 질문 문구를 생성하고 기존 클러스터 조건식(options)과 결합합니다.
    """
    if not settings.LLM_API_KEY:
        return []

    cluster_options_map = {item["cluster_key"]: item["options"] for item in base_pool}
    prompt = (
        "당신은 음식 추천 서비스 WhatBap의 주문서 질문 생성 AI입니다. "
        "다음 cluster_key 목록에 대해 사용자에게 물어볼 자연스럽고 재치 있는 한국어 질문 문구를 "
        "각 클러스터당 2개씩 JSON 배열로 생성하세요. "
        "출력 형식: [{\"cluster_key\": \"has_soup\", \"question_text\": \"...\"}]\n"
        f"지원 cluster_key: {list(cluster_options_map.keys())}"
    )

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                settings.LLM_API_URL,
                headers={
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            items = json.loads(cleaned)
            extra: list[dict[str, Any]] = []
            for idx, item in enumerate(items, start=1):
                c_key = item.get("cluster_key")
                q_text = item.get("question_text")
                if c_key in cluster_options_map and q_text:
                    extra.append(
                        {
                            "question_code": f"q_llm_{c_key}_{idx:02d}",
                            "cluster_key": c_key,
                            "question_text": q_text,
                            "options": cluster_options_map[c_key],
                        }
                    )
            return extra
    except Exception as exc:
        logger.warning(f"LLM 추가 질문 생성 건너뜀 (내장 질문 풀 사용): {exc}")
        return []


def seed_database(
    db: Session,
    csv_path: Path = DEFAULT_CSV_PATH,
    max_menus: Optional[int] = None,
    include_beverages_and_bakery: bool = True,
    use_llm: bool = False,
) -> dict[str, int]:
    """
    1단계: CSV 파싱 및 MySQL menus 테이블 적재
    2단계: 200+ 질문 후보 풀(LLM 연동 포함) 생성
    3단계: 샤논 엔트로피·커버리지와 주제 다양성으로 초기 중요도를 계산해 질문 테이블에 적재
    """
    parsed_menus = parse_and_clean_csv(
        csv_path=csv_path,
        max_menus=max_menus,
        include_beverages_and_bakery=include_beverages_and_bakery,
    )

    existing_menus = {m.food_code: m for m in db.query(Menu).all()}
    new_menu_objs: list[Menu] = []
    for m_data in parsed_menus:
        code = m_data["food_code"]
        if code not in existing_menus:
            new_menu_objs.append(Menu(**m_data))
            existing_menus[code] = None
        elif existing_menus[code] is not None:
            existing_m = existing_menus[code]
            existing_m.kcal = m_data["kcal"]
            existing_m.heaviness = m_data["heaviness"]
            existing_m.main_ingredient = m_data["main_ingredient"]
            existing_m.allergen_tags = m_data["allergen_tags"]

    if new_menu_objs:
        db.bulk_save_objects(new_menu_objs)
    db.commit()

    all_menus = db.query(Menu).all()
    main_meals = [m for m in all_menus if m.meal_type == "main_meal"]
    reference_menus = main_meals if main_meals else all_menus

    raw_questions = build_candidate_question_pool()
    if use_llm:
        raw_questions.extend(fetch_llm_generated_questions(raw_questions))

    scored_questions = rank_and_score_question_pool(reference_menus, raw_questions)

    existing_q_map = {
        q.question_code: q for q in db.query(OrderSheetQuestion).all()
    }
    inserted_q = 0
    for q_data in scored_questions:
        code = q_data["question_code"]
        if code in existing_q_map:
            existing_q = existing_q_map[code]
            existing_q.question_text = q_data["question_text"]
            existing_q.options_json = q_data["options"]
            existing_q.initial_score = q_data["initial_score"]
            if existing_q.shown_count == 0 and existing_q.positive_count == 0:
                existing_q.importance_weight = q_data["importance_weight"]
        else:
            db.add(
                OrderSheetQuestion(
                    question_code=code,
                    cluster_key=q_data["cluster_key"],
                    question_text=q_data["question_text"],
                    options_json=q_data["options"],
                    initial_score=q_data["initial_score"],
                    importance_weight=q_data["importance_weight"],
                    shown_count=0,
                    positive_count=0,
                    is_active=True,
                )
            )
            inserted_q += 1

    db.commit()
    return {
        "total_menus_in_db": len(all_menus),
        "inserted_menus": len(new_menu_objs),
        "total_questions_in_pool": len(scored_questions),
        "inserted_questions": inserted_q,
    }


def _ensure_mysql_database_exists() -> None:
    """MySQL 연결 시 대상 데이터베이스(예: watbab)가 아직 없으면 자동으로 생성합니다."""
    url = make_url(settings.DATABASE_URL)
    if not url.drivername.startswith("mysql"):
        return
    db_name = url.database
    if not db_name:
        return
    conn = pymysql.connect(
        host=url.host or "localhost",
        port=url.port or 3306,
        user=url.username or "root",
        password=url.password or "",
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            )
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="WhatBap CSV 메뉴 파싱 및 주문서 질문 풀 시드 스크립트")
    parser.add_argument("--csv", type=str, default=str(DEFAULT_CSV_PATH), help="식품영양성분 CSV 파일 경로")
    parser.add_argument("--max-menus", type=int, default=None, help="최대 적재 메뉴 수 제한 (전체 적재 시 생략)")
    parser.add_argument("--meals-only", action="store_true", help="음료/베이커리 제외하고 정규 식사 메뉴만 적재")
    args = parser.parse_args()

    _ensure_mysql_database_exists()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        summary = seed_database(
            db=db,
            csv_path=Path(args.csv),
            max_menus=args.max_menus,
            include_beverages_and_bakery=not args.meals_only,
            use_llm=True,
        )
        print(f"[시드 완료] {summary}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

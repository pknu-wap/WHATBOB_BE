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
from app.recommend.question_pool import (
    MAX_QUESTION_TEXT_LENGTH,
    QUESTION_CODE_PREFIX,
    build_candidate_question_pool,
)

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


MEAL_BREAD_NAMES = frozenset({
    "버거", "햄버거", "피자", "피자빵", "샌드위치", "핫도그", "토스트",
    "또띠아", "부리토", "브리토", "타코", "퀘사디아", "케밥", "파니니",
})


def _is_meal_bread(cat_large: str, name: str) -> bool:
    return cat_large == "빵 및 과자류" and name.split("_")[0].strip() in MEAL_BREAD_NAMES


def _derive_meal_type(cat_large: str, name: str = "") -> str:
    if _is_meal_bread(cat_large, name):
        return "main_meal"
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

            if (not include_beverages_and_bakery
                    and cat_large in ("음료 및 차류", "빵 및 과자류", "유제품류 및 빙과류")
                    and not _is_meal_bread(cat_large, name)):
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
                "meal_type": _derive_meal_type(cat_large, name),
                "has_soup": _derive_has_soup(cat_large, name),
                "cooking_style": _derive_cooking_style(cat_large),
                "main_ingredient": _derive_main_ingredient(cat_medium, cat_small, name),
                "flavor_intensity": _derive_flavor_intensity(cat_medium, name, sodium_mg, sugar_g),
                "temperature": "hot" if _is_meal_bread(cat_large, name) else _derive_temperature(cat_large, name),
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
        "질문은 공백과 문장부호를 포함해 24자 이내여야 합니다. "
        "각 질문은 두 취향 선택지와 상관없어요 중 하나를 고를 수 있는 내용이어야 합니다. "
        "출력 형식: [{\"cluster_key\": \"has_soup\", \"question_text\": \"...\"}]\n"
        f"클러스터별 선택지: {json.dumps(cluster_options_map, ensure_ascii=False)}"
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
                if not isinstance(q_text, str):
                    continue
                q_text = q_text.strip()
                if c_key in cluster_options_map and 1 <= len(q_text) <= MAX_QUESTION_TEXT_LENGTH:
                    extra.append(
                        {
                            "question_code": f"{QUESTION_CODE_PREFIX}llm_{c_key}_{idx:02d}",
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
            for field, value in m_data.items():
                setattr(existing_m, field, value)

    if new_menu_objs:
        db.bulk_save_objects(new_menu_objs)
    db.commit()

    question_summary = seed_question_pool(db, use_llm=use_llm)
    return {
        "total_menus_in_db": db.query(Menu).count(),
        "inserted_menus": len(new_menu_objs),
        **question_summary,
    }


def seed_question_pool(db: Session, use_llm: bool = False) -> dict[str, int]:
    """기존 취향 선택지 ID를 유지하며 상관없음을 추가하고 이전 질문은 이력용으로 보관한다."""
    all_menus = db.query(Menu).all()
    if not all_menus:
        raise ValueError("질문의 초기 점수를 계산하려면 메뉴 데이터가 필요합니다.")
    main_meals = [m for m in all_menus if m.meal_type == "main_meal"]
    reference_menus = main_meals if main_meals else all_menus

    raw_questions = build_candidate_question_pool()
    if use_llm:
        raw_questions.extend(fetch_llm_generated_questions(raw_questions))

    scored_questions = rank_and_score_question_pool(reference_menus, raw_questions)

    existing_q_map = {
        q.question_code: q for q in db.query(OrderSheetQuestion).all()
    }
    deactivated_q = 0
    for question in existing_q_map.values():
        # 과거 답변·피드백이 참조하는 선택지와 조건은 덮어쓰지 않는다.
        if not question.question_code.startswith(QUESTION_CODE_PREFIX) and question.is_active:
            question.is_active = False
            deactivated_q += 1
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
        "total_questions_in_pool": len(scored_questions),
        "inserted_questions": inserted_q,
        "deactivated_questions": deactivated_q,
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
    parser.add_argument("--meals-only", action="store_true", help="햄버거·피자 등 식사용 빵류를 포함하고 음료·디저트는 제외")
    parser.add_argument("--questions-only", action="store_true", help="기존 메뉴를 수정하지 않고 상관없음·24자 질문만 갱신 (LLM 호출 없음)")
    parser.add_argument("--no-llm", action="store_true", help="외부 LLM 호출 없이 내장 질문만 적재")
    args = parser.parse_args()

    if not args.questions_only:
        _ensure_mysql_database_exists()
        Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if args.questions_only:
            summary = seed_question_pool(db)
        else:
            summary = seed_database(
                db=db,
                csv_path=Path(args.csv),
                max_menus=args.max_menus,
                include_beverages_and_bakery=not args.meals_only,
                use_llm=not args.no_llm,
            )
        print(f"[시드 완료] {summary}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

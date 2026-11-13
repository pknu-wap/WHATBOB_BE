from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.menus.models import UserFoodExclusion
from app.recommend.common import (
    ALLERGEN_BY_CODE,
    ALLERGEN_CATALOG_VERSION,
    OFFICIAL_ALLERGEN_ITEMS,
    UNABLE_BY_CODE,
    UNABLE_CATALOG_VERSION,
    UNABLE_INGREDIENT_ITEMS,
)

ALLERGY_EXACT_SYNONYMS: dict[str, str] = {
    "달걀": "egg",
    "계란": "egg",
    "알류": "egg",
    "난류": "egg",
    "알류(달걀)": "egg",
    "우유": "milk",
    "메밀": "buckwheat",
    "땅콩": "peanut",
    "대두": "soybean",
    "대두(콩)": "soybean",
    "콩": "soybean",
    "밀": "wheat",
    "밀가루": "wheat",
    "고등어": "mackerel",
    "게": "crab",
    "새우": "shrimp",
    "돼지고기": "pork",
    "복숭아": "peach",
    "토마토": "tomato",
    "아황산류": "sulfite",
    "호두": "walnut",
    "닭고기": "chicken",
    "쇠고기": "beef",
    "소고기": "beef",
    "쇠고기(소고기)": "beef",
    "오징어": "squid",
    "조개류": "shellfish",
    "조개류(굴/전복/홍합 포함)": "shellfish",
    "잣": "pine_nut",
}
for _item in OFFICIAL_ALLERGEN_ITEMS:
    ALLERGY_EXACT_SYNONYMS[_item["id"].lower()] = _item["id"]
    ALLERGY_EXACT_SYNONYMS[_item["name"].lower()] = _item["id"]
    ALLERGY_EXACT_SYNONYMS[_item["legal_name"].lower()] = _item["id"]

UNABLE_EXACT_SYNONYMS: dict[str, str] = {
    "돼지고기": "pork",
    "돼지": "pork",
    "소고기": "beef",
    "쇠고기": "beef",
    "쇠고기(소고기)": "beef",
    "닭고기": "poultry",
    "닭": "poultry",
    "가금류": "poultry",
    "닭·가금류": "poultry",
    "해산물": "seafood",
    "어패류": "seafood",
    "해산물·어패류": "seafood",
    "달걀": "egg",
    "계란": "egg",
    "난류": "egg",
    "알류": "egg",
    "알류(달걀)": "egg",
    "달걀(난류)": "egg",
    "우유": "dairy",
    "유제품": "dairy",
    "밀": "wheat",
    "밀가루": "wheat",
    "글루텐": "wheat",
    "밀·글루텐": "wheat",
    "대두": "soybean",
    "콩": "soybean",
    "대두(콩)": "soybean",
    "대두·콩류": "soybean",
}
for _item in UNABLE_INGREDIENT_ITEMS:
    UNABLE_EXACT_SYNONYMS[_item["id"].lower()] = _item["id"]
    UNABLE_EXACT_SYNONYMS[_item["name"].lower()] = _item["id"]


def resolve_legacy_exclusion_code(exclusion_type: str, keyword: str | None) -> tuple[str, int, str] | None:
    """
    기존 자유 문자열 제외 항목이 표준 카탈로그 코드와 1:1로 정확히 대응하면
    (catalog_code, catalog_version, canonical_label)을 반환하고,
    모호하거나 대응되지 않으면 None을 반환한다.
    """
    cleaned = (keyword or "").strip().lower()
    if not cleaned:
        return None
    exc_type = (exclusion_type or "").upper()
    if exc_type == "ALLERGY":
        code = ALLERGY_EXACT_SYNONYMS.get(cleaned)
        if code and code in ALLERGEN_BY_CODE:
            return code, ALLERGEN_CATALOG_VERSION, ALLERGEN_BY_CODE[code]["name"]
        return None
    if exc_type == "UNABLE":
        code = UNABLE_EXACT_SYNONYMS.get(cleaned)
        if code and code in UNABLE_BY_CODE:
            return code, UNABLE_CATALOG_VERSION, UNABLE_BY_CODE[code]["name"]
        return None
    return None


def inspect_legacy_exclusions(db: Session) -> dict[str, Any]:
    rows = (
        db.query(UserFoodExclusion)
        .filter(UserFoodExclusion.exclusion_type.in_(["ALLERGY", "UNABLE"]))
        .order_by(UserFoodExclusion.id.asc())
        .all()
    )
    already_mapped = 0
    mappable = 0
    unmapped_preserved = 0
    unmapped_examples: list[dict[str, Any]] = []

    for row in rows:
        if row.catalog_code:
            already_mapped += 1
            continue
        resolved = resolve_legacy_exclusion_code(row.exclusion_type, row.keyword)
        if resolved is not None:
            mappable += 1
        else:
            unmapped_preserved += 1
            if len(unmapped_examples) < 20:
                unmapped_examples.append(
                    {
                        "id": row.id,
                        "user_id": row.user_id,
                        "exclusion_type": row.exclusion_type,
                        "keyword": row.keyword,
                    }
                )

    return {
        "total_allergy_unable_rows": len(rows),
        "already_mapped": already_mapped,
        "mappable": mappable,
        "unmapped_preserved": unmapped_preserved,
        "unmapped_examples": unmapped_examples,
    }


def apply_legacy_exclusion_mapping(db: Session) -> dict[str, int]:
    """
    기존 ALLERGY / UNABLE 자유 문자열 행 중 정확 대응 가능한 항목만 catalog_code를 채우고,
    동일 (user_id, exclusion_type, catalog_code) 중복 행은 하나로 합치며,
    모호한 항목은 catalog_code=None 상태로 보존한다.
    """
    rows = (
        db.query(UserFoodExclusion)
        .filter(UserFoodExclusion.exclusion_type.in_(["ALLERGY", "UNABLE"]))
        .order_by(UserFoodExclusion.id.asc())
        .with_for_update()
        .all()
    )
    seen_codes: set[tuple[int, str, str]] = set()
    for row in rows:
        if row.catalog_code:
            seen_codes.add((row.user_id, row.exclusion_type.upper(), row.catalog_code))

    mapped_count = 0
    deduplicated_count = 0
    preserved_unmapped_count = 0

    for row in rows:
        if row.catalog_code:
            continue
        resolved = resolve_legacy_exclusion_code(row.exclusion_type, row.keyword)
        if resolved is None:
            preserved_unmapped_count += 1
            continue
        code, version, canonical_label = resolved
        key = (row.user_id, row.exclusion_type.upper(), code)
        if key in seen_codes:
            db.delete(row)
            deduplicated_count += 1
        else:
            row.catalog_code = code
            row.catalog_version = version
            row.keyword = canonical_label
            seen_codes.add(key)
            mapped_count += 1

    db.commit()
    return {
        "mapped_count": mapped_count,
        "deduplicated_count": deduplicated_count,
        "preserved_unmapped_count": preserved_unmapped_count,
    }

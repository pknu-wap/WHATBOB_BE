"""DB나 외부 API에 접근하지 않고 공유용 식사 메뉴·질문 데이터를 재생성한다."""

import csv
import json
from pathlib import Path

from app.menus.models import Menu
from app.recommend.order_sheet import rank_and_score_question_pool
from app.recommend.question_pool import build_candidate_question_pool
from scripts.seed_menus import parse_and_clean_csv


def main():
    directory = Path(__file__).resolve().parents[1] / "data"
    menus = parse_and_clean_csv(include_beverages_and_bakery=False)
    (directory / "menus_meals_only.json").write_text(
        json.dumps(menus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    with (directory / "menus_meals_only.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(menus[0]))
        writer.writeheader()
        for menu in menus:
            writer.writerow({**menu, "allergen_tags": json.dumps(menu["allergen_tags"], ensure_ascii=False)})
    questions = rank_and_score_question_pool([Menu(**menu) for menu in menus], build_candidate_question_pool())
    for question in questions:
        question["info_gain_score"] = question.pop("initial_score")
        question["ml_weight"] = question.pop("importance_weight")
    (directory / "order_sheet_questions_pool.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"공유 데이터 생성: 식사 메뉴 {len(menus)}개, 질문 {len(questions)}개")


if __name__ == "__main__":
    main()

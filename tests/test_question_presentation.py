from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import get_current_user
from app.core.database import Base, get_db
from app.main import handle_recommendation_domain_exception
from app.menus.models import Menu, OrderSheetQuestion
from app.recommend.common import RecommendationDomainException, evaluate_option_rules
from app.recommend.order_sheet import _to_question_dto, calculate_question_entropy_score
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.router import router
from app.users.models import User
from scripts.seed_menus import seed_question_pool


def carb_question():
    return next(q for q in build_candidate_question_pool() if q["cluster_key"] == "carb_level")


def make_old_question():
    definition = carb_question()
    options = deepcopy(definition["options"])
    options[0]["label"] = "탄수화물 15g 이하"
    options[1]["label"] = "탄수화물 15g 초과"
    return OrderSheetQuestion(
        id=1, question_code=definition["question_code"], cluster_key="carb_level",
        question_text="오늘은 저탄수 메뉴 어때요?", options_json=options,
        initial_score=0.72, importance_weight=0.67, shown_count=12, positive_count=3,
    )


@pytest.mark.parametrize("cluster,field,boundary,yes_below,yes_equal,yes_above", [
    ("carb_level", "carb_g", 15, True, True, False),
    ("protein_level", "protein_g", 10, False, True, True),
    ("sodium_level", "sodium_mg", 250, True, True, False),
    ("sugar_level", "sugar_g", 3, True, True, False),
])
def test_yes_no_numeric_boundaries_keep_existing_meaning(cluster, field, boundary, yes_below, yes_equal, yes_above):
    question = next(q for q in build_candidate_question_pool() if q["cluster_key"] == cluster)
    yes, no, any_option = question["options"]
    assert [yes["label"], no["label"], any_option["label"]] == ["예", "아니요", "상관없어요"]
    for value, expected in ((boundary - .01, yes_below), (boundary, yes_equal), (boundary + .01, yes_above)):
        menu = Menu(**{field: value})
        assert evaluate_option_rules(menu, yes["rules"]) is expected
        assert evaluate_option_rules(menu, no["rules"]) is not expected
    missing = Menu(**{field: None})
    assert not evaluate_option_rules(missing, yes["rules"])
    assert not evaluate_option_rules(missing, no["rules"])


@pytest.mark.parametrize("ingredient,yes_expected", [
    ("beef", True), ("pork", True), ("poultry", True), ("seafood", False), ("veggie_tofu", False),
])
def test_meat_yes_no_keeps_original_option_ids_and_rules(ingredient, yes_expected):
    question = next(q for q in build_candidate_question_pool() if q["cluster_key"] == "main_ingredient")
    yes, no, _ = question["options"]
    assert (yes["option_id"], no["option_id"]) == ("meat", "non_meat")
    menu = Menu(main_ingredient=ingredient)
    assert evaluate_option_rules(menu, yes["rules"]) is yes_expected
    assert evaluate_option_rules(menu, no["rules"]) is not yes_expected


def test_old_database_gets_new_display_without_mutation_or_score_change():
    row = make_old_question()
    before = deepcopy(row.options_json)
    dto = _to_question_dto(row)
    assert dto.question_text == "저탄수화물 식사를 하고 싶나요?"
    assert [o.label for o in dto.options] == ["예", "아니요", "상관없어요"]
    assert [o.option_id for o in dto.options] == ["low_carb", "rich_carb", "any"]
    assert [o.model_dump()["rules"] for o in dto.options] == [o["rules"] for o in before]
    assert row.options_json == before and row.question_text == "오늘은 저탄수 메뉴 어때요?"
    assert (dto.id, dto.initial_score, dto.importance_weight, dto.shown_count, dto.positive_count) == (1, .72, .67, 12, 3)
    menus = [Menu(carb_g=value) for value in (10, 15, 20, None)]
    assert calculate_question_entropy_score(menus, before) == calculate_question_entropy_score(
        menus, [o.model_dump() for o in dto.options],
    )


def test_unknown_or_manually_changed_question_is_not_relabelled():
    row = make_old_question()
    row.options_json[0]["rules"][0]["op"] = "gte"
    dto = _to_question_dto(row)
    assert dto.question_text == row.question_text
    assert dto.options[0].label == row.options_json[0]["label"]
    row = make_old_question()
    row.question_code = "custom_carb_question"
    assert _to_question_dto(row).question_text == row.question_text


def test_old_llm_question_uses_affirmative_text_with_existing_rules():
    row = make_old_question()
    row.question_code = "q_binary_v1_llm_carb_level_01"
    row.question_text = "탄수화물을 피하고 싶지 않나요?"
    dto = _to_question_dto(row)
    assert dto.question_text == "저탄수화물 식사를 하고 싶나요?"
    assert dto.options[0].option_id == "low_carb" and dto.options[0].label == "예"


def test_http_legacy_labels_display_yes_no_and_submit_original_ids():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            row = make_old_question()
            row.options_json = row.options_json[:2]  # any 복구 전 DB도 그대로 동작한다.
            menus = [Menu(food_code=f"presentation-{index}", name=name, base_name=name,
                          category_large="식사", meal_type="main_meal", carb_g=carbs)
                     for index, (name, carbs) in enumerate((("메뉴A", 14), ("메뉴B", 16)))]
            db.add_all([row, *menus])
            db.commit()
            user = User(kakao_id=701999, nickname="표시테스터")
            db.add(user)
            db.commit()
            before = deepcopy(row.options_json)
            app = FastAPI()
            app.include_router(router)
            app.add_exception_handler(RecommendationDomainException, handle_recommendation_domain_exception)
            app.dependency_overrides[get_db] = lambda: db
            app.dependency_overrides[get_current_user] = lambda: user
            with TestClient(app) as client:
                response = client.get("/api/recommendations/order-sheet/questions")
                assert response.status_code == 200
                question = response.json()["data"]["questions"][0]
                assert question["question_text"] == "저탄수화물 식사를 하고 싶나요?"
                assert [o["label"] for o in question["options"]] == ["예", "아니요", "상관없어요"]
                for option, expected in zip(question["options"], ("메뉴A", "메뉴B", None)):
                    response = client.post("/api/recommendations/order-sheet", json={
                        "answers": [{"question_id": row.id, "option_id": option["option_id"]}], "top_k": 1,
                    })
                    assert response.status_code == 201
                    menu = response.json()["data"]["recommended_menus"][0]
                    if expected:
                        assert menu["name"] == expected and menu["match_score"] == .67
                    else:
                        assert menu["match_score"] == 0
                # 표시 글자가 아닌 기존 답변 ID를 보내는 계약을 유지한다.
                invalid = client.post("/api/recommendations/order-sheet", json={
                    "answers": [{"question_id": row.id, "option_id": "예"}],
                })
                assert invalid.status_code == 400
            db.refresh(row)
            assert row.options_json == before
            assert row.importance_weight == .67
            # 질문만 재시드해도 ID와 사용 중인 중요도를 유지한다.
            seed_question_pool(db)
            db.refresh(row)
            assert row.id == 1 and row.importance_weight == .67
            assert [o["label"] for o in row.options_json] == ["예", "아니요", "상관없어요"]
    finally:
        engine.dispose()

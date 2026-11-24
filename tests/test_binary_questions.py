import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.menus.models import Menu, OrderSheetQuestion, Recommendation
from app.users.models import User
from app.recommend.common import (
    derive_allergens_from_text,
    evaluate_option_rules,
    matches_unable_code,
)
from app.recommend.order_sheet import record_recommendation_feedback
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.router import router
from app.recommend.schemas import OrderSheetQuestionDTO, RecommendationFeedbackRequest
from scripts import seed_menus


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        menus = seed_menus.parse_and_clean_csv(
            Path(seed_menus.DEFAULT_CSV_PATH), max_menus=30,
            include_beverages_and_bakery=False,
        )
        session.add_all(Menu(**menu) for menu in menus)
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.parametrize("category,name,expected", [
    ("조림류", "닭볶음탕", True),
    ("조림류", "닭도리탕", True),
    ("찜류", "닭찜_간장", True),
    ("찜류", "닭찜_매운양념", True),
    ("튀김류", "닭튀김_불짬뽕 치킨", False),
    ("면 및 만두류", "짬뽕", True),
])
def test_broth_classification_for_chicken_and_jjamppong(category, name, expected):
    assert seed_menus._derive_has_soup(category, name) is expected


@pytest.mark.parametrize("name,style,has_soup", [
    ("떡만두국_고기만두", "soup_stew", True),
    ("만두_고기만두", "other", False),
    ("고기만두", "other", False),
    ("만두_군만두", "fried_pan", False),
    ("라면_만두", "noodle", True),
])
def test_dumpling_menu_classification(name, style, has_soup):
    assert seed_menus._derive_cooking_style("면 및 만두류", name) == style
    assert seed_menus._derive_has_soup("면 및 만두류", name) is has_soup


@pytest.mark.parametrize("category,name,expected", [
    ("면 및 만두류", "라멘_간편조리세트_매운돈코츠라멘", True),
    ("면 및 만두류", "라멘_돈코츠라멘", True),
    ("면 및 만두류", "탄탄면_간편조리세트_마라 탄탄면", True),
    ("생채·무침류", "물회", True),
    ("생채·무침류", "물회_오징어", True),
    ("면 및 만두류", "국수_잔치국수", True),
    ("면 및 만두류", "메밀국수_온면", True),
    ("면 및 만두류", "밀면_물밀면", True),
    ("면 및 만두류", "콩국수", True),
    ("면 및 만두류", "메밀소바_간편조리세트_부타이 마제소바", False),
    ("면 및 만두류", "국수_비빔국수", False),
    ("면 및 만두류", "냉면_비빔냉면", False),
    ("면 및 만두류", "볶음 우동", False),
])
def test_broth_classification_for_ramen_tantanmen_mulhoe_and_broth_noodles(category, name, expected):
    assert seed_menus._derive_has_soup(category, name) is expected


@pytest.mark.parametrize("name,expected_ing,expect_seafood_allergen", [
    ("스파게티_미트 토마토 파스타", "beef", False),
    ("스파게티_베이컨 크림 파스타", "pork", False),
    ("스파게티_간편조리세트_알리오올리오파스타", "other", False),
    ("피자_마르게리타 피자", "other", False),
    ("샌드위치_불고기치즈 바게트", "beef", False),
    ("육회", "beef", False),
    ("육회비빔밥", "beef", False),
    ("피자_갈릭쉬림프 피자 오리지널 (M)", "seafood", True),
    ("피자_골드고구마 피자 오리지널 (M)", "veggie_tofu", False),
    ("버거_NBB 오리지널 버거", "other", False),
    ("샌드위치_잠봉뵈르 샌드위치", "pork", False),
    ("피자_쉬림프갈릭스노윙 피자", "seafood", True),
    ("에이드_한라봉 에이드", "other", False),
    ("오리백숙", "poultry", False),
    ("훈제오리", "poultry", False),
    ("꽃게탕", "seafood", True),
    ("게살죽", "seafood", True),
    ("회덮밥", "seafood", True),
    ("물회_오징어", "seafood", True),
    ("닭다리튀김_봉 (5조각)", "poultry", False),
    ("닭튀김_교촌윙 치킨", "poultry", False),
])
def test_main_ingredient_and_seafood_allergen_avoid_substring_false_positives(
    name, expected_ing, expect_seafood_allergen,
):
    assert seed_menus._derive_main_ingredient("", "", name) == expected_ing
    allergens = derive_allergens_from_text(name)
    assert ("해산물" in allergens) is expect_seafood_allergen


@pytest.mark.parametrize("name,expected_soup,expected_flavor", [
    ("떡볶이", True, "spicy_bold"),
    ("떡볶이_국물떡볶이", True, "spicy_bold"),
    ("떡볶이_매콤 떡볶이", True, "spicy_bold"),
    ("떡볶이_간편조리세트_마라떡볶이", True, "spicy_bold"),
    ("떡볶이_간편조리세트_달콤짭짤 궁중식 떡볶이", True, "sweet_savory"),
    ("떡볶이_간편조리세트_로제떡볶이", True, "sweet_savory"),
    ("떡볶이_까르보떡볶이", True, "sweet_savory"),
    ("떡볶이_간편조리세트_짜장떡볶이(순한맛)", True, "sweet_savory"),
    ("떡볶이_간장", True, "sweet_savory"),
    ("라볶이", True, "mild_clean"),
])
def test_tteokbokki_broth_and_flavor_classification(name, expected_soup, expected_flavor):
    assert seed_menus._derive_has_soup("볶음류", name) is expected_soup
    assert seed_menus._derive_flavor_intensity("", name, 460.0 if expected_flavor == "sweet_savory" else 300.0, 4.0) == expected_flavor


def test_unable_poultry_does_not_exclude_original_or_jambon_menus():
    original_pizza = Menu(
        name="피자_갈릭쉬림프 피자 오리지널 (M)",
        base_name="피자",
        main_ingredient="seafood",
        allergen_tags=["밀", "해산물"],
    )
    duck_soup = Menu(
        name="오리백숙",
        base_name="오리백숙",
        main_ingredient="poultry",
        allergen_tags=[],
    )
    assert matches_unable_code(original_pizza, "poultry") is False
    assert matches_unable_code(duck_soup, "poultry") is True


def test_binary_options_cover_existing_menu_categories():
    """다섯 선택지를 단순 삭제하지 않고 모든 기존 분류를 두 그룹에 포함한다."""
    pool = build_candidate_question_pool()
    assert len(pool) == 204
    assert len({q["question_code"] for q in pool}) == 204
    assert len({q["cluster_key"] for q in pool}) == 12
    menus = seed_menus.parse_and_clean_csv(
        Path(seed_menus.DEFAULT_CSV_PATH), include_beverages_and_bakery=True,
    )
    for question in pool:
        assert 1 <= len(question["question_text"]) <= 24
        if question["cluster_key"] == "cooking_style":
            assert len(question["options"]) == 4
            assert [o["label"] for o in question["options"]] == ["밥", "면", "그 외", "상관없어요"]
            assert len({o["option_id"] for o in question["options"]}) == 4
        else:
            assert len(question["options"]) == 3
            assert [o["label"] for o in question["options"]] == ["예", "아니요", "상관없어요"]
            assert len({o["option_id"] for o in question["options"]}) == 3
        assert question["options"][-1] == {"option_id": "any", "label": "상관없어요", "rules": []}
    # 같은 주제의 문구 변형은 동일한 두 선택지를 사용한다.
    for question in {q["cluster_key"]: q for q in pool}.values():
        options = [o for o in question["options"] if o["rules"]]
        field = options[0]["rules"][0]["field"]
        for menu_data in menus:
            if menu_data[field] is not None:
                menu = Menu(**menu_data)
                assert sum(evaluate_option_rules(menu, o["rules"]) for o in options) == 1


@pytest.mark.parametrize("cluster,field,boundary", [
    ("protein_level", "protein_g", 10.0),
    ("carb_level", "carb_g", 15.0),
    ("sodium_level", "sodium_mg", 250.0),
    ("sugar_level", "sugar_g", 3.0),
])
def test_numeric_options_do_not_overlap_at_boundary(cluster, field, boundary):
    question = next(q for q in build_candidate_question_pool() if q["cluster_key"] == cluster)
    for value in (boundary - 0.01, boundary, boundary + 0.01):
        menu = Menu(**{field: value})
        assert sum(evaluate_option_rules(menu, o["rules"]) for o in question["options"] if o["rules"]) == 1


def test_response_contract_enforces_24_characters_and_two_to_four_options():
    question = build_candidate_question_pool()[0]
    payload = dict(question, id=1, initial_score=0.5, importance_weight=0.5)
    payload["question_text"] = "가" * 24
    assert len(OrderSheetQuestionDTO(**payload).question_text) == 24
    with pytest.raises(ValidationError):
        OrderSheetQuestionDTO(**dict(payload, question_text="가" * 25))
    assert len(OrderSheetQuestionDTO(**dict(payload, options=question["options"][:2])).options) == 2
    q8 = next(q for q in build_candidate_question_pool() if q["cluster_key"] == "cooking_style")
    assert len(OrderSheetQuestionDTO(**dict(payload, options=q8["options"])).options) == 4
    for count in (1, 5):
        with pytest.raises(ValidationError):
            OrderSheetQuestionDTO(**dict(payload, options=[question["options"][0]] * count))


def test_shared_question_json_matches_binary_pool():
    path = Path(__file__).resolve().parents[1] / "data" / "order_sheet_questions_pool.json"
    shared = json.loads(path.read_text(encoding="utf-8"))
    expected = {q["question_code"]: q for q in build_candidate_question_pool()}
    assert len(shared) == len(expected)
    assert {q["question_code"] for q in shared} == set(expected)
    for question in shared:
        source = expected[question["question_code"]]
        assert question["question_text"] == source["question_text"]
        assert question["options"] == source["options"]
        assert "info_gain_score" in question and "ml_weight" in question


def test_llm_discards_overlong_questions_without_truncating(monkeypatch):
    items = [
        {"cluster_key": "has_soup", "question_text": text}
        for text in ("가" * 24, "나" * 25, "   ", 123)
    ]
    def respond(request):
        prompt = json.loads(request.content)["messages"][0]["content"]
        assert "24자" in prompt
        assert "soup_yes" in prompt and "soup_no" in prompt
        assert "예/아니요" in prompt and "긍정형" in prompt
        return httpx.Response(200, json={"choices": [{
            "message": {"content": json.dumps(items, ensure_ascii=False)},
        }]})

    client_class = httpx.Client
    monkeypatch.setattr(seed_menus.httpx, "Client", lambda **kwargs: client_class(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    monkeypatch.setattr(seed_menus.settings, "LLM_API_KEY", "test-key")
    questions = seed_menus.fetch_llm_generated_questions(build_candidate_question_pool())
    assert len(questions) == 1
    assert questions[0]["question_text"] == "가" * 24
    assert len(questions[0]["options"]) == 3


def test_seed_preserves_old_feedback_and_serves_binary_questions(db):
    """이전 질문의 조건은 보존하고 새 질문만 조회·추천에 사용한다."""
    menu = db.query(Menu).first()
    old_options = [{"option_id": "legacy_soup", "label": "예전 선택지", "rules": [
        {"field": "has_soup", "op": "eq", "value": menu.has_soup},
    ]}, {"option_id": "any", "label": "상관없어요", "rules": []}]
    old = OrderSheetQuestion(
        question_code="q_has_soup_01", cluster_key="has_soup",
        question_text="국물이 있는 메뉴를 추천해 드릴까요, 없는 메뉴를 추천해 드릴까요?",
        options_json=old_options, initial_score=0.8, importance_weight=0.8,
    )
    db.add(old)
    db.flush()
    record = Recommendation(
        shown_question_ids=[old.id],
        answers_json=[{"question_id": old.id, "option_id": "legacy_soup"}],
        recommended_menu_ids=[menu.id],
    )
    db.add(record)
    db.commit()
    before_menus = [(m.id, m.name, m.kcal, m.allergen_tags) for m in db.query(Menu).all()]
    summary = seed_menus.seed_question_pool(db)
    assert summary["inserted_questions"] == 204
    assert summary["deactivated_questions"] == 1
    assert old.is_active is False
    assert old.options_json == old_options
    assert before_menus == [(m.id, m.name, m.kcal, m.allergen_tags) for m in db.query(Menu).all()]

    user = User(kakao_id=701001, nickname="이진질문테스터")
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    from app.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        response = client.get("/api/recommendations/order-sheet/questions")
        assert response.status_code == 200
        data = response.json()["data"]
        # 30개 샘플 메뉴에서 실제 후보를 구분하는 유효 주제만 반환한다 (퇴화 주제 제외).
        assert (data["total_count"], data["exploitation_count"], data["exploration_count"]) == (8, 8, 0)
        for question in data["questions"]:
            assert question["id"] != old.id
            assert len(question["question_text"]) <= 24
            expected_opts = 4 if question["cluster_key"] == "cooking_style" else 3
            assert len(question["options"]) == expected_opts
        initial_shown_counts = {q["id"]: q["shown_count"] for q in data["questions"]}
        # 두 취향 답변과 상관없음 모두 추천과 피드백까지 처리할 수 있다.
        # Spec #77 (#81): 하드필터는 자동 완화하지 않으므로 남은 음식 종류 수(최대 5종)만큼 반환한다.
        feedback_recorded = 0
        for index in (0, 1, 2):
            response = client.post("/api/recommendations/order-sheet", json={
                "answers": [
                    {
                        "question_id": q["id"],
                        "option_id": q["options"][-1]["option_id"] if index == 2 else q["options"][index]["option_id"],
                    }
                    for q in data["questions"]
                ],
                "top_k": 5,
            })
            assert response.status_code == 201
            result = response.json()["data"]
            assert len(result["recommended_menus"]) == min(result["candidate_count"], 5)
            if result["recommended_menus"]:
                feedback = client.post(f'/api/recommendations/{result["recommendation_id"]}/feedback', json={
                    "selected_menu_id": result["recommended_menus"][0]["menu_id"],
                    "is_satisfied": index == 0,
                })
                assert feedback.status_code == 200
                feedback_recorded += 1
        assert feedback_recorded >= 2

    submitted_questions = (
        db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.id.in_(list(initial_shown_counts)))
        .all()
    )
    assert {q.id: q.shown_count for q in submitted_questions} == {
        question_id: count + 3 for question_id, count in initial_shown_counts.items()
    }

    legacy_fb = record_recommendation_feedback(db, record.id, RecommendationFeedbackRequest(
        selected_menu_id=menu.id, is_satisfied=True,
    ))
    # Spec #77 (#79): 비활성 이전 질문의 답변 조건 일치 기록은 유지하되 질문 전역 positive_count로 전파하지 않는다.
    assert legacy_fb.exposure_rank == 1
    assert legacy_fb.updated_weights == {}
    assert old.positive_count == 0
    new_question = db.query(OrderSheetQuestion).filter(OrderSheetQuestion.is_active.is_(True)).first()
    weight = new_question.importance_weight
    repeated = seed_menus.seed_question_pool(db)
    assert repeated["inserted_questions"] == 0
    assert repeated["deactivated_questions"] == 0
    assert new_question.importance_weight == weight

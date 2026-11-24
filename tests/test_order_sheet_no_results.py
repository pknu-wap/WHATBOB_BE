"""Spec #77 / Ticket #84: 개인 주문서 후보 0건 답변 변경 제안 단위·서비스 테스트."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base
from app.menus.models import Menu, OrderSheetQuestion
from app.recommend.no_results import suggest_no_result_answer_changes
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.schemas import AnswerItemRequest


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed_questions(db: Session) -> dict[str, OrderSheetQuestion]:
    pool = build_candidate_question_pool()
    by_cluster: dict[str, OrderSheetQuestion] = {}
    for cluster in settings.ORDER_SHEET_INITIAL_CLUSTERS:
        raw = next(q for q in pool if q["cluster_key"] == cluster)
        q = OrderSheetQuestion(
            question_code=raw["question_code"],
            cluster_key=cluster,
            question_text=raw["question_text"],
            options_json=raw["options"],
            initial_score=0.75,
            importance_weight=0.75,
            is_active=True,
        )
        db.add(q)
        by_cluster[cluster] = q
    db.flush()
    return by_cluster


def _add_menu(
    db: Session,
    *,
    food_code: str,
    name: str,
    base_name: str,
    has_soup: bool = True,
    cooking_style: str = "rice",
    main_ingredient: str = "beef",
    sodium_mg: float | None = 500.0,
    heaviness: str = "moderate",
    flavor_intensity: str = "mild_clean",
    origin_type: str = "restaurant",
    carb_g: float | None = 50.0,
    protein_g: float | None = 15.0,
    allergen_tags: list[str] | None = None,
) -> Menu:
    menu = Menu(
        food_code=food_code,
        name=name,
        base_name=base_name,
        category_large="식사",
        meal_type="main_meal",
        has_soup=has_soup,
        cooking_style=cooking_style,
        main_ingredient=main_ingredient,
        sodium_mg=sodium_mg,
        heaviness=heaviness,
        flavor_intensity=flavor_intensity,
        origin_type=origin_type,
        carb_g=carb_g,
        protein_g=protein_g,
        temperature="hot",
        allergen_tags=list(allergen_tags or []),
    )
    db.add(menu)
    db.flush()
    return menu


def test_suggest_flips_hard_filters_independently_sorts_by_capped_count_and_preserves_tie_order(
    db: Session,
):
    """
    Spec #77 §2 / Q12:
    - 적용 중인 취향 하드필터(has_soup, cooking_style, main_ingredient)만 하나씩 'any'로 바꿔 계산한다.
    - 점수제 질문(flavor_intensity 등)은 제안 대상에서 제외한다.
    - 음식 종류별로 중복 제거한 뒤 최대 5종으로 상한을 적용해 내림차순 정렬한다.
    - 6종과 10종은 모두 5종으로 표시되므로 동률이며, 원래 답변 순서를 유지한다.
    - 제외 조건과 나머지 답변은 그대로 유지한다.
    """
    q_map = _seed_questions(db)

    # 원래 답변: has_soup=soup_yes, cooking_style=rice, main_ingredient=meat -> 0종
    # 1) has_soup만 any로 바꾸면 (has_soup=False, rice, beef): 6종 (상한 5종) + 같은 종류 중복 상품 2개 + 알레르기 제외 1개
    for idx in range(6):
        kind = f"덮밥종류{idx}"
        for prod in range(2):
            _add_menu(
                db,
                food_code=f"SOUP_FLIP_{idx}_{prod}",
                name=f"{kind}_상품{prod}",
                base_name=kind,
                has_soup=False,
                cooking_style="rice",
                main_ingredient="beef",
            )
    _add_menu(
        db,
        food_code="SOUP_FLIP_EXCLUDED",
        name="돼지불고기덮밥",
        base_name="돼지불고기덮밥",
        has_soup=False,
        cooking_style="rice",
        main_ingredient="pork",
        allergen_tags=["돼지고기"],
    )

    # 2) cooking_style만 any로 바꾸면 (has_soup=True, noodle/other, beef): 10종 (상한 5종 -> has_soup와 동률!)
    for idx in range(10):
        kind = f"국수탕종류{idx}"
        _add_menu(
            db,
            food_code=f"STYLE_FLIP_{idx}",
            name=kind,
            base_name=kind,
            has_soup=True,
            cooking_style="noodle" if idx < 5 else "soup_stew",
            main_ingredient="beef",
        )

    # 3) main_ingredient만 any로 바꾸면 (has_soup=True, rice, seafood): 2종 (+ 제외된 새우국밥 1종)
    for idx, kind in enumerate(("해물국밥", "굴국밥")):
        _add_menu(
            db,
            food_code=f"ING_FLIP_{idx}",
            name=kind,
            base_name=kind,
            has_soup=True,
            cooking_style="rice",
            main_ingredient="seafood",
        )
    _add_menu(
        db,
        food_code="ING_FLIP_EXCLUDED",
        name="돼지육수해물국밥",
        base_name="돼지육수해물국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="seafood",
        allergen_tags=["돼지고기"],
    )

    # 두 하드필터를 동시에 바꿔야만 매칭되는 메뉴(has_soup=False, noodle, seafood) -> 단일 변경 시 포함되면 안 됨
    _add_menu(
        db,
        food_code="MULTI_ONLY",
        name="해물볶음면",
        base_name="해물볶음면",
        has_soup=False,
        cooking_style="noodle",
        main_ingredient="seafood",
    )
    db.commit()

    # 답변 순서: main_ingredient(2종) -> has_soup(6종->5) -> flavor_intensity(점수제) -> cooking_style(10종->5)
    answers = [
        AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
        AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
        AnswerItemRequest(question_id=q_map["flavor_intensity"].id, option_id="spicy_bold"),
        AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="rice"),
    ]

    result = suggest_no_result_answer_changes(
        db=db,
        request_answers=answers,
        exclusion_keywords={"allergy_code:pork"},
        all_menus=db.query(Menu).all(),
    )

    assert result.can_review_my_answers is True
    # 6종(cap 5)과 10종(cap 5)은 동률이므로 원래 답변 순서(has_soup 먼저, cooking_style 다음) 유지,
    # 그 다음 2종(main_ingredient). 점수제(flavor_intensity)는 포함되지 않는다.
    assert [
        (item.question_id, item.replacement_option_id, item.display_menu_count)
        for item in result.suggested_answer_changes
    ] == [
        (q_map["has_soup"].id, "any", 5),
        (q_map["cooking_style"].id, "any", 5),
        (q_map["main_ingredient"].id, "any", 2),
    ]
    for item in result.suggested_answer_changes:
        assert item.display_text
        assert "상관없어요" in item.display_text
        assert f"{item.display_menu_count}종" in item.display_text


def test_suggest_returns_empty_when_original_answers_already_have_candidates(db: Session):
    """Q12: 원래 제출 답변으로 후보가 1종 이상 있으면 변경 제안을 반환하지 않는다."""
    q_map = _seed_questions(db)
    _add_menu(
        db,
        food_code="MATCH_01",
        name="소고기국밥",
        base_name="소고기국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="beef",
    )
    _add_menu(
        db,
        food_code="OTHER_01",
        name="비빔밥",
        base_name="비빔밥",
        has_soup=False,
        cooking_style="rice",
        main_ingredient="beef",
    )
    db.commit()

    answers = [
        AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
        AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="rice"),
        AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
    ]
    result = suggest_no_result_answer_changes(
        db=db,
        request_answers=answers,
        exclusion_keywords=set(),
        all_menus=db.query(Menu).all(),
    )
    assert result.suggested_answer_changes == []
    assert result.can_review_my_answers is True


def test_suggest_returns_empty_when_exclusions_leave_zero_available_menus(db: Session):
    """알레르기·제외 조건만으로 후보가 0개이면 하드필터를 바꿔도 제외 조건을 우회할 수 없으므로 빈 목록을 반환한다."""
    q_map = _seed_questions(db)
    _add_menu(
        db,
        food_code="ONLY_PORK",
        name="돼지국밥",
        base_name="돼지국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="pork",
        allergen_tags=["돼지고기"],
    )
    db.commit()

    answers = [
        AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_no"),
        AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="noodle"),
        AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="non_meat"),
    ]
    result = suggest_no_result_answer_changes(
        db=db,
        request_answers=answers,
        exclusion_keywords={"allergy_code:pork"},
        all_menus=db.query(Menu).all(),
    )
    assert result.suggested_answer_changes == []
    assert result.can_review_my_answers is True


def test_suggest_returns_empty_when_no_single_hard_filter_flip_yields_candidates(db: Session):
    """어느 하드필터 하나만 중립으로 바꿔도 후보가 생기지 않으면 빈 목록과 내 답변 다시 보기 가능 플래그를 반환한다."""
    q_map = _seed_questions(db)
    # 세 조건 중 최소 2개를 동시에 바꿔야만 매칭되는 메뉴만 존재
    _add_menu(
        db,
        food_code="TWO_FLIPS",
        name="해물볶음면",
        base_name="해물볶음면",
        has_soup=False,
        cooking_style="noodle",
        main_ingredient="seafood",
    )
    db.commit()

    answers = [
        AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
        AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="rice"),
        AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
    ]
    result = suggest_no_result_answer_changes(
        db=db,
        request_answers=answers,
        exclusion_keywords=set(),
        all_menus=db.query(Menu).all(),
    )
    assert result.suggested_answer_changes == []
    assert result.can_review_my_answers is True


def test_suggest_respects_question_role_overrides_and_skips_already_neutral_answers(db: Session):
    """질문 ID별 역할 오버라이드를 반영하고, 이미 'any'로 답한 하드필터는 제안하지 않는다."""
    q_map = _seed_questions(db)
    # flavor_intensity를 hard_filter로, has_soup를 score로 오버라이드
    # 메뉴는 mild_clean + rice + beef (has_soup=False)
    _add_menu(
        db,
        food_code="OVR_01",
        name="소고기불고기덮밥",
        base_name="불고기덮밥",
        has_soup=False,
        cooking_style="rice",
        main_ingredient="beef",
        flavor_intensity="mild_clean",
    )
    db.commit()

    role_overrides = {
        q_map["has_soup"].id: "score",
        q_map["flavor_intensity"].id: "hard_filter",
    }
    answers = [
        AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
        AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="any"),
        AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
        AnswerItemRequest(question_id=q_map["flavor_intensity"].id, option_id="spicy_bold"),
    ]
    result = suggest_no_result_answer_changes(
        db=db,
        request_answers=answers,
        exclusion_keywords=set(),
        all_menus=db.query(Menu).all(),
        question_role_overrides=role_overrides,
    )
    assert [
        (item.question_id, item.replacement_option_id, item.display_menu_count)
        for item in result.suggested_answer_changes
    ] == [
        (q_map["flavor_intensity"].id, "any", 1),
    ]
    assert "매운맛" in result.suggested_answer_changes[0].display_text


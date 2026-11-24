"""Spec #77 / Tickets #84·#86: 후보 0건일 때 단일 취향 하드필터 답변 변경 제안 모듈.

개인 주문서(#84)와 같이 고르기(#86)에서 동일한 시뮬레이션·정렬·표시 규칙을
읽기 전용으로 재사용할 수 있도록 분리한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.menus.models import Menu, OrderSheetQuestion, PersonalOrderSheetQuestionAssignment
from app.recommend.common import (
    NoAvailableFoodException,
    collect_user_exclusion_keywords,
    filter_available_menus,
)
from app.recommend.order_sheet import (
    _find_valid_order_sheet_issuance,
    _question_options,
    validate_order_sheet_answers,
)
from app.recommend.question_pool import get_question_display
from app.recommend.ranking import (
    _is_neutral_option,
    evaluate_order_sheet_candidates,
    resolve_question_role,
)
from app.recommend.schemas import (
    OrderSheetNoResultsResponse,
    OrderSheetRecommendRequest,
    SuggestedAnswerChangeDTO,
)
from app.users.models import User

CLUSTER_TOPIC_LABELS: dict[str, str] = {
    "has_soup": "국물 유무",
    "cooking_style": "밥·면·그 외",
    "main_ingredient": "고기 요리 여부",
    "sodium_level": "간의 세기",
    "heaviness": "가벼움·든든함",
    "flavor_intensity": "매운맛",
    "origin_type": "집밥·외식 스타일",
    "carb_level": "저탄수화물",
    "protein_level": "고단백",
    "sugar_level": "당류",
    "temperature": "음식 온도",
    "meal_type": "식사 유형",
}


def _find_neutral_option(question: OrderSheetQuestion) -> dict[str, Any] | None:
    options = _question_options(question)
    for opt in options:
        if opt.get("option_id") == "any" and not opt.get("rules"):
            return opt
    for opt in options:
        if _is_neutral_option(opt):
            return opt
    return None


def _build_suggestion_display_text(
    question: OrderSheetQuestion,
    neutral_option: Mapping[str, Any],
    display_menu_count: int,
) -> str:
    topic_label = CLUSTER_TOPIC_LABELS.get(question.cluster_key)
    if not topic_label:
        options = _question_options(question)
        display = get_question_display(question.question_code, question.cluster_key, options)
        raw_text = (display["question_text"] if display else question.question_text) or ""
        topic_label = raw_text.rstrip("?？").strip() or f"질문 {question.id}"

    neutral_label = str(neutral_option.get("label") or "상관없어요").strip() or "상관없어요"
    return f"{topic_label} 답변을 '{neutral_label}'로 바꾸면 {display_menu_count}종 추천"


def suggest_no_result_answer_changes(
    db: Session,
    request_answers: Sequence[Any],
    exclusion_keywords: set[str] | Sequence[str],
    all_menus: Sequence[Menu] | None = None,
    *,
    question_role_overrides: Mapping[int | str, str] | None = None,
    questions_snapshot: list[dict] | None = None,
    max_display_count: int | None = None,
) -> OrderSheetNoResultsResponse:
    """
    Spec #77 §2 / Q12·Q27:
    - 원래 제출 답변과 알레르기·제외 조건을 기준으로 후보 음식 종류가 0종일 때만 제안을 계산한다.
    - 적용 중인 취향 하드필터 답변을 하나씩 독립적으로 중립('상관없어요')으로 바꿔 시뮬레이션한다.
    - 매번 나머지 답변과 알레르기·제외 조건은 고정한다.
    - 변경 후 실제 후보가 1종 이상 생기는 질문만 포함하고, 표시 가능 수(상한 max_display_count, 기본 5)
      내림차순, 동률 시 원래 답변 순서로 정렬한다.
    - DB 상태를 변경하지 않는 순수 읽기 동작이다.
    """
    validated_answers = validate_order_sheet_answers(
        db, list(request_answers), questions_snapshot=questions_snapshot,
    )

    normalized_exclusions = {
        str(kw).strip().lower()
        for kw in exclusion_keywords
        if kw is not None and str(kw).strip()
    }
    effective_max_display = max(
        1,
        int(max_display_count)
        if max_display_count is not None
        else int(settings.ORDER_SHEET_MAX_RECOMMENDATIONS),
    )

    menus = list(all_menus) if all_menus is not None else db.query(Menu).all()
    try:
        available_menus, _ = filter_available_menus(menus, normalized_exclusions)
    except NoAvailableFoodException:
        return OrderSheetNoResultsResponse(
            suggested_answer_changes=[],
            can_review_my_answers=True,
        )

    baseline = evaluate_order_sheet_candidates(
        menus=available_menus,
        exclusion_keywords=set(),
        validated_answers=validated_answers,
        top_k=effective_max_display,
        role_overrides=question_role_overrides,
    )
    if len(baseline.surviving_kind_representatives) >= 1:
        return OrderSheetNoResultsResponse(
            suggested_answer_changes=[],
            can_review_my_answers=True,
        )

    ranked_suggestions: list[tuple[int, int, SuggestedAnswerChangeDTO]] = []
    for original_idx, (question, option) in enumerate(validated_answers):
        role = resolve_question_role(
            question.id,
            question.cluster_key,
            role_overrides=question_role_overrides,
        )
        if role != "hard_filter" or _is_neutral_option(option):
            continue

        neutral_option = _find_neutral_option(question)
        if neutral_option is None:
            continue

        simulated_answers = list(validated_answers)
        simulated_answers[original_idx] = (question, neutral_option)

        sim_eval = evaluate_order_sheet_candidates(
            menus=available_menus,
            exclusion_keywords=set(),
            validated_answers=simulated_answers,
            top_k=effective_max_display,
            role_overrides=question_role_overrides,
        )
        surviving_kind_count = len(sim_eval.surviving_kind_representatives)
        if surviving_kind_count < 1:
            continue

        display_menu_count = min(surviving_kind_count, effective_max_display)
        dto = SuggestedAnswerChangeDTO(
            question_id=int(question.id),
            replacement_option_id=str(neutral_option["option_id"]),
            display_text=_build_suggestion_display_text(
                question=question,
                neutral_option=neutral_option,
                display_menu_count=display_menu_count,
            ),
            display_menu_count=display_menu_count,
        )
        ranked_suggestions.append((display_menu_count, original_idx, dto))

    ranked_suggestions.sort(key=lambda item: (-item[0], item[1]))
    return OrderSheetNoResultsResponse(
        suggested_answer_changes=[item[2] for item in ranked_suggestions],
        can_review_my_answers=True,
    )


def get_personal_order_sheet_no_results_suggestions(
    db: Session,
    request: OrderSheetRecommendRequest,
    user_id: int | None = None,
    current_user: User | None = None,
    *,
    fixed_exclusion_keywords: set[str] | Sequence[str] | None = None,
) -> OrderSheetNoResultsResponse:
    """
    개인 주문서 후보 0건 답변 변경 제안 조회 서비스 (읽기 전용).
    Recommendation, OrderSheetIssuance, QuestionSelectionState 등 어떤 테이블도 수정하지 않는다.
    """
    effective_user_id = current_user.id if current_user is not None else user_id

    if fixed_exclusion_keywords is not None:
        exclusion_keywords = {
            str(kw).strip().lower()
            for kw in fixed_exclusion_keywords
            if kw is not None and str(kw).strip()
        }
    else:
        exclusion_keywords = collect_user_exclusion_keywords(
            db=db,
            user_id=effective_user_id,
            extra_exclusions=request.exclusions,
        )

    issuance = _find_valid_order_sheet_issuance(
        db=db,
        order_sheet_id=request.order_sheet_id,
        user_id=effective_user_id,
    )
    frozen_role_overrides = (
        issuance.question_roles_json
        if issuance is not None
        and isinstance(issuance.question_roles_json, dict)
        and issuance.question_roles_json
        else None
    )
    questions_snapshot = None
    if issuance is not None and db.get(PersonalOrderSheetQuestionAssignment, issuance.id) is not None:
        questions_snapshot = issuance.questions_snapshot_json

    max_display = min(int(request.top_k), int(settings.ORDER_SHEET_MAX_RECOMMENDATIONS))
    return suggest_no_result_answer_changes(
        db=db,
        request_answers=request.answers,
        exclusion_keywords=exclusion_keywords,
        question_role_overrides=frozen_role_overrides,
        questions_snapshot=questions_snapshot,
        max_display_count=max_display,
    )

"""Spec #77 주문서 추천 후보 필터링·점수화·음식 종류 중복 제거·결정적 순위 계산 모듈.

하위 티켓(#82 최근 식사 감점, #83 장기 취향 보정, #84 후보 0건 제안, #85 같이 고르기,
#87 하드필터 추적 기록, #88 과거 첫 목록 재현)이 단일 평가 경계를 재사용할 수 있도록
DB I/O 없는 순수 평가 파이프라인으로 분리한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.menus.food_kinds import classify_food_kind
from app.menus.models import Menu, OrderSheetQuestion
from app.recommend.common import evaluate_option_rules, filter_available_menus


@dataclass(frozen=True)
class HardFilterTraceStep:
    step_order: int
    question_id: int
    question_code: str
    cluster_key: str
    option_id: str
    before_count: int
    after_count: int
    before_kind_count: int
    after_kind_count: int


@dataclass(frozen=True)
class RankedCandidate:
    menu: Menu
    food_kind_key: str
    food_kind_id: int | None
    rule_score: float
    long_term_score: float
    recent_meal_penalty: float
    final_score: float
    matched_question_codes: tuple[str, ...]
    question_scores: dict[int, float]


@dataclass(frozen=True)
class OrderSheetEvaluationResult:
    available_count: int
    excluded_count: int
    surviving_candidates: list[RankedCandidate]
    surviving_kind_representatives: list[RankedCandidate]
    selected_candidates: list[RankedCandidate]
    hard_filter_traces: list[HardFilterTraceStep]
    applied_hard_filter_question_ids: list[int]
    score_question_ids: list[int]
    ranking_rule_version: str


def resolve_question_role(
    question_id: int | None,
    cluster_key: str,
    *,
    role_overrides: Mapping[int | str, str] | None = None,
) -> str:
    """질문 ID별 역할 재정의와 기본 서버 설정을 기준으로 'hard_filter' 또는 'score'를 반환한다."""
    if role_overrides and question_id is not None:
        explicit = role_overrides.get(question_id)
        if explicit is None:
            explicit = role_overrides.get(str(question_id))
        if explicit in ("hard_filter", "score"):
            return explicit

    overrides = settings.ORDER_SHEET_QUESTION_ROLE_OVERRIDES or {}
    if question_id is not None:
        override = overrides.get(question_id)
        if override is None:
            override = overrides.get(str(question_id))
        if override in ("hard_filter", "score"):
            return override

    if cluster_key in set(settings.ORDER_SHEET_HARD_FILTER_CLUSTERS):
        return "hard_filter"
    return "score"


def resolve_score_weight(
    question_id: int | None,
    cluster_key: str,
    question_code: str | None = None,
    *,
    score_weights: Mapping[int | str, float] | None = None,
) -> float:
    """점수제 질문별 가중치를 반환한다. 기본값은 settings.ORDER_SHEET_DEFAULT_SCORE_WEIGHT(1.0)이다."""
    for source in (score_weights, settings.ORDER_SHEET_SCORE_WEIGHTS):
        if not source:
            continue
        if question_id is not None:
            if question_id in source:
                return float(source[question_id])
            if str(question_id) in source:
                return float(source[str(question_id)])
        if question_code and question_code in source:
            return float(source[question_code])
        if cluster_key in source:
            return float(source[cluster_key])
    return float(settings.ORDER_SHEET_DEFAULT_SCORE_WEIGHT)


def resolve_menu_food_kind_key(menu: Menu) -> str:
    """메뉴의 정규화된 음식 종류 키(FoodKind.kind_key와 동일 규칙)를 반환한다."""
    raw_base = (getattr(menu, "base_name", None) or "").strip()
    raw_name = (getattr(menu, "name", None) or "").strip()
    if raw_base or raw_name:
        kind_key, _ = classify_food_kind(base_name=raw_base or None, name=raw_name or None)
        return kind_key
    fk_id = getattr(menu, "food_kind_id", None)
    if fk_id is not None:
        return f"kind_id:{fk_id}"
    return f"menu:{getattr(menu, 'id', id(menu))}"


def _dedup_kind_key(menu: Menu) -> str:
    fk_id = getattr(menu, "food_kind_id", None)
    if fk_id is not None:
        return f"kind_id:{fk_id}"
    return f"kind_key:{resolve_menu_food_kind_key(menu)}"


def _is_neutral_option(option: Mapping[str, Any]) -> bool:
    if option.get("option_id") == "any":
        return True
    rules = option.get("rules")
    return not rules


def _lookup_long_term_score(
    menu: Menu,
    food_kind_key: str,
    scores: Mapping[str | int, float] | None,
) -> float:
    if not scores:
        return 0.0
    if food_kind_key in scores:
        return float(scores[food_kind_key])
    fk_id = getattr(menu, "food_kind_id", None)
    if fk_id is not None:
        if fk_id in scores:
            return float(scores[fk_id])
        fk_key = f"kind_id:{fk_id}"
        if fk_key in scores:
            return float(scores[fk_key])
    return 0.0


def _candidate_sort_key(candidate: RankedCandidate) -> tuple[float, str, int]:
    return (
        -candidate.final_score,
        candidate.menu.food_code or "",
        int(candidate.menu.id or 0),
    )


def evaluate_order_sheet_candidates(
    *,
    menus: Sequence[Menu],
    exclusion_keywords: set[str],
    validated_answers: Sequence[tuple[OrderSheetQuestion, Mapping[str, Any]]],
    top_k: int = 5,
    role_overrides: Mapping[int | str, str] | None = None,
    score_weights: Mapping[int | str, float] | None = None,
    long_term_preference_scores: Mapping[str | int, float] | None = None,
    recent_meal_penalties: Mapping[int, float] | None = None,
) -> OrderSheetEvaluationResult:
    """
    Spec #77 첫 메뉴 추천 순위 평가 파이프라인:
      0. 알레르기·제외 재료를 먼저 적용한다. 0개면 NoAvailableFoodException을 발생시킨다.
      1. 역할이 hard_filter인 비-중립 답변만 순서대로 엄격 적용한다(미확인=탈락, 자동 완화 없음).
         각 적용 단계의 전후 상품·음식 종류 수를 hard_filter_traces에 기록한다.
      2. 역할이 score인 답변에 서버 설정 가중치(초기 1.0)를 적용해 점수를 합산하고,
         장기 취향 보정(#83)과 최근 식사 감점(#82)을 더해 결정적으로 정렬한다.
      3. 음식 종류별로 최상위 상품 1개를 대표로 뽑아 0종 -> 0개, 1~5종 -> 전부,
         6종 이상 -> min(top_k, ORDER_SHEET_MAX_RECOMMENDATIONS)개를 반환한다.
    """
    available_menus, excluded_count = filter_available_menus(list(menus), exclusion_keywords)

    hard_filter_answers: list[tuple[OrderSheetQuestion, Mapping[str, Any]]] = []
    score_answers: list[tuple[OrderSheetQuestion, Mapping[str, Any]]] = []
    for question, option in validated_answers:
        role = resolve_question_role(
            question.id,
            question.cluster_key,
            role_overrides=role_overrides,
        )
        if role == "hard_filter":
            hard_filter_answers.append((question, option))
        else:
            score_answers.append((question, option))

    filtered_menus = list(available_menus)
    hard_filter_traces: list[HardFilterTraceStep] = []
    applied_hard_filter_codes: list[str] = []
    applied_hard_filter_question_ids: list[int] = []

    step_order = 0
    for question, option in hard_filter_answers:
        if _is_neutral_option(option):
            continue
        rules = list(option.get("rules") or [])
        before_count = len(filtered_menus)
        before_kind_count = len({_dedup_kind_key(m) for m in filtered_menus})

        filtered_menus = [m for m in filtered_menus if evaluate_option_rules(m, rules)]

        after_count = len(filtered_menus)
        after_kind_count = len({_dedup_kind_key(m) for m in filtered_menus})
        step_order += 1
        hard_filter_traces.append(
            HardFilterTraceStep(
                step_order=step_order,
                question_id=int(question.id),
                question_code=question.question_code,
                cluster_key=question.cluster_key,
                option_id=str(option.get("option_id", "")),
                before_count=before_count,
                after_count=after_count,
                before_kind_count=before_kind_count,
                after_kind_count=after_kind_count,
            )
        )
        applied_hard_filter_codes.append(question.question_code)
        applied_hard_filter_question_ids.append(int(question.id))

    score_question_ids = [int(q.id) for q, _ in score_answers]
    score_specs: list[tuple[OrderSheetQuestion, list[dict[str, Any]], float, bool]] = []
    for question, option in score_answers:
        is_neutral = _is_neutral_option(option)
        rules = [] if is_neutral else list(option.get("rules") or [])
        weight = (
            0.0
            if is_neutral
            else resolve_score_weight(
                question.id,
                question.cluster_key,
                question.question_code,
                score_weights=score_weights,
            )
        )
        score_specs.append((question, rules, weight, is_neutral))

    surviving_candidates: list[RankedCandidate] = []
    for menu in filtered_menus:
        rule_score = 0.0
        question_scores: dict[int, float] = {}
        matched_codes = list(applied_hard_filter_codes)

        for question, rules, weight, is_neutral in score_specs:
            qid = int(question.id)
            if is_neutral or not rules:
                question_scores[qid] = 0.0
                continue
            if evaluate_option_rules(menu, rules):
                contribution = round(float(weight), 6)
                rule_score += contribution
                question_scores[qid] = contribution
                matched_codes.append(question.question_code)
            else:
                question_scores[qid] = 0.0

        food_kind_key = resolve_menu_food_kind_key(menu)
        long_term_score = round(
            _lookup_long_term_score(menu, food_kind_key, long_term_preference_scores),
            6,
        )
        recent_meal_penalty = round(
            float((recent_meal_penalties or {}).get(int(menu.id), 0.0))
            if getattr(menu, "id", None) is not None
            else 0.0,
            6,
        )
        rule_score = round(rule_score, 6)
        final_score = round(rule_score + long_term_score + recent_meal_penalty, 6)

        surviving_candidates.append(
            RankedCandidate(
                menu=menu,
                food_kind_key=food_kind_key,
                food_kind_id=getattr(menu, "food_kind_id", None),
                rule_score=rule_score,
                long_term_score=long_term_score,
                recent_meal_penalty=recent_meal_penalty,
                final_score=final_score,
                matched_question_codes=tuple(matched_codes),
                question_scores=question_scores,
            )
        )

    surviving_candidates.sort(key=_candidate_sort_key)

    seen_kinds: set[str] = set()
    surviving_kind_representatives: list[RankedCandidate] = []
    for candidate in surviving_candidates:
        dedup_key = _dedup_kind_key(candidate.menu)
        if dedup_key in seen_kinds:
            continue
        seen_kinds.add(dedup_key)
        surviving_kind_representatives.append(candidate)

    max_limit = min(int(top_k), int(settings.ORDER_SHEET_MAX_RECOMMENDATIONS))
    selected_candidates = surviving_kind_representatives[:max_limit]

    return OrderSheetEvaluationResult(
        available_count=len(available_menus),
        excluded_count=excluded_count,
        surviving_candidates=surviving_candidates,
        surviving_kind_representatives=surviving_kind_representatives,
        selected_candidates=selected_candidates,
        hard_filter_traces=hard_filter_traces,
        applied_hard_filter_question_ids=applied_hard_filter_question_ids,
        score_question_ids=score_question_ids,
        ranking_rule_version=str(settings.ORDER_SHEET_RANKING_RULE_VERSION),
    )

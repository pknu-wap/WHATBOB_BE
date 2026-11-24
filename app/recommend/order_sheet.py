from dataclasses import dataclass
from datetime import datetime, timezone
import math
import random
from typing import Any
from uuid import uuid4
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.operations import (
    acquire_or_inspect_operation,
    compute_payload_hash,
    get_user_session_id,
    lock_user_and_verify_session,
    normalize_idempotency_key,
)
from app.auth.sessions import utc_now
from app.core.config import settings
from app.menus.display import format_menu_name
from app.menus.food_kinds import classify_food_kind
from app.menus.models import (
    Menu,
    OrderSheetIssuance,
    OrderSheetQuestion,
    QuestionSelectionState,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
    RecommendationItemSnapshot,
)
from app.recommend.common import (
    InvalidOrderSheetException,
    QuestionPoolEmptyException,
    RecommendationDomainException,
    RecommendationNotFoundException,
    collect_user_exclusion_keywords,
    evaluate_option_rules,
    filter_available_menus,
    is_menu_excluded,
)
from app.recommend.personal_model import FEATURE_SCHEMA_VERSION
from app.recommend.schemas import (
    FilterRuleDTO,
    OrderSheetQuestionDTO,
    OrderSheetQuestionsResponse,
    OrderSheetRecommendRequest,
    OrderSheetRecommendResponse,
    OrderSheetWeightTuneRequest,
    OrderSheetWeightTuneResponse,
    QuestionOptionDTO,
    RecommendationFeedbackRequest,
    RecommendationFeedbackResponse,
    RecommendedMenuDTO,
)
from app.recommend.question_pool import (
    COOKING_STYLE_OPTIONS,
    LEGACY_COOKING_STYLE_RULES,
    QUESTION_CODE_PREFIX,
    get_question_display,
)
from app.recommend.variety import menu_group, select_diverse_menus_with_snapshots
from app.users.models import User

DEFAULT_TOTAL_QUESTIONS = 9
DEFAULT_EXPLOITATION_COUNT = 7

# 숫자가 작을수록 먼저 적용되는 하드 필터 우선순위 (후보 부족 시 후순위부터 완화되어 점수제로 전환)
HARD_FILTER_PRIORITY: dict[str, int] = {
    "meal_type": 1,        # 식사 종류 (식사 vs 간식/음료)
    "main_ingredient": 2,  # 주요 재료 (육류 vs 비육류)
    "has_soup": 3,         # 국물 여부
    "cooking_style": 4,    # 조리 방식
}
HARD_FILTER_CLUSTERS = frozenset(HARD_FILTER_PRIORITY.keys())


def resolve_question_role(question_id: int | None, cluster_key: str) -> str:
    """질문 ID별 역할 재정의와 기본 서버 설정을 기준으로 'hard_filter' 또는 'score'를 반환한다."""
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


def _question_options(question: OrderSheetQuestion) -> list[dict[str, Any]]:
    options = list(question.options_json or [])
    # 기존 두 선택지 DB도 재적재 전까지 any를 조회·제출할 수 있다.
    if len(options) == 2 and not any(opt.get("option_id") == "any" for opt in options):
        options.append({"option_id": "any", "label": "상관없어요", "rules": []})
    # 기존 3선택지(rice_noodle/other_style/any) Q8 행도 재적재 없이 4선택지(밥·면·그 외·상관없어요)로 정규화한다.
    if (
        question.cluster_key == "cooking_style"
        and (question.question_code or "").startswith(QUESTION_CODE_PREFIX)
    ):
        actual_rules = {opt.get("option_id"): opt.get("rules", []) for opt in options}
        if actual_rules == LEGACY_COOKING_STYLE_RULES:
            return [
                {
                    "option_id": opt["option_id"],
                    "label": opt["label"],
                    "rules": [dict(rule) for rule in opt["rules"]],
                }
                for opt in COOKING_STYLE_OPTIONS
            ]
    return options


def _to_question_dto(
    question: OrderSheetQuestion,
    is_exploration: bool = False,
) -> OrderSheetQuestionDTO:
    source_options = _question_options(question)
    display = get_question_display(question.question_code, question.cluster_key, source_options)
    labels = {option["option_id"]: option["label"] for option in display["options"]} if display else {}
    options = []
    for opt in source_options:
        rules = [
            FilterRuleDTO(
                field=r["field"],
                op=r.get("op", "eq"),
                value=r.get("value"),
            )
            for r in opt.get("rules", [])
        ]
        options.append(
            QuestionOptionDTO(
                option_id=opt["option_id"],
                label=labels.get(opt["option_id"], opt["label"]),
                rules=rules,
            )
        )
    return OrderSheetQuestionDTO(
        id=question.id,
        question_code=question.question_code,
        cluster_key=question.cluster_key,
        question_text=display["question_text"] if display else question.question_text,
        options=options,
        initial_score=round(float(question.initial_score), 4),
        importance_weight=round(float(question.importance_weight), 4),
        shown_count=int(question.shown_count or 0),
        positive_count=int(question.positive_count or 0),
        is_exploration=is_exploration,
    )


@dataclass(frozen=True)
class QuestionRankingMetrics:
    score: float
    normalized_entropy: float
    coverage_ratio: float
    matched_option_count: int
    is_degenerate: bool
    option_votes: tuple[float, ...]
    total_kind_votes: float


def _menu_food_kind_key(menu: Menu, fallback_index: int) -> str:
    if getattr(menu, "food_kind_id", None) is not None:
        return f"kind_id:{menu.food_kind_id}"
    raw_base = (getattr(menu, "base_name", None) or "").strip()
    raw_name = (getattr(menu, "name", None) or "").strip()
    if raw_base or raw_name:
        kind_key, _ = classify_food_kind(base_name=raw_base or None, name=raw_name or None)
        return f"kind_key:{kind_key}"
    return f"menu_idx:{fallback_index}"


def _build_menu_kind_weights(menus: list[Menu]) -> tuple[list[tuple[Menu, float]], float]:
    kind_keys = [_menu_food_kind_key(menu, idx) for idx, menu in enumerate(menus)]
    kind_counts: dict[str, int] = {}
    for key in kind_keys:
        kind_counts[key] = kind_counts.get(key, 0) + 1
    weighted = [(menu, 1.0 / kind_counts[key]) for menu, key in zip(menus, kind_keys)]
    return weighted, float(len(kind_counts))


def calculate_question_ranking_metrics(
    menus: list[Menu],
    options_json: list[dict[str, Any]],
    *,
    entropy_weight: float | None = None,
    coverage_weight: float | None = None,
    weighted_menus: tuple[list[tuple[Menu, float]], float] | None = None,
) -> QuestionRankingMetrics:
    """
    Spec #77 §1 / Q18·Q19:
    음식 종류마다 총 1표를 해당 종류의 N개 상품에 1/N표씩 배분하고,
    속성 미입력 상품은 분모 N에만 포함(어느 선택지에도 표를 보태지 않음)한 뒤
    정규화 샤논 엔트로피와 커버리지의 가중합을 계산한다.
    한 선택지에만 표가 모이는 퇴화(degenerate) 질문은 제외 대상(score=0.0)으로 표시한다.
    """
    if not menus or not options_json:
        return QuestionRankingMetrics(
            score=0.0,
            normalized_entropy=0.0,
            coverage_ratio=0.0,
            matched_option_count=0,
            is_degenerate=True,
            option_votes=(),
            total_kind_votes=0.0,
        )

    specific_options = [opt for opt in options_json if opt.get("rules")]
    if len(specific_options) < 2:
        return QuestionRankingMetrics(
            score=0.0,
            normalized_entropy=0.0,
            coverage_ratio=0.0,
            matched_option_count=0,
            is_degenerate=True,
            option_votes=(),
            total_kind_votes=0.0,
        )

    menu_weights, total_kind_votes = (
        weighted_menus if weighted_menus is not None else _build_menu_kind_weights(menus)
    )
    if total_kind_votes <= 0.0:
        return QuestionRankingMetrics(
            score=0.0,
            normalized_entropy=0.0,
            coverage_ratio=0.0,
            matched_option_count=0,
            is_degenerate=True,
            option_votes=tuple(0.0 for _ in specific_options),
            total_kind_votes=0.0,
        )

    votes: list[float] = []
    for opt in specific_options:
        rules = opt.get("rules", [])
        vote = sum(weight for menu, weight in menu_weights if evaluate_option_rules(menu, rules))
        votes.append(vote)

    total_matched = sum(votes)
    coverage_ratio = min(1.0, total_matched / total_kind_votes) if total_kind_votes > 0.0 else 0.0
    non_zero_votes = [v for v in votes if v > 0.0]
    matched_option_count = len(non_zero_votes)
    if total_matched <= 0.0 or matched_option_count <= 1:
        return QuestionRankingMetrics(
            score=0.0,
            normalized_entropy=0.0,
            coverage_ratio=coverage_ratio,
            matched_option_count=matched_option_count,
            is_degenerate=True,
            option_votes=tuple(votes),
            total_kind_votes=total_kind_votes,
        )

    probs = [v / total_matched for v in non_zero_votes]
    entropy = -sum(p * math.log2(p) for p in probs)
    max_entropy = math.log2(len(specific_options))
    normalized_entropy = entropy / max_entropy if max_entropy > 0.0 else 0.0

    w_e = settings.ORDER_SHEET_ENTROPY_WEIGHT if entropy_weight is None else float(entropy_weight)
    w_c = settings.ORDER_SHEET_COVERAGE_WEIGHT if coverage_weight is None else float(coverage_weight)
    w_sum = w_e + w_c
    raw_score = (w_e * normalized_entropy + w_c * coverage_ratio) / w_sum if w_sum > 0.0 else 0.0
    score = round(max(0.0, min(1.0, raw_score)), 4)

    return QuestionRankingMetrics(
        score=score,
        normalized_entropy=normalized_entropy,
        coverage_ratio=coverage_ratio,
        matched_option_count=matched_option_count,
        is_degenerate=False,
        option_votes=tuple(votes),
        total_kind_votes=total_kind_votes,
    )


def calculate_question_entropy_score(
    menus: list[Menu],
    options_json: list[dict[str, Any]],
    *,
    entropy_weight: float | None = None,
    coverage_weight: float | None = None,
) -> float:
    """음식 종류별 1/N 투표 기반 정규화 샤논 엔트로피와 커버리지 가중합 점수(0.0~1.0)를 반환한다."""
    metrics = calculate_question_ranking_metrics(
        menus,
        options_json,
        entropy_weight=entropy_weight,
        coverage_weight=coverage_weight,
    )
    return metrics.score


def rank_initial_question_candidates(
    menus: list[Menu],
    raw_questions: list[dict[str, Any]],
    *,
    entropy_weight: float | None = None,
    coverage_weight: float | None = None,
) -> list[dict[str, Any]]:
    """
    초기 질문 후보를 종류별 1/N 정규화 엔트로피·커버리지 순으로 정렬하고,
    한 선택지에만 쏠리는 퇴화 후보는 제외한다.
    """
    weighted_menus = _build_menu_kind_weights(menus)
    ranked: list[dict[str, Any]] = []
    for q in raw_questions:
        metrics = calculate_question_ranking_metrics(
            menus,
            q["options"],
            entropy_weight=entropy_weight,
            coverage_weight=coverage_weight,
            weighted_menus=weighted_menus,
        )
        if metrics.is_degenerate:
            continue
        q_copy = dict(q)
        q_copy["initial_score"] = metrics.score
        q_copy["normalized_entropy"] = round(metrics.normalized_entropy, 6)
        q_copy["coverage_ratio"] = round(metrics.coverage_ratio, 6)
        ranked.append(q_copy)

    ranked.sort(key=lambda x: (-x["initial_score"], x.get("question_code", "")))
    return ranked


def rank_and_score_question_pool(
    menus: list[Menu],
    raw_questions: list[dict[str, Any]],
    *,
    entropy_weight: float | None = None,
    coverage_weight: float | None = None,
    exclude_degenerate: bool = False,
) -> list[dict[str, Any]]:
    """
    후보 질문 풀 전체에 종류별 1/N 정규화 엔트로피·커버리지 점수를 계산하고,
    동일 cluster_key 내 문구 변형에는 감쇠 계수를 적용해 초기 importance_weight를 산출한다.
    """
    weighted_menus = _build_menu_kind_weights(menus)
    metrics_cache: dict[str, QuestionRankingMetrics] = {}
    scored: list[dict[str, Any]] = []
    for q in raw_questions:
        rules_sig = repr([(opt.get("option_id"), opt.get("rules", [])) for opt in q["options"]])
        metrics = metrics_cache.get(rules_sig)
        if metrics is None:
            metrics = calculate_question_ranking_metrics(
                menus,
                q["options"],
                entropy_weight=entropy_weight,
                coverage_weight=coverage_weight,
                weighted_menus=weighted_menus,
            )
            metrics_cache[rules_sig] = metrics
        if exclude_degenerate and metrics.is_degenerate:
            continue
        q_copy = dict(q)
        q_copy["initial_score"] = metrics.score
        scored.append(q_copy)

    scored.sort(key=lambda x: (-x["initial_score"], x.get("question_code", "")))

    cluster_seen_counts: dict[str, int] = {}
    for item in scored:
        cluster = item["cluster_key"]
        rank_in_cluster = cluster_seen_counts.get(cluster, 0)
        cluster_seen_counts[cluster] = rank_in_cluster + 1
        if item["initial_score"] <= 0.0:
            item["importance_weight"] = 0.0
        else:
            diversity_factor = 0.90 ** rank_in_cluster
            item["importance_weight"] = round(max(0.05, item["initial_score"] * diversity_factor), 4)

    scored.sort(key=lambda x: (-x["importance_weight"], x.get("question_code", "")))
    return scored


FIELD_SEMANTIC_AXIS_MAP: dict[str, str] = {
    "has_soup": "axis:has_soup",
    "cooking_style": "axis:cooking_style",
    "main_ingredient": "axis:main_ingredient",
    "flavor_intensity": "axis:flavor_intensity",
    "temperature": "axis:temperature",
    "heaviness": "axis:heaviness",
    "kcal": "axis:heaviness",
    "protein_g": "axis:protein_level",
    "carb_g": "axis:carb_level",
    "sodium_mg": "axis:sodium_level",
    "sugar_g": "axis:sugar_level",
    "origin_type": "axis:origin_type",
    "fat_g": "axis:fat_level",
    "meal_type": "axis:meal_type",
}

TEXT_SEMANTIC_AXIS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("axis:heaviness", ("든든한 식사", "가벼운 식사", "든든하게", "가볍게", "포만감", "칼로리")),
    ("axis:main_ingredient", ("돼지고기", "소고기", "쇠고기", "닭고기", "오리고기", "육류", "고기 요리")),
    ("axis:has_soup", ("국물 있", "국물 없", "국물 요리")),
    ("axis:cooking_style", ("밥이나 면", "밥·면", "면 요리", "밥 요리")),
    ("axis:flavor_intensity", ("매콤", "매운", "얼큰", "담백한 맛")),
    ("axis:sodium_level", ("싱거운", "짭짤", "짠 음식", "나트륨", "간의 세기")),
    ("axis:origin_type", ("집밥", "외식 스타일", "프랜차이즈")),
    ("axis:carb_level", ("저탄수", "탄수화물")),
    ("axis:protein_level", ("고단백", "단백질")),
    ("axis:sugar_level", ("저당", "당이 적", "당류")),
    ("axis:temperature", ("따뜻한 음식", "차가운 음식", "시원한 음식")),
)

EXCLUSION_ALIAS_GROUPS: dict[str, frozenset[str]] = {
    "wheat": frozenset({"allergy_code:wheat", "unable_code:wheat", "wheat", "밀", "밀가루", "글루텐"}),
    "pork": frozenset({"allergy_code:pork", "unable_code:pork", "pork", "돼지고기"}),
    "beef": frozenset({"allergy_code:beef", "unable_code:beef", "beef", "소고기", "쇠고기", "쇠고기(소고기)"}),
    "poultry": frozenset({
        "allergy_code:chicken",
        "unable_code:poultry",
        "chicken",
        "poultry",
        "닭고기",
        "닭·가금류",
        "오리고기",
    }),
    "seafood": frozenset({"unable_code:seafood", "seafood", "해산물", "해산물·어패류"}),
    "egg": frozenset({"allergy_code:egg", "unable_code:egg", "egg", "달걀", "계란", "알류(달걀)"}),
    "dairy": frozenset({"allergy_code:milk", "unable_code:dairy", "milk", "dairy", "우유", "유제품"}),
}


def _extract_excluded_ingredient_groups(exclusion_keywords: set[str]) -> set[str]:
    lowered = {str(kw).strip().lower() for kw in exclusion_keywords if kw and str(kw).strip()}
    excluded: set[str] = set()
    for group_name, aliases in EXCLUSION_ALIAS_GROUPS.items():
        if lowered & {a.lower() for a in aliases}:
            excluded.add(group_name)
    return excluded


def question_semantic_axes(question: OrderSheetQuestion) -> frozenset[str]:
    """
    질문의 규칙 필드·문구를 정규화된 의미 축 집합으로 변환한다.
    문구 재표현, 반대 방향 선호(예: 가벼움 vs 든든함), 좁은 하위 주제(예: 고기 vs 돼지고기)는
    동일한 의미 축을 공유하므로 한 주문서 안에 동시에 들어가지 않는다.
    """
    axes: set[str] = set()
    cluster = (question.cluster_key or "").strip()
    if cluster:
        axes.add(f"cluster:{cluster}")

    options = _question_options(question)
    display = get_question_display(question.question_code, question.cluster_key, options)
    effective_text = (display["question_text"] if display else question.question_text) or ""

    for opt in options:
        for rule in opt.get("rules", []) or []:
            field = str(rule.get("field") or "").strip()
            axis = FIELD_SEMANTIC_AXIS_MAP.get(field)
            if axis:
                axes.add(axis)
            elif field in ("name", "base_name", "category_large", "category_medium"):
                val_text = str(rule.get("value") or "")
                for hint_axis, tokens in TEXT_SEMANTIC_AXIS_HINTS:
                    if any(tok in val_text for tok in tokens):
                        axes.add(hint_axis)

    for hint_axis, tokens in TEXT_SEMANTIC_AXIS_HINTS:
        if any(tok in effective_text for tok in tokens):
            axes.add(hint_axis)

    return frozenset(axes)


def is_question_inappropriate_for_exclusions(
    question: OrderSheetQuestion,
    exclusion_keywords: set[str],
) -> bool:
    """사용자 알레르기·제외 재료 때문에 묻기 부적절한 질문인지 판정한다."""
    if not exclusion_keywords:
        return False
    excluded_groups = _extract_excluded_ingredient_groups(exclusion_keywords)
    if not excluded_groups:
        return False

    options = _question_options(question)
    specific_rules = [
        rule
        for opt in options
        for rule in (opt.get("rules") or [])
    ]

    # 밀 알레르기·제외 시 밥·면·그 외(cooking_style) 질문을 생략한다.
    if "wheat" in excluded_groups:
        if question.cluster_key == "cooking_style":
            return True
        for rule in specific_rules:
            if rule.get("field") == "cooking_style":
                val = rule.get("value")
                values = val if isinstance(val, (list, tuple, set)) else [val]
                if any(v in ("noodle", "bakery") for v in values):
                    return True

    # 주재료 질문의 대상 재료가 모두 사용자 제외 대상이면 생략한다.
    for rule in specific_rules:
        if rule.get("field") != "main_ingredient":
            continue
        op = rule.get("op", "eq")
        val = rule.get("value")
        if op == "eq" and isinstance(val, str):
            target_set = {val}
        elif op == "in" and isinstance(val, (list, tuple, set)):
            target_set = {str(v) for v in val}
        else:
            continue
        if not target_set:
            continue
        if target_set == {"egg_dairy"} and {"egg", "dairy"}.issubset(excluded_groups):
            return True
        if target_set.issubset(excluded_groups):
            return True

    return False


def _rules_signature(options: list[dict[str, Any]]) -> str:
    return repr([(opt.get("option_id"), opt.get("rules", [])) for opt in options])


def select_order_sheet_questions(
    db: Session,
    total_count: int | None = None,
    exploitation_count: int | None = None,
    random_seed: int | None = None,
    scope_key: str | None = None,
    *,
    user_id: int | None = None,
    exclusion_keywords: set[str] | list[str] | None = None,
    commit: bool = True,
) -> OrderSheetQuestionsResponse:
    """
    Spec #77 주문서 질문 선별:
    - 기본 문항 수는 settings.ORDER_SHEET_QUESTION_COUNT(9)에서 관리한다.
    - 사용자 알레르기·제외 조건에 부적절한 질문, 남은 후보 메뉴를 구분하지 못하는 퇴화 질문,
      이미 선택된 질문과 의미가 겹치는 질문(재표현·반대 방향·하위 주제)을 제외한다.
    - exploitation_count 미지정 시 Spec #77 기본 9개 주제(Q1~Q9)를 우선 채우고,
      빈자리는 남은 후보를 잘 구분하는 상위 후보(종류별 1/N 엔트로피·커버리지 순)로 채운다.
    - commit=False 인 경우 상위 트랜잭션을 조기 커밋하지 않고 flush 만 수행한다.
    """
    target_total = (
        int(total_count)
        if total_count is not None
        else int(settings.ORDER_SHEET_QUESTION_COUNT)
    )
    active_questions = (
        db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.is_active.is_(True))
        .order_by(OrderSheetQuestion.importance_weight.desc(), OrderSheetQuestion.id.asc())
        .all()
    )
    if not active_questions:
        raise QuestionPoolEmptyException()

    if exclusion_keywords is not None:
        effective_exclusions = {
            str(kw).strip()
            for kw in exclusion_keywords
            if kw is not None and str(kw).strip()
        }
    elif user_id is not None:
        effective_exclusions = collect_user_exclusion_keywords(db=db, user_id=user_id)
    else:
        effective_exclusions = set()

    all_menus = db.query(Menu).all()
    available_menus: list[Menu] = []
    weighted_menus: tuple[list[tuple[Menu, float]], float] | None = None
    if all_menus:
        available_menus = [
            menu for menu in all_menus if not is_menu_excluded(menu, effective_exclusions)
        ]
        if len(available_menus) >= 2:
            weighted_menus = _build_menu_kind_weights(available_menus)

    metrics_cache: dict[str, QuestionRankingMetrics] = {}
    valid_candidates: list[tuple[OrderSheetQuestion, frozenset[str], float]] = []
    non_inappropriate_fallback: list[tuple[OrderSheetQuestion, frozenset[str], float]] = []
    for q in active_questions:
        if is_question_inappropriate_for_exclusions(q, effective_exclusions):
            continue
        axes = question_semantic_axes(q)
        options = _question_options(q)
        non_inappropriate_fallback.append((q, axes, float(q.importance_weight or 0.0)))
        if weighted_menus is not None:
            sig = _rules_signature(options)
            metrics = metrics_cache.get(sig)
            if metrics is None:
                metrics = calculate_question_ranking_metrics(
                    available_menus,
                    options,
                    weighted_menus=weighted_menus,
                )
                metrics_cache[sig] = metrics
            if metrics.is_degenerate:
                continue
            candidate_score = metrics.score
        else:
            candidate_score = float(q.importance_weight or 0.0)
        valid_candidates.append((q, axes, candidate_score))

    if not valid_candidates and non_inappropriate_fallback:
        valid_candidates = non_inappropriate_fallback

    selected_exploit: list[OrderSheetQuestion] = []
    selected_explore: list[OrderSheetQuestion] = []
    used_axes: set[str] = set()

    if exploitation_count is None:
        # Spec #77 기본 개인 주문서: 설정된 초기 자리(Q1~Q9)를 우선 배치하고,
        # 생략·결원 자리는 남은 후보를 가장 잘 구분하는 비겹침 주제로 채운다.
        by_cluster: dict[str, list[tuple[OrderSheetQuestion, frozenset[str], float]]] = {}
        for item in valid_candidates:
            by_cluster.setdefault(item[0].cluster_key, []).append(item)

        for cluster_key in settings.ORDER_SHEET_INITIAL_CLUSTERS:
            if len(selected_exploit) >= target_total:
                break
            for q, axes, _ in by_cluster.get(cluster_key, ()):
                if axes & used_axes:
                    continue
                selected_exploit.append(q)
                used_axes.update(axes)
                break

        if len(selected_exploit) < target_total:
            remaining = sorted(
                valid_candidates,
                key=lambda item: (
                    -item[2],
                    -float(item[0].importance_weight or 0.0),
                    item[0].id,
                ),
            )
            for q, axes, _ in remaining:
                if len(selected_exploit) >= target_total:
                    break
                if axes & used_axes:
                    continue
                selected_exploit.append(q)
                used_axes.update(axes)

        if scope_key is not None:
            state = (
                db.query(QuestionSelectionState)
                .filter(QuestionSelectionState.scope_key == scope_key)
                .with_for_update()
                .first()
            )
            if state is None:
                state = QuestionSelectionState(scope_key=scope_key)
                db.add(state)
            topic_counts = dict(state.topic_counts or {})
            for q in selected_exploit:
                topic_counts[q.cluster_key] = topic_counts.get(q.cluster_key, 0) + 1
            state.last_clusters = [q.cluster_key for q in selected_exploit]
            state.topic_counts = topic_counts
            if commit:
                db.commit()
            else:
                db.flush()
    else:
        actual_exploit = min(int(exploitation_count), target_total)
        for q, axes, _ in valid_candidates:
            if len(selected_exploit) >= actual_exploit:
                break
            if axes & used_axes:
                continue
            selected_exploit.append(q)
            used_axes.update(axes)

        remaining_pool = [
            (q, axes, score)
            for q, axes, score in valid_candidates
            if not (axes & used_axes)
        ]
        actual_explore = max(0, target_total - len(selected_exploit))
        state = None
        topic_counts: dict[str, int] = {}
        last_clusters: set[str] = set()
        if scope_key is not None:
            state = (
                db.query(QuestionSelectionState)
                .filter(QuestionSelectionState.scope_key == scope_key)
                .with_for_update()
                .first()
            )
            if state is None:
                state = QuestionSelectionState(scope_key=scope_key)
                db.add(state)
            topic_counts = dict(state.topic_counts or {})
            last_clusters = set(state.last_clusters or [])

        if actual_explore > 0 and remaining_pool:
            rng = random.Random(random_seed)
            pool_copy = list(remaining_pool)
            while len(selected_explore) < actual_explore and pool_copy:
                fresh = [item for item in pool_copy if item[0].cluster_key not in last_clusters]
                eligible = fresh or pool_copy
                if state is not None:
                    least_count = min(topic_counts.get(item[0].cluster_key, 0) for item in eligible)
                    eligible = [
                        item
                        for item in eligible
                        if topic_counts.get(item[0].cluster_key, 0) == least_count
                    ]
                weights = [
                    (float(item[0].importance_weight) + 0.3)
                    / math.sqrt(float(item[0].shown_count or 0) + 1.0)
                    for item in eligible
                ]
                chosen_q, chosen_axes, _ = rng.choices(eligible, weights=weights, k=1)[0]
                selected_explore.append(chosen_q)
                topic_counts[chosen_q.cluster_key] = topic_counts.get(chosen_q.cluster_key, 0) + 1
                used_axes.update(chosen_axes)
                pool_copy = [item for item in pool_copy if not (item[1] & used_axes)]

        if state is not None:
            state.last_clusters = [q.cluster_key for q in selected_explore]
            state.topic_counts = topic_counts
            if commit:
                db.commit()
            else:
                db.flush()

    dtos = [
        _to_question_dto(q, is_exploration=False) for q in selected_exploit
    ] + [
        _to_question_dto(q, is_exploration=True) for q in selected_explore
    ]

    return OrderSheetQuestionsResponse(
        questions=dtos,
        total_count=len(dtos),
        exploitation_count=len(selected_exploit),
        exploration_count=len(selected_explore),
    )


def _issuance_to_response(issuance: OrderSheetIssuance) -> OrderSheetQuestionsResponse:
    questions = [
        OrderSheetQuestionDTO(**item)
        for item in (issuance.questions_snapshot_json or [])
    ]
    return OrderSheetQuestionsResponse(
        order_sheet_id=issuance.order_sheet_id,
        questions=questions,
        total_count=int(issuance.total_count),
        exploitation_count=int(issuance.exploitation_count),
        exploration_count=int(issuance.exploration_count),
    )


def issue_order_sheet_questions(
    db: Session,
    *,
    user_id: int | None = None,
    total_count: int | None = None,
    exploitation_count: int | None = None,
    scope_key: str | None = None,
    idempotency_key: str | None = None,
    issuance_key: str | None = None,
    commit: bool = True,
) -> OrderSheetQuestionsResponse:
    """
    Spec #77 §6 #78:
    개인 주문서 진입 시 새 주문서를 발급(OrderSheetIssuance 저장)한다.
    동일한 Idempotency-Key(또는 issuance_key)로 재전송되면 최초 발급된 주문서와 질문을 그대로 반환하고,
    새로고침·재입장(키 미전달 또는 새 키) 시 새 주문서를 발급한다.
    """
    try:
        norm_header = normalize_idempotency_key(idempotency_key)
        norm_query = normalize_idempotency_key(issuance_key)
    except ValueError as exc:
        raise RecommendationDomainException(str(exc), status_code=400) from exc

    if norm_header is not None and norm_query is not None and norm_header != norm_query:
        raise InvalidOrderSheetException(
            "Idempotency-Key 헤더와 issuance_key 파라미터가 일치하지 않습니다."
        )
    effective_key = norm_header or norm_query

    if effective_key is not None and user_id is not None:
        existing = (
            db.query(OrderSheetIssuance)
            .filter(
                OrderSheetIssuance.user_id == user_id,
                OrderSheetIssuance.issuance_key == effective_key,
            )
            .first()
        )
        if existing is not None:
            return _issuance_to_response(existing)

    effective_total = (
        int(total_count)
        if total_count is not None
        else int(settings.ORDER_SHEET_QUESTION_COUNT)
    )
    effective_exploit = (
        min(int(exploitation_count), effective_total)
        if exploitation_count is not None
        else None
    )

    selection = select_order_sheet_questions(
        db=db,
        total_count=effective_total,
        exploitation_count=effective_exploit,
        scope_key=scope_key,
        user_id=user_id,
        commit=False,
    )

    issuance = OrderSheetIssuance(
        order_sheet_id=f"os_{uuid4().hex}",
        user_id=user_id,
        issuance_key=effective_key,
        question_ids_json=[q.id for q in selection.questions],
        questions_snapshot_json=[
            q.model_dump(mode="json", by_alias=True) for q in selection.questions
        ],
        question_roles_json={
            str(q.id): resolve_question_role(q.id, q.cluster_key)
            for q in selection.questions
        },
        total_count=selection.total_count,
        exploitation_count=selection.exploitation_count,
        exploration_count=selection.exploration_count,
        status="ISSUED",
    )
    db.add(issuance)
    try:
        if commit:
            db.commit()
            db.refresh(issuance)
        else:
            db.flush()
    except IntegrityError:
        db.rollback()
        if effective_key is not None and user_id is not None:
            existing = (
                db.query(OrderSheetIssuance)
                .filter(
                    OrderSheetIssuance.user_id == user_id,
                    OrderSheetIssuance.issuance_key == effective_key,
                )
                .first()
            )
            if existing is not None:
                return _issuance_to_response(existing)
        raise

    return _issuance_to_response(issuance)


def create_order_sheet_recommendation(
    db: Session,
    request: OrderSheetRecommendRequest,
    user_id: int | None = None,
    current_user: User | None = None,
    *,
    commit: bool = True,
    fixed_exclusion_keywords: set[str] | list[str] | None = None,
    skip_user_lock: bool = False,
) -> OrderSheetRecommendResponse:
    """
    [하이브리드 주문서 추천 파이프라인]
    0. 사용자 알레르기/비선호/섭취불가 메뉴 제외
    1. 우선순위 기반 태그 하드 필터링 (meal_type -> main_ingredient -> has_soup -> cooking_style)
       - 서로 다른 추천 메뉴 그룹이 top_k 미만으로 줄어드는 필터는 점수제 질문으로 전환
    2. 남은 후보 메뉴에 대해 점수제 질문(완화된 필터 포함) 가중치 합산
    3. 다양성 보정 후 상위 top_k 메뉴 추천, 실제 제출된 질문의 shown_count 갱신 및 스냅샷 저장
    """
    effective_user_id = user_id
    if current_user is not None:
        if skip_user_lock:
            effective_user_id = current_user.id
        else:
            locked_user = lock_user_and_verify_session(db, current_user)
            effective_user_id = locked_user.id

    question_ids = [ans.question_id for ans in request.answers]
    if len(set(question_ids)) != len(question_ids):
        raise InvalidOrderSheetException("동일한 질문에 중복 응답할 수 없습니다.")

    all_menus = db.query(Menu).all()
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
    available_menus, excluded_count = filter_available_menus(all_menus, exclusion_keywords)

    questions = (
        db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.id.in_(question_ids))
        .all()
    )
    question_map = {q.id: q for q in questions}

    if not question_map:
        raise InvalidOrderSheetException("제출된 질문 ID에 해당하는 주문서 질문이 없습니다.")

    validated_answers: list[tuple[OrderSheetQuestion, dict[str, Any]]] = []
    for ans in request.answers:
        q = question_map.get(ans.question_id)
        if not q:
            raise InvalidOrderSheetException(f"존재하지 않는 질문 ID입니다: {ans.question_id}")
        matched_opt = next(
            (opt for opt in _question_options(q) if opt.get("option_id") == ans.option_id),
            None,
        )
        if not matched_opt:
            raise InvalidOrderSheetException(
                f"질문({q.question_code})에 유효하지 않은 선택지 ID입니다: {ans.option_id}"
            )
        validated_answers.append((q, matched_opt))

    # ---------------------------------------------------------
    # ① 우선순위 기반 태그 하드 필터링 ('상관없어요'는 rules=[] 이므로 자동 패스)
    # ---------------------------------------------------------
    hard_filter_answers: list[tuple[OrderSheetQuestion, dict[str, Any]]] = [
        (q, opt)
        for q, opt in validated_answers
        if q.cluster_key in HARD_FILTER_CLUSTERS and opt.get("rules")
    ]
    hard_filter_answers.sort(
        key=lambda item: HARD_FILTER_PRIORITY.get(item[0].cluster_key, 999)
    )

    score_answers: list[tuple[OrderSheetQuestion, dict[str, Any]]] = [
        (q, opt)
        for q, opt in validated_answers
        if q.cluster_key not in HARD_FILTER_CLUSTERS
    ]

    filtered_menus = list(available_menus)
    applied_hard_filters: list[tuple[OrderSheetQuestion, list[dict[str, Any]]]] = []

    for q, opt in hard_filter_answers:
        rules = opt.get("rules", [])
        next_filtered = [m for m in filtered_menus if evaluate_option_rules(m, rules)]
        # 최종 추천은 그룹당 한 메뉴를 고르므로 필터도 같은 단위로 후보를 센다.
        if len({menu_group(menu) for menu in next_filtered}) >= request.top_k:
            filtered_menus = next_filtered
            applied_hard_filters.append((q, rules))
        else:
            # 후보 부족으로 완화된 하드 필터는 점수제 질문으로 전환해 선호도가 반영되도록 보존
            score_answers.append((q, opt))

    applied_filter_codes = [q.question_code for q, _ in applied_hard_filters]

    # ---------------------------------------------------------
    # ② 필터링된 후보 메뉴 대상으로 점수제 질문(완화된 필터 포함) 평가
    # ---------------------------------------------------------
    scored_candidates: list[tuple[float, list[str], Menu]] = []
    for menu in filtered_menus:
        score = 0.0
        # 하드 필터를 통과한 질문 코드는 조건을 충족했으므로 matched_codes에 포함
        matched_codes: list[str] = list(applied_filter_codes)

        for q, opt in score_answers:
            rules = opt.get("rules", [])
            weight = float(q.importance_weight)
            if rules and evaluate_option_rules(menu, rules):
                score += weight * 1.0
                matched_codes.append(q.question_code)

        scored_candidates.append((round(score, 4), matched_codes, menu))

    score_total_weight = sum(
        float(q.importance_weight) for q, opt in score_answers if opt.get("rules")
    )

    top_details = select_diverse_menus_with_snapshots(
        db=db,
        candidates=scored_candidates,
        user_id=effective_user_id,
        top_k=request.top_k,
        total_weight=score_total_weight if score_total_weight > 0 else 1.0,
    )

    # 실제로 사용자에게 노출되어 제출된 질문들의 shown_count 증가 (NULL 방어 포함)
    submitted_q_ids = [q.id for q, _ in validated_answers]
    if submitted_q_ids:
        db.query(OrderSheetQuestion).filter(OrderSheetQuestion.id.in_(submitted_q_ids)).update(
            {OrderSheetQuestion.shown_count: func.coalesce(OrderSheetQuestion.shown_count, 0) + 1},
            synchronize_session="fetch",
        )

    rec_record = Recommendation(
        user_id=effective_user_id,
        shown_question_ids=submitted_q_ids,
        answers_json=[
            {"question_id": q.id, "option_id": opt["option_id"]}
            for q, opt in validated_answers
        ],
        recommended_menu_ids=[d.menu.id for d in top_details],
    )
    db.add(rec_record)
    db.flush()

    if effective_user_id is not None:
        now = datetime.now(timezone.utc)
        for rank_idx, detail in enumerate(top_details, start=1):
            m = detail.menu
            mkey = menu_group(m)
            snapshot = RecommendationItemSnapshot(
                recommendation_id=rec_record.id,
                user_id=effective_user_id,
                menu_id=m.id,
                menu_key=mkey,
                menu_base_name=(m.base_name or m.name or "").strip() or mkey,
                menu_main_ingredient=(m.main_ingredient or "other").strip() or "other",
                rank_position=rank_idx,
                rule_score=detail.rule_score,
                recent_meal_adjustment=detail.personal_result.recent_meal_adjustment,
                personal_adjustment=detail.personal_result.personal_adjustment,
                final_score=detail.final_score,
                draw_weight=detail.personal_result.draw_weight,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                features_json=dict(detail.personal_result.features),
                created_at=now,
            )
            db.add(snapshot)

    if commit:
        db.commit()
        db.refresh(rec_record)
    else:
        db.flush()

    recommended_dtos = [
        RecommendedMenuDTO(
            menu_id=d.menu.id,
            food_code=d.menu.food_code,
            name=format_menu_name(d.menu.name),
            base_name=d.menu.base_name,
            category_large=d.menu.category_large,
            category_medium=d.menu.category_medium,
            meal_type=d.menu.meal_type,
            has_soup=d.menu.has_soup,
            cooking_style=d.menu.cooking_style,
            main_ingredient=d.menu.main_ingredient,
            flavor_intensity=d.menu.flavor_intensity,
            temperature=d.menu.temperature,
            heaviness=d.menu.heaviness,
            kcal=d.menu.kcal,
            protein_g=d.menu.protein_g,
            fat_g=d.menu.fat_g,
            carb_g=d.menu.carb_g,
            sodium_mg=d.menu.sodium_mg,
            match_score=d.rule_score,
            matched_question_codes=d.matched_codes,
        )
        for d in top_details
    ]

    return OrderSheetRecommendResponse(
        recommendation_id=rec_record.id,
        recommended_menus=recommended_dtos,
        excluded_count=excluded_count,
        candidate_count=len(filtered_menus),
    )


def effective_question_feedback(db: Session) -> list[RecommendationFeedbackState | RecommendationFeedback]:
    state_rows = db.query(RecommendationFeedbackState).all()
    state_rec_ids = {state.recommendation_id for state in state_rows}
    legacy_rows = [
        feedback
        for feedback in db.query(RecommendationFeedback).all()
        if feedback.recommendation_id not in state_rec_ids and feedback.answer_match_results
    ]
    return [state for state in state_rows if state.answer_match_results] + legacy_rows


def recalculate_question_weights(
    db: Session, *, commit: bool = True, removed_feedback_question_ids: set[int] | None = None,
) -> dict[str, float]:
    """
    [4·5단계 피드백 누적 데이터 기반 ML 가중치 재학습]
    추천×음식(menu_key) 단위 현재 유효 상태(RecommendationFeedbackState)를 우선 사용하여
    동일 추천·음식의 재전송이나 수정으로 인해 전역 통계가 중복 가산되지 않도록 보장한다.
    """
    questions = db.query(OrderSheetQuestion).all()
    effective_votes = effective_question_feedback(db)

    if not effective_votes and not removed_feedback_question_ids:
        return {q.question_code: round(float(q.importance_weight), 4) for q in questions}

    total_fb = len(effective_votes)
    global_pos = sum(1 for fb in effective_votes if fb.is_satisfied)
    base_rate = (global_pos + 1.0) / (total_fb + 2.0)

    stats: dict[str, dict[str, float]] = {}
    for fb in effective_votes:
        vec = fb.answer_match_results or {}
        y = 1.0 if fb.is_satisfied else 0.0
        for q_id_str, match_val in vec.items():
            st = stats.setdefault(
                str(q_id_str),
                {"match_pos": 0.0, "match_total": 0.0, "unmatch_pos": 0.0, "unmatch_total": 0.0},
            )
            if float(match_val) >= 0.5:
                st["match_total"] += 1.0
                st["match_pos"] += y
            else:
                st["unmatch_total"] += 1.0
                st["unmatch_pos"] += y

    updated: dict[str, float] = {}
    for q in questions:
        st = stats.get(str(q.id))
        if removed_feedback_question_ids and q.id in removed_feedback_question_ids:
            q.positive_count = int(st["match_pos"]) if st else 0
            if not st:
                q.importance_weight = q.initial_score
        if not st or (st["match_total"] + st["unmatch_total"]) == 0:
            updated[q.question_code] = round(float(q.importance_weight), 4)
            continue

        sample_n = st["match_total"] + st["unmatch_total"]
        p_match = (st["match_pos"] + 1.0 * base_rate) / (st["match_total"] + 1.0)
        p_unmatch = (st["unmatch_pos"] + 1.0 * base_rate) / (st["unmatch_total"] + 1.0)

        lift = p_match - p_unmatch
        feedback_importance = 1.0 / (1.0 + math.exp(-3.2 * (lift + (p_match - 0.5))))

        alpha = min(0.85, sample_n / (sample_n + 4.0))
        new_weight = (1.0 - alpha) * float(q.initial_score) + alpha * feedback_importance
        q.importance_weight = round(max(0.05, min(2.0, new_weight)), 4)
        updated[q.question_code] = q.importance_weight

    if commit:
        db.commit()
    else:
        db.flush()
    return updated


def record_recommendation_feedback(
    db: Session,
    recommendation_id: int,
    request: RecommendationFeedbackRequest,
    user_id: int | None = None,
    current_user: User | None = None,
    idempotency_key: str | None = None,
) -> RecommendationFeedbackResponse:
    """
    추천 결과에 대한 사용자 피드백 이력(RecommendationFeedback)과 현재 유효 상태(RecommendationFeedbackState)를
    단일 트랜잭션으로 저장하고 질문 가중치를 재계산한다.
    """
    effective_user_id = user_id
    if current_user is not None:
        locked_user = lock_user_and_verify_session(db, current_user)
        effective_user_id = locked_user.id

    try:
        op_key = normalize_idempotency_key(idempotency_key)
    except ValueError as exc:
        raise RecommendationDomainException(str(exc), status_code=400) from exc

    rec = (
        db.query(Recommendation)
        .filter(Recommendation.id == recommendation_id)
        .with_for_update()
        .first()
    )
    if not rec:
        raise RecommendationNotFoundException(recommendation_id)
    if effective_user_id is not None and rec.user_id != effective_user_id:
        raise RecommendationNotFoundException(recommendation_id)

    target_menu_id = request.selected_menu_id
    if target_menu_id is None and rec.recommended_menu_ids:
        target_menu_id = rec.recommended_menu_ids[0]
    if target_menu_id is None or target_menu_id not in (rec.recommended_menu_ids or []):
        raise InvalidOrderSheetException("해당 추천 결과에 포함된 메뉴만 평가할 수 있습니다.")

    owner_user_id = effective_user_id if effective_user_id is not None else rec.user_id
    payload_hash = compute_payload_hash(
        {
            "recommendation_id": int(recommendation_id),
            "selected_menu_id": int(target_menu_id) if target_menu_id is not None else None,
            "is_satisfied": bool(request.is_satisfied),
        }
    )

    operation = None
    if op_key is not None and owner_user_id is not None:
        operation, is_existing = acquire_or_inspect_operation(
            db,
            user_id=owner_user_id,
            auth_session_id=get_user_session_id(current_user) if current_user is not None else None,
            scope="RECOMMENDATION_FEEDBACK",
            operation_key=op_key,
            payload_hash=payload_hash,
        )
        if is_existing:
            if operation.payload_hash != payload_hash:
                db.rollback()
                raise RecommendationDomainException(
                    "동일한 Idempotency-Key로 다른 피드백 내용을 전송할 수 없습니다. (IDEMPOTENCY_KEY_REUSED)",
                    status_code=409,
                )
            if operation.status == "SUCCEEDED" and operation.response_json:
                db.rollback()
                return RecommendationFeedbackResponse(**operation.response_json)

    target_menu = (
        db.query(Menu).filter(Menu.id == target_menu_id).first()
        if target_menu_id is not None
        else None
    )
    mkey = menu_group(target_menu) if target_menu is not None else f"menu:{target_menu_id}"
    m_base = (target_menu.base_name or target_menu.name or mkey).strip() if target_menu is not None else mkey
    m_ing = (target_menu.main_ingredient or "other").strip() if target_menu is not None else "other"

    existing_state = (
        db.query(RecommendationFeedbackState)
        .filter(
            RecommendationFeedbackState.recommendation_id == rec.id,
            RecommendationFeedbackState.menu_key == mkey,
        )
        .with_for_update()
        .first()
        if owner_user_id is not None
        else None
    )

    answers = rec.answers_json or []
    prev_q_ids = (
        [
            int(qid_str)
            for qid_str in (existing_state.answer_match_results or {}).keys()
            if str(qid_str).isdigit()
        ]
        if existing_state is not None
        else []
    )
    q_ids = list({a["question_id"] for a in answers if "question_id" in a} | set(prev_q_ids))
    questions = (
        db.query(OrderSheetQuestion).filter(OrderSheetQuestion.id.in_(q_ids)).all()
        if q_ids
        else []
    )
    q_map = {q.id: q for q in questions}

    answer_match_results: dict[str, float] = {}
    matched_questions: list[OrderSheetQuestion] = []
    for ans in answers:
        qid = ans.get("question_id")
        opt_id = ans.get("option_id")
        q = q_map.get(qid)
        if not q:
            continue
        # 추천 시점과 동일하게 _question_options(q)를 사용해 기존 2선택지 DB의 any 보완을 일치시킨다.
        opt = next(
            (o for o in _question_options(q) if o.get("option_id") == opt_id),
            None,
        )
        rules = opt.get("rules", []) if opt else []
        if not rules:
            continue

        matched = True
        if target_menu is not None and rules:
            matched = evaluate_option_rules(target_menu, rules)

        answer_match_results[str(q.id)] = 1.0 if matched else 0.0
        if matched:
            matched_questions.append(q)

    feedback = RecommendationFeedback(
        recommendation_id=rec.id,
        user_id=owner_user_id,
        selected_menu_id=target_menu_id,
        menu_key=mkey,
        menu_base_name=m_base,
        menu_main_ingredient=m_ing,
        operation_key=op_key,
        is_satisfied=request.is_satisfied,
        answer_match_results=answer_match_results,
    )
    db.add(feedback)
    db.flush()

    if owner_user_id is not None:
        now = datetime.now(timezone.utc)
        new_satisfied = bool(request.is_satisfied)
        new_pos_q_ids = {q.id for q in matched_questions} if new_satisfied else set()

        if existing_state is None:
            state = RecommendationFeedbackState(
                recommendation_id=rec.id,
                user_id=owner_user_id,
                selected_menu_id=target_menu_id,
                menu_key=mkey,
                menu_base_name=m_base,
                menu_main_ingredient=m_ing,
                is_satisfied=new_satisfied,
                answer_match_results=answer_match_results,
                latest_feedback_id=feedback.id,
                created_at=now,
                updated_at=now,
            )
            db.add(state)
            for q_id in new_pos_q_ids:
                q = q_map.get(q_id)
                if q:
                    q.positive_count = int(q.positive_count or 0) + 1
        else:
            prev_satisfied = bool(existing_state.is_satisfied)
            prev_match_results = existing_state.answer_match_results or {}
            prev_pos_q_ids = (
                {
                    int(qid_str)
                    for qid_str, val in prev_match_results.items()
                    if float(val) >= 0.5
                }
                if prev_satisfied
                else set()
            )

            existing_state.selected_menu_id = target_menu_id
            existing_state.menu_base_name = m_base
            existing_state.menu_main_ingredient = m_ing
            existing_state.is_satisfied = new_satisfied
            existing_state.answer_match_results = answer_match_results
            existing_state.latest_feedback_id = feedback.id
            if prev_satisfied != new_satisfied or prev_pos_q_ids != new_pos_q_ids:
                existing_state.updated_at = now

            # 이전 상태와 새 상태의 긍정 기여 질문 차이를 비교해 positive_count 증감 동기화
            for q_id in new_pos_q_ids - prev_pos_q_ids:
                q = q_map.get(q_id)
                if q:
                    q.positive_count = int(q.positive_count or 0) + 1
            for q_id in prev_pos_q_ids - new_pos_q_ids:
                q = q_map.get(q_id)
                if q:
                    q.positive_count = max(0, int(q.positive_count or 0) - 1)
    else:
        if request.is_satisfied:
            for q in matched_questions:
                q.positive_count = int(q.positive_count or 0) + 1

    db.flush()
    updated_weights = recalculate_question_weights(db, commit=False)

    response = RecommendationFeedbackResponse(
        feedback_id=feedback.id,
        recommendation_id=rec.id,
        is_satisfied=feedback.is_satisfied,
        updated_weights=updated_weights,
    )

    if operation is not None:
        operation.status = "SUCCEEDED"
        operation.resource_id = str(feedback.id)
        operation.response_status = 200
        operation.response_json = response.model_dump()
        operation.updated_at = utc_now()

    db.commit()
    return response


def tune_question_weights(
    db: Session,
    request: OrderSheetWeightTuneRequest,
) -> OrderSheetWeightTuneResponse:
    """
    [4단계 개발팀 사전 피드백 튜닝 및 수동 가중치 조절]
    특정 질문의 가중치를 직접 지정하거나 증감(feedback_delta)시킨 뒤 상위 질문 구성을 반환합니다.
    """
    updated_count = 0
    if request.adjustments:
        q_ids = [item.question_id for item in request.adjustments]
        questions = db.query(OrderSheetQuestion).filter(OrderSheetQuestion.id.in_(q_ids)).all()
        q_map = {q.id: q for q in questions}

        for adj in request.adjustments:
            q = q_map.get(adj.question_id)
            if not q:
                continue
            if adj.importance_weight is not None:
                q.importance_weight = round(float(adj.importance_weight), 4)
            if adj.feedback_delta is not None:
                q.importance_weight = round(
                    max(0.05, min(2.0, float(q.importance_weight) + float(adj.feedback_delta))),
                    4,
                )
            if adj.is_active is not None:
                q.is_active = adj.is_active
            updated_count += 1
        db.commit()

    if request.recalculate_from_feedback:
        recalculate_question_weights(db)

    top_rows = (
        db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.is_active.is_(True))
        .order_by(OrderSheetQuestion.importance_weight.desc(), OrderSheetQuestion.id.asc())
        .limit(DEFAULT_TOTAL_QUESTIONS)
        .all()
    )
    return OrderSheetWeightTuneResponse(
        updated_count=updated_count,
        top_questions=[_to_question_dto(q, is_exploration=False) for q in top_rows],
    )

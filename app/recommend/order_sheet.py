"""샤논 엔트로피 기반 초기 점수와 사용자 피드백 통계로 메뉴를 추천한다.

초기에는 선택지별 음식 분포와 주제 다양성으로 질문 중요도를 정한다.
이후에는 조건 일치 여부별 만족도를 집계해 같은 중요도를 재계산한다.
"""

import math
import random
from typing import Any
from sqlalchemy.orm import Session

from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedback,
)
from app.recommend.common import (
    InvalidOrderSheetException,
    QuestionPoolEmptyException,
    RecommendationNotFoundException,
    collect_user_exclusion_keywords,
    evaluate_option_rules,
    filter_available_menus,
)
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

DEFAULT_TOTAL_QUESTIONS = 9
DEFAULT_EXPLOITATION_COUNT = 7


def _to_question_dto(
    question: OrderSheetQuestion,
    is_exploration: bool = False,
) -> OrderSheetQuestionDTO:
    options = []
    for opt in question.options_json or []:
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
                label=opt["label"],
                rules=rules,
            )
        )
    return OrderSheetQuestionDTO(
        id=question.id,
        question_code=question.question_code,
        cluster_key=question.cluster_key,
        question_text=question.question_text,
        options=options,
        initial_score=round(float(question.initial_score), 4),
        importance_weight=round(float(question.importance_weight), 4),
        shown_count=int(question.shown_count or 0),
        positive_count=int(question.positive_count or 0),
        is_exploration=is_exploration,
    )


def calculate_question_entropy_score(
    menus: list[Menu],
    options_json: list[dict[str, Any]],
) -> float:
    """
    [3단계 샤논 엔트로피 기반 초기 질문 점수]
    조건이 있는 선택지별 메뉴 수로 정규화 샤논 엔트로피를 계산하고,
    커버리지를 결합해 초기 점수(0.05 ~ 1.0)를 산출합니다.
    조건 선택지가 하나면 일치/불일치의 이진 엔트로피를 사용합니다.
    선택지별 개수는 중복 매칭을 포함하므로 커버리지 비율은 최대 1로 제한합니다.
    """
    if not menus or not options_json:
        return 0.1

    specific_options = [opt for opt in options_json if opt.get("rules")]
    if len(specific_options) < 2:
        if len(specific_options) == 1:
            rules = specific_options[0].get("rules", [])
            match_cnt = sum(1 for m in menus if evaluate_option_rules(m, rules))
            p = match_cnt / len(menus)
            if p <= 0.0 or p >= 1.0:
                return 0.05
            binary_entropy = -(p * math.log2(p) + (1 - p) * math.log2(1 - p))
            return round(max(0.05, min(1.0, binary_entropy)), 4)
        return 0.1

    counts = []
    for opt in specific_options:
        rules = opt.get("rules", [])
        cnt = sum(1 for m in menus if evaluate_option_rules(m, rules))
        counts.append(cnt)

    total_matched = sum(counts)
    if total_matched == 0:
        return 0.05

    probs = [c / total_matched for c in counts if c > 0]
    if len(probs) <= 1:
        return 0.08

    entropy = -sum(p * math.log2(p) for p in probs)
    max_entropy = math.log2(len(specific_options))
    normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0.0

    coverage_ratio = min(1.0, total_matched / len(menus))
    score = 0.75 * normalized_entropy + 0.25 * coverage_ratio
    return round(max(0.05, min(1.0, score)), 4)


def rank_and_score_question_pool(
    menus: list[Menu],
    raw_questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    [3단계 엔트로피 점수와 주제 다양성에 따른 초기 중요도 산정]
    후보 질문마다 엔트로피 기반 초기 점수를 계산합니다.
    미리 지정된 같은 주제(cluster_key)의 질문에는 순서대로 감쇠 계수를 적용합니다.
    """
    scored: list[dict[str, Any]] = []
    for q in raw_questions:
        initial_score = calculate_question_entropy_score(menus, q["options"])
        q_copy = dict(q)
        q_copy["initial_score"] = initial_score
        scored.append(q_copy)

    scored.sort(key=lambda x: x["initial_score"], reverse=True)

    cluster_seen_counts: dict[str, int] = {}
    for item in scored:
        cluster = item["cluster_key"]
        rank_in_cluster = cluster_seen_counts.get(cluster, 0)
        cluster_seen_counts[cluster] = rank_in_cluster + 1
        diversity_factor = 0.90 ** rank_in_cluster
        item["importance_weight"] = round(max(0.05, item["initial_score"] * diversity_factor), 4)

    scored.sort(key=lambda x: x["importance_weight"], reverse=True)
    return scored


def select_order_sheet_questions(
    db: Session,
    total_count: int = DEFAULT_TOTAL_QUESTIONS,
    exploitation_count: int = DEFAULT_EXPLOITATION_COUNT,
    random_seed: int | None = None,
) -> OrderSheetQuestionsResponse:
    """
    [5단계 탐색+활용(Exploration-Exploitation) 질문 추출]
    - 상위 exploitation_count(기본 7개): 서로 다른 cluster_key를 우선하여 importance_weight 최상위 질문 선택
    - 나머지 exploration_count(기본 2개): 후보 풀에서 노출 횟수가 적은 질문을 가중 샘플링하여
      새로운 질문도 사용자 피드백에 따른 중요도 재계산 기회를 얻도록 합니다.
    """
    active_questions = (
        db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.is_active.is_(True))
        .order_by(OrderSheetQuestion.importance_weight.desc(), OrderSheetQuestion.id.asc())
        .all()
    )
    if not active_questions:
        raise QuestionPoolEmptyException()

    actual_total = min(total_count, len(active_questions))
    actual_exploit = min(exploitation_count, actual_total)
    actual_explore = actual_total - actual_exploit

    selected_exploit: list[OrderSheetQuestion] = []
    used_ids: set[int] = set()
    used_clusters: set[str] = set()

    for q in active_questions:
        if len(selected_exploit) >= actual_exploit:
            break
        if q.cluster_key not in used_clusters:
            selected_exploit.append(q)
            used_ids.add(q.id)
            used_clusters.add(q.cluster_key)

    for q in active_questions:
        if len(selected_exploit) >= actual_exploit:
            break
        if q.id not in used_ids:
            selected_exploit.append(q)
            used_ids.add(q.id)

    remaining_pool = [q for q in active_questions if q.id not in used_ids]
    selected_explore: list[OrderSheetQuestion] = []

    if actual_explore > 0 and remaining_pool:
        rng = random.Random(random_seed)
        pool_copy = list(remaining_pool)
        while len(selected_explore) < actual_explore and pool_copy:
            weights = [
                (float(q.importance_weight) + 0.3) / math.sqrt(float(q.shown_count or 0) + 1.0)
                for q in pool_copy
            ]
            chosen = rng.choices(pool_copy, weights=weights, k=1)[0]
            selected_explore.append(chosen)
            pool_copy.remove(chosen)

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


def create_order_sheet_recommendation(
    db: Session,
    request: OrderSheetRecommendRequest,
) -> OrderSheetRecommendResponse:
    """
    [5단계 주문서 답변 기반 메뉴 추천]
    1. 사용자 알레르기/비선호/섭취불가 메뉴를 먼저 제외(Hard Filter)
    2. 제출된 질문별 선택지 조건식(rules)을 평가하여 질문의 importance_weight 가중치 합산
    3. 상위 top_k 메뉴를 추천하고 추천 기록을 MySQL에 저장
    """
    all_menus = db.query(Menu).all()
    exclusion_keywords = collect_user_exclusion_keywords(
        db=db,
        user_id=request.user_id,
        extra_exclusions=request.exclusions,
    )
    available_menus, excluded_count = filter_available_menus(all_menus, exclusion_keywords)

    question_ids = [ans.question_id for ans in request.answers]
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
            (opt for opt in (q.options_json or []) if opt.get("option_id") == ans.option_id),
            None,
        )
        if not matched_opt:
            raise InvalidOrderSheetException(
                f"질문({q.question_code})에 유효하지 않은 선택지 ID입니다: {ans.option_id}"
            )
        validated_answers.append((q, matched_opt))
        q.shown_count = int(q.shown_count or 0) + 1

    scored_candidates: list[tuple[float, list[str], Menu]] = []
    for menu in available_menus:
        score = 0.0
        matched_codes: list[str] = []
        for q, opt in validated_answers:
            rules = opt.get("rules", [])
            weight = float(q.importance_weight)
            if not rules:
                score += weight * 0.35
            elif evaluate_option_rules(menu, rules):
                score += weight * 1.0
                matched_codes.append(q.question_code)
        scored_candidates.append((round(score, 4), matched_codes, menu))

    scored_candidates.sort(
        key=lambda item: (item[0], len(item[1]), -item[2].id),
        reverse=True,
    )
    top_items = scored_candidates[: request.top_k]

    rec_record = Recommendation(
        user_id=request.user_id,
        shown_question_ids=[q.id for q, _ in validated_answers],
        answers_json=[
            {"question_id": q.id, "option_id": opt["option_id"]}
            for q, opt in validated_answers
        ],
        recommended_menu_ids=[menu.id for _, _, menu in top_items],
    )
    db.add(rec_record)
    db.commit()
    db.refresh(rec_record)

    recommended_dtos = [
        RecommendedMenuDTO(
            menu_id=menu.id,
            food_code=menu.food_code,
            name=menu.name,
            base_name=menu.base_name,
            category_large=menu.category_large,
            category_medium=menu.category_medium,
            meal_type=menu.meal_type,
            has_soup=menu.has_soup,
            cooking_style=menu.cooking_style,
            main_ingredient=menu.main_ingredient,
            flavor_intensity=menu.flavor_intensity,
            temperature=menu.temperature,
            heaviness=menu.heaviness,
            kcal=menu.kcal,
            protein_g=menu.protein_g,
            fat_g=menu.fat_g,
            carb_g=menu.carb_g,
            sodium_mg=menu.sodium_mg,
            match_score=score,
            matched_question_codes=matched_codes,
        )
        for score, matched_codes, menu in top_items
    ]

    return OrderSheetRecommendResponse(
        recommendation_id=rec_record.id,
        recommended_menus=recommended_dtos,
        excluded_count=excluded_count,
        candidate_count=len(available_menus),
    )


def recalculate_question_weights(db: Session) -> dict[str, float]:
    """
    [4·5단계 누적 사용자 피드백 기반 질문 중요도 재계산]
    질문별 조건 일치/불일치 그룹의 만족도를 전체 만족도로 평활화합니다.
    두 그룹의 만족도 차이와 일치 그룹의 만족도를 시그모이드로 변환한 뒤,
    후기 수에 따른 비율로 초기 엔트로피 기반 점수와 혼합해 중요도를 갱신합니다.
    별도 모델 최적화 없이 고정된 통계 수식을 적용합니다.
    """
    questions = db.query(OrderSheetQuestion).all()
    feedbacks = db.query(RecommendationFeedback).all()

    if not feedbacks:
        return {q.question_code: round(float(q.importance_weight), 4) for q in questions}

    total_fb = len(feedbacks)
    global_pos = sum(1 for fb in feedbacks if fb.is_satisfied)
    base_rate = (global_pos + 1.0) / (total_fb + 2.0)

    stats: dict[str, dict[str, float]] = {}
    for fb in feedbacks:
        match_results = fb.answer_match_results or {}
        satisfaction = 1.0 if fb.is_satisfied else 0.0
        for q_id_str, match_val in match_results.items():
            st = stats.setdefault(
                str(q_id_str),
                {"match_pos": 0.0, "match_total": 0.0, "unmatch_pos": 0.0, "unmatch_total": 0.0},
            )
            if float(match_val) >= 0.5:
                st["match_total"] += 1.0
                st["match_pos"] += satisfaction
            else:
                st["unmatch_total"] += 1.0
                st["unmatch_pos"] += satisfaction

    updated: dict[str, float] = {}
    for q in questions:
        st = stats.get(str(q.id))
        if not st or (st["match_total"] + st["unmatch_total"]) == 0:
            updated[q.question_code] = round(float(q.importance_weight), 4)
            continue

        sample_n = st["match_total"] + st["unmatch_total"]
        p_match = (st["match_pos"] + 1.0 * base_rate) / (st["match_total"] + 1.0)
        p_unmatch = (st["unmatch_pos"] + 1.0 * base_rate) / (st["unmatch_total"] + 1.0)

        lift = p_match - p_unmatch
        feedback_importance = 1.0 / (1.0 + math.exp(-3.2 * (lift + (p_match - 0.5))))

        feedback_ratio = min(0.85, sample_n / (sample_n + 4.0))
        new_weight = (
            (1.0 - feedback_ratio) * float(q.initial_score)
            + feedback_ratio * feedback_importance
        )
        q.importance_weight = round(max(0.05, min(2.0, new_weight)), 4)
        updated[q.question_code] = q.importance_weight

    db.commit()
    return updated


def record_recommendation_feedback(
    db: Session,
    recommendation_id: int,
    request: RecommendationFeedbackRequest,
) -> RecommendationFeedbackResponse:
    """
    추천 결과에 대한 사용자(또는 4단계 개발팀) 피드백을 기록하고,
    누적 만족도 통계로 질문 중요도를 즉시 재계산해 DB에 반영합니다.
    """
    rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
    if not rec:
        raise RecommendationNotFoundException(recommendation_id)

    target_menu_id = request.selected_menu_id
    if target_menu_id is None and rec.recommended_menu_ids:
        target_menu_id = rec.recommended_menu_ids[0]

    target_menu = (
        db.query(Menu).filter(Menu.id == target_menu_id).first()
        if target_menu_id is not None
        else None
    )

    answers = rec.answers_json or []
    q_ids = [a["question_id"] for a in answers if "question_id" in a]
    questions = (
        db.query(OrderSheetQuestion).filter(OrderSheetQuestion.id.in_(q_ids)).all()
        if q_ids
        else []
    )
    q_map = {q.id: q for q in questions}

    answer_match_results: dict[str, float] = {}
    for ans in answers:
        qid = ans.get("question_id")
        opt_id = ans.get("option_id")
        q = q_map.get(qid)
        if not q:
            continue
        opt = next((o for o in (q.options_json or []) if o.get("option_id") == opt_id), None)
        rules = opt.get("rules", []) if opt else []

        matched = True
        if target_menu is not None and rules:
            matched = evaluate_option_rules(target_menu, rules)

        answer_match_results[str(q.id)] = 1.0 if matched else 0.0
        if request.is_satisfied and matched:
            q.positive_count = int(q.positive_count or 0) + 1

    feedback = RecommendationFeedback(
        recommendation_id=rec.id,
        user_id=request.user_id or rec.user_id,
        selected_menu_id=target_menu_id,
        is_satisfied=request.is_satisfied,
        answer_match_results=answer_match_results,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)

    updated_weights = recalculate_question_weights(db)

    return RecommendationFeedbackResponse(
        feedback_id=feedback.id,
        recommendation_id=rec.id,
        is_satisfied=feedback.is_satisfied,
        updated_weights=updated_weights,
    )


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

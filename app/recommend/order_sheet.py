import math
import random
from typing import Any
from sqlalchemy.orm import Session

from app.menus.display import format_menu_name
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    QuestionSelectionState,
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
from app.recommend.variety import select_diverse_menus

DEFAULT_TOTAL_QUESTIONS = 9
DEFAULT_EXPLOITATION_COUNT = 7


def _question_options(question: OrderSheetQuestion) -> list[dict[str, Any]]:
    options = list(question.options_json or [])
    # 기존 두 선택지 DB도 재적재 전까지 any를 조회·제출할 수 있다.
    if len(options) == 2 and not any(opt.get("option_id") == "any" for opt in options):
        options.append({"option_id": "any", "label": "상관없어요", "rules": []})
    return options


def _to_question_dto(
    question: OrderSheetQuestion,
    is_exploration: bool = False,
) -> OrderSheetQuestionDTO:
    options = []
    for opt in _question_options(question):
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
    [3단계 초기 ML 선별]
    피드백 데이터가 0건일 때 음식 후보 분포에 대한 선택지별 정규화 샤논 엔트로피(Information Gain)와
    커버리지(Coverage)를 결합해 질문의 메뉴 분할 변별력(0.05 ~ 1.0)을 산출합니다.
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
    [3단계 초기 ML 클러스터링 & 정보 이득 정렬]
    수백 개의 후보 질문 각각에 대해 정보 이득을 계산하고,
    동일 의미 클러스터(cluster_key) 내 중복 질문에는 감쇠 계수를 적용해 초기 importance_weight를 산출합니다.
    """
    scored: list[dict[str, Any]] = []
    for q in raw_questions:
        ig = calculate_question_entropy_score(menus, q["options"])
        q_copy = dict(q)
        q_copy["initial_score"] = ig
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
    scope_key: str | None = None,
) -> OrderSheetQuestionsResponse:
    """
    [5단계 탐색+활용(Exploration-Exploitation) 질문 추출]
    - 상위 exploitation_count(기본 7개): 서로 다른 cluster_key의 importance_weight 최상위 질문 선택
    - 나머지 exploration_count(기본 2개): 아직 선택하지 않은 주제에서 노출 횟수를 고려해 가중 샘플링
    - 상위·탐색 질문 전체에서 주제 중복을 금지하며, 주제가 부족하면 가능한 개수만 반환
    """
    active_questions = (
        db.query(OrderSheetQuestion)
        .filter(OrderSheetQuestion.is_active.is_(True))
        .order_by(OrderSheetQuestion.importance_weight.desc(), OrderSheetQuestion.id.asc())
        .all()
    )
    if not active_questions:
        raise QuestionPoolEmptyException()

    actual_total = min(total_count, len({q.cluster_key for q in active_questions}))
    actual_exploit = min(exploitation_count, actual_total)
    actual_explore = actual_total - actual_exploit

    selected_exploit: list[OrderSheetQuestion] = []
    used_clusters: set[str] = set()

    for q in active_questions:
        if len(selected_exploit) >= actual_exploit:
            break
        if q.cluster_key not in used_clusters:
            selected_exploit.append(q)
            used_clusters.add(q.cluster_key)

    remaining_pool = [q for q in active_questions if q.cluster_key not in used_clusters]
    selected_explore: list[OrderSheetQuestion] = []
    state = None
    topic_counts: dict[str, int] = {}
    last_clusters: set[str] = set()
    if scope_key is not None:
        state = (db.query(QuestionSelectionState)
                 .filter(QuestionSelectionState.scope_key == scope_key)
                 .with_for_update().first())
        if state is None:
            state = QuestionSelectionState(scope_key=scope_key)
            db.add(state)
        topic_counts = dict(state.topic_counts or {})
        last_clusters = set(state.last_clusters or [])

    if actual_explore > 0 and remaining_pool:
        rng = random.Random(random_seed)
        pool_copy = list(remaining_pool)
        while len(selected_explore) < actual_explore and pool_copy:
            # 직전 주제는 뒤로 미루고, 덜 나온 주제부터 순환한다.
            fresh = [q for q in pool_copy if q.cluster_key not in last_clusters]
            eligible = fresh or pool_copy
            if state is not None:
                least_count = min(topic_counts.get(q.cluster_key, 0) for q in eligible)
                eligible = [q for q in eligible if topic_counts.get(q.cluster_key, 0) == least_count]
            weights = [
                (float(q.importance_weight) + 0.3) / math.sqrt(float(q.shown_count or 0) + 1.0)
                for q in eligible
            ]
            chosen = rng.choices(eligible, weights=weights, k=1)[0]
            selected_explore.append(chosen)
            topic_counts[chosen.cluster_key] = topic_counts.get(chosen.cluster_key, 0) + 1
            used_clusters.add(chosen.cluster_key)
            pool_copy = [q for q in pool_copy if q.cluster_key not in used_clusters]

    if state is not None:
        state.last_clusters = [q.cluster_key for q in selected_explore]
        state.topic_counts = topic_counts
        selected_ids = [q.id for q in selected_exploit + selected_explore]
        db.query(OrderSheetQuestion).filter(OrderSheetQuestion.id.in_(selected_ids)).update(
            {OrderSheetQuestion.shown_count: OrderSheetQuestion.shown_count + 1},
            synchronize_session="fetch",
        )
        db.commit()

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
            (opt for opt in _question_options(q) if opt.get("option_id") == ans.option_id),
            None,
        )
        if not matched_opt:
            raise InvalidOrderSheetException(
                f"질문({q.question_code})에 유효하지 않은 선택지 ID입니다: {ans.option_id}"
            )
        validated_answers.append((q, matched_opt))

    scored_candidates: list[tuple[float, list[str], Menu]] = []
    for menu in available_menus:
        score = 0.0
        matched_codes: list[str] = []
        for q, opt in validated_answers:
            rules = opt.get("rules", [])
            weight = float(q.importance_weight)
            if rules and evaluate_option_rules(menu, rules):
                score += weight * 1.0
                matched_codes.append(q.question_code)
        scored_candidates.append((round(score, 4), matched_codes, menu))

    top_items = select_diverse_menus(
        db, scored_candidates, request.user_id, request.top_k,
        total_weight=sum(float(q.importance_weight) for q, opt in validated_answers if opt.get("rules")),
    )

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
            name=format_menu_name(menu.name),
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
    [4·5단계 피드백 누적 데이터 기반 ML 가중치 재학습]
    누적된 RecommendationFeedback 로그의 (answer_match_results X, is_satisfied y)를 학습하여
    질문별 조건 일치 시 만족 확률 향상도(Empirical Lift + L2 정규화 로지스틱 가중치)를 계산하고,
    초기 initial_score와 결합해 MySQL의 importance_weight를 업데이트합니다.
    """
    questions = db.query(OrderSheetQuestion).all()
    feedbacks = [fb for fb in db.query(RecommendationFeedback).all() if fb.answer_match_results]

    if not feedbacks:
        return {q.question_code: round(float(q.importance_weight), 4) for q in questions}

    total_fb = len(feedbacks)
    global_pos = sum(1 for fb in feedbacks if fb.is_satisfied)
    base_rate = (global_pos + 1.0) / (total_fb + 2.0)

    stats: dict[str, dict[str, float]] = {}
    for fb in feedbacks:
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

    db.commit()
    return updated


def record_recommendation_feedback(
    db: Session,
    recommendation_id: int,
    request: RecommendationFeedbackRequest,
) -> RecommendationFeedbackResponse:
    """
    추천 결과에 대한 사용자(또는 4단계 개발팀) 피드백을 기록하고,
    즉시 질문 가중치 ML 재학습을 수행해 MySQL에 반영합니다.
    """
    rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
    if not rec:
        raise RecommendationNotFoundException(recommendation_id)

    target_menu_id = request.selected_menu_id
    if target_menu_id is None and rec.recommended_menu_ids:
        target_menu_id = rec.recommended_menu_ids[0]
    if target_menu_id not in (rec.recommended_menu_ids or []):
        raise InvalidOrderSheetException("해당 추천 결과에 포함된 메뉴만 평가할 수 있습니다.")
    if request.user_id is not None and rec.user_id != request.user_id:
        raise InvalidOrderSheetException("추천을 요청한 사용자와 피드백 사용자가 다릅니다.")

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
        # 상관없음은 취향 신호가 아니므로 질문 중요도 학습에서 제외한다.
        if not rules:
            continue

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

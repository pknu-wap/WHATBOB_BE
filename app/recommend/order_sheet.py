from datetime import datetime, timezone
import math
import random
from typing import Any
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.operations import (
    acquire_or_inspect_operation,
    compute_payload_hash,
    get_user_session_id,
    lock_user_and_verify_session,
    normalize_idempotency_key,
)
from app.auth.sessions import utc_now
from app.menus.display import format_menu_name
from app.menus.models import (
    Menu,
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
from app.recommend.question_pool import get_question_display
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
    *,
    commit: bool = True,
) -> OrderSheetQuestionsResponse:
    """
    [5단계 탐색+활용(Exploration-Exploitation) 질문 추출]
    - 상위 exploitation_count(기본 7개): 서로 다른 cluster_key의 importance_weight 최상위 질문 선택
    - 나머지 exploration_count(기본 2개): 아직 선택하지 않은 주제에서 노출 횟수를 고려해 가중 샘플링
    - 조회 단계에서는 순환 상태(QuestionSelectionState)만 갱신하며, 실제 노출 횟수(shown_count)는
      사용자가 주문서를 제출해 추천이 실행될 때 증가시킨다.
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
       - 후보가 top_k 미만으로 줄어드는 후순위 필터는 완화하여 점수제 질문으로 전환
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
        # 필터 적용 시 최소 추천 개수(top_k) 이상이 유지될 때만 하드 필터 확정
        if len(next_filtered) >= request.top_k:
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

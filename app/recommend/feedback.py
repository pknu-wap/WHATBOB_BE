from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from app.auth.operations import (
    acquire_or_inspect_operation,
    compute_payload_hash,
    get_user_session_id,
    lock_user_and_verify_session,
    normalize_idempotency_key,
)
from app.auth.sessions import as_utc_naive, utc_now
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
)
from app.recommend.common import (
    InvalidOrderSheetException,
    RecommendationDomainException,
    RecommendationNotFoundException,
    evaluate_option_rules,
)
from app.recommend.personal_experiment_events import record_personal_experiment_reaction
from app.recommend.question_pool import normalize_question_options as _question_options
from app.recommend.question_review import enqueue_rejected_initial_list_review
from app.recommend.schemas import (
    RecommendationFeedbackRequest,
    RecommendationFeedbackResponse,
    RecommendationRejectResponse,
)
from app.recommend.variety import menu_group
from app.users.models import User

OUTCOME_ZERO_CANDIDATES = "zero_candidates"
OUTCOME_FIRST_LIST_LIKED = "first_list_liked"
OUTCOME_LIST_REJECTED = "list_rejected"
OUTCOME_INDIVIDUAL_DISLIKED_ONLY = "individual_disliked_only"
OUTCOME_NO_RESPONSE = "no_response"


@dataclass(frozen=True)
class MenuReactionRecord:
    feedback_id: int
    recommendation_id: int
    selected_menu_id: int | None
    menu_key: str
    food_kind_id: int | None
    exposure_rank: int | None
    is_satisfied: bool
    reacted_at: datetime | None


@dataclass(frozen=True)
class RecommendationReactionOutcome:
    recommendation_id: int
    outcome: str
    shown_menu_ids: list[int]
    shown_count: int
    is_zero_candidates: bool
    is_first_list_liked: bool
    is_list_rejected: bool
    is_closed: bool
    list_rejected_at: datetime | None
    closed_at: datetime | None
    menu_reaction_count: int
    liked_menu_count: int
    disliked_menu_count: int
    first_liked_exposure_rank: int | None
    reactions: list[MenuReactionRecord]


@dataclass(frozen=True)
class FirstListReactionSummary:
    total_recommendations: int
    shown_list_count: int
    zero_candidates_count: int
    first_list_liked_count: int
    list_rejected_count: int
    individual_disliked_only_count: int
    no_response_count: int
    explicit_reaction_count: int
    total_menu_reaction_count: int
    total_liked_menu_count: int
    total_disliked_menu_count: int
    outcomes: list[RecommendationReactionOutcome]


def _shown_menu_ids(rec: Recommendation) -> list[int]:
    return [int(mid) for mid in (rec.recommended_menu_ids or []) if mid is not None]


def _resolve_exposure_rank(rec: Recommendation, menu_id: int | None) -> int | None:
    if menu_id is None:
        return None
    raw_ids = rec.recommended_menu_ids or []
    for idx, item_id in enumerate(raw_ids, start=1):
        if item_id is not None and int(item_id) == int(menu_id):
            return idx
    return None


def _as_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_zero_candidates_recommendation(rec: Recommendation) -> bool:
    if rec.initial_candidate_count is not None:
        return int(rec.initial_candidate_count) == 0
    return len(_shown_menu_ids(rec)) == 0


def _compute_answer_match_results(
    db: Session,
    rec: Recommendation,
    target_menu: Menu | None,
) -> dict[str, float]:
    answers = rec.answers_json or []
    q_ids = list({a["question_id"] for a in answers if "question_id" in a})
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
    return answer_match_results


def record_recommendation_feedback(
    db: Session,
    recommendation_id: int,
    request: RecommendationFeedbackRequest,
    user_id: int | None = None,
    current_user: User | None = None,
    idempotency_key: str | None = None,
) -> RecommendationFeedbackResponse:
    """
    첫 추천 목록의 메뉴별 좋아요·싫어요와 최초 노출 위치(1-based)를 추천 이력에 연결해 저장한다.
    Spec #77: 단일 메뉴의 피드백을 주문서 질문 전체의 학습 신호(importance_weight/positive_count)로
    전파하지 않으며, 동일 클릭의 재전송은 중복 집계하지 않는다.
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

    exposure_rank = _resolve_exposure_rank(rec, target_menu_id)
    owner_user_id = effective_user_id if effective_user_id is not None else rec.user_id
    payload_hash = compute_payload_hash(
        {
            "recommendation_id": int(recommendation_id),
            "selected_menu_id": int(target_menu_id),
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

    if rec.closed_at is not None or rec.list_rejected_at is not None:
        db.rollback()
        raise RecommendationDomainException(
            "이미 종료된 주문서에는 새로운 피드백을 기록할 수 없습니다. (ORDER_SHEET_CLOSED)",
            status_code=409,
        )

    target_menu = db.query(Menu).filter(Menu.id == target_menu_id).first()
    mkey = menu_group(target_menu) if target_menu is not None else f"menu:{target_menu_id}"
    m_base = (target_menu.base_name or target_menu.name or mkey).strip() if target_menu is not None else mkey
    m_ing = (target_menu.main_ingredient or "other").strip() if target_menu is not None else "other"
    food_kind_id = target_menu.food_kind_id if target_menu is not None else None

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

    desired_satisfied = bool(request.is_satisfied)
    duplicate_feedback_id: int | None = None
    if (
        existing_state is not None
        and existing_state.selected_menu_id == target_menu_id
        and bool(existing_state.is_satisfied) == desired_satisfied
        and existing_state.latest_feedback_id is not None
    ):
        duplicate_feedback_id = int(existing_state.latest_feedback_id)
        if existing_state.exposure_rank is None and exposure_rank is not None:
            existing_state.exposure_rank = exposure_rank
        if existing_state.food_kind_id is None and food_kind_id is not None:
            existing_state.food_kind_id = food_kind_id
    elif owner_user_id is None:
        latest_fb = (
            db.query(RecommendationFeedback)
            .filter(
                RecommendationFeedback.recommendation_id == rec.id,
                RecommendationFeedback.selected_menu_id == target_menu_id,
            )
            .order_by(RecommendationFeedback.id.desc())
            .first()
        )
        if latest_fb is not None and bool(latest_fb.is_satisfied) == desired_satisfied:
            duplicate_feedback_id = int(latest_fb.id)
            if latest_fb.exposure_rank is None and exposure_rank is not None:
                latest_fb.exposure_rank = exposure_rank
            if latest_fb.food_kind_id is None and food_kind_id is not None:
                latest_fb.food_kind_id = food_kind_id

    if duplicate_feedback_id is not None:
        response = RecommendationFeedbackResponse(
            feedback_id=duplicate_feedback_id,
            recommendation_id=rec.id,
            is_satisfied=desired_satisfied,
            updated_weights={},
            selected_menu_id=target_menu_id,
            exposure_rank=exposure_rank,
        )
        if operation is not None:
            operation.status = "SUCCEEDED"
            operation.resource_id = str(duplicate_feedback_id)
            operation.response_status = 200
            operation.response_json = response.model_dump()
            operation.updated_at = utc_now()
        db.commit()
        return response

    answer_match_results = _compute_answer_match_results(db, rec, target_menu)

    feedback = RecommendationFeedback(
        recommendation_id=rec.id,
        user_id=owner_user_id,
        selected_menu_id=target_menu_id,
        food_kind_id=food_kind_id,
        exposure_rank=exposure_rank,
        menu_key=mkey,
        menu_base_name=m_base,
        menu_main_ingredient=m_ing,
        operation_key=op_key,
        is_satisfied=desired_satisfied,
        answer_match_results=answer_match_results,
    )
    db.add(feedback)
    db.flush()

    if owner_user_id is not None:
        now = datetime.now(timezone.utc)
        if existing_state is None:
            state = RecommendationFeedbackState(
                recommendation_id=rec.id,
                user_id=owner_user_id,
                selected_menu_id=target_menu_id,
                food_kind_id=food_kind_id,
                exposure_rank=exposure_rank,
                menu_key=mkey,
                menu_base_name=m_base,
                menu_main_ingredient=m_ing,
                is_satisfied=desired_satisfied,
                answer_match_results=answer_match_results,
                latest_feedback_id=feedback.id,
                created_at=now,
                updated_at=now,
            )
            db.add(state)
        else:
            prev_satisfied = bool(existing_state.is_satisfied)
            existing_state.selected_menu_id = target_menu_id
            existing_state.food_kind_id = food_kind_id
            existing_state.exposure_rank = exposure_rank
            existing_state.menu_base_name = m_base
            existing_state.menu_main_ingredient = m_ing
            existing_state.is_satisfied = desired_satisfied
            existing_state.answer_match_results = answer_match_results
            existing_state.latest_feedback_id = feedback.id
            if prev_satisfied != desired_satisfied:
                existing_state.updated_at = now

    response = RecommendationFeedbackResponse(
        feedback_id=feedback.id,
        recommendation_id=rec.id,
        is_satisfied=bool(feedback.is_satisfied),
        updated_weights={},
        selected_menu_id=target_menu_id,
        exposure_rank=exposure_rank,
    )

    if operation is not None:
        operation.status = "SUCCEEDED"
        operation.resource_id = str(feedback.id)
        operation.response_status = 200
        operation.response_json = response.model_dump()
        operation.updated_at = utc_now()

    record_personal_experiment_reaction(db, rec.id, explicit=desired_satisfied)
    db.commit()
    return response


def reject_recommendation_list(
    db: Session,
    recommendation_id: int,
    user_id: int | None = None,
    current_user: User | None = None,
    idempotency_key: str | None = None,
) -> RecommendationRejectResponse:
    """
    사용자가 첫 추천 목록에서 '이 중엔 먹고 싶은 게 없어요'를 선택했을 때
    해당 주문서를 종료하고 명시적 목록 거절 한 건을 멱등하게 기록한다.
    최초 후보가 0건인 주문서는 409로 거절해 zero_candidates와 구별한다.
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

    if _is_zero_candidates_recommendation(rec):
        db.rollback()
        raise RecommendationDomainException(
            "표시된 추천 메뉴가 없는 주문서는 목록 거절할 수 없습니다. (ZERO_CANDIDATES_RECOMMENDATION)",
            status_code=409,
        )

    owner_user_id = effective_user_id if effective_user_id is not None else rec.user_id
    payload_hash = compute_payload_hash(
        {
            "recommendation_id": int(recommendation_id),
            "action": "LIST_REJECT",
        }
    )

    operation = None
    if op_key is not None and owner_user_id is not None:
        operation, is_existing = acquire_or_inspect_operation(
            db,
            user_id=owner_user_id,
            auth_session_id=get_user_session_id(current_user) if current_user is not None else None,
            scope="RECOMMENDATION_LIST_REJECT",
            operation_key=op_key,
            payload_hash=payload_hash,
        )
        if is_existing:
            if operation.payload_hash != payload_hash:
                db.rollback()
                raise RecommendationDomainException(
                    "동일한 Idempotency-Key로 다른 목록 거절 요청을 전송할 수 없습니다. (IDEMPOTENCY_KEY_REUSED)",
                    status_code=409,
                )
            if operation.status == "SUCCEEDED" and operation.response_json:
                db.rollback()
                return RecommendationRejectResponse(**operation.response_json)

    now = utc_now()
    if rec.list_rejected_at is None:
        rec.list_rejected_at = now
        enqueue_rejected_initial_list_review(db, rec, now)
        record_personal_experiment_reaction(db, rec.id, explicit=True)
    if rec.closed_at is None:
        rec.closed_at = rec.list_rejected_at

    db.flush()
    classified = classify_recommendation_outcome(db, rec.id)
    response = RecommendationRejectResponse(
        recommendation_id=rec.id,
        is_list_rejected=True,
        is_closed=True,
        list_rejected_at=_as_utc_aware(rec.list_rejected_at),
        closed_at=_as_utc_aware(rec.closed_at),
        outcome=classified.outcome,
    )

    if operation is not None:
        operation.status = "SUCCEEDED"
        operation.resource_id = str(rec.id)
        operation.response_status = 200
        operation.response_json = response.model_dump(mode="json")
        operation.updated_at = utc_now()

    db.commit()
    return response


def _collect_effective_reactions_for_recommendation(
    db: Session,
    rec: Recommendation,
    *,
    cutoff_utc: datetime | None = None,
) -> tuple[list[MenuReactionRecord], list[RecommendationFeedback]]:
    raw_feedbacks = (
        db.query(RecommendationFeedback)
        .filter(RecommendationFeedback.recommendation_id == rec.id)
        .order_by(RecommendationFeedback.id.asc())
        .all()
    )
    if cutoff_utc is not None:
        cutoff_naive = as_utc_naive(cutoff_utc)
        raw_feedbacks = [
            fb for fb in raw_feedbacks
            if fb.created_at is not None and as_utc_naive(fb.created_at) <= cutoff_naive
        ]

    if cutoff_utc is None:
        states = (
            db.query(RecommendationFeedbackState)
            .filter(RecommendationFeedbackState.recommendation_id == rec.id)
            .order_by(RecommendationFeedbackState.id.asc())
            .all()
        )
        if states:
            return [
                MenuReactionRecord(
                    feedback_id=int(st.latest_feedback_id or st.id),
                    recommendation_id=rec.id,
                    selected_menu_id=st.selected_menu_id,
                    menu_key=st.menu_key,
                    food_kind_id=st.food_kind_id,
                    exposure_rank=st.exposure_rank
                    if st.exposure_rank is not None
                    else _resolve_exposure_rank(rec, st.selected_menu_id),
                    is_satisfied=bool(st.is_satisfied),
                    reacted_at=st.updated_at or st.created_at,
                )
                for st in states
            ], raw_feedbacks

    latest_by_key: dict[str, RecommendationFeedback] = {}
    for fb in raw_feedbacks:
        key = fb.menu_key or (f"menu:{fb.selected_menu_id}" if fb.selected_menu_id is not None else f"fb:{fb.id}")
        latest_by_key[key] = fb

    return [
        MenuReactionRecord(
            feedback_id=int(fb.id),
            recommendation_id=rec.id,
            selected_menu_id=fb.selected_menu_id,
            menu_key=key,
            food_kind_id=fb.food_kind_id,
            exposure_rank=fb.exposure_rank
            if fb.exposure_rank is not None
            else _resolve_exposure_rank(rec, fb.selected_menu_id),
            is_satisfied=bool(fb.is_satisfied),
            reacted_at=fb.created_at,
        )
        for key, fb in latest_by_key.items()
    ], raw_feedbacks


def _build_outcome_from_loaded(
    rec: Recommendation,
    reactions: list[MenuReactionRecord],
    *,
    raw_feedbacks: list[RecommendationFeedback],
    cutoff_utc: datetime | None = None,
) -> RecommendationReactionOutcome:
    shown_ids = _shown_menu_ids(rec)
    is_zero = _is_zero_candidates_recommendation(rec)
    effective_reactions = [] if is_zero else reactions

    liked = [r for r in effective_reactions if r.is_satisfied]
    disliked = [r for r in effective_reactions if not r.is_satisfied]
    like_events = [feedback for feedback in raw_feedbacks if feedback.is_satisfied]
    first_list_liked = not is_zero and bool(liked or like_events)

    rejected_at = rec.list_rejected_at
    if rejected_at is not None and cutoff_utc is not None:
        if as_utc_naive(rejected_at) > as_utc_naive(cutoff_utc):
            rejected_at = None

    if is_zero:
        outcome = OUTCOME_ZERO_CANDIDATES
    elif first_list_liked:
        outcome = OUTCOME_FIRST_LIST_LIKED
    elif rejected_at is not None:
        outcome = OUTCOME_LIST_REJECTED
    elif disliked:
        outcome = OUTCOME_INDIVIDUAL_DISLIKED_ONLY
    else:
        outcome = OUTCOME_NO_RESPONSE

    liked_ranks = [r.exposure_rank for r in liked if r.exposure_rank is not None]
    liked_ranks.extend(
        rank
        for feedback in like_events
        if (rank := feedback.exposure_rank or _resolve_exposure_rank(rec, feedback.selected_menu_id)) is not None
    )
    return RecommendationReactionOutcome(
        recommendation_id=rec.id,
        outcome=outcome,
        shown_menu_ids=shown_ids,
        shown_count=len(shown_ids),
        is_zero_candidates=is_zero,
        is_first_list_liked=first_list_liked,
        is_list_rejected=not is_zero and rejected_at is not None,
        is_closed=(rec.closed_at is not None),
        list_rejected_at=rejected_at,
        closed_at=rec.closed_at,
        menu_reaction_count=len(effective_reactions),
        liked_menu_count=len(liked),
        disliked_menu_count=len(disliked),
        first_liked_exposure_rank=min(liked_ranks) if first_list_liked and liked_ranks else None,
        reactions=effective_reactions,
    )


def classify_recommendation_outcome(
    db: Session,
    recommendation_id: int,
    *,
    reaction_window_hours: float | None = None,
) -> RecommendationReactionOutcome:
    rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
    if rec is None:
        raise RecommendationNotFoundException(recommendation_id)

    cutoff_utc = None
    if reaction_window_hours is not None and rec.created_at is not None:
        cutoff_utc = as_utc_naive(rec.created_at) + timedelta(hours=float(reaction_window_hours))

    reactions, raw_feedbacks = (
        ([], [])
        if _is_zero_candidates_recommendation(rec)
        else _collect_effective_reactions_for_recommendation(db, rec, cutoff_utc=cutoff_utc)
    )
    return _build_outcome_from_loaded(rec, reactions, raw_feedbacks=raw_feedbacks, cutoff_utc=cutoff_utc)


def summarize_first_list_reactions(
    db: Session,
    *,
    user_id: int | None = None,
    recommendation_ids: Iterable[int] | None = None,
    reaction_window_hours: float | None = None,
    reaction_cutoffs: Mapping[int, datetime] | None = None,
    for_update: bool = False,
) -> FirstListReactionSummary:
    """
    추천 목록 집합에 대해 최초 후보 0건, 첫 목록 좋아요(1건 이상), 명시적 목록 거절,
    개별 싫어요만 있음, 무응답의 5가지 결과를 상호 배타적으로 분류·집계한다.
    """
    query = db.query(Recommendation)
    if user_id is not None:
        query = query.filter(Recommendation.user_id == user_id)
    if recommendation_ids is not None:
        id_list = [int(rid) for rid in recommendation_ids]
        if not id_list:
            return FirstListReactionSummary(
                total_recommendations=0,
                shown_list_count=0,
                zero_candidates_count=0,
                first_list_liked_count=0,
                list_rejected_count=0,
                individual_disliked_only_count=0,
                no_response_count=0,
                explicit_reaction_count=0,
                total_menu_reaction_count=0,
                total_liked_menu_count=0,
                total_disliked_menu_count=0,
                outcomes=[],
            )
        query = query.filter(Recommendation.id.in_(id_list))

    if for_update:
        # Feedback writers lock the recommendation before persisting a reaction.
        query = query.populate_existing().with_for_update()
    recs = query.order_by(Recommendation.created_at.asc(), Recommendation.id.asc()).all()
    if not recs:
        return FirstListReactionSummary(
            total_recommendations=0,
            shown_list_count=0,
            zero_candidates_count=0,
            first_list_liked_count=0,
            list_rejected_count=0,
            individual_disliked_only_count=0,
            no_response_count=0,
            explicit_reaction_count=0,
            total_menu_reaction_count=0,
            total_liked_menu_count=0,
            total_disliked_menu_count=0,
            outcomes=[],
        )

    rec_ids = [rec.id for rec in recs]
    states_by_rec: dict[int, list[RecommendationFeedbackState]] = {}
    if reaction_window_hours is None and reaction_cutoffs is None:
        for st in (
            db.query(RecommendationFeedbackState)
            .filter(RecommendationFeedbackState.recommendation_id.in_(rec_ids))
            .order_by(RecommendationFeedbackState.id.asc())
            .all()
        ):
            states_by_rec.setdefault(int(st.recommendation_id), []).append(st)

    feedbacks_by_rec: dict[int, list[RecommendationFeedback]] = {}
    feedback_query = (
        db.query(RecommendationFeedback)
        .filter(RecommendationFeedback.recommendation_id.in_(rec_ids))
        .order_by(RecommendationFeedback.id.asc())
    )
    if for_update:
        # Read committed reactions even if the worker's original snapshot predates the lock.
        feedback_query = feedback_query.populate_existing().with_for_update()
    for fb in feedback_query.all():
        feedbacks_by_rec.setdefault(int(fb.recommendation_id), []).append(fb)

    outcomes: list[RecommendationReactionOutcome] = []
    for rec in recs:
        cutoff_utc = reaction_cutoffs.get(rec.id) if reaction_cutoffs is not None else None
        if cutoff_utc is None and reaction_window_hours is not None and rec.created_at is not None:
            cutoff_utc = as_utc_naive(rec.created_at) + timedelta(hours=float(reaction_window_hours))

        raw_fbs = feedbacks_by_rec.get(rec.id, [])
        if cutoff_utc is not None:
            cutoff_utc = as_utc_naive(cutoff_utc)
            raw_fbs = [
                fb for fb in raw_fbs
                if fb.created_at is not None and as_utc_naive(fb.created_at) <= cutoff_utc
            ]
        if _is_zero_candidates_recommendation(rec):
            reactions: list[MenuReactionRecord] = []
        elif reaction_window_hours is None and rec.id in states_by_rec:
            reactions = [
                MenuReactionRecord(
                    feedback_id=int(st.latest_feedback_id or st.id),
                    recommendation_id=rec.id,
                    selected_menu_id=st.selected_menu_id,
                    menu_key=st.menu_key,
                    food_kind_id=st.food_kind_id,
                    exposure_rank=st.exposure_rank
                    if st.exposure_rank is not None
                    else _resolve_exposure_rank(rec, st.selected_menu_id),
                    is_satisfied=bool(st.is_satisfied),
                    reacted_at=st.updated_at or st.created_at,
                )
                for st in states_by_rec[rec.id]
            ]
        else:
            latest_by_key: dict[str, RecommendationFeedback] = {}
            for fb in raw_fbs:
                key = fb.menu_key or (
                    f"menu:{fb.selected_menu_id}" if fb.selected_menu_id is not None else f"fb:{fb.id}"
                )
                latest_by_key[key] = fb
            reactions = [
                MenuReactionRecord(
                    feedback_id=int(fb.id),
                    recommendation_id=rec.id,
                    selected_menu_id=fb.selected_menu_id,
                    menu_key=key,
                    food_kind_id=fb.food_kind_id,
                    exposure_rank=fb.exposure_rank
                    if fb.exposure_rank is not None
                    else _resolve_exposure_rank(rec, fb.selected_menu_id),
                    is_satisfied=bool(fb.is_satisfied),
                    reacted_at=fb.created_at,
                )
                for key, fb in latest_by_key.items()
            ]

        outcomes.append(_build_outcome_from_loaded(rec, reactions, raw_feedbacks=raw_fbs, cutoff_utc=cutoff_utc))

    zero_cnt = sum(1 for o in outcomes if o.outcome == OUTCOME_ZERO_CANDIDATES)
    liked_cnt = sum(o.is_first_list_liked for o in outcomes)
    rejected_cnt = sum(o.is_list_rejected for o in outcomes)
    disliked_only_cnt = sum(1 for o in outcomes if o.outcome == OUTCOME_INDIVIDUAL_DISLIKED_ONLY)
    no_resp_cnt = sum(1 for o in outcomes if o.outcome == OUTCOME_NO_RESPONSE)

    return FirstListReactionSummary(
        total_recommendations=len(outcomes),
        shown_list_count=len(outcomes) - zero_cnt,
        zero_candidates_count=zero_cnt,
        first_list_liked_count=liked_cnt,
        list_rejected_count=rejected_cnt,
        individual_disliked_only_count=disliked_only_cnt,
        no_response_count=no_resp_cnt,
        explicit_reaction_count=sum(o.is_first_list_liked or o.is_list_rejected for o in outcomes),
        total_menu_reaction_count=sum(o.menu_reaction_count for o in outcomes),
        total_liked_menu_count=sum(o.liked_menu_count for o in outcomes),
        total_disliked_menu_count=sum(o.disliked_menu_count for o in outcomes),
        outcomes=outcomes,
    )

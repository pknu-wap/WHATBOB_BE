"""Spec #77 / Ticket #87: 주문서별 하드필터 후보 축소 과정 기록 및 운영자 점검 조회 모듈.

이 모듈은 읽기·기록 전용 감사 경계이며, 축적된 점검 기록이나 1종 거절 집계 결과로
하드필터 설정(ORDER_SHEET_HARD_FILTER_CLUSTERS, ORDER_SHEET_QUESTION_ROLE_OVERRIDES)이나
OrderSheetQuestion 역할·가중치를 자동으로 교체하거나 수정하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.auth.sessions import as_utc_naive
from app.menus.models import OrderSheetHardFilterTrace, Recommendation
from app.recommend.common import RecommendationNotFoundException
from app.recommend.feedback import (
    OUTCOME_INDIVIDUAL_DISLIKED_ONLY,
    OUTCOME_NO_RESPONSE,
    FirstListReactionSummary,
    RecommendationReactionOutcome,
    classify_recommendation_outcome,
    summarize_first_list_reactions,
)
from app.recommend.ranking import HardFilterTraceStep


@dataclass(frozen=True)
class HardFilterTraceRecord:
    step_order: int
    question_id: int
    question_code: str
    cluster_key: str
    option_id: str
    before_count: int
    after_count: int
    before_kind_count: int
    after_kind_count: int
    created_at: datetime | None = None


@dataclass(frozen=True)
class OrderSheetHardFilterAudit:
    recommendation_id: int
    user_id: int | None
    created_at: datetime | None
    traces: list[HardFilterTraceRecord]
    applied_step_count: int
    initial_available_count: int | None
    initial_available_kind_count: int | None
    final_surviving_count: int | None
    final_surviving_kind_count: int | None
    recommended_menu_ids: list[int]
    outcome: RecommendationReactionOutcome
    is_single_candidate: bool
    is_single_candidate_rejection: bool
    is_single_candidate_liked: bool
    answer_combination: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SingleCandidateRejectionCombination:
    answer_combination: tuple[tuple[str, str], ...]
    rejection_count: int
    recommendation_ids: list[int]
    distinct_user_count: int
    rejected_menu_ids: list[int]


@dataclass(frozen=True)
class SingleCandidateRejectionSummary:
    total_single_candidate_rejections: int
    unique_combination_count: int
    repeated_combination_count: int
    combinations: list[SingleCandidateRejectionCombination]
    cases: list[OrderSheetHardFilterAudit]


@dataclass(frozen=True)
class HardFilterSatisfactionSummary:
    overall: FirstListReactionSummary
    single_candidate_total_count: int
    single_candidate_liked_count: int
    single_candidate_rejected_count: int
    single_candidate_disliked_only_count: int
    single_candidate_no_response_count: int
    multi_candidate_total_count: int
    multi_candidate_liked_count: int
    multi_candidate_rejected_count: int
    overall_like_rate: float | None
    overall_rejection_rate: float | None


def record_hard_filter_traces(
    db: Session,
    recommendation_id: int,
    user_id: int | None,
    traces: Sequence[HardFilterTraceStep],
    *,
    created_at: datetime | None = None,
) -> list[OrderSheetHardFilterTrace]:
    """
    주문서 추천과 동일한 트랜잭션 안에서 하드필터 단계별 추적 행을 저장한다.
    커밋은 호출자 트랜잭션에 위임하므로 추천 저장이 롤백되면 추적 행도 함께 롤백된다.
    """
    if not traces:
        return []
    ts = created_at or datetime.now(timezone.utc)
    rows: list[OrderSheetHardFilterTrace] = []
    for step in traces:
        row = OrderSheetHardFilterTrace(
            recommendation_id=int(recommendation_id),
            user_id=int(user_id) if user_id is not None else None,
            step_order=int(step.step_order),
            question_id=int(step.question_id),
            question_code=str(step.question_code),
            cluster_key=str(step.cluster_key),
            option_id=str(step.option_id),
            before_count=int(step.before_count),
            after_count=int(step.after_count),
            before_kind_count=int(step.before_kind_count),
            after_kind_count=int(step.after_kind_count),
            created_at=ts,
        )
        db.add(row)
        rows.append(row)
    return rows


def _to_trace_record(row: OrderSheetHardFilterTrace) -> HardFilterTraceRecord:
    return HardFilterTraceRecord(
        step_order=int(row.step_order),
        question_id=int(row.question_id),
        question_code=str(row.question_code),
        cluster_key=str(row.cluster_key),
        option_id=str(row.option_id),
        before_count=int(row.before_count),
        after_count=int(row.after_count),
        before_kind_count=int(row.before_kind_count),
        after_kind_count=int(row.after_kind_count),
        created_at=row.created_at,
    )


def _canonical_answer_combination(
    traces: Sequence[HardFilterTraceRecord],
) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((t.question_code, t.option_id) for t in traces))


def _build_audit_record(
    rec: Recommendation,
    trace_rows: Sequence[OrderSheetHardFilterTrace],
    outcome: RecommendationReactionOutcome,
) -> OrderSheetHardFilterAudit:
    traces = [_to_trace_record(row) for row in trace_rows]
    shown_ids = list(outcome.shown_menu_ids)

    if traces:
        initial_available_count: int | None = traces[0].before_count
        initial_available_kind_count: int | None = traces[0].before_kind_count
        final_surviving_count: int | None = traces[-1].after_count
        final_surviving_kind_count: int | None = traces[-1].after_kind_count
    else:
        initial_available_count = None
        initial_available_kind_count = (
            int(rec.initial_candidate_count)
            if rec.initial_candidate_count is not None
            else len(shown_ids)
        )
        final_surviving_count = None
        final_surviving_kind_count = initial_available_kind_count

    is_single_candidate = final_surviving_kind_count == 1
    return OrderSheetHardFilterAudit(
        recommendation_id=int(rec.id),
        user_id=int(rec.user_id) if rec.user_id is not None else None,
        created_at=rec.created_at,
        traces=traces,
        applied_step_count=len(traces),
        initial_available_count=initial_available_count,
        initial_available_kind_count=initial_available_kind_count,
        final_surviving_count=final_surviving_count,
        final_surviving_kind_count=final_surviving_kind_count,
        recommended_menu_ids=shown_ids,
        outcome=outcome,
        is_single_candidate=is_single_candidate,
        is_single_candidate_rejection=(
            is_single_candidate and outcome.is_list_rejected
        ),
        is_single_candidate_liked=(
            is_single_candidate and outcome.is_first_list_liked
        ),
        answer_combination=_canonical_answer_combination(traces),
    )


def get_order_sheet_hard_filter_audit(
    db: Session,
    recommendation_id: int,
    *,
    reaction_window_hours: float | None = None,
) -> OrderSheetHardFilterAudit:
    """주문서 추천 ID에 연결된 하드필터 축소 과정, 당시 첫 추천 목록, 사용자 반응을 함께 조회한다."""
    rec = db.query(Recommendation).filter(Recommendation.id == recommendation_id).first()
    if rec is None:
        raise RecommendationNotFoundException(recommendation_id)

    trace_rows = (
        db.query(OrderSheetHardFilterTrace)
        .filter(OrderSheetHardFilterTrace.recommendation_id == rec.id)
        .order_by(OrderSheetHardFilterTrace.step_order.asc(), OrderSheetHardFilterTrace.id.asc())
        .all()
    )
    outcome = classify_recommendation_outcome(
        db,
        rec.id,
        reaction_window_hours=reaction_window_hours,
    )
    return _build_audit_record(rec, trace_rows, outcome)


def _load_filtered_recommendations(
    db: Session,
    *,
    user_id: int | None = None,
    recommendation_ids: Iterable[int] | None = None,
    since_utc: datetime | None = None,
    until_utc: datetime | None = None,
) -> list[Recommendation]:
    query = db.query(Recommendation)
    if user_id is not None:
        query = query.filter(Recommendation.user_id == int(user_id))
    if recommendation_ids is not None:
        id_list = [int(rid) for rid in recommendation_ids]
        if not id_list:
            return []
        query = query.filter(Recommendation.id.in_(id_list))

    recs = query.order_by(Recommendation.created_at.asc(), Recommendation.id.asc()).all()
    if since_utc is None and until_utc is None:
        return recs

    since_naive = as_utc_naive(since_utc) if since_utc is not None else None
    until_naive = as_utc_naive(until_utc) if until_utc is not None else None
    filtered: list[Recommendation] = []
    for rec in recs:
        if rec.created_at is None:
            continue
        rec_ts = as_utc_naive(rec.created_at)
        if since_naive is not None and rec_ts < since_naive:
            continue
        if until_naive is not None and rec_ts > until_naive:
            continue
        filtered.append(rec)
    return filtered


def _load_traces_by_recommendation(
    db: Session,
    recommendation_ids: Sequence[int],
) -> dict[int, list[OrderSheetHardFilterTrace]]:
    if not recommendation_ids:
        return {}
    rows = (
        db.query(OrderSheetHardFilterTrace)
        .filter(OrderSheetHardFilterTrace.recommendation_id.in_(list(recommendation_ids)))
        .order_by(
            OrderSheetHardFilterTrace.recommendation_id.asc(),
            OrderSheetHardFilterTrace.step_order.asc(),
            OrderSheetHardFilterTrace.id.asc(),
        )
        .all()
    )
    grouped: dict[int, list[OrderSheetHardFilterTrace]] = {}
    for row in rows:
        grouped.setdefault(int(row.recommendation_id), []).append(row)
    return grouped


def summarize_single_candidate_rejections(
    db: Session,
    *,
    user_id: int | None = None,
    recommendation_ids: Iterable[int] | None = None,
    min_occurrences: int = 1,
    since_utc: datetime | None = None,
    until_utc: datetime | None = None,
    reaction_window_hours: float | None = None,
) -> SingleCandidateRejectionSummary:
    """
    모든 하드필터를 적용한 뒤 마지막 생존 음식 종류가 1종이었고
    사용자가 첫 목록을 명시적으로 거절(list_rejected)한 주문서를 식별해
    하드필터 (question_code, option_id) 답변 조합별로 집계한다.
    """
    threshold = max(1, int(min_occurrences))
    recs = _load_filtered_recommendations(
        db,
        user_id=user_id,
        recommendation_ids=recommendation_ids,
        since_utc=since_utc,
        until_utc=until_utc,
    )
    if not recs:
        return SingleCandidateRejectionSummary(
            total_single_candidate_rejections=0,
            unique_combination_count=0,
            repeated_combination_count=0,
            combinations=[],
            cases=[],
        )

    rec_by_id = {int(rec.id): rec for rec in recs}
    rec_ids = list(rec_by_id.keys())
    traces_by_rec = _load_traces_by_recommendation(db, rec_ids)
    reaction_summary = summarize_first_list_reactions(
        db,
        recommendation_ids=rec_ids,
        reaction_window_hours=reaction_window_hours,
    )

    grouped_cases: dict[tuple[tuple[str, str], ...], list[OrderSheetHardFilterAudit]] = {}
    for outcome in reaction_summary.outcomes:
        if not outcome.is_list_rejected:
            continue
        trace_rows = traces_by_rec.get(outcome.recommendation_id)
        if not trace_rows:
            continue
        if int(trace_rows[-1].after_kind_count) != 1:
            continue
        audit = _build_audit_record(rec_by_id[outcome.recommendation_id], trace_rows, outcome)
        grouped_cases.setdefault(audit.answer_combination, []).append(audit)

    combinations: list[SingleCandidateRejectionCombination] = []
    included_cases: list[OrderSheetHardFilterAudit] = []
    for combo_key, combo_cases in grouped_cases.items():
        if len(combo_cases) < threshold:
            continue
        user_ids = {c.user_id for c in combo_cases if c.user_id is not None}
        rejected_menu_ids = sorted(
            {mid for c in combo_cases for mid in c.recommended_menu_ids}
        )
        combinations.append(
            SingleCandidateRejectionCombination(
                answer_combination=combo_key,
                rejection_count=len(combo_cases),
                recommendation_ids=[c.recommendation_id for c in combo_cases],
                distinct_user_count=len(user_ids),
                rejected_menu_ids=rejected_menu_ids,
            )
        )
        included_cases.extend(combo_cases)

    combinations.sort(key=lambda item: (-item.rejection_count, item.answer_combination))
    included_cases.sort(key=lambda item: item.recommendation_id)

    return SingleCandidateRejectionSummary(
        total_single_candidate_rejections=len(included_cases),
        unique_combination_count=len(combinations),
        repeated_combination_count=sum(1 for c in combinations if c.rejection_count >= 2),
        combinations=combinations,
        cases=included_cases,
    )


def summarize_overall_satisfaction_with_hard_filter_audit(
    db: Session,
    *,
    user_id: int | None = None,
    recommendation_ids: Iterable[int] | None = None,
    since_utc: datetime | None = None,
    until_utc: datetime | None = None,
    reaction_window_hours: float | None = None,
) -> HardFilterSatisfactionSummary:
    """
    하드필터 후 후보가 1종뿐인 주문서의 좋아요·명시적 목록 거절을 포함해
    전체 첫 추천 만족도 지표(summarize_first_list_reactions)와 1종/2종 이상 내역을 함께 집계한다.
    """
    recs = _load_filtered_recommendations(
        db,
        user_id=user_id,
        recommendation_ids=recommendation_ids,
        since_utc=since_utc,
        until_utc=until_utc,
    )
    rec_by_id = {int(rec.id): rec for rec in recs}
    rec_ids = list(rec_by_id.keys())

    overall = summarize_first_list_reactions(
        db,
        recommendation_ids=rec_ids,
        reaction_window_hours=reaction_window_hours,
    )
    traces_by_rec = _load_traces_by_recommendation(db, rec_ids)

    single_total = 0
    single_liked = 0
    single_rejected = 0
    single_disliked_only = 0
    single_no_response = 0
    multi_total = 0
    multi_liked = 0
    multi_rejected = 0

    for outcome in overall.outcomes:
        if outcome.is_zero_candidates:
            continue
        rec = rec_by_id[outcome.recommendation_id]
        trace_rows = traces_by_rec.get(outcome.recommendation_id)
        if trace_rows:
            final_kind_count = int(trace_rows[-1].after_kind_count)
        elif rec.initial_candidate_count is not None:
            final_kind_count = int(rec.initial_candidate_count)
        else:
            final_kind_count = outcome.shown_count

        if final_kind_count == 1:
            single_total += 1
            if outcome.is_first_list_liked:
                single_liked += 1
            if outcome.is_list_rejected:
                single_rejected += 1
            if outcome.outcome == OUTCOME_INDIVIDUAL_DISLIKED_ONLY:
                single_disliked_only += 1
            elif outcome.outcome == OUTCOME_NO_RESPONSE:
                single_no_response += 1
        elif final_kind_count >= 2:
            multi_total += 1
            if outcome.is_first_list_liked:
                multi_liked += 1
            if outcome.is_list_rejected:
                multi_rejected += 1

    shown_count = overall.shown_list_count
    like_rate = (overall.first_list_liked_count / shown_count) if shown_count > 0 else None
    rejection_rate = (overall.list_rejected_count / shown_count) if shown_count > 0 else None

    return HardFilterSatisfactionSummary(
        overall=overall,
        single_candidate_total_count=single_total,
        single_candidate_liked_count=single_liked,
        single_candidate_rejected_count=single_rejected,
        single_candidate_disliked_only_count=single_disliked_only,
        single_candidate_no_response_count=single_no_response,
        multi_candidate_total_count=multi_total,
        multi_candidate_liked_count=multi_liked,
        multi_candidate_rejected_count=multi_rejected,
        overall_like_rate=like_rate,
        overall_rejection_rate=rejection_rate,
    )

"""Spec #77 §4 / Ticket #88: 과거 개인 주문서의 첫 추천 순위 재현 모듈.

하드필터 통과 전체 후보(6위 이하 및 동일 음식 종류 비대표 상품 포함)와
당시 질문별 가점·최근 식사 감점·장기 취향 보정·동점 처리 입력을
추천 이력과 같은 트랜잭션으로 1건의 불변 스냅샷에 저장하고,
이후 메뉴 변경·삭제나 이력 변화와 무관하게 순수 재계산으로 첫 목록을 재현한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.sessions import as_utc_naive, utc_now
from app.core.config import settings
from app.menus.models import OrderSheetReplaySnapshot, Recommendation
from app.recommend.ranking import OrderSheetEvaluationResult, RankedCandidate

logger = logging.getLogger(__name__)

SORT_RULE_NAME = "final_score_desc_food_code_asc_menu_id_asc"


@dataclass(frozen=True)
class ReplayedMenuEntry:
    rank: int
    menu_id: int
    food_code: str
    food_kind_key: str
    food_kind_id: int | None
    total_score: float
    rule_score: float
    recent_meal_adjustment: float
    personal_adjustment: float
    other_score: float


@dataclass(frozen=True)
class ReplayResult:
    recommendation_id: int
    omit_question_id: int | None
    replayed_menu_ids: list[int]
    replayed_entries: list[ReplayedMenuEntry]
    all_kind_entries: list[ReplayedMenuEntry]
    matches_shown: bool


@dataclass(frozen=True)
class VerifiedReplaySheet:
    snapshot: OrderSheetReplaySnapshot
    baseline_replay: ReplayResult


@dataclass(frozen=True)
class ExcludedReplaySheet:
    recommendation_id: int
    snapshot_id: int | None
    reason: str
    expected_menu_ids: list[int]
    replayed_menu_ids: list[int] | None
    details: dict[str, Any]


@dataclass(frozen=True)
class ReplayLoadBatchResult:
    user_id: int
    window_days: int
    max_sheets: int
    verified_sheets: list[VerifiedReplaySheet]
    excluded_sheets: list[ExcludedReplaySheet]


def _serialize_candidate(candidate: RankedCandidate) -> dict[str, Any]:
    menu = candidate.menu
    menu_id = int(menu.id)
    food_code = str(menu.food_code or "")
    question_scores = {
        str(int(qid)): round(float(score), 6)
        for qid, score in candidate.question_scores.items()
    }
    question_sum = round(sum(question_scores.values()), 6)
    recent_meal_adj = round(float(candidate.recent_meal_penalty), 6)
    personal_adj = round(float(candidate.long_term_score), 6)
    other_score = round(
        float(candidate.final_score) - (question_sum + recent_meal_adj + personal_adj),
        6,
    )
    food_kind_id = int(candidate.food_kind_id) if candidate.food_kind_id is not None else None
    dedup_kind_key = (
        f"kind_id:{food_kind_id}"
        if food_kind_id is not None
        else f"kind_key:{candidate.food_kind_key}"
    )
    return {
        "menu_id": menu_id,
        "food_code": food_code,
        "food_kind_key": str(candidate.food_kind_key),
        "food_kind_id": food_kind_id,
        "dedup_kind_key": dedup_kind_key,
        "question_scores": question_scores,
        "recent_meal_adjustment": recent_meal_adj,
        "personal_adjustment": personal_adj,
        "other_score": other_score,
        "tie_breaker": [food_code, menu_id],
    }


def save_replay_snapshot_if_eligible(
    db: Session,
    recommendation: Recommendation,
    evaluation: OrderSheetEvaluationResult,
    answers_payload: Sequence[dict[str, Any]],
    *,
    order_sheet_id: int | None = None,
    max_display_count: int = 5,
    completed_at: datetime | None = None,
    question_answer_version: str | None = None,
) -> OrderSheetReplaySnapshot | None:
    """
    하드필터 통과 음식 종류가 2종 이상인 개인 주문서에 한해
    주문서당 불변 재현 스냅샷 1건을 현재 트랜잭션에 저장한다.
    동일 recommendation_id(또는 동일 order_sheet_id)로 이미 저장된 기록이 있으면
    최초 기록을 수정하지 않고 그대로 반환한다.
    """
    if recommendation.user_id is None:
        return None

    surviving_kinds = getattr(
        evaluation,
        "surviving_kind_representatives",
        getattr(evaluation, "ranked_kinds", ()),
    )
    if len(surviving_kinds) < 2:
        return None

    existing = (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.recommendation_id == recommendation.id)
        .first()
    )
    if existing is not None:
        return existing

    if order_sheet_id is not None:
        existing_by_sheet = (
            db.query(OrderSheetReplaySnapshot)
            .filter(OrderSheetReplaySnapshot.order_sheet_id == order_sheet_id)
            .first()
        )
        if existing_by_sheet is not None:
            return existing_by_sheet

    completed_naive = (
        as_utc_naive(completed_at)
        if completed_at is not None
        else (
            as_utc_naive(recommendation.created_at)
            if recommendation.created_at is not None
            else utc_now()
        )
    )
    retention_days = int(settings.ORDER_SHEET_REPLAY_RETENTION_DAYS)
    expires_naive = completed_naive + timedelta(days=retention_days)

    shown_menu_ids = [
        int(mid)
        for mid in (
            recommendation.recommended_menu_ids
            if recommendation.recommended_menu_ids is not None
            else [cand.menu.id for cand in evaluation.selected_candidates]
        )
    ]
    effective_display_count = max(1, int(max_display_count))

    snapshot = OrderSheetReplaySnapshot(
        recommendation_id=int(recommendation.id),
        user_id=int(recommendation.user_id),
        order_sheet_id=int(order_sheet_id) if order_sheet_id is not None else None,
        completed_at=completed_naive,
        expires_at=expires_naive,
        question_answer_version=str(
            question_answer_version or settings.ORDER_SHEET_QUESTION_ANSWER_VERSION
        ),
        ranking_rule_version=str(
            evaluation.ranking_rule_version or settings.ORDER_SHEET_RANKING_RULE_VERSION
        ),
        max_display_count=effective_display_count,
        shown_menu_ids_json=shown_menu_ids,
        score_question_ids_json=[int(qid) for qid in evaluation.score_question_ids],
        answers_json=[dict(item) for item in answers_payload],
        candidates_json=[
            _serialize_candidate(cand) for cand in evaluation.surviving_candidates
        ],
        tie_break_inputs_json={
            "sort_rule": SORT_RULE_NAME,
            "uses_random_draw": False,
            "random_seed": None,
        },
        created_at=completed_naive,
    )

    try:
        with db.begin_nested():
            db.add(snapshot)
            db.flush()
    except IntegrityError:
        existing_after_race = (
            db.query(OrderSheetReplaySnapshot)
            .filter(OrderSheetReplaySnapshot.recommendation_id == recommendation.id)
            .first()
        )
        if existing_after_race is not None:
            return existing_after_race
        raise

    return snapshot


def _normalize_tie_breaker(entry: dict[str, Any]) -> tuple[Any, ...]:
    raw_tb = entry.get("tie_breaker")
    food_code = str(entry.get("food_code") or "")
    menu_id = int(entry["menu_id"])
    if isinstance(raw_tb, (list, tuple)):
        return tuple(raw_tb)
    if raw_tb is not None:
        return (raw_tb, food_code, menu_id)
    return (food_code, menu_id)


def _candidate_dedup_key(entry: dict[str, Any]) -> str:
    explicit = entry.get("dedup_kind_key")
    if explicit:
        return str(explicit)
    fk_id = entry.get("food_kind_id")
    if fk_id is not None:
        return f"kind_id:{int(fk_id)}"
    fk_key = entry.get("food_kind_key")
    if fk_key:
        return f"kind_key:{fk_key}"
    return f"menu_id:{int(entry['menu_id'])}"


def replay_first_list_from_snapshot(
    snapshot: OrderSheetReplaySnapshot,
    *,
    omit_question_id: int | None = None,
) -> ReplayResult:
    """
    저장된 스냅샷의 candidates_json과 고정 입력만으로 첫 추천 목록을 순수 재계산한다.
    DB(menus, user_meals, feedback 등)를 절대 다시 조회하지 않으므로
    이후 메뉴 수정·삭제나 이력 변경이 있어도 원래 순위를 결정적으로 재현한다.
    omit_question_id가 주어지면 해당 질문의 가점만 0으로 치환해 재계산한다.
    """
    omit_key = str(int(omit_question_id)) if omit_question_id is not None else None
    raw_candidates = snapshot.candidates_json
    if not isinstance(raw_candidates, list):
        raise ValueError("snapshot.candidates_json must be a list")

    scored_rows: list[tuple[tuple[float, tuple[Any, ...]], str, dict[str, Any], float, float, float, float, float]] = []
    for entry in raw_candidates:
        if not isinstance(entry, dict) or "menu_id" not in entry:
            raise ValueError("invalid candidate entry in snapshot.candidates_json")
        q_scores = entry.get("question_scores") or {}
        if not isinstance(q_scores, dict):
            raise ValueError("candidate question_scores must be a dict")

        rule_score = 0.0
        for qid_key, val in q_scores.items():
            if omit_key is not None and str(qid_key) == omit_key:
                continue
            rule_score += float(val)
        rule_score = round(rule_score, 6)

        recent_meal_adj = round(float(entry.get("recent_meal_adjustment", 0.0)), 6)
        personal_adj = round(float(entry.get("personal_adjustment", 0.0)), 6)
        other_score = round(float(entry.get("other_score", 0.0)), 6)
        total_score = round(rule_score + recent_meal_adj + personal_adj + other_score, 6)

        tie_breaker = _normalize_tie_breaker(entry)
        dedup_key = _candidate_dedup_key(entry)
        scored_rows.append(
            (
                (-total_score, tie_breaker),
                dedup_key,
                entry,
                total_score,
                rule_score,
                recent_meal_adj,
                personal_adj,
                other_score,
            )
        )

    scored_rows.sort(key=lambda item: item[0])

    seen_kinds: set[str] = set()
    all_kind_entries: list[ReplayedMenuEntry] = []
    for _, dedup_key, entry, total_score, rule_score, recent_meal_adj, personal_adj, other_score in scored_rows:
        if dedup_key in seen_kinds:
            continue
        seen_kinds.add(dedup_key)
        fk_id_raw = entry.get("food_kind_id")
        all_kind_entries.append(
            ReplayedMenuEntry(
                rank=len(all_kind_entries) + 1,
                menu_id=int(entry["menu_id"]),
                food_code=str(entry.get("food_code") or ""),
                food_kind_key=str(entry.get("food_kind_key") or ""),
                food_kind_id=int(fk_id_raw) if fk_id_raw is not None else None,
                total_score=total_score,
                rule_score=rule_score,
                recent_meal_adjustment=recent_meal_adj,
                personal_adjustment=personal_adj,
                other_score=other_score,
            )
        )

    limit = max(1, int(snapshot.max_display_count or settings.ORDER_SHEET_MAX_RECOMMENDATIONS))
    replayed_entries = all_kind_entries[:limit]
    replayed_menu_ids = [entry.menu_id for entry in replayed_entries]
    expected_shown = [int(mid) for mid in (snapshot.shown_menu_ids_json or [])]

    return ReplayResult(
        recommendation_id=int(snapshot.recommendation_id),
        omit_question_id=int(omit_question_id) if omit_question_id is not None else None,
        replayed_menu_ids=replayed_menu_ids,
        replayed_entries=replayed_entries,
        all_kind_entries=all_kind_entries,
        matches_shown=(replayed_menu_ids == expected_shown),
    )


def _group_round_recommendation_ids(
    db: Session,
    candidate_rec_ids: Sequence[int],
) -> set[int]:
    if not candidate_rec_ids:
        return set()
    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table("room_round_members"):
        return set()
    from app.rooms.models import RoomRoundMember

    rows = (
        db.query(RoomRoundMember.recommendation_id)
        .filter(RoomRoundMember.recommendation_id.in_(list(candidate_rec_ids)))
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def verify_and_load_recent_replay_snapshots(
    db: Session,
    user_id: int,
    *,
    now: datetime | None = None,
    window_days: int | None = None,
    max_sheets: int | None = None,
) -> ReplayLoadBatchResult:
    """
    Spec #77 §4 / #88:
    사용자·완료 시각 인덱스를 사용해 최근 window_days(기본 30일) 이내의
    미만료 재현 스냅샷을 최신순(completed_at DESC, id DESC)으로 최대 max_sheets(기본 20장) 조회한다.
    각 스냅샷의 재계산 결과가 저장된 첫 목록(shown_menu_ids_json)과 불일치하거나
    대상 개인 주문서의 재현 스냅샷이 누락된 경우 배치를 중단하지 않고 해당 주문서만 제외하며
    구조화된 제외 사유('missing_snapshot' / 'replay_mismatch')와 경고 로그를 남긴다.
    """
    effective_window_days = (
        int(window_days)
        if window_days is not None
        else int(settings.ORDER_SHEET_REPLAY_RETENTION_DAYS)
    )
    effective_max_sheets = (
        int(max_sheets)
        if max_sheets is not None
        else int(settings.ORDER_SHEET_REPLAY_MAX_SHEETS)
    )
    now_naive = as_utc_naive(now) if now is not None else utc_now()
    cutoff = now_naive - timedelta(days=effective_window_days)

    snapshots = (
        db.query(OrderSheetReplaySnapshot)
        .filter(
            OrderSheetReplaySnapshot.user_id == user_id,
            OrderSheetReplaySnapshot.completed_at >= cutoff,
            OrderSheetReplaySnapshot.expires_at > now_naive,
        )
        .order_by(
            OrderSheetReplaySnapshot.completed_at.desc(),
            OrderSheetReplaySnapshot.id.desc(),
        )
        .limit(effective_max_sheets)
        .all()
    )

    verified_sheets: list[VerifiedReplaySheet] = []
    excluded_sheets: list[ExcludedReplaySheet] = []

    for snap in snapshots:
        expected_ids = [int(mid) for mid in (snap.shown_menu_ids_json or [])]
        try:
            baseline = replay_first_list_from_snapshot(snap, omit_question_id=None)
        except (ValueError, TypeError, KeyError) as exc:
            details = {
                "error": str(exc),
                "ranking_rule_version": snap.ranking_rule_version,
                "question_answer_version": snap.question_answer_version,
            }
            logger.warning(
                "order_sheet_replay_excluded reason=replay_mismatch user_id=%s recommendation_id=%s snapshot_id=%s error=%s",
                user_id,
                snap.recommendation_id,
                snap.id,
                exc,
            )
            excluded_sheets.append(
                ExcludedReplaySheet(
                    recommendation_id=int(snap.recommendation_id),
                    snapshot_id=int(snap.id),
                    reason="replay_mismatch",
                    expected_menu_ids=expected_ids,
                    replayed_menu_ids=None,
                    details=details,
                )
            )
            continue

        if not baseline.matches_shown:
            details = {
                "ranking_rule_version": snap.ranking_rule_version,
                "question_answer_version": snap.question_answer_version,
            }
            logger.warning(
                "order_sheet_replay_excluded reason=replay_mismatch user_id=%s recommendation_id=%s snapshot_id=%s expected=%s replayed=%s",
                user_id,
                snap.recommendation_id,
                snap.id,
                expected_ids,
                baseline.replayed_menu_ids,
            )
            excluded_sheets.append(
                ExcludedReplaySheet(
                    recommendation_id=int(snap.recommendation_id),
                    snapshot_id=int(snap.id),
                    reason="replay_mismatch",
                    expected_menu_ids=expected_ids,
                    replayed_menu_ids=baseline.replayed_menu_ids,
                    details=details,
                )
            )
            continue

        verified_sheets.append(
            VerifiedReplaySheet(snapshot=snap, baseline_replay=baseline)
        )

    # 최근 윈도우 내 2종 이상 통과 개인 주문서 중 재현 스냅샷이 누락된 건을 탐지·제외 기록
    recent_eligible_recs = (
        db.query(Recommendation)
        .filter(
            Recommendation.user_id == user_id,
            Recommendation.created_at >= cutoff,
            Recommendation.initial_candidate_count >= 2,
        )
        .order_by(Recommendation.created_at.desc(), Recommendation.id.desc())
        .limit(effective_max_sheets)
        .all()
    )
    if recent_eligible_recs:
        rec_ids = [int(rec.id) for rec in recent_eligible_recs]
        existing_snap_rec_ids = {
            int(row[0])
            for row in db.query(OrderSheetReplaySnapshot.recommendation_id)
            .filter(OrderSheetReplaySnapshot.recommendation_id.in_(rec_ids))
            .all()
        }
        group_rec_ids = _group_round_recommendation_ids(db, rec_ids)
        for rec in recent_eligible_recs:
            rec_id = int(rec.id)
            if rec_id in existing_snap_rec_ids or rec_id in group_rec_ids:
                continue
            expected_ids = [int(mid) for mid in (rec.recommended_menu_ids or [])]
            logger.warning(
                "order_sheet_replay_excluded reason=missing_snapshot user_id=%s recommendation_id=%s",
                user_id,
                rec_id,
            )
            excluded_sheets.append(
                ExcludedReplaySheet(
                    recommendation_id=rec_id,
                    snapshot_id=None,
                    reason="missing_snapshot",
                    expected_menu_ids=expected_ids,
                    replayed_menu_ids=None,
                    details={
                        "initial_candidate_count": rec.initial_candidate_count,
                    },
                )
            )

    return ReplayLoadBatchResult(
        user_id=int(user_id),
        window_days=effective_window_days,
        max_sheets=effective_max_sheets,
        verified_sheets=verified_sheets,
        excluded_sheets=excluded_sheets,
    )


def purge_expired_replay_snapshots(
    db: Session,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """만료 시각(expires_at <= now)이 지난 재현 스냅샷만 삭제하고 기존 추천·피드백 이력은 보존한다."""
    now_naive = as_utc_naive(now) if now is not None else utc_now()
    deleted_count = (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.expires_at <= now_naive)
        .delete(synchronize_session=False)
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return int(deleted_count)

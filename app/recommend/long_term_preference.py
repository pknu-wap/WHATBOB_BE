"""Spec #77 / Ticket #83: 메뉴 평가를 음식 종류별 장기 취향 점수로 계산·조회하는 모듈."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.menus.food_kinds import classify_food_kind
from app.menus.models import FoodKind, Menu, RecommendationFeedbackState
from app.menus.popular import get_seoul_tz
from app.recommend.ranking import resolve_menu_food_kind_key


@dataclass(frozen=True)
class FoodKindReaction:
    food_kind_key: str
    is_satisfied: bool
    reacted_at: datetime
    recommendation_id: int | None = None


def _to_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_kst_date(value: datetime) -> date:
    return _to_utc_aware(value).astimezone(get_seoul_tz()).date()


def calculate_long_term_preference_scores(
    reactions: Sequence[FoodKindReaction],
    *,
    reference_time: datetime | None = None,
    min_days: int | None = None,
    half_life_days: float | None = None,
    max_score: float | None = None,
    exclude_recommendation_id: int | None = None,
) -> dict[str, float]:
    """
    음식 종류별 좋아요·싫어요 반응으로부터 장기 취향 보정 점수([-max_score, +max_score])를 계산한다.

    - 가중합이 음수면 싫어요 한 건부터 감점한다.
    - 가중합이 0 이상이면 한국 날짜(Asia/Seoul) 기준으로 같은 방향의 반응이 서로 다른
      min_days(초기 2)일 이상 쌓였을 때 활성화한다.
    - 같은 날 서로 다른 메뉴에 남긴 반응은 각각 가중치를 합산하되 날짜는 하루로 센다.
    - 반응 한 건의 가중치는 0.5 ** (경과 한국 일수 / half_life_days)이며,
      좋아요는 양수, 싫어요는 음수로 합산한 뒤 [-1.0, +1.0]로 제한하고 max_score(초기 0.25)를 곱한다.
    """
    if not reactions:
        return {}

    ref_utc = _to_utc_aware(reference_time if reference_time is not None else datetime.now(timezone.utc))
    ref_kst_date = ref_utc.astimezone(get_seoul_tz()).date()

    effective_min_days = (
        int(settings.ORDER_SHEET_LONG_TERM_MIN_DAYS) if min_days is None else int(min_days)
    )
    effective_half_life = float(
        settings.ORDER_SHEET_LONG_TERM_HALF_LIFE_DAYS if half_life_days is None else half_life_days
    )
    effective_max_score = float(
        settings.ORDER_SHEET_LONG_TERM_MAX_SCORE if max_score is None else max_score
    )
    if effective_half_life <= 0.0:
        raise ValueError("half_life_days는 0보다 커야 합니다.")
    if effective_min_days < 1:
        raise ValueError("min_days는 1 이상이어야 합니다.")
    if effective_max_score < 0.0:
        raise ValueError("max_score는 0 이상이어야 합니다.")

    grouped: dict[str, list[tuple[date, bool]]] = {}
    for reaction in reactions:
        key = (reaction.food_kind_key or "").strip()
        if not key:
            continue
        if (
            exclude_recommendation_id is not None
            and reaction.recommendation_id is not None
            and int(reaction.recommendation_id) == int(exclude_recommendation_id)
        ):
            continue
        reacted_utc = _to_utc_aware(reaction.reacted_at)
        if reacted_utc > ref_utc:
            continue
        reaction_kst_date = reacted_utc.astimezone(get_seoul_tz()).date()
        grouped.setdefault(key, []).append((reaction_kst_date, bool(reaction.is_satisfied)))

    scores: dict[str, float] = {}
    for kind_key, entries in grouped.items():
        like_days: set[date] = set()
        dislike_days: set[date] = set()
        net_weight = 0.0

        for reaction_kst_date, is_satisfied in entries:
            elapsed_days = max((ref_kst_date - reaction_kst_date).days, 0)
            weight = 0.5 ** (elapsed_days / effective_half_life)
            if is_satisfied:
                like_days.add(reaction_kst_date)
                net_weight += weight
            else:
                dislike_days.add(reaction_kst_date)
                net_weight -= weight

        if net_weight >= 0.0 and max(len(like_days), len(dislike_days)) < effective_min_days:
            continue

        clamped = max(-1.0, min(1.0, net_weight))
        score = round(clamped * effective_max_score, 6)
        if score == 0.0:
            score = 0.0
        scores[kind_key] = score

    return scores


def _resolve_feedback_state_food_kind_key(
    state: RecommendationFeedbackState,
    *,
    food_kinds_by_id: dict[int, FoodKind],
    menus_by_id: dict[int, Menu],
) -> str | None:
    if state.food_kind_id is not None:
        fk = food_kinds_by_id.get(int(state.food_kind_id))
        if fk is not None and (fk.kind_key or "").strip():
            return fk.kind_key.strip()

    if state.selected_menu_id is not None:
        menu = menus_by_id.get(int(state.selected_menu_id))
        if menu is not None:
            if menu.food_kind_id is not None:
                fk = food_kinds_by_id.get(int(menu.food_kind_id))
                if fk is not None and (fk.kind_key or "").strip():
                    return fk.kind_key.strip()
            return resolve_menu_food_kind_key(menu)

    raw_base = (state.menu_base_name or "").strip()
    raw_key = (state.menu_key or "").strip()
    if raw_base or raw_key:
        kind_key, _ = classify_food_kind(base_name=raw_base or None, name=raw_key or None)
        return kind_key

    if state.food_kind_id is not None:
        return f"kind_id:{int(state.food_kind_id)}"
    return None


def load_long_term_preference_scores(
    db: Session,
    user_id: int,
    *,
    reference_time: datetime | None = None,
    min_days: int | None = None,
    half_life_days: float | None = None,
    max_score: float | None = None,
    exclude_recommendation_id: int | None = None,
) -> dict[str, float]:
    """
    DB의 RecommendationFeedbackState(메뉴별 좋아요·싫어요 현재 유효 상태)에서만
    사용자의 음식 종류별 장기 취향 보정 점수를 조회·계산한다.
    목록 전체 거절(Recommendation.list_rejected_at)과 지도 가게 반응은 포함하지 않는다.
    """
    query = db.query(RecommendationFeedbackState).filter(
        RecommendationFeedbackState.user_id == user_id
    )
    if exclude_recommendation_id is not None:
        query = query.filter(
            RecommendationFeedbackState.recommendation_id != int(exclude_recommendation_id)
        )
    states = query.all()
    if not states:
        return {}

    menu_ids = {int(st.selected_menu_id) for st in states if st.selected_menu_id is not None}
    menus_by_id: dict[int, Menu] = {}
    if menu_ids:
        menus_by_id = {
            int(m.id): m for m in db.query(Menu).filter(Menu.id.in_(menu_ids)).all()
        }

    food_kind_ids = {int(st.food_kind_id) for st in states if st.food_kind_id is not None}
    food_kind_ids.update(
        int(m.food_kind_id) for m in menus_by_id.values() if m.food_kind_id is not None
    )
    food_kinds_by_id: dict[int, FoodKind] = {}
    if food_kind_ids:
        food_kinds_by_id = {
            int(fk.id): fk for fk in db.query(FoodKind).filter(FoodKind.id.in_(food_kind_ids)).all()
        }

    reactions: list[FoodKindReaction] = []
    for st in states:
        reacted_at = st.updated_at or st.created_at
        if reacted_at is None:
            continue
        kind_key = _resolve_feedback_state_food_kind_key(
            st,
            food_kinds_by_id=food_kinds_by_id,
            menus_by_id=menus_by_id,
        )
        if not kind_key:
            continue
        reactions.append(
            FoodKindReaction(
                food_kind_key=kind_key,
                is_satisfied=bool(st.is_satisfied),
                reacted_at=reacted_at,
                recommendation_id=int(st.recommendation_id) if st.recommendation_id is not None else None,
            )
        )

    return calculate_long_term_preference_scores(
        reactions,
        reference_time=reference_time,
        min_days=min_days,
        half_life_days=half_life_days,
        max_score=max_score,
        exclude_recommendation_id=exclude_recommendation_id,
    )

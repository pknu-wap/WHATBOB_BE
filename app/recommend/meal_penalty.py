"""Spec #77 / Ticket #82: 실제 식사 기록 기반 최근 식사 감점 계산 모듈."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.menus.display import format_menu_name
from app.menus.food_kinds import classify_food_kind
from app.menus.models import Menu, UserMeal
from app.menus.popular import get_seoul_tz


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _menu_kind_identity(menu: Menu) -> str | None:
    fk_id = getattr(menu, "food_kind_id", None)
    if fk_id is not None:
        return f"kind_id:{int(fk_id)}"
    raw_base = (getattr(menu, "base_name", None) or "").strip()
    raw_name = (getattr(menu, "name", None) or "").strip()
    if raw_base or raw_name:
        kind_key, _ = classify_food_kind(base_name=raw_base or None, name=raw_name or None)
        return f"kind_key:{kind_key}"
    return None


@dataclass(frozen=True)
class RecentMealPenaltyConfig:
    same_day: float = -2.0
    next_day: float = -1.0
    two_days_ago: float = -0.5
    other_same_kind: float = 0.0
    day_penalties: tuple[float, ...] | None = None
    other_same_kind_by_day: tuple[float, ...] | None = None

    @classmethod
    def from_settings(cls) -> RecentMealPenaltyConfig:
        return cls(
            same_day=float(settings.ORDER_SHEET_RECENT_MEAL_PENALTY_SAME_DAY),
            next_day=float(settings.ORDER_SHEET_RECENT_MEAL_PENALTY_NEXT_DAY),
            two_days_ago=float(settings.ORDER_SHEET_RECENT_MEAL_PENALTY_TWO_DAYS_AGO),
            other_same_kind=float(settings.ORDER_SHEET_RECENT_MEAL_PENALTY_OTHER_SAME_KIND),
        )

    @property
    def same_menu_schedule(self) -> tuple[float, ...]:
        if self.day_penalties is not None:
            return tuple(float(v) for v in self.day_penalties)
        return (float(self.same_day), float(self.next_day), float(self.two_days_ago))

    @property
    def active_window_days(self) -> int:
        same_len = len(self.same_menu_schedule)
        other_len = len(self.other_same_kind_by_day) if self.other_same_kind_by_day is not None else 0
        return max(same_len, other_len)

    def same_menu_penalty_for_day_diff(self, day_diff: int) -> float:
        schedule = self.same_menu_schedule
        if 0 <= day_diff < len(schedule):
            return schedule[day_diff]
        return 0.0

    def other_same_kind_penalty_for_day_diff(self, day_diff: int) -> float:
        if self.other_same_kind_by_day is not None:
            if 0 <= day_diff < len(self.other_same_kind_by_day):
                return float(self.other_same_kind_by_day[day_diff])
            return 0.0
        if 0 <= day_diff < len(self.same_menu_schedule):
            return float(self.other_same_kind)
        return 0.0


def compute_recent_meal_penalties(
    meals: Sequence[Any],
    menus: Sequence[Menu],
    *,
    now: datetime | None = None,
    config: RecentMealPenaltyConfig | None = None,
) -> dict[int, float]:
    """한국 날짜(Asia/Seoul) 기준으로 최근 식사 기록별 메뉴 감점을 합산한다."""
    if not meals or not menus:
        return {}

    effective_config = config if config is not None else RecentMealPenaltyConfig.from_settings()
    if effective_config.active_window_days <= 0:
        return {}

    seoul_tz = get_seoul_tz()
    now_utc = datetime.now(timezone.utc) if now is None else _as_utc(now)
    reference_date_kst = now_utc.astimezone(seoul_tz).date()

    menu_by_id: dict[int, Menu] = {}
    menu_ids_by_exact_name: dict[str, list[int]] = {}
    menu_ids_by_kind: dict[str, list[int]] = {}
    for menu in menus:
        raw_id = getattr(menu, "id", None)
        if raw_id is None:
            continue
        mid = int(raw_id)
        menu_by_id[mid] = menu

        raw_name = (getattr(menu, "name", None) or "").strip()
        candidate_names: set[str] = set()
        if raw_name:
            candidate_names.add(raw_name)
            formatted = (format_menu_name(raw_name) or "").strip()
            if formatted:
                candidate_names.add(formatted)
        for cname in candidate_names:
            menu_ids_by_exact_name.setdefault(cname, []).append(mid)

        kind_identity = _menu_kind_identity(menu)
        if kind_identity is not None:
            menu_ids_by_kind.setdefault(kind_identity, []).append(mid)

    penalties: dict[int, float] = {}

    for meal in meals:
        eaten_at = getattr(meal, "eaten_at", None)
        if eaten_at is None:
            continue
        eaten_utc = _as_utc(eaten_at)
        if eaten_utc > now_utc:
            continue
        eaten_date_kst = eaten_utc.astimezone(seoul_tz).date()
        day_diff = (reference_date_kst - eaten_date_kst).days
        if day_diff < 0 or day_diff >= effective_config.active_window_days:
            continue

        same_penalty = effective_config.same_menu_penalty_for_day_diff(day_diff)
        other_penalty = effective_config.other_same_kind_penalty_for_day_diff(day_diff)
        if same_penalty == 0.0 and other_penalty == 0.0:
            continue

        matched_menu_ids: set[int] = set()
        meal_menu_id = getattr(meal, "menu_id", None)
        meal_name = (getattr(meal, "menu_name", None) or "").strip()
        if meal_menu_id is not None:
            mid = int(meal_menu_id)
            if mid in menu_by_id:
                matched_menu_ids.add(mid)
        elif meal_name:
            matched_menu_ids.update(menu_ids_by_exact_name.get(meal_name, ()))

        if same_penalty != 0.0:
            for mid in matched_menu_ids:
                penalties[mid] = round(penalties.get(mid, 0.0) + same_penalty, 6)

        if other_penalty != 0.0:
            eaten_kinds: set[str] = set()
            for mid in matched_menu_ids:
                kind_identity = _menu_kind_identity(menu_by_id[mid])
                if kind_identity is not None:
                    eaten_kinds.add(kind_identity)
            if not eaten_kinds and meal_name:
                kind_key, _ = classify_food_kind(name=meal_name)
                eaten_kinds.add(f"kind_key:{kind_key}")

            other_targets: set[int] = set()
            for kind_identity in eaten_kinds:
                for candidate_mid in menu_ids_by_kind.get(kind_identity, ()):
                    if candidate_mid not in matched_menu_ids:
                        other_targets.add(candidate_mid)
            for mid in other_targets:
                penalties[mid] = round(penalties.get(mid, 0.0) + other_penalty, 6)

    return {mid: val for mid, val in penalties.items() if val != 0.0}


def load_recent_meal_penalties(
    db: Session,
    user_id: int | None,
    menus: Sequence[Menu],
    *,
    now: datetime | None = None,
    config: RecentMealPenaltyConfig | None = None,
) -> dict[int, float]:
    """DB에서 사용자의 활성 기간(KST 기준) 내 실제 식사 기록을 조회해 메뉴별 감점을 계산한다."""
    if user_id is None or not menus:
        return {}

    effective_config = config if config is not None else RecentMealPenaltyConfig.from_settings()
    if effective_config.active_window_days <= 0:
        return {}

    seoul_tz = get_seoul_tz()
    now_utc = datetime.now(timezone.utc) if now is None else _as_utc(now)
    reference_date_kst = now_utc.astimezone(seoul_tz).date()
    oldest_active_kst_date = reference_date_kst - timedelta(
        days=effective_config.active_window_days - 1
    )
    window_start_kst = datetime(
        oldest_active_kst_date.year,
        oldest_active_kst_date.month,
        oldest_active_kst_date.day,
        0,
        0,
        0,
        tzinfo=seoul_tz,
    )
    window_start_utc_naive = window_start_kst.astimezone(timezone.utc).replace(tzinfo=None)
    window_end_utc_naive = now_utc.replace(tzinfo=None)

    meals = (
        db.query(UserMeal)
        .filter(
            UserMeal.user_id == user_id,
            UserMeal.eaten_at >= window_start_utc_naive,
            UserMeal.eaten_at <= window_end_utc_naive,
        )
        .all()
    )
    if not meals:
        return {}

    return compute_recent_meal_penalties(
        meals=meals,
        menus=menus,
        now=now_utc,
        config=effective_config,
    )

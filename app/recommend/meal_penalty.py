"""Recent meal adjustments for the exact menus recorded by a user."""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from app.auth.sessions import as_utc_naive
from app.core.config import settings
from app.menus.models import UserMeal
from app.menus.popular import get_seoul_tz


def load_recent_meal_penalties(
    db: Session,
    user_id: int,
    *,
    reference_time: datetime,
) -> dict[int, float]:
    schedule = (
        settings.ORDER_SHEET_RECENT_MEAL_PENALTY_SAME_DAY,
        settings.ORDER_SHEET_RECENT_MEAL_PENALTY_NEXT_DAY,
        settings.ORDER_SHEET_RECENT_MEAL_PENALTY_TWO_DAYS_AGO,
    )
    seoul = get_seoul_tz()
    now_utc = as_utc_naive(reference_time)
    today = now_utc.replace(tzinfo=timezone.utc).astimezone(seoul).date()
    first_day = today - timedelta(days=len(schedule) - 1)
    window_start = as_utc_naive(datetime.combine(first_day, time.min, tzinfo=seoul))

    meals = (
        db.query(UserMeal.menu_id, UserMeal.eaten_at)
        .filter(
            UserMeal.user_id == user_id,
            UserMeal.menu_id.is_not(None),
            UserMeal.eaten_at >= window_start,
            UserMeal.eaten_at <= now_utc,
        )
        .all()
    )

    penalties: defaultdict[int, float] = defaultdict(float)
    for menu_id, eaten_at in meals:
        eaten_day = as_utc_naive(eaten_at).replace(tzinfo=timezone.utc).astimezone(seoul).date()
        penalties[menu_id] += schedule[(today - eaten_day).days]
    return dict(penalties)

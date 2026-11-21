from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.sessions import as_utc_naive
from app.menus.food_kinds import CANONICAL_FOOD_KINDS, resolve_menu_illustration_url
from app.menus.models import (
    FoodKind,
    InitialPopularFoodKind,
    Menu,
    PopularMenuRankingItem,
    PopularMenuRankingState,
    UserMeal,
)
from app.menus.schemas import PopularMenuItemResponse, PopularMenusResponse

INITIAL_POPULAR_MENU_COUNT = 8
POPULAR_RANKING_MAX_ITEMS = 8
POPULAR_RANKING_MIN_USERS = 5
POPULAR_RANKING_STATE_ID = 1
SEOUL_TIMEZONE_NAME = "Asia/Seoul"

# 이슈 #54 댓글 합의 순서: 떡볶이 → 돈까스 → 햄버거 → 마라탕 → 파스타 → 국밥 → 짜장면 → 제육볶음
DEFAULT_INITIAL_POPULAR_KIND_KEYS: tuple[str, ...] = tuple(
    spec.kind_key for spec in CANONICAL_FOOD_KINDS
)

_CANONICAL_NAME_BY_KEY: dict[str, str] = {
    spec.kind_key: spec.name for spec in CANONICAL_FOOD_KINDS
}


def _normalize_kind_keys(kind_keys: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_key in kind_keys:
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("초기 인기 메뉴의 음식 종류 키는 비어 있을 수 없습니다.")
        key = raw_key.strip()
        if key in seen:
            raise ValueError(f"초기 인기 메뉴에 중복된 음식 종류 키가 포함되어 있습니다: {key}")
        seen.add(key)
        normalized.append(key)

    if len(normalized) != INITIAL_POPULAR_MENU_COUNT:
        raise ValueError(
            f"초기 인기 메뉴는 정확히 {INITIAL_POPULAR_MENU_COUNT}개여야 합니다. "
            f"(입력: {len(normalized)}개)"
        )
    return normalized


def seed_initial_popular_food_kinds(
    db: Session,
    kind_keys: Sequence[str] | None = None,
    *,
    ensure_canonical_kinds: bool | None = None,
    commit: bool = True,
) -> list[InitialPopularFoodKind]:
    """초기 인기 메뉴 8종의 위치(1..8)와 음식 종류 연결을 한 트랜잭션으로 검증·반영한다."""
    use_default = kind_keys is None
    target_keys = _normalize_kind_keys(
        DEFAULT_INITIAL_POPULAR_KIND_KEYS if use_default else kind_keys
    )
    should_ensure_canonical = use_default if ensure_canonical_kinds is None else ensure_canonical_kinds

    with db.begin_nested():
        existing_kinds = {
            fk.kind_key: fk
            for fk in db.query(FoodKind).filter(FoodKind.kind_key.in_(target_keys)).all()
        }

        if should_ensure_canonical:
            created_any = False
            for key in target_keys:
                if key not in existing_kinds and key in _CANONICAL_NAME_BY_KEY:
                    fk = FoodKind(kind_key=key, name=_CANONICAL_NAME_BY_KEY[key])
                    db.add(fk)
                    existing_kinds[key] = fk
                    created_any = True
            if created_any:
                db.flush()

        missing_keys = [key for key in target_keys if key not in existing_kinds]
        if missing_keys:
            raise ValueError(
                f"등록되지 않은 음식 종류 키가 포함되어 있습니다: {', '.join(missing_keys)}"
            )

        desired_pairs = [
            (position, existing_kinds[key].id)
            for position, key in enumerate(target_keys, start=1)
        ]

        current_rows = (
            db.query(InitialPopularFoodKind)
            .order_by(InitialPopularFoodKind.position.asc())
            .all()
        )
        current_pairs = [(row.position, row.food_kind_id) for row in current_rows]
        if current_pairs == desired_pairs:
            result_rows = current_rows
        else:
            # position과 food_kind_id 모두 UNIQUE 제약이 있으므로 동일 트랜잭션 내에서 비우고 재적재한다.
            for row in current_rows:
                db.delete(row)
            db.flush()

            result_rows = [
                InitialPopularFoodKind(position=position, food_kind_id=food_kind_id)
                for position, food_kind_id in desired_pairs
            ]
            db.add_all(result_rows)
            db.flush()

    if commit:
        db.commit()
    return result_rows


def ensure_initial_popular_food_kinds(
    db: Session,
    *,
    commit: bool = True,
) -> list[InitialPopularFoodKind]:
    """초기 인기 메뉴 테이블이 비어 있을 때만 기본 8종을 등록하고, 기존 순서는 보존한다."""
    existing_rows = (
        db.query(InitialPopularFoodKind)
        .order_by(InitialPopularFoodKind.position.asc())
        .all()
    )
    if existing_rows:
        return existing_rows
    return seed_initial_popular_food_kinds(db, commit=commit)


def _fetch_initial_popular_rows(
    db: Session,
) -> list[tuple[InitialPopularFoodKind, FoodKind]]:
    return (
        db.query(InitialPopularFoodKind, FoodKind)
        .join(FoodKind, InitialPopularFoodKind.food_kind_id == FoodKind.id)
        .order_by(InitialPopularFoodKind.position.asc())
        .all()
    )


def get_seoul_tz() -> tzinfo:
    """Asia/Seoul 시간대를 반환한다. tzdata가 없는 환경에서는 고정 UTC+9로 폴백한다."""
    try:
        return ZoneInfo(SEOUL_TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=9), name=SEOUL_TIMEZONE_NAME)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_db_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return as_utc_naive(value)


def _resolve_kst_date(target: date | datetime) -> date:
    if isinstance(target, datetime):
        return _as_utc(target).astimezone(get_seoul_tz()).date()
    if isinstance(target, date):
        return target
    raise TypeError("target_kst_date는 date 또는 datetime이어야 합니다.")


def get_kst_previous_day_window_utc(
    target_kst_date: date | datetime,
) -> tuple[date, datetime, datetime]:
    """집계 기준 한국 날짜에 해당하는 [전날 00:00 KST, 당일 00:00 KST) 범위를 UTC로 계산한다."""
    resolved_date = _resolve_kst_date(target_kst_date)
    seoul_tz = get_seoul_tz()
    prev_date = resolved_date - timedelta(days=1)
    window_start_kst = datetime(prev_date.year, prev_date.month, prev_date.day, 0, 0, 0, tzinfo=seoul_tz)
    window_end_kst = datetime(
        resolved_date.year,
        resolved_date.month,
        resolved_date.day,
        0,
        0,
        0,
        tzinfo=seoul_tz,
    )
    return (
        resolved_date,
        window_start_kst.astimezone(timezone.utc),
        window_end_kst.astimezone(timezone.utc),
    )


@dataclass(frozen=True)
class PopularAggregationResult:
    target_kst_date: date
    window_start_utc: datetime
    window_end_utc: datetime
    distinct_user_count: int
    published: bool
    published_as_of: datetime | None
    item_count: int
    skipped: bool = False


def _lock_or_create_ranking_state(
    db: Session,
    *,
    now_utc_naive: datetime,
    can_rollback_outer: bool,
) -> PopularMenuRankingState:
    """popular_menu_ranking_state 단일 행(id=1)을 보장하고 쓰기 잠금(FOR UPDATE)으로 조회한다."""
    for _attempt in range(2):
        existing_id = (
            db.query(PopularMenuRankingState.id)
            .filter(PopularMenuRankingState.id == POPULAR_RANKING_STATE_ID)
            .scalar()
        )
        if existing_id is None:
            try:
                with db.begin_nested():
                    db.add(
                        PopularMenuRankingState(
                            id=POPULAR_RANKING_STATE_ID,
                            last_succeeded_kst_date=None,
                            published_as_of=None,
                            updated_at=now_utc_naive,
                        )
                    )
                    db.flush()
            except IntegrityError:
                # 동시 최초 실행이 id=1 행을 먼저 생성한 경우 잠금을 해제한 뒤 잠금 조회로 합류한다.
                if can_rollback_outer:
                    db.rollback()

        state = (
            db.query(PopularMenuRankingState)
            .filter(PopularMenuRankingState.id == POPULAR_RANKING_STATE_ID)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if state is not None:
            return state

    raise RuntimeError("popular_menu_ranking_state 단일 행(id=1) 잠금 획득에 실패했습니다.")


def _query_window_distinct_user_count(
    db: Session,
    *,
    window_start_naive: datetime,
    window_end_naive: datetime,
) -> int:
    """집계 기간 내 카탈로그에 존재하는 메뉴를 먹은 서로 다른 사용자 수를 계산한다."""
    return int(
        db.query(func.count(func.distinct(UserMeal.user_id)))
        .join(Menu, UserMeal.menu_id == Menu.id)
        .filter(
            UserMeal.eaten_at >= window_start_naive,
            UserMeal.eaten_at < window_end_naive,
        )
        .scalar()
        or 0
    )


def _query_window_kind_rankings(
    db: Session,
    *,
    window_start_naive: datetime,
    window_end_naive: datetime,
):
    """집계 기간 내 음식 종류별 고유 사용자 수·최근 식사 시각 기준으로 상위 8종을 조회한다."""
    user_count_expr = func.count(func.distinct(UserMeal.user_id)).label("user_count")
    last_eaten_at_expr = func.max(UserMeal.eaten_at).label("last_eaten_at")

    return (
        db.query(
            Menu.food_kind_id.label("food_kind_id"),
            user_count_expr,
            last_eaten_at_expr,
        )
        .join(Menu, UserMeal.menu_id == Menu.id)
        .filter(
            UserMeal.eaten_at >= window_start_naive,
            UserMeal.eaten_at < window_end_naive,
        )
        .group_by(Menu.food_kind_id)
        .order_by(
            user_count_expr.desc(),
            last_eaten_at_expr.desc(),
            Menu.food_kind_id.asc(),
        )
        .limit(POPULAR_RANKING_MAX_ITEMS)
        .all()
    )


def _replace_published_ranking_items(
    db: Session,
    ranked_rows,
    state: PopularMenuRankingState,
    *,
    window_end_naive: datetime,
    kst_date: date,
    now_utc_naive: datetime,
) -> list[PopularMenuRankingItem]:
    """기존 공개 순위 항목을 새 순위로 교체하고 상태 행의 기준 시각·성공 날짜를 함께 갱신한다."""
    db.query(PopularMenuRankingItem).delete(synchronize_session=False)
    db.flush()

    new_items = [
        PopularMenuRankingItem(
            rank_position=rank_position,
            food_kind_id=int(row.food_kind_id),
            user_count=int(row.user_count),
            last_eaten_at=_coerce_db_datetime(row.last_eaten_at),
        )
        for rank_position, row in enumerate(ranked_rows, start=1)
    ]
    db.add_all(new_items)
    state.published_as_of = window_end_naive
    state.last_succeeded_kst_date = kst_date
    state.updated_at = now_utc_naive
    db.flush()
    return new_items


def aggregate_popular_menus_for_kst_date(
    db: Session,
    target_kst_date: date | datetime,
    *,
    commit: bool = True,
) -> PopularAggregationResult:
    """한국시간 자정 기준으로 전날 하루치 식사 기록을 집계해 실제 인기 순위(최대 8종)를 저장한다."""
    kst_date, window_start_utc, window_end_utc = get_kst_previous_day_window_utc(target_kst_date)
    window_start_naive = window_start_utc.replace(tzinfo=None)
    window_end_naive = window_end_utc.replace(tzinfo=None)
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    can_rollback_outer = commit and not db.new and not db.dirty and not db.deleted

    def _run_locked_aggregation() -> PopularAggregationResult:
        state = _lock_or_create_ranking_state(
            db,
            now_utc_naive=now_utc_naive,
            can_rollback_outer=can_rollback_outer,
        )

        if state.last_succeeded_kst_date is not None and kst_date <= state.last_succeeded_kst_date:
            active_as_of = _as_utc(state.published_as_of) if state.published_as_of is not None else None
            return PopularAggregationResult(
                target_kst_date=kst_date,
                window_start_utc=window_start_utc,
                window_end_utc=window_end_utc,
                distinct_user_count=0,
                published=False,
                published_as_of=active_as_of,
                item_count=0,
                skipped=True,
            )

        distinct_user_count = _query_window_distinct_user_count(
            db,
            window_start_naive=window_start_naive,
            window_end_naive=window_end_naive,
        )

        if distinct_user_count < POPULAR_RANKING_MIN_USERS:
            state.last_succeeded_kst_date = kst_date
            state.updated_at = now_utc_naive
            db.flush()
            active_as_of = _as_utc(state.published_as_of) if state.published_as_of is not None else None
            return PopularAggregationResult(
                target_kst_date=kst_date,
                window_start_utc=window_start_utc,
                window_end_utc=window_end_utc,
                distinct_user_count=distinct_user_count,
                published=False,
                published_as_of=active_as_of,
                item_count=0,
                skipped=False,
            )

        ranked_rows = _query_window_kind_rankings(
            db,
            window_start_naive=window_start_naive,
            window_end_naive=window_end_naive,
        )
        new_items = _replace_published_ranking_items(
            db,
            ranked_rows,
            state,
            window_end_naive=window_end_naive,
            kst_date=kst_date,
            now_utc_naive=now_utc_naive,
        )

        return PopularAggregationResult(
            target_kst_date=kst_date,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
            distinct_user_count=distinct_user_count,
            published=True,
            published_as_of=window_end_utc,
            item_count=len(new_items),
            skipped=False,
        )

    if can_rollback_outer:
        try:
            result = _run_locked_aggregation()
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise

    with db.begin_nested():
        result = _run_locked_aggregation()
    if commit:
        db.commit()
    return result


def recover_missed_popular_menu_ranking(
    db: Session,
    now: datetime | None = None,
    *,
    commit: bool = True,
) -> PopularAggregationResult:
    """서버 재시작 시 현재 시각 기준 가장 최근에 지난 한국시간 자정 집계 1회만 보충 실행한다."""
    current_utc = datetime.now(timezone.utc) if now is None else _as_utc(now)
    latest_passed_kst_date = current_utc.astimezone(get_seoul_tz()).date()
    return aggregate_popular_menus_for_kst_date(
        db,
        latest_passed_kst_date,
        commit=commit,
    )


def _fetch_published_popular_rows(
    db: Session,
) -> list[tuple[PopularMenuRankingItem, FoodKind]]:
    has_catalog_menu = exists(1).where(Menu.food_kind_id == PopularMenuRankingItem.food_kind_id)
    return (
        db.query(PopularMenuRankingItem, FoodKind)
        .join(FoodKind, PopularMenuRankingItem.food_kind_id == FoodKind.id)
        .filter(has_catalog_menu)
        .order_by(PopularMenuRankingItem.rank_position.asc())
        .all()
    )


def _to_popular_menu_items(food_kinds: Sequence[FoodKind]) -> list[PopularMenuItemResponse]:
    return [
        PopularMenuItemResponse(
            food_kind_id=food_kind.id,
            name=food_kind.name,
            image_url=resolve_menu_illustration_url(food_kind.representative_image_url),
        )
        for food_kind in food_kinds
    ]


def get_popular_menus(db: Session) -> PopularMenusResponse:
    """요즘 인기 메뉴를 읽기 전용으로 조회한다. 발행된 실제 순위가 있으면 저장 순위를, 없으면 초기 인기 목록을 반환한다."""
    state = (
        db.query(PopularMenuRankingState)
        .filter(PopularMenuRankingState.id == POPULAR_RANKING_STATE_ID)
        .one_or_none()
    )
    if state is not None and state.published_as_of is not None:
        ranking_rows = _fetch_published_popular_rows(db)
        return PopularMenusResponse(
            source="aggregated",
            as_of=_as_utc(state.published_as_of),
            items=_to_popular_menu_items([food_kind for _item, food_kind in ranking_rows]),
        )

    rows = _fetch_initial_popular_rows(db)
    return PopularMenusResponse(
        source="initial",
        as_of=None,
        items=_to_popular_menu_items([food_kind for _entry, food_kind in rows]),
    )

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import os
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as app_main
from app.core.database import Base, get_db
from app.main import app
from app.menus.models import (
    Menu,
    PopularMenuRankingItem,
    PopularMenuRankingState,
    UserMeal,
)
from app.menus.popular import (
    aggregate_popular_menus_for_kst_date,
    get_seoul_tz,
    recover_missed_popular_menu_ranking,
)
from app.users.models import User

POPULAR_MENUS_URL = "/api/menus/popular"


def _build_menu(food_code: str, name: str, base_name: str, category_large: str = "기타") -> Menu:
    return Menu(
        food_code=food_code,
        name=name,
        base_name=base_name,
        category_large=category_large,
        category_medium=base_name,
    )


def _create_users(db: Session, count: int, *, start_kakao_id: int = 66001) -> list[User]:
    users = [
        User(
            kakao_id=start_kakao_id + idx,
            nickname=f"유지복구유저{start_kakao_id + idx}",
        )
        for idx in range(count)
    ]
    db.add_all(users)
    db.flush()
    return users


@pytest.fixture
def sqlite_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def popular_api(sqlite_engine):
    overrides = dict(app.dependency_overrides)
    try:
        with Session(sqlite_engine) as db:
            app.dependency_overrides[get_db] = lambda: db
            yield SimpleNamespace(
                client=TestClient(app),
                db=db,
                engine=sqlite_engine,
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def test_quiet_day_before_and_after_publication_updates_only_last_succeeded_date(popular_api):
    db = popular_api.db
    burger = _build_menu("T66-Q-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T66-Q-GB", "국밥_돼지국밥", "국밥")
    pasta = _build_menu("T66-Q-PS", "스파게티_토마토파스타", "스파게티")
    db.add_all([burger, gukbap, pasta])
    db.flush()

    users = _create_users(db, 5, start_kakao_id=66001)

    # Day 1 (KST 2026-11-15 -> 집계일 2026-11-16): 3명만 기록 (실제 순위 발행 전)
    day1_utc = datetime(2026, 11, 15, 3, 0, 0, tzinfo=timezone.utc)
    # Day 2 (KST 2026-11-16 -> 집계일 2026-11-17): 5명 기록 (햄버거 3명, 국밥 2명)
    day2_utc = datetime(2026, 11, 16, 3, 0, 0, tzinfo=timezone.utc)
    # Day 3 (KST 2026-11-17 -> 집계일 2026-11-18): 4명만 파스타 기록 (< 5명)
    day3_utc = datetime(2026, 11, 17, 3, 0, 0, tzinfo=timezone.utc)

    db.add_all(
        [
            *[
                UserMeal(user_id=users[i].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=day1_utc)
                for i in range(3)
            ],
            *[
                UserMeal(user_id=users[i].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day2_utc)
                for i in range(3)
            ],
            *[
                UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day2_utc)
                for i in range(3, 5)
            ],
            *[
                UserMeal(user_id=users[i].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=day3_utc)
                for i in range(4)
            ],
        ]
    )
    db.commit()

    # 1) 실제 순위 발행 전 5명 미만: last_succeeded_kst_date만 갱신, 초기 목록 유지
    res_day1 = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 16))
    assert res_day1.published is False
    assert res_day1.skipped is False
    assert res_day1.distinct_user_count == 3
    assert res_day1.published_as_of is None

    state = db.query(PopularMenuRankingState).filter(PopularMenuRankingState.id == 1).one()
    assert state.last_succeeded_kst_date == date(2026, 11, 16)
    assert state.published_as_of is None
    assert db.query(PopularMenuRankingItem).count() == 0

    data_initial = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data_initial["source"] == "initial"
    assert data_initial["as_of"] is None
    assert len(data_initial["items"]) == 8

    # 2) 다음 날 5명 이상: 실제 순위 최초 발행
    res_day2 = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 17))
    assert res_day2.published is True
    assert res_day2.skipped is False
    assert res_day2.distinct_user_count == 5
    assert res_day2.published_as_of == datetime(2026, 11, 16, 15, 0, 0, tzinfo=timezone.utc)

    # 3) 발행 이후 5명 미만: last_succeeded_kst_date만 갱신, 기존 순위와 as_of 보존
    res_day3 = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 18))
    assert res_day3.published is False
    assert res_day3.skipped is False
    assert res_day3.distinct_user_count == 4
    assert res_day3.published_as_of == datetime(2026, 11, 16, 15, 0, 0, tzinfo=timezone.utc)

    db.expire_all()
    state_after = db.query(PopularMenuRankingState).filter(PopularMenuRankingState.id == 1).one()
    assert state_after.last_succeeded_kst_date == date(2026, 11, 18)
    assert state_after.published_as_of == datetime(2026, 11, 16, 15, 0, 0)

    data_retained = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data_retained["source"] == "aggregated"
    assert data_retained["as_of"] == "2026-11-16T15:00:00Z"
    assert [item["name"] for item in data_retained["items"]] == ["햄버거", "국밥"]


def test_duplicate_same_date_and_older_date_aggregation_are_idempotently_skipped(popular_api):
    db = popular_api.db
    burger = _build_menu("T66-IDEM-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T66-IDEM-GB", "국밥_돼지국밥", "국밥")
    pasta = _build_menu("T66-IDEM-PS", "스파게티_알리오올리오", "스파게티")
    db.add_all([burger, gukbap, pasta])
    db.flush()

    users = _create_users(db, 6, start_kakao_id=66101)
    day1_utc = datetime(2026, 11, 18, 4, 0, 0, tzinfo=timezone.utc)  # 집계일 11-19 대상
    day2_utc = datetime(2026, 11, 19, 4, 0, 0, tzinfo=timezone.utc)  # 집계일 11-20 대상

    db.add_all(
        [
            *[
                UserMeal(user_id=users[i].id, menu_id=pasta.id, menu_name="알리오올리오", eaten_at=day1_utc)
                for i in range(5)
            ],
            *[
                UserMeal(user_id=users[i].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day2_utc)
                for i in range(3)
            ],
            *[
                UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day2_utc)
                for i in range(3, 5)
            ],
        ]
    )
    db.commit()

    # 11-20 자정 첫 실행 -> 햄버거(3) -> 국밥(2) 발행
    first = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert first.published is True
    assert first.skipped is False
    assert first.published_as_of == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)

    # 사후적으로 11-19 식사 기록에 국밥을 대거 추가하더라도, 이미 성공 처리된 11-20 재실행은 건너뛴다.
    db.add_all(
        [
            UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day2_utc + timedelta(hours=1))
            for i in range(3)
        ]
    )
    db.commit()

    duplicate = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert duplicate.skipped is True
    assert duplicate.published is False
    assert duplicate.published_as_of == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)

    # 더 과거 날짜(11-19)를 호출해도 기존 순위·as_of·last_succeeded_kst_date가 덮어쓰이거나 후퇴하지 않는다.
    older = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 19))
    assert older.skipped is True
    assert older.published is False
    assert older.published_as_of == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)

    db.expire_all()
    state = db.query(PopularMenuRankingState).filter(PopularMenuRankingState.id == 1).one()
    assert state.last_succeeded_kst_date == date(2026, 11, 20)
    assert state.published_as_of == datetime(2026, 11, 19, 15, 0, 0)

    data = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data["source"] == "aggregated"
    assert data["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in data["items"]] == ["햄버거", "국밥"]


def test_duplicate_same_date_after_quiet_day_is_also_skipped(popular_api):
    db = popular_api.db
    burger = _build_menu("T66-QIDEM-BG", "버거_치즈버거", "버거")
    db.add(burger)
    db.flush()

    users = _create_users(db, 5, start_kakao_id=66151)
    day_utc = datetime(2026, 11, 19, 4, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            UserMeal(user_id=users[i].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day_utc)
            for i in range(4)
        ]
    )
    db.commit()

    first = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert first.published is False
    assert first.skipped is False

    # 같은 날짜가 이미 성공 처리되었으므로 이후 5번째 기록이 추가되어도 동일 날짜 재실행은 건너뛴다.
    db.add(UserMeal(user_id=users[4].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day_utc))
    db.commit()

    second = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert second.skipped is True
    assert second.published is False
    assert second.published_as_of is None
    assert popular_api.client.get(POPULAR_MENUS_URL).json()["data"]["source"] == "initial"


def test_mid_transaction_failure_preserves_prior_state_and_allows_same_date_retry(popular_api):
    db = popular_api.db
    burger = _build_menu("T66-FAIL-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T66-FAIL-GB", "국밥_돼지국밥", "국밥")
    db.add_all([burger, gukbap])
    db.flush()

    users = _create_users(db, 5, start_kakao_id=66201)
    day1_utc = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
    day2_utc = datetime(2026, 11, 20, 3, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            *[UserMeal(user_id=u.id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day1_utc) for u in users],
            *[UserMeal(user_id=u.id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day2_utc) for u in users],
        ]
    )
    db.commit()

    # 11-20 자정 발행: 햄버거
    first = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert first.published is True

    # 11-21 자정 발행 중 PopularMenuRankingItem flush 단계에서 오류 발생 시뮬레이션
    def _fail_on_item_insert(session, _flush_context, _instances):
        if any(isinstance(obj, PopularMenuRankingItem) for obj in session.new):
            raise RuntimeError("simulated mid-transaction failure")

    event.listen(db, "before_flush", _fail_on_item_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated mid-transaction failure"):
            aggregate_popular_menus_for_kst_date(db, date(2026, 11, 21))
    finally:
        event.remove(db, "before_flush", _fail_on_item_insert)

    # 실패 직후 이전 순위(햄버거), as_of, last_succeeded_kst_date(11-20)가 온전히 보존되어야 한다.
    db.expire_all()
    state_after_fail = db.query(PopularMenuRankingState).filter(PopularMenuRankingState.id == 1).one()
    assert state_after_fail.last_succeeded_kst_date == date(2026, 11, 20)
    assert state_after_fail.published_as_of == datetime(2026, 11, 19, 15, 0, 0)

    data_after_fail = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data_after_fail["source"] == "aggregated"
    assert data_after_fail["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in data_after_fail["items"]] == ["햄버거"]

    # 동일 날짜(11-21)를 재시도하면 정상 발행되어야 한다.
    retry = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 21))
    assert retry.published is True
    assert retry.skipped is False
    assert retry.published_as_of == datetime(2026, 11, 20, 15, 0, 0, tzinfo=timezone.utc)

    data_after_retry = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data_after_retry["source"] == "aggregated"
    assert data_after_retry["as_of"] == "2026-11-20T15:00:00Z"
    assert [item["name"] for item in data_after_retry["items"]] == ["국밥"]


def test_restart_recovery_runs_only_latest_missed_midnight_and_skips_second_restart(popular_api):
    db = popular_api.db
    burger = _build_menu("T66-REC-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T66-REC-GB", "국밥_돼지국밥", "국밥")
    pasta = _build_menu("T66-REC-PS", "스파게티_알리오올리오", "스파게티")
    db.add_all([burger, gukbap, pasta])
    db.flush()

    users = _create_users(db, 6, start_kakao_id=66301)

    # 일요일(11-15 KST -> 월요일 11-16 자정 집계): 햄버거 5명
    sun_utc = datetime(2026, 11, 15, 3, 0, 0, tzinfo=timezone.utc)
    # 월요일(11-16 KST -> 화요일 11-17 자정 집계 대상, 건너뛰어져야 함): 국밥 5명
    mon_utc = datetime(2026, 11, 16, 3, 0, 0, tzinfo=timezone.utc)
    # 화요일(11-17 KST -> 수요일 11-18 자정 집계 대상, 복구되어야 함): 파스타 5명
    tue_utc = datetime(2026, 11, 17, 3, 0, 0, tzinfo=timezone.utc)

    db.add_all(
        [
            *[UserMeal(user_id=users[i].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=sun_utc) for i in range(5)],
            *[UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=mon_utc) for i in range(5)],
            *[UserMeal(user_id=users[i].id, menu_id=pasta.id, menu_name="알리오올리오", eaten_at=tue_utc) for i in range(5)],
        ]
    )
    db.commit()

    # 월요일(11-16) 자정 정상 발행 -> 햄버거
    mon_res = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 16))
    assert mon_res.published is True

    # 화요일·수요일 자정을 모두 놓친 뒤 수요일 오전 9시 KST에 재시작:
    # 화요일 자정(국밥)은 건너뛰고 가장 최근 놓친 수요일 자정(화요일 식사=파스타)만 1회 보충한다.
    wed_09_kst = datetime(2026, 11, 18, 9, 0, 0, tzinfo=get_seoul_tz())
    rec1 = recover_missed_popular_menu_ranking(db, now=wed_09_kst)
    assert rec1.target_kst_date == date(2026, 11, 18)
    assert rec1.published is True
    assert rec1.skipped is False
    assert rec1.published_as_of == datetime(2026, 11, 17, 15, 0, 0, tzinfo=timezone.utc)

    data1 = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data1["source"] == "aggregated"
    assert data1["as_of"] == "2026-11-17T15:00:00Z"
    assert [item["name"] for item in data1["items"]] == ["파스타"]

    # 수요일 오후에 추가 기록이 생기고 같은 날 다시 재시작해도 수요일 집계는 중복 실행되지 않는다.
    db.add_all(
        [
            UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=tue_utc + timedelta(hours=2))
            for i in range(6)
        ]
    )
    db.commit()

    wed_15_kst = datetime(2026, 11, 18, 15, 30, 0, tzinfo=get_seoul_tz())
    rec2 = recover_missed_popular_menu_ranking(db, now=wed_15_kst)
    assert rec2.target_kst_date == date(2026, 11, 18)
    assert rec2.skipped is True
    assert rec2.published is False

    data2 = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data2["as_of"] == "2026-11-17T15:00:00Z"
    assert [item["name"] for item in data2["items"]] == ["파스타"]


def test_restart_recovery_after_quiet_missed_day_retains_prior_ranking_and_skips_repeat_restart(
    popular_api,
):
    db = popular_api.db
    burger = _build_menu("T66-QREC-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T66-QREC-GB", "국밥_돼지국밥", "국밥")
    pasta = _build_menu("T66-QREC-PS", "스파게티_토마토파스타", "스파게티")
    db.add_all([burger, gukbap, pasta])
    db.flush()

    users = _create_users(db, 5, start_kakao_id=66401)
    sun_utc = datetime(2026, 11, 15, 3, 0, 0, tzinfo=timezone.utc)
    mon_utc = datetime(2026, 11, 16, 3, 0, 0, tzinfo=timezone.utc)
    tue_utc = datetime(2026, 11, 17, 3, 0, 0, tzinfo=timezone.utc)

    db.add_all(
        [
            # 일요일(월요일 자정 발행): 햄버거 5명
            *[UserMeal(user_id=u.id, menu_id=burger.id, menu_name="치즈버거", eaten_at=sun_utc) for u in users],
            # 월요일(화요일 자정 - 건너뛰어져야 함): 국밥 5명
            *[UserMeal(user_id=u.id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=mon_utc) for u in users],
            # 화요일(수요일 자정 복구 대상): 3명만 파스타 (< 5명)
            *[UserMeal(user_id=users[i].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=tue_utc) for i in range(3)],
        ]
    )
    db.commit()

    aggregate_popular_menus_for_kst_date(db, date(2026, 11, 16))

    # 수요일 오전 9시 재시작: 화요일 식사가 3명(<5)이므로 월요일(11-16) 순위(햄버거)와 as_of를 유지하고
    # last_succeeded_kst_date만 수요일(11-18)로 갱신한다. (화요일 자정 국밥으로 교체되면 안 됨!)
    rec1 = recover_missed_popular_menu_ranking(
        db,
        now=datetime(2026, 11, 18, 9, 0, 0, tzinfo=get_seoul_tz()),
    )
    assert rec1.target_kst_date == date(2026, 11, 18)
    assert rec1.published is False
    assert rec1.skipped is False
    assert rec1.distinct_user_count == 3
    assert rec1.published_as_of == datetime(2026, 11, 15, 15, 0, 0, tzinfo=timezone.utc)

    db.expire_all()
    state = db.query(PopularMenuRankingState).filter(PopularMenuRankingState.id == 1).one()
    assert state.last_succeeded_kst_date == date(2026, 11, 18)
    assert state.published_as_of == datetime(2026, 11, 15, 15, 0, 0)

    data = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data["source"] == "aggregated"
    assert data["as_of"] == "2026-11-15T15:00:00Z"
    assert [item["name"] for item in data["items"]] == ["햄버거"]

    # 같은 날 두 번째 재시작은 건너뛴다.
    rec2 = recover_missed_popular_menu_ranking(
        db,
        now=datetime(2026, 11, 18, 22, 0, 0, tzinfo=get_seoul_tz()),
    )
    assert rec2.skipped is True


def test_first_ever_restart_with_no_state_runs_latest_passed_kst_midnight_once(popular_api):
    db = popular_api.db
    gukbap = _build_menu("T66-FIRST-GB", "국밥_돼지국밥", "국밥")
    db.add(gukbap)
    db.flush()

    users = _create_users(db, 5, start_kakao_id=66501)
    # KST 2026-11-19 식사 기록
    prev_day_utc = datetime(2026, 11, 19, 5, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [UserMeal(user_id=u.id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=prev_day_utc) for u in users]
    )
    db.commit()

    assert db.query(PopularMenuRankingState).count() == 0

    # KST 2026-11-20 00:00:00 (자정 정각) 최초 기동 복구
    midnight_kst = datetime(2026, 11, 20, 0, 0, 0, tzinfo=get_seoul_tz())
    res = recover_missed_popular_menu_ranking(db, now=midnight_kst)
    assert res.target_kst_date == date(2026, 11, 20)
    assert res.published is True
    assert res.skipped is False
    assert res.published_as_of == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)

    # 같은 날 23:59:59 KST 재기동은 중복 실행하지 않는다.
    res_repeat = recover_missed_popular_menu_ranking(
        db,
        now=datetime(2026, 11, 20, 23, 59, 59, tzinfo=get_seoul_tz()),
    )
    assert res_repeat.target_kst_date == date(2026, 11, 20)
    assert res_repeat.skipped is True
    assert res_repeat.published is False


def test_fastapi_lifespan_runs_missed_midnight_recovery_once_and_survives_recovery_error(
    sqlite_engine,
    monkeypatch,
):
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sqlite_engine)

    # 현재 한국 날짜의 전날(어제)에 5명 식사 기록을 미리 넣어 두고 서버 재시작 복구를 검증한다.
    seoul_tz = get_seoul_tz()
    today_kst = datetime.now(timezone.utc).astimezone(seoul_tz).date()
    yesterday_kst = today_kst - timedelta(days=1)
    yesterday_noon_utc = datetime(
        yesterday_kst.year,
        yesterday_kst.month,
        yesterday_kst.day,
        12,
        0,
        0,
        tzinfo=seoul_tz,
    ).astimezone(timezone.utc)
    expected_as_of_utc = datetime(
        today_kst.year,
        today_kst.month,
        today_kst.day,
        0,
        0,
        0,
        tzinfo=seoul_tz,
    ).astimezone(timezone.utc)

    with TestingSessionLocal() as setup_db:
        burger = _build_menu("T66-LIFE-BG", "버거_치즈버거", "버거")
        gukbap = _build_menu("T66-LIFE-GB", "국밥_돼지국밥", "국밥")
        setup_db.add_all([burger, gukbap])
        setup_db.flush()
        burger_id = burger.id
        gukbap_id = gukbap.id
        users = _create_users(setup_db, 5, start_kakao_id=66601)
        user_ids = [u.id for u in users]
        setup_db.add_all(
            [UserMeal(user_id=uid, menu_id=burger_id, menu_name="치즈버거", eaten_at=yesterday_noon_utc) for uid in user_ids]
        )
        setup_db.commit()

    async def idle_loop():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(app_main, "engine", sqlite_engine)
    monkeypatch.setattr(app_main, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(app_main, "room_cleanup_loop", idle_loop)
    monkeypatch.setattr(app_main, "popular_ranking_scheduler_loop", idle_loop)

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override_get_db
    try:
        # 1) 첫 FastAPI 시작: 놓친 자정 집계 자동 복구 -> 즉시 aggregated 반환
        with TestClient(app) as client:
            resp1 = client.get(POPULAR_MENUS_URL)
            assert resp1.status_code == 200
            data1 = resp1.json()["data"]
            assert data1["source"] == "aggregated"
            assert data1["as_of"] == expected_as_of_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            assert [item["name"] for item in data1["items"]] == ["햄버거"]

        # 어제 국밥 기록을 더 추가한 뒤 같은 날 서버를 다시 시작해도 이미 성공한 오늘 집계는 중복 게시하지 않는다.
        with TestingSessionLocal() as db:
            db.add_all(
                [
                    UserMeal(
                        user_id=uid,
                        menu_id=gukbap_id,
                        menu_name="돼지국밥",
                        eaten_at=yesterday_noon_utc + timedelta(hours=1),
                    )
                    for uid in user_ids
                ]
            )
            db.commit()

        with TestClient(app) as client:
            resp2 = client.get(POPULAR_MENUS_URL)
            assert resp2.status_code == 200
            data2 = resp2.json()["data"]
            assert data2["source"] == "aggregated"
            assert [item["name"] for item in data2["items"]] == ["햄버거"]

        # 3) 시작 복구 중 일시 오류가 발생해도 FastAPI 앱 기동 자체는 실패하지 않는다.
        def broken_recovery(_db):
            raise RuntimeError("transient recovery failure")

        monkeypatch.setattr(app_main, "recover_missed_popular_menu_ranking", broken_recovery)
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            assert client.get(POPULAR_MENUS_URL).status_code == 200
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def test_saved_popular_ranking_hides_only_food_kinds_with_zero_remaining_catalog_menus(popular_api):
    db = popular_api.db
    burger_1 = _build_menu("T66-DEL-BG1", "버거_치즈버거", "버거")
    burger_2 = _build_menu("T66-DEL-BG2", "햄버거_불고기버거", "햄버거")
    gukbap = _build_menu("T66-DEL-GB1", "국밥_돼지국밥", "국밥")
    pasta = _build_menu("T66-DEL-PS1", "스파게티_토마토파스타", "스파게티")
    db.add_all([burger_1, burger_2, gukbap, pasta])
    db.flush()

    users = _create_users(db, 6, start_kakao_id=66701)
    meal_utc = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
    # 햄버거 3명 -> 국밥 2명 -> 파스타 1명
    db.add_all(
        [
            UserMeal(user_id=users[0].id, menu_id=burger_1.id, menu_name="치즈버거", eaten_at=meal_utc),
            UserMeal(user_id=users[1].id, menu_id=burger_1.id, menu_name="치즈버거", eaten_at=meal_utc),
            UserMeal(user_id=users[2].id, menu_id=burger_2.id, menu_name="불고기버거", eaten_at=meal_utc),
            UserMeal(user_id=users[3].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=meal_utc),
            UserMeal(user_id=users[4].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=meal_utc),
            UserMeal(user_id=users[5].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=meal_utc),
        ]
    )
    db.commit()

    res = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert res.published is True

    before = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert before["source"] == "aggregated"
    assert before["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in before["items"]] == ["햄버거", "국밥", "파스타"]

    # 1) 햄버거 종류의 메뉴 2개 중 1개만 삭제해도 다른 메뉴(불고기버거)가 남아 있으므로 햄버거는 유지된다.
    db.delete(burger_1)
    db.commit()

    after_partial = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert after_partial["source"] == "aggregated"
    assert after_partial["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in after_partial["items"]] == ["햄버거", "국밥", "파스타"]

    # 2) 국밥 종류의 유일한 카탈로그 메뉴를 삭제하면 국밥만 숨기고 나머지 순서와 as_of를 유지한다.
    db.delete(gukbap)
    db.commit()

    after_gukbap_deleted = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert after_gukbap_deleted["source"] == "aggregated"
    assert after_gukbap_deleted["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in after_gukbap_deleted["items"]] == ["햄버거", "파스타"]
    assert [item["food_kind_id"] for item in after_gukbap_deleted["items"]] == [
        burger_2.food_kind_id,
        pasta.food_kind_id,
    ]


def test_saved_popular_ranking_returns_empty_aggregated_list_when_all_ranked_kinds_lose_catalog_menus(
    popular_api,
):
    db = popular_api.db
    burger = _build_menu("T66-ALLDEL-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T66-ALLDEL-GB", "국밥_돼지국밥", "국밥")
    unranked_pasta = _build_menu("T66-ALLDEL-PS", "스파게티_알리오올리오", "스파게티")
    db.add_all([burger, gukbap, unranked_pasta])
    db.flush()

    users = _create_users(db, 5, start_kakao_id=66801)
    meal_utc = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            *[UserMeal(user_id=users[i].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=meal_utc) for i in range(3)],
            *[UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=meal_utc) for i in range(3, 5)],
        ]
    )
    db.commit()

    res = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert res.published is True

    # 저장 순위에 포함된 모든 음식 종류(햄버거, 국밥)의 카탈로그 메뉴를 전부 삭제
    db.delete(burger)
    db.delete(gukbap)
    db.commit()

    resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["source"] == "aggregated"
    assert data["as_of"] == "2026-11-19T15:00:00Z"
    assert data["items"] == []


@pytest.fixture
def mysql_engine():
    raw_url = os.environ.get("MYSQL_TEST_DATABASE_URL", "").strip()
    if not raw_url:
        pytest.skip("Set MYSQL_TEST_DATABASE_URL to run MySQL Ticket #66 integration tests.")

    url = make_url(raw_url)
    assert url.drivername.startswith("mysql"), "MYSQL_TEST_DATABASE_URL must use a MySQL driver."
    assert url.database and "test" in url.database.lower(), (
        "MYSQL_TEST_DATABASE_URL must point to an isolated test database."
    )

    schema = f"whatbob_t66_{uuid4().hex[:12]}"
    admin = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE `{schema}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    engine = create_engine(
        url.set(database=schema),
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE `{schema}`"))
        admin.dispose()


def test_mysql_cold_start_concurrent_aggregation_serializes_without_deadlock_or_duplicate_publish(
    mysql_engine,
):
    Base.metadata.create_all(mysql_engine)
    with Session(mysql_engine) as setup_db:
        burger = _build_menu("MY66-CS-BG", "버거_치즈버거", "버거")
        gukbap = _build_menu("MY66-CS-GB", "국밥_돼지국밥", "국밥")
        setup_db.add_all([burger, gukbap])
        setup_db.flush()
        users = _create_users(setup_db, 5, start_kakao_id=66901)
        meal_utc = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
        setup_db.add_all(
            [
                *[UserMeal(user_id=users[i].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=meal_utc) for i in range(3)],
                *[UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=meal_utc) for i in range(3, 5)],
            ]
        )
        setup_db.commit()

    # popular_menu_ranking_state가 비어 있는 최초 상태에서 3개 스레드가 동일 날짜 집계를 동시 실행한다.
    barrier = threading.Barrier(3)

    def _worker():
        barrier.wait(timeout=10.0)
        with Session(mysql_engine) as worker_db:
            return aggregate_popular_menus_for_kst_date(worker_db, date(2026, 11, 20))

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_worker) for _ in range(3)]
        results = [f.result(timeout=15.0) for f in futures]

    published_results = [r for r in results if r.published]
    skipped_results = [r for r in results if r.skipped]
    assert len(published_results) == 1
    assert len(skipped_results) == 2
    assert published_results[0].published_as_of == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)
    for skipped in skipped_results:
        assert skipped.published is False
        assert skipped.published_as_of == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)

    overrides = dict(app.dependency_overrides)
    try:
        def override_get_db():
            with Session(mysql_engine) as read_db:
                yield read_db

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        data = client.get(POPULAR_MENUS_URL).json()["data"]
        assert data["source"] == "aggregated"
        assert data["as_of"] == "2026-11-19T15:00:00Z"
        assert [item["name"] for item in data["items"]] == ["햄버거", "국밥"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def test_mysql_steady_state_concurrent_aggregation_locks_state_row_and_api_reads_consistent_snapshot(
    mysql_engine,
):
    Base.metadata.create_all(mysql_engine)
    with Session(mysql_engine) as setup_db:
        burger = _build_menu("MY66-LK-BG", "버거_치즈버거", "버거")
        gukbap = _build_menu("MY66-LK-GB", "국밥_돼지국밥", "국밥")
        pasta = _build_menu("MY66-LK-PS", "스파게티_토마토파스타", "스파게티")
        malatang = _build_menu("MY66-LK-ML", "마라탕_소고기마라탕", "마라탕")
        setup_db.add_all([burger, gukbap, pasta, malatang])
        setup_db.flush()
        users = _create_users(setup_db, 5, start_kakao_id=66921)

        day1_utc = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
        day2_utc = datetime(2026, 11, 20, 3, 0, 0, tzinfo=timezone.utc)
        setup_db.add_all(
            [
                *[UserMeal(user_id=users[i].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day1_utc) for i in range(3)],
                *[UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day1_utc) for i in range(3, 5)],
                *[UserMeal(user_id=users[i].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=day2_utc) for i in range(3)],
                *[UserMeal(user_id=users[i].id, menu_id=malatang.id, menu_name="소고기마라탕", eaten_at=day2_utc) for i in range(3, 5)],
            ]
        )
        setup_db.commit()

        # Day 1(11-20 자정) 선행 발행: 햄버거 -> 국밥
        day1_res = aggregate_popular_menus_for_kst_date(setup_db, date(2026, 11, 20))
        assert day1_res.published is True

    in_flight_ready = threading.Event()
    allow_commit = threading.Event()
    thread_b_started = threading.Event()
    thread_b_done = threading.Event()
    worker_results: dict[str, object] = {}
    worker_errors: list[BaseException] = []

    def _run_thread_a():
        try:
            with Session(mysql_engine) as db_a:
                paused_once = False

                def _pause_before_commit(_session):
                    nonlocal paused_once
                    if not paused_once:
                        paused_once = True
                        in_flight_ready.set()
                        assert allow_commit.wait(timeout=10.0), "Timed out waiting to commit Thread A"

                event.listen(db_a, "before_commit", _pause_before_commit)
                try:
                    worker_results["a"] = aggregate_popular_menus_for_kst_date(db_a, date(2026, 11, 21))
                finally:
                    event.remove(db_a, "before_commit", _pause_before_commit)
        except BaseException as exc:
            in_flight_ready.set()
            worker_errors.append(exc)

    def _run_thread_b():
        try:
            thread_b_started.set()
            with Session(mysql_engine) as db_b:
                worker_results["b"] = aggregate_popular_menus_for_kst_date(db_b, date(2026, 11, 21))
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            thread_b_done.set()

    overrides = dict(app.dependency_overrides)
    try:
        def override_get_db():
            with Session(mysql_engine) as read_db:
                yield read_db

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)

        thread_a = threading.Thread(target=_run_thread_a, daemon=True)
        thread_b = threading.Thread(target=_run_thread_b, daemon=True)
        thread_a.start()
        assert in_flight_ready.wait(timeout=10.0), "Thread A did not reach in-flight flush"

        # Thread A가 상태 행 FOR UPDATE 잠금을 잡은 채 커밋 대기 중일 때 Thread B 시작
        thread_b.start()
        assert thread_b_started.wait(timeout=5.0)
        time.sleep(0.35)
        # Thread B는 InnoDB FOR UPDATE 행 잠금에서 대기 중이어야 한다.
        assert not thread_b_done.is_set()

        # 같은 순간 공개 API는 잠금 없이 일관된 이전 스냅샷(Day 1: 햄버거 -> 국밥)을 읽어야 한다.
        mid_resp = client.get(POPULAR_MENUS_URL)
        assert mid_resp.status_code == 200
        mid_data = mid_resp.json()["data"]
        assert mid_data["source"] == "aggregated"
        assert mid_data["as_of"] == "2026-11-19T15:00:00Z"
        assert [item["name"] for item in mid_data["items"]] == ["햄버거", "국밥"]

        # Thread A 커밋 허용 -> Thread B 잠금 해제 후 이미 완료된 날짜(11-21)를 감지해 건너뛴다.
        allow_commit.set()
        thread_a.join(timeout=10.0)
        thread_b.join(timeout=10.0)
        assert not worker_errors, f"Unexpected worker errors: {worker_errors}"
        assert thread_b_done.is_set()

        res_a = worker_results["a"]
        res_b = worker_results["b"]
        assert res_a.published is True
        assert res_a.skipped is False
        assert res_a.published_as_of == datetime(2026, 11, 20, 15, 0, 0, tzinfo=timezone.utc)

        assert res_b.published is False
        assert res_b.skipped is True
        assert res_b.published_as_of == datetime(2026, 11, 20, 15, 0, 0, tzinfo=timezone.utc)

        # 커밋 직후 공개 API는 새 스냅샷(Day 2: 파스타 -> 마라탕)을 원자적으로 읽는다.
        after_resp = client.get(POPULAR_MENUS_URL)
        assert after_resp.status_code == 200
        after_data = after_resp.json()["data"]
        assert after_data["source"] == "aggregated"
        assert after_data["as_of"] == "2026-11-20T15:00:00Z"
        assert [item["name"] for item in after_data["items"]] == ["파스타", "마라탕"]
    finally:
        allow_commit.set()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def test_mysql_failure_rollback_retry_deleted_menu_hiding_and_restart_recovery(mysql_engine):
    Base.metadata.create_all(mysql_engine)
    with Session(mysql_engine) as db:
        burger = _build_menu("MY66-E2E-BG", "버거_치즈버거", "버거")
        gukbap = _build_menu("MY66-E2E-GB", "국밥_돼지국밥", "국밥")
        pasta = _build_menu("MY66-E2E-PS", "스파게티_알리오올리오", "스파게티")
        db.add_all([burger, gukbap, pasta])
        db.flush()
        burger_id = burger.id
        gukbap_id = gukbap.id
        pasta_id = pasta.id

        users = _create_users(db, 6, start_kakao_id=66951)
        day1_utc = datetime(2026, 11, 15, 3, 0, 0, tzinfo=timezone.utc)  # 11-16 자정: 햄버거(3), 국밥(2), 파스타(1)
        day2_utc = datetime(2026, 11, 16, 3, 0, 0, tzinfo=timezone.utc)  # 11-17 자정(놓침): 국밥(5)
        day3_utc = datetime(2026, 11, 17, 3, 0, 0, tzinfo=timezone.utc)  # 11-18 자정(복구): 파스타(3), 햄버거(2)
        db.add_all(
            [
                *[UserMeal(user_id=users[i].id, menu_id=burger_id, menu_name="치즈버거", eaten_at=day1_utc) for i in range(3)],
                *[UserMeal(user_id=users[i].id, menu_id=gukbap_id, menu_name="돼지국밥", eaten_at=day1_utc) for i in range(3, 5)],
                UserMeal(user_id=users[5].id, menu_id=pasta_id, menu_name="알리오올리오", eaten_at=day1_utc),
                *[UserMeal(user_id=users[i].id, menu_id=gukbap_id, menu_name="돼지국밥", eaten_at=day2_utc) for i in range(5)],
                *[UserMeal(user_id=users[i].id, menu_id=pasta_id, menu_name="알리오올리오", eaten_at=day3_utc) for i in range(3)],
                *[UserMeal(user_id=users[i].id, menu_id=burger_id, menu_name="치즈버거", eaten_at=day3_utc) for i in range(3, 5)],
            ]
        )
        db.commit()

        # 1) 월요일(11-16) 자정 발행: 햄버거 -> 국밥 -> 파스타
        assert aggregate_popular_menus_for_kst_date(db, date(2026, 11, 16)).published is True

        # 2) 수요일(11-18) 복구 도중 오류 발생 -> 완전 롤백(월요일 순위 및 11-16 성공일 보존)
        def _abort_flush(session, _flush_context, _instances):
            if any(isinstance(obj, PopularMenuRankingItem) for obj in session.new):
                raise RuntimeError("mysql simulated mid-tx failure")

        event.listen(db, "before_flush", _abort_flush)
        try:
            with pytest.raises(RuntimeError, match="mysql simulated mid-tx failure"):
                recover_missed_popular_menu_ranking(
                    db,
                    now=datetime(2026, 11, 18, 9, 0, 0, tzinfo=get_seoul_tz()),
                )
        finally:
            event.remove(db, "before_flush", _abort_flush)

    overrides = dict(app.dependency_overrides)
    try:
        def override_get_db():
            with Session(mysql_engine) as read_db:
                yield read_db

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)

        # 실패 직후 월요일 순위 유지 확인
        after_fail = client.get(POPULAR_MENUS_URL).json()["data"]
        assert after_fail["source"] == "aggregated"
        assert after_fail["as_of"] == "2026-11-15T15:00:00Z"
        assert [item["name"] for item in after_fail["items"]] == ["햄버거", "국밥", "파스타"]

        # 3) 수요일(11-18) 복구 재시도 성공: 화요일 자정(국밥)은 건너뛰고 수요일 자정(파스타 -> 햄버거)만 반영
        with Session(mysql_engine) as retry_db:
            rec = recover_missed_popular_menu_ranking(
                retry_db,
                now=datetime(2026, 11, 18, 9, 5, 0, tzinfo=get_seoul_tz()),
            )
            assert rec.published is True
            assert rec.skipped is False
            assert rec.target_kst_date == date(2026, 11, 18)

            # 같은 날 중복 복구는 건너뜀
            rec_dup = recover_missed_popular_menu_ranking(
                retry_db,
                now=datetime(2026, 11, 18, 12, 0, 0, tzinfo=get_seoul_tz()),
            )
            assert rec_dup.skipped is True

        after_rec = client.get(POPULAR_MENUS_URL).json()["data"]
        assert after_rec["source"] == "aggregated"
        assert after_rec["as_of"] == "2026-11-17T15:00:00Z"
        assert [item["name"] for item in after_rec["items"]] == ["파스타", "햄버거"]

        # 4) 저장 순위에서 파스타 메뉴를 모두 삭제하면 파스타만 숨기고 햄버거와 원래 as_of를 유지한다.
        with Session(mysql_engine) as del_db:
            del_db.delete(del_db.get(Menu, pasta_id))
            del_db.commit()

        after_one_del = client.get(POPULAR_MENUS_URL).json()["data"]
        assert after_one_del["source"] == "aggregated"
        assert after_one_del["as_of"] == "2026-11-17T15:00:00Z"
        assert [item["name"] for item in after_one_del["items"]] == ["햄버거"]

        # 5) 햄버거 메뉴까지 모두 삭제하면 source="aggregated", 원래 as_of, items=[]를 반환한다.
        with Session(mysql_engine) as del_db:
            del_db.delete(del_db.get(Menu, burger_id))
            del_db.commit()

        after_all_del = client.get(POPULAR_MENUS_URL).json()["data"]
        assert after_all_del["source"] == "aggregated"
        assert after_all_del["as_of"] == "2026-11-17T15:00:00Z"
        assert after_all_del["items"] == []
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)

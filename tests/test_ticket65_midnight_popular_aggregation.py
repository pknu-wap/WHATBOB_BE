import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.main as app_main
from app.core.database import Base, get_db
from app.main import app
from app.menus.food_kinds import DEFAULT_MENU_ILLUSTRATION_URL
from app.menus.models import (
    FoodKind,
    Menu,
    PopularMenuRankingItem,
    PopularMenuRankingState,
    UserMeal,
)
from app.menus.popular import (
    aggregate_popular_menus_for_kst_date,
    get_seoul_tz,
)
from app.menus.popular_scheduler import popular_ranking_scheduler_loop
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


def _create_users(db: Session, count: int, *, start_kakao_id: int = 65001) -> list[User]:
    users = [
        User(
            kakao_id=start_kakao_id + idx,
            nickname=f"집계유저{start_kakao_id + idx}",
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


def test_seoul_tz_resolves_utc_plus_9():
    seoul_tz = get_seoul_tz()
    sample = datetime(2026, 11, 20, 0, 0, 0, tzinfo=seoul_tz)
    assert sample.utcoffset() == timedelta(hours=9)
    assert sample.astimezone(timezone.utc) == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)


def test_four_users_keeps_initial_list_and_five_users_publishes_aggregated_ranking(popular_api):
    db = popular_api.db
    burger = _build_menu("T65-BG-1", "버거_치즈버거", "버거")
    gukbap = _build_menu("T65-GB-1", "국밥_돼지국밥", "국밥")
    pasta = _build_menu("T65-PS-1", "스파게티_토마토파스타", "스파게티")
    db.add_all([burger, gukbap, pasta])
    db.flush()

    users = _create_users(db, 5)
    # KST 2026-11-19 12:00:00 = UTC 2026-11-19 03:00:00
    day_mid_utc = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)

    # 4명의 유효 사용자만 기록
    db.add_all(
        [
            UserMeal(user_id=users[0].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day_mid_utc),
            UserMeal(user_id=users[1].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day_mid_utc),
            UserMeal(user_id=users[2].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day_mid_utc),
            UserMeal(user_id=users[3].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=day_mid_utc),
        ]
    )
    db.commit()

    result_4 = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert result_4.published is False
    assert result_4.distinct_user_count == 4
    assert result_4.published_as_of is None

    resp_4 = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp_4.status_code == 200
    data_4 = resp_4.json()["data"]
    assert data_4["source"] == "initial"
    assert data_4["as_of"] is None
    assert len(data_4["items"]) == 8

    # 5번째 사용자 추가 후 집계하면 실제 순위가 발행된다.
    db.add(
        UserMeal(
            user_id=users[4].id,
            menu_id=burger.id,
            menu_name="치즈버거",
            eaten_at=day_mid_utc + timedelta(hours=1),
        )
    )
    db.commit()

    result_5 = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert result_5.published is True
    assert result_5.distinct_user_count == 5
    assert result_5.published_as_of == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)
    assert result_5.item_count == 3

    resp_5 = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp_5.status_code == 200
    body_5 = resp_5.json()
    assert body_5["success"] is True
    assert body_5["message"] is None
    data_5 = body_5["data"]
    assert data_5["source"] == "aggregated"
    assert data_5["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in data_5["items"]] == ["햄버거", "국밥", "파스타"]
    assert [item["food_kind_id"] for item in data_5["items"]] == [
        burger.food_kind_id,
        gukbap.food_kind_id,
        pasta.food_kind_id,
    ]


def test_kst_midnight_window_boundaries_in_utc_and_delayed_datetime_target(popular_api):
    db = popular_api.db
    burger = _build_menu("T65-WIN-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T65-WIN-GB", "국밥_순대국밥", "국밥")
    malatang = _build_menu("T65-WIN-ML", "마라탕_소고기마라탕", "마라탕")
    pasta = _build_menu("T65-WIN-PS", "스파게티_알리오올리오", "스파게티")
    db.add_all([burger, gukbap, malatang, pasta])
    db.flush()

    users = _create_users(db, 7, start_kakao_id=65101)

    # KST 2026-11-20 00:00:00 = UTC 2026-11-19 15:00:00
    # 전날 KST 2026-11-19 [00:00:00, 24:00:00) = UTC [2026-11-18 15:00:00, 2026-11-19 15:00:00)
    before_start_utc = datetime(2026, 11, 18, 14, 59, 59, tzinfo=timezone.utc)  # KST 11-18 23:59:59 (제외)
    exact_start_utc = datetime(2026, 11, 18, 15, 0, 0, tzinfo=timezone.utc)     # KST 11-19 00:00:00 (포함)
    mid_day_utc = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)          # KST 11-19 12:00:00 (포함)
    last_second_utc = datetime(2026, 11, 19, 14, 59, 59, tzinfo=timezone.utc)   # KST 11-19 23:59:59 (포함)
    exact_end_utc = datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)       # KST 11-20 00:00:00 (제외)

    # 범위 밖 2명(before_start, exact_end) + 범위 안 4명 => 총 4명만 유효하므로 초기 목록 유지
    db.add_all(
        [
            UserMeal(user_id=users[0].id, menu_id=malatang.id, menu_name="마라탕", eaten_at=before_start_utc),
            UserMeal(user_id=users[1].id, menu_id=pasta.id, menu_name="알리오올리오", eaten_at=exact_end_utc),
            UserMeal(user_id=users[2].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=exact_start_utc),
            UserMeal(user_id=users[3].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=mid_day_utc),
            UserMeal(user_id=users[4].id, menu_id=gukbap.id, menu_name="순대국밥", eaten_at=mid_day_utc),
            UserMeal(user_id=users[5].id, menu_id=gukbap.id, menu_name="순대국밥", eaten_at=last_second_utc),
        ]
    )
    db.commit()

    res_under = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert res_under.published is False
    assert res_under.distinct_user_count == 4
    assert popular_api.client.get(POPULAR_MENUS_URL).json()["data"]["source"] == "initial"

    # 범위 안에 5번째 사용자(국밥, exact_start) 추가 -> 국밥 3명, 햄버거 2명; 마라탕·파스타는 범위 밖이라 제외
    db.add(
        UserMeal(user_id=users[6].id, menu_id=gukbap.id, menu_name="순대국밥", eaten_at=exact_start_utc)
    )
    db.commit()

    # 자정보다 7분 늦은 시각(KST 2026-11-20 00:07:30)을 datetime으로 넘겨도 기준 as_of는 정확히 자정(UTC 15:00:00Z)이다.
    delayed_kst = datetime(2026, 11, 20, 0, 7, 30, tzinfo=get_seoul_tz())
    res_pub = aggregate_popular_menus_for_kst_date(db, delayed_kst)
    assert res_pub.published is True
    assert res_pub.target_kst_date == date(2026, 11, 20)
    assert res_pub.distinct_user_count == 5
    assert res_pub.window_start_utc == exact_start_utc
    assert res_pub.window_end_utc == exact_end_utc

    data = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data["source"] == "aggregated"
    assert data["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in data["items"]] == ["국밥", "햄버거"]


def test_deleted_catalog_menu_excluded_from_both_five_user_threshold_and_ranking_scores(popular_api):
    db = popular_api.db
    burger_active = _build_menu("T65-DEL-BG1", "햄버거_불고기버거", "햄버거")
    burger_deleted = _build_menu("T65-DEL-BG2", "버거_단종치즈버거", "버거")
    gukbap = _build_menu("T65-DEL-GB1", "국밥_돼지국밥", "국밥")
    pasta = _build_menu("T65-DEL-PS1", "스파게티_토마토파스타", "스파게티")
    malatang_deleted = _build_menu("T65-DEL-ML1", "마라탕_단종마라탕", "마라탕")
    db.add_all([burger_active, burger_deleted, gukbap, pasta, malatang_deleted])
    db.flush()

    users = _create_users(db, 6, start_kakao_id=65201)
    meal_time = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)

    # 1) 5명이 기록했으나 그중 1명(users[4])의 유일한 기록 메뉴(malatang_deleted)가 삭제되어
    #    유효 사용자가 4명이 되면 초기 목록을 유지해야 한다.
    db.add_all(
        [
            UserMeal(user_id=users[0].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=meal_time),
            UserMeal(user_id=users[1].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=meal_time),
            UserMeal(user_id=users[2].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=meal_time),
            UserMeal(user_id=users[3].id, menu_id=burger_active.id, menu_name="불고기버거", eaten_at=meal_time),
            UserMeal(
                user_id=users[4].id,
                menu_id=malatang_deleted.id,
                menu_name="단종마라탕",
                eaten_at=meal_time,
            ),
            # users[0..2]가 삭제될 버거 메뉴도 먹음 (삭제 후 점수에 반영되면 안 됨)
            UserMeal(user_id=users[0].id, menu_id=burger_deleted.id, menu_name="단종치즈버거", eaten_at=meal_time),
            UserMeal(user_id=users[1].id, menu_id=burger_deleted.id, menu_name="단종치즈버거", eaten_at=meal_time),
            UserMeal(user_id=users[2].id, menu_id=burger_deleted.id, menu_name="단종치즈버거", eaten_at=meal_time),
        ]
    )
    db.commit()

    db.delete(malatang_deleted)
    db.delete(burger_deleted)
    db.commit()
    db.expire_all()

    res_4_valid = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert res_4_valid.published is False
    assert res_4_valid.distinct_user_count == 4
    data_before = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data_before["source"] == "initial"
    assert data_before["as_of"] is None

    # 2) users[4], users[5]가 유효한 파스타 기록을 추가해 유효 6명이 되면 실제 순위 발행:
    #    파스타 3명(users[2,4,5]) > 국밥 2명(users[0,1]) > 햄버거 1명(users[3])
    #    삭제된 burger_deleted(3명)와 malatang_deleted(1명)은 점수에서 완전히 제외되어야 한다.
    db.add_all(
        [
            UserMeal(user_id=users[4].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=meal_time),
            UserMeal(user_id=users[5].id, menu_id=pasta.id, menu_name="토마토파스타", eaten_at=meal_time),
        ]
    )
    db.commit()

    res_6_valid = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert res_6_valid.published is True
    assert res_6_valid.distinct_user_count == 6

    data_after = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data_after["source"] == "aggregated"
    assert data_after["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in data_after["items"]] == ["파스타", "국밥", "햄버거"]


def test_repeated_meals_counted_once_per_user_and_aliases_combined_into_one_food_kind(popular_api):
    db = popular_api.db
    burger_a = _build_menu("T65-AL-BG1", "버거_치즈버거", "버거")
    burger_b = _build_menu("T65-AL-BG2", "햄버거_불고기버거", "햄버거")
    burger_c = _build_menu("T65-AL-BG3", "버거_새우버거", "버거")
    jjajang_a = _build_menu("T65-AL-JJ1", "자장면_삼선자장면", "자장면")
    jjajang_b = _build_menu("T65-AL-JJ2", "짜장면_간짜장", "짜장면")
    jeyuk_a = _build_menu("T65-AL-JY1", "제육볶음_매콤제육", "제육볶음")
    jeyuk_b = _build_menu("T65-AL-JY2", "돼지고기볶음(제육볶음)_직화제육", "돼지고기볶음(제육볶음)")
    tteokbokki = _build_menu("T65-AL-TB1", "떡볶이_국물떡볶이", "떡볶이")
    db.add_all([burger_a, burger_b, burger_c, jjajang_a, jjajang_b, jeyuk_a, jeyuk_b, tteokbokki])
    db.flush()

    users = _create_users(db, 5, start_kakao_id=65301)
    base_time = datetime(2026, 11, 19, 1, 0, 0, tzinfo=timezone.utc)

    meals = [
        # users[0]가 떡볶이를 10번 먹어도 떡볶이의 고유 사용자 수는 1명이다.
        *[
            UserMeal(
                user_id=users[0].id,
                menu_id=tteokbokki.id,
                menu_name="국물떡볶이",
                eaten_at=base_time + timedelta(minutes=i * 10),
            )
            for i in range(10)
        ],
        # 버거 + 햄버거 별칭 합산: users[0](2회), users[1], users[2], users[3] => 4명
        UserMeal(user_id=users[0].id, menu_id=burger_a.id, menu_name="치즈버거", eaten_at=base_time),
        UserMeal(
            user_id=users[0].id,
            menu_id=burger_b.id,
            menu_name="불고기버거",
            eaten_at=base_time + timedelta(hours=1),
        ),
        UserMeal(user_id=users[1].id, menu_id=burger_a.id, menu_name="치즈버거", eaten_at=base_time),
        UserMeal(user_id=users[2].id, menu_id=burger_b.id, menu_name="불고기버거", eaten_at=base_time),
        UserMeal(user_id=users[3].id, menu_id=burger_c.id, menu_name="새우버거", eaten_at=base_time),
        # 자장면 + 짜장면 별칭 합산: users[1], users[2], users[4](2회) => 3명
        UserMeal(user_id=users[1].id, menu_id=jjajang_a.id, menu_name="삼선자장면", eaten_at=base_time),
        UserMeal(user_id=users[2].id, menu_id=jjajang_b.id, menu_name="간짜장", eaten_at=base_time),
        UserMeal(user_id=users[4].id, menu_id=jjajang_a.id, menu_name="삼선자장면", eaten_at=base_time),
        UserMeal(
            user_id=users[4].id,
            menu_id=jjajang_b.id,
            menu_name="간짜장",
            eaten_at=base_time + timedelta(hours=2),
        ),
        # 제육볶음 + 돼지고기볶음(제육볶음) 별칭 합산: users[3], users[4] => 2명
        UserMeal(user_id=users[3].id, menu_id=jeyuk_a.id, menu_name="매콤제육", eaten_at=base_time),
        UserMeal(user_id=users[4].id, menu_id=jeyuk_b.id, menu_name="직화제육", eaten_at=base_time),
    ]
    db.add_all(meals)
    db.commit()

    result = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert result.published is True
    assert result.distinct_user_count == 5
    assert result.item_count == 4

    data = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data["source"] == "aggregated"
    assert [item["name"] for item in data["items"]] == ["햄버거", "짜장면", "제육볶음", "떡볶이"]
    assert [item["food_kind_id"] for item in data["items"]] == [
        burger_a.food_kind_id,
        jjajang_a.food_kind_id,
        jeyuk_a.food_kind_id,
        tteokbokki.food_kind_id,
    ]


def test_tie_breaking_order_and_max_eight_food_kinds_cap(popular_api):
    db = popular_api.db
    # 10개 음식 종류를 명시적으로 생성해 ID 순서를 고정한다 (kinds[0].id < ... < kinds[9].id).
    kind_specs = [
        ("kind_01", "김밥"),
        ("kind_02", "라면"),
        ("kind_03", "비빔밥"),
        ("kind_04", "냉면"),
        ("kind_05", "칼국수"),
        ("kind_06", "순두부찌개"),
        ("kind_07", "김치찌개"),
        ("kind_08", "된장찌개"),
        ("kind_09", "우동"),
        ("kind_10", "카레"),
    ]
    menus: list[Menu] = []
    for idx, (key, name) in enumerate(kind_specs, start=1):
        fk = FoodKind(kind_key=key, name=name)
        db.add(fk)
        db.flush()
        menu = Menu(
            food_code=f"T65-TIE-{idx:02d}",
            name=f"{name}_기본",
            base_name=name,
            food_kind_id=fk.id,
            category_large="기타",
            category_medium=name,
        )
        db.add(menu)
        db.flush()
        menus.append(menu)

    users = _create_users(db, 6, start_kakao_id=65401)
    t_early = datetime(2026, 11, 19, 1, 0, 0, tzinfo=timezone.utc)
    t_mid = datetime(2026, 11, 19, 5, 0, 0, tzinfo=timezone.utc)
    t_late = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)

    # 순위 기대값 계산:
    # 1위: 카레(idx=9) -> 4명
    # 2위: 우동(idx=8) -> 3명, last_eaten_at=t_late  (동률 시 최근 식사 시각 내림차순)
    # 3위: 김밥(idx=0) -> 3명, last_eaten_at=t_early (ID가 작아도 시각이 앞선 우동이 우선)
    # 4위: 라면(idx=1) -> 2명, last_eaten_at=t_mid   (동률 + 동시각 시 food_kind_id 오름차순)
    # 5위: 된장찌개(idx=7) -> 2명, last_eaten_at=t_mid
    # 6위: 김치찌개(idx=6) -> 1명, last_eaten_at=t_late
    # 7위: 비빔밥(idx=2) -> 1명, last_eaten_at=t_mid (동률 + 동시각 시 id 오름차순: 비빔밥 < 냉면 < 칼국수)
    # 8위: 냉면(idx=3) -> 1명, last_eaten_at=t_mid
    # 제외(9~10위): 칼국수(idx=4, 1명 t_mid), 순두부찌개(idx=5, 1명 t_early)
    meals = [
        # 카레 (4명)
        *[UserMeal(user_id=users[u].id, menu_id=menus[9].id, menu_name="카레", eaten_at=t_early) for u in range(4)],
        # 우동 (3명, 마지막 식사 t_late)
        UserMeal(user_id=users[0].id, menu_id=menus[8].id, menu_name="우동", eaten_at=t_early),
        UserMeal(user_id=users[1].id, menu_id=menus[8].id, menu_name="우동", eaten_at=t_early),
        UserMeal(user_id=users[2].id, menu_id=menus[8].id, menu_name="우동", eaten_at=t_late),
        # 김밥 (3명, 마지막 식사 t_early)
        *[UserMeal(user_id=users[u].id, menu_id=menus[0].id, menu_name="김밥", eaten_at=t_early) for u in range(3)],
        # 라면 (2명, 마지막 식사 t_mid)
        UserMeal(user_id=users[0].id, menu_id=menus[1].id, menu_name="라면", eaten_at=t_early),
        UserMeal(user_id=users[3].id, menu_id=menus[1].id, menu_name="라면", eaten_at=t_mid),
        # 된장찌개 (2명, 마지막 식사 t_mid)
        UserMeal(user_id=users[1].id, menu_id=menus[7].id, menu_name="된장찌개", eaten_at=t_early),
        UserMeal(user_id=users[4].id, menu_id=menus[7].id, menu_name="된장찌개", eaten_at=t_mid),
        # 1명 후보 5종
        UserMeal(user_id=users[5].id, menu_id=menus[6].id, menu_name="김치찌개", eaten_at=t_late),
        UserMeal(user_id=users[5].id, menu_id=menus[2].id, menu_name="비빔밥", eaten_at=t_mid),
        UserMeal(user_id=users[5].id, menu_id=menus[3].id, menu_name="냉면", eaten_at=t_mid),
        UserMeal(user_id=users[5].id, menu_id=menus[4].id, menu_name="칼국수", eaten_at=t_mid),
        UserMeal(user_id=users[5].id, menu_id=menus[5].id, menu_name="순두부찌개", eaten_at=t_early),
    ]
    db.add_all(meals)
    db.commit()

    result = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert result.published is True
    assert result.distinct_user_count == 6
    assert result.item_count == 8

    resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["source"] == "aggregated"
    assert data["as_of"] == "2026-11-19T15:00:00Z"
    assert len(data["items"]) == 8
    assert [item["name"] for item in data["items"]] == [
        "카레",
        "우동",
        "김밥",
        "라면",
        "된장찌개",
        "김치찌개",
        "비빔밥",
        "냉면",
    ]


def test_subsequent_quiet_day_preserves_previous_ranking_and_busy_day_replaces_it(popular_api):
    db = popular_api.db
    burger = _build_menu("T65-SEQ-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T65-SEQ-GB", "국밥_돼지국밥", "국밥")
    pasta = _build_menu("T65-SEQ-PS", "스파게티_까르보나라", "스파게티")
    db.add_all([burger, gukbap, pasta])
    db.flush()

    users = _create_users(db, 5, start_kakao_id=65501)

    # Day 1 (KST 2026-11-19): 5명이 햄버거/국밥 기록 -> 11-20 자정 발행
    day1_utc = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            UserMeal(user_id=users[0].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day1_utc),
            UserMeal(user_id=users[1].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day1_utc),
            UserMeal(user_id=users[2].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day1_utc),
            UserMeal(user_id=users[3].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day1_utc),
            UserMeal(user_id=users[4].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day1_utc),
        ]
    )
    # Day 2 (KST 2026-11-20): 2명만 파스타 기록 (< 5명) -> 11-21 자정 집계 시 기존 순위 유지
    day2_utc = datetime(2026, 11, 20, 3, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            UserMeal(user_id=users[0].id, menu_id=pasta.id, menu_name="까르보나라", eaten_at=day2_utc),
            UserMeal(user_id=users[1].id, menu_id=pasta.id, menu_name="까르보나라", eaten_at=day2_utc),
        ]
    )
    # Day 3 (KST 2026-11-21): 5명이 파스타 기록 -> 11-22 자정 집계 시 새 순위로 교체
    day3_utc = datetime(2026, 11, 21, 3, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            UserMeal(user_id=users[u].id, menu_id=pasta.id, menu_name="까르보나라", eaten_at=day3_utc)
            for u in range(5)
        ]
    )
    db.commit()

    res_day1 = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
    assert res_day1.published is True
    assert [i["name"] for i in popular_api.client.get(POPULAR_MENUS_URL).json()["data"]["items"]] == [
        "햄버거",
        "국밥",
    ]

    res_day2 = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 21))
    assert res_day2.published is False
    assert res_day2.distinct_user_count == 2
    assert res_day2.published_as_of == datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)

    state = db.query(PopularMenuRankingState).filter(PopularMenuRankingState.id == 1).one()
    assert state.last_succeeded_kst_date == date(2026, 11, 21)

    data_day2 = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data_day2["source"] == "aggregated"
    assert data_day2["as_of"] == "2026-11-19T15:00:00Z"
    assert [i["name"] for i in data_day2["items"]] == ["햄버거", "국밥"]

    res_day3 = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 22))
    assert res_day3.published is True
    data_day3 = popular_api.client.get(POPULAR_MENUS_URL).json()["data"]
    assert data_day3["source"] == "aggregated"
    assert data_day3["as_of"] == "2026-11-21T15:00:00Z"
    assert [i["name"] for i in data_day3["items"]] == ["파스타"]


def test_public_aggregated_popular_api_leaks_no_user_or_meal_details_and_serves_valid_images(
    popular_api,
):
    db = popular_api.db
    burger = _build_menu("T65-PRIV-BG", "버거_시크릿트러플버거", "버거")
    gukbap = _build_menu("T65-PRIV-GB", "국밥_비공개돼지국밥", "국밥")
    db.add_all([burger, gukbap])
    db.flush()

    burger_fk = db.query(FoodKind).filter(FoodKind.id == burger.food_kind_id).one()
    burger_fk.representative_image_url = "/static/default-profile.svg"
    gukbap_fk = db.query(FoodKind).filter(FoodKind.id == gukbap.food_kind_id).one()
    gukbap_fk.representative_image_url = "/static/menu-illustrations/missing-file.png"

    users = [
        User(
            kakao_id=65600 + i,
            nickname=f"비공개닉네임{i}",
            email=f"private{i}@example.com",
        )
        for i in range(5)
    ]
    db.add_all(users)
    db.flush()

    meal_time = datetime(2026, 11, 19, 6, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            *[
                UserMeal(user_id=users[i].id, menu_id=burger.id, menu_name="시크릿트러플버거", eaten_at=meal_time)
                for i in range(3)
            ],
            *[
                UserMeal(user_id=users[i].id, menu_id=gukbap.id, menu_name="비공개돼지국밥", eaten_at=meal_time)
                for i in range(3, 5)
            ],
        ]
    )
    db.commit()

    aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))

    # 토큰 없이 공개 조회 가능
    resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp.status_code == 200
    payload = resp.json()
    raw_json = json.dumps(payload, ensure_ascii=False)

    for forbidden in (
        "비공개닉네임",
        "private0@example.com",
        "시크릿트러플버거",
        "비공개돼지국밥",
        "user_id",
        "user_count",
        "last_eaten_at",
        "eaten_at",
        "nickname",
    ):
        assert forbidden not in raw_json

    data = payload["data"]
    assert set(data.keys()) == {"source", "as_of", "items"}
    assert data["source"] == "aggregated"
    assert data["as_of"] == "2026-11-19T15:00:00Z"
    assert len(data["items"]) == 2
    assert set(data["items"][0].keys()) == {"food_kind_id", "name", "image_url"}
    assert data["items"][0]["image_url"] == "/static/default-profile.svg"
    assert data["items"][1]["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL
    for item in data["items"]:
        assert popular_api.client.get(item["image_url"]).status_code == 200


def test_popular_ranking_tables_enforce_constraints_and_atomic_rollback_on_mid_write_failure(
    sqlite_engine,
):
    with Session(sqlite_engine) as db:
        fk1 = FoodKind(kind_key="c_burger", name="햄버거")
        fk2 = FoodKind(kind_key="c_gukbap", name="국밥")
        db.add_all([fk1, fk2])
        db.commit()
        fk1_id = fk1.id

        # 1) 상태 테이블은 id=1 단일 행만 허용한다.
        db.add(PopularMenuRankingState(id=2))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        now_utc = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
        db.add(
            PopularMenuRankingItem(
                rank_position=1,
                food_kind_id=fk1.id,
                user_count=3,
                last_eaten_at=now_utc,
            )
        )
        db.commit()

        # 2) rank_position 범위 밖 (0, 9) 거부
        for bad_rank in (0, 9):
            db.add(
                PopularMenuRankingItem(
                    rank_position=bad_rank,
                    food_kind_id=fk2.id,
                    user_count=1,
                    last_eaten_at=now_utc,
                )
            )
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

        # 3) 중복 food_kind_id 거부
        db.add(
            PopularMenuRankingItem(
                rank_position=2,
                food_kind_id=fk1.id,
                user_count=2,
                last_eaten_at=now_utc,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 4) 존재하지 않는 food_kind_id FK 거부
        db.add(
            PopularMenuRankingItem(
                rank_position=2,
                food_kind_id=999999,
                user_count=2,
                last_eaten_at=now_utc,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 5) 집계 교체 도중 INSERT 실패 시 이전 순위와 상태가 그대로 보존된다.
        db.query(PopularMenuRankingItem).delete()
        db.commit()

        m1 = Menu(
            food_code="T65-RB-1",
            name="햄버거_치즈",
            base_name="햄버거",
            food_kind_id=fk1.id,
            category_large="기타",
        )
        m2 = Menu(
            food_code="T65-RB-2",
            name="국밥_돼지",
            base_name="국밥",
            food_kind_id=fk2.id,
            category_large="기타",
        )
        db.add_all([m1, m2])
        users = _create_users(db, 5, start_kakao_id=65701)
        day1 = datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc)
        day2 = datetime(2026, 11, 20, 3, 0, 0, tzinfo=timezone.utc)
        db.add_all(
            [
                *[UserMeal(user_id=u.id, menu_id=m1.id, menu_name="치즈버거", eaten_at=day1) for u in users],
                *[UserMeal(user_id=u.id, menu_id=m2.id, menu_name="돼지국밥", eaten_at=day2) for u in users],
            ]
        )
        db.commit()

        aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))

        def _abort_on_ranking_insert(session, _flush_context, _instances):
            if any(isinstance(obj, PopularMenuRankingItem) for obj in session.new):
                raise RuntimeError("simulated ranking write failure")

        event.listen(db, "before_flush", _abort_on_ranking_insert)
        try:
            with pytest.raises(RuntimeError, match="simulated ranking write failure"):
                aggregate_popular_menus_for_kst_date(db, date(2026, 11, 21))
        finally:
            event.remove(db, "before_flush", _abort_on_ranking_insert)

        db.commit()

    # 새 DB 세션에서도 이전(11-20 자정) 순위와 as_of가 온전히 유지되어야 한다.
    with Session(sqlite_engine) as verify_db:
        state = verify_db.query(PopularMenuRankingState).filter(PopularMenuRankingState.id == 1).one()
        items = (
            verify_db.query(PopularMenuRankingItem)
            .order_by(PopularMenuRankingItem.rank_position.asc())
            .all()
        )
        assert state.last_succeeded_kst_date == date(2026, 11, 20)
        assert state.published_as_of == datetime(2026, 11, 19, 15, 0, 0)
        assert [(i.rank_position, i.food_kind_id, i.user_count) for i in items] == [(1, fk1_id, 5)]


def test_scheduler_loop_waits_until_asia_seoul_midnight_aggregates_previous_day_and_shuts_down_cleanly(
    popular_api,
):
    db = popular_api.db
    burger = _build_menu("T65-SCH-BG", "버거_치즈버거", "버거")
    gukbap = _build_menu("T65-SCH-GB", "국밥_돼지국밥", "국밥")
    db.add_all([burger, gukbap])
    db.flush()

    users = _create_users(db, 5, start_kakao_id=65801)
    # KST 2026-11-19 하루 안의 식사 기록 (햄버거 3명, 국밥 2명)
    day_utc = datetime(2026, 11, 19, 6, 0, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            UserMeal(user_id=users[0].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day_utc),
            UserMeal(user_id=users[1].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day_utc),
            UserMeal(user_id=users[2].id, menu_id=burger.id, menu_name="치즈버거", eaten_at=day_utc),
            UserMeal(user_id=users[3].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day_utc),
            UserMeal(user_id=users[4].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=day_utc),
        ]
    )
    db.commit()

    # 제어 시계: KST 2026-11-19 23:55:00 (= UTC 2026-11-19 14:55:00), 자정까지 정확히 300초 남음
    current_utc = [datetime(2026, 11, 19, 14, 55, 0, tzinfo=timezone.utc)]
    recorded_delays: list[float] = []

    async def _run_scheduler_scenario():
        first_run_completed = asyncio.Event()

        async def fake_sleep(delay: float) -> None:
            recorded_delays.append(delay)
            if len(recorded_delays) == 1:
                current_utc[0] = current_utc[0] + timedelta(seconds=delay)
                return
            first_run_completed.set()
            await asyncio.Future()

        task = asyncio.create_task(
            popular_ranking_scheduler_loop(
                now_fn=lambda: current_utc[0],
                sleep_fn=fake_sleep,
                session_factory=lambda: Session(popular_api.engine),
            )
        )
        await asyncio.wait_for(first_run_completed.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_scheduler_scenario())

    assert recorded_delays == [300.0, 86400.0]

    # 별도 세션(worker thread)에서 커밋된 결과를 공개 API가 즉시 읽는다.
    db.expire_all()
    resp = popular_api.client.get(POPULAR_MENUS_URL)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["source"] == "aggregated"
    assert data["as_of"] == "2026-11-19T15:00:00Z"
    assert [item["name"] for item in data["items"]] == ["햄버거", "국밥"]


def test_scheduler_loop_survives_transient_aggregation_error_and_schedules_next_midnight(
    sqlite_engine,
):
    current_utc = [datetime(2026, 11, 19, 14, 59, 0, tzinfo=timezone.utc)]
    invoked_dates: list[date] = []
    recorded_delays: list[float] = []

    def flaky_aggregate(db: Session, target_kst_date: date):
        invoked_dates.append(target_kst_date)
        if len(invoked_dates) == 1:
            raise RuntimeError("transient db error")
        return aggregate_popular_menus_for_kst_date(db, target_kst_date)

    async def _run():
        second_run_completed = asyncio.Event()

        async def fake_sleep(delay: float) -> None:
            recorded_delays.append(delay)
            if len(recorded_delays) <= 2:
                current_utc[0] = current_utc[0] + timedelta(seconds=delay)
                return
            second_run_completed.set()
            await asyncio.Future()

        task = asyncio.create_task(
            popular_ranking_scheduler_loop(
                now_fn=lambda: current_utc[0],
                sleep_fn=fake_sleep,
                session_factory=lambda: Session(sqlite_engine),
                aggregate_fn=flaky_aggregate,
            )
        )
        await asyncio.wait_for(second_run_completed.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    assert recorded_delays == [60.0, 86400.0, 86400.0]
    assert invoked_dates == [date(2026, 11, 20), date(2026, 11, 21)]


def test_fastapi_lifespan_starts_and_cancels_popular_ranking_scheduler_task(monkeypatch):
    events: list[str] = []

    async def fake_popular_loop():
        events.append("popular_started")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append("popular_cancelled")
            raise

    async def fake_room_loop():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(app_main, "popular_ranking_scheduler_loop", fake_popular_loop)
    monkeypatch.setattr(app_main, "room_cleanup_loop", fake_room_loop)

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "popular_started" in events

    assert events == ["popular_started", "popular_cancelled"]


@pytest.fixture
def mysql_engine():
    raw_url = os.environ.get("MYSQL_TEST_DATABASE_URL", "").strip()
    if not raw_url:
        pytest.skip("Set MYSQL_TEST_DATABASE_URL to run MySQL Ticket #65 integration tests.")

    url = make_url(raw_url)
    assert url.drivername.startswith("mysql"), "MYSQL_TEST_DATABASE_URL must use a MySQL driver."
    assert url.database and "test" in url.database.lower(), (
        "MYSQL_TEST_DATABASE_URL must point to an isolated test database."
    )

    schema = f"whatbob_t65_{uuid4().hex[:12]}"
    admin = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE `{schema}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    engine = create_engine(url.set(database=schema), pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE `{schema}`"))
        admin.dispose()


def test_mysql_midnight_popular_aggregation_constraints_and_public_api(mysql_engine):
    Base.metadata.create_all(mysql_engine)

    # 1) MySQL 8.4 CHECK / UNIQUE / FK 제약 확인
    with Session(mysql_engine) as db:
        fk_a = FoodKind(kind_key="mysql_kind_a", name="테스트A")
        fk_b = FoodKind(kind_key="mysql_kind_b", name="테스트B")
        db.add_all([fk_a, fk_b])
        db.commit()

        db.add(PopularMenuRankingState(id=2))
        with pytest.raises((IntegrityError, OperationalError)) as exc_state:
            db.commit()
        assert "ck_popular_menu_ranking_state_singleton" in str(exc_state.value)
        db.rollback()

        now_utc = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)
        db.add(
            PopularMenuRankingItem(
                rank_position=9,
                food_kind_id=fk_a.id,
                user_count=1,
                last_eaten_at=now_utc,
            )
        )
        with pytest.raises((IntegrityError, OperationalError)) as exc_rank:
            db.commit()
        assert "ck_popular_menu_ranking_items_rank_range" in str(exc_rank.value)
        db.rollback()

        db.add(
            PopularMenuRankingItem(
                rank_position=1,
                food_kind_id=999999,
                user_count=1,
                last_eaten_at=now_utc,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 2) 별칭 합산, 자정 경계, 삭제 메뉴 제외, 동률 정렬, 8종 상한을 MySQL에서 검증한다.
        burger_1 = _build_menu("MY65-BG1", "버거_치즈버거", "버거")
        burger_2 = _build_menu("MY65-BG2", "햄버거_불고기버거", "햄버거")
        gukbap = _build_menu("MY65-GB1", "국밥_돼지국밥", "국밥")
        deleted_menu = _build_menu("MY65-DEL", "마라탕_삭제메뉴", "마라탕")
        extra_menus = [
            _build_menu(f"MY65-EX{i}", f"별도메뉴{i}_기본", f"별도메뉴{i}")
            for i in range(1, 8)
        ]
        db.add_all([burger_1, burger_2, gukbap, deleted_menu, *extra_menus])
        db.flush()

        users = _create_users(db, 6, start_kakao_id=65901)
        exact_start_utc = datetime(2026, 11, 18, 15, 0, 0, tzinfo=timezone.utc)   # KST 11-19 00:00:00 (포함)
        mid_utc = datetime(2026, 11, 19, 4, 0, 0, tzinfo=timezone.utc)            # KST 11-19 13:00:00 (포함)
        late_utc = datetime(2026, 11, 19, 14, 59, 59, tzinfo=timezone.utc)        # KST 11-19 23:59:59 (포함)
        exact_end_utc = datetime(2026, 11, 19, 15, 0, 0, tzinfo=timezone.utc)     # KST 11-20 00:00:00 (제외)

        db.add_all(
            [
                # 햄버거(버거+햄버거 별칭 합산): users[0](2회), users[1], users[2] => 3명 (last=mid_utc)
                UserMeal(user_id=users[0].id, menu_id=burger_1.id, menu_name="치즈버거", eaten_at=exact_start_utc),
                UserMeal(user_id=users[0].id, menu_id=burger_2.id, menu_name="불고기버거", eaten_at=mid_utc),
                UserMeal(user_id=users[1].id, menu_id=burger_1.id, menu_name="치즈버거", eaten_at=exact_start_utc),
                UserMeal(user_id=users[2].id, menu_id=burger_2.id, menu_name="불고기버거", eaten_at=mid_utc),
                # 국밥: users[2], users[3], users[4] => 3명 (last=late_utc, 햄버거와 3명 동률이나 더 최근 -> 1위)
                UserMeal(user_id=users[2].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=exact_start_utc),
                UserMeal(user_id=users[3].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=mid_utc),
                UserMeal(user_id=users[4].id, menu_id=gukbap.id, menu_name="돼지국밥", eaten_at=late_utc),
                # 삭제될 메뉴(users[5]의 유일한 범위 내 기록) -> 삭제 후 제외
                UserMeal(user_id=users[5].id, menu_id=deleted_menu.id, menu_name="삭제메뉴", eaten_at=late_utc),
                # 범위 밖(exact_end_utc) -> 제외
                UserMeal(user_id=users[5].id, menu_id=burger_1.id, menu_name="치즈버거", eaten_at=exact_end_utc),
                # 추가 7개 종류(각 1명) -> 총 9종 중 상위 8종만 저장되어야 함
                *[
                    UserMeal(
                        user_id=users[0].id,
                        menu_id=m.id,
                        menu_name=m.base_name,
                        eaten_at=exact_start_utc + timedelta(minutes=idx),
                    )
                    for idx, m in enumerate(extra_menus, start=1)
                ],
            ]
        )
        db.commit()

        db.delete(deleted_menu)
        db.commit()

        result = aggregate_popular_menus_for_kst_date(db, date(2026, 11, 20))
        assert result.published is True
        assert result.distinct_user_count == 5
        assert result.item_count == 8

    # 3) 새 MySQL 세션을 통해 공개 API가 저장된 실제 순위를 반환하는지 확인
    overrides = dict(app.dependency_overrides)
    try:
        with Session(mysql_engine) as read_db:
            app.dependency_overrides[get_db] = lambda: read_db
            client = TestClient(app)
            resp = client.get(POPULAR_MENUS_URL)
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["source"] == "aggregated"
            assert data["as_of"] == "2026-11-19T15:00:00Z"
            assert len(data["items"]) == 8
            assert data["items"][0]["name"] == "국밥"
            assert data["items"][1]["name"] == "햄버거"
            assert all(item["name"] != "마라탕" for item in data["items"])

            # 다음 날(11-21 자정) 유효 사용자 1명(exact_end_utc에 기록한 users[5]) < 5명 -> 이전 순위 유지
            quiet_res = aggregate_popular_menus_for_kst_date(read_db, date(2026, 11, 21))
            assert quiet_res.published is False
            assert quiet_res.distinct_user_count == 1

            resp_after_quiet = client.get(POPULAR_MENUS_URL)
            assert resp_after_quiet.status_code == 200
            data_quiet = resp_after_quiet.json()["data"]
            assert data_quiet["source"] == "aggregated"
            assert data_quiet["as_of"] == "2026-11-19T15:00:00Z"
            assert [i["name"] for i in data_quiet["items"]] == [i["name"] for i in data["items"]]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)








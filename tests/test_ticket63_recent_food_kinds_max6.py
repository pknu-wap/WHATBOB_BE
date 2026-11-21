from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.sessions import create_uuid_user
from app.core.database import Base, get_db
from app.main import app
from app.menus.food_kinds import DEFAULT_MENU_ILLUSTRATION_URL
from app.menus.models import Menu, UserMeal
from app.users.models import User
from app.users.router import get_utc_now
from tests.auth_helpers import session_token

RECENT_HOME_CARD_URL = "/api/users/me/home-card-recently-eaten-food-max-6"
MEALS_URL = "/api/users/me/meals"


def _build_menu(food_code: str, name: str, base_name: str, category_large: str = "기타") -> Menu:
    return Menu(
        food_code=food_code,
        name=name,
        base_name=base_name,
        category_large=category_large,
        category_medium=base_name,
    )


@pytest.fixture
def recent_foods_api():
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
    overrides = app.dependency_overrides.copy()
    try:
        with Session(engine) as db:
            kakao_user = User(kakao_id=96301, nickname="최근여섯카드유저")
            uuid_user = create_uuid_user(db)
            other_user = User(kakao_id=96302, nickname="타계정유저63")

            menus = {
                "cheese_burger": _build_menu("T63-BG-01", "버거_치즈버거", "버거", "빵 및 과자류"),
                "bulgogi_burger": _build_menu("T63-BG-02", "햄버거_불고기버거", "햄버거", "빵 및 과자류"),
                "tteokbokki": _build_menu("T63-TB-01", "떡볶이_로제떡볶이", "떡볶이", "볶음류"),
                "tonkatsu_1": _build_menu("T63-TK-01", "돈가스_등심돈가스", "돈가스", "튀김류"),
                "tonkatsu_2": _build_menu("T63-TK-02", "돈까스_치즈돈까스", "돈까스", "튀김류"),
                "malatang": _build_menu("T63-ML-01", "마라탕_소고기마라탕", "마라탕", "국 및 탕류"),
                "pasta": _build_menu("T63-PS-01", "스파게티_토마토파스타", "스파게티", "면류"),
                "gukbap": _build_menu("T63-GB-01", "국밥_돼지국밥", "국밥", "국 및 탕류"),
                "jjajang_1": _build_menu("T63-JJ-01", "자장면_삼선자장면", "자장면", "면류"),
                "jjajang_2": _build_menu("T63-JJ-02", "짜장면_간짜장", "짜장면", "면류"),
                "jeyuk": _build_menu("T63-JY-01", "제육볶음_매콤제육", "제육볶음", "볶음류"),
                "bibimbap": _build_menu("T63-BB-01", "비빔밥_돌솥비빔밥", "비빔밥", "밥류"),
            }
            db.add_all([kakao_user, other_user, *menus.values()])
            db.commit()

            app.dependency_overrides[get_db] = lambda: db
            yield SimpleNamespace(
                client=TestClient(app),
                db=db,
                kakao_user=kakao_user,
                uuid_user=uuid_user,
                other_user=other_user,
                menus=menus,
                kakao_headers={"Authorization": f"Bearer {session_token(db, kakao_user)}"},
                uuid_headers={"Authorization": f"Bearer {session_token(db, uuid_user)}"},
                other_headers={"Authorization": f"Bearer {session_token(db, other_user)}"},
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        engine.dispose()


def _record_meal(client: TestClient, headers: dict[str, str], menu_id: int, eaten_at: datetime) -> dict:
    resp = client.post(
        MEALS_URL,
        headers=headers,
        json={"menu_id": menu_id, "eaten_at": eaten_at.isoformat()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _parse_utc(iso_text: str) -> datetime:
    assert iso_text.endswith("Z") or iso_text.endswith("+00:00")
    return datetime.fromisoformat(iso_text.replace("Z", "+00:00"))


def test_recent_home_cards_returns_latest_six_distinct_food_kinds_when_more_than_six_exist(
    recent_foods_api,
):
    base_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=12)
    ordered_keys = [
        "bibimbap",
        "jeyuk",
        "jjajang_1",
        "gukbap",
        "pasta",
        "malatang",
        "tonkatsu_1",
        "tteokbokki",
    ]
    recorded = {}
    for idx, key in enumerate(ordered_keys):
        eaten_at = base_time + timedelta(hours=idx)
        recorded[key] = (
            _record_meal(
                recent_foods_api.client,
                recent_foods_api.kakao_headers,
                recent_foods_api.menus[key].id,
                eaten_at,
            ),
            eaten_at,
        )

    resp = recent_foods_api.client.get(RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers)
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    payload = resp.json()
    assert payload["success"] is True
    assert payload["message"] is None

    items = payload["data"]["items"]
    expected_keys = ["tteokbokki", "tonkatsu_1", "malatang", "pasta", "gukbap", "jjajang_1"]
    assert len(items) == 6
    assert [item["food_kind_id"] for item in items] == [
        recent_foods_api.menus[k].food_kind_id for k in expected_keys
    ]
    assert [item["menu_name"] for item in items] == [
        recorded[k][0]["menu_name"] for k in expected_keys
    ]
    assert [_parse_utc(item["eaten_at"]) for item in items] == [
        recorded[k][1] for k in expected_keys
    ]
    assert all(item["image_url"] == DEFAULT_MENU_ILLUSTRATION_URL for item in items)


def test_recent_home_cards_returns_empty_and_fewer_than_six_exact_counts(recent_foods_api):
    # 식사 기록이 없을 때는 빈 배열
    empty_resp = recent_foods_api.client.get(
        RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers
    )
    assert empty_resp.status_code == 200
    assert empty_resp.headers.get("cache-control") == "no-store"
    assert empty_resp.json() == {
        "success": True,
        "data": {"items": []},
        "message": None,
    }

    # 유효한 음식 종류가 3개뿐이면 정확히 3개만 최신순 반환
    base_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=5)
    t1 = base_time
    t2 = base_time + timedelta(hours=1)
    t3 = base_time + timedelta(hours=2)

    m1 = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["gukbap"].id,
        t1,
    )
    m2 = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["pasta"].id,
        t2,
    )
    m3 = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["malatang"].id,
        t3,
    )

    resp = recent_foods_api.client.get(RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers)
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    items = resp.json()["data"]["items"]
    assert len(items) == 3
    assert [item["food_kind_id"] for item in items] == [
        recent_foods_api.menus["malatang"].food_kind_id,
        recent_foods_api.menus["pasta"].food_kind_id,
        recent_foods_api.menus["gukbap"].food_kind_id,
    ]
    assert [item["menu_name"] for item in items] == [
        m3["menu_name"],
        m2["menu_name"],
        m1["menu_name"],
    ]


def test_recent_home_cards_deduplicates_variants_of_same_food_kind_keeping_latest_meal(
    recent_foods_api,
):
    cheese_burger = recent_foods_api.menus["cheese_burger"]
    bulgogi_burger = recent_foods_api.menus["bulgogi_burger"]
    tonkatsu_1 = recent_foods_api.menus["tonkatsu_1"]
    tonkatsu_2 = recent_foods_api.menus["tonkatsu_2"]
    jjajang_1 = recent_foods_api.menus["jjajang_1"]
    jjajang_2 = recent_foods_api.menus["jjajang_2"]
    gukbap = recent_foods_api.menus["gukbap"]

    # 별칭으로 묶인 세부 메뉴들이 동일한 음식 종류 ID를 공유한다.
    assert cheese_burger.food_kind_id == bulgogi_burger.food_kind_id
    assert tonkatsu_1.food_kind_id == tonkatsu_2.food_kind_id
    assert jjajang_1.food_kind_id == jjajang_2.food_kind_id

    base_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=10)
    t_cheese = base_time
    t_jj1 = base_time + timedelta(hours=1)
    t_tk1 = base_time + timedelta(hours=2)
    t_gukbap = base_time + timedelta(hours=3)
    t_bulgogi = base_time + timedelta(hours=4)
    t_jj2 = base_time + timedelta(hours=5)
    t_tk2 = base_time + timedelta(hours=6)

    _record_meal(recent_foods_api.client, recent_foods_api.kakao_headers, cheese_burger.id, t_cheese)
    _record_meal(recent_foods_api.client, recent_foods_api.kakao_headers, jjajang_1.id, t_jj1)
    _record_meal(recent_foods_api.client, recent_foods_api.kakao_headers, tonkatsu_1.id, t_tk1)
    m_gukbap = _record_meal(
        recent_foods_api.client, recent_foods_api.kakao_headers, gukbap.id, t_gukbap
    )
    m_bulgogi = _record_meal(
        recent_foods_api.client, recent_foods_api.kakao_headers, bulgogi_burger.id, t_bulgogi
    )
    m_jj2 = _record_meal(
        recent_foods_api.client, recent_foods_api.kakao_headers, jjajang_2.id, t_jj2
    )
    m_tk2 = _record_meal(
        recent_foods_api.client, recent_foods_api.kakao_headers, tonkatsu_2.id, t_tk2
    )

    resp = recent_foods_api.client.get(RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers)
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 4
    assert [item["food_kind_id"] for item in items] == [
        tonkatsu_2.food_kind_id,
        jjajang_2.food_kind_id,
        bulgogi_burger.food_kind_id,
        gukbap.food_kind_id,
    ]
    assert [item["menu_name"] for item in items] == [
        m_tk2["menu_name"],
        m_jj2["menu_name"],
        m_bulgogi["menu_name"],
        m_gukbap["menu_name"],
    ]
    assert [_parse_utc(item["eaten_at"]) for item in items] == [
        t_tk2,
        t_jj2,
        t_bulgogi,
        t_gukbap,
    ]

    # 개인 식사 기록 조회(/api/users/me/meals)에는 7건 모두 개별 기록으로 남는다.
    meals_resp = recent_foods_api.client.get(MEALS_URL, headers=recent_foods_api.kakao_headers)
    assert meals_resp.status_code == 200
    assert meals_resp.json()["data"]["total_count"] == 7


def test_recent_home_cards_scans_past_100_consecutive_same_kind_meals_across_chunks(
    recent_foods_api,
):
    db = recent_foods_api.db
    user = recent_foods_api.kakao_user
    base_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)

    # 1) 오래된 서로 다른 음식 종류 6개 (최신 5개가 버거 뒤를 채우고, 가장 오래된 bibimbap은 탈락해야 함)
    older_keys = ["bibimbap", "jeyuk", "jjajang_1", "gukbap", "pasta", "tteokbokki"]
    older_times = {}
    for idx, key in enumerate(older_keys):
        t = base_time + timedelta(minutes=idx)
        older_times[key] = t
        menu = recent_foods_api.menus[key]
        db.add(
            UserMeal(
                user_id=user.id,
                menu_id=menu.id,
                menu_name=f"옛기록_{key}",
                eaten_at=t,
                created_at=t,
            )
        )

    # 2) 그 뒤로 100건 연속 햄버거(버거/햄버거 번갈아 기록) — 청크 경계를 여러 번 넘는다.
    burger_start = base_time + timedelta(hours=2)
    latest_burger_time = None
    for i in range(100):
        menu = (
            recent_foods_api.menus["cheese_burger"]
            if i % 2 == 0
            else recent_foods_api.menus["bulgogi_burger"]
        )
        t = burger_start + timedelta(minutes=i)
        latest_burger_time = t
        db.add(
            UserMeal(
                user_id=user.id,
                menu_id=menu.id,
                menu_name=f"버거기록_{i}",
                eaten_at=t,
                created_at=t,
            )
        )
    db.commit()

    resp = recent_foods_api.client.get(RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers)
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 6
    assert [item["food_kind_id"] for item in items] == [
        recent_foods_api.menus["bulgogi_burger"].food_kind_id,
        recent_foods_api.menus["tteokbokki"].food_kind_id,
        recent_foods_api.menus["pasta"].food_kind_id,
        recent_foods_api.menus["gukbap"].food_kind_id,
        recent_foods_api.menus["jjajang_1"].food_kind_id,
        recent_foods_api.menus["jeyuk"].food_kind_id,
    ]
    assert items[0]["menu_name"] == "버거기록_99"
    assert _parse_utc(items[0]["eaten_at"]) == latest_burger_time
    assert [item["menu_name"] for item in items[1:]] == [
        "옛기록_tteokbokki",
        "옛기록_pasta",
        "옛기록_gukbap",
        "옛기록_jjajang_1",
        "옛기록_jeyuk",
    ]


def test_recent_home_cards_handles_same_timestamp_meals_crossing_chunk_boundaries_and_backdated_higher_ids(
    recent_foods_api,
):
    db = recent_foods_api.db
    user = recent_foods_api.kakao_user
    same_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    earlier_ts = same_ts - timedelta(minutes=5)

    # 같은 시각(same_ts)에 서로 다른 4종류를 먼저 넣고(낮은 ID), 그 위로 같은 시각 버거 40건을 쌓는다(높은 ID).
    # chunk_size(32)를 넘는 44건이 모두 동일한 same_ts를 가지므로 복합 커서 (eaten_at == T AND id < last_id)가 필수다.
    same_ts_other_keys = ["jeyuk", "jjajang_1", "gukbap", "pasta"]
    for key in same_ts_other_keys:
        menu = recent_foods_api.menus[key]
        db.add(
            UserMeal(
                user_id=user.id,
                menu_id=menu.id,
                menu_name=f"동시각_{key}",
                eaten_at=same_ts,
                created_at=same_ts,
            )
        )
        db.flush()

    for i in range(40):
        menu = (
            recent_foods_api.menus["cheese_burger"]
            if i % 2 == 0
            else recent_foods_api.menus["bulgogi_burger"]
        )
        db.add(
            UserMeal(
                user_id=user.id,
                menu_id=menu.id,
                menu_name=f"동시각버거_{i}",
                eaten_at=same_ts,
                created_at=same_ts,
            )
        )
        db.flush()

    # 나중에 기록해 ID가 더 크지만 식사 시각은 더 이른(earlier_ts) 기록을 추가한다.
    # 복합 커서가 (eaten_at < last_eaten_at) 조건을 올바르게 평가해야 이 행을 놓치지 않는다.
    db.add(
        UserMeal(
            user_id=user.id,
            menu_id=recent_foods_api.menus["malatang"].id,
            menu_name="뒤늦게입력한과거마라탕",
            eaten_at=earlier_ts,
            created_at=same_ts,
        )
    )
    db.commit()

    resp = recent_foods_api.client.get(RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers)
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 6
    assert [item["food_kind_id"] for item in items] == [
        recent_foods_api.menus["bulgogi_burger"].food_kind_id,
        recent_foods_api.menus["pasta"].food_kind_id,
        recent_foods_api.menus["gukbap"].food_kind_id,
        recent_foods_api.menus["jjajang_1"].food_kind_id,
        recent_foods_api.menus["jeyuk"].food_kind_id,
        recent_foods_api.menus["malatang"].food_kind_id,
    ]
    assert [item["menu_name"] for item in items] == [
        "동시각버거_39",
        "동시각_pasta",
        "동시각_gukbap",
        "동시각_jjajang_1",
        "동시각_jeyuk",
        "뒤늦게입력한과거마라탕",
    ]


def test_recent_home_cards_breaks_same_timestamp_ties_by_meal_id_desc_within_and_across_kinds(
    recent_foods_api,
):
    same_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=30)

    # 같은 시각에: 치즈버거(id1) -> 국밥(id2) -> 불고기버거(id3, 같은 버거 종류) -> 파스타(id4)
    # 예상 결과: 파스타(id4) -> 불고기버거(id3, 치즈버거 id1을 대체) -> 국밥(id2)
    m_cheese = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["cheese_burger"].id,
        same_ts,
    )
    m_gukbap = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["gukbap"].id,
        same_ts,
    )
    m_bulgogi = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["bulgogi_burger"].id,
        same_ts,
    )
    m_pasta = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["pasta"].id,
        same_ts,
    )
    assert m_cheese["id"] < m_gukbap["id"] < m_bulgogi["id"] < m_pasta["id"]

    resp = recent_foods_api.client.get(RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers)
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 3
    assert [item["food_kind_id"] for item in items] == [
        recent_foods_api.menus["pasta"].food_kind_id,
        recent_foods_api.menus["bulgogi_burger"].food_kind_id,
        recent_foods_api.menus["gukbap"].food_kind_id,
    ]
    assert [item["menu_name"] for item in items] == [
        m_pasta["menu_name"],
        m_bulgogi["menu_name"],
        m_gukbap["menu_name"],
    ]


def test_recent_home_cards_future_meal_does_not_mask_past_meal_and_appears_at_exact_boundary(
    recent_foods_api,
):
    t_past_burger = datetime(2026, 11, 20, 9, 0, 0, tzinfo=timezone.utc)
    t_gukbap = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
    t_future_burger = datetime(2026, 11, 20, 14, 0, 0, tzinfo=timezone.utc)

    m_past_burger = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["cheese_burger"].id,
        t_past_burger,
    )
    m_gukbap = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["gukbap"].id,
        t_gukbap,
    )
    m_future_burger = _record_meal(
        recent_foods_api.client,
        recent_foods_api.kakao_headers,
        recent_foods_api.menus["bulgogi_burger"].id,
        t_future_burger,
    )

    # 미래 식사 시각 1초 전(KST +09:00 오프셋으로도 동일 UTC 비교 확인):
    # 미래 불고기버거가 같은 종류의 과거 치즈버거를 가리지 않아야 한다.
    kst = timezone(timedelta(hours=9))
    before_future_kst = (t_future_burger - timedelta(seconds=1)).astimezone(kst)
    app.dependency_overrides[get_utc_now] = lambda: before_future_kst

    before_resp = recent_foods_api.client.get(
        RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers
    )
    assert before_resp.status_code == 200
    before_items = before_resp.json()["data"]["items"]
    assert [item["food_kind_id"] for item in before_items] == [
        recent_foods_api.menus["gukbap"].food_kind_id,
        recent_foods_api.menus["cheese_burger"].food_kind_id,
    ]
    assert [item["menu_name"] for item in before_items] == [
        m_gukbap["menu_name"],
        m_past_burger["menu_name"],
    ]
    assert [_parse_utc(item["eaten_at"]) for item in before_items] == [
        t_gukbap,
        t_past_burger,
    ]

    # 정확히 미래 식사 시각에 도달하면 그 기록이 즉시 표시 대상이 되어 버거 카드를 갱신한다.
    at_future_kst = t_future_burger.astimezone(kst)
    app.dependency_overrides[get_utc_now] = lambda: at_future_kst

    at_resp = recent_foods_api.client.get(
        RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers
    )
    assert at_resp.status_code == 200
    at_items = at_resp.json()["data"]["items"]
    assert [item["food_kind_id"] for item in at_items] == [
        recent_foods_api.menus["bulgogi_burger"].food_kind_id,
        recent_foods_api.menus["gukbap"].food_kind_id,
    ]
    assert [item["menu_name"] for item in at_items] == [
        m_future_burger["menu_name"],
        m_gukbap["menu_name"],
    ]
    assert [_parse_utc(item["eaten_at"]) for item in at_items] == [
        t_future_burger,
        t_gukbap,
    ]


def test_recent_home_cards_excludes_deleted_menus_without_masking_and_keeps_names_in_my_meals(
    recent_foods_api,
):
    base_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=12)

    # 오래된 유효 5종 + 과거 치즈버거 + 최신 국밥 + 삭제될 불고기버거(같은 버거 종류) + 삭제될 떡볶이(단독 종류)
    seq = [
        ("bibimbap", 0),
        ("jeyuk", 1),
        ("jjajang_1", 2),
        ("pasta", 3),
        ("malatang", 4),
        ("cheese_burger", 5),
        ("gukbap", 6),
        ("bulgogi_burger", 7),
        ("tteokbokki", 8),
    ]
    recorded = {}
    for key, offset_hours in seq:
        eaten_at = base_time + timedelta(hours=offset_hours)
        recorded[key] = _record_meal(
            recent_foods_api.client,
            recent_foods_api.kakao_headers,
            recent_foods_api.menus[key].id,
            eaten_at,
        )

    # 카탈로그에서 불고기버거와 떡볶이 삭제 (ON DELETE SET NULL -> user_meals.menu_id = NULL, menu_name 보존)
    recent_foods_api.db.delete(recent_foods_api.menus["bulgogi_burger"])
    recent_foods_api.db.delete(recent_foods_api.menus["tteokbokki"])
    recent_foods_api.db.commit()
    recent_foods_api.db.expire_all()

    # 1) 최근 메뉴 홈 카드에서는 삭제된 두 메뉴가 제외되며, 삭제된 불고기버거가 과거 치즈버거를 가리지 않고 6칸을 모두 채운다.
    home_resp = recent_foods_api.client.get(
        RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers
    )
    assert home_resp.status_code == 200
    assert home_resp.headers.get("cache-control") == "no-store"
    home_items = home_resp.json()["data"]["items"]
    expected_home_keys = ["gukbap", "cheese_burger", "malatang", "pasta", "jjajang_1", "jeyuk"]
    assert len(home_items) == 6
    assert [item["food_kind_id"] for item in home_items] == [
        recent_foods_api.menus[k].food_kind_id for k in expected_home_keys
    ]
    assert [item["menu_name"] for item in home_items] == [
        recorded[k]["menu_name"] for k in expected_home_keys
    ]

    # 2) 개인 식사 기록 조회(/api/users/me/meals)에서는 삭제된 메뉴의 저장된 menu_name이 계속 조회된다.
    meals_resp = recent_foods_api.client.get(MEALS_URL, headers=recent_foods_api.kakao_headers)
    assert meals_resp.status_code == 200
    meals_data = meals_resp.json()["data"]
    assert meals_data["total_count"] == 9
    meal_items_by_id = {item["id"]: item for item in meals_data["items"]}

    deleted_tteok = meal_items_by_id[recorded["tteokbokki"]["id"]]
    assert deleted_tteok["menu_id"] is None
    assert deleted_tteok["menu"] is None
    assert deleted_tteok["menu_name"] == recorded["tteokbokki"]["menu_name"]

    deleted_bulgogi = meal_items_by_id[recorded["bulgogi_burger"]["id"]]
    assert deleted_bulgogi["menu_id"] is None
    assert deleted_bulgogi["menu"] is None
    assert deleted_bulgogi["menu_name"] == recorded["bulgogi_burger"]["menu_name"]


def test_recent_home_cards_isolates_kakao_uuid_and_other_accounts_with_no_store(recent_foods_api):
    base_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=5)

    # UUID 사용자: 2종류 기록
    m_uuid_1 = _record_meal(
        recent_foods_api.client,
        recent_foods_api.uuid_headers,
        recent_foods_api.menus["gukbap"].id,
        base_time,
    )
    m_uuid_2 = _record_meal(
        recent_foods_api.client,
        recent_foods_api.uuid_headers,
        recent_foods_api.menus["pasta"].id,
        base_time + timedelta(hours=1),
    )

    # 다른 사용자: 더 최신 시각에 7종류 기록
    other_keys = ["bibimbap", "jeyuk", "jjajang_1", "malatang", "tonkatsu_1", "tteokbokki", "cheese_burger"]
    for idx, key in enumerate(other_keys):
        _record_meal(
            recent_foods_api.client,
            recent_foods_api.other_headers,
            recent_foods_api.menus[key].id,
            base_time + timedelta(hours=2, minutes=idx),
        )

    uuid_resp = recent_foods_api.client.get(
        f"{RECENT_HOME_CARD_URL}?user_id={recent_foods_api.other_user.id}",
        headers=recent_foods_api.uuid_headers,
    )
    assert uuid_resp.status_code == 200
    assert uuid_resp.headers.get("cache-control") == "no-store"
    uuid_items = uuid_resp.json()["data"]["items"]
    assert [item["food_kind_id"] for item in uuid_items] == [
        recent_foods_api.menus["pasta"].food_kind_id,
        recent_foods_api.menus["gukbap"].food_kind_id,
    ]
    assert [item["menu_name"] for item in uuid_items] == [
        m_uuid_2["menu_name"],
        m_uuid_1["menu_name"],
    ]

    kakao_resp = recent_foods_api.client.get(
        RECENT_HOME_CARD_URL, headers=recent_foods_api.kakao_headers
    )
    assert kakao_resp.status_code == 200
    assert kakao_resp.headers.get("cache-control") == "no-store"
    assert kakao_resp.json()["data"]["items"] == []

    other_resp = recent_foods_api.client.get(
        RECENT_HOME_CARD_URL, headers=recent_foods_api.other_headers
    )
    assert other_resp.status_code == 200
    assert other_resp.headers.get("cache-control") == "no-store"
    other_items = other_resp.json()["data"]["items"]
    assert len(other_items) == 6
    assert [item["food_kind_id"] for item in other_items] == [
        recent_foods_api.menus[k].food_kind_id
        for k in ["cheese_burger", "tteokbokki", "tonkatsu_1", "malatang", "jjajang_1", "jeyuk"]
    ]


@pytest.fixture
def mysql_engine():
    value = os.getenv("MYSQL_TEST_DATABASE_URL")
    if not value:
        pytest.skip("MYSQL_TEST_DATABASE_URL is required for real MySQL tests")
    url = make_url(value)
    if not url.drivername.startswith("mysql") or not url.database or "test" not in url.database.lower():
        pytest.fail("MYSQL_TEST_DATABASE_URL must point to a dedicated MySQL test database")
    schema = f"{url.database[:28]}_i63_{uuid4().hex[:10]}"
    admin = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(
            text(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        )
    engine = create_engine(url.set(database=schema), pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE `{schema}`"))
        admin.dispose()


def test_mysql_recent_home_cards_chunked_cursor_future_boundary_and_deleted_menu_behavior(
    mysql_engine,
):
    Base.metadata.create_all(mysql_engine)
    overrides = app.dependency_overrides.copy()
    try:
        with Session(mysql_engine) as db:
            user = User(kakao_id=96399, nickname="MySQL최근6카드유저")
            menus = {
                "cheese_burger": _build_menu("M63-BG-01", "버거_치즈버거", "버거", "빵 및 과자류"),
                "bulgogi_burger": _build_menu("M63-BG-02", "햄버거_불고기버거", "햄버거", "빵 및 과자류"),
                "tteokbokki": _build_menu("M63-TB-01", "떡볶이_로제떡볶이", "떡볶이", "볶음류"),
                "tonkatsu": _build_menu("M63-TK-01", "돈가스_등심돈가스", "돈가스", "튀김류"),
                "malatang": _build_menu("M63-ML-01", "마라탕_소고기마라탕", "마라탕", "국 및 탕류"),
                "pasta": _build_menu("M63-PS-01", "스파게티_토마토파스타", "스파게티", "면류"),
                "gukbap": _build_menu("M63-GB-01", "국밥_돼지국밥", "국밥", "국 및 탕류"),
                "jjajang": _build_menu("M63-JJ-01", "자장면_삼선자장면", "자장면", "면류"),
            }
            db.add_all([user, *menus.values()])
            db.commit()

            headers = {"Authorization": f"Bearer {session_token(db, user)}"}
            app.dependency_overrides[get_db] = lambda: db
            client = TestClient(app)

            t_base = datetime(2026, 11, 20, 6, 0, 0, tzinfo=timezone.utc)
            t_tie = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
            t_future = datetime(2026, 11, 20, 15, 0, 0, tzinfo=timezone.utc)

            # 1) 과거 4종류(jjajang, pasta, malatang, tonkatsu)
            for idx, key in enumerate(["jjajang", "pasta", "malatang", "tonkatsu"]):
                t = t_base + timedelta(minutes=idx)
                db.add(
                    UserMeal(
                        user_id=user.id,
                        menu_id=menus[key].id,
                        menu_name=f"mysql_{key}",
                        eaten_at=t,
                        created_at=t,
                    )
                )
                db.flush()

            # 2) 동일 시각(t_tie)에 국밥 1건 + 버거 70건 연속(청크 경계 2회 이상 돌파)
            db.add(
                UserMeal(
                    user_id=user.id,
                    menu_id=menus["gukbap"].id,
                    menu_name="mysql_gukbap_tie",
                    eaten_at=t_tie,
                    created_at=t_tie,
                )
            )
            db.flush()
            for i in range(70):
                db.add(
                    UserMeal(
                        user_id=user.id,
                        menu_id=menus["cheese_burger"].id,
                        menu_name=f"mysql_burger_{i}",
                        eaten_at=t_tie,
                        created_at=t_tie,
                    )
                )
                db.flush()

            # 3) 삭제될 떡볶이(t_tie + 10분) 및 미래 시각 불고기버거(t_future)
            deleted_meal = UserMeal(
                user_id=user.id,
                menu_id=menus["tteokbokki"].id,
                menu_name="삭제될로제떡볶이",
                eaten_at=t_tie + timedelta(minutes=10),
                created_at=t_tie + timedelta(minutes=10),
            )
            future_meal = UserMeal(
                user_id=user.id,
                menu_id=menus["bulgogi_burger"].id,
                menu_name="미래불고기버거",
                eaten_at=t_future,
                created_at=t_tie + timedelta(minutes=11),
            )
            db.add_all([deleted_meal, future_meal])
            db.commit()

            # 떡볶이 카탈로그 메뉴 삭제
            db.delete(menus["tteokbokki"])
            db.commit()
            db.expire_all()

            # 미래 시각 1초 전: 미래 불고기버거·삭제 떡볶이 제외, 청크 경계 넘어 6종 반환
            app.dependency_overrides[get_utc_now] = lambda: t_future - timedelta(seconds=1)
            resp_before = client.get(RECENT_HOME_CARD_URL, headers=headers)
            assert resp_before.status_code == 200
            assert resp_before.headers.get("cache-control") == "no-store"
            before_items = resp_before.json()["data"]["items"]
            assert [item["food_kind_id"] for item in before_items] == [
                menus["cheese_burger"].food_kind_id,
                menus["gukbap"].food_kind_id,
                menus["tonkatsu"].food_kind_id,
                menus["malatang"].food_kind_id,
                menus["pasta"].food_kind_id,
                menus["jjajang"].food_kind_id,
            ]
            assert before_items[0]["menu_name"] == "mysql_burger_69"
            assert before_items[1]["menu_name"] == "mysql_gukbap_tie"

            # 미래 시각 도달: 미래 불고기버거가 버거 카드를 대체
            app.dependency_overrides[get_utc_now] = lambda: t_future
            resp_at = client.get(RECENT_HOME_CARD_URL, headers=headers)
            assert resp_at.status_code == 200
            at_items = resp_at.json()["data"]["items"]
            assert at_items[0]["food_kind_id"] == menus["bulgogi_burger"].food_kind_id
            assert at_items[0]["menu_name"] == "미래불고기버거"
            assert _parse_utc(at_items[0]["eaten_at"]) == t_future

            # 개인 식사 기록 조회에는 삭제된 떡볶이의 menu_name이 보존됨
            meals_resp = client.get(f"{MEALS_URL}?limit=200", headers=headers)
            assert meals_resp.status_code == 200
            deleted_entries = [
                m for m in meals_resp.json()["data"]["items"] if m["menu_name"] == "삭제될로제떡볶이"
            ]
            assert len(deleted_entries) == 1
            assert deleted_entries[0]["menu_id"] is None
            assert deleted_entries[0]["menu"] is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


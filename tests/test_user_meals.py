from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.auth.models import AuthSession
from tests.auth_helpers import session_token
from app.main import app
from app.menus.models import Menu, UserMeal
from app.users import repository
from app.users.models import User
from app.users.schemas import UserMealCreateRequest
from app.users.service import record_my_meal

URL = "/api/users/me/meals"


@pytest.fixture
def api():
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
            user = User(kakao_id=99001, nickname="밥먹는유저")
            other = User(kakao_id=99002, nickname="다른유저")
            menu_gukbap = Menu(
                food_code="MEAL-001",
                name="국밥_콩나물",
                base_name="국밥",
                category_large="국 및 탕류",
                category_medium="국밥",
                meal_type="main_meal",
                has_soup=True,
                cooking_style="boiled_soup",
                main_ingredient="grain",
                flavor_intensity="mild_clean",
                temperature="hot",
                heaviness="moderate",
                kcal=98.5,
                protein_g=5.2,
                fat_g=1.8,
                carb_g=15.4,
                sugar_g=0.6,
                sodium_mg=280.0,
            )
            menu_chicken = Menu(
                food_code="MEAL-002",
                name="닭튀김_땡초 치킨",
                base_name="닭튀김",
                category_large="튀김류",
                category_medium="닭튀김",
                meal_type="snack_drink",
                has_soup=False,
                cooking_style="deep_fried",
                main_ingredient="chicken",
                flavor_intensity="spicy",
                temperature="hot",
                heaviness="heavy",
                kcal=289.0,
                protein_g=18.4,
                fat_g=17.9,
                carb_g=13.5,
                sugar_g=3.1,
                sodium_mg=490.0,
            )
            menu_pasta = Menu(
                food_code="MEAL-003",
                name="파스타_토마토",
                base_name="파스타",
                category_large="면 및 만두류",
                category_medium="파스타",
                meal_type="main_meal",
                has_soup=False,
                cooking_style="noodle",
                main_ingredient="flour",
                flavor_intensity="savory",
                temperature="hot",
                heaviness="moderate",
                kcal=165.0,
                protein_g=5.8,
                fat_g=4.2,
                carb_g=26.1,
                sugar_g=4.0,
                sodium_mg=360.0,
            )
            db.add_all([user, other, menu_gukbap, menu_chicken, menu_pasta])
            db.commit()

            app.dependency_overrides[get_db] = lambda: db
            yield SimpleNamespace(
                client=TestClient(app),
                engine=engine,
                db=db,
                user=user,
                other=other,
                menus={
                    "gukbap": menu_gukbap,
                    "chicken": menu_chicken,
                    "pasta": menu_pasta,
                },
                headers={"Authorization": f'Bearer {session_token(db, user)}'},
                other_headers={"Authorization": f'Bearer {session_token(db, other)}'},
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        engine.dispose()


def test_empty_meal_history_returns_common_response_and_no_store_header(api):
    response = api.client.get(URL, headers=api.headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "success": True,
        "data": {"items": [], "total_count": 0},
        "message": None,
    }


def test_record_meal_saves_to_user_meals_and_returns_formatted_menu_details(api):
    gukbap = api.menus["gukbap"]
    response = api.client.post(
        URL,
        headers=api.headers,
        json={"menu_id": gukbap.id},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["success"] is True
    assert payload["message"] is None

    data = payload["data"]
    assert data["menu_id"] == gukbap.id
    assert data["menu_name"] == "콩나물국밥"
    assert data["eaten_at"].endswith("Z") or data["eaten_at"].endswith("+00:00")
    assert data["created_at"].endswith("Z") or data["created_at"].endswith("+00:00")
    assert data["menu"] == {
        "menu_id": gukbap.id,
        "food_code": "MEAL-001",
        "name": "콩나물국밥",
        "base_name": "국밥",
        "category_large": "국 및 탕류",
        "category_medium": "국밥",
        "meal_type": "main_meal",
        "has_soup": True,
        "cooking_style": "boiled_soup",
        "main_ingredient": "grain",
        "flavor_intensity": "mild_clean",
        "temperature": "hot",
        "heaviness": "moderate",
        "kcal": 98.5,
        "protein_g": 5.2,
        "fat_g": 1.8,
        "carb_g": 15.4,
        "sugar_g": 0.6,
        "sodium_mg": 280.0,
    }

    saved = api.db.get(UserMeal, data["id"])
    assert saved is not None
    assert saved.user_id == api.user.id
    assert saved.menu_id == gukbap.id
    assert saved.menu_name == "콩나물국밥"


def test_meals_are_ordered_by_eaten_at_desc_then_id_desc_across_timezones(api):
    gukbap = api.menus["gukbap"]
    chicken = api.menus["chicken"]
    pasta = api.menus["pasta"]

    # 1) 2026-11-05 12:00 UTC
    r1 = api.client.post(
        URL,
        headers=api.headers,
        json={"menu_id": gukbap.id, "eaten_at": "2026-11-05T12:00:00Z"},
    )
    # 2) 2026-11-06 18:00 KST == 2026-11-06 09:00 UTC
    r2 = api.client.post(
        URL,
        headers=api.headers,
        json={"menu_id": chicken.id, "eaten_at": "2026-11-06T18:00:00+09:00"},
    )
    # 3) 2026-11-06 10:00 UTC (r2보다 1시간 뒤)
    r3 = api.client.post(
        URL,
        headers=api.headers,
        json={"menu_id": pasta.id, "eaten_at": "2026-11-06T10:00:00Z"},
    )
    # 4) r3와 동일한 시각에 같은 메뉴를 한 번 더 먹은 경우 (id DESC 우선)
    r4 = api.client.post(
        URL,
        headers=api.headers,
        json={"menu_id": gukbap.id, "eaten_at": "2026-11-06T10:00:00Z"},
    )
    assert all(resp.status_code == 201 for resp in (r1, r2, r3, r4))

    listed = api.client.get(URL, headers=api.headers)
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert data["total_count"] == 4
    assert [item["id"] for item in data["items"]] == [
        r4.json()["data"]["id"],
        r3.json()["data"]["id"],
        r2.json()["data"]["id"],
        r1.json()["data"]["id"],
    ]
    assert [item["menu_name"] for item in data["items"]] == [
        "콩나물국밥",
        "토마토파스타",
        "땡초 치킨",
        "콩나물국밥",
    ]


def test_pagination_slices_items_while_preserving_total_count(api):
    gukbap = api.menus["gukbap"]
    created_ids = []
    for hour in range(5):
        resp = api.client.post(
            URL,
            headers=api.headers,
            json={"menu_id": gukbap.id, "eaten_at": f"2026-11-06T0{hour}:00:00Z"},
        )
        assert resp.status_code == 201
        created_ids.append(resp.json()["data"]["id"])

    expected_desc = list(reversed(created_ids))

    first_page = api.client.get(f"{URL}?limit=2&offset=0", headers=api.headers).json()["data"]
    assert first_page["total_count"] == 5
    assert [item["id"] for item in first_page["items"]] == expected_desc[:2]

    second_page = api.client.get(f"{URL}?limit=2&offset=2", headers=api.headers).json()["data"]
    assert second_page["total_count"] == 5
    assert [item["id"] for item in second_page["items"]] == expected_desc[2:4]

    beyond_end = api.client.get(f"{URL}?limit=2&offset=10", headers=api.headers).json()["data"]
    assert beyond_end["total_count"] == 5
    assert beyond_end["items"] == []


def test_user_isolation_and_query_user_id_ignored(api):
    own_resp = api.client.post(
        URL, headers=api.headers, json={"menu_id": api.menus["gukbap"].id}
    )
    other_resp = api.client.post(
        URL, headers=api.other_headers, json={"menu_id": api.menus["chicken"].id}
    )
    assert own_resp.status_code == 201 and other_resp.status_code == 201

    # 쿼리스트링으로 다른 user_id를 넘겨도 토큰 주인의 기록만 조회되어야 한다.
    my_list = api.client.get(f"{URL}?user_id={api.other.id}", headers=api.headers).json()["data"]
    assert my_list["total_count"] == 1
    assert [item["id"] for item in my_list["items"]] == [own_resp.json()["data"]["id"]]

    other_list = api.client.get(URL, headers=api.other_headers).json()["data"]
    assert other_list["total_count"] == 1
    assert [item["id"] for item in other_list["items"]] == [other_resp.json()["data"]["id"]]


def test_missing_menu_returns_404_domain_exception_without_saving(api):
    response = api.client.post(URL, headers=api.headers, json={"menu_id": 99999})
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "data": None,
        "message": "메뉴(id=99999)를 찾을 수 없습니다.",
    }
    assert api.db.query(UserMeal).count() == 0


def test_referential_integrity_preserves_meal_snapshot_on_menu_delete_and_cascades_on_user_delete(api):
    pasta = api.menus["pasta"]
    resp = api.client.post(URL, headers=api.headers, json={"menu_id": pasta.id})
    other_resp = api.client.post(URL, headers=api.other_headers, json={"menu_id": api.menus["chicken"].id})
    assert resp.status_code == 201 and other_resp.status_code == 201

    # 메뉴가 삭제되면 menu_id는 NULL이 되지만 식사 기록 스냅샷(menu_name)은 유지된다.
    api.db.delete(pasta)
    api.db.commit()
    api.db.expire_all()

    after_menu_delete = api.client.get(URL, headers=api.headers)
    assert after_menu_delete.status_code == 200
    items = after_menu_delete.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["menu_id"] is None
    assert items[0]["menu_name"] == "토마토파스타"
    assert items[0]["menu"] is None

    # Authentication references must be explicitly removed before deleting a user.
    api.db.query(AuthSession).filter(AuthSession.user_id == api.user.id).delete()
    # 사용자가 삭제되면 해당 사용자의 user_meals만 CASCADE 삭제된다.
    api.db.delete(api.user)
    api.db.commit()
    api.db.expire_all()

    remaining = api.db.query(UserMeal).all()
    assert [row.user_id for row in remaining] == [api.other.id]


def test_get_my_meals_uses_single_outer_join_without_n_plus_one(api):
    menus = list(api.menus.values())
    for idx in range(9):
        menu = menus[idx % len(menus)]
        resp = api.client.post(
            URL,
            headers=api.headers,
            json={"menu_id": menu.id, "eaten_at": f"2026-11-06T0{idx}:00:00Z"},
        )
        assert resp.status_code == 201

    statements: list[str] = []

    def _capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(api.engine, "before_cursor_execute", _capture_sql)
    try:
        response = api.client.get(URL, headers=api.headers)
    finally:
        event.remove(api.engine, "before_cursor_execute", _capture_sql)

    assert response.status_code == 200
    assert len(response.json()["data"]["items"]) == 9
    select_statements = [stmt for stmt in statements if stmt.lstrip().upper().startswith("SELECT")]
    # 1) users 인증 조회, 2) user_meals count 조회, 3) user_meals LEFT OUTER JOIN menus 조회
    assert len(select_statements) <= 3


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "token_kind",
    ["missing", "invalid", "expired", "unknown_user", "bad_subject", "wrong_scheme"],
)
def test_endpoints_require_valid_bearer_token_without_mutation(api, method, token_kind):
    headers = {}
    if token_kind == "invalid":
        headers["Authorization"] = "Bearer invalid"
    elif token_kind != "missing":
        subject = str(api.user.id)
        if token_kind == "unknown_user":
            subject = "99999"
        elif token_kind == "bad_subject":
            subject = "not-an-id"
        expiry = timedelta(seconds=-1) if token_kind == "expired" else timedelta(minutes=5)
        token = create_access_token({"sub": subject}, expires_delta=expiry)
        headers["Authorization"] = f'{"Basic" if token_kind == "wrong_scheme" else "Bearer"} {token}'

    kwargs = {"json": {"menu_id": api.menus["gukbap"].id}} if method == "POST" else {}
    response = api.client.request(method, URL, headers=headers, **kwargs)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert api.db.query(UserMeal).count() == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"menu_id": None},
        {"menu_id": 0},
        {"menu_id": -1},
        {"menu_id": True},
        {"menu_id": "1"},
        {"menu_id": 1.5},
        {"menu_id": 1, "eaten_at": 1700000000},
        {"menu_id": 1, "eaten_at": 1700000000.5},
        {"menu_id": 1, "eaten_at": True},
        {"menu_id": 1, "eaten_at": "not-a-datetime"},
        {"menu_id": 1, "user_id": 999},
        {"menu_id": 1, "menu_name": "임의이름"},
    ],
)
def test_invalid_post_payload_returns_422_without_saving(api, body):
    response = api.client.post(URL, headers=api.headers, json=body)
    assert response.status_code == 422
    assert api.db.query(UserMeal).count() == 0


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=-1",
        "limit=201",
        "limit=abc",
        "offset=-1",
        "offset=abc",
    ],
)
def test_invalid_pagination_query_returns_422(api, query):
    response = api.client.get(f"{URL}?{query}", headers=api.headers)
    assert response.status_code == 422


def test_naive_datetime_in_request_is_normalized_to_utc(api):
    response = api.client.post(
        URL,
        headers=api.headers,
        json={"menu_id": api.menus["gukbap"].id, "eaten_at": "2026-11-06T08:30:00"},
    )
    assert response.status_code == 201
    eaten_at = datetime.fromisoformat(response.json()["data"]["eaten_at"].replace("Z", "+00:00"))
    assert eaten_at == datetime(2026, 11, 6, 8, 30, 0, tzinfo=timezone.utc)


def test_db_error_during_record_my_meal_rolls_back(api, monkeypatch):
    existing = api.client.post(URL, headers=api.headers, json={"menu_id": api.menus["gukbap"].id})
    assert existing.status_code == 201

    def _fail(*args, **kwargs):
        raise SQLAlchemyError("simulated write failure")

    monkeypatch.setattr(repository, "create_user_meal", _fail)
    with pytest.raises(SQLAlchemyError):
        record_my_meal(api.db, api.user.id, UserMealCreateRequest(menu_id=api.menus["chicken"].id))

    items = api.client.get(URL, headers=api.headers).json()["data"]["items"]
    assert [item["id"] for item in items] == [existing.json()["data"]["id"]]


from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.auth.models  # noqa: F401
import app.map.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.users.models  # noqa: F401
from app.auth.dependencies import get_current_user
from app.auth.errors import AuthError
from app.auth.sessions import create_uuid_user
from app.core.database import Base, get_db
from app.map.exceptions import PlaceSearchException
from app.map.kakao_local import get_place_search_client
from app.map.models import PlaceSatisfaction, PlaceSelection
from app.map.router import router
from app.menus.models import (
    Menu,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
)
from app.users.models import User

ORIGIN_LAT = 35.1336
ORIGIN_LNG = 129.1030


def make_menu(food_code: str, name: str, base_name: str) -> Menu:
    return Menu(
        food_code=food_code,
        name=name,
        base_name=base_name,
        category_large="외식",
        category_medium="식사",
        origin_type="외식",
        meal_type="main_meal",
        has_soup=True,
        cooking_style="soup_stew",
        main_ingredient="pork",
        flavor_intensity="mild_clean",
        temperature="hot",
        heaviness="moderate",
        allergen_tags=[],
    )


def make_document(place_id: str, name: str, distance: str = "120") -> dict:
    return {
        "id": place_id,
        "place_name": name,
        "category_name": "음식점 > 한식 > 국밥",
        "category_group_code": "FD6",
        "phone": "051-000-0000",
        "address_name": "부산 남구 대연동 1",
        "road_address_name": "부산 남구 용소로 45",
        "x": "129.1040",
        "y": "35.1340",
        "place_url": f"http://place.map.kakao.com/{place_id}",
        "distance": distance,
    }


class FakePlaceSearchClient:
    def __init__(self, documents=None):
        self.documents = documents or []

    def search_restaurants(self, **kwargs):
        return self.documents


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def seed_data(db):
    user = create_uuid_user(db)
    other_user = create_uuid_user(db)
    menu1 = make_menu("gukbap-1", "국밥_돼지국밥", "국밥")
    menu2 = make_menu("udon-1", "우동_가쓰오우동", "우동")
    db.add_all([menu1, menu2])
    db.flush()

    recommendation = Recommendation(
        user_id=user.id,
        shown_question_ids=[1, 2],
        answers_json=[{"question_id": 1, "option_id": "soup_yes"}],
        recommended_menu_ids=[menu1.id],
    )
    db.add(recommendation)
    db.commit()
    db.refresh(user)
    db.refresh(other_user)
    db.refresh(menu1)
    db.refresh(menu2)
    db.refresh(recommendation)
    return {
        "user": user,
        "other_user": other_user,
        "menu1": menu1,
        "menu2": menu2,
        "recommendation": recommendation,
    }


def build_client(db, user: User | None = None, fake_docs=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(PlaceSearchException)
    async def handle_place(request: Request, exc: PlaceSearchException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "data": None, "message": exc.message},
        )

    @app.exception_handler(AuthError)
    async def handle_auth(request: Request, exc: AuthError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_place_search_client] = lambda: FakePlaceSearchClient(fake_docs or [])
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_viewing_nearby_places_creates_no_selection_satisfaction_or_menu_feedback(db, seed_data):
    user = seed_data["user"]
    menu1 = seed_data["menu1"]
    client = build_client(db, user=user, fake_docs=[make_document("place-101", "쌍둥이돼지국밥")])

    response = client.get(
        "/api/places/nearby",
        params={"menu_id": menu1.id, "latitude": ORIGIN_LAT, "longitude": ORIGIN_LNG},
    )

    assert response.status_code == 200
    assert response.json()["data"]["total_count"] == 1
    assert db.query(PlaceSelection).count() == 0
    assert db.query(PlaceSatisfaction).count() == 0
    assert db.query(RecommendationFeedback).count() == 0
    assert db.query(RecommendationFeedbackState).count() == 0


def test_explicit_place_selection_links_recommendation_menu_and_leaves_satisfaction_unconfirmed(db, seed_data):
    user = seed_data["user"]
    menu1 = seed_data["menu1"]
    rec = seed_data["recommendation"]
    client = build_client(db, user=user)

    response = client.post(
        "/api/places/selections",
        json={
            "recommendation_id": rec.id,
            "menu_id": menu1.id,
            "kakao_place_id": "place-101",
            "place_name": "쌍둥이돼지국밥",
            "place_address": "부산 남구 대연동 1",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["id"] >= 1
    assert data["recommendation_id"] == rec.id
    assert data["menu_id"] == menu1.id
    assert data["kakao_place_id"] == "place-101"
    assert data["place_name"] == "쌍둥이돼지국밥"
    assert data["place_address"] == "부산 남구 대연동 1"
    assert data["satisfaction"] is None

    assert db.query(PlaceSelection).count() == 1
    assert db.query(PlaceSatisfaction).count() == 0
    assert db.query(RecommendationFeedback).count() == 0
    assert db.query(RecommendationFeedbackState).count() == 0


def test_place_selection_allows_omitted_recommendation_id_and_strips_address(db, seed_data):
    user = seed_data["user"]
    menu2 = seed_data["menu2"]
    client = build_client(db, user=user)

    response = client.post(
        "/api/places/selections",
        json={
            "menu_id": menu2.id,
            "kakao_place_id": "  place-202  ",
            "place_name": "  부산우동  ",
            "place_address": "   ",
        },
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["recommendation_id"] is None
    assert data["menu_id"] == menu2.id
    assert data["kakao_place_id"] == "place-202"
    assert data["place_name"] == "부산우동"
    assert data["place_address"] is None
    assert data["satisfaction"] is None


def test_place_selection_validation_errors(db, seed_data):
    user = seed_data["user"]
    other_user = seed_data["other_user"]
    menu1 = seed_data["menu1"]
    menu2 = seed_data["menu2"]
    rec = seed_data["recommendation"]
    client = build_client(db, user=user)
    other_client = build_client(db, user=other_user)
    unauth_client = build_client(db, user=None)

    # 1. Missing menu -> 404
    res = client.post(
        "/api/places/selections",
        json={"menu_id": 99999, "kakao_place_id": "p1", "place_name": "가게"},
    )
    assert res.status_code == 404
    assert res.json()["success"] is False

    # 2. Missing recommendation -> 404
    res = client.post(
        "/api/places/selections",
        json={"recommendation_id": 99999, "menu_id": menu1.id, "kakao_place_id": "p1", "place_name": "가게"},
    )
    assert res.status_code == 404
    assert res.json()["success"] is False

    # 3. Another user's recommendation -> 403
    res = other_client.post(
        "/api/places/selections",
        json={"recommendation_id": rec.id, "menu_id": menu1.id, "kakao_place_id": "p1", "place_name": "가게"},
    )
    assert res.status_code == 403
    assert res.json()["success"] is False

    # 4. Menu not included in recommendation -> 400
    res = client.post(
        "/api/places/selections",
        json={"recommendation_id": rec.id, "menu_id": menu2.id, "kakao_place_id": "p1", "place_name": "가게"},
    )
    assert res.status_code == 400
    assert res.json()["success"] is False

    # 5. Blank kakao_place_id / place_name -> 422
    res = client.post(
        "/api/places/selections",
        json={"menu_id": menu1.id, "kakao_place_id": "   ", "place_name": "가게"},
    )
    assert res.status_code == 422

    # 6. Unauthenticated -> 401
    res = unauth_client.post(
        "/api/places/selections",
        json={"menu_id": menu1.id, "kakao_place_id": "p1", "place_name": "가게"},
    )
    assert res.status_code == 401
    assert db.query(PlaceSelection).count() == 0


def test_get_and_list_place_selections_enforce_owner_and_filters(db, seed_data):
    user = seed_data["user"]
    other_user = seed_data["other_user"]
    menu1 = seed_data["menu1"]
    menu2 = seed_data["menu2"]
    rec = seed_data["recommendation"]
    client = build_client(db, user=user)
    other_client = build_client(db, user=other_user)

    sel1 = client.post(
        "/api/places/selections",
        json={
            "recommendation_id": rec.id,
            "menu_id": menu1.id,
            "kakao_place_id": "place-101",
            "place_name": "쌍둥이돼지국밥",
        },
    ).json()["data"]
    sel2 = client.post(
        "/api/places/selections",
        json={
            "menu_id": menu2.id,
            "kakao_place_id": "place-202",
            "place_name": "부산우동",
        },
    ).json()["data"]
    other_client.post(
        "/api/places/selections",
        json={
            "menu_id": menu1.id,
            "kakao_place_id": "place-999",
            "place_name": "다른사람가게",
        },
    )

    # Read single selection (satisfaction stays None / 미확인)
    get_res = client.get(f"/api/places/selections/{sel1['id']}")
    assert get_res.status_code == 200
    assert get_res.json()["data"]["id"] == sel1["id"]
    assert get_res.json()["data"]["recommendation_id"] == rec.id
    assert get_res.json()["data"]["satisfaction"] is None

    # Owner check: other user -> 403, missing -> 404
    assert other_client.get(f"/api/places/selections/{sel1['id']}").status_code == 403
    assert client.get("/api/places/selections/99999").status_code == 404

    # List selections scoped to current user only, newest first
    list_res = client.get("/api/places/selections")
    assert list_res.status_code == 200
    list_data = list_res.json()["data"]
    assert list_data["total_count"] == 2
    assert [s["id"] for s in list_data["selections"]] == [sel2["id"], sel1["id"]]

    # Filter by recommendation_id
    filtered_res = client.get("/api/places/selections", params={"recommendation_id": rec.id})
    assert filtered_res.status_code == 200
    assert [s["id"] for s in filtered_res.json()["data"]["selections"]] == [sel1["id"]]

    # Filter by menu_id
    menu_filtered = client.get("/api/places/selections", params={"menu_id": menu2.id})
    assert menu_filtered.status_code == 200
    assert [s["id"] for s in menu_filtered.json()["data"]["selections"]] == [sel2["id"]]


def test_explicit_place_satisfaction_is_separate_upsertable_and_never_creates_menu_feedback(db, seed_data):
    user = seed_data["user"]
    other_user = seed_data["other_user"]
    menu1 = seed_data["menu1"]
    rec = seed_data["recommendation"]
    client = build_client(db, user=user, fake_docs=[make_document("place-101", "쌍둥이돼지국밥")])
    other_client = build_client(db, user=other_user)
    unauth_client = build_client(db, user=None)

    # 1. View nearby places -> still 0 selections, 0 satisfactions, 0 menu feedbacks
    assert client.get(
        "/api/places/nearby",
        params={"menu_id": menu1.id, "latitude": ORIGIN_LAT, "longitude": ORIGIN_LNG},
    ).status_code == 200

    # 2. Explicitly select place -> satisfaction is None (미확인)
    sel_res = client.post(
        "/api/places/selections",
        json={
            "recommendation_id": rec.id,
            "menu_id": menu1.id,
            "kakao_place_id": "place-101",
            "place_name": "쌍둥이돼지국밥",
            "place_address": "부산 남구 대연동 1",
        },
    )
    assert sel_res.status_code == 201
    selection_id = sel_res.json()["data"]["id"]
    assert sel_res.json()["data"]["satisfaction"] is None
    assert db.query(PlaceSatisfaction).count() == 0

    # 3. Owner & existence checks on satisfaction endpoint
    assert other_client.post(
        f"/api/places/selections/{selection_id}/satisfaction",
        json={"is_satisfied": True},
    ).status_code == 403
    assert client.post(
        "/api/places/selections/99999/satisfaction",
        json={"is_satisfied": True},
    ).status_code == 404
    assert unauth_client.post(
        f"/api/places/selections/{selection_id}/satisfaction",
        json={"is_satisfied": True},
    ).status_code == 401
    assert db.query(PlaceSatisfaction).count() == 0

    # 4. Explicitly record positive satisfaction
    sat_res = client.post(
        f"/api/places/selections/{selection_id}/satisfaction",
        json={"is_satisfied": True},
    )
    assert sat_res.status_code == 200
    sat_data = sat_res.json()["data"]
    assert sat_data["place_selection_id"] == selection_id
    assert sat_data["recommendation_id"] == rec.id
    assert sat_data["menu_id"] == menu1.id
    assert sat_data["kakao_place_id"] == "place-101"
    assert sat_data["is_satisfied"] is True
    sat_id = sat_data["id"]

    # 5. Read back selection -> satisfaction is populated
    read_res = client.get(f"/api/places/selections/{selection_id}")
    assert read_res.status_code == 200
    assert read_res.json()["data"]["satisfaction"]["id"] == sat_id
    assert read_res.json()["data"]["satisfaction"]["is_satisfied"] is True

    # 6. Idempotent retry with same value -> same row, no duplicates
    retry_res = client.post(
        f"/api/places/selections/{selection_id}/satisfaction",
        json={"is_satisfied": True},
    )
    assert retry_res.status_code == 200
    assert retry_res.json()["data"]["id"] == sat_id
    assert db.query(PlaceSatisfaction).count() == 1

    # 7. Safe upsert when user updates satisfaction to False
    toggle_res = client.post(
        f"/api/places/selections/{selection_id}/satisfaction",
        json={"is_satisfied": False},
    )
    assert toggle_res.status_code == 200
    assert toggle_res.json()["data"]["id"] == sat_id
    assert toggle_res.json()["data"]["is_satisfied"] is False
    assert db.query(PlaceSatisfaction).count() == 1
    assert client.get(f"/api/places/selections/{selection_id}").json()["data"]["satisfaction"]["is_satisfied"] is False

    # 8. Crucially: viewing, selecting, and rating a place NEVER creates menu like/dislike records
    assert db.query(RecommendationFeedback).count() == 0
    assert db.query(RecommendationFeedbackState).count() == 0


def test_main_app_registers_place_selection_routes_and_tables():
    from app.main import app

    route_paths = {route.path for route in app.routes}
    assert "/api/places/nearby" in route_paths
    assert "/api/places/selections" in route_paths
    assert "/api/places/selections/{selection_id}" in route_paths
    assert "/api/places/selections/{selection_id}/satisfaction" in route_paths
    assert "place_selections" in Base.metadata.tables
    assert "place_satisfactions" in Base.metadata.tables


def test_uuid_to_kakao_transition_preserves_place_selections_and_satisfactions(monkeypatch):
    from sqlalchemy import event

    from app.auth.sessions import new_session
    from app.core.config import settings
    from app.core.security import create_access_token
    from app.main import app

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        bind=engine,
    )
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with TestingSessionLocal() as session:
            yield session

    async def fake_resolve_kakao(code: str) -> dict:
        return {
            "kakao_id": 888080,
            "nickname": "카카오유저80",
            "profile_image": "https://k.kakaocdn.net/dn/default.jpg",
            "email": "user888080@example.com",
        }

    monkeypatch.setattr("app.auth.transitions.resolve_kakao_profile_from_code", fake_resolve_kakao)
    app.dependency_overrides[get_db] = override_get_db

    csrf_headers = {
        "Origin": "http://localhost:8081",
        "X-Whatbob-Request": "1",
    }

    with TestingSessionLocal() as session:
        origin = create_uuid_user(session)
        menu1 = make_menu("tr-gukbap-1", "국밥_돼지국밥", "국밥")
        session.add(menu1)
        session.flush()
        rec = Recommendation(
            user_id=origin.id,
            shown_question_ids=[1],
            answers_json=[],
            recommended_menu_ids=[menu1.id],
        )
        session.add(rec)
        session.flush()
        sess_rec, secret = new_session(user_id=origin.id)
        session.add(sess_rec)
        session.commit()
        session.refresh(origin)
        session.refresh(menu1)
        session.refresh(rec)
        session.refresh(sess_rec)
        origin_id = origin.id
        origin_sid = sess_rec.id
        menu1_id = menu1.id
        rec_id = rec.id
        origin_token = create_access_token({"sub": str(origin.id), "sid": str(sess_rec.id)})

    client = TestClient(app, base_url="https://testserver")
    try:
        origin_headers = {**csrf_headers, "Authorization": f"Bearer {origin_token}"}
        client.cookies.set(settings.AUTH_COOKIE_NAME, secret, domain="testserver.local", path="/api/auth")

        # Create 2 selections: one rated, one unconfirmed (satisfaction=None)
        sel_rated = client.post(
            "/api/places/selections",
            json={
                "recommendation_id": rec_id,
                "menu_id": menu1_id,
                "kakao_place_id": "place-rated",
                "place_name": "평가한 국밥집",
                "place_address": "부산 남구 용소로 1",
            },
            headers=origin_headers,
        )
        assert sel_rated.status_code == 201
        rated_id = sel_rated.json()["data"]["id"]

        sat_res = client.post(
            f"/api/places/selections/{rated_id}/satisfaction",
            json={"is_satisfied": True},
            headers=origin_headers,
        )
        assert sat_res.status_code == 200
        sat_id = sat_res.json()["data"]["id"]

        sel_unconfirmed = client.post(
            "/api/places/selections",
            json={
                "recommendation_id": rec_id,
                "menu_id": menu1_id,
                "kakao_place_id": "place-unconfirmed",
                "place_name": "미확인 국밥집",
            },
            headers=origin_headers,
        )
        assert sel_unconfirmed.status_code == 201
        unconfirmed_id = sel_unconfirmed.json()["data"]["id"]

        # Execute UUID -> Kakao link transition
        prep = client.post(
            "/api/auth/transitions",
            json={"action": "KAKAO_LINK", "code": "valid-code"},
            headers=origin_headers,
        )
        assert prep.status_code == 200
        transition_id = prep.json()["transition_id"]

        commit = client.post(
            f"/api/auth/transitions/{transition_id}/commit",
            headers={**csrf_headers, "X-Whatbob-Auth-Context": str(origin_sid)},
        )
        assert commit.status_code == 200
        target_id = commit.json()["user"]["id"]
        target_token = commit.json()["access_token"]
        assert target_id != origin_id

        # Origin user physically deleted; place records migrated to target user
        with TestingSessionLocal() as session:
            assert session.get(User, origin_id) is None
            selections = session.query(PlaceSelection).order_by(PlaceSelection.id.asc()).all()
            assert len(selections) == 2
            assert all(s.user_id == target_id for s in selections)
            satisfactions = session.query(PlaceSatisfaction).all()
            assert len(satisfactions) == 1
            assert satisfactions[0].id == sat_id
            assert satisfactions[0].user_id == target_id
            assert session.query(RecommendationFeedback).count() == 0
            assert session.query(RecommendationFeedbackState).count() == 0

        # Target Kakao user can read migrated selections and submit satisfaction on unconfirmed one
        target_headers = {**csrf_headers, "Authorization": f"Bearer {target_token}"}
        list_res = client.get("/api/places/selections", headers=target_headers)
        assert list_res.status_code == 200
        by_id = {item["id"]: item for item in list_res.json()["data"]["selections"]}
        assert set(by_id.keys()) == {rated_id, unconfirmed_id}
        assert by_id[rated_id]["satisfaction"]["is_satisfied"] is True
        assert by_id[unconfirmed_id]["satisfaction"] is None

        rate_migrated = client.post(
            f"/api/places/selections/{unconfirmed_id}/satisfaction",
            json={"is_satisfied": False},
            headers=target_headers,
        )
        assert rate_migrated.status_code == 200
        assert rate_migrated.json()["data"]["is_satisfied"] is False
    finally:
        client.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()

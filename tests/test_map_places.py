from urllib.parse import unquote

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import get_current_user
from app.core.database import Base, get_db
from app.map.exceptions import PlaceSearchException, PlaceSearchUnavailableException
from app.map.kakao_local import KAKAO_KEYWORD_SEARCH_URL, KakaoLocalClient, get_place_search_client
from app.map.router import router
from app.map.search_keywords import build_place_search_keyword
from app.map.service import build_walking_directions_url
from app.menus.models import Menu
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
        has_soup=False,
        cooking_style="other",
        main_ingredient="other",
        flavor_intensity="mild_clean",
        temperature="hot",
        heaviness="moderate",
        allergen_tags=[],
    )


def make_document(place_id: str, name: str, distance: str | None, **overrides) -> dict:
    document = {
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
    document.update(overrides)
    return document


class FakePlaceSearchClient:
    def __init__(self, documents=None, error: Exception | None = None):
        self.documents = documents or []
        self.error = error
        self.calls: list[dict] = []

    def search_restaurants(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.documents


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def menu(db):
    item = make_menu("gukbap-1", "국밥_돼지국밥", "국밥")
    db.add(item)
    db.commit()
    return item


def build_client(db, fake: FakePlaceSearchClient, authenticated: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(PlaceSearchException)
    async def handle(request: Request, exc: PlaceSearchException):
        return JSONResponse(status_code=exc.status_code, content={"success": False, "data": None, "message": exc.message})

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_place_search_client] = lambda: fake
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: User(id=1)
    return TestClient(app)


def nearby_params(menu_id: int, **overrides) -> dict:
    params = {"menu_id": menu_id, "latitude": ORIGIN_LAT, "longitude": ORIGIN_LNG}
    params.update(overrides)
    return params


def test_returns_restaurants_sorted_by_straight_line_distance(db, menu):
    fake = FakePlaceSearchClient([
        make_document("2", "먼 국밥집", "800"),
        make_document("1", "가까운 국밥집", "120"),
    ])
    response = build_client(db, fake).get("/api/places/nearby", params=nearby_params(menu.id))

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["search_keyword"] == "국밥"
    assert data["radius_m"] == 1000
    assert data["distance_type"] == "STRAIGHT_LINE"
    assert [p["kakao_place_id"] for p in data["places"]] == ["1", "2"]
    first = data["places"][0]
    assert first["distance_m"] == 120
    assert first["directions_url"] == "https://map.kakao.com/link/to/1"
    assert first["walking_directions_url"].startswith("https://map.kakao.com/link/by/walk/")
    assert fake.calls == [{
        "keyword": "국밥",
        "latitude": ORIGIN_LAT,
        "longitude": ORIGIN_LNG,
        "radius_m": 1000,
        "size": 10,
    }]


def test_excludes_non_restaurant_out_of_radius_duplicate_and_invalid_results(db, menu):
    fake = FakePlaceSearchClient([
        make_document("1", "국밥집", "100"),
        make_document("1", "국밥집 중복", "100"),
        make_document("2", "국밥 카페", "50", category_group_code="CE7"),
        make_document("3", "반경 밖 국밥집", "1500"),
        make_document("4", "좌표 없는 국밥집", "200", x=None),
        make_document("", "ID 없는 국밥집", "200"),
    ])
    data = build_client(db, fake).get("/api/places/nearby", params=nearby_params(menu.id)).json()["data"]

    assert [p["kakao_place_id"] for p in data["places"]] == ["1"]
    assert data["total_count"] == 1


def test_returns_at_most_ten_places(db, menu):
    fake = FakePlaceSearchClient([make_document(str(i), f"국밥집{i}", str(i * 10)) for i in range(1, 16)])
    data = build_client(db, fake).get("/api/places/nearby", params=nearby_params(menu.id)).json()["data"]

    assert len(data["places"]) == 10
    assert data["places"][-1]["kakao_place_id"] == "10"


def test_empty_result_does_not_expand_radius_automatically(db, menu):
    fake = FakePlaceSearchClient([])
    response = build_client(db, fake).get("/api/places/nearby", params=nearby_params(menu.id))

    assert response.status_code == 200
    assert response.json()["data"]["places"] == []
    assert len(fake.calls) == 1
    assert fake.calls[0]["radius_m"] == 1000


def test_expanded_radius_is_used_when_requested(db, menu):
    fake = FakePlaceSearchClient([make_document("1", "국밥집", "2500")])
    data = build_client(db, fake).get(
        "/api/places/nearby", params=nearby_params(menu.id, radius=3000)
    ).json()["data"]

    assert data["radius_m"] == 3000
    assert fake.calls[0]["radius_m"] == 3000
    assert [p["distance_m"] for p in data["places"]] == [2500]


@pytest.mark.parametrize("override", [
    {"radius": 2000},
    {"radius": 20000},
    {"latitude": 91},
    {"longitude": -181},
    {"menu_id": 0},
])
def test_rejects_invalid_input_without_calling_kakao(db, menu, override):
    fake = FakePlaceSearchClient([])
    params = nearby_params(menu.id)
    params.update(override)
    response = build_client(db, fake).get("/api/places/nearby", params=params)

    assert response.status_code == 422
    assert fake.calls == []


def test_unknown_menu_returns_404(db, menu):
    fake = FakePlaceSearchClient([])
    response = build_client(db, fake).get("/api/places/nearby", params=nearby_params(9999))

    assert response.status_code == 404
    assert response.json()["success"] is False
    assert fake.calls == []


def test_external_failure_returns_503(db, menu):
    fake = FakePlaceSearchClient(error=PlaceSearchUnavailableException())
    response = build_client(db, fake).get("/api/places/nearby", params=nearby_params(menu.id))

    assert response.status_code == 503
    assert response.json() == {
        "success": False,
        "data": None,
        "message": "주변 음식점 검색을 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    }


def test_application_registers_map_and_menu_routers():
    from app.main import app

    route_paths = {route.path for route in app.routes}
    assert "/api/places/nearby" in route_paths
    assert "/api/menus/popular" in route_paths


def test_requires_authentication(db, menu):
    fake = FakePlaceSearchClient([])
    response = build_client(db, fake, authenticated=False).get("/api/places/nearby", params=nearby_params(menu.id))

    assert response.status_code == 401
    assert fake.calls == []


def test_missing_distance_falls_back_to_straight_line_calculation(db, menu):
    fake = FakePlaceSearchClient([make_document("1", "국밥집", None, x=f"{ORIGIN_LNG}", y="35.1416")])
    data = build_client(db, fake).get("/api/places/nearby", params=nearby_params(menu.id)).json()["data"]

    # 위도 0.008도는 약 890m이므로 직선거리 계산값이 그 근처여야 한다.
    assert 880 <= data["places"][0]["distance_m"] <= 900


@pytest.mark.parametrize(("base_name", "expected"), [
    ("국밥", "국밥"),
    ("닭튀김", "치킨"),
    ("스파게티", "파스타"),
    ("닭볶음(닭갈비)", "닭갈비"),
    ("고구마죽(단호박)", "고구마죽"),
    ("파스타/스파게티", "파스타"),
    ("  ", "국밥_돼지국밥"),
])
def test_build_place_search_keyword(base_name, expected):
    assert build_place_search_keyword(base_name, "국밥_돼지국밥") == expected


def test_walking_directions_url_escapes_link_separators():
    url = build_walking_directions_url(35.1, 129.1, "국밥, 본점/2호", 35.2, 129.2)

    prefix = "https://map.kakao.com/link/by/walk/"
    assert url.startswith(prefix)
    origin, destination = url[len(prefix):].split("/")
    assert unquote(origin) == "내 위치,35.1,129.1"
    assert unquote(destination) == "국밥 본점 2호,35.2,129.2"


def test_kakao_client_sends_restaurant_distance_query():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"documents": [make_document("1", "국밥집", "10")], "meta": {}})

    client = KakaoLocalClient(" kakao-key ", transport=httpx.MockTransport(handler))
    documents = client.search_restaurants("국밥", ORIGIN_LAT, ORIGIN_LNG, 1000, 10)

    request = captured["request"]
    assert str(request.url).startswith(KAKAO_KEYWORD_SEARCH_URL)
    assert request.headers["Authorization"] == "KakaoAK kakao-key"
    params = request.url.params
    assert params["query"] == "국밥"
    assert params["category_group_code"] == "FD6"
    assert params["sort"] == "distance"
    assert params["radius"] == "1000"
    assert params["x"] == str(ORIGIN_LNG)
    assert params["y"] == str(ORIGIN_LAT)
    assert params["size"] == "10"
    assert [d["id"] for d in documents] == ["1"]


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(401, json={"msg": "invalid key"}),
    lambda request: httpx.Response(429, json={"msg": "quota"}),
    lambda request: httpx.Response(200, text="not-json"),
    lambda request: httpx.Response(200, json=[]),
    lambda request: httpx.Response(200, json=None),
    lambda request: httpx.Response(200, json={"meta": {}}),
])
def test_kakao_client_maps_bad_responses_to_unavailable(handler):
    client = KakaoLocalClient("kakao-key", transport=httpx.MockTransport(handler))
    with pytest.raises(PlaceSearchUnavailableException):
        client.search_restaurants("국밥", ORIGIN_LAT, ORIGIN_LNG, 1000, 10)


def test_kakao_client_maps_timeout_to_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    client = KakaoLocalClient("kakao-key", transport=httpx.MockTransport(handler))
    with pytest.raises(PlaceSearchUnavailableException):
        client.search_restaurants("국밥", ORIGIN_LAT, ORIGIN_LNG, 1000, 10)

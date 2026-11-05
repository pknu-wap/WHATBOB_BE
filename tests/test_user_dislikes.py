from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.main import app
from app.menus.models import Menu, OrderSheetQuestion, UserFoodExclusion
from app.recommend.question_pool import build_candidate_question_pool
from app.users import repository
from app.users.models import User
from app.users.schemas import DislikedFoodsUpdateRequest
from app.users.service import replace_my_disliked_foods

URL = "/api/users/me/dislikes"


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    overrides = app.dependency_overrides.copy()
    try:
        with Session(engine) as db:
            user, other = User(kakao_id=88001), User(kakao_id=88002)
            db.add_all([user, other])
            db.commit()
            app.dependency_overrides[get_db] = lambda: db
            yield SimpleNamespace(
                client=TestClient(app), db=db, user=user, other=other,
                headers={"Authorization": f'Bearer {create_access_token({"sub": str(user.id)})}'},
                other_headers={"Authorization": f'Bearer {create_access_token({"sub": str(other.id)})}'},
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        engine.dispose()


def add_exclusion(api, keyword, exclusion_type="DISLIKE", other=False):
    row = UserFoodExclusion(user_id=api.other.id if other else api.user.id,
                            keyword=keyword, exclusion_type=exclusion_type)
    api.db.add(row)
    api.db.commit()
    return row.id


def test_empty_list_has_common_response_and_does_not_change_profile(api):
    response = api.client.get(URL, headers=api.headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"success": True, "data": {"items": [], "total_count": 0}, "message": None}
    profile = api.client.get("/api/users/me", headers=api.headers)
    assert profile.status_code == 200 and profile.json()["id"] == api.user.id
    assert "data" not in profile.json()


def test_save_trims_deduplicates_and_is_idempotent(api):
    response = api.client.put(URL, headers=api.headers, json={"keywords": [" 피자 ", "홍어", "피자", "ABC", "abc"]})
    assert response.status_code == 200
    first = response.json()["data"]
    assert first["total_count"] == 3
    assert [item["keyword"] for item in first["items"]] == ["피자", "홍어", "ABC"]
    assert all(set(item) == {"id", "keyword"} for item in first["items"])
    assert api.client.get(URL, headers=api.headers).json()["data"] == first
    repeated = api.client.put(URL, headers=api.headers, json={"keywords": ["피자", "홍어", "ABC"]})
    assert repeated.json()["data"] == first
    assert api.db.query(UserFoodExclusion).count() == 3


def test_full_replace_keeps_retained_ids_and_other_exclusion_types(api):
    pizza_id = add_exclusion(api, "피자")
    old_id = add_exclusion(api, "홍어")
    allergy_id = add_exclusion(api, "피자", "ALLERGY")
    unable_id = add_exclusion(api, "우유", "UNABLE")
    other_id = add_exclusion(api, "해산물", other=True)
    response = api.client.put(URL, headers=api.headers, json={"keywords": ["피자", "버섯"]})
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert {item["keyword"] for item in items} == {"피자", "버섯"}
    assert next(item["id"] for item in items if item["keyword"] == "피자") == pizza_id
    assert api.db.get(UserFoodExclusion, old_id) is None
    assert all(api.db.get(UserFoodExclusion, identifier) is not None for identifier in (allergy_id, unable_id, other_id))
    assert api.client.get(URL, headers=api.other_headers).json()["data"]["items"] == [{"id": other_id, "keyword": "해산물"}]


def test_empty_array_clears_only_my_dislikes(api):
    add_exclusion(api, "피자")
    protected = [add_exclusion(api, "밀", "ALLERGY"), add_exclusion(api, "우유", "UNABLE"),
                 add_exclusion(api, "피자", other=True)]
    response = api.client.put(URL, headers=api.headers, json={"keywords": []})
    assert response.status_code == 200 and response.json()["data"] == {"items": [], "total_count": 0}
    assert {row.id for row in api.db.query(UserFoodExclusion)} == set(protected)


def test_get_always_uses_token_identity(api):
    own_id = add_exclusion(api, "홍어")
    add_exclusion(api, "피자", other=True)
    response = api.client.get(f"{URL}?user_id={api.other.id}", headers=api.headers)
    assert response.json()["data"]["items"] == [{"id": own_id, "keyword": "홍어"}]


def test_delete_one_returns_no_content_and_repeat_is_not_found(api):
    own_id = add_exclusion(api, "피자")
    kept_id = add_exclusion(api, "홍어")
    response = api.client.delete(f"{URL}/{own_id}", headers=api.headers)
    assert response.status_code == 204 and response.content == b""
    assert api.client.get(URL, headers=api.headers).json()["data"]["items"] == [{"id": kept_id, "keyword": "홍어"}]
    assert api.client.delete(f"{URL}/{own_id}", headers=api.headers).status_code == 404


@pytest.mark.parametrize("kind", ["other_user", "allergy", "unable", "missing"])
def test_delete_cannot_touch_other_user_or_other_exclusion_type(api, kind):
    if kind == "missing":
        identifier = 99999
    else:
        identifier = add_exclusion(api, "피자", exclusion_type={"allergy": "ALLERGY", "unable": "UNABLE"}.get(kind, "DISLIKE"),
                                   other=kind == "other_user")
    response = api.client.delete(f"{URL}/{identifier}", headers=api.headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "비선호 음식 항목을 찾을 수 없습니다."}
    if kind != "missing":
        assert api.db.get(UserFoodExclusion, identifier) is not None


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
@pytest.mark.parametrize("token_kind", ["missing", "invalid", "expired", "unknown_user", "bad_subject", "wrong_scheme"])
def test_all_endpoints_require_valid_login_without_mutation(api, method, token_kind):
    identifier = add_exclusion(api, "피자")
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
    kwargs = {"json": {"keywords": []}} if method == "PUT" else {}
    response = api.client.request(method, f"{URL}/{identifier}" if method == "DELETE" else URL, headers=headers, **kwargs)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert api.db.get(UserFoodExclusion, identifier) is not None


@pytest.mark.parametrize("body", [
    {}, {"keywords": None}, {"keywords": "피자"}, {"keywords": [None]}, {"keywords": [12]},
    {"keywords": [True]}, {"keywords": [""]}, {"keywords": [" \t "]},
    {"keywords": ["가" * 101]}, {"keywords": [str(index) for index in range(101)]},
    {"keywords": ["피자"], "user_id": 999}, {"keywords": ["피자"], "exclusion_type": "ALLERGY"},
])
def test_bad_input_is_rejected_without_erasing_saved_list(api, body):
    identifier = add_exclusion(api, "홍어")
    response = api.client.put(URL, headers=api.headers, json=body)
    assert response.status_code == 422
    assert api.db.get(UserFoodExclusion, identifier).keyword == "홍어"
    assert api.db.query(UserFoodExclusion).count() == 1


def test_keyword_and_list_length_boundaries_are_accepted(api):
    keywords = ["가" * 100] + [f"음식{index}" for index in range(99)]
    response = api.client.put(URL, headers=api.headers, json={"keywords": keywords})
    assert response.status_code == 200 and response.json()["data"]["total_count"] == 100


def test_failed_save_rolls_back_deletions_and_insertions(api, monkeypatch):
    original_id = add_exclusion(api, "피자")
    def fail(*args, **kwargs):
        raise SQLAlchemyError("simulated persistence failure")
    monkeypatch.setattr(repository, "add_disliked_food", fail)
    with pytest.raises(SQLAlchemyError):
        replace_my_disliked_foods(api.db, api.user.id, DislikedFoodsUpdateRequest(keywords=["홍어"]))
    assert api.client.get(URL, headers=api.headers).json()["data"]["items"] == [{"id": original_id, "keyword": "피자"}]


def test_saved_dislikes_apply_to_next_recommendation_and_clearing_restores_candidates(api):
    for index, name in enumerate(("피자_치즈", "피자_불고기", "국밥_소고기", "햄버거_치즈")):
        api.db.add(Menu(food_code=f"dislike-{index}", name=name, base_name=name.split("_")[0],
                        category_large="식사", meal_type="main_meal", has_soup=False))
    source = next(q for q in build_candidate_question_pool() if q["cluster_key"] == "has_soup")
    question = OrderSheetQuestion(question_code=source["question_code"], cluster_key="has_soup",
                                 question_text=source["question_text"], options_json=source["options"],
                                 initial_score=1, importance_weight=1)
    api.db.add(question)
    api.db.commit()
    body = {"answers": [{"question_id": question.id, "option_id": "any"}]}
    assert api.client.put(URL, headers=api.headers, json={"keywords": ["피자"]}).status_code == 200
    result = api.client.post("/api/recommendations/order-sheet", headers=api.headers, json=body)
    assert result.status_code == 201
    data = result.json()["data"]
    assert data["excluded_count"] == 2
    assert {item["base_name"] for item in data["recommended_menus"]} == {"국밥", "햄버거"}
    assert api.client.put(URL, headers=api.headers, json={"keywords": []}).status_code == 200
    restored = api.client.post("/api/recommendations/order-sheet", headers=api.headers, json=body)
    assert restored.status_code == 201
    assert restored.json()["data"]["excluded_count"] == 0
    assert "피자" in {item["base_name"] for item in restored.json()["data"]["recommended_menus"]}
    api.client.put(URL, headers=api.headers, json={"keywords": ["피자", "국밥", "햄버거"]})
    empty = api.client.post("/api/recommendations/order-sheet", headers=api.headers, json=body)
    assert empty.status_code == 404
    assert empty.json()["success"] is False

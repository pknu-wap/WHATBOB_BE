import random

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.dependencies import get_optional_current_user
from app.core.database import Base, get_db
from app.menus.models import Menu, UserFoodExclusion
from app.recommend.common import NoAvailableFoodException
from app.recommend.random_pick import pick_random_menu
from app.recommend.router import router
from app.users.models import User


def make_menu(food_code: str, name: str, base_name: str, **overrides) -> Menu:
    values = {
        "food_code": food_code,
        "name": name,
        "base_name": base_name,
        "category_large": "외식",
        "category_medium": "식사",
        "origin_type": "외식",
        "meal_type": "main_meal",
        "has_soup": False,
        "cooking_style": "other",
        "main_ingredient": "other",
        "flavor_intensity": "mild_clean",
        "temperature": "hot",
        "heaviness": "moderate",
        "allergen_tags": [],
    }
    values.update(overrides)
    return Menu(**values)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(bind=engine)
    session = testing_session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_random_pick_chooses_type_before_menu(db):
    db.add_all([
        make_menu("pizza-1", "피자_치즈", "피자"),
        make_menu("pizza-2", "피자_페퍼로니", "피자"),
        make_menu("pizza-3", "피자_고구마", "피자"),
        make_menu("soup-1", "국밥_순대국밥", "국밥", has_soup=True),
    ])
    db.commit()

    # 삽입 순서상 마지막 종류인 국밥을 고른 뒤 그 종류의 실제 메뉴를 고른다.
    class LastChoiceRandom(random.Random):
        def choice(self, sequence):
            return sequence[-1]

    result = pick_random_menu(db, rng=LastChoiceRandom())

    assert result.menu.base_name == "국밥"
    assert result.candidate_count == 4
    assert result.menu_type_count == 2
    assert result.excluded_count == 0


def test_random_pick_groups_known_menu_name_aliases(db):
    db.add_all([
        make_menu("burger-1", "햄버거_치즈버거", "햄버거"),
        make_menu("burger-2", "버거_불고기버거", "버거"),
        make_menu("rice-1", "비빔밥_전주비빔밥", "비빔밥"),
    ])
    db.commit()

    result = pick_random_menu(db, rng=random.Random(1))

    assert result.candidate_count == 3
    assert result.menu_type_count == 2


def test_random_pick_applies_user_and_request_exclusions(db):
    user = User(kakao_id=777001, nickname="랜덤 사용자")
    db.add(user)
    db.flush()
    db.add(UserFoodExclusion(
        user_id=user.id,
        exclusion_type="ALLERGY",
        keyword="돼지고기",
    ))
    db.add_all([
        make_menu(
            "pork-1",
            "덮밥_돼지고기 덮밥",
            "돼지고기 덮밥",
            main_ingredient="pork",
            allergen_tags=["돼지고기"],
        ),
        make_menu("salad-1", "샐러드_채소 샐러드", "샐러드"),
        make_menu("noodle-1", "국수_잔치국수", "국수"),
    ])
    db.commit()

    result = pick_random_menu(
        db,
        user_id=user.id,
        extra_exclusions=["샐러드"],
        rng=random.Random(1),
    )

    assert result.menu.base_name == "국수"
    assert result.excluded_count == 2
    assert result.candidate_count == 1
    assert result.menu_type_count == 1


def test_random_pick_does_not_relax_exclusions_when_no_candidate_remains(db):
    db.add(make_menu("only-1", "국수_잔치국수", "국수"))
    db.commit()

    with pytest.raises(NoAvailableFoodException):
        pick_random_menu(db, extra_exclusions=["국수"])


def test_random_api_uses_authenticated_users_exclusions(db):
    user = User(kakao_id=777002, nickname="로그인 사용자")
    db.add(user)
    db.flush()
    db.add(UserFoodExclusion(
        user_id=user.id,
        exclusion_type="DISLIKE",
        keyword="피자",
    ))
    db.add_all([
        make_menu("pizza-1", "피자_치즈", "피자"),
        make_menu("rice-1", "비빔밥_전주", "비빔밥", kcal=500.0),
    ])
    db.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_optional_current_user] = lambda: user

    with TestClient(app) as client:
        response = client.post("/api/recommendations/random", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["selected_menu"]["base_name"] == "비빔밥"
    assert body["data"]["excluded_count"] == 1
    assert body["data"]["candidate_count"] == 1
    assert body["data"]["menu_type_count"] == 1

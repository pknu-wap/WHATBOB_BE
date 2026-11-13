from pathlib import Path
import csv

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.users.models  # noqa: F401
from app.core.database import Base, get_db
from app.menus.display import format_menu_name
from app.menus.models import Menu, OrderSheetQuestion, Recommendation
from app.users.models import User
from app.recommend.router import router


@pytest.mark.parametrize("original,expected", [
    ("김밥_김치", "김치김밥"),
    ("김밥_참치", "참치김밥"),
    ("볶음밥_계란", "계란볶음밥"),
    ("덮밥_불고기", "불고기덮밥"),
    ("김치찌개_돼지고기", "김치찌개 (돼지고기)"),
    ("국밥_순대국밥", "순대국밥"),
    ("김밥_참치 김밥 (대)", "참치 김밥 (대)"),
    ("닭튀김_땡초 치킨", "땡초 치킨"),
    ("닭튀김_마늘알마니 치킨", "마늘알마니 치킨"),
    ("스파게티_베이컨 크림 파스타", "베이컨 크림 파스타"),
    ("스프_단호박 크림 수프", "단호박 크림 수프"),
    ("돈가스_부타이로스카츠 (통등심돈가스)", "부타이로스카츠 (통등심돈가스)"),
    ("국밥_간편조리세트_부산식 돼지국밥", "부산식 돼지국밥"),
    ("돼지갈비찜_간편조리세트_양념갈비", "양념갈비"),
    ("두부찌개_소고기_된장", "두부찌개 (소고기·된장)"),
    ("갈비탕_소금제외", "갈비탕 (소금제외)"),
    ("김밥_김밥", "김밥"),
    ("김밥_", "김밥"),
    ("김밥_간편조리세트", "김밥"),
    ("  김밥 __ 김치  ", "김치김밥"),
    ("김치찌개", "김치찌개"),
    ("", ""),
    (" __ ", ""),
])
def test_format_menu_name(original, expected):
    assert format_menu_name(original) == expected
    assert format_menu_name(expected) == expected


def test_all_shared_menu_names_remain_nonempty_without_separators():
    path = Path(__file__).resolve().parents[1] / "data" / "menus_meals_only.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        for menu in csv.DictReader(stream):
            formatted = format_menu_name(menu["name"])
            assert formatted
            assert "_" not in formatted
            assert format_menu_name(formatted) == formatted


def test_recommendation_formats_names_without_changing_storage_or_exclusions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            menus = [Menu(
                food_code=f"display-{index}", name=name, base_name=base,
                category_large="식사", has_soup=False,
            ) for index, (name, base) in enumerate([
                ("김밥_김치", "김밥"), ("닭튀김_땡초 치킨", "닭튀김"),
            ])]
            question = OrderSheetQuestion(
                question_code="q_display_soup", cluster_key="has_soup",
                question_text="국물 요리가 좋나요?", initial_score=0.8, importance_weight=0.8,
                options_json=[
                    {"option_id": "soup_yes", "label": "국물 있음", "rules": [
                        {"field": "has_soup", "op": "eq", "value": True},
                    ]},
                    {"option_id": "soup_no", "label": "국물 없음", "rules": [
                        {"field": "has_soup", "op": "eq", "value": False},
                    ]},
                ],
            )
            user = User(kakao_id=702001, nickname="표시명테스터")
            db.add_all([user, *menus, question])
            db.commit()
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_db] = lambda: db
            from app.auth.dependencies import get_current_user
            app.dependency_overrides[get_current_user] = lambda: user
            body = {"answers": [{"question_id": question.id, "option_id": "soup_no"}]}
            with TestClient(app) as client:
                response = client.post("/api/recommendations/order-sheet", json=body)
                assert response.status_code == 201
                data = response.json()["data"]
                results = data["recommended_menus"]
                results = sorted(results, key=lambda item: item["menu_id"])
                assert [item["name"] for item in results] == ["김치김밥", "땡초 치킨"]
                assert [item["base_name"] for item in results] == ["김밥", "닭튀김"]
                assert [item["menu_id"] for item in results] == [m.id for m in menus]
                assert [item["food_code"] for item in results] == [m.food_code for m in menus]
                assert [item["match_score"] for item in results] == [0.8, 0.8]
                record = db.get(Recommendation, data["recommendation_id"])
                assert set(record.recommended_menu_ids) == {m.id for m in menus}
                feedback = client.post(f'/api/recommendations/{record.id}/feedback', json={
                    "selected_menu_id": menus[1].id, "is_satisfied": True,
                })
                assert feedback.status_code == 200
                # 표시명에서 빠진 원본 분류명도 기존과 같이 제외 조건에 적용한다.
                response = client.post("/api/recommendations/order-sheet", json={
                    **body, "exclusions": ["닭튀김"],
                })
                assert response.status_code == 201
                data = response.json()["data"]
                assert data["excluded_count"] == 1
                assert [m["name"] for m in data["recommended_menus"]] == ["김치김밥"]
            for menu, original in zip(menus, ["김밥_김치", "닭튀김_땡초 치킨"]):
                db.refresh(menu)
                assert menu.name == original
    finally:
        engine.dispose()

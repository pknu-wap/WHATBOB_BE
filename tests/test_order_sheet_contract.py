import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.menus.models import OrderSheetQuestion
from app.recommend.router import router


@pytest.mark.parametrize("weight_key, recalculate_key", [
    ("ml_weight", "run_ml_retrain"),
    ("importance_weight", "recalculate_from_feedback"),
])
def test_weight_names_preserve_http_and_database_compatibility(weight_key, recalculate_key):
    """내부 이름 변경 후에도 기존 요청·응답 키와 DB 컬럼을 사용할 수 있다."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    OrderSheetQuestion.__table__.create(engine)
    try:
        with Session(engine) as db:
            question = OrderSheetQuestion(
                question_code="q_soup_contract",
                cluster_key="has_soup",
                question_text="국물 있는 음식이 좋은가요?",
                options_json=[
                    {"option_id": "soup_yes", "label": "국물 있음", "rules": [
                        {"field": "has_soup", "op": "eq", "value": True},
                    ]},
                    {"option_id": "soup_no", "label": "국물 없음", "rules": [
                        {"field": "has_soup", "op": "eq", "value": False},
                    ]},
                ],
                initial_score=0.8,
                importance_weight=0.7,
            )
            db.add(question)
            db.commit()
            question_id = question.id

            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_db] = lambda: db
            with TestClient(app) as client:
                response = client.post("/api/recommendations/order-sheet/weights", json={
                    "adjustments": [{"question_id": question_id, weight_key: 1.25}],
                    recalculate_key: False,
                })
                assert response.status_code == 200
                assert response.json()["data"]["updated_count"] == 1

                response = client.get("/api/recommendations/order-sheet/questions")
                assert response.status_code == 200
                result = response.json()["data"]["questions"][0]
                assert result["ml_weight"] == 1.25
                assert result["info_gain_score"] == 0.8
                assert "importance_weight" not in result
                assert "initial_score" not in result

            stored = db.execute(text(
                "SELECT ml_weight, info_gain_score FROM order_sheet_questions WHERE id = :id"
            ), {"id": question_id}).one()
            assert tuple(stored) == (1.25, 0.8)
    finally:
        engine.dispose()

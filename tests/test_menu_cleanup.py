from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.auth.models import RequestOperation
from app.core.database import Base
from app.menus.models import Menu, OrderSheetQuestion, Recommendation, RecommendationFeedback, UserMeal
from app.recommend.common import InvalidOrderSheetException
from app.recommend.order_sheet import record_recommendation_feedback
from app.recommend.schemas import RecommendationFeedbackRequest
from app.rooms.models import Room, RoomNotification, RoomRound, RoomRoundCandidate, RoomRoundMember
from app.users.models import User
from scripts.remove_side_dishes_and_desserts import clean_menu_records
from scripts.seed_menus import parse_and_clean_csv


REMOVED_CODE = "D110-472000000-0001"
RETAINED_CODE = "D106-266050000-0001"


def test_legacy_csv_cannot_restore_removed_food_code(tmp_path):
    source = tmp_path / "legacy.csv"
    source.write_text(
        "식품코드,식품명,식품대분류명\n"
        f"{REMOVED_CODE},멸치볶음,볶음류\n"
        "D514-607323300-0001,골뱅이무침_채소_소면,생채·무침류\n"
        f"{RETAINED_CODE},김치찌개_꽁치,찌개 및 전골류\n",
        encoding="utf-8",
    )

    for meals_only in (True, False):
        menus = parse_and_clean_csv(source, include_beverages_and_bakery=not meals_only)
        assert [menu["food_code"] for menu in menus] == [RETAINED_CODE]


def test_cleanup_preserves_other_menus_in_the_same_records():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    payload = {"current_round": {
        "candidates": [{"menu_id": 1}, {"menu_id": 2}], "winners": [{"menu_id": 1}],
    }}

    with engine.begin() as connection:
        connection.execute(User.__table__.insert().values(id=1))
        connection.execute(Menu.__table__.insert(), [
            dict(id=1, food_code=REMOVED_CODE, name="멸치볶음", base_name="멸치볶음", category_large="볶음류"),
            dict(id=2, food_code=RETAINED_CODE, name="김치찌개", base_name="김치찌개", category_large="찌개 및 전골류"),
        ])
        connection.execute(UserMeal.__table__.insert(), [
            dict(id=1, user_id=1, menu_id=1, menu_name="멸치볶음"),
            dict(id=2, user_id=1, menu_id=2, menu_name="김치찌개"),
        ])
        connection.execute(OrderSheetQuestion.__table__.insert(), [
            dict(id=1, question_code="mixed", cluster_key="test", question_text="혼합",
                 options_json=[], info_gain_score=0.2, ml_weight=0.8, positive_count=2),
            dict(id=2, question_code="removed", cluster_key="test", question_text="삭제",
                 options_json=[], info_gain_score=0.3, ml_weight=0.9, positive_count=1),
            dict(id=3, question_code="unchanged", cluster_key="test", question_text="유지",
                 options_json=[], info_gain_score=0.4, ml_weight=0.5, positive_count=0),
        ])
        connection.execute(Recommendation.__table__.insert(), [
            dict(id=1, user_id=1, recommended_menu_ids=[1, 2]),
            dict(id=2, user_id=1, recommended_menu_ids=[1]),
            dict(id=3, user_id=1, recommended_menu_ids=[2]),
        ])
        connection.execute(RecommendationFeedback.__table__.insert(), [
            dict(id=1, recommendation_id=1, user_id=1, selected_menu_id=1,
                 is_satisfied=True, feature_match_vector={"1": 1.0, "2": 1.0}),
            dict(id=2, recommendation_id=1, user_id=1, selected_menu_id=2,
                 is_satisfied=True, feature_match_vector={"1": 1.0}),
        ])
        connection.execute(RequestOperation.__table__.insert(), [
            dict(scope="USER_MEAL_CREATE", operation_key=str(meal_id), resource_id=str(meal_id),
                 user_id=1, payload_hash="hash", created_at=now, updated_at=now)
            for meal_id in (1, 2)
        ])
        connection.execute(Room.__table__.insert().values(id=1, host_user_id=1))
        connection.execute(RoomRound.__table__.insert().values(id=1, room_id=1, status="VOTING"))
        connection.execute(RoomRoundCandidate.__table__.insert(), [
            dict(id=1, round_id=1, menu_id=1), dict(id=2, round_id=1, menu_id=2),
        ])
        connection.execute(RoomRoundMember.__table__.insert().values(
            round_id=1, user_id=1, role="HOST", top_menu_id=1, voted_candidate_ids=[1, 2],
        ))
        connection.execute(RoomNotification.__table__.insert().values(
            room_id=1, historical_room_id=1, historical_public_id="test", state_version=1,
            created_at=now, payload_json={**payload, "common_state": payload},
        ))

    with engine.begin() as connection:
        preview = clean_menu_records(connection, {REMOVED_CODE})
        assert preview["menus"] == preview["user_meals"] == 1
        assert preview["affected_active_rounds"] == [1]
        assert preview["questions_with_removed_feedback"] == 2
        assert connection.scalar(select(OrderSheetQuestion.positive_count).where(OrderSheetQuestion.id == 2)) == 1

    with pytest.raises(ValueError, match="진행 중 라운드"), engine.begin() as connection:
        clean_menu_records(connection, {REMOVED_CODE}, apply=True)

    with engine.begin() as connection:
        assert list(connection.scalars(select(Menu.id).order_by(Menu.id))) == [1, 2]
        connection.execute(RoomRound.__table__.update().values(status="RESULT"))
        assert clean_menu_records(connection, {REMOVED_CODE}, apply=True)["menus"] == 1
        assert list(connection.scalars(select(Menu.id))) == [2]
        assert list(connection.scalars(select(UserMeal.id))) == [2]
        assert list(connection.scalars(select(RecommendationFeedback.id))) == [2]
        assert list(connection.scalars(select(RequestOperation.resource_id))) == ["2"]
        assert list(connection.scalars(select(Recommendation.recommended_menu_ids).order_by(Recommendation.id))) == [
            [None, 2], [], [2],
        ]
        weights = connection.execute(select(
            OrderSheetQuestion.positive_count, OrderSheetQuestion.importance_weight,
        ).order_by(OrderSheetQuestion.id)).all()
        mixed_count, mixed_weight = weights[0]
        assert mixed_count == 1
        assert mixed_weight < 0.8
        assert weights[1:] == [(0, 0.3), (0, 0.5)]
        assert list(connection.scalars(select(RoomRoundCandidate.id))) == [2]
        assert connection.execute(select(RoomRoundMember.top_menu_id, RoomRoundMember.voted_candidate_ids)).one() == (None, [2])
        state = {"current_round": {"candidates": [{"menu_id": 2}], "winners": []}}
        assert connection.scalar(select(RoomNotification.payload_json)) == {**state, "common_state": state}
        assert clean_menu_records(connection, {REMOVED_CODE}, apply=True) == {"menus": 0}
        with Session(bind=connection) as db:
            implicit_feedback = RecommendationFeedbackRequest(is_satisfied=True)
            for recommendation_id in (1, 2):
                with pytest.raises(InvalidOrderSheetException):
                    record_recommendation_feedback(db, recommendation_id, implicit_feedback, user_id=1)
            explicit_feedback = RecommendationFeedbackRequest(is_satisfied=True, selected_menu_id=2)
            response = record_recommendation_feedback(db, 1, explicit_feedback, user_id=1)
            assert db.get(RecommendationFeedback, response.feedback_id).selected_menu_id == 2
            response = record_recommendation_feedback(db, 3, implicit_feedback, user_id=1)
            assert db.get(RecommendationFeedback, response.feedback_id).selected_menu_id == 2

    engine.dispose()

from sqlalchemy import create_engine, inspect, text

from scripts.migrate_order_sheet import migrate_order_sheet, pending_order_sheet_schema


def test_order_sheet_migration_is_additive_and_resumable():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for table in (
            "users",
            "food_kinds",
            "recommendations",
            "recommendation_feedbacks",
            "recommendation_feedback_states",
        ):
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
            connection.execute(text(f"INSERT INTO {table} (id) VALUES (42)"))

    with engine.begin() as connection:
        pending = pending_order_sheet_schema(connection)
        assert "recommendations.initial_candidate_count" in pending
        assert "order_sheet_issuances" in pending
        assert "recommendation_feedbacks.food_kind_id" in pending
        migrate_order_sheet(connection)
        assert pending_order_sheet_schema(connection) == []
        assert migrate_order_sheet(connection) == []
        assert connection.execute(text("SELECT id FROM recommendations")).scalar_one() == 42
        assert connection.execute(text("SELECT id FROM recommendation_feedbacks")).scalar_one() == 42
        assert inspect(connection).get_foreign_keys("recommendation_feedbacks")[0]["referred_table"] == "food_kinds"

from sqlalchemy import create_engine, inspect, text

from scripts.migrate_order_sheet import migrate_order_sheet, pending_order_sheet_schema
from tests.test_issue39_mysql_concurrency import mysql_env  # noqa: F401


def assert_legacy_schema_upgrades(engine):
    with engine.begin() as connection:
        for table in (
            "users",
            "food_kinds",
            "menus",
            "recommendations",
            "recommendation_feedbacks",
            "recommendation_feedback_states",
        ):
            legacy_columns = {
                "recommendations": ", user_id INTEGER, created_at DATETIME",
                "recommendation_feedbacks": ", created_at DATETIME",
            }.get(table, "")
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY{legacy_columns})"))
            connection.execute(text(f"INSERT INTO {table} (id) VALUES (42)"))

    with engine.begin() as connection:
        pending = pending_order_sheet_schema(connection)
        assert "recommendations.initial_candidate_count" in pending
        assert "recommendations.order_sheet_list_scope" in pending
        assert "order_sheet_issuances" in pending
        assert "order_sheet_replay_snapshots" in pending
        assert "order_sheet_question_reviews" in pending
        assert "personal_order_sheet_question_assignments" in pending
        assert "common_order_sheet_question_experiments" in pending
        assert "common_order_sheet_question_assignments" in pending
        assert "recommendation_feedbacks.food_kind_id" in pending
        migrate_order_sheet(connection)
        assert pending_order_sheet_schema(connection) == []
        assert migrate_order_sheet(connection) == []
        assert connection.execute(text("SELECT id FROM recommendations")).scalar_one() == 42
        assert connection.execute(text("SELECT id FROM recommendation_feedbacks")).scalar_one() == 42
        assert inspect(connection).get_foreign_keys("recommendation_feedbacks")[0]["referred_table"] == "food_kinds"


def test_order_sheet_migration_is_additive_and_resumable():
    engine = create_engine("sqlite://")
    try:
        assert_legacy_schema_upgrades(engine)
    finally:
        engine.dispose()


def test_mysql_order_sheet_migration_preserves_legacy_records(mysql_env):
    engine = mysql_env["engine"]
    assert_legacy_schema_upgrades(engine)
    with engine.connect() as connection:
        for table in ("recommendations", "recommendation_feedbacks", "order_sheet_issuances"):
            timestamps = {column["name"]: column["type"] for column in inspect(connection).get_columns(table)}
            assert timestamps["created_at"].fsp == 6

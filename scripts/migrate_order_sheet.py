"""Add the #77 order-sheet schema to an existing database."""

import argparse
import json

from sqlalchemy import inspect, text

import app.map.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.users.models  # noqa: F401
from app.core.database import Base, engine


ADDED_COLUMNS = {
    "recommendations": (
        "initial_candidate_count", "list_rejected_at", "closed_at", "order_sheet_list_scope",
    ),
    "recommendation_feedbacks": ("food_kind_id", "exposure_rank"),
    "recommendation_feedback_states": ("food_kind_id", "exposure_rank"),
}
ADDED_TABLES = (
    "order_sheet_issuances",
    "order_sheet_hard_filter_traces",
    "order_sheet_replay_snapshots",
    "order_sheet_question_reviews",
    "place_selections",
    "place_satisfactions",
)


def _missing_schema(connection):
    inspector = inspect(connection)
    for table_name, names in ADDED_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for name in names:
            if name not in existing:
                column = table.c[name]
                column_type = column.type.compile(dialect=connection.dialect)
                definition = f"{name} {column_type} NULL"
                if name == "food_kind_id" and connection.dialect.name == "sqlite":
                    definition += " REFERENCES food_kinds (id) ON DELETE SET NULL"
                yield f"{table_name}.{name}", text(f"ALTER TABLE {table_name} ADD COLUMN {definition}")

        if "food_kind_id" in names and connection.dialect.name in {"mysql", "mariadb"}:
            keys = inspector.get_foreign_keys(table_name)
            if not any(key["constrained_columns"] == ["food_kind_id"] for key in keys):
                name = f"fk_{table_name}_food_kind"
                yield name, text(
                    f"ALTER TABLE {table_name} ADD CONSTRAINT {name} FOREIGN KEY (food_kind_id) "
                    "REFERENCES food_kinds (id) ON DELETE SET NULL"
                )

        indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        for index in sorted(table.indexes, key=lambda item: item.name):
            if any(column.name in names for column in index.columns):
                if index.name not in indexes:
                    yield index.name, index

    existing_tables = set(inspector.get_table_names())
    for name in ADDED_TABLES:
        if name not in existing_tables:
            yield name, Base.metadata.tables[name]


def pending_order_sheet_schema(connection) -> list[str]:
    return [name for name, _ in _missing_schema(connection)]


def migrate_order_sheet(connection) -> list[str]:
    changes = list(_missing_schema(connection))
    for _, operation in changes:
        if hasattr(operation, "create"):
            operation.create(connection)
        else:
            connection.execute(operation)
    return [name for name, _ in changes]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    with engine.begin() as connection:
        applied = migrate_order_sheet(connection) if args.apply else []
        pending = pending_order_sheet_schema(connection)
    print(json.dumps({"applied": applied, "pending": pending}))
    if pending and (args.apply or args.verify):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

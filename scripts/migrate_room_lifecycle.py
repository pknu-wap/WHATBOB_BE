"""Add room and member lifecycle columns to an existing MySQL database.

Run once before deploying the room deletion feature:
    python -m scripts.migrate_room_lifecycle
"""

from sqlalchemy import inspect, text

from app.core.database import engine


def migrate_room_lifecycle() -> None:
    if engine.dialect.name != "mysql":
        raise RuntimeError("This migration supports the project's MySQL database only.")

    inspector = inspect(engine)
    if "rooms" not in inspector.get_table_names():
        raise RuntimeError("rooms table does not exist. Start the application first.")
    if "room_members" not in inspector.get_table_names():
        raise RuntimeError("room_members table does not exist. Start the application first.")

    columns = {column["name"] for column in inspector.get_columns("rooms")}
    with engine.begin() as connection:
        if "expires_at" not in columns:
            connection.execute(text("ALTER TABLE rooms ADD COLUMN expires_at DATETIME NULL"))
            connection.execute(text(
                "UPDATE rooms SET expires_at = DATE_ADD(created_at, INTERVAL 1 HOUR) "
                "WHERE expires_at IS NULL"
            ))
            connection.execute(text(
                "ALTER TABLE rooms MODIFY COLUMN expires_at DATETIME NOT NULL"
            ))
        if "closed_at" not in columns:
            connection.execute(text("ALTER TABLE rooms ADD COLUMN closed_at DATETIME NULL"))
        if "close_reason" not in columns:
            connection.execute(text(
                "ALTER TABLE rooms ADD COLUMN close_reason VARCHAR(30) NULL"
            ))
        if "host_disconnected_at" not in columns:
            connection.execute(text(
                "ALTER TABLE rooms ADD COLUMN host_disconnected_at DATETIME NULL"
            ))

    index_names = {index["name"] for index in inspect(engine).get_indexes("rooms")}
    if "ix_rooms_expires_at" not in index_names:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE INDEX ix_rooms_expires_at ON rooms (expires_at)"
            ))

    member_columns = {
        column["name"] for column in inspect(engine).get_columns("room_members")
    }
    with engine.begin() as connection:
        if "is_online" not in member_columns:
            connection.execute(text(
                "ALTER TABLE room_members "
                "ADD COLUMN is_online BOOLEAN NOT NULL DEFAULT FALSE"
            ))
        if "disconnected_at" not in member_columns:
            connection.execute(text(
                "ALTER TABLE room_members ADD COLUMN disconnected_at DATETIME NULL"
            ))


if __name__ == "__main__":
    migrate_room_lifecycle()
    print("Room lifecycle migration completed.")

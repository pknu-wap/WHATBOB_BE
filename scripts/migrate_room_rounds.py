"""Explicit, additive, resumable MySQL 8 migration for #43 / #36 (T01N ~ T07).

Stop ALL writers and keep a restorable backup before --apply
--maintenance-confirmed. Never run automatically from API startup. Preserve the
manifest across interruptions; MySQL DDL commits independently of transactions.
"""
import argparse
import json
from pathlib import Path

from sqlalchemy import DateTime, Integer, String, inspect, text
from sqlalchemy.exc import SQLAlchemyError

import app.menus.models  # noqa: F401
import app.users.models  # noqa: F401
from app.core.database import engine
from app.rooms.models import (
    RoomNotification,
    RoomRound,
    RoomRoundCandidate,
    RoomRoundCandidateProposer,
    RoomRoundMember,
    RoomRoundSearchOrigin,
)
from scripts import migrate_room_notifications as notif_migration
from scripts.migrate_uuid_auth import (
    MigrationError,
    _check_index,
    _require,
    table_fingerprint,
)

ROUND_TABLES = (
    RoomRound.__table__,
    RoomRoundMember.__table__,
    RoomRoundCandidate.__table__,
    RoomRoundCandidateProposer.__table__,
    RoomRoundSearchOrigin.__table__,
)
EXCLUDED_TABLES = {"room_notifications"} | {t.name for t in ROUND_TABLES}


def validate_schema(connection, *, final: bool = False) -> None:
    notif_migration.validate_schema(connection, final=final)
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())

    for tbl in ROUND_TABLES:
        if tbl.name not in existing_tables:
            _require(not final, f"Missing {tbl.name}.")
            continue
        _require(
            inspector.get_table_options(tbl.name).get("mysql_engine", "").lower() == "innodb",
            f"{tbl.name} must use InnoDB.",
        )
        columns = {c["name"]: c for c in inspector.get_columns(tbl.name)}
        for expected in tbl.columns:
            actual = columns.get(expected.name)
            _require(actual is not None, f"Missing {tbl.name}.{expected.name}.")
            type_ = expected.type
            _require(
                actual["nullable"] == expected.nullable,
                f"Incompatible nullability for {tbl.name}.{expected.name}.",
            )
            if isinstance(type_, String):
                _require(
                    actual["type"].length == type_.length,
                    f"Incompatible length for {tbl.name}.{expected.name}.",
                )
            if isinstance(type_, DateTime):
                _require(
                    getattr(actual["type"], "fsp", None) == 6,
                    f"{tbl.name}.{expected.name} requires DATETIME(6).",
                )
            if isinstance(type_, Integer):
                _require(
                    not getattr(actual["type"], "unsigned", False),
                    f"Unsigned {tbl.name}.{expected.name} is unsupported.",
                )
        for idx in tbl.indexes:
            _check_index(
                connection,
                tbl.name,
                idx.name,
                [c.name for c in idx.columns],
                unique=bool(idx.unique),
                required=final,
            )


def create_manifest(connection) -> dict:
    tables = {}
    inspector = inspect(connection)
    for table in inspector.get_table_names():
        if table in EXCLUDED_TABLES:
            continue
        columns = [
            c["name"]
            for c in inspector.get_columns(table)
            if not (table == "rooms" and c["name"] == "state_version")
        ]
        tables[table] = dict(columns=columns, **table_fingerprint(connection, table, columns))
    return dict(version=1, database=connection.engine.url.database, tables=tables)


def verify_preservation(connection, manifest: dict) -> None:
    notif_migration.verify_preservation(connection, manifest)


def apply_migration(connection, manifest: dict) -> None:
    notif_migration.apply_migration(connection, manifest)
    for tbl in ROUND_TABLES:
        tbl.create(connection, checkfirst=True)
        connection.commit()
        indexes = {i["name"] for i in inspect(connection).get_indexes(tbl.name)}
        for idx in tbl.indexes:
            if idx.name not in indexes:
                idx.create(connection)
                connection.commit()
    validate_schema(connection, final=True)
    verify_preservation(connection, manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("check", "apply", "verify"):
        modes.add_argument("--" + mode, action="store_true")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(".tools/migrations/room-rounds.json"),
    )
    args = parser.parse_args()
    if args.apply and not args.maintenance_confirmed:
        parser.error(
            "--apply requires --maintenance-confirmed after blocking writers and verifying backup"
        )
    try:
        with engine.connect() as connection:
            validate_schema(connection, final=args.verify)
            if args.check:
                print(json.dumps(dict(stage="check", result="passed")))
                return
            if args.manifest.exists():
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            else:
                _require(args.apply, "Preservation manifest is required for --verify.")
                manifest = create_manifest(connection)
                args.manifest.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.manifest.with_suffix(".tmp")
                temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                temporary.replace(args.manifest)
            if args.apply:
                apply_migration(connection, manifest)
            else:
                verify_preservation(connection, manifest)
            print(json.dumps(dict(stage="apply" if args.apply else "verify", result="passed")))
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (SQLAlchemyError, OSError, ValueError):
        raise SystemExit(
            "Migration failed. Keep writers blocked; inspect schema, manifest and backup before resuming."
        ) from None


if __name__ == "__main__":
    main()

"""Explicit, resumable MySQL migration. Never called by application startup.

Run check, then apply during a write-blocked maintenance window, then verify.
The local manifest contains hashes/counts, not profile or credential plaintext.
It supplements an operator's database backup; it is not itself a backup.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import inspect, text
from sqlalchemy.dialects.mysql import BIGINT, BINARY, CHAR, DATETIME, INTEGER, TINYINT, VARBINARY
from sqlalchemy.exc import SQLAlchemyError

from app.auth.models import AuthSession
from app.core.database import engine
import app.users.models  # noqa: F401 — register users before creating its referencing table

ADDED_USER_COLUMNS = {"uuid", "nickname_key", "is_legacy_kakao"}
MANIFEST_VERSION = 1


class MigrationError(RuntimeError):
    pass


def _require(condition: bool, message: str):
    if not condition:
        raise MigrationError(message)


def _columns(connection, table):
    return {column["name"]: column for column in inspect(connection).get_columns(table)}


def _indexes(connection, table):
    inspector = inspect(connection)
    return {index["name"]: index for index in inspector.get_indexes(table)}


def _check_index(connection, table, name, columns, *, unique=False, required=False):
    index = _indexes(connection, table).get(name)
    if index is None:
        _require(not required, f"Missing required index: {table}.{name}")
    else:
        _require(index["column_names"] == columns and bool(index["unique"]) == unique,
                 f"Incompatible index: {table}.{name}")


def validate_schema(connection, *, final=False):
    _require(connection.dialect.name == "mysql", "Only MySQL is supported.")
    version = connection.dialect.server_version_info
    _require(not getattr(connection.dialect, "is_mariadb", False) and version >= (8, 0, 16),
             "MySQL 8.0.16 or later is required; MariaDB is not validated.")
    inspector = inspect(connection)
    _require("users" in inspector.get_table_names(), "The existing users table is required.")
    options = inspector.get_table_options("users")
    _require(options.get("mysql_engine", "").lower() == "innodb", "users must use InnoDB.")
    columns = _columns(connection, "users")
    _require(set(["id", "kakao_id", "nickname", "email", "profile_image", "created_at", "updated_at"]) <= columns.keys(),
             "The existing users schema is incompatible.")
    _require(isinstance(columns["id"]["type"], INTEGER) and not columns["id"]["type"].unsigned,
             "users.id must be a signed INTEGER.")
    _require(inspector.get_pk_constraint("users")["constrained_columns"] == ["id"], "Incompatible users primary key.")
    _require(isinstance(columns["kakao_id"]["type"], BIGINT) and not columns["kakao_id"]["type"].unsigned,
             "users.kakao_id must be a signed BIGINT.")
    unique_sets = [i["column_names"] for i in inspector.get_indexes("users") if i["unique"]]
    _require(["kakao_id"] in unique_sets, "The existing unique Kakao identifier index is required.")
    if final:
        _require(columns["kakao_id"]["nullable"], "users.kakao_id must allow NULL.")
    for name in ADDED_USER_COLUMNS:
        if name not in columns:
            _require(not final, f"Missing users.{name}")
            continue
        column = columns[name]
        type_ = column["type"]
        if name == "uuid":
            _require(isinstance(type_, CHAR) and type_.length == 36 and type_.charset == "ascii",
                     "Incompatible users.uuid; expected CHAR(36) CHARACTER SET ascii.")
            if final:
                _require(not column["nullable"], "users.uuid must be NOT NULL.")
        elif name == "nickname_key":
            _require(isinstance(type_, VARBINARY) and type_.length == 400 and column["nullable"],
                     "Incompatible users.nickname_key.")
        else:
            _require(isinstance(type_, TINYINT) and type_.display_width == 1,
                     "Incompatible users.is_legacy_kakao.")
            if final:
                _require(not column["nullable"], "users.is_legacy_kakao must be NOT NULL.")
    _check_index(connection, "users", "uq_users_uuid", ["uuid"], unique=True, required=final)
    _check_index(connection, "users", "uq_users_nickname_key", ["nickname_key"], unique=True, required=final)
    if "auth_sessions" in inspector.get_table_names():
        _validate_auth_table(connection, final=final)
    else:
        _require(not final, "Missing auth_sessions table.")


def _validate_auth_table(connection, *, final):
    inspector = inspect(connection)
    _require(inspector.get_table_options("auth_sessions").get("mysql_engine", "").lower() == "innodb",
             "auth_sessions must use InnoDB.")
    columns = _columns(connection, "auth_sessions")
    _require(set(columns) == {"id", "user_id", "secret_hash", "created_at", "expires_at", "revoked_at"},
             "Incompatible auth_sessions columns.")
    _require(isinstance(columns["id"]["type"], BIGINT) and not columns["id"]["type"].unsigned
             and inspector.get_pk_constraint("auth_sessions")["constrained_columns"] == ["id"]
             and columns["id"].get("autoincrement"), "Incompatible auth_sessions primary key.")
    _require(isinstance(columns["user_id"]["type"], INTEGER) and not columns["user_id"]["type"].unsigned
             and columns["user_id"]["nullable"], "Incompatible auth_sessions.user_id.")
    _require(isinstance(columns["secret_hash"]["type"], BINARY) and columns["secret_hash"]["type"].length == 32
             and not columns["secret_hash"]["nullable"], "Incompatible auth_sessions.secret_hash.")
    for name in ("created_at", "expires_at", "revoked_at"):
        _require(isinstance(columns[name]["type"], DATETIME) and columns[name]["type"].fsp == 6
                 and columns[name]["nullable"] == (name == "revoked_at"), f"Incompatible auth_sessions.{name}.")
    fks = inspector.get_foreign_keys("auth_sessions")
    _require(len(fks) == 1 and fks[0]["name"] == "fk_auth_sessions_user"
             and fks[0]["constrained_columns"] == ["user_id"] and fks[0]["referred_table"] == "users"
             and fks[0]["referred_columns"] == ["id"]
             and fks[0].get("options", {}).get("ondelete", "RESTRICT").upper() in {"RESTRICT", "NO ACTION"},
             "Incompatible auth_sessions foreign key.")
    for name, keys, unique in [
        ("uq_auth_sessions_secret_hash", ["secret_hash"], True),
        ("ix_auth_sessions_user_id", ["user_id"], False),
        ("ix_auth_sessions_expires_id", ["expires_at", "id"], False),
        ("ix_auth_sessions_revoked_id", ["revoked_at", "id"], False),
    ]:
        _check_index(connection, "auth_sessions", name, keys, unique=unique, required=final)


def inspect_user_data(connection, *, final=False):
    columns = _columns(connection, "users")
    selected = ["id", "nickname"] + sorted(ADDED_USER_COLUMNS & columns.keys())
    rows = connection.execute(text("SELECT " + ", ".join(f"`{name}`" for name in selected) + " FROM users")).mappings().all()
    nickname_keys, uuids = [], []
    for row in rows:
        nickname = row["nickname"]
        expected_key = nickname.strip().encode("utf-8") if nickname and nickname.strip() else None
        if expected_key is not None:
            _require(len(expected_key) <= 400, "An existing nickname exceeds the key capacity.")
            nickname_keys.append(expected_key)
        if "nickname_key" in row:
            _require(row["nickname_key"] in (None, expected_key), "An existing nickname_key disagrees with its nickname.")
            if final:
                _require(row["nickname_key"] == expected_key, "An existing nickname_key has not been filled.")
        value = row.get("uuid")
        if value is not None:
            try:
                parsed = UUID(value)
                _require(parsed.version == 4 and str(parsed) == value, "An existing UUID is not canonical UUIDv4.")
            except (ValueError, TypeError, AttributeError):
                raise MigrationError("An existing UUID is invalid.") from None
            uuids.append(value)
        elif final:
            raise MigrationError("A user UUID has not been filled.")
        if final:
            _require(row.get("is_legacy_kakao") is not None, "Legacy markers have not been filled.")
    _require(all(count == 1 for count in Counter(nickname_keys).values()),
             "Existing trimmed nicknames collide. Resolve explicitly before applying; no names were changed.")
    _require(len(set(uuids)) == len(uuids), "Existing UUIDs collide.")
    return len(rows)


def inspect_references(connection):
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] != "users":
                continue
            _require(fk["referred_columns"] == ["id"] and len(fk["constrained_columns"]) == 1,
                     "An unexpected users reference requires manual review.")
            column = quote(fk["constrained_columns"][0])
            query = (f"SELECT COUNT(*) FROM {quote(table)} child LEFT JOIN users parent ON child.{column}=parent.id "
                     f"WHERE child.{column} IS NOT NULL AND parent.id IS NULL")
            _require(connection.execute(text(query)).scalar_one() == 0, "An orphaned user reference was found.")


def table_fingerprint(connection, table, columns):
    quote = connection.dialect.identifier_preparer.quote
    result = connection.execute(text(f"SELECT {', '.join(map(quote, columns))} FROM {quote(table)}"))
    row_hashes = []
    for row in result:
        encoded = json.dumps(list(row), default=lambda value: value.hex() if isinstance(value, bytes) else str(value),
                             ensure_ascii=True, separators=(",", ":")).encode()
        row_hashes.append(hashlib.sha256(encoded).digest())
    return {"count": len(row_hashes), "digest": hashlib.sha256(b"".join(sorted(row_hashes))).hexdigest()}


def create_manifest(connection):
    inspector = inspect(connection)
    tables = {}
    for table in inspector.get_table_names():
        if table == "auth_sessions":
            continue
        columns = sorted(_columns(connection, table))
        if table == "users":
            columns = sorted(set(columns) - ADDED_USER_COLUMNS)
        tables[table] = {"columns": columns, **table_fingerprint(connection, table, columns)}
    return {"version": MANIFEST_VERSION, "database": engine.url.database, "tables": tables,
            "legacy_user_ids": list(connection.execute(text("SELECT id FROM users WHERE kakao_id IS NOT NULL")).scalars())
            if "is_legacy_kakao" not in _columns(connection, "users") else []}


def verify_preservation(connection, manifest):
    _require(manifest.get("version") == MANIFEST_VERSION and manifest.get("database") == engine.url.database,
             "The manifest does not match this database.")
    for table, baseline in manifest["tables"].items():
        _require(table in inspect(connection).get_table_names(), "A baseline table is missing.")
        actual = table_fingerprint(connection, table, baseline["columns"])
        _require(actual == {key: baseline[key] for key in ("count", "digest")},
                 f"Existing data changed in {table}; keep maintenance enabled and investigate.")


def apply_migration(connection, manifest):
    verify_preservation(connection, manifest)
    columns = _columns(connection, "users")
    additions = {
        "uuid": "CHAR(36) CHARACTER SET ascii NULL",
        "nickname_key": "VARBINARY(400) NULL",
        "is_legacy_kakao": "BOOLEAN NULL DEFAULT NULL",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))
            connection.commit()
    # Preserve old timestamps even on schemas that use ON UPDATE CURRENT_TIMESTAMP.
    rows = connection.execute(text("SELECT id, nickname, uuid, nickname_key FROM users")).mappings().all()
    for row in rows:
        key = row["nickname"].strip().encode("utf-8") if row["nickname"] and row["nickname"].strip() else None
        connection.execute(text("UPDATE users SET uuid=:uuid, nickname_key=:key, updated_at=updated_at WHERE id=:id"),
                           {"uuid": row["uuid"] or str(uuid4()), "key": key, "id": row["id"]})
    for user_id in manifest["legacy_user_ids"]:
        connection.execute(text("UPDATE users SET is_legacy_kakao=TRUE, updated_at=updated_at WHERE id=:id"), {"id": user_id})
    connection.execute(text("UPDATE users SET is_legacy_kakao=FALSE, updated_at=updated_at WHERE is_legacy_kakao IS NULL"))
    connection.commit()
    inspect_user_data(connection, final=True)
    verify_preservation(connection, manifest)
    columns = _columns(connection, "users")
    if columns["uuid"]["nullable"]:
        connection.execute(text("ALTER TABLE users MODIFY COLUMN uuid CHAR(36) CHARACTER SET ascii NOT NULL"))
    if not columns["kakao_id"]["nullable"]:
        connection.execute(text("ALTER TABLE users MODIFY COLUMN kakao_id BIGINT NULL"))
    if columns["is_legacy_kakao"]["nullable"] or str(columns["is_legacy_kakao"]["default"]) not in {"0", "'0'"}:
        connection.execute(text("ALTER TABLE users MODIFY COLUMN is_legacy_kakao BOOLEAN NOT NULL DEFAULT FALSE"))
    connection.commit()
    for name, column in [("uq_users_uuid", "uuid"), ("uq_users_nickname_key", "nickname_key")]:
        if name not in _indexes(connection, "users"):
            connection.execute(text(f"CREATE UNIQUE INDEX {name} ON users ({column})"))
            connection.commit()
    if "auth_sessions" not in inspect(connection).get_table_names():
        AuthSession.__table__.create(connection)
        connection.commit()
    for index in sorted(AuthSession.__table__.indexes, key=lambda index: index.name):
        if index.name not in _indexes(connection, "auth_sessions"):
            index.create(connection)
            connection.commit()
    validate_schema(connection, final=True)
    inspect_references(connection)
    verify_preservation(connection, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("check", "apply", "verify"):
        modes.add_argument(f"--{mode}", action="store_true")
    parser.add_argument("--manifest", type=Path, default=Path(".tools/migrations/uuid-auth.json"))
    parser.add_argument("--maintenance-confirmed", action="store_true",
                        help="Confirm all writes are blocked and a restorable DB backup is available.")
    args = parser.parse_args()
    if args.apply and not args.maintenance_confirmed:
        parser.error("--apply requires --maintenance-confirmed after write blocking and backup verification")
    try:
        with engine.connect() as connection:
            validate_schema(connection, final=args.verify)
            count = inspect_user_data(connection, final=args.verify)
            inspect_references(connection)
            if args.check:
                print(json.dumps({"stage": "check", "users": count, "result": "passed"}))
                return
            if args.manifest.exists():
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            else:
                _require(args.apply, "The preservation manifest is required for --verify.")
                manifest = create_manifest(connection)
                args.manifest.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.manifest.with_suffix(".tmp")
                temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                temporary.replace(args.manifest)
            if args.apply:
                apply_migration(connection, manifest)
            else:
                verify_preservation(connection, manifest)
            print(json.dumps({"stage": "apply" if args.apply else "verify", "users": count, "result": "passed"}))
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (SQLAlchemyError, OSError, ValueError):
        raise SystemExit("Migration failed. Keep writes blocked; inspect schema, manifest and backup before resuming.") from None


if __name__ == "__main__":
    main()

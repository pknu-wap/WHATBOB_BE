"""Explicit, idempotent migration for Issue #67 (menu_illustration_exceptions & deployed illustration sync).

Never called automatically by application startup.
Supports:
  --check
  --apply --maintenance-confirmed
  --verify
Creates menu_illustration_exceptions (menu_id PK + FK menus.id ON DELETE CASCADE),
idempotently seeds non-tomato pasta exceptions matched by menus.food_code,
and syncs food_kinds.representative_image_url only for illustration files actually deployed on disk,
while preserving all existing menus, user_meals, food_kinds IDs, and custom representative_image_url paths.
"""
import argparse
import json
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.menus.illustrations import (
    seed_menu_illustration_exceptions,
    sync_food_kind_representative_illustrations,
)
from app.menus.models import MenuIllustrationException
import app.users.models  # noqa: F401
from scripts.migrate_uuid_auth import (
    MigrationError,
    _columns,
    _require,
    table_fingerprint,
)

MANIFEST_VERSION = 67
EXCLUDED_MANIFEST_TABLES = {"menu_illustration_exceptions"}
MUTABLE_FOOD_KIND_COLUMNS = {"representative_image_url"}


def validate_issue67_schema(connection, *, final: bool = False) -> None:
    if connection.dialect.name == "mysql":
        version = connection.dialect.server_version_info
        _require(
            not getattr(connection.dialect, "is_mariadb", False) and version >= (8, 0, 16),
            "MySQL 8.0.16 or later is required; MariaDB is not validated.",
        )

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    _require("menus" in existing_tables, "The existing menus table is required.")
    _require("food_kinds" in existing_tables, "The existing food_kinds table is required.")

    menu_cols = _columns(connection, "menus")
    _require(
        {"id", "food_code", "food_kind_id"} <= menu_cols.keys(),
        "The existing menus schema is missing required columns (id, food_code, food_kind_id).",
    )
    fk_cols = _columns(connection, "food_kinds")
    _require(
        {"id", "kind_key", "name", "representative_image_url"} <= fk_cols.keys(),
        "The existing food_kinds schema is missing required columns.",
    )

    if "menu_illustration_exceptions" in existing_tables:
        exc_cols = _columns(connection, "menu_illustration_exceptions")
        _require(
            {"menu_id", "reason", "created_at"} <= exc_cols.keys(),
            "Incompatible menu_illustration_exceptions columns.",
        )
        pk = inspector.get_pk_constraint("menu_illustration_exceptions") or {}
        _require(
            pk.get("constrained_columns") == ["menu_id"],
            "menu_illustration_exceptions must have PRIMARY KEY (menu_id).",
        )
        if final:
            fks = inspector.get_foreign_keys("menu_illustration_exceptions")
            has_cascade_fk = any(
                fk.get("constrained_columns") == ["menu_id"]
                and fk.get("referred_table") == "menus"
                and fk.get("referred_columns") == ["id"]
                and str((fk.get("options") or {}).get("ondelete") or "").upper() == "CASCADE"
                for fk in fks
            )
            _require(
                has_cascade_fk,
                "Missing ON DELETE CASCADE foreign key on menu_illustration_exceptions.menu_id -> menus.id.",
            )
    else:
        _require(not final, "Missing required table: menu_illustration_exceptions")


def inspect_issue67_data(connection, *, final: bool = False) -> dict[str, int]:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())

    total_menus = int(connection.execute(text("SELECT COUNT(*) FROM menus")).scalar_one())
    total_food_kinds = int(connection.execute(text("SELECT COUNT(*) FROM food_kinds")).scalar_one())
    registered_illustrations = int(
        connection.execute(
            text(
                "SELECT COUNT(*) FROM food_kinds "
                "WHERE representative_image_url IS NOT NULL AND TRIM(representative_image_url) <> ''"
            )
        ).scalar_one()
    )

    total_exceptions = 0
    orphan_exceptions = 0
    if "menu_illustration_exceptions" in existing_tables:
        total_exceptions = int(
            connection.execute(text("SELECT COUNT(*) FROM menu_illustration_exceptions")).scalar_one()
        )
        orphan_exceptions = int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM menu_illustration_exceptions e "
                    "LEFT JOIN menus m ON m.id = e.menu_id "
                    "WHERE m.id IS NULL"
                )
            ).scalar_one()
        )
        _require(
            orphan_exceptions == 0,
            f"Orphaned menu_illustration_exceptions rows were found ({orphan_exceptions}).",
        )
    elif final:
        _require(False, "Missing required table: menu_illustration_exceptions")

    return {
        "total_menus": total_menus,
        "total_food_kinds": total_food_kinds,
        "registered_illustrations": registered_illustrations,
        "total_exceptions": total_exceptions,
        "orphan_exceptions": orphan_exceptions,
    }


def create_issue67_manifest(connection) -> dict:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    tables: dict[str, dict] = {}
    for table in sorted(existing_tables):
        if table in EXCLUDED_MANIFEST_TABLES:
            continue
        cols = sorted(_columns(connection, table))
        if table == "food_kinds":
            cols = sorted(set(cols) - MUTABLE_FOOD_KIND_COLUMNS)
        if not cols:
            continue
        tables[table] = {"columns": cols, **table_fingerprint(connection, table, cols)}

    preserved_representative_urls: dict[str, str] = {}
    for row in connection.execute(
        text(
            "SELECT kind_key, representative_image_url FROM food_kinds "
            "WHERE representative_image_url IS NOT NULL AND TRIM(representative_image_url) <> ''"
        )
    ).mappings():
        preserved_representative_urls[str(row["kind_key"])] = str(row["representative_image_url"])

    return {
        "version": MANIFEST_VERSION,
        "database": connection.engine.url.database,
        "tables": tables,
        "preserved_representative_urls": preserved_representative_urls,
    }


def verify_issue67_preservation(connection, manifest: dict) -> None:
    _require(
        manifest.get("version") == MANIFEST_VERSION
        and manifest.get("database") == connection.engine.url.database,
        "The manifest does not match this database.",
    )
    existing_tables = set(inspect(connection).get_table_names())
    for table, baseline in manifest["tables"].items():
        _require(table in existing_tables, f"Baseline table missing: {table}")
        actual = table_fingerprint(connection, table, baseline["columns"])
        _require(
            actual == {k: baseline[k] for k in ("count", "digest")},
            f"Existing data changed in {table}; keep maintenance enabled and investigate.",
        )

    preserved_urls: dict[str, str] = manifest.get("preserved_representative_urls", {})
    if preserved_urls:
        current_urls = {
            str(r["kind_key"]): r["representative_image_url"]
            for r in connection.execute(
                text("SELECT kind_key, representative_image_url FROM food_kinds")
            ).mappings()
        }
        for kind_key, expected_url in preserved_urls.items():
            _require(
                current_urls.get(kind_key) == expected_url,
                f"Existing representative_image_url changed for kind_key={kind_key}.",
            )


def apply_issue67_migration(
    connection,
    manifest: dict,
    *,
    static_dir: Path | None = None,
) -> dict[str, int]:
    verify_issue67_preservation(connection, manifest)
    validate_issue67_schema(connection, final=False)

    existing_tables = set(inspect(connection).get_table_names())
    if "menu_illustration_exceptions" not in existing_tables:
        MenuIllustrationException.__table__.create(connection)
    connection.commit()

    with Session(bind=connection) as db:
        exc_summary = seed_menu_illustration_exceptions(db, commit=False)
        sync_summary = sync_food_kind_representative_illustrations(
            db,
            static_dir=static_dir,
            commit=False,
        )
        db.commit()

    validate_issue67_schema(connection, final=True)
    stats = inspect_issue67_data(connection, final=True)
    verify_issue67_preservation(connection, manifest)
    return {
        **stats,
        "inserted_exceptions": exc_summary["inserted"],
        "synced_illustrations": sync_summary["updated"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("check", "apply", "verify"):
        modes.add_argument(f"--{mode}", action="store_true")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(".tools/migrations/issue67-illustrations-manifest.json"),
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=None,
        help="Optional static directory override for checking deployed illustration files.",
    )
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="Confirm all catalog writes are blocked and a restorable DB backup is available.",
    )
    args = parser.parse_args()
    if args.apply and not args.maintenance_confirmed:
        parser.error("--apply requires --maintenance-confirmed after write blocking and backup verification")

    try:
        with engine.connect() as connection:
            validate_issue67_schema(connection, final=args.verify)
            stats = inspect_issue67_data(connection, final=args.verify)
            if args.check:
                print(json.dumps({"stage": "check", **stats, "result": "passed"}))
                return
            if args.manifest.exists():
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            else:
                _require(args.apply, "The preservation manifest is required for --verify.")
                manifest = create_issue67_manifest(connection)
                args.manifest.parent.mkdir(parents=True, exist_ok=True)
                tmp = args.manifest.with_suffix(".tmp")
                tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                tmp.replace(args.manifest)
            if args.apply:
                stats = apply_issue67_migration(connection, manifest, static_dir=args.static_dir)
            else:
                verify_issue67_preservation(connection, manifest)
            print(json.dumps({"stage": "apply" if args.apply else "verify", **stats, "result": "passed"}))
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (SQLAlchemyError, OSError, ValueError):
        raise SystemExit(
            "Migration failed. Keep writes blocked; inspect schema, manifest and backup before resuming."
        ) from None


if __name__ == "__main__":
    main()

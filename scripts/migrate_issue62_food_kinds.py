"""Explicit, resumable migration and food_kinds backfill for Issue #62 (Part of #54/#61).

Never called automatically by application startup.
Supports:
  --check
  --apply --maintenance-confirmed
  --verify
Backfills existing menus by food_code using shared alias rules and base_name,
preserves existing menus, user_meals, food_kinds IDs, and manual food_kind_id links,
verifies zero unlinked or invalid rows, and enforces NOT NULL and FOREIGN KEY constraints.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import engine
from app.menus.food_kinds import CANONICAL_FOOD_KINDS, classify_food_kind
from app.menus.models import FoodKind, InitialPopularFoodKind
from app.menus.popular import DEFAULT_INITIAL_POPULAR_KIND_KEYS
import app.users.models  # noqa: F401
from scripts.migrate_uuid_auth import (
    MigrationError,
    _check_index,
    _columns,
    _indexes,
    _require,
    table_fingerprint,
)

MANIFEST_VERSION = 62
EXCLUDED_MANIFEST_TABLES = {"food_kinds", "initial_popular_food_kinds"}
ADDED_MENU_COLUMNS = {"food_kind_id"}


def validate_issue62_schema(connection, *, final: bool = False) -> None:
    if connection.dialect.name == "mysql":
        version = connection.dialect.server_version_info
        _require(
            not getattr(connection.dialect, "is_mariadb", False) and version >= (8, 0, 16),
            "MySQL 8.0.16 or later is required; MariaDB is not validated.",
        )

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    _require("menus" in existing_tables, "The existing menus table is required.")

    menu_cols = _columns(connection, "menus")
    _require(
        {"id", "food_code", "name", "base_name"} <= menu_cols.keys(),
        "The existing menus schema is missing required columns (id, food_code, name, base_name).",
    )

    if "food_kinds" in existing_tables:
        fk_cols = _columns(connection, "food_kinds")
        _require(
            {"id", "kind_key", "name", "representative_image_url", "created_at"} <= fk_cols.keys(),
            "Incompatible food_kinds columns.",
        )
        if connection.dialect.name == "mysql":
            _check_index(
                connection,
                "food_kinds",
                "ix_food_kinds_kind_key",
                ["kind_key"],
                unique=True,
                required=final,
            )
            _check_index(
                connection,
                "food_kinds",
                "ix_food_kinds_name",
                ["name"],
                unique=False,
                required=final,
            )
    else:
        _require(not final, "Missing required table: food_kinds")

    if "food_kind_id" in menu_cols:
        if final:
            _require(
                not menu_cols["food_kind_id"]["nullable"],
                "menus.food_kind_id must be NOT NULL.",
            )
            _check_index(
                connection,
                "menus",
                "ix_menus_food_kind_id",
                ["food_kind_id"],
                unique=False,
                required=True,
            )
            if connection.dialect.name == "mysql":
                fks = inspector.get_foreign_keys("menus")
                has_fk = any(
                    fk.get("constrained_columns") == ["food_kind_id"]
                    and fk.get("referred_table") == "food_kinds"
                    and fk.get("referred_columns") == ["id"]
                    for fk in fks
                )
                _require(has_fk, "Missing foreign key on menus.food_kind_id -> food_kinds.id.")
    else:
        _require(not final, "Missing menus.food_kind_id")

    if "initial_popular_food_kinds" in existing_tables:
        ip_cols = _columns(connection, "initial_popular_food_kinds")
        _require(
            {"id", "position", "food_kind_id", "created_at", "updated_at"} <= ip_cols.keys(),
            "Incompatible initial_popular_food_kinds columns.",
        )
        if final and connection.dialect.name == "mysql":
            ip_fks = inspector.get_foreign_keys("initial_popular_food_kinds")
            has_ip_fk = any(
                fk.get("constrained_columns") == ["food_kind_id"]
                and fk.get("referred_table") == "food_kinds"
                and fk.get("referred_columns") == ["id"]
                for fk in ip_fks
            )
            _require(
                has_ip_fk,
                "Missing foreign key on initial_popular_food_kinds.food_kind_id -> food_kinds.id.",
            )
    else:
        _require(not final, "Missing required table: initial_popular_food_kinds")


def inspect_issue62_data(connection, *, final: bool = False) -> dict[str, int]:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    menu_cols = _columns(connection, "menus")

    total_menus = int(connection.execute(text("SELECT COUNT(*) FROM menus")).scalar_one())
    empty_code_or_base = int(
        connection.execute(
            text(
                "SELECT COUNT(*) FROM menus "
                "WHERE food_code IS NULL OR TRIM(food_code) = '' "
                "   OR ((base_name IS NULL OR TRIM(base_name) = '') AND (name IS NULL OR TRIM(name) = ''))"
            )
        ).scalar_one()
    )
    _require(empty_code_or_base == 0, "Menus with empty food_code or base_name/name were found.")

    dup_codes = int(
        connection.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT food_code FROM menus GROUP BY food_code HAVING COUNT(*) > 1"
                ") dup"
            )
        ).scalar_one()
    )
    _require(dup_codes == 0, "Duplicate food_code values exist in menus.")

    total_food_kinds = 0
    if "food_kinds" in existing_tables:
        total_food_kinds = int(connection.execute(text("SELECT COUNT(*) FROM food_kinds")).scalar_one())

    unlinked_menus = total_menus
    invalid_linked_menus = 0
    if "food_kind_id" in menu_cols:
        unlinked_menus = int(
            connection.execute(text("SELECT COUNT(*) FROM menus WHERE food_kind_id IS NULL")).scalar_one()
        )
        if "food_kinds" in existing_tables:
            invalid_linked_menus = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM menus m "
                        "LEFT JOIN food_kinds fk ON fk.id = m.food_kind_id "
                        "WHERE m.food_kind_id IS NOT NULL AND fk.id IS NULL"
                    )
                ).scalar_one()
            )
        else:
            invalid_linked_menus = int(
                connection.execute(
                    text("SELECT COUNT(*) FROM menus WHERE food_kind_id IS NOT NULL")
                ).scalar_one()
            )

    total_user_meals = 0
    if "user_meals" in existing_tables:
        total_user_meals = int(connection.execute(text("SELECT COUNT(*) FROM user_meals")).scalar_one())
        orphan_meals = int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM user_meals um "
                    "LEFT JOIN menus m ON m.id = um.menu_id "
                    "WHERE um.menu_id IS NOT NULL AND m.id IS NULL"
                )
            ).scalar_one()
        )
        _require(orphan_meals == 0, "Orphaned user_meals.menu_id references were found.")

    if final:
        _require(unlinked_menus == 0, f"Unlinked menus remain ({unlinked_menus} rows have NULL food_kind_id).")
        _require(
            invalid_linked_menus == 0,
            f"Menus with invalid food_kind_id remain ({invalid_linked_menus} rows).",
        )

    return {
        "total_menus": total_menus,
        "total_food_kinds": total_food_kinds,
        "unlinked_menus": unlinked_menus,
        "invalid_linked_menus": invalid_linked_menus,
        "total_user_meals": total_user_meals,
    }


def create_issue62_manifest(connection) -> dict:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    tables: dict[str, dict] = {}
    for table in sorted(existing_tables):
        if table in EXCLUDED_MANIFEST_TABLES:
            continue
        cols = sorted(_columns(connection, table))
        if table == "menus":
            cols = sorted(set(cols) - ADDED_MENU_COLUMNS)
        if not cols:
            continue
        tables[table] = {"columns": cols, **table_fingerprint(connection, table, cols)}

    preserved_food_kinds: dict[str, int] = {}
    if "food_kinds" in existing_tables:
        for row in connection.execute(text("SELECT id, kind_key FROM food_kinds")).mappings():
            preserved_food_kinds[str(row["kind_key"])] = int(row["id"])

    preserved_manual_links: dict[str, int] = {}
    menu_cols = _columns(connection, "menus")
    if "food_kind_id" in menu_cols and "food_kinds" in existing_tables:
        for row in connection.execute(
            text(
                "SELECT m.food_code, m.food_kind_id "
                "FROM menus m JOIN food_kinds fk ON fk.id = m.food_kind_id"
            )
        ).mappings():
            preserved_manual_links[str(row["food_code"])] = int(row["food_kind_id"])

    return {
        "version": MANIFEST_VERSION,
        "database": connection.engine.url.database,
        "tables": tables,
        "preserved_food_kinds": preserved_food_kinds,
        "preserved_manual_links": preserved_manual_links,
    }


def verify_issue62_preservation(connection, manifest: dict) -> None:
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

    preserved_kinds: dict[str, int] = manifest.get("preserved_food_kinds", {})
    if preserved_kinds:
        _require("food_kinds" in existing_tables, "food_kinds table missing after baseline had food_kinds.")
        current_kinds = {
            str(r["kind_key"]): int(r["id"])
            for r in connection.execute(text("SELECT id, kind_key FROM food_kinds")).mappings()
        }
        for kind_key, expected_id in preserved_kinds.items():
            _require(
                current_kinds.get(kind_key) == expected_id,
                f"Existing food_kinds.id changed for kind_key={kind_key}.",
            )

    preserved_links: dict[str, int] = manifest.get("preserved_manual_links", {})
    if preserved_links:
        current_links = {
            str(r["food_code"]): r["food_kind_id"]
            for r in connection.execute(text("SELECT food_code, food_kind_id FROM menus")).mappings()
        }
        for food_code, expected_fk_id in preserved_links.items():
            _require(
                current_links.get(food_code) == expected_fk_id,
                f"Preserved food_kind_id link changed for menu food_code={food_code}.",
            )


def backfill_food_kinds(connection) -> dict[str, int]:
    """기존 menus를 food_code 기준으로 백필하고, 이미 연결된 유효한 food_kind_id(수동 매핑 포함)는 보존한다."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    existing_kinds: dict[str, int] = {
        str(row["kind_key"]): int(row["id"])
        for row in connection.execute(text("SELECT id, kind_key FROM food_kinds")).mappings()
    }

    for spec in CANONICAL_FOOD_KINDS:
        if spec.kind_key not in existing_kinds:
            connection.execute(
                text(
                    "INSERT INTO food_kinds (kind_key, name, representative_image_url, created_at) "
                    "VALUES (:kind_key, :name, NULL, :created_at)"
                ),
                {"kind_key": spec.kind_key, "name": spec.name, "created_at": now_utc},
            )
    connection.commit()

    existing_kinds = {
        str(row["kind_key"]): int(row["id"])
        for row in connection.execute(text("SELECT id, kind_key FROM food_kinds")).mappings()
    }
    valid_kind_ids = set(existing_kinds.values())

    menu_rows = (
        connection.execute(
            text("SELECT id, food_code, name, base_name, food_kind_id FROM menus ORDER BY id ASC")
        )
        .mappings()
        .all()
    )

    missing_kinds_to_insert: dict[str, str] = {}
    menus_to_update: list[tuple[str, str]] = []
    preserved_menus = 0

    for row in menu_rows:
        fk_id = row["food_kind_id"]
        if fk_id is not None:
            if int(fk_id) in valid_kind_ids:
                preserved_menus += 1
            continue

        kind_key, display_name = classify_food_kind(
            base_name=row["base_name"],
            name=row["name"],
        )
        if kind_key not in existing_kinds and kind_key not in missing_kinds_to_insert:
            missing_kinds_to_insert[kind_key] = display_name
        menus_to_update.append((str(row["food_code"]), kind_key))

    if missing_kinds_to_insert:
        connection.execute(
            text(
                "INSERT INTO food_kinds (kind_key, name, representative_image_url, created_at) "
                "VALUES (:kind_key, :name, NULL, :created_at)"
            ),
            [
                {"kind_key": k, "name": name, "created_at": now_utc}
                for k, name in missing_kinds_to_insert.items()
            ],
        )
        connection.commit()
        existing_kinds = {
            str(row["kind_key"]): int(row["id"])
            for row in connection.execute(text("SELECT id, kind_key FROM food_kinds")).mappings()
        }

    if menus_to_update:
        connection.execute(
            text(
                "UPDATE menus SET food_kind_id = :fk_id "
                "WHERE food_code = :food_code AND food_kind_id IS NULL"
            ),
            [
                {"fk_id": existing_kinds[kind_key], "food_code": food_code}
                for food_code, kind_key in menus_to_update
            ],
        )
        connection.commit()

    return {
        "backfilled_menus": len(menus_to_update),
        "preserved_menus": preserved_menus,
        "total_food_kinds": len(existing_kinds),
    }


def backfill_initial_popular_food_kinds(connection) -> dict[str, int]:
    """초기 인기 메뉴 8종 테이블을 보장하고, 비어 있을 때만 합의된 8종을 위치 1~8로 적재한다."""
    existing_tables = set(inspect(connection).get_table_names())
    if "initial_popular_food_kinds" not in existing_tables:
        InitialPopularFoodKind.__table__.create(connection)
        connection.commit()

    existing_count = int(
        connection.execute(text("SELECT COUNT(*) FROM initial_popular_food_kinds")).scalar_one()
    )
    if existing_count > 0:
        return {
            "seeded_initial_popular": 0,
            "total_initial_popular": existing_count,
        }

    kind_ids_by_key = {
        str(row["kind_key"]): int(row["id"])
        for row in connection.execute(text("SELECT id, kind_key FROM food_kinds")).mappings()
    }
    missing = [key for key in DEFAULT_INITIAL_POPULAR_KIND_KEYS if key not in kind_ids_by_key]
    _require(
        not missing,
        f"Missing canonical food_kinds required for initial popular list: {', '.join(missing)}",
    )

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    connection.execute(
        text(
            "INSERT INTO initial_popular_food_kinds (position, food_kind_id, created_at, updated_at) "
            "VALUES (:position, :food_kind_id, :created_at, :updated_at)"
        ),
        [
            {
                "position": position,
                "food_kind_id": kind_ids_by_key[kind_key],
                "created_at": now_utc,
                "updated_at": now_utc,
            }
            for position, kind_key in enumerate(DEFAULT_INITIAL_POPULAR_KIND_KEYS, start=1)
        ],
    )
    connection.commit()
    return {
        "seeded_initial_popular": len(DEFAULT_INITIAL_POPULAR_KIND_KEYS),
        "total_initial_popular": len(DEFAULT_INITIAL_POPULAR_KIND_KEYS),
    }


def apply_issue62_migration(connection, manifest: dict) -> None:
    verify_issue62_preservation(connection, manifest)
    validate_issue62_schema(connection, final=False)

    existing_tables = set(inspect(connection).get_table_names())
    if "food_kinds" not in existing_tables:
        FoodKind.__table__.create(connection)
        connection.commit()
        existing_tables.add("food_kinds")

    current_fk_idx_names = set(_indexes(connection, "food_kinds").keys())
    for idx in sorted(FoodKind.__table__.indexes, key=lambda i: i.name):
        if idx.name not in current_fk_idx_names:
            idx.create(connection)
            connection.commit()

    menu_cols = _columns(connection, "menus")
    if "food_kind_id" not in menu_cols:
        connection.execute(text("ALTER TABLE menus ADD COLUMN food_kind_id INTEGER NULL"))
        connection.commit()

    # food_code 기준 백필 실행 (기존 연결 및 수동 분류 보존)
    backfill_food_kinds(connection)

    # 제약 적용 전 미연결(NULL) 또는 잘못된 food_kind_id가 없는지 먼저 검증
    inspect_issue62_data(connection, final=True)
    verify_issue62_preservation(connection, manifest)

    menu_indexes = _indexes(connection, "menus")
    if "ix_menus_food_kind_id" not in menu_indexes:
        connection.execute(text("CREATE INDEX ix_menus_food_kind_id ON menus (food_kind_id)"))
        connection.commit()

    if connection.dialect.name == "mysql":
        menu_cols = _columns(connection, "menus")
        if menu_cols["food_kind_id"]["nullable"]:
            connection.execute(text("ALTER TABLE menus MODIFY COLUMN food_kind_id INTEGER NOT NULL"))
            connection.commit()

        fks = inspect(connection).get_foreign_keys("menus")
        has_fk = any(
            fk.get("constrained_columns") == ["food_kind_id"]
            and fk.get("referred_table") == "food_kinds"
            and fk.get("referred_columns") == ["id"]
            for fk in fks
        )
        if not has_fk:
            connection.execute(
                text(
                    "ALTER TABLE menus "
                    "ADD CONSTRAINT fk_menus_food_kind "
                    "FOREIGN KEY (food_kind_id) REFERENCES food_kinds (id)"
                )
            )
            connection.commit()

    backfill_initial_popular_food_kinds(connection)

    validate_issue62_schema(connection, final=True)
    inspect_issue62_data(connection, final=True)
    verify_issue62_preservation(connection, manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("check", "apply", "verify"):
        modes.add_argument(f"--{mode}", action="store_true")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(".tools/migrations/issue62-food-kinds-manifest.json"),
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
            validate_issue62_schema(connection, final=args.verify)
            stats = inspect_issue62_data(connection, final=args.verify)
            if args.check:
                print(json.dumps({"stage": "check", **stats, "result": "passed"}))
                return
            if args.manifest.exists():
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            else:
                _require(args.apply, "The preservation manifest is required for --verify.")
                manifest = create_issue62_manifest(connection)
                args.manifest.parent.mkdir(parents=True, exist_ok=True)
                tmp = args.manifest.with_suffix(".tmp")
                tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                tmp.replace(args.manifest)
            if args.apply:
                apply_issue62_migration(connection, manifest)
                stats = inspect_issue62_data(connection, final=True)
            else:
                verify_issue62_preservation(connection, manifest)
            print(json.dumps({"stage": "apply" if args.apply else "verify", **stats, "result": "passed"}))
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (SQLAlchemyError, OSError, ValueError):
        raise SystemExit(
            "Migration failed. Keep writes blocked; inspect schema, manifest and backup before resuming."
        ) from None


if __name__ == "__main__":
    main()

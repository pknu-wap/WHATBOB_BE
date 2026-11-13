"""Explicit, resumable MySQL migration for Issue #39 (T01~T09).

Never called automatically by application startup.
Supports:
  --check
  --apply --maintenance-confirmed
  --verify
Preserves all existing user/child records, ownerless legacy feedback rows,
and unmapped legacy exclusions, and supports resumption after partial failure.
"""
import argparse
import json
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.auth.models import AccountTransition, AuthSession, KakaoLinkGuard, RequestOperation
from app.core.database import engine
from app.menus.models import (
    RecommendationFeedbackState,
    RecommendationItemSnapshot,
)
import app.rooms.models  # noqa: F401
import app.users.models  # noqa: F401
from scripts import migrate_uuid_auth as base_migration
from scripts.migrate_uuid_auth import (
    MigrationError,
    _check_index,
    _columns,
    _indexes,
    _require,
    table_fingerprint,
)

ISSUE39_USER_COLUMNS: dict[str, str] = {
    "nickname_edited": "BOOLEAN NOT NULL DEFAULT FALSE",
    "nickname_source_edited": "BOOLEAN NOT NULL DEFAULT FALSE",
    "nickname_source_uuid": "CHAR(36) CHARACTER SET ascii NULL",
    "nickname_source_id": "INTEGER NULL",
    "nickname_source_created_at": "DATETIME(6) NULL",
    "profile_image_edited": "BOOLEAN NOT NULL DEFAULT FALSE",
    "profile_image_source_edited": "BOOLEAN NOT NULL DEFAULT FALSE",
    "profile_image_source_uuid": "CHAR(36) CHARACTER SET ascii NULL",
    "profile_image_source_id": "INTEGER NULL",
    "profile_image_source_created_at": "DATETIME(6) NULL",
}

ISSUE39_EXCLUSION_COLUMNS: dict[str, str] = {
    "catalog_code": "VARCHAR(50) NULL",
    "catalog_version": "INTEGER NULL",
}

ISSUE39_FEEDBACK_COLUMNS: dict[str, str] = {
    "menu_key": "VARCHAR(100) NULL",
    "menu_base_name": "VARCHAR(100) NULL",
    "menu_main_ingredient": "VARCHAR(30) NULL",
    "operation_key": "VARCHAR(64) NULL",
}

NEW_TABLES = (
    AuthSession.__table__,
    RequestOperation.__table__,
    AccountTransition.__table__,
    KakaoLinkGuard.__table__,
    RecommendationFeedbackState.__table__,
    RecommendationItemSnapshot.__table__,
)

EXCLUDED_MANIFEST_TABLES = {t.name for t in NEW_TABLES}
ALL_ADDED_USER_COLUMNS = base_migration.ADDED_USER_COLUMNS | set(ISSUE39_USER_COLUMNS.keys())
MANIFEST_VERSION = 2


def validate_issue39_schema(connection, *, final: bool = False) -> None:
    base_migration.validate_schema(connection, final=final)
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())

    user_cols = _columns(connection, "users")
    for col_name in ISSUE39_USER_COLUMNS:
        if col_name not in user_cols:
            _require(not final, f"Missing users.{col_name}")

    if "user_food_exclusions" in existing_tables:
        exc_cols = _columns(connection, "user_food_exclusions")
        for col_name in ISSUE39_EXCLUSION_COLUMNS:
            if col_name not in exc_cols:
                _require(not final, f"Missing user_food_exclusions.{col_name}")
        _check_index(
            connection,
            "user_food_exclusions",
            "ix_user_food_exclusions_catalog_code",
            ["catalog_code"],
            unique=False,
            required=final,
        )

    if "recommendation_feedbacks" in existing_tables:
        fb_cols = _columns(connection, "recommendation_feedbacks")
        for col_name in ISSUE39_FEEDBACK_COLUMNS:
            if col_name not in fb_cols:
                _require(not final, f"Missing recommendation_feedbacks.{col_name}")
        _check_index(
            connection,
            "recommendation_feedbacks",
            "ix_recommendation_feedbacks_menu_key",
            ["menu_key"],
            unique=False,
            required=final,
        )
        _check_index(
            connection,
            "recommendation_feedbacks",
            "ix_recommendation_feedbacks_operation_key",
            ["operation_key"],
            unique=False,
            required=final,
        )

    for tbl in NEW_TABLES:
        if tbl.name not in existing_tables:
            _require(not final, f"Missing required table: {tbl.name}")
            continue
        for idx in tbl.indexes:
            _check_index(
                connection,
                tbl.name,
                idx.name,
                [c.name for c in idx.columns],
                unique=bool(idx.unique),
                required=final,
            )


def inspect_issue39_data(connection, *, final: bool = False) -> dict[str, int]:
    user_count = base_migration.inspect_user_data(connection, final=final)
    base_migration.inspect_references(connection)

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    unmapped_exclusions = 0
    ownerless_feedbacks = 0

    if "user_food_exclusions" in existing_tables:
        exc_cols = _columns(connection, "user_food_exclusions")
        if "catalog_code" in exc_cols:
            unmapped_exclusions = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM user_food_exclusions "
                        "WHERE exclusion_type IN ('ALLERGY', 'UNABLE') "
                        "AND (catalog_code IS NULL OR TRIM(catalog_code) = '')"
                    )
                ).scalar_one()
            )
        else:
            unmapped_exclusions = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM user_food_exclusions "
                        "WHERE exclusion_type IN ('ALLERGY', 'UNABLE')"
                    )
                ).scalar_one()
            )

    if "recommendation_feedbacks" in existing_tables:
        ownerless_feedbacks = int(
            connection.execute(
                text("SELECT COUNT(*) FROM recommendation_feedbacks WHERE user_id IS NULL")
            ).scalar_one()
        )

    return {
        "users": user_count,
        "unmapped_catalog_exclusions": unmapped_exclusions,
        "ownerless_feedbacks": ownerless_feedbacks,
    }


def create_issue39_manifest(connection) -> dict:
    inspector = inspect(connection)
    tables: dict[str, dict] = {}
    for table in sorted(inspector.get_table_names()):
        if table in EXCLUDED_MANIFEST_TABLES:
            continue
        cols = sorted(_columns(connection, table))
        if table == "users":
            cols = sorted(set(cols) - ALL_ADDED_USER_COLUMNS)
        elif table == "user_food_exclusions":
            cols = sorted(set(cols) - set(ISSUE39_EXCLUSION_COLUMNS.keys()))
        elif table == "recommendation_feedbacks":
            cols = sorted(set(cols) - set(ISSUE39_FEEDBACK_COLUMNS.keys()))
        if not cols:
            continue
        tables[table] = {"columns": cols, **table_fingerprint(connection, table, cols)}

    user_cols = _columns(connection, "users")
    legacy_user_ids = (
        list(connection.execute(text("SELECT id FROM users WHERE kakao_id IS NOT NULL")).scalars())
        if "is_legacy_kakao" not in user_cols
        else []
    )
    return {
        "version": MANIFEST_VERSION,
        "database": connection.engine.url.database,
        "tables": tables,
        "legacy_user_ids": legacy_user_ids,
    }


def verify_issue39_preservation(connection, manifest: dict) -> None:
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


def _apply_base_uuid_migration_steps(connection, manifest: dict) -> None:
    user_cols = _columns(connection, "users")
    base_additions = {
        "uuid": "CHAR(36) CHARACTER SET ascii NULL",
        "nickname_key": "VARBINARY(400) NULL",
        "is_legacy_kakao": "BOOLEAN NULL DEFAULT NULL",
    }
    for name, definition in base_additions.items():
        if name not in user_cols:
            connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))
            connection.commit()

    rows = connection.execute(text("SELECT id, nickname, uuid, nickname_key FROM users")).mappings().all()
    for row in rows:
        key = row["nickname"].strip().encode("utf-8") if row["nickname"] and row["nickname"].strip() else None
        connection.execute(
            text("UPDATE users SET uuid=:uuid, nickname_key=:key, updated_at=updated_at WHERE id=:id"),
            {"uuid": row["uuid"] or str(base_migration.uuid4()), "key": key, "id": row["id"]},
        )
    for user_id in manifest.get("legacy_user_ids", []):
        connection.execute(
            text("UPDATE users SET is_legacy_kakao=TRUE, updated_at=updated_at WHERE id=:id"),
            {"id": user_id},
        )
    connection.execute(
        text("UPDATE users SET is_legacy_kakao=FALSE, updated_at=updated_at WHERE is_legacy_kakao IS NULL")
    )
    connection.commit()

    user_cols = _columns(connection, "users")
    if user_cols["uuid"]["nullable"]:
        connection.execute(text("ALTER TABLE users MODIFY COLUMN uuid CHAR(36) CHARACTER SET ascii NOT NULL"))
    if not user_cols["kakao_id"]["nullable"]:
        connection.execute(text("ALTER TABLE users MODIFY COLUMN kakao_id BIGINT NULL"))
    if user_cols["is_legacy_kakao"]["nullable"] or str(user_cols["is_legacy_kakao"]["default"]) not in {"0", "'0'"}:
        connection.execute(text("ALTER TABLE users MODIFY COLUMN is_legacy_kakao BOOLEAN NOT NULL DEFAULT FALSE"))
    connection.commit()

    for idx_name, col_name in [("uq_users_uuid", "uuid"), ("uq_users_nickname_key", "nickname_key")]:
        if idx_name not in _indexes(connection, "users"):
            connection.execute(text(f"CREATE UNIQUE INDEX {idx_name} ON users ({col_name})"))
            connection.commit()


def apply_issue39_migration(connection, manifest: dict) -> None:
    verify_issue39_preservation(connection, manifest)
    _apply_base_uuid_migration_steps(connection, manifest)

    # 1. users 프로필 출처·직접 편집 컬럼 추가 및 UUID 사용자 기본 출처 backfill
    user_cols = _columns(connection, "users")
    for name, definition in ISSUE39_USER_COLUMNS.items():
        if name not in user_cols:
            connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))
            connection.commit()

    connection.execute(
        text(
            "UPDATE users "
            "SET nickname_source_uuid = COALESCE(nickname_source_uuid, uuid), "
            "    nickname_source_id = COALESCE(nickname_source_id, id), "
            "    nickname_source_created_at = COALESCE(nickname_source_created_at, created_at), "
            "    profile_image_source_uuid = COALESCE(profile_image_source_uuid, uuid), "
            "    profile_image_source_id = COALESCE(profile_image_source_id, id), "
            "    profile_image_source_created_at = COALESCE(profile_image_source_created_at, created_at), "
            "    updated_at = updated_at "
            "WHERE kakao_id IS NULL"
        )
    )
    connection.commit()

    existing_tables = set(inspect(connection).get_table_names())

    # 2. user_food_exclusions 카탈로그 컬럼·인덱스 확장 (미대응 자유 문자열 원본은 절대 임의 변환/삭제하지 않음)
    if "user_food_exclusions" in existing_tables:
        exc_cols = _columns(connection, "user_food_exclusions")
        for name, definition in ISSUE39_EXCLUSION_COLUMNS.items():
            if name not in exc_cols:
                connection.execute(text(f"ALTER TABLE user_food_exclusions ADD COLUMN {name} {definition}"))
                connection.commit()
        if "ix_user_food_exclusions_catalog_code" not in _indexes(connection, "user_food_exclusions"):
            connection.execute(
                text(
                    "CREATE INDEX ix_user_food_exclusions_catalog_code "
                    "ON user_food_exclusions (catalog_code)"
                )
            )
            connection.commit()

    # 3. recommendation_feedbacks 스냅샷·작업 키 컬럼 추가 및 기존 이력 backfill
    if "recommendation_feedbacks" in existing_tables:
        fb_cols = _columns(connection, "recommendation_feedbacks")
        for name, definition in ISSUE39_FEEDBACK_COLUMNS.items():
            if name not in fb_cols:
                connection.execute(
                    text(f"ALTER TABLE recommendation_feedbacks ADD COLUMN {name} {definition}")
                )
                connection.commit()

        if "menus" in existing_tables:
            connection.execute(
                text(
                    "UPDATE recommendation_feedbacks fb "
                    "LEFT JOIN menus m ON m.id = fb.selected_menu_id "
                    "SET fb.menu_key = COALESCE(fb.menu_key, m.base_name, "
                    "    CASE WHEN fb.selected_menu_id IS NOT NULL THEN CONCAT('id:', fb.selected_menu_id) ELSE 'legacy:unknown' END), "
                    "    fb.menu_base_name = COALESCE(fb.menu_base_name, m.base_name, "
                    "    CASE WHEN fb.selected_menu_id IS NOT NULL THEN CONCAT('id:', fb.selected_menu_id) ELSE 'legacy:unknown' END), "
                    "    fb.menu_main_ingredient = COALESCE(fb.menu_main_ingredient, m.main_ingredient, 'other') "
                    "WHERE fb.menu_key IS NULL"
                )
            )
        else:
            connection.execute(
                text(
                    "UPDATE recommendation_feedbacks "
                    "SET menu_key = COALESCE(menu_key, CASE WHEN selected_menu_id IS NOT NULL THEN CONCAT('id:', selected_menu_id) ELSE 'legacy:unknown' END), "
                    "    menu_base_name = COALESCE(menu_base_name, CASE WHEN selected_menu_id IS NOT NULL THEN CONCAT('id:', selected_menu_id) ELSE 'legacy:unknown' END), "
                    "    menu_main_ingredient = COALESCE(menu_main_ingredient, 'other') "
                    "WHERE menu_key IS NULL"
                )
            )
        connection.commit()

        fb_indexes = _indexes(connection, "recommendation_feedbacks")
        if "ix_recommendation_feedbacks_menu_key" not in fb_indexes:
            connection.execute(
                text("CREATE INDEX ix_recommendation_feedbacks_menu_key ON recommendation_feedbacks (menu_key)")
            )
            connection.commit()
        if "ix_recommendation_feedbacks_operation_key" not in fb_indexes:
            connection.execute(
                text(
                    "CREATE INDEX ix_recommendation_feedbacks_operation_key "
                    "ON recommendation_feedbacks (operation_key)"
                )
            )
            connection.commit()

    # 4. 신규 테이블 및 인덱스 생성 (부분 중단 후 재실행 시 누락 인덱스 복구 포함)
    existing_tables = set(inspect(connection).get_table_names())
    for tbl in NEW_TABLES:
        if tbl.name not in existing_tables:
            # 의존하는 부모 테이블(recommendations, menus 등)이 없는 축소 스키마에서도 생성 가능하도록 FK 대상 확인
            missing_parent = any(
                fk.column.table.name not in existing_tables and fk.column.table.name != tbl.name
                for fk in tbl.foreign_keys
            )
            if not missing_parent:
                tbl.create(connection)
                connection.commit()
                existing_tables.add(tbl.name)
        if tbl.name in existing_tables:
            current_idx_names = set(_indexes(connection, tbl.name).keys())
            for idx in sorted(tbl.indexes, key=lambda i: i.name):
                if idx.name not in current_idx_names:
                    idx.create(connection)
                    connection.commit()

    # 5. recommendation_feedback_states 파생 상태 재구축 (소유자 있는 추천·피드백 기준, 원본 피드백은 보존)
    if (
        "recommendation_feedbacks" in existing_tables
        and "recommendation_feedback_states" in existing_tables
    ):
        rows = (
            connection.execute(
                text(
                    "SELECT id, recommendation_id, user_id, selected_menu_id, menu_key, "
                    "       menu_base_name, menu_main_ingredient, is_satisfied, "
                    "       feature_match_vector, created_at "
                    "FROM recommendation_feedbacks "
                    "WHERE user_id IS NOT NULL "
                    "ORDER BY id ASC"
                )
            )
            .mappings()
            .all()
        )
        latest_by_rec_menu: dict[tuple[int, str], dict] = {}
        first_time_by_rec_menu: dict[tuple[int, str], object] = {}
        for r in rows:
            mkey = r["menu_key"] or (
                f"id:{r['selected_menu_id']}" if r["selected_menu_id"] is not None else "legacy:unknown"
            )
            key = (int(r["recommendation_id"]), str(mkey))
            if key not in first_time_by_rec_menu:
                first_time_by_rec_menu[key] = r["created_at"]
            latest_by_rec_menu[key] = {**dict(r), "resolved_menu_key": str(mkey)}

        for (rec_id, mkey), fb in latest_by_rec_menu.items():
            existing_state_id = connection.execute(
                text(
                    "SELECT id FROM recommendation_feedback_states "
                    "WHERE recommendation_id = :rec_id AND menu_key = :mkey"
                ),
                {"rec_id": rec_id, "mkey": mkey},
            ).scalar_one_or_none()
            vec_val = fb["feature_match_vector"]
            vec_json = vec_val if isinstance(vec_val, str) else json.dumps(vec_val or {})
            created_ts = first_time_by_rec_menu[(rec_id, mkey)]
            updated_ts = fb["created_at"]
            if existing_state_id is None:
                connection.execute(
                    text(
                        "INSERT INTO recommendation_feedback_states "
                        "(recommendation_id, user_id, selected_menu_id, menu_key, menu_base_name, "
                        " menu_main_ingredient, is_satisfied, feature_match_vector, latest_feedback_id, "
                        " created_at, updated_at) "
                        "VALUES (:rec_id, :user_id, :sel_id, :mkey, :base_name, :main_ing, "
                        "        :is_sat, :vec, :fb_id, :created_at, :updated_at)"
                    ),
                    {
                        "rec_id": rec_id,
                        "user_id": fb["user_id"],
                        "sel_id": fb["selected_menu_id"],
                        "mkey": mkey,
                        "base_name": fb["menu_base_name"] or mkey,
                        "main_ing": fb["menu_main_ingredient"] or "other",
                        "is_sat": bool(fb["is_satisfied"]),
                        "vec": vec_json,
                        "fb_id": fb["id"],
                        "created_at": created_ts,
                        "updated_at": updated_ts,
                    },
                )
            else:
                connection.execute(
                    text(
                        "UPDATE recommendation_feedback_states "
                        "SET user_id = :user_id, selected_menu_id = :sel_id, "
                        "    menu_base_name = :base_name, menu_main_ingredient = :main_ing, "
                        "    is_satisfied = :is_sat, feature_match_vector = :vec, "
                        "    latest_feedback_id = :fb_id, updated_at = :updated_at "
                        "WHERE id = :state_id"
                    ),
                    {
                        "user_id": fb["user_id"],
                        "sel_id": fb["selected_menu_id"],
                        "base_name": fb["menu_base_name"] or mkey,
                        "main_ing": fb["menu_main_ingredient"] or "other",
                        "is_sat": bool(fb["is_satisfied"]),
                        "vec": vec_json,
                        "fb_id": fb["id"],
                        "updated_at": updated_ts,
                        "state_id": existing_state_id,
                    },
                )
        connection.commit()

    validate_issue39_schema(connection, final=True)
    inspect_issue39_data(connection, final=True)
    verify_issue39_preservation(connection, manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("check", "apply", "verify"):
        modes.add_argument(f"--{mode}", action="store_true")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(".tools/migrations/issue39-manifest.json"),
    )
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="Confirm all writes are blocked and a restorable DB backup is available.",
    )
    args = parser.parse_args()
    if args.apply and not args.maintenance_confirmed:
        parser.error("--apply requires --maintenance-confirmed after write blocking and backup verification")

    try:
        with engine.connect() as connection:
            validate_issue39_schema(connection, final=args.verify)
            stats = inspect_issue39_data(connection, final=args.verify)
            if args.check:
                print(json.dumps({"stage": "check", **stats, "result": "passed"}))
                return
            if args.manifest.exists():
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            else:
                _require(args.apply, "The preservation manifest is required for --verify.")
                manifest = create_issue39_manifest(connection)
                args.manifest.parent.mkdir(parents=True, exist_ok=True)
                tmp = args.manifest.with_suffix(".tmp")
                tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                tmp.replace(args.manifest)
            if args.apply:
                apply_issue39_migration(connection, manifest)
                stats = inspect_issue39_data(connection, final=True)
            else:
                verify_issue39_preservation(connection, manifest)
            print(json.dumps({"stage": "apply" if args.apply else "verify", **stats, "result": "passed"}))
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (SQLAlchemyError, OSError, ValueError):
        raise SystemExit(
            "Migration failed. Keep writes blocked; inspect schema, manifest and backup before resuming."
        ) from None


if __name__ == "__main__":
    main()

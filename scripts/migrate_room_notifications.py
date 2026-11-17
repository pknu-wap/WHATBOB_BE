"""Explicit, additive, resumable MySQL 8 migration for #45.

Stop ALL writers and keep a restorable backup before --apply
--maintenance-confirmed. Never run automatically from API startup. Preserve the
manifest across interruptions; MySQL DDL commits independently of transactions.
"""
import argparse
import json
from pathlib import Path

from sqlalchemy import inspect, text, Integer, String, DateTime, JSON
from sqlalchemy.exc import SQLAlchemyError

import app.users.models  # noqa: F401
from app.core.database import engine
from app.rooms.models import RoomNotification
from scripts.migrate_uuid_auth import MigrationError, _require, table_fingerprint, _check_index


def validate_schema(connection, *, final=False):
    _require(connection.dialect.name == 'mysql' and not getattr(connection.dialect, 'is_mariadb', False)
             and connection.dialect.server_version_info >= (8, 0, 16), 'MySQL 8.0.16+ is required.')
    inspector = inspect(connection)
    _require('rooms' in inspector.get_table_names(), 'Existing rooms schema is required.')
    _require(inspector.get_table_options('rooms').get('mysql_engine', '').lower() == 'innodb',
             'rooms must use InnoDB.')
    columns = {c['name']: c for c in inspector.get_columns('rooms')}
    _require(isinstance(columns['id']['type'], Integer) and not getattr(columns['id']['type'], 'unsigned', False),
             'rooms.id must be a signed INTEGER.')
    version = columns.get('state_version')
    _require(version is not None or not final, 'Missing rooms.state_version.')
    if version:
        _require(isinstance(version['type'], Integer) and not version['nullable']
                 and str(version['default']).strip("'()") == '0', 'Incompatible rooms.state_version.')
    if 'room_notifications' not in inspector.get_table_names():
        _require(not final, 'Missing room_notifications.')
        return
    _require(inspector.get_table_options('room_notifications').get('mysql_engine', '').lower() == 'innodb',
             'room_notifications must use InnoDB.')
    columns = {c['name']: c for c in inspector.get_columns('room_notifications')}
    for expected in RoomNotification.__table__.columns:
        actual = columns.get(expected.name)
        _require(actual is not None, f'Missing notification column {expected.name}.')
        type_ = expected.type
        _require(isinstance(actual['type'], type(type_)), f'Incompatible type for {expected.name}.')
        _require(actual['nullable'] == expected.nullable, f'Incompatible nullability for {expected.name}.')
        if isinstance(type_, String):
            _require(actual['type'].length == type_.length, f'Incompatible length for {expected.name}.')
        if isinstance(type_, DateTime):
            _require(getattr(actual['type'], 'fsp', None) == 6, f'{expected.name} requires DATETIME(6).')
        if isinstance(type_, Integer):
            _require(not getattr(actual['type'], 'unsigned', False), f'Unsigned {expected.name} is unsupported.')
    _require(inspector.get_pk_constraint('room_notifications')['constrained_columns'] == ['id'],
             'Incompatible notification primary key.')
    foreign = inspector.get_foreign_keys('room_notifications')
    _require(any(f['constrained_columns'] == ['room_id'] and f['referred_table'] == 'rooms'
                 and f['referred_columns'] == ['id'] and f.get('options', {}).get('ondelete', '').upper() == 'SET NULL'
                 for f in foreign), 'room_id requires ON DELETE SET NULL foreign key.')
    _check_index(connection, 'room_notifications', 'uq_room_notifications_version',
                 ['historical_room_id', 'state_version'], unique=True, required=final)
    for index in RoomNotification.__table__.indexes:
        _check_index(connection, 'room_notifications', index.name, [c.name for c in index.columns],
                     unique=index.unique, required=final)
    invalid = connection.execute(text('''SELECT COUNT(*) FROM room_notifications
        WHERE finished_at IS NOT NULL AND
        (payload_json IS NOT NULL OR terminal_reason IS NULL OR next_attempt_at IS NOT NULL)''')).scalar_one()
    _require(invalid == 0, 'Terminal notification invariants violated; inspect without deleting evidence.')


def create_manifest(connection):
    tables = {}
    inspector = inspect(connection)
    for table in inspector.get_table_names():
        if table == 'room_notifications':
            continue
        columns = [c['name'] for c in inspector.get_columns(table)
                   if not (table == 'rooms' and c['name'] == 'state_version')]
        tables[table] = dict(columns=columns, **table_fingerprint(connection, table, columns))
    return dict(version=1, database=connection.engine.url.database, tables=tables)


def verify_preservation(connection, manifest):
    _require(manifest.get('version') == 1 and manifest.get('database') == connection.engine.url.database,
             'Manifest does not match this database.')
    for table, baseline in manifest['tables'].items():
        actual = table_fingerprint(connection, table, baseline['columns'])
        _require(actual == {k: baseline[k] for k in ('count', 'digest')}, f'Existing data changed: {table}.')


def apply_migration(connection, manifest):
    validate_schema(connection)
    verify_preservation(connection, manifest)
    if 'state_version' not in {c['name'] for c in inspect(connection).get_columns('rooms')}:
        connection.execute(text('ALTER TABLE rooms ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0'))
        connection.commit()
    RoomNotification.__table__.create(connection, checkfirst=True)
    connection.commit()
    indexes = {i['name'] for i in inspect(connection).get_indexes('room_notifications')}
    if 'uq_room_notifications_version' not in indexes:
        connection.execute(text('ALTER TABLE room_notifications ADD CONSTRAINT uq_room_notifications_version '
                                'UNIQUE (historical_room_id, state_version)'))
        connection.commit()
    for index in RoomNotification.__table__.indexes:
        if index.name not in indexes:
            index.create(connection)
            connection.commit()
    validate_schema(connection, final=True)
    verify_preservation(connection, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ('check', 'apply', 'verify'):
        modes.add_argument('--' + mode, action='store_true')
    parser.add_argument('--maintenance-confirmed', action='store_true')
    parser.add_argument('--manifest', type=Path, default=Path('.tools/migrations/room-notifications.json'))
    args = parser.parse_args()
    if args.apply and not args.maintenance_confirmed:
        parser.error('--apply requires --maintenance-confirmed after blocking writers and verifying backup')
    try:
        with engine.connect() as connection:
            validate_schema(connection, final=args.verify)
            if args.check:
                print(json.dumps(dict(stage='check', result='passed')))
                return
            if args.manifest.exists():
                manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
            else:
                _require(args.apply, 'Preservation manifest is required for --verify.')
                manifest = create_manifest(connection)
                args.manifest.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.manifest.with_suffix('.tmp')
                temporary.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
                temporary.replace(args.manifest)
            if args.apply:
                apply_migration(connection, manifest)
            else:
                verify_preservation(connection, manifest)
            print(json.dumps(dict(stage='apply' if args.apply else 'verify', result='passed')))
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (SQLAlchemyError, OSError, ValueError):
        raise SystemExit('Migration failed. Keep writers blocked; inspect schema, manifest and backup before resuming.') from None


if __name__ == '__main__':
    main()

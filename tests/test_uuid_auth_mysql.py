"""Real InnoDB tests. MYSQL_TEST_DATABASE_URL must name a dedicated test DB.

The account needs CREATE/DROP DATABASE permission. Each case creates and drops
only its own randomly named child test database; application databases are untouched.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import subprocess
import sys
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth.cookie_router import router
from app.auth.models import AuthSession
from app.auth.sessions import new_session, revoke_session
from app.core.config import settings
from app.core.database import Base, get_db
from app.users.models import User
from scripts import migrate_uuid_auth as migration
from tests.test_uuid_auth import WEB_HEADERS


@pytest.fixture
def mysql_engine():
    value = os.getenv("MYSQL_TEST_DATABASE_URL")
    if not value:
        pytest.skip("MYSQL_TEST_DATABASE_URL is required for real MySQL tests")
    url = make_url(value)
    if not url.drivername.startswith("mysql") or not url.database or "test" not in url.database.lower():
        pytest.fail("MYSQL_TEST_DATABASE_URL must point to a dedicated MySQL test database")
    schema = f"{url.database[:32]}_uuid_{uuid4().hex[:12]}"
    admin = create_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"))
    engine = create_engine(url.set(database=schema), pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE `{schema}`"))
        admin.dispose()


def old_schema(connection):
    connection.execute(text("""CREATE TABLE users (
        id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY,
        kakao_id BIGINT NOT NULL, nickname VARCHAR(100) NULL,
        email VARCHAR(255) NULL, profile_image VARCHAR(500) NULL,
        created_at DATETIME(6) NULL,
        updated_at DATETIME(6) NULL ON UPDATE CURRENT_TIMESTAMP(6),
        UNIQUE INDEX ix_users_kakao_id (kakao_id)
    ) ENGINE=InnoDB"""))
    connection.execute(text("""CREATE TABLE legacy_meals (
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
        menu_name VARCHAR(200), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB"""))
    connection.execute(text("""INSERT INTO users (id,kakao_id,nickname,email,profile_image,created_at,updated_at)
        VALUES (7,701,'  Preserved profile  ','person@example.test','/preserved.jpg','2024-01-01','2024-02-02'),
               (9,901,NULL,NULL,NULL,'2024-01-03','2024-02-03')"""))
    connection.execute(text("INSERT INTO legacy_meals VALUES (11,7,'original meal')"))
    connection.commit()


def test_migration_preserves_records_resumes_and_can_be_reapplied(mysql_engine, monkeypatch):
    monkeypatch.setattr(migration, "engine", mysql_engine)
    with mysql_engine.connect() as connection:
        old_schema(connection)
        migration.validate_schema(connection)
        assert migration.inspect_user_data(connection) == 2
        manifest = migration.create_manifest(connection)
        # Simulate interruption after expansion and partial UUID fill.
        existing_uuid = str(uuid4())
        connection.execute(text("ALTER TABLE users ADD COLUMN uuid CHAR(36) CHARACTER SET ascii NULL"))
        connection.execute(text("UPDATE users SET uuid=:uuid,updated_at=updated_at WHERE id=7"), {"uuid": existing_uuid})
        connection.commit()
        migration.apply_migration(connection, manifest)
        migration.validate_schema(connection, final=True)
        assert migration.inspect_user_data(connection, final=True) == 2
        migration.verify_preservation(connection, manifest)
        rows = connection.execute(text("SELECT id,uuid,is_legacy_kakao,nickname_key FROM users ORDER BY id")).all()
        assert rows[0][1] == existing_uuid
        assert rows[0][3] == b"Preserved profile"
        assert all(row[2] == 1 for row in rows)
        assert rows[1][3] is None
        migration.apply_migration(connection, manifest)
        assert connection.execute(text("SELECT id,uuid,is_legacy_kakao,nickname_key FROM users ORDER BY id")).all() == rows
        connection.execute(text("UPDATE legacy_meals SET menu_name='changed' WHERE id=11"))
        with pytest.raises(migration.MigrationError, match="Existing data changed"):
            migration.verify_preservation(connection, manifest)


def test_migration_stops_before_schema_changes_on_nickname_collision(mysql_engine):
    with mysql_engine.connect() as connection:
        old_schema(connection)
        connection.execute(text("UPDATE users SET nickname='Preserved profile' WHERE id=9"))
        connection.commit()
        with pytest.raises(migration.MigrationError, match="nicknames collide"):
            migration.inspect_user_data(connection)
        assert "uuid" not in {column["name"] for column in inspect(connection).get_columns("users")}


def test_migration_refuses_same_name_incompatible_schema(mysql_engine):
    with mysql_engine.connect() as connection:
        old_schema(connection)
        connection.execute(text("ALTER TABLE users ADD COLUMN uuid VARCHAR(36) NULL"))
        connection.commit()
        with pytest.raises(migration.MigrationError, match="Incompatible users.uuid"):
            migration.validate_schema(connection)


def test_migration_commands_require_maintenance_and_verify_saved_manifest(mysql_engine, tmp_path):
    with mysql_engine.connect() as connection:
        old_schema(connection)
    environment = {**os.environ, "DATABASE_URL": mysql_engine.url.render_as_string(hide_password=False)}
    manifest = str(tmp_path / "preservation.json")
    def run(*arguments):
        return subprocess.run([sys.executable, "-m", "scripts.migrate_uuid_auth", *arguments, "--manifest", manifest],
                              env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert run("--check").returncode == 0
    assert run("--apply").returncode == 2
    for arguments in [("--apply", "--maintenance-confirmed"), ("--verify",), ("--apply", "--maintenance-confirmed")]:
        result = run(*arguments)
        assert result.returncode == 0, result.stdout + result.stderr


def test_migration_resumes_missing_auth_indexes(mysql_engine, monkeypatch):
    monkeypatch.setattr(migration, "engine", mysql_engine)
    with mysql_engine.connect() as connection:
        old_schema(connection)
        manifest = migration.create_manifest(connection)
        migration.apply_migration(connection, manifest)
        connection.execute(text("DROP INDEX ix_auth_sessions_expires_id ON auth_sessions"))
        connection.commit()
        migration.validate_schema(connection)
        migration.apply_migration(connection, manifest)
        migration.validate_schema(connection, final=True)


def api_for_engine(engine):
    app = FastAPI()
    app.include_router(router)
    def get_test_db():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = get_test_db
    return app


def test_same_preparation_concurrent_http_requests_create_exactly_one_user(mysql_engine):
    Base.metadata.create_all(mysql_engine)
    with Session(mysql_engine) as db:
        record, secret = new_session()
        db.add(record)
        db.commit()
    app = api_for_engine(mysql_engine)
    barrier = threading.Barrier(2)
    def create():
        with TestClient(app, base_url="https://testserver", headers=WEB_HEADERS) as client:
            client.cookies.set(settings.AUTH_COOKIE_NAME, secret)
            barrier.wait(timeout=10)
            response = client.post("/api/auth/anonymous-users")
            return response.status_code, response.json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sorted(status for status, _ in results) == [200, 201], results
    assert results[0][1]["user"] == results[1][1]["user"]
    with Session(mysql_engine) as db:
        assert db.query(User).count() == db.query(AuthSession).count() == 1


def test_refresh_and_revocation_cannot_resurrect_a_session(mysql_engine):
    Base.metadata.create_all(mysql_engine)
    with Session(mysql_engine) as db:
        user = User(nickname="race", nickname_key=b"race")
        db.add(user)
        db.flush()
        record, secret = new_session(user.id)
        db.add(record)
        db.commit()
        session_id = record.id
    app = api_for_engine(mysql_engine)
    barrier = threading.Barrier(2)
    def refresh():
        with TestClient(app, base_url="https://testserver", headers=WEB_HEADERS) as client:
            client.cookies.set(settings.AUTH_COOKIE_NAME, secret)
            barrier.wait(timeout=10)
            return client.post("/api/auth/tokens").status_code
    def revoke():
        with Session(mysql_engine) as db:
            barrier.wait(timeout=10)
            revoke_session(db, session_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshing = pool.submit(refresh)
        revoking = pool.submit(revoke)
        assert refreshing.result(timeout=15) in {200, 401}
        revoking.result(timeout=15)
    with TestClient(app, base_url="https://testserver", headers=WEB_HEADERS) as client:
        client.cookies.set(settings.AUTH_COOKIE_NAME, secret)
        assert client.post("/api/auth/tokens").json()["detail"]["code"] == "SESSION_REVOKED"


def test_cleanup_rechecks_expiry_after_waiting_for_a_concurrent_renewal(mysql_engine):
    from datetime import timedelta
    from app.auth.sessions import cleanup_auth_sessions_once, utc_now
    Base.metadata.create_all(mysql_engine)
    with Session(mysql_engine) as db:
        record, _ = new_session()
        record.expires_at = utc_now() - timedelta(seconds=1)
        db.add(record)
        db.commit()
        session_id = record.id
    attempting_lock = threading.Event()
    def observe_lock(connection, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement.upper() and threading.current_thread() is not threading.main_thread():
            attempting_lock.set()
    event.listen(mysql_engine, "before_cursor_execute", observe_lock)
    try:
        with mysql_engine.connect() as renewing:
            transaction = renewing.begin()
            renewing.execute(text("SELECT id FROM auth_sessions WHERE id=:id FOR UPDATE"), {"id": session_id})
            renewing.execute(text("UPDATE auth_sessions SET expires_at=:future WHERE id=:id"),
                             {"future": utc_now() + timedelta(days=30), "id": session_id})
            def cleanup():
                with Session(mysql_engine) as db:
                    return cleanup_auth_sessions_once(db)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(cleanup)
                observed = attempting_lock.wait(timeout=5)
                transaction.commit()
                assert observed
                assert pending.result(timeout=10) == 0
        with Session(mysql_engine) as db:
            assert db.get(AuthSession, session_id) is not None
    finally:
        event.remove(mysql_engine, "before_cursor_execute", observe_lock)

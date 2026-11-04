import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.rooms.models import Room, RoomMember
from app.rooms.schemas import RoomCreateRequest
from app.rooms.service import create_room, join_room
from app.users.models import User


MYSQL_TEST_DATABASE_URL_ENV = "MYSQL_TEST_DATABASE_URL"


def get_mysql_test_database_url() -> str:
    database_url = os.getenv(MYSQL_TEST_DATABASE_URL_ENV)
    if not database_url:
        pytest.skip(
            f"Set {MYSQL_TEST_DATABASE_URL_ENV} to run the MySQL integration test"
        )

    parsed_url = make_url(database_url)
    if not parsed_url.drivername.startswith("mysql"):
        pytest.fail(f"{MYSQL_TEST_DATABASE_URL_ENV} must use a MySQL driver")
    if not parsed_url.database or "test" not in parsed_url.database.lower():
        pytest.fail(
            f"{MYSQL_TEST_DATABASE_URL_ENV} must point to a dedicated test database"
        )
    return database_url


def test_concurrent_join_does_not_exceed_capacity_on_mysql():
    database_url = get_mysql_test_database_url()
    engine = create_engine(
        database_url,
        isolation_level="REPEATABLE READ",
        pool_pre_ping=True,
    )
    testing_session = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    unique_suffix = uuid.uuid4().int % 9_000_000_000_000_000_000
    created_user_ids = []
    room_database_id = None

    setup_db = testing_session()
    try:
        users = [
            User(kakao_id=unique_suffix, nickname="mysql-host"),
            User(kakao_id=unique_suffix + 1, nickname="mysql-participant-1"),
            User(kakao_id=unique_suffix + 2, nickname="mysql-participant-2"),
        ]
        setup_db.add_all(users)
        setup_db.commit()
        for user in users:
            setup_db.refresh(user)
        created_user_ids = [user.id for user in users]

        room_response = create_room(
            db=setup_db,
            host=users[0],
            request=RoomCreateRequest(max_members=2),
        )
        room = (
            setup_db.query(Room)
            .filter(Room.public_id == room_response.room_id)
            .one()
        )
        room_database_id = room.id
    finally:
        setup_db.close()

    ready_to_join = threading.Barrier(2)

    def attempt_join(user_id: int):
        db = testing_session()
        try:
            # Match the real request flow: authentication performs a normal
            # read first and may establish a REPEATABLE READ snapshot.
            user = db.get(User, user_id)
            ready_to_join.wait(timeout=10)
            join_room(db=db, room_public_id=room_response.room_id, user=user)
            return status.HTTP_201_CREATED
        except HTTPException as exc:
            return exc.status_code
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(attempt_join, created_user_ids[1:])
            )

        assert sorted(results) == [
            status.HTTP_201_CREATED,
            status.HTTP_409_CONFLICT,
        ]

        verify_db = testing_session()
        try:
            member_count = (
                verify_db.query(RoomMember)
                .filter(RoomMember.room_id == room_database_id)
                .count()
            )
            assert member_count == 2
        finally:
            verify_db.close()
    finally:
        cleanup_db = testing_session()
        try:
            if room_database_id is not None:
                cleanup_db.query(RoomMember).filter(
                    RoomMember.room_id == room_database_id
                ).delete(synchronize_session=False)
                cleanup_db.query(Room).filter(
                    Room.id == room_database_id
                ).delete(synchronize_session=False)
            if created_user_ids:
                cleanup_db.query(User).filter(
                    User.id.in_(created_user_ids)
                ).delete(synchronize_session=False)
            cleanup_db.commit()
        finally:
            cleanup_db.close()
            engine.dispose()

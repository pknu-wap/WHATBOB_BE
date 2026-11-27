"""Isolated MySQL 8 migration, FK cascade, and independent-session concurrency checks for #43 (T02 ~ T07)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading

import pytest
from sqlalchemy import inspect, text

from app.core.database import Base
from app.menus.models import Menu, Recommendation, RecommendationItemSnapshot
from app.rooms.models import (
    Room,
    RoomRound,
    RoomRoundCandidate,
    RoomRoundCandidateProposer,
    RoomRoundMember,
    RoomRoundSearchOrigin,
)
from app.rooms.result_places import get_room_round_search_origin, set_room_round_search_origin
from app.rooms.rounds import (
    decide_room_round_departure,
    start_room_round,
    submit_room_round_order_sheet,
    submit_room_round_votes,
)
from app.rooms.schemas import (
    RoomCreateRequest,
    RoomDepartureDecisionRequest,
    RoomOrderSheetSubmitRequest,
    RoomRoundSearchCoordinates,
    RoomVoteSubmitRequest,
)
from app.rooms.service import close_room_by_host, create_room, delete_closed_room, join_room, leave_room_member
from app.users.models import User
from tests.test_group_rounds import build_answers_for_questions, seed_menus_and_questions
from tests.test_issue39_mysql_concurrency import mysql_env


def test_mysql_room_rounds_migration_check_apply_resume_verify(mysql_env, tmp_path):
    from scripts import migrate_room_rounds as migration

    engine = mysql_env["engine"]
    Base.metadata.create_all(engine)
    with mysql_env["SessionLocal"]() as db:
        host = User(nickname="round-mig-host")
        db.add(host)
        db.commit()
        create_room(db, host, RoomCreateRequest())

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE room_round_search_origins"))
        conn.execute(text("DROP TABLE room_round_candidate_proposers"))
        conn.execute(text("DROP TABLE room_round_candidates"))
        conn.execute(text("DROP TABLE room_round_members"))
        conn.execute(text("DROP TABLE room_rounds"))
        conn.execute(text("DROP TABLE room_notifications"))
        conn.execute(text("ALTER TABLE rooms DROP COLUMN state_version"))

    manifest_path = tmp_path / "round-manifest.json"
    environment = {
        **os.environ,
        "DATABASE_URL": engine.url.render_as_string(hide_password=False),
    }

    def command(*args):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.migrate_room_rounds",
                *args,
                "--manifest",
                str(manifest_path),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert command("--check").returncode == 0
    assert command("--apply").returncode != 0
    with engine.connect() as conn:
        baseline = migration.create_manifest(conn)
    manifest_path.write_text(json.dumps(baseline), encoding="utf-8")

    applied = command("--apply", "--maintenance-confirmed")
    assert applied.returncode == 0, applied.stderr
    assert command("--apply", "--maintenance-confirmed").returncode == 0
    assert command("--verify").returncode == 0

    with engine.connect() as conn:
        migration.verify_preservation(conn, baseline)
        inspector = inspect(conn)
        assert {
            "room_rounds",
            "room_round_members",
            "room_round_candidates",
            "room_round_candidate_proposers",
            "room_round_search_origins",
        } <= set(inspector.get_table_names())


def test_mysql_concurrent_final_submissions_and_votes_converge_to_single_result(mysql_env):
    engine = mysql_env["engine"]
    Base.metadata.create_all(engine)
    factory = mysql_env["SessionLocal"]

    with factory() as db:
        seed_menus_and_questions(db)
        users = [User(nickname=f"mysql-round-u{i}") for i in range(3)]
        db.add_all(users)
        db.commit()
        uids = [u.id for u in users]
        room_id = create_room(db, users[0], RoomCreateRequest(max_members=4)).room_id
        for uid in uids[1:]:
            join_room(db, room_id, db.get(User, uid))
        started, _, _ = start_room_round(db, room_id, db.get(User, uids[0]))
        round_id = started.round_id

        rms = {
            rm.user_id: list(rm.assigned_questions_json)
            for rm in db.query(RoomRoundMember).filter_by(round_id=round_id).all()
        }

    # Concurrent order sheet submissions from all 3 independent MySQL sessions
    barrier = threading.Barrier(3)

    def submit_sheet(idx: int):
        uid = uids[idx]
        answers = build_answers_for_questions(rms[uid], idx)
        with factory() as db:
            user = db.get(User, uid)
            barrier.wait(timeout=10)
            resp, _, _ = submit_room_round_order_sheet(
                db,
                room_id,
                user,
                RoomOrderSheetSubmitRequest(answers=answers, round_id=round_id),
            )
            return resp.round_status

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(submit_sheet, range(3)))

    assert any(s in ("VOTING", "RESULT") for s in statuses)
    with factory() as db:
        r_row = db.get(RoomRound, round_id)
        assert r_row.status in ("VOTING", "RESULT")
        assert db.query(Recommendation).count() == 3
        assert db.query(RecommendationItemSnapshot).count() >= 3

        # Ensure 2 candidates for concurrent voting test
        if r_row.status == "RESULT":
            r_row.status = "VOTING"
            r_row.is_uncontested = False
            r_row.vote_round_no = 1
            menus = db.query(Menu).order_by(Menu.id.asc()).limit(2).all()
            existing_cands = db.query(RoomRoundCandidate).filter_by(round_id=round_id).all()
            if len(existing_cands) < 2:
                rm_second = db.query(RoomRoundMember).filter_by(round_id=round_id, user_id=uids[1]).one()
                c2 = RoomRoundCandidate(
                    round_id=round_id,
                    menu_id=menus[1].id if existing_cands[0].menu_id == menus[0].id else menus[0].id,
                    display_order=2,
                    is_active=True,
                )
                db.add(c2)
                db.flush()
                db.add(
                    RoomRoundCandidateProposer(
                        candidate_id=c2.id,
                        round_member_id=rm_second.id,
                        user_id=rm_second.user_id,
                    )
                )
            db.commit()

        cids = [
            c.id
            for c in db.query(RoomRoundCandidate)
            .filter_by(round_id=round_id, is_active=True)
            .order_by(RoomRoundCandidate.id.asc())
            .all()
        ]

    vote_barrier = threading.Barrier(3)

    def submit_vote(idx: int):
        uid = uids[idx]
        chosen = (cids[:2], [cids[0]], [cids[1]])[idx]
        with factory() as db:
            user = db.get(User, uid)
            vote_barrier.wait(timeout=10)
            resp, _, _ = submit_room_round_votes(
                db,
                room_id,
                user,
                RoomVoteSubmitRequest(candidate_ids=chosen, vote_round_no=1, round_id=round_id),
            )
            return resp.round_status

    with ThreadPoolExecutor(max_workers=3) as pool:
        vote_statuses = list(pool.map(submit_vote, range(3)))

    assert vote_statuses.count("RESULT") == 1
    with factory() as db:
        r_row = db.get(RoomRound, round_id)
        assert r_row.status == "RESULT"
        winners = (
            db.query(RoomRoundCandidate)
            .filter_by(round_id=round_id, is_winner=True)
            .all()
        )
        assert len(winners) == 1
        assert winners[0].id in cids[:2]
        assert winners[0].final_vote_count == 2

        winner_menu_id = winners[0].menu_id

    coordinates = RoomRoundSearchCoordinates(latitude=35.13362239, longitude=129.10301127)
    registration_barrier = threading.Barrier(2)

    def register_location():
        with factory() as db:
            host = db.get(User, uids[0])
            registration_barrier.wait(timeout=10)
            return set_room_round_search_origin(db, room_id, round_id, host, coordinates)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_request = pool.submit(register_location)
        repeated_request = pool.submit(register_location)
        first = first_request.result(timeout=20)
        repeated = repeated_request.result(timeout=20)

    assert first == repeated
    assert first.common_state["current_round"]["nearby_location_ready"] is True
    with factory() as db:
        assert get_room_round_search_origin(db, room_id, round_id, db.get(User, uids[1])) == (
            winner_menu_id, coordinates,
        )
        assert db.query(RoomRoundSearchOrigin).count() == 1

        # Physical room deletion cascades round tables but preserves personal Recommendations
        close_room_by_host(db, room_id, db.get(User, uids[0]))
        delete_closed_room(db, room_id)
        assert db.query(RoomRound).count() == 0
        assert db.query(RoomRoundMember).count() == 0
        assert db.query(RoomRoundCandidate).count() == 0
        assert db.query(RoomRoundSearchOrigin).count() == 0
        assert db.query(Recommendation).count() == 3


def test_mysql_host_decision_and_additional_departure_race(mysql_env):
    from fastapi import HTTPException

    engine = mysql_env["engine"]
    Base.metadata.create_all(engine)
    factory = mysql_env["SessionLocal"]

    with factory() as db:
        seed_menus_and_questions(db)
        users = [User(nickname=f"mysql-pause-u{i}") for i in range(4)]
        db.add_all(users)
        db.commit()
        uids = [u.id for u in users]
        room_id = create_room(db, users[0], RoomCreateRequest(max_members=6)).room_id
        for uid in uids[1:]:
            join_room(db, room_id, db.get(User, uid))
        start_room_round(db, room_id, db.get(User, uids[0]))
        # First departure pauses round with decision_version == 1
        leave_room_member(db, room_id, uids[3], db.get(User, uids[3]))

    barrier = threading.Barrier(2)

    def host_decide():
        with factory() as db:
            host = db.get(User, uids[0])
            barrier.wait(timeout=10)
            try:
                resp, _ = decide_room_round_departure(
                    db,
                    room_id,
                    host,
                    RoomDepartureDecisionRequest(action="CONTINUE", decision_version=1),
                )
                return ("DECIDED", resp.status)
            except HTTPException as exc:
                return ("REJECTED", exc.status_code)

    def second_depart():
        with factory() as db:
            member2 = db.get(User, uids[2])
            barrier.wait(timeout=10)
            leave_room_member(db, room_id, uids[2], member2)
            return "LEFT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_host = pool.submit(host_decide)
        f_dep = pool.submit(second_depart)
        host_outcome = f_host.result(timeout=20)
        dep_outcome = f_dep.result(timeout=20)

    assert dep_outcome == "LEFT"
    with factory() as db:
        r_row = db.query(RoomRound).one()
        if host_outcome[0] == "REJECTED":
            assert host_outcome[1] == 409
            assert r_row.status == "ANSWERING_PAUSED"
            assert r_row.decision_version == 2
        else:
            # Host decision ran first, then second departure paused it again at decision_version == 2
            assert r_row.status == "ANSWERING_PAUSED"
            assert r_row.decision_version == 2

"""Comprehensive REST/WS and domain tests for #43 / #36 group rounds (T02 ~ T07)."""
from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.auth.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.rooms.models  # noqa: F401
import app.users.models  # noqa: F401
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import Base, get_db
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    QuestionSelectionState,
    Recommendation,
    RecommendationItemSnapshot,
    UserFoodExclusion,
)
from app.recommend.question_pool import build_candidate_question_pool
from app.rooms.models import (
    Room,
    RoomRound,
    RoomRoundCandidate,
    RoomRoundCandidateProposer,
    RoomRoundMember,
)
from app.rooms.router import operations_router, router as rooms_router
from app.rooms.service import expire_room_member_after_grace, recover_rooms_after_restart, utcnow
from app.rooms.websocket import router as rooms_ws_router
from app.users.models import User


def seed_menus_and_questions(db: Session) -> list[Menu]:
    menus = [
        Menu(
            food_code="M001",
            name="김치찌개",
            base_name="김치찌개",
            category_large="한식",
            category_medium="찌개",
            meal_type="main_meal",
            has_soup=True,
            cooking_style="soup_stew",
            main_ingredient="pork",
            flavor_intensity="spicy_bold",
            temperature="hot",
            heaviness="filling",
            kcal=650.0,
            protein_g=28.0,
            fat_g=22.0,
            carb_g=35.0,
            sodium_mg=900.0,
            sugar_g=4.0,
            origin_type="home_style",
            allergen_tags=["돼지고기", "대두"],
        ),
        Menu(
            food_code="M002",
            name="연어초밥",
            base_name="초밥",
            category_large="일식",
            category_medium="초밥",
            meal_type="main_meal",
            has_soup=False,
            cooking_style="rice",
            main_ingredient="seafood",
            flavor_intensity="mild_clean",
            temperature="cold",
            heaviness="light",
            kcal=520.0,
            protein_g=24.0,
            fat_g=10.0,
            carb_g=68.0,
            sodium_mg=420.0,
            sugar_g=6.0,
            origin_type="eating_out",
            allergen_tags=["생선"],
        ),
        Menu(
            food_code="M003",
            name="소고기쌀국수",
            base_name="쌀국수",
            category_large="아시안",
            category_medium="면류",
            meal_type="main_meal",
            has_soup=True,
            cooking_style="noodle",
            main_ingredient="beef",
            flavor_intensity="mild_clean",
            temperature="hot",
            heaviness="light",
            kcal=580.0,
            protein_g=26.0,
            fat_g=12.0,
            carb_g=72.0,
            sodium_mg=780.0,
            sugar_g=3.0,
            origin_type="eating_out",
            allergen_tags=["소고기"],
        ),
        Menu(
            food_code="M004",
            name="닭가슴살샐러드",
            base_name="샐러드",
            category_large="양식",
            category_medium="샐러드",
            meal_type="snack_drink",
            has_soup=False,
            cooking_style="salad",
            main_ingredient="poultry",
            flavor_intensity="mild_clean",
            temperature="cold",
            heaviness="light",
            kcal=320.0,
            protein_g=32.0,
            fat_g=8.0,
            carb_g=12.0,
            sodium_mg=180.0,
            sugar_g=2.0,
            origin_type="eating_out",
            allergen_tags=["닭고기"],
        ),
    ]
    db.add_all(menus)

    raw_pool = build_candidate_question_pool()
    seen_clusters: set[str] = set()
    rank = 0
    for q in raw_pool:
        if q["cluster_key"] in seen_clusters:
            continue
        seen_clusters.add(q["cluster_key"])
        rank += 1
        weight = round(1.0 - rank * 0.04, 4)
        db.add(
            OrderSheetQuestion(
                question_code=q["question_code"],
                cluster_key=q["cluster_key"],
                question_text=q["question_text"],
                options_json=q["options"],
                initial_score=weight,
                importance_weight=weight,
                is_active=True,
            )
        )
    db.commit()
    return menus


def build_group_env(member_count: int = 3):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    db = TestingSession()
    menus = seed_menus_and_questions(db)
    users = [
        User(kakao_id=1000 + idx, nickname=f"참여자{idx}")
        for idx in range(1, member_count + 1)
    ]
    db.add_all(users)
    db.commit()
    for u in users:
        db.refresh(u)

    state = {"current_user": users[0]}
    app = FastAPI()
    app.include_router(rooms_router)
    app.include_router(operations_router)
    app.include_router(rooms_ws_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: state["current_user"]

    client = TestClient(app, headers={"Origin": settings.FRONTEND_BASE_URL.rstrip("/")})

    def as_user(user: User):
        state["current_user"] = user
        return client

    created = as_user(users[0]).post("/api/rooms", json={"max_members": 8}).json()
    room_id = created["room_id"]
    for u in users[1:]:
        res = as_user(u).post(f"/api/rooms/{room_id}/join")
        assert res.status_code == 201

    return client, db, users, menus, room_id, as_user


def build_answers_for_questions(questions: list[dict], prefer_index: int = 0) -> list[dict]:
    # Spec #77 (#81): 하드필터(Q3 국물·Q8 밥/면/그 외·Q9 고기)는 자동 완화 없이 엄격 적용되므로
    # 테스트 시드 메뉴(M001 김치찌개 / M002 연어초밥 / M003 소고기쌀국수)와 모순되지 않는 조합을 선택한다.
    hard_filter_profiles = (
        {"has_soup": "soup_yes", "cooking_style": "other_style", "main_ingredient": "meat"},
        {"has_soup": "soup_no", "cooking_style": "rice", "main_ingredient": "non_meat"},
        {"has_soup": "soup_yes", "cooking_style": "noodle", "main_ingredient": "meat"},
    )
    profile = hard_filter_profiles[prefer_index % len(hard_filter_profiles)]
    answers = []
    for q in questions:
        opts = q["options"]
        cluster = q.get("cluster_key")
        preferred_opt_id = profile.get(cluster)
        if preferred_opt_id and any(opt["option_id"] == preferred_opt_id for opt in opts):
            chosen = preferred_opt_id
        else:
            non_any_opts = [opt for opt in opts if opt["option_id"] != "any"]
            pool = non_any_opts or opts
            chosen = pool[prefer_index % len(pool)]["option_id"]
        answers.append({"question_id": q["id"], "option_id": chosen})
    return answers


def test_t02_host_only_minimum_members_and_pure_question_get():
    client, db, users, _, room_id, as_user = build_group_env(member_count=2)
    host, member = users[0], users[1]

    # Non-host cannot start
    forbidden = as_user(member).post(f"/api/rooms/{room_id}/rounds/start")
    assert forbidden.status_code == 403

    # If member leaves so only 1 remains, host cannot start
    as_user(member).delete(f"/api/rooms/{room_id}/members/{member.id}")
    insufficient = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert insufficient.status_code == 409
    assert insufficient.json()["detail"]["code"] == "INSUFFICIENT_ROOM_MEMBERS"

    # Re-join and set legacy completion flag before start
    as_user(member).post(f"/api/rooms/{room_id}/join")
    legacy_complete = as_user(member).post(f"/api/rooms/{room_id}/order-sheet/complete")
    assert legacy_complete.status_code == 200

    # Host starts round
    started = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert started.status_code == 200
    body = started.json()
    assert body["status"] == "ANSWERING"
    assert body["room_status"] == "IN_PROGRESS"
    assert body["common_state"]["completed_member_count"] == 0
    assert body["common_state"]["current_round"]["status"] == "ANSWERING"
    assert len(body["viewer_state"]["assigned_questions"]) == 9

    # Duplicate start by host is idempotent and does not bump state_version
    version_after_start = body["common_state"]["state_version"]
    dup_start = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert dup_start.status_code == 200
    assert dup_start.json()["common_state"]["state_version"] == version_after_start

    # Outsider cannot join after round started
    outsider = User(kakao_id=9999, nickname="외부인")
    db.add(outsider)
    db.commit()
    join_after_start = as_user(outsider).post(f"/api/rooms/{room_id}/join")
    assert join_after_start.status_code == 409

    # Pure GET questions does not mutate shown_count or QuestionSelectionState on repeat calls
    q_before = {
        q.id: q.shown_count for q in db.query(OrderSheetQuestion).all()
    }
    state_before = (
        db.query(QuestionSelectionState)
        .filter_by(scope_key=f"user:{member.id}")
        .one()
        .topic_counts
    )
    q1 = as_user(member).get(f"/api/rooms/{room_id}/rounds/current/questions")
    q2 = as_user(member).get(f"/api/rooms/{room_id}/rounds/current/questions")
    assert q1.status_code == 200
    assert q1.headers.get("cache-control") == "no-store"
    assert q1.json() == q2.json()
    assert q1.json()["total_count"] == 9
    assert q1.json()["exploitation_count"] == 7
    assert q1.json()["exploration_count"] == 2
    q_after = {
        q.id: q.shown_count for q in db.query(OrderSheetQuestion).all()
    }
    state_after = (
        db.query(QuestionSelectionState)
        .filter_by(scope_key=f"user:{member.id}")
        .one()
        .topic_counts
    )
    assert q_before == q_after
    assert state_before == state_after


def test_t02_fail_fast_when_shared_exclusions_eliminate_all_menus():
    _, db, users, _, room_id, as_user = build_group_env(member_count=2)
    host, member = users[0], users[1]
    for kw in ("김치찌개", "초밥", "쌀국수", "샐러드"):
        db.add(
            UserFoodExclusion(
                user_id=member.id,
                exclusion_type="DISLIKE",
                keyword=kw,
            )
        )
    db.commit()

    res = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "NO_AVAILABLE_MENUS"

    detail = as_user(host).get(f"/api/rooms/{room_id}").json()
    assert detail["status"] == "WAITING"
    assert detail["current_round"]["status"] == "CANCELLED"
    assert detail["current_round"]["cancel_reason"] == "NO_AVAILABLE_MENUS"


def test_t03_d20_and_t04_full_journey_with_cumulative_recommendations_and_co_winners():
    _, db, users, _, room_id, as_user = build_group_env(member_count=3)
    host, m1, m2 = users[0], users[1], users[2]

    # Exclude 샐러드 for m2 before start; snapshot freezes this exclusion
    db.add(UserFoodExclusion(user_id=m2.id, exclusion_type="DISLIKE", keyword="샐러드"))
    db.commit()

    start_res = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert start_res.status_code == 200

    # Changing m1's profile exclusions AFTER start must not affect this round's snapshot
    db.add(UserFoodExclusion(user_id=m1.id, exclusion_type="DISLIKE", keyword="김치찌개"))
    db.commit()

    # Legacy no-answer completion endpoint is blocked during active round
    legacy_res = as_user(host).post(f"/api/rooms/{room_id}/order-sheet/complete")
    assert legacy_res.status_code == 409
    assert legacy_res.json()["detail"]["code"] == "ROUND_ORDER_SHEET_ANSWERS_REQUIRED"

    host_qs = as_user(host).get(f"/api/rooms/{room_id}/rounds/current/questions").json()["questions"]
    m1_qs = as_user(m1).get(f"/api/rooms/{room_id}/rounds/current/questions").json()["questions"]
    m2_qs = as_user(m2).get(f"/api/rooms/{room_id}/rounds/current/questions").json()["questions"]

    host_answers = build_answers_for_questions(host_qs, prefer_index=0)
    m1_answers = build_answers_for_questions(m1_qs, prefer_index=1)
    m2_answers = build_answers_for_questions(m2_qs, prefer_index=2)

    # D20: extra_exclusions or exclusions in request body are forbidden (422)
    d20_rejected = as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": host_answers, "exclusions": ["초밥"]},
    )
    assert d20_rejected.status_code == 422

    # Submit host and m1 order sheets
    s_host = as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": host_answers},
    )
    assert s_host.status_code == 200
    assert s_host.json()["submitted_member_count"] == 1
    assert s_host.json()["common_state"]["current_round"]["candidates"] == []
    assert "exclusion_snapshot_json" not in str(s_host.json())

    # Force distinct top menus on host and m1, and duplicate on m2 to verify exact-menu merging + tie voting
    rm_host = db.query(RoomRoundMember).filter_by(user_id=host.id).one()
    rm_host.top_menu_id = db.query(Menu).filter_by(food_code="M001").one().id
    db.commit()

    s_m1 = as_user(m1).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": m1_answers},
    )
    assert s_m1.status_code == 200
    rm_m1 = db.query(RoomRoundMember).filter_by(user_id=m1.id).one()
    rm_m1.top_menu_id = db.query(Menu).filter_by(food_code="M002").one().id
    db.commit()

    # Final submission by m2 triggers candidate deduplication and transitions to VOTING
    s_m2 = as_user(m2).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": m2_answers},
    )
    assert s_m2.status_code == 200
    round_state = s_m2.json()["common_state"]["current_round"]
    assert round_state["status"] == "VOTING"
    assert round_state["vote_round_no"] == 1
    cands = round_state["candidates"]
    assert len(cands) >= 2
    assert all(c["vote_count"] is None and c["is_winner"] is False for c in cands)

    # T03-D03: Personal Recommendation & RecommendationItemSnapshot rows were created for all 3 users
    assert db.query(Recommendation).count() == 3
    assert db.query(RecommendationItemSnapshot).count() >= 3

    # Same-answer replay after entering VOTING succeeds without new Recommendation or version bump
    ver_before = s_m2.json()["common_state"]["state_version"]
    replay_m2 = as_user(m2).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": list(reversed(m2_answers))},
    )
    assert replay_m2.status_code == 200
    assert replay_m2.json()["common_state"]["state_version"] == ver_before
    assert db.query(Recommendation).count() == 3

    # Modified answer replay is rejected with 409
    changed = list(m2_answers)
    changed[0] = {"question_id": changed[0]["question_id"], "option_id": "any"}
    if changed != m2_answers:
        mod_res = as_user(m2).post(
            f"/api/rooms/{room_id}/rounds/current/order-sheet",
            json={"answers": changed},
        )
        assert mod_res.status_code == 409

    # T04: Multiple-choice voting & co-winners
    cid1, cid2 = cands[0]["candidate_id"], cands[1]["candidate_id"]
    # Empty vote and duplicate candidate ID are rejected
    assert (
        as_user(host)
        .post(f"/api/rooms/{room_id}/rounds/current/votes", json={"candidate_ids": []})
        .status_code
        == 400
    )
    assert (
        as_user(host)
        .post(
            f"/api/rooms/{room_id}/rounds/current/votes",
            json={"candidate_ids": [cid1, cid1]},
        )
        .status_code
        == 400
    )

    # Host votes [cid1, cid2], m1 votes [cid1], m2 votes [cid2] -> cid1=2, cid2=2 co-winners!
    v_host = as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/votes",
        json={"candidate_ids": [cid2, cid1], "vote_round_no": 1},
    )
    assert v_host.status_code == 200
    assert v_host.json()["voted_member_count"] == 1
    assert all(
        c["vote_count"] is None
        for c in v_host.json()["common_state"]["current_round"]["candidates"]
    )

    v_m1 = as_user(m1).post(
        f"/api/rooms/{room_id}/rounds/current/votes",
        json={"candidate_ids": [cid1], "vote_round_no": 1},
    )
    assert v_m1.status_code == 200

    v_m2 = as_user(m2).post(
        f"/api/rooms/{room_id}/rounds/current/votes",
        json={"candidate_ids": [cid2], "vote_round_no": 1},
    )
    assert v_m2.status_code == 200
    final_round = v_m2.json()["common_state"]["current_round"]
    assert final_round["status"] == "RESULT"
    assert final_round["is_uncontested"] is False
    winners = final_round["winners"]
    assert {w["candidate_id"] for w in winners} == {cid1, cid2}
    assert all(w["final_vote_count"] == 2 and w["is_winner"] is True for w in winners)
    assert "voted_candidate_ids" not in str(v_m2.json()["common_state"])


def test_t05_answering_departure_pause_version_token_and_host_continue_or_cancel():
    _, db, users, _, room_id, as_user = build_group_env(member_count=3)
    host, m1, m2 = users[0], users[1], users[2]

    as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    host_qs = as_user(host).get(f"/api/rooms/{room_id}/rounds/current/questions").json()["questions"]
    m1_qs = as_user(m1).get(f"/api/rooms/{room_id}/rounds/current/questions").json()["questions"]

    # Host submits before m2 departs
    as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": build_answers_for_questions(host_qs, 0)},
    )

    # m2 leaves during ANSWERING -> round pauses with decision_version == 1
    leave_res = as_user(m2).delete(f"/api/rooms/{room_id}/members/{m2.id}")
    assert leave_res.status_code == 200

    detail = as_user(host).get(f"/api/rooms/{room_id}").json()
    assert detail["current_round"]["status"] == "ANSWERING_PAUSED"
    assert detail["current_round"]["decision_version"] == 1
    assert detail["viewer_state"]["requires_host_decision"] is True
    assert detail["current_round"]["pending_departed_members"][0]["user_id"] == m2.id

    # Submissions are blocked while paused
    blocked = as_user(m1).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": build_answers_for_questions(m1_qs, 1)},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "ROUND_PAUSED_FOR_HOST_DECISION"

    # Stale decision_version is rejected
    stale = as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/departure-decision",
        json={"action": "CONTINUE", "decision_version": 99},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_DECISION_VERSION"

    # Host chooses CONTINUE -> round resumes ANSWERING and m1 can submit
    cont = as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/departure-decision",
        json={"action": "CONTINUE", "decision_version": 1},
    )
    assert cont.status_code == 200
    assert cont.json()["status"] == "ANSWERING"

    # Force identical top_menu_id for host and m1 so m1's submission triggers single-candidate uncontested RESULT!
    menu_id = db.query(Menu).filter_by(food_code="M001").one().id
    rm_host = db.query(RoomRoundMember).filter_by(user_id=host.id).one()
    rm_host.top_menu_id = menu_id
    db.commit()

    s_m1 = as_user(m1).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": build_answers_for_questions(m1_qs, 0)},
    )
    assert s_m1.status_code == 200


def test_t06_voting_departure_candidate_cleanup_revote_and_single_candidate_win():
    _, db, users, _, room_id, as_user = build_group_env(member_count=3)
    host, m1, m2 = users[0], users[1], users[2]

    as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    for idx, u in enumerate(users):
        qs = as_user(u).get(f"/api/rooms/{room_id}/rounds/current/questions").json()["questions"]
        as_user(u).post(
            f"/api/rooms/{room_id}/rounds/current/order-sheet",
            json={"answers": build_answers_for_questions(qs, idx)},
        )

    # Ensure 3 distinct candidates (one per user) in VOTING
    current_round = db.query(RoomRound).one()
    current_round.status = "VOTING"
    current_round.vote_round_no = 1
    current_round.is_uncontested = False
    db.query(RoomRoundCandidateProposer).delete()
    db.query(RoomRoundCandidate).delete()
    db.flush()

    menus = db.query(Menu).order_by(Menu.id.asc()).limit(3).all()
    rms = db.query(RoomRoundMember).order_by(RoomRoundMember.id.asc()).all()
    cands = []
    for idx, (rm, menu) in enumerate(zip(rms, menus), start=1):
        rm.top_menu_id = menu.id
        c = RoomRoundCandidate(
            round_id=current_round.id,
            menu_id=menu.id,
            display_order=idx,
            is_active=True,
            created_at=utcnow(),
        )
        db.add(c)
        db.flush()
        db.add(
            RoomRoundCandidateProposer(
                candidate_id=c.id,
                round_member_id=rm.id,
                user_id=rm.user_id,
            )
        )
        cands.append(c)
    db.commit()

    # Host votes in vote_round_no=1 before m2 departs via 60s grace expiration
    v1 = as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/votes",
        json={"candidate_ids": [cands[2].id], "vote_round_no": 1},
    )
    assert v1.status_code == 200

    # Simulate m2's 60s disconnect grace expiring
    from app.rooms.models import RoomMember
    m2_member = db.query(RoomMember).filter_by(user_id=m2.id).one()
    m2_member.disconnected_at = utcnow() - timedelta(seconds=61)
    db.commit()
    expired = expire_room_member_after_grace(db, room_id, m2.id, now=utcnow())
    assert expired is not None

    detail = as_user(host).get(f"/api/rooms/{room_id}").json()
    assert detail["current_round"]["status"] == "VOTING_PAUSED"
    assert detail["current_round"]["decision_version"] == 1

    # Host continues -> m2's sole candidate (cands[2]) is deactivated, vote_round_no becomes 2, host's previous vote is cleared!
    resumed = as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/departure-decision",
        json={"action": "CONTINUE", "decision_version": 1},
    )
    assert resumed.status_code == 200
    r_state = resumed.json()["common_state"]["current_round"]
    assert r_state["status"] == "VOTING"
    assert r_state["vote_round_no"] == 2
    assert r_state["voted_member_count"] == 0
    assert [c["candidate_id"] for c in r_state["candidates"]] == [cands[0].id, cands[1].id]
    assert resumed.json()["viewer_state"]["has_voted"] is False

    # Delayed vote from vote_round_no=1 is fenced off with 409 STALE_VOTE_ROUND
    stale_vote = as_user(m1).post(
        f"/api/rooms/{room_id}/rounds/current/votes",
        json={"candidate_ids": [cands[0].id], "vote_round_no": 1},
    )
    assert stale_vote.status_code == 409
    assert stale_vote.json()["detail"]["code"] == "STALE_VOTE_ROUND"

    # Voting for deactivated candidate cands[2] in round 2 is rejected with 409 CANDIDATE_INACTIVE
    inactive_vote = as_user(m1).post(
        f"/api/rooms/{room_id}/rounds/current/votes",
        json={"candidate_ids": [cands[2].id], "vote_round_no": 2},
    )
    assert inactive_vote.status_code == 409
    assert inactive_vote.json()["detail"]["code"] == "CANDIDATE_INACTIVE"


def test_t07_restart_recovery_and_room_delete_preserves_personal_recommendations():
    _, db, users, _, room_id, as_user = build_group_env(member_count=2)
    host, member = users[0], users[1]

    as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    host_qs = as_user(host).get(f"/api/rooms/{room_id}/rounds/current/questions").json()["questions"]
    as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": build_answers_for_questions(host_qs, 0)},
    )
    assert db.query(Recommendation).filter_by(user_id=host.id).count() == 1

    # Server restart recovery preserves round state and does not extend existing disconnected_at
    from app.rooms.models import RoomMember
    m_row = db.query(RoomMember).filter_by(user_id=member.id).one()
    fixed_disc = utcnow() - timedelta(seconds=20)
    m_row.disconnected_at = fixed_disc
    db.commit()

    recover_rooms_after_restart(db, now=utcnow())
    db.refresh(m_row)
    assert m_row.disconnected_at.replace(tzinfo=None) == fixed_disc.replace(tzinfo=None)

    recovered = as_user(host).get(f"/api/rooms/{room_id}").json()
    assert recovered["current_round"]["status"] == "ANSWERING"
    assert recovered["viewer_state"]["has_submitted_order_sheet"] is True

    # Host deletes room -> round rows are cleaned up, but personal Recommendation & snapshots survive!
    del_res = as_user(host).delete(f"/api/rooms/{room_id}")
    assert del_res.status_code == 200
    assert db.query(Room).count() == 0
    assert db.query(RoomRound).count() == 0
    assert db.query(RoomRoundMember).count() == 0
    assert db.query(Recommendation).filter_by(user_id=host.id).count() == 1
    assert db.query(RecommendationItemSnapshot).filter_by(user_id=host.id).count() >= 1

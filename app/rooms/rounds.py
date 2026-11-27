"""Group menu selection & voting round engine (T02 ~ T07)."""
from __future__ import annotations

from datetime import datetime
from random import choice
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.auth.operations import lock_user_and_verify_session
from app.core.config import settings
from app.menus.display import format_menu_name
from app.menus.food_kinds import resolve_menu_illustration_url
from app.menus.models import FoodKind, Menu, MenuIllustrationException
from app.recommend.common import (
    InvalidOrderSheetException,
    NoAvailableFoodException,
    QuestionPoolEmptyException,
    collect_user_exclusion_keywords,
    filter_available_menus,
)
from app.recommend.common_question_experiments import load_common_question_overrides
from app.recommend.no_results import suggest_no_result_answer_changes
from app.recommend.order_sheet import (
    create_order_sheet_recommendation,
    load_order_sheet_question_pool,
    select_order_sheet_questions,
)
from app.recommend.question_experiments import apply_question_overrides
from app.recommend.schemas import (
    AnswerItemRequest,
    OrderSheetNoResultsResponse,
    OrderSheetQuestionDTO,
    OrderSheetRecommendRequest,
)
from app.rooms.departures import lock_order_sheets, lock_room, lock_room_members
from app.rooms.models import (
    Room,
    RoomMember,
    RoomMemberOrderSheet,
    RoomRound,
    RoomRoundCandidate,
    RoomRoundCandidateProposer,
    RoomRoundMember,
    RoomRoundSearchOrigin,
)
from app.rooms.schemas import (
    RoomDepartureDecisionRequest,
    RoomOrderSheetNoResultsRequest,
    RoomOrderSheetSubmitRequest,
    RoomRoundCommandResponse,
    RoomRoundOrderSheetSubmitResponse,
    RoomRoundQuestionsResponse,
    RoomRoundVoteSubmitResponse,
    RoomVoteSubmitRequest,
)
from app.rooms.state import mark_room_changed, now_utc, response_common_state, stamp
from app.rooms.transactions import commit_room_transaction, current_read_session, room_transaction
from app.users.models import User

ACTIVE_ROUND_STATUSES = ("ANSWERING", "ANSWERING_PAUSED", "VOTING", "VOTING_PAUSED")
PAUSED_ROUND_STATUSES = ("ANSWERING_PAUSED", "VOTING_PAUSED")


def get_latest_round(
    db: Session,
    room_id: int,
    *,
    for_update: bool = False,
) -> RoomRound | None:
    query = (
        db.query(RoomRound)
        .filter(RoomRound.room_id == room_id)
        .order_by(RoomRound.round_no.desc(), RoomRound.id.desc())
    )
    if for_update:
        query = query.populate_existing().with_for_update()
    return query.first()


def lock_round_members(db: Session, round_id: int) -> list[RoomRoundMember]:
    return (
        db.query(RoomRoundMember)
        .filter(RoomRoundMember.round_id == round_id)
        .order_by(RoomRoundMember.id.asc())
        .populate_existing()
        .with_for_update()
        .all()
    )


def lock_round_candidates(db: Session, round_id: int) -> list[RoomRoundCandidate]:
    return (
        db.query(RoomRoundCandidate)
        .filter(RoomRoundCandidate.round_id == round_id)
        .order_by(RoomRoundCandidate.display_order.asc(), RoomRoundCandidate.id.asc())
        .populate_existing()
        .with_for_update()
        .all()
    )


def lock_round_proposers(db: Session, candidate_ids: list[int]) -> list[RoomRoundCandidateProposer]:
    if not candidate_ids:
        return []
    return (
        db.query(RoomRoundCandidateProposer)
        .filter(RoomRoundCandidateProposer.candidate_id.in_(candidate_ids))
        .order_by(RoomRoundCandidateProposer.id.asc())
        .populate_existing()
        .with_for_update()
        .all()
    )


def delete_room_rounds_for_rooms(db: Session, room_ids: list[int]) -> None:
    """Delete all round progress rows for the given rooms without touching personal Recommendations."""
    if not room_ids:
        return
    round_ids = [
        int(r[0])
        for r in db.query(RoomRound.id).filter(RoomRound.room_id.in_(room_ids)).all()
    ]
    if not round_ids:
        return
    candidate_ids = [
        int(c[0])
        for c in db.query(RoomRoundCandidate.id)
        .filter(RoomRoundCandidate.round_id.in_(round_ids))
        .all()
    ]
    if candidate_ids:
        db.query(RoomRoundCandidateProposer).filter(
            RoomRoundCandidateProposer.candidate_id.in_(candidate_ids)
        ).delete(synchronize_session=False)
        db.query(RoomRoundCandidate).filter(
            RoomRoundCandidate.id.in_(candidate_ids)
        ).delete(synchronize_session=False)
    db.query(RoomRoundMember).filter(
        RoomRoundMember.round_id.in_(round_ids)
    ).delete(synchronize_session=False)
    db.query(RoomRoundSearchOrigin).filter(
        RoomRoundSearchOrigin.round_id.in_(round_ids)
    ).delete(synchronize_session=False)
    db.query(RoomRound).filter(
        RoomRound.id.in_(round_ids)
    ).delete(synchronize_session=False)


def _reset_room_order_sheet_flags(db: Session, room_id: int) -> None:
    members = lock_room_members(db, room_id)
    for sheet in lock_order_sheets(db, [m.id for m in members]):
        sheet.is_completed = False
        sheet.completed_at = None


def apply_round_member_departures(
    db: Session,
    room: Room,
    departed_user_ids: list[int] | tuple[int, ...],
    *,
    remaining_sheets: list[RoomMemberOrderSheet] | None = None,
) -> str | None:
    """Apply T05/T06 round pause or minimum-member cancellation on confirmed member departure."""
    if not departed_user_ids:
        return None
    current_round = get_latest_round(db, room.id, for_update=True)
    if current_round is None or current_round.status not in ACTIVE_ROUND_STATUSES:
        return None

    now = now_utc()
    departed_set = {int(uid) for uid in departed_user_ids}
    round_members = lock_round_members(db, current_round.id)
    marked_any = False
    for rm in round_members:
        if rm.user_id in departed_set and rm.member_status == "ACTIVE":
            rm.member_status = "LEFT_PENDING"
            rm.left_at = now
            marked_any = True

    if not marked_any:
        return None

    active_members = [rm for rm in round_members if rm.member_status == "ACTIVE"]
    if len(active_members) < 2:
        for rm in round_members:
            if rm.member_status == "LEFT_PENDING":
                rm.member_status = "LEFT_CONFIRMED"
        current_round.status = "CANCELLED"
        current_round.cancel_reason = "INSUFFICIENT_MEMBERS"
        current_round.cancelled_at = now
        room.status = "WAITING"
        if remaining_sheets is not None:
            for sheet in remaining_sheets:
                sheet.is_completed = False
                sheet.completed_at = None
        else:
            _reset_room_order_sheet_flags(db, room.id)
        db.flush()
        return "ROUND_CANCELLED_INSUFFICIENT_MEMBERS"

    if current_round.status in ("ANSWERING", "ANSWERING_PAUSED"):
        current_round.status = "ANSWERING_PAUSED"
    else:
        current_round.status = "VOTING_PAUSED"
    current_round.decision_version = int(current_round.decision_version or 0) + 1
    db.flush()
    return "ROUND_PAUSED_MEMBER_LEFT"


def _finalize_candidates_and_advance_after_answering(
    db: Session,
    room: Room,
    current_round: RoomRound,
    active_round_members: list[RoomRoundMember],
    actor_user_id: int,
) -> None:
    """Merge active members' top_menu_id into exact-menu candidates and advance to VOTING or RESULT."""
    now = now_utc()
    existing_cands = lock_round_candidates(db, current_round.id)
    if existing_cands:
        existing_cand_ids = [c.id for c in existing_cands]
        db.query(RoomRoundCandidateProposer).filter(
            RoomRoundCandidateProposer.candidate_id.in_(existing_cand_ids)
        ).delete(synchronize_session=False)
        db.query(RoomRoundCandidate).filter(
            RoomRoundCandidate.id.in_(existing_cand_ids)
        ).delete(synchronize_session=False)
        db.flush()

    ordered_members = sorted(active_round_members, key=lambda rm: (rm.id, rm.user_id))
    proposers_by_menu: dict[int, list[RoomRoundMember]] = {}
    menu_order: list[int] = []
    for rm in ordered_members:
        if rm.top_menu_id is None:
            continue
        menu_id = int(rm.top_menu_id)
        if menu_id not in proposers_by_menu:
            proposers_by_menu[menu_id] = []
            menu_order.append(menu_id)
        proposers_by_menu[menu_id].append(rm)

    if not menu_order:
        current_round.status = "CANCELLED"
        current_round.cancel_reason = "NO_AVAILABLE_MENUS"
        current_round.cancelled_at = now
        room.status = "WAITING"
        _reset_room_order_sheet_flags(db, room.id)
        db.flush()
        mark_room_changed(db, room, "ROUND_CANCELLED_NO_MENUS", actor_user_id)
        return

    created_candidates: list[RoomRoundCandidate] = []
    for idx, menu_id in enumerate(menu_order, start=1):
        cand = RoomRoundCandidate(
            round_id=current_round.id,
            menu_id=menu_id,
            display_order=idx,
            is_active=True,
            final_vote_count=None,
            is_winner=(len(menu_order) == 1),
            created_at=now,
        )
        db.add(cand)
        db.flush()
        for rm in proposers_by_menu[menu_id]:
            db.add(
                RoomRoundCandidateProposer(
                    candidate_id=cand.id,
                    round_member_id=rm.id,
                    user_id=rm.user_id,
                )
            )
        created_candidates.append(cand)

    db.flush()
    if len(created_candidates) == 1:
        current_round.is_uncontested = True
        current_round.status = "RESULT"
        current_round.completed_at = now
        mark_room_changed(db, room, "ROUND_RESULT_FINALIZED", actor_user_id)
    else:
        current_round.is_uncontested = False
        current_round.status = "VOTING"
        current_round.vote_round_no = 1
        mark_room_changed(db, room, "VOTING_STARTED", actor_user_id)
    db.flush()


def build_round_common_state(
    db: Session,
    room: Room,
    users_by_id: dict[int, User] | None = None,
) -> dict[str, Any] | None:
    """Build the public/common snapshot of the latest round without exposing private answers/exclusions/ballots."""
    # Use current reads so the round state matches the locked room version.
    current_round = get_latest_round(db, room.id, for_update=True)
    if current_round is None:
        return None

    round_members = lock_round_members(db, current_round.id)
    needed_uids = {rm.user_id for rm in round_members}
    user_map = dict(users_by_id or {})
    missing_uids = needed_uids - set(user_map.keys())
    if missing_uids:
        with current_read_session(db) as read_db:
            for u in read_db.query(User).filter(User.id.in_(missing_uids)).all():
                user_map[u.id] = u

    active_members = [rm for rm in round_members if rm.member_status == "ACTIVE"]
    pending_departed = [rm for rm in round_members if rm.member_status == "LEFT_PENDING"]
    submitted_count = sum(1 for rm in active_members if rm.submitted_at is not None)
    voted_count = sum(1 for rm in active_members if rm.voted_at is not None)
    active_count = len(active_members)
    active_uids = {rm.user_id for rm in active_members}

    pending_departed_dtos = [
        {
            "user_id": rm.user_id,
            "nickname": user_map[rm.user_id].nickname if rm.user_id in user_map else None,
            "left_at": stamp(rm.left_at),
        }
        for rm in pending_departed
    ]

    candidate_dtos: list[dict[str, Any]] = []
    winner_dtos: list[dict[str, Any]] = []
    if current_round.status in ("VOTING", "VOTING_PAUSED", "RESULT"):
        candidates = [c for c in lock_round_candidates(db, current_round.id) if c.is_active]
        cand_ids = [c.id for c in candidates]
        menu_ids = [c.menu_id for c in candidates]
        proposers = lock_round_proposers(db, cand_ids)
        menu_rows = (
            db.query(Menu, FoodKind.representative_image_url, MenuIllustrationException.menu_id)
            .outerjoin(FoodKind, Menu.food_kind_id == FoodKind.id)
            .outerjoin(MenuIllustrationException, Menu.id == MenuIllustrationException.menu_id)
            .filter(Menu.id.in_(menu_ids))
            .all()
            if menu_ids
            else []
        )
        menus_by_id = {
            menu.id: (menu, representative_url, exception_menu_id)
            for menu, representative_url, exception_menu_id in menu_rows
        }
        proposers_by_cand: dict[int, list[int]] = {}
        for p in proposers:
            if p.user_id in active_uids:
                proposers_by_cand.setdefault(p.candidate_id, []).append(p.user_id)

        is_result = current_round.status == "RESULT"
        for c in candidates:
            menu, representative_url, exception_menu_id = menus_by_id.get(c.menu_id, (None, None, None))
            prop_uids = proposers_by_cand.get(c.id, [])
            vote_count_val = (
                (None if current_round.is_uncontested else int(c.final_vote_count or 0))
                if is_result
                else None
            )
            dto = {
                "candidate_id": c.id,
                "menu_id": c.menu_id,
                "food_code": menu.food_code if menu else None,
                "name": format_menu_name(menu.name) if menu else None,
                "image_url": resolve_menu_illustration_url(
                    representative_url, use_common_fallback=exception_menu_id is not None
                ),
                "base_name": menu.base_name if menu else None,
                "category_large": menu.category_large if menu else None,
                "category_medium": menu.category_medium if menu else None,
                "display_order": c.display_order,
                "proposer_user_ids": prop_uids,
                "proposer_count": len(prop_uids),
                "vote_count": vote_count_val,
                "final_vote_count": vote_count_val,
                "is_winner": bool(c.is_winner) if is_result else False,
                "is_uncontested": bool(current_round.is_uncontested) if is_result else False,
            }
            candidate_dtos.append(dto)
            if is_result and c.is_winner:
                winner_dtos.append(dto)

    return {
        "round_id": current_round.id,
        "round_no": current_round.round_no,
        "status": current_round.status,
        "phase": current_round.status,
        "vote_round_no": current_round.vote_round_no,
        "decision_version": current_round.decision_version,
        "is_uncontested": bool(current_round.is_uncontested),
        "cancel_reason": current_round.cancel_reason,
        "started_at": stamp(current_round.started_at),
        "completed_at": stamp(current_round.completed_at),
        "cancelled_at": stamp(current_round.cancelled_at),
        "active_member_count": active_count,
        "submitted_member_count": submitted_count,
        "all_orders_submitted": bool(active_count > 0 and submitted_count == active_count),
        "voted_member_count": voted_count,
        "all_votes_submitted": bool(
            current_round.status == "RESULT"
            and not current_round.is_uncontested
            and active_count > 0
            and voted_count == active_count
        ),
        "pending_departed_members": pending_departed_dtos,
        "candidates": candidate_dtos,
        "winners": winner_dtos,
        "nearby_location_ready": len(winner_dtos) == 1 and db.get(
            RoomRoundSearchOrigin, current_round.id, populate_existing=True, with_for_update=True
        ) is not None,
    }


def build_viewer_state(
    db: Session,
    room: Room,
    viewer_user_id: int,
    *,
    is_member: bool,
    my_role: str | None,
) -> dict[str, Any] | None:
    """Build T07-D01 unified viewer-specific recovery snapshot (never broadcast to other members)."""
    if not is_member:
        return None
    current_round = get_latest_round(db, room.id, for_update=False)
    if current_round is None:
        return {
            "user_id": viewer_user_id,
            "role": my_role,
            "is_host": room.host_user_id == viewer_user_id,
            "round_id": None,
            "round_no": None,
            "round_status": None,
            "vote_round_no": None,
            "decision_version": None,
            "has_submitted_order_sheet": False,
            "submitted_at": None,
            "has_voted": False,
            "voted_at": None,
            "voted_candidate_ids": [],
            "assigned_questions": [],
            "requires_host_decision": False,
        }

    my_rm = (
        db.query(RoomRoundMember)
        .filter(
            RoomRoundMember.round_id == current_round.id,
            RoomRoundMember.user_id == viewer_user_id,
        )
        .one_or_none()
    )
    has_submitted = bool(my_rm and my_rm.submitted_at is not None)
    has_voted = bool(my_rm and my_rm.voted_at is not None)
    show_questions = current_round.status in ("ANSWERING", "ANSWERING_PAUSED") and my_rm is not None
    return {
        "user_id": viewer_user_id,
        "role": my_role,
        "is_host": room.host_user_id == viewer_user_id,
        "round_id": current_round.id,
        "round_no": current_round.round_no,
        "round_status": current_round.status,
        "vote_round_no": current_round.vote_round_no,
        "decision_version": current_round.decision_version,
        "has_submitted_order_sheet": has_submitted,
        "submitted_at": stamp(my_rm.submitted_at) if my_rm else None,
        "has_voted": has_voted,
        "voted_at": stamp(my_rm.voted_at) if my_rm else None,
        "voted_candidate_ids": list(my_rm.voted_candidate_ids or []) if has_voted else [],
        "assigned_questions": list(my_rm.assigned_questions_json or []) if show_questions else [],
        "requires_host_decision": bool(
            room.host_user_id == viewer_user_id
            and current_round.status in PAUSED_ROUND_STATUSES
        ),
    }


@room_transaction(http=True)
def start_room_round(
    db: Session,
    room_public_id: str,
    host: User,
) -> tuple[RoomRoundCommandResponse, bool, list[Any]]:
    """T02: Start a group round, freeze shared exclusions, fail-fast verify menus, and eagerly assign 9 questions per member."""
    from app.rooms.service import _apply_lazy_room_deadlines

    try:
        locked_host = lock_user_and_verify_session(db, host)
        room = lock_room(db, room_public_id)
        if room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

        left_events = _apply_lazy_room_deadlines(
            db,
            room,
            closed_status_code=status.HTTP_409_CONFLICT,
        )
        members = lock_room_members(db, room.id)
        host_member = next((m for m in members if m.user_id == locked_host.id), None)
        if host_member is None or room.host_user_id != locked_host.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the room host can start a round",
            )

        latest_round = get_latest_round(db, room.id, for_update=True)
        if latest_round is not None:
            if latest_round.status in ACTIVE_ROUND_STATUSES:
                # Idempotent duplicate start by host while active round is already running
                common = response_common_state(db, room)
                viewer = build_viewer_state(
                    db,
                    room,
                    locked_host.id,
                    is_member=True,
                    my_role=host_member.role,
                )
                commit_room_transaction(db)
                return (
                    RoomRoundCommandResponse(
                        room_id=room.public_id,
                        round_id=latest_round.id,
                        round_no=latest_round.round_no,
                        status=latest_round.status,
                        room_status=room.status,
                        vote_round_no=latest_round.vote_round_no,
                        decision_version=latest_round.decision_version,
                        cancel_reason=latest_round.cancel_reason,
                        common_state=common,
                        viewer_state=viewer,
                    ),
                    True,
                    left_events,
                )
            if latest_round.status == "RESULT":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "ROUND_ALREADY_COMPLETED",
                        "message": "이미 결과가 확정된 방입니다.",
                    },
                )

        if room.status != "WAITING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ROOM_NOT_WAITING",
                    "message": "대기실 상태에서만 라운드를 시작할 수 있습니다.",
                },
            )

        if len(members) < 2:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "INSUFFICIENT_ROOM_MEMBERS",
                    "message": "같이 고르기를 시작하려면 최소 2명의 참여자가 필요합니다.",
                },
            )

        next_round_no = (latest_round.round_no + 1) if latest_round is not None else 1
        ordered_members = sorted(members, key=lambda m: m.user_id)

        shared_exclusions_set: set[str] = set()
        for m in ordered_members:
            user_kws = collect_user_exclusion_keywords(
                db=db,
                user_id=m.user_id,
                extra_exclusions=None,
            )
            shared_exclusions_set.update(user_kws)
        shared_exclusions = sorted(shared_exclusions_set)

        all_menus = db.query(Menu).all()
        now = now_utc()
        try:
            available_menus, _ = filter_available_menus(all_menus, shared_exclusions_set)
        except NoAvailableFoodException:
            cancelled_round = RoomRound(
                room_id=room.id,
                round_no=next_round_no,
                status="CANCELLED",
                exclusion_snapshot_json=shared_exclusions,
                vote_round_no=1,
                decision_version=0,
                is_uncontested=False,
                cancel_reason="NO_AVAILABLE_MENUS",
                started_at=now,
                cancelled_at=now,
            )
            db.add(cancelled_round)
            room.status = "WAITING"
            _reset_room_order_sheet_flags(db, room.id)
            db.flush()
            mark_room_changed(db, room, "ROUND_CANCELLED_NO_MENUS", locked_host.id)
            commit_room_transaction(db)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "NO_AVAILABLE_MENUS",
                    "message": "참여자 전원의 제외 조건을 만족하는 추천 가능한 메뉴가 없습니다.",
                    "cancel_reason": "NO_AVAILABLE_MENUS",
                },
            ) from None

        target_question_count = int(settings.ORDER_SHEET_QUESTION_COUNT)
        try:
            shared_question_pool = load_order_sheet_question_pool(
                db=db,
                exclusion_keywords=shared_exclusions_set,
                available_menus=available_menus,
            )
        except QuestionPoolEmptyException as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "QUESTION_POOL_EMPTY",
                    "message": exc.message,
                },
            ) from exc
        common_overrides = load_common_question_overrides(db)

        allocated_by_user: dict[int, list[dict[str, Any]]] = {}
        for m in ordered_members:
            try:
                q_resp = select_order_sheet_questions(
                    db=db,
                    total_count=target_question_count,
                    exploitation_count=None,
                    scope_key=f"user:{m.user_id}",
                    exclusion_keywords=shared_exclusions_set,
                    available_menus=available_menus,
                    fill_question_count=True,
                    commit=False,
                    question_pool=shared_question_pool,
                )
            except QuestionPoolEmptyException as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "QUESTION_POOL_EMPTY",
                        "message": exc.message,
                    },
                ) from exc
            slot_keys = tuple(q.cluster_key for q in q_resp.questions)
            q_resp = apply_question_overrides(
                q_resp,
                slot_keys,
                shared_question_pool,
                common_overrides,
            )
            if len(q_resp.questions) < target_question_count:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "INSUFFICIENT_QUESTION_POOL",
                        "message": f"그룹 주문서를 구성할 {target_question_count}개 질문 주제가 부족합니다.",
                    },
                )
            allocated_by_user[m.user_id] = [
                q.model_dump(mode="json") for q in q_resp.questions
            ]

        new_round = RoomRound(
            room_id=room.id,
            round_no=next_round_no,
            status="ANSWERING",
            exclusion_snapshot_json=shared_exclusions,
            vote_round_no=1,
            decision_version=0,
            is_uncontested=False,
            cancel_reason=None,
            started_at=now,
        )
        db.add(new_round)
        db.flush()

        for m in sorted(members, key=lambda x: (x.joined_at, x.id)):
            q_dicts = allocated_by_user[m.user_id]
            db.add(
                RoomRoundMember(
                    round_id=new_round.id,
                    user_id=m.user_id,
                    role=m.role,
                    member_status="ACTIVE",
                    assigned_question_ids=[int(q["id"]) for q in q_dicts],
                    assigned_questions_json=q_dicts,
                )
            )

        _reset_room_order_sheet_flags(db, room.id)
        room.status = "IN_PROGRESS"
        db.flush()

        mark_room_changed(db, room, "ROUND_STARTED", locked_host.id)
        common = response_common_state(db, room)
        viewer = build_viewer_state(
            db,
            room,
            locked_host.id,
            is_member=True,
            my_role=host_member.role,
        )
        commit_room_transaction(db)
        return (
            RoomRoundCommandResponse(
                room_id=room.public_id,
                round_id=new_round.id,
                round_no=new_round.round_no,
                status=new_round.status,
                room_status=room.status,
                vote_round_no=new_round.vote_round_no,
                decision_version=new_round.decision_version,
                cancel_reason=new_round.cancel_reason,
                common_state=common,
                viewer_state=viewer,
            ),
            False,
            left_events,
        )
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise


def _get_room_round_question_context(
    db: Session,
    room_public_id: str,
    user: User,
) -> tuple[Room, RoomRound, RoomRoundMember]:
    from app.rooms.service import _as_utc, utcnow
    from datetime import timedelta

    now = utcnow()
    room = db.query(Room).filter(Room.public_id == room_public_id).one_or_none()
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")
    if _as_utc(room.expires_at) <= now:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Room has expired")
    if room.status == "CLOSED":
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Room is closed")
    if room.host_disconnected_at is not None and _as_utc(room.host_disconnected_at) <= (
        now - timedelta(seconds=settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS)
    ):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Room is closed")

    member = (
        db.query(RoomMember)
        .filter(RoomMember.room_id == room.id, RoomMember.user_id == user.id)
        .one_or_none()
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only room members can view round questions",
        )
    if (
        member.role == "MEMBER"
        and member.disconnected_at is not None
        and _as_utc(member.disconnected_at)
        <= now - timedelta(seconds=settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only active room members can view round questions",
        )

    current_round = get_latest_round(db, room.id, for_update=False)
    if current_round is None or current_round.status == "CANCELLED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ROUND_NOT_ACTIVE",
                "message": "현재 진행 중인 라운드가 없습니다.",
            },
        )

    rm = (
        db.query(RoomRoundMember)
        .filter(
            RoomRoundMember.round_id == current_round.id,
            RoomRoundMember.user_id == user.id,
            RoomRoundMember.member_status == "ACTIVE",
        )
        .one_or_none()
    )
    if rm is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not an active participant of this round",
        )
    return room, current_round, rm


def get_room_round_questions(
    db: Session,
    room_public_id: str,
    user: User,
) -> RoomRoundQuestionsResponse:
    """Read the caller's assigned order-sheet questions for the current round."""
    room, current_round, rm = _get_room_round_question_context(db, room_public_id, user)

    q_dtos = [OrderSheetQuestionDTO.model_validate(q) for q in (rm.assigned_questions_json or [])]
    exploit_cnt = sum(1 for q in q_dtos if not q.is_exploration)
    explore_cnt = sum(1 for q in q_dtos if q.is_exploration)
    return RoomRoundQuestionsResponse(
        room_id=room.public_id,
        round_id=current_round.id,
        round_no=current_round.round_no,
        round_status=current_round.status,
        questions=q_dtos,
        total_count=len(q_dtos),
        exploitation_count=exploit_cnt,
        exploration_count=explore_cnt,
        has_submitted=rm.submitted_at is not None,
        submitted_at=rm.submitted_at,
    )


def _validate_order_sheet_round_fence(
    current_round: RoomRound,
    request: RoomOrderSheetSubmitRequest,
) -> None:
    if request.round_id is not None and request.round_id != current_round.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "STALE_ROUND_ID",
                "message": "이전 라운드에 대한 주문서 제출 요청입니다.",
            },
        )
    if request.round_no is not None and request.round_no != current_round.round_no:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "STALE_ROUND_NO",
                "message": "이전 라운드에 대한 주문서 제출 요청입니다.",
            },
        )


def _order_assigned_answers(
    member: RoomRoundMember,
    answers: list[AnswerItemRequest],
) -> list[AnswerItemRequest]:
    answers_by_question_id = {answer.question_id: answer for answer in answers}
    if len(answers_by_question_id) != len(answers):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="동일한 질문에 중복 응답할 수 없습니다.",
        )
    assigned_qids = [int(qid) for qid in (member.assigned_question_ids or [])]
    if set(answers_by_question_id) != set(assigned_qids) or len(answers) != len(assigned_qids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"배정된 {len(assigned_qids)}개 주문서 질문 전체에 정확히 응답해야 합니다.",
        )
    return [answers_by_question_id[qid] for qid in assigned_qids]


def get_room_round_no_results_suggestions(
    db: Session,
    room_public_id: str,
    user: User,
    request: RoomOrderSheetNoResultsRequest,
) -> OrderSheetNoResultsResponse:
    _, current_round, member = _get_room_round_question_context(db, room_public_id, user)
    _validate_order_sheet_round_fence(current_round, request)
    answers = _order_assigned_answers(member, request.answers)
    try:
        return suggest_no_result_answer_changes(
            db=db,
            request_answers=answers,
            exclusion_keywords=current_round.exclusion_snapshot_json or [],
            max_display_count=min(5, int(settings.ORDER_SHEET_MAX_RECOMMENDATIONS)),
        )
    except InvalidOrderSheetException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc


@room_transaction(http=True)
def submit_room_round_order_sheet(
    db: Session,
    room_public_id: str,
    user: User,
    request: RoomOrderSheetSubmitRequest,
) -> tuple[RoomRoundOrderSheetSubmitResponse, bool, list[Any]]:
    """T03 + D20: Evaluate personal order sheet against frozen shared exclusions, record cumulative Recommendation, and merge candidates when all submitted."""
    from app.rooms.service import _apply_lazy_room_deadlines

    try:
        locked_user = lock_user_and_verify_session(db, user)
        room = lock_room(db, room_public_id)
        if room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

        left_events = _apply_lazy_room_deadlines(
            db,
            room,
            closed_status_code=status.HTTP_409_CONFLICT,
        )
        members = lock_room_members(db, room.id)
        current_member = next((m for m in members if m.user_id == locked_user.id), None)
        if current_member is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only room members can submit an order sheet",
            )

        current_round = get_latest_round(db, room.id, for_update=True)
        if current_round is None or current_round.status == "CANCELLED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ROUND_NOT_ACTIVE",
                    "message": "현재 진행 중인 라운드가 없습니다.",
                },
            )
        _validate_order_sheet_round_fence(current_round, request)

        round_members = lock_round_members(db, current_round.id)
        my_rm = next(
            (
                rm
                for rm in round_members
                if rm.user_id == locked_user.id and rm.member_status == "ACTIVE"
            ),
            None,
        )
        if my_rm is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not an active participant of this round",
            )

        ordered_answer_items = _order_assigned_answers(my_rm, request.answers)

        canonical_answers = [
            {"question_id": int(a.question_id), "option_id": str(a.option_id)}
            for a in sorted(request.answers, key=lambda x: int(x.question_id))
        ]

        # Idempotent replay check BEFORE checking current_round.status == 'ANSWERING'
        if my_rm.submitted_at is not None:
            saved_canonical = sorted(
                (my_rm.answers_json or []),
                key=lambda x: int(x["question_id"]),
            )
            if canonical_answers == saved_canonical:
                active_rms = [rm for rm in round_members if rm.member_status == "ACTIVE"]
                submitted_cnt = sum(1 for rm in active_rms if rm.submitted_at is not None)
                common = response_common_state(db, room)
                viewer = build_viewer_state(
                    db,
                    room,
                    locked_user.id,
                    is_member=True,
                    my_role=current_member.role,
                )
                commit_room_transaction(db)
                return (
                    RoomRoundOrderSheetSubmitResponse(
                        room_id=room.public_id,
                        round_id=current_round.id,
                        round_no=current_round.round_no,
                        user_id=locked_user.id,
                        round_status=current_round.status,
                        submitted=True,
                        submitted_at=my_rm.submitted_at,
                        recommendation_id=my_rm.recommendation_id,
                        submitted_member_count=submitted_cnt,
                        active_member_count=len(active_rms),
                        all_orders_submitted=(
                            len(active_rms) > 0 and submitted_cnt == len(active_rms)
                        ),
                        common_state=common,
                        viewer_state=viewer,
                    ),
                    True,
                    left_events,
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ORDER_SHEET_ALREADY_SUBMITTED",
                    "message": "이미 제출한 주문서 답변은 수정할 수 없습니다.",
                },
            )

        if current_round.status in PAUSED_ROUND_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ROUND_PAUSED_FOR_HOST_DECISION",
                    "message": "참여자 퇴장으로 방장의 진행 여부 결정을 기다리는 중입니다.",
                },
            )
        if current_round.status != "ANSWERING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ROUND_NOT_ACCEPTING_ORDER_SHEETS",
                    "message": "현재 주문서 제출 단계가 아닙니다.",
                },
            )

        try:
            rec_resp = create_order_sheet_recommendation(
                db=db,
                request=OrderSheetRecommendRequest(
                    answers=ordered_answer_items,
                    exclusions=[],
                    top_k=int(settings.ORDER_SHEET_MAX_RECOMMENDATIONS),
                ),
                user_id=locked_user.id,
                current_user=locked_user,
                commit=False,
                fixed_exclusion_keywords=list(current_round.exclusion_snapshot_json or []),
                skip_user_lock=True,
            )
        except InvalidOrderSheetException as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=exc.message,
            ) from exc
        except NoAvailableFoodException:
            now = now_utc()
            current_round.status = "CANCELLED"
            current_round.cancel_reason = "NO_AVAILABLE_MENUS"
            current_round.cancelled_at = now
            room.status = "WAITING"
            _reset_room_order_sheet_flags(db, room.id)
            db.flush()
            mark_room_changed(db, room, "ROUND_CANCELLED_NO_MENUS", locked_user.id)
            commit_room_transaction(db)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "NO_AVAILABLE_MENUS",
                    "message": "공통 제외 조건을 만족하는 추천 가능한 메뉴가 없습니다.",
                    "cancel_reason": "NO_AVAILABLE_MENUS",
                },
            ) from None

        if not rec_resp.recommended_menus:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "NO_MATCHING_MENUS_FOR_ANSWERS",
                    "message": "선택한 답변에 맞는 메뉴가 없습니다.",
                },
            )

        now = now_utc()
        my_rm.answers_json = canonical_answers
        my_rm.submitted_at = now
        my_rm.recommendation_id = rec_resp.recommendation_id
        my_rm.top_menu_id = rec_resp.recommended_menus[0].menu_id

        sheets = lock_order_sheets(db, [current_member.id])
        sheet = sheets[0] if sheets else None
        if sheet is None:
            sheet = RoomMemberOrderSheet(room_member_id=current_member.id)
            db.add(sheet)
        sheet.is_completed = True
        sheet.completed_at = now
        db.flush()

        active_rms = [rm for rm in round_members if rm.member_status == "ACTIVE"]
        submitted_cnt = sum(1 for rm in active_rms if rm.submitted_at is not None)
        all_submitted = len(active_rms) > 0 and submitted_cnt == len(active_rms)

        if all_submitted:
            _finalize_candidates_and_advance_after_answering(
                db=db,
                room=room,
                current_round=current_round,
                active_round_members=active_rms,
                actor_user_id=locked_user.id,
            )
        else:
            mark_room_changed(db, room, "ORDER_SHEET_SUBMITTED", locked_user.id)

        common = response_common_state(db, room)
        viewer = build_viewer_state(
            db,
            room,
            locked_user.id,
            is_member=True,
            my_role=current_member.role,
        )
        commit_room_transaction(db)
        return (
            RoomRoundOrderSheetSubmitResponse(
                room_id=room.public_id,
                round_id=current_round.id,
                round_no=current_round.round_no,
                user_id=locked_user.id,
                round_status=current_round.status,
                submitted=True,
                submitted_at=my_rm.submitted_at,
                recommendation_id=my_rm.recommendation_id,
                submitted_member_count=submitted_cnt,
                active_member_count=len(active_rms),
                all_orders_submitted=all_submitted,
                common_state=common,
                viewer_state=viewer,
            ),
            False,
            left_events,
        )
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise


@room_transaction(http=True)
def submit_room_round_votes(
    db: Session,
    room_public_id: str,
    user: User,
    request: RoomVoteSubmitRequest,
) -> tuple[RoomRoundVoteSubmitResponse, bool, list[Any]]:
    """Save a participant's current ballot and finalize one winner when everyone has voted."""
    from app.rooms.service import _apply_lazy_room_deadlines

    try:
        locked_user = lock_user_and_verify_session(db, user)
        room = lock_room(db, room_public_id)
        if room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

        left_events = _apply_lazy_room_deadlines(
            db,
            room,
            closed_status_code=status.HTTP_409_CONFLICT,
        )
        members = lock_room_members(db, room.id)
        current_member = next((m for m in members if m.user_id == locked_user.id), None)
        if current_member is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only room members can vote",
            )

        current_round = get_latest_round(db, room.id, for_update=True)
        if current_round is None or current_round.status == "CANCELLED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ROUND_NOT_ACTIVE",
                    "message": "현재 진행 중인 라운드가 없습니다.",
                },
            )
        if request.round_id is not None and request.round_id != current_round.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "STALE_ROUND_ID",
                    "message": "이전 라운드에 대한 투표 요청입니다.",
                },
            )
        if request.round_no is not None and request.round_no != current_round.round_no:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "STALE_ROUND_NO",
                    "message": "이전 라운드에 대한 투표 요청입니다.",
                },
            )
        if (
            request.vote_round_no is not None
            and request.vote_round_no != current_round.vote_round_no
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "STALE_VOTE_ROUND",
                    "message": "이전 투표 회차의 요청입니다. 최신 후보 목록을 확인해 다시 투표해 주세요.",
                },
            )

        round_members = lock_round_members(db, current_round.id)
        my_rm = next(
            (
                rm
                for rm in round_members
                if rm.user_id == locked_user.id and rm.member_status == "ACTIVE"
            ),
            None,
        )
        if my_rm is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not an active participant of this round",
            )

        if not request.candidate_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="최소 1개 이상의 후보를 선택해야 합니다.",
            )
        raw_cids = [int(cid) for cid in request.candidate_ids]
        if len(set(raw_cids)) != len(raw_cids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="같은 후보에 중복으로 투표할 수 없습니다.",
            )

        all_cands = lock_round_candidates(db, current_round.id)
        active_cands_by_id = {c.id: c for c in all_cands if c.is_active}
        inactive_cand_ids = {c.id for c in all_cands if not c.is_active}

        for cid in raw_cids:
            if cid in inactive_cand_ids:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "CANDIDATE_INACTIVE",
                        "message": "퇴장자 정리로 제외된 후보가 포함되어 있습니다.",
                    },
                )
            if cid not in active_cands_by_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"현재 라운드에 존재하지 않는 후보 ID입니다: {cid}",
                )

        normalized_cids = sorted(raw_cids)

        # Idempotent replay check before checking current_round.status == 'VOTING'
        if my_rm.voted_at is not None:
            saved_cids = sorted(int(cid) for cid in (my_rm.voted_candidate_ids or []))
            if normalized_cids == saved_cids:
                active_rms = [rm for rm in round_members if rm.member_status == "ACTIVE"]
                voted_cnt = sum(1 for rm in active_rms if rm.voted_at is not None)
                common = response_common_state(db, room)
                viewer = build_viewer_state(
                    db,
                    room,
                    locked_user.id,
                    is_member=True,
                    my_role=current_member.role,
                )
                commit_room_transaction(db)
                return (
                    RoomRoundVoteSubmitResponse(
                        room_id=room.public_id,
                        round_id=current_round.id,
                        round_no=current_round.round_no,
                        vote_round_no=current_round.vote_round_no,
                        user_id=locked_user.id,
                        round_status=current_round.status,
                        voted=True,
                        voted_at=my_rm.voted_at,
                        voted_member_count=voted_cnt,
                        active_member_count=len(active_rms),
                        all_votes_submitted=(
                            len(active_rms) > 0 and voted_cnt == len(active_rms)
                        ),
                        common_state=common,
                        viewer_state=viewer,
                    ),
                    True,
                    left_events,
                )
            if current_round.status == "RESULT":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "VOTE_ALREADY_SUBMITTED",
                        "message": "결과가 확정되어 투표를 수정할 수 없습니다.",
                    },
                )

        if current_round.status in PAUSED_ROUND_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ROUND_PAUSED_FOR_HOST_DECISION",
                    "message": "참여자 퇴장으로 방장의 진행 여부 결정을 기다리는 중입니다.",
                },
            )
        if current_round.status != "VOTING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ROUND_NOT_IN_VOTING",
                    "message": "현재 투표 진행 단계가 아닙니다.",
                },
            )

        now = now_utc()
        my_rm.voted_candidate_ids = normalized_cids
        my_rm.voted_at = now
        db.flush()

        active_rms = [rm for rm in round_members if rm.member_status == "ACTIVE"]
        voted_cnt = sum(1 for rm in active_rms if rm.voted_at is not None)
        all_voted = len(active_rms) > 0 and voted_cnt == len(active_rms)

        if all_voted:
            counts: dict[int, int] = {cid: 0 for cid in active_cands_by_id}
            for rm in active_rms:
                for cid in (rm.voted_candidate_ids or []):
                    cid_int = int(cid)
                    if cid_int in counts:
                        counts[cid_int] += 1
            max_votes = max(counts.values())
            top_candidate_ids = [cid for cid, count in counts.items() if count == max_votes]
            winner_id = (
                choice(top_candidate_ids) if len(top_candidate_ids) > 1 else top_candidate_ids[0]
            )
            for cid, cand in active_cands_by_id.items():
                cand.final_vote_count = counts[cid]
                cand.is_winner = cid == winner_id
            current_round.is_uncontested = False
            current_round.status = "RESULT"
            current_round.completed_at = now
            db.flush()
            mark_room_changed(db, room, "ROUND_RESULT_FINALIZED", locked_user.id)
        else:
            mark_room_changed(db, room, "VOTE_SUBMITTED", locked_user.id)

        common = response_common_state(db, room)
        viewer = build_viewer_state(
            db,
            room,
            locked_user.id,
            is_member=True,
            my_role=current_member.role,
        )
        commit_room_transaction(db)
        return (
            RoomRoundVoteSubmitResponse(
                room_id=room.public_id,
                round_id=current_round.id,
                round_no=current_round.round_no,
                vote_round_no=current_round.vote_round_no,
                user_id=locked_user.id,
                round_status=current_round.status,
                voted=True,
                voted_at=my_rm.voted_at,
                voted_member_count=voted_cnt,
                active_member_count=len(active_rms),
                all_votes_submitted=all_voted,
                common_state=common,
                viewer_state=viewer,
            ),
            False,
            left_events,
        )
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise


@room_transaction(http=True)
def decide_room_round_departure(
    db: Session,
    room_public_id: str,
    host: User,
    request: RoomDepartureDecisionRequest,
) -> tuple[RoomRoundCommandResponse, list[Any]]:
    """T05 + T06: Host decision (CONTINUE / CANCEL) with decision_version optimistic token check."""
    from app.rooms.service import _apply_lazy_room_deadlines

    try:
        locked_host = lock_user_and_verify_session(db, host)
        room = lock_room(db, room_public_id)
        if room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

        left_events = _apply_lazy_room_deadlines(
            db,
            room,
            closed_status_code=status.HTTP_409_CONFLICT,
        )
        members = lock_room_members(db, room.id)
        host_member = next((m for m in members if m.user_id == locked_host.id), None)
        if host_member is None or room.host_user_id != locked_host.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the room host can decide whether to continue the round",
            )

        current_round = get_latest_round(db, room.id, for_update=True)
        if current_round is None or current_round.status not in PAUSED_ROUND_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "ROUND_NOT_PAUSED",
                    "message": "현재 방장 결정을 기다리는 일시 정지 상태가 아닙니다.",
                },
            )
        if request.round_id is not None and request.round_id != current_round.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "STALE_ROUND_ID",
                    "message": "이전 라운드에 대한 결정 요청입니다.",
                },
            )
        if request.decision_version != current_round.decision_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "STALE_DECISION_VERSION",
                    "message": "결정 대기 중 퇴장자 목록이 변경되었습니다. 최신 퇴장 목록을 다시 확인해 주세요.",
                    "current_decision_version": current_round.decision_version,
                },
            )

        action_norm = request.action.strip().upper()
        if action_norm in ("YES", "CONTINUE"):
            is_continue = True
        elif action_norm in ("NO", "CANCEL"):
            is_continue = False
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="action must be CONTINUE (YES) or CANCEL (NO)",
            )

        now = now_utc()
        round_members = lock_round_members(db, current_round.id)
        for rm in round_members:
            if rm.member_status == "LEFT_PENDING":
                rm.member_status = "LEFT_CONFIRMED"

        active_rms = [rm for rm in round_members if rm.member_status == "ACTIVE"]
        if len(active_rms) < 2:
            current_round.status = "CANCELLED"
            current_round.cancel_reason = "INSUFFICIENT_MEMBERS"
            current_round.cancelled_at = now
            room.status = "WAITING"
            _reset_room_order_sheet_flags(db, room.id)
            db.flush()
            mark_room_changed(db, room, "ROUND_CANCELLED_INSUFFICIENT_MEMBERS", locked_host.id)
        elif not is_continue:
            current_round.status = "CANCELLED"
            current_round.cancel_reason = "HOST_CANCELLED"
            current_round.cancelled_at = now
            room.status = "WAITING"
            _reset_room_order_sheet_flags(db, room.id)
            db.flush()
            mark_room_changed(db, room, "ROUND_CANCELLED_BY_HOST", locked_host.id)
        elif current_round.status == "ANSWERING_PAUSED":
            all_submitted = all(rm.submitted_at is not None for rm in active_rms)
            if all_submitted:
                _finalize_candidates_and_advance_after_answering(
                    db=db,
                    room=room,
                    current_round=current_round,
                    active_round_members=active_rms,
                    actor_user_id=locked_host.id,
                )
            else:
                current_round.status = "ANSWERING"
                db.flush()
                mark_room_changed(db, room, "ROUND_RESUMED_ANSWERING", locked_host.id)
        else:
            # VOTING_PAUSED -> T06 candidate cleanup & revote (or single-candidate uncontested win)
            active_uids = {rm.user_id for rm in active_rms}
            candidates = lock_round_candidates(db, current_round.id)
            proposers = lock_round_proposers(db, [c.id for c in candidates])
            active_proposers_by_cand: dict[int, list[int]] = {}
            for p in proposers:
                if p.user_id in active_uids:
                    active_proposers_by_cand.setdefault(p.candidate_id, []).append(p.user_id)

            for cand in candidates:
                if cand.is_active and not active_proposers_by_cand.get(cand.id):
                    cand.is_active = False

            # Reset previous round votes in-place (T06-D02)
            for rm in active_rms:
                rm.voted_candidate_ids = None
                rm.voted_at = None

            surviving = [c for c in candidates if c.is_active]
            if not surviving:
                current_round.status = "CANCELLED"
                current_round.cancel_reason = "NO_AVAILABLE_MENUS"
                current_round.cancelled_at = now
                room.status = "WAITING"
                _reset_room_order_sheet_flags(db, room.id)
                db.flush()
                mark_room_changed(db, room, "ROUND_CANCELLED_NO_MENUS", locked_host.id)
            elif len(surviving) == 1:
                surviving[0].is_winner = True
                surviving[0].final_vote_count = None
                current_round.is_uncontested = True
                current_round.status = "RESULT"
                current_round.completed_at = now
                db.flush()
                mark_room_changed(db, room, "ROUND_RESULT_FINALIZED", locked_host.id)
            else:
                current_round.is_uncontested = False
                current_round.vote_round_no = int(current_round.vote_round_no or 1) + 1
                current_round.status = "VOTING"
                db.flush()
                mark_room_changed(db, room, "REVOTE_STARTED", locked_host.id)

        common = response_common_state(db, room)
        viewer = build_viewer_state(
            db,
            room,
            locked_host.id,
            is_member=True,
            my_role=host_member.role,
        )
        commit_room_transaction(db)
        return (
            RoomRoundCommandResponse(
                room_id=room.public_id,
                round_id=current_round.id,
                round_no=current_round.round_no,
                status=current_round.status,
                room_status=room.status,
                vote_round_no=current_round.vote_round_no,
                decision_version=current_round.decision_version,
                cancel_reason=current_round.cancel_reason,
                common_state=common,
                viewer_state=viewer,
            ),
            left_events,
        )
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise

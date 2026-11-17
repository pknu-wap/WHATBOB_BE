"""Locked, transaction-neutral removal of ordinary room members.

Callers own authentication, eligibility, Room -> RoomMember locks and the final
commit. All reads used for decisions refresh the identity map and use current
locking reads, including when authentication established an older snapshot.
"""
from dataclasses import dataclass
import logging

from sqlalchemy.orm import Session

from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemberDeparture:
    room_id: str
    user_ids: tuple[int, ...] = ()
    member_count: int = 0
    completed_member_count: int = 0
    all_members_completed: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.user_ids)


def lock_room(db: Session, public_id: str) -> Room | None:
    return (db.query(Room).filter(Room.public_id == public_id)
            .populate_existing().with_for_update().one_or_none())


def lock_room_members(db: Session, room_id: int) -> list[RoomMember]:
    return (db.query(RoomMember).filter(RoomMember.room_id == room_id)
            .order_by(RoomMember.id).populate_existing().with_for_update().all())


def lock_order_sheets(db: Session, member_ids: list[int]) -> list[RoomMemberOrderSheet]:
    if not member_ids:
        return []
    return (db.query(RoomMemberOrderSheet)
            .filter(RoomMemberOrderSheet.room_member_id.in_(member_ids))
            .order_by(RoomMemberOrderSheet.id).populate_existing().with_for_update().all())


def remove_locked_members(db: Session, room: Room, members: list[RoomMember]) -> MemberDeparture:
    """Remove same-room locked members, flush once, return one final aggregate.

    The entire input is validated before any mutation. Duplicate records are
    benign; a host or foreign-room record is a programming error. Empty input
    deliberately avoids both deletion and aggregation.
    """
    for member in members:
        if member.room_id != room.id or member.role != 'MEMBER' or member.user_id == room.host_user_id:
            raise ValueError('Ordinary departure requires non-host members of the locked room')
    unique = {member.id: member for member in members}
    if len(unique) != len(members):
        logger.warning('Duplicate member departure input for room %s', room.public_id)
    if not unique:
        return MemberDeparture(room.public_id)

    # Lock remaining members before any completion rows. The Room lock excludes
    # concurrent joins and completion changes in this room.
    all_members = lock_room_members(db, room.id)
    sheets = lock_order_sheets(db, [m.id for m in all_members])
    for sheet in sheets:
        if sheet.room_member_id in unique:
            db.delete(sheet)
    ordered = [unique[key] for key in sorted(unique)]
    for member in ordered:
        db.delete(member)
    db.flush()
    from app.rooms.state import mark_room_changed
    mark_room_changed(db, room, 'MEMBER_LEFT', ordered[0].user_id)
    remaining_ids = {m.id for m in all_members if m.id not in unique}
    completed = sum(s.is_completed for s in sheets if s.room_member_id in remaining_ids)
    count = len(remaining_ids)
    return MemberDeparture(room.public_id, tuple(m.user_id for m in ordered), count, completed,
                           count > 0 and count == completed)

"""Final common room state and transaction-neutral outbox staging.

Owners mark mutations while holding Room, then prepare once at their final
commit boundary. Several departures/join changes in one transaction become one
version and one immutable snapshot. No I/O occurs here beyond the owner's DB.
"""
from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet, RoomNotification
from app.rooms.transactions import current_read_session
from app.users.models import User


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value is not None else None


def mark_room_changed(db, room, reason="ROOM_STATE_CHANGED", user_id=None):
    db.info.setdefault('changed_rooms', {})[room.id] = (room, reason, user_id)


def build_common_state(db, room):
    members = (db.query(RoomMember).filter(RoomMember.room_id == room.id)
               .order_by(RoomMember.joined_at, RoomMember.id).populate_existing().with_for_update().all())
    sheets = {s.room_member_id: s for s in db.query(RoomMemberOrderSheet)
              .filter(RoomMemberOrderSheet.room_member_id.in_([m.id for m in members]))
              .populate_existing().with_for_update().all()}
    with current_read_session(db) as read_db:
        users = {u.id: u for u in read_db.query(User).filter(User.id.in_([m.user_id for m in members])).all()}
    rows = []
    for m in members:
        sheet = sheets.get(m.id)
        user = users[m.user_id]
        rows.append(dict(user_id=m.user_id, nickname=user.nickname, profile_image=user.profile_image,
                         role=m.role, joined_at=stamp(m.joined_at), is_online=m.is_online,
                         disconnected_at=stamp(m.disconnected_at),
                         order_sheet_completed=bool(sheet and sheet.is_completed),
                         order_sheet_completed_at=stamp(sheet.completed_at) if sheet else None))
    completed = sum(row['order_sheet_completed'] for row in rows)
    return dict(room_id=room.public_id, state_version=room.state_version, status=room.status,
                host_user_id=room.host_user_id, max_members=room.max_members,
                created_at=stamp(room.created_at), expires_at=stamp(room.expires_at),
                member_count=len(rows), is_full=len(rows) >= room.max_members,
                members=rows, completed_member_count=completed,
                all_members_completed=bool(rows) and completed == len(rows))


def close_pending_notifications(db, room_id, at=None):
    at = at or now_utc()
    db.query(RoomNotification).filter(RoomNotification.room_id == room_id,
                                     RoomNotification.finished_at.is_(None)).update({
        RoomNotification.finished_at: at, RoomNotification.terminal_reason: 'ROOM_CLOSED',
        RoomNotification.payload_json: None, RoomNotification.next_attempt_at: None,
    }, synchronize_session=False)


def prepare_room_notifications(db):
    changes = db.info.get('changed_rooms', {})
    if not changes:
        return
    db.flush()
    prepared = db.info.setdefault('prepared_room_states', {})
    for room_id, (room, reason, user_id) in sorted(changes.items()):
        if room_id in prepared:
            continue
        room.state_version += 1
        db.flush()
        if room.status == 'CLOSED':
            close_pending_notifications(db, room_id, room.closed_at)
            prepared[room_id] = None
            continue
        db.refresh(room)  # Snapshot persisted legacy DATETIME precision, including new rooms.
        state = build_common_state(db, room)
        payload = dict(type='room.updated', reason=reason, user_id=user_id, occurred_at=stamp(now_utc()),
                       **state, common_state=state)
        db.add(RoomNotification(room_id=room.id, historical_room_id=room.id,
                                historical_public_id=room.public_id, state_version=room.state_version,
                                payload_json=payload, created_at=now_utc(), next_attempt_at=now_utc()))
        prepared[room_id] = state
    db.flush()


def response_common_state(db, room):
    prepare_room_notifications(db)
    return db.info.get('prepared_room_states', {}).get(room.id) or build_common_state(db, room)


@event.listens_for(Session, 'after_commit')
@event.listens_for(Session, 'after_rollback')
def clear_staging(db):
    if not db.in_nested_transaction():
        db.info.pop('changed_rooms', None)
        db.info.pop('prepared_room_states', None)

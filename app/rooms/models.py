import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Index,
    String,
    UniqueConstraint,
)

from sqlalchemy.dialects.mysql import DATETIME

from app.core.database import Base


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint(
            "status IN ('WAITING', 'IN_PROGRESS', 'CLOSED')",
            name="ck_rooms_status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    public_id = Column(
        String(36),
        unique=True,
        index=True,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
    )
    host_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status = Column(String(20), nullable=False, default="WAITING")
    state_version = Column(Integer, nullable=False, default=0, server_default="0")
    max_members = Column(Integer, nullable=False, default=8)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc) + timedelta(hours=1),
        index=True,
    )
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String(30), nullable=True)
    host_disconnected_at = Column(DateTime, nullable=True)


class RoomMember(Base):
    __tablename__ = "room_members"
    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_room_members_room_user"),
        CheckConstraint(
            "role IN ('HOST', 'MEMBER')",
            name="ck_room_members_role",
        ),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    room_id = Column(
        Integer,
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(10), nullable=False, default="MEMBER")
    joined_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    is_online = Column(Boolean, nullable=False, default=False)
    disconnected_at = Column(DateTime, nullable=True)


class RoomMemberOrderSheet(Base):
    __tablename__ = "room_member_order_sheets"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    room_member_id = Column(
        Integer,
        ForeignKey("room_members.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    is_completed = Column(Boolean, nullable=False, default=False)
    completed_at = Column(DateTime, nullable=True)


class RoomNotification(Base):
    __tablename__ = "room_notifications"
    __table_args__ = (
        UniqueConstraint("historical_room_id", "state_version", name="uq_room_notifications_version"),
        Index("ix_room_notifications_due", "finished_at", "next_attempt_at", "id"),
        Index("ix_room_notifications_retention", "finished_at", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True)
    historical_room_id = Column(Integer, nullable=False)
    historical_public_id = Column(String(36), nullable=False)
    state_version = Column(Integer, nullable=False)
    payload_json = Column(JSON(none_as_null=True), nullable=True)
    created_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    started_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    first_failed_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    deadline_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    next_attempt_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    terminal_reason = Column(String(30), nullable=True)
    finished_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)

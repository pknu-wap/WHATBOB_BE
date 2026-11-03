import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)

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
    max_members = Column(Integer, nullable=False, default=8)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


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

from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, LargeBinary, String, Index, text
from sqlalchemy.dialects.mysql import CHAR, DATETIME, VARBINARY
from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    uuid = Column(String(36).with_variant(CHAR(36, charset="ascii"), "mysql"),
                  nullable=False, default=lambda: str(uuid4()))
    kakao_id = Column(BigInteger, unique=True, index=True, nullable=True)
    nickname = Column(String(100), nullable=True)
    nickname_key = Column(LargeBinary(400).with_variant(VARBINARY(400), "mysql"), nullable=True)
    is_legacy_kakao = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    nickname_edited = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    nickname_source_edited = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    nickname_source_uuid = Column(String(36).with_variant(CHAR(36, charset="ascii"), "mysql"), nullable=True)
    nickname_source_id = Column(Integer, nullable=True)
    nickname_source_created_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    email = Column(String(255), nullable=True)
    profile_image = Column(String(500), nullable=True)
    profile_image_edited = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    profile_image_source_edited = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    profile_image_source_uuid = Column(String(36).with_variant(CHAR(36, charset="ascii"), "mysql"), nullable=True)
    profile_image_source_id = Column(Integer, nullable=True)
    profile_image_source_created_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("uq_users_uuid", "uuid", unique=True),
        Index("uq_users_nickname_key", "nickname_key", unique=True),
    )


from datetime import datetime, timezone
from sqlalchemy import BigInteger, Column, DateTime, Integer, String
from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    kakao_id = Column(BigInteger, unique=True, index=True, nullable=False)
    nickname = Column(String(100), nullable=True)
    email = Column(String(255), nullable=True)
    profile_image = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )


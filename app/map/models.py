from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from app.core.database import Base


class PlaceSelection(Base):
    """지도에서 사용자가 명시적으로 선택한 음식점 기록 테이블"""

    __tablename__ = "place_selections"
    __table_args__ = (
        Index("ix_place_selections_user_created", "user_id", "created_at"),
        Index("ix_place_selections_rec_menu", "recommendation_id", "menu_id"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    recommendation_id = Column(
        Integer,
        ForeignKey("recommendations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    menu_id = Column(
        Integer,
        ForeignKey("menus.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    kakao_place_id = Column(String(100), index=True, nullable=False)
    place_name = Column(String(200), nullable=False)
    place_address = Column(String(300), nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    satisfaction = relationship(
        "PlaceSatisfaction",
        back_populates="place_selection",
        uselist=False,
        cascade="all, delete-orphan",
    )


class PlaceSatisfaction(Base):
    """지도에서 선택한 음식점에 대한 명시적 만족 기록 테이블 (가게 선택·메뉴 피드백과 분리)"""

    __tablename__ = "place_satisfactions"
    __table_args__ = (
        Index("ix_place_satisfactions_user_updated", "user_id", "updated_at"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    place_selection_id = Column(
        Integer,
        ForeignKey("place_selections.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    recommendation_id = Column(
        Integer,
        ForeignKey("recommendations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    menu_id = Column(
        Integer,
        ForeignKey("menus.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    kakao_place_id = Column(String(100), index=True, nullable=False)
    is_satisfied = Column(Boolean, nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    place_selection = relationship("PlaceSelection", back_populates="satisfaction")

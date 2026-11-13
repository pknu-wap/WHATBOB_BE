from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
)
from sqlalchemy.dialects.mysql import BINARY, CHAR, DATETIME

from app.core.database import Base


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", name="fk_auth_sessions_user", ondelete="RESTRICT"))
    secret_hash = Column(LargeBinary(32).with_variant(BINARY(32), "mysql"), nullable=False)
    created_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    expires_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    revoked_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"))

    __table_args__ = (
        CheckConstraint("length(secret_hash) = 32", name="ck_auth_sessions_secret_length"),
        Index("uq_auth_sessions_secret_hash", "secret_hash", unique=True),
        Index("ix_auth_sessions_user_id", "user_id"),
        Index("ix_auth_sessions_expires_id", "expires_at", "id"),
        Index("ix_auth_sessions_revoked_id", "revoked_at", "id"),
        {"mysql_engine": "InnoDB"},
    )


class RequestOperation(Base):
    """방 생성/참여, 식사 기록, 추천 피드백 등의 멱등 실행 및 취소 원장"""
    __tablename__ = "request_operations"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    scope = Column(String(40), nullable=False)
    operation_key = Column(String(64), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    auth_session_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True, index=True)
    status = Column(String(20), nullable=False, default="PENDING")
    payload_hash = Column(String(64), nullable=False)
    room_id = Column(String(36), nullable=True, index=True)
    resource_id = Column(String(64), nullable=True)
    response_status = Column(Integer, nullable=True)
    response_json = Column(JSON, nullable=True)
    created_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    updated_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)

    __table_args__ = (
        Index("uq_request_operations_user_scope_key", "user_id", "scope", "operation_key", unique=True),
        Index("ix_request_operations_user_status", "user_id", "status"),
        {"mysql_engine": "InnoDB"},
    )


class AccountTransition(Base):
    """카카오 연결(KAKAO_LINK) 및 로그아웃(LOGOUT) 2단계 계정 전환 원장"""
    __tablename__ = "account_transitions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    transition_id = Column(
        String(36).with_variant(CHAR(36, charset="ascii"), "mysql"),
        nullable=False,
        unique=True,
        index=True,
    )
    operation_key = Column(String(64), nullable=True, index=True)
    action = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="PREPARED")
    origin_user_id = Column(Integer, nullable=True, index=True)
    origin_uuid = Column(String(36).with_variant(CHAR(36, charset="ascii"), "mysql"), nullable=False)
    origin_session_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, index=True)
    provider_context_json = Column(JSON, nullable=True)
    result_user_id = Column(Integer, nullable=True, index=True)
    result_uuid = Column(String(36).with_variant(CHAR(36, charset="ascii"), "mysql"), nullable=True)
    result_session_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    result_is_new_user = Column(Boolean, nullable=True)
    prepare_secret_hash = Column(LargeBinary(32).with_variant(BINARY(32), "mysql"), nullable=False)
    recovery_secret_hash = Column(LargeBinary(32).with_variant(BINARY(32), "mysql"), nullable=False)
    prepare_expires_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    recovery_expires_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    committed_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    recovered_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    created_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)
    updated_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)

    __table_args__ = (
        Index("ix_account_transitions_origin_session", "origin_session_id", "status"),
        {"mysql_engine": "InnoDB"},
    )


class KakaoLinkGuard(Base):
    """동일 kakao_id 동시 연결 직렬화를 위한 경량 잠금 가드 테이블"""
    __tablename__ = "kakao_link_guards"

    kakao_id = Column(BigInteger, primary_key=True, autoincrement=False)
    created_at = Column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False)

    __table_args__ = ({"mysql_engine": "InnoDB"},)

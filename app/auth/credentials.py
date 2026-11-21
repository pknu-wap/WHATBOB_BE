import base64
from datetime import datetime
import hashlib

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.auth.models import AccountTransition, KakaoSessionCredential
from app.core.config import settings


def _get_fernet() -> Fernet:
    raw_secret = (settings.KAKAO_TOKEN_ENCRYPTION_KEY or settings.JWT_SECRET).encode("utf-8")
    derived_key = base64.urlsafe_b64encode(hashlib.sha256(raw_secret).digest())
    return Fernet(derived_key)


def encrypt_kakao_token(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    cleaned = plaintext.strip()
    if not cleaned:
        return None
    return _get_fernet().encrypt(cleaned.encode("utf-8")).decode("ascii")


def decrypt_kakao_token(ciphertext: str | None) -> str | None:
    if ciphertext is None:
        return None
    cleaned = ciphertext.strip()
    if not cleaned:
        return None
    return _get_fernet().decrypt(cleaned.encode("ascii")).decode("utf-8")


def shred_kakao_credential_tokens(
    cred: KakaoSessionCredential,
    *,
    status: str,
    now: datetime,
    terminal_reason: str | None = None,
) -> None:
    cred.encrypted_access_token = None
    cred.encrypted_refresh_token = None
    cred.status = status
    if terminal_reason is not None:
        cred.terminal_reason = terminal_reason
    cred.next_attempt_at = None
    cred.finished_at = now
    cred.updated_at = now


def shred_prepared_kakao_credential(
    db: Session,
    transition_id: str,
    *,
    status: str,
    now: datetime,
    terminal_reason: str | None = None,
) -> None:
    cred = (
        db.query(KakaoSessionCredential)
        .filter(KakaoSessionCredential.transition_id == transition_id)
        .with_for_update()
        .first()
    )
    if cred is not None and cred.status == "PREPARED":
        shred_kakao_credential_tokens(
            cred,
            status=status,
            now=now,
            terminal_reason=terminal_reason or status,
        )
        db.flush()

def shred_session_prepared_kakao_credentials(
    db: Session,
    origin_session_id: int,
    *,
    status: str = "CANCELLED",
    now: datetime,
    exclude_transition_id: str | None = None,
) -> None:
    query = (
        db.query(KakaoSessionCredential)
        .join(
            AccountTransition,
            AccountTransition.transition_id == KakaoSessionCredential.transition_id,
        )
        .filter(
            AccountTransition.origin_session_id == int(origin_session_id),
            KakaoSessionCredential.status == "PREPARED",
        )
    )
    if exclude_transition_id is not None:
        query = query.filter(KakaoSessionCredential.transition_id != exclude_transition_id)
    for cred in query.with_for_update().all():
        shred_kakao_credential_tokens(
            cred,
            status=status,
            now=now,
            terminal_reason="SUPERSEDED" if status == "CANCELLED" else status,
        )
    db.flush()

from datetime import datetime, timedelta, timezone
import re
import jwt
from app.auth.errors import AuthError
from app.core.config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_SECONDS = 3600


def is_positive_id(value: object) -> bool:
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None
            and int(value) <= 9223372036854775807)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    lifetime = int(expires_delta.total_seconds()) if expires_delta is not None else ACCESS_TOKEN_SECONDS
    payload = {**data, "iat": now, "exp": now + lifetime,
               "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    invalid = AuthError("ACCESS_TOKEN_INVALID", "유효하지 않은 접근 토큰입니다.", bearer=True)
    try:
        # Verify the signature before classifying legacy tokens or expiry.
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM], options={
            "verify_exp": False, "verify_iat": False, "verify_nbf": False,
            "verify_aud": False, "verify_iss": False,
        })
    except (jwt.InvalidTokenError, TypeError, ValueError):
        raise invalid from None
    if (set(payload) <= {"sub", "exp", "kakao_id"} and is_positive_id(payload.get("sub"))
            and type(payload.get("exp")) is int):
        raise AuthError("REAUTHENTICATION_REQUIRED", "카카오 계정으로 다시 로그인해 주세요.", bearer=True)
    if not (is_positive_id(payload.get("sub")) and is_positive_id(payload.get("sid"))
            and type(payload.get("iat")) is int and type(payload.get("exp")) is int
            and payload.get("iss") == settings.JWT_ISSUER
            and payload.get("aud") == settings.JWT_AUDIENCE):
        raise invalid
    now = datetime.now(timezone.utc).timestamp()
    if payload["iat"] > now + settings.JWT_CLOCK_SKEW_SECONDS or "nbf" in payload:
        raise invalid
    if now >= payload["exp"]:
        raise AuthError("ACCESS_TOKEN_EXPIRED", "접근 토큰이 만료되었습니다.", bearer=True)
    return payload


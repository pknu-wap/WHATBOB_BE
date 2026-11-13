from contextlib import contextmanager
import logging

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError, TimeoutError


class AuthError(HTTPException):
    def __init__(self, code: str, message: str, status_code: int = 401, *, bearer: bool = False):
        self.code = code
        headers = {"Cache-Control": "no-store"}
        if bearer:
            headers["WWW-Authenticate"] = "Bearer"
        super().__init__(status_code, {"code": code, "message": message}, headers=headers)


@contextmanager
def database_errors(db, *, bearer: bool = False):
    try:
        yield
    except SQLAlchemyError as error:
        db.rollback()
        logging.getLogger("uvicorn.error").warning("Authentication database operation failed (%s)", type(error).__name__)
        temporary = isinstance(error, (OperationalError, TimeoutError)) or (
            isinstance(error, DBAPIError) and error.connection_invalidated
        )
        if temporary:
            raise AuthError("AUTH_TEMPORARILY_UNAVAILABLE", "일시적으로 인증을 확인할 수 없습니다.",
                            503, bearer=bearer) from None
        raise AuthError("AUTH_INTERNAL_ERROR", "인증 처리 중 오류가 발생했습니다.",
                        500, bearer=bearer) from None

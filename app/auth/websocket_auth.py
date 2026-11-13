import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone

from fastapi import WebSocket
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.auth.errors import AuthError
from app.auth.sessions import authenticate_access_token


class ConnectionAuthentication:
    """Serialize token replacement, expiry and protected sends for one connection."""

    def __init__(self, websocket: WebSocket, payload: dict, token: str, database_bind):
        self.websocket = websocket
        self.payload = payload
        self.token_hash = hashlib.sha256(token.encode()).digest()
        self.database_bind = database_bind
        self.lock = asyncio.Lock()
        self.requests: dict[str, bytes] = {}
        self.closed = False

    @property
    def remaining_seconds(self) -> float:
        return max(0, self.payload["exp"] - time.time())

    @property
    def expires_at(self) -> str:
        return datetime.fromtimestamp(self.payload["exp"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _send(self, message: dict) -> None:
        await asyncio.wait_for(self.websocket.send_json(message), timeout=2)

    async def _error(self, code: str, message: str, request_id=None, *, close_code=None, **extra):
        try:
            await self._send({"type": "auth.error", "request_id": request_id, "code": code,
                              "message": message, **extra})
        finally:
            if close_code is not None:
                self.closed = True
                await self.websocket.close(code=close_code, reason=code)

    async def _expire_if_due(self) -> bool:
        if not self.closed and self.remaining_seconds <= 0:
            await self._error("ACCESS_TOKEN_EXPIRED", "접근 토큰이 만료되었습니다.", close_code=4408)
        return self.closed

    async def expire_if_due(self) -> bool:
        async with self.lock:
            return await self._expire_if_due()

    async def send_json(self, message: dict) -> None:
        async with self.lock:
            if await self._expire_if_due():
                raise WebSocketDisconnect(code=4408)
            await self._send(message)

    def _authenticate(self, token: str):
        with Session(bind=self.database_bind) as db:
            _, payload = authenticate_access_token(db, token)
            return payload

    async def refresh(self, message: dict) -> None:
        async with self.lock:
            if await self._expire_if_due():
                return
            request_id = message.get("request_id")
            if (not isinstance(request_id, str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) is None):
                await self._error("INVALID_AUTH_MESSAGE", "재인증 요청 형식이 올바르지 않습니다.", close_code=4400)
                return
            token = message.get("access_token")
            if not isinstance(token, str) or not token or len(token) > 4096:
                code = "ACCESS_TOKEN_MISSING" if not token else "ACCESS_TOKEN_INVALID"
                await self._error(code, "유효한 접근 토큰이 필요합니다.", request_id, close_code=4401)
                return
            token_hash = hashlib.sha256(token.encode()).digest()
            if request_id in self.requests and self.requests[request_id] != token_hash:
                await self._error("INVALID_AUTH_MESSAGE", "요청 번호가 다른 토큰에 사용되었습니다.", request_id, close_code=4400)
                return
            try:
                payload = await asyncio.to_thread(self._authenticate, token)
            except AuthError as error:
                if await self._expire_if_due():
                    return
                close_code = None if error.code in {"ACCESS_TOKEN_EXPIRED", "AUTH_TEMPORARILY_UNAVAILABLE"} else (
                    1011 if error.code == "AUTH_INTERNAL_ERROR" else 4401
                )
                await self._error(error.code, error.detail["message"], request_id, close_code=close_code)
                return
            if await self._expire_if_due():
                return
            if (payload["sub"], payload["sid"]) != (self.payload["sub"], self.payload["sid"]):
                await self._error("AUTH_CONTEXT_MISMATCH", "연결의 인증 정보와 일치하지 않습니다.", request_id, close_code=4401)
                return
            self.requests[request_id] = token_hash
            if (payload["iat"] < self.payload["iat"] or payload["exp"] < self.payload["exp"]
                    or (payload["iat"] == self.payload["iat"] and payload["exp"] == self.payload["exp"]
                        and token_hash != self.token_hash)):
                await self._error("AUTH_REFRESH_SUPERSEDED", "더 최신 인증이 적용되어 있습니다.",
                                  request_id, expires_at=self.expires_at)
                return
            self.payload, self.token_hash = payload, token_hash
            await self._send({"type": "auth.refreshed", "request_id": request_id, "expires_at": self.expires_at})

import asyncio
from collections import defaultdict

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_access_token
from app.rooms.models import Room, RoomMember
from app.users.models import User


AUTH_TIMEOUT_SECONDS = 5
SEND_TIMEOUT_SECONDS = 2

router = APIRouter(prefix="/ws/rooms", tags=["Room WebSocket"])


class RoomConnectionManager:
    """In-memory room connections for a single application worker."""

    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    def connect(self, room_public_id: str, websocket: WebSocket) -> None:
        self._connections[room_public_id].add(websocket)

    def disconnect(self, room_public_id: str, websocket: WebSocket) -> None:
        room_connections = self._connections.get(room_public_id)
        if room_connections is None:
            return
        room_connections.discard(websocket)
        if not room_connections:
            self._connections.pop(room_public_id, None)

    async def broadcast(self, room_public_id: str, message: dict) -> None:
        connections = list(self._connections.get(room_public_id, set()))
        if not connections:
            return

        async def send(websocket: WebSocket) -> None:
            try:
                await asyncio.wait_for(
                    websocket.send_json(message),
                    timeout=SEND_TIMEOUT_SECONDS,
                )
            except Exception:
                self.disconnect(room_public_id, websocket)

        await asyncio.gather(*(send(websocket) for websocket in connections))


room_connection_manager = RoomConnectionManager()


def is_allowed_origin(origin: str | None) -> bool:
    if origin is None:
        return True
    return origin in {
        settings.FRONTEND_BASE_URL.rstrip("/"),
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    }


async def close_websocket(websocket: WebSocket, code: int, reason: str) -> None:
    await websocket.close(code=code, reason=reason)


@router.websocket("/{room_id}")
async def room_websocket(
    websocket: WebSocket,
    room_id: str,
    db: Session = Depends(get_db),
):
    if not is_allowed_origin(websocket.headers.get("origin")):
        await close_websocket(websocket, 4403, "Origin is not allowed")
        return

    await websocket.accept()
    connected = False
    try:
        try:
            auth_message = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=AUTH_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, ValueError, TypeError):
            await close_websocket(websocket, 4401, "Authentication required")
            return

        if not isinstance(auth_message, dict) or (
            auth_message.get("type") != "auth"
            or not auth_message.get("access_token")
        ):
            await close_websocket(websocket, 4401, "Authentication required")
            return

        try:
            payload = decode_access_token(auth_message["access_token"])
            user_id = int(payload["sub"])
        except (InvalidTokenError, KeyError, TypeError, ValueError):
            await close_websocket(websocket, 4401, "Invalid access token")
            return

        user = db.get(User, user_id)
        if user is None:
            await close_websocket(websocket, 4401, "Invalid access token")
            return

        room = (
            db.query(Room)
            .filter(Room.public_id == room_id)
            .one_or_none()
        )
        if room is None:
            await close_websocket(websocket, 4404, "Room not found")
            return

        member = (
            db.query(RoomMember)
            .filter(
                RoomMember.room_id == room.id,
                RoomMember.user_id == user.id,
            )
            .one_or_none()
        )
        if member is None:
            await close_websocket(websocket, 4403, "Room membership required")
            return

        authenticated_user_id = user.id
        # A long-lived socket must not hold a database connection after auth.
        db.close()
        room_connection_manager.connect(room_id, websocket)
        connected = True
        await websocket.send_json(
            {
                "type": "connection.ready",
                "room_id": room_id,
                "user_id": authenticated_user_id,
            }
        )

        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict) and message.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        if connected:
            room_connection_manager.disconnect(room_id, websocket)

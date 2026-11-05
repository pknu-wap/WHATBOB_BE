import asyncio
from collections import defaultdict

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_access_token
from app.rooms.models import Room, RoomMember
from app.rooms.service import (
    clear_host_disconnected,
    close_room_after_host_disconnect,
    delete_closed_room,
    mark_host_disconnected,
    mark_room_member_disconnected,
    mark_room_member_offline_after_grace,
    mark_room_member_online,
)
from app.users.models import User


AUTH_TIMEOUT_SECONDS = 5
SEND_TIMEOUT_SECONDS = 2

router = APIRouter(prefix="/ws/rooms", tags=["Room WebSocket"])


class RoomConnectionManager:
    """In-memory room connections for a single application worker."""

    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._user_connections: dict[str, dict[int, set[WebSocket]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self._host_disconnect_tasks: dict[str, asyncio.Task] = {}
        self._member_disconnect_tasks: dict[tuple[str, int], asyncio.Task] = {}

    def connect(
        self,
        room_public_id: str,
        user_id: int,
        websocket: WebSocket,
    ) -> None:
        self._connections[room_public_id].add(websocket)
        self._user_connections[room_public_id][user_id].add(websocket)

    def disconnect(
        self,
        room_public_id: str,
        user_id: int,
        websocket: WebSocket,
    ) -> bool:
        room_connections = self._connections.get(room_public_id)
        if room_connections is None:
            return False
        room_connections.discard(websocket)
        if not room_connections:
            self._connections.pop(room_public_id, None)

        users = self._user_connections.get(room_public_id)
        if users is None:
            return False
        user_connections = users.get(user_id)
        if user_connections is None:
            return False
        user_connections.discard(websocket)
        is_last_user_connection = not user_connections
        if is_last_user_connection:
            users.pop(user_id, None)
        if not users:
            self._user_connections.pop(room_public_id, None)
        return is_last_user_connection

    def cancel_host_disconnect(self, room_public_id: str) -> None:
        task = self._host_disconnect_tasks.pop(room_public_id, None)
        if task is not None:
            task.cancel()

    def schedule_host_disconnect(
        self,
        room_public_id: str,
        host_user_id: int,
        database_bind,
    ) -> None:
        self.cancel_host_disconnect(room_public_id)
        task = asyncio.create_task(
            close_room_after_host_grace(
                room_public_id,
                host_user_id,
                database_bind,
            )
        )
        self._host_disconnect_tasks[room_public_id] = task

        def remove_finished(finished_task: asyncio.Task) -> None:
            if self._host_disconnect_tasks.get(room_public_id) is finished_task:
                self._host_disconnect_tasks.pop(room_public_id, None)

        task.add_done_callback(remove_finished)

    def cancel_member_disconnect(self, room_public_id: str, user_id: int) -> None:
        key = (room_public_id, user_id)
        task = self._member_disconnect_tasks.pop(key, None)
        if task is not None:
            task.cancel()

    def schedule_member_disconnect(
        self,
        room_public_id: str,
        user_id: int,
        database_bind,
    ) -> None:
        key = (room_public_id, user_id)
        self.cancel_member_disconnect(room_public_id, user_id)
        task = asyncio.create_task(
            mark_member_offline_after_grace(
                room_public_id,
                user_id,
                database_bind,
            )
        )
        self._member_disconnect_tasks[key] = task

        def remove_finished(finished_task: asyncio.Task) -> None:
            if self._member_disconnect_tasks.get(key) is finished_task:
                self._member_disconnect_tasks.pop(key, None)

        task.add_done_callback(remove_finished)

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
                pass

        await asyncio.gather(*(send(websocket) for websocket in connections))

    async def close_room_connections(self, room_public_id: str) -> None:
        connections = list(self._connections.pop(room_public_id, set()))
        self._user_connections.pop(room_public_id, None)
        member_task_keys = [
            key for key in self._member_disconnect_tasks if key[0] == room_public_id
        ]
        for _, user_id in member_task_keys:
            self.cancel_member_disconnect(room_public_id, user_id)
        for websocket in connections:
            try:
                await websocket.close(code=4000, reason="Room closed")
            except Exception:
                pass

    async def close_user_connections(
        self,
        room_public_id: str,
        user_id: int,
    ) -> None:
        users = self._user_connections.get(room_public_id)
        if users is None:
            return
        connections = list(users.pop(user_id, set()))
        room_connections = self._connections.get(room_public_id)
        if room_connections is not None:
            room_connections.difference_update(connections)
            if not room_connections:
                self._connections.pop(room_public_id, None)
        if not users:
            self._user_connections.pop(room_public_id, None)
        for websocket in connections:
            try:
                await websocket.close(code=4001, reason="Room member left")
            except Exception:
                pass


room_connection_manager = RoomConnectionManager()


async def close_room_after_host_grace(
    room_public_id: str,
    host_user_id: int,
    database_bind,
) -> None:
    try:
        await asyncio.sleep(settings.ROOM_HOST_DISCONNECT_GRACE_SECONDS)
        with Session(bind=database_bind) as db:
            closed_room = await asyncio.to_thread(
                close_room_after_host_disconnect,
                db,
                room_public_id,
                host_user_id,
            )
        if closed_room is None:
            return

        await room_connection_manager.broadcast(
            room_public_id,
            {
                "type": "room.closed",
                "room_id": room_public_id,
                "reason": closed_room.reason,
                "closed_at": closed_room.closed_at.isoformat(),
            },
        )
        await room_connection_manager.close_room_connections(room_public_id)
        with Session(bind=database_bind) as db:
            await asyncio.to_thread(delete_closed_room, db, room_public_id)
    except asyncio.CancelledError:
        return


async def mark_member_offline_after_grace(
    room_public_id: str,
    user_id: int,
    database_bind,
) -> None:
    try:
        await asyncio.sleep(settings.ROOM_MEMBER_DISCONNECT_GRACE_SECONDS)
        with Session(bind=database_bind) as db:
            changed = await asyncio.to_thread(
                mark_room_member_offline_after_grace,
                db,
                room_public_id,
                user_id,
            )
        if changed:
            await room_connection_manager.broadcast(
                room_public_id,
                {
                    "type": "room.updated",
                    "room_id": room_public_id,
                    "reason": "MEMBER_OFFLINE",
                    "user_id": user_id,
                },
            )
    except asyncio.CancelledError:
        return


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
    authenticated_user_id: int | None = None
    authenticated_is_host = False
    database_bind = db.get_bind()
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
        authenticated_is_host = room.host_user_id == user.id
        room_connection_manager.cancel_member_disconnect(
            room_id,
            authenticated_user_id,
        )
        with Session(bind=database_bind) as reconnect_db:
            became_online = mark_room_member_online(
                reconnect_db,
                room_id,
                authenticated_user_id,
            )
        if authenticated_is_host:
            room_connection_manager.cancel_host_disconnect(room_id)
            with Session(bind=database_bind) as reconnect_db:
                clear_host_disconnected(
                    reconnect_db,
                    room_id,
                    authenticated_user_id,
                )
        # A long-lived socket must not hold a database connection after auth.
        db.close()
        room_connection_manager.connect(
            room_id,
            authenticated_user_id,
            websocket,
        )
        connected = True
        await websocket.send_json(
            {
                "type": "connection.ready",
                "room_id": room_id,
                "user_id": authenticated_user_id,
            }
        )
        if became_online and not authenticated_is_host:
            await room_connection_manager.broadcast(
                room_id,
                {
                    "type": "room.updated",
                    "room_id": room_id,
                    "reason": "MEMBER_ONLINE",
                    "user_id": authenticated_user_id,
                },
            )

        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict) and message.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        if connected and authenticated_user_id is not None:
            is_last_connection = room_connection_manager.disconnect(
                room_id,
                authenticated_user_id,
                websocket,
            )
            if authenticated_is_host and is_last_connection:
                with Session(bind=database_bind) as disconnect_db:
                    mark_room_member_disconnected(
                        disconnect_db,
                        room_id,
                        authenticated_user_id,
                    )
                    marked = mark_host_disconnected(
                        disconnect_db,
                        room_id,
                        authenticated_user_id,
                    )
                if marked:
                    room_connection_manager.schedule_host_disconnect(
                        room_id,
                        authenticated_user_id,
                        database_bind,
                    )
            elif is_last_connection:
                with Session(bind=database_bind) as disconnect_db:
                    marked = mark_room_member_disconnected(
                        disconnect_db,
                        room_id,
                        authenticated_user_id,
                    )
                if marked:
                    room_connection_manager.schedule_member_disconnect(
                        room_id,
                        authenticated_user_id,
                        database_bind,
                    )

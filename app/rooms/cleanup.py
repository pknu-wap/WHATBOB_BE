import asyncio
import logging

from app.core.config import settings
from app.core.database import SessionLocal
from app.rooms.service import close_due_rooms, delete_closed_room
from app.rooms.websocket import room_connection_manager


logger = logging.getLogger("uvicorn")


async def cleanup_due_rooms_once() -> int:
    """Close, notify, and physically delete every due room."""
    with SessionLocal() as db:
        closed_rooms = await asyncio.to_thread(close_due_rooms, db)

    deleted_count = 0
    for closed_room in closed_rooms:
        await room_connection_manager.broadcast(
            closed_room.room_id,
            {
                "type": "room.closed",
                "room_id": closed_room.room_id,
                "reason": closed_room.reason,
                "closed_at": closed_room.closed_at.isoformat(),
            },
        )
        await room_connection_manager.close_room_connections(closed_room.room_id)
        with SessionLocal() as db:
            deleted = await asyncio.to_thread(
                delete_closed_room,
                db,
                closed_room.room_id,
            )
        deleted_count += int(deleted)
    return deleted_count


async def room_cleanup_loop() -> None:
    """Periodically clean rooms, surviving transient database failures."""
    while True:
        try:
            await cleanup_due_rooms_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("방 만료 정리 작업에 실패했습니다.")
        await asyncio.sleep(settings.ROOM_CLEANUP_INTERVAL_SECONDS)

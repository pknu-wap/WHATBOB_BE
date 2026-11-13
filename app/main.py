import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import app.menus.models  # noqa: F401
import app.users.models  # noqa: F401
import app.auth.models  # noqa: F401
from app.auth.cookie_router import router as cookie_auth_router, allowed_web_origins
from app.auth.router import router as auth_router
from app.auth.transition_router import router as transition_auth_router
from app.core.database import Base, SessionLocal, engine
from app.recommend.common import RecommendationDomainException
from app.recommend.router import router as recommend_router
from app.rooms.router import operations_router as room_operations_router, router as rooms_router
from app.rooms.cleanup import room_cleanup_loop
from app.rooms.service import recover_rooms_after_restart
from app.rooms.websocket import router as rooms_websocket_router
from app.users.profile_image import uploads_router as profile_uploads_router
from app.users.router import router as users_router

logger = logging.getLogger("uvicorn")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 기동 시 DB 테이블 생성 및 재시작 방 유예 복구 시도
    try:
        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            recover_rooms_after_restart(db)
        logger.info("데이터베이스 테이블 생성/확인 및 방 재시작 유예 복구 완료")
    except Exception as e:
        logger.warning(f"데이터베이스 연결 실패 (DB가 아직 실행되지 않았을 수 있습니다): {e}")
    cleanup_task = asyncio.create_task(room_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="WHATBOB API",
    description="왓밥(WHATBOB) 백엔드 API 서버",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_web_origins(),
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "ngrok-skip-browser-warning",
        "X-Whatbob-Auth-Context",
        "X-Whatbob-Request",
    ],
)


@app.exception_handler(RecommendationDomainException)
async def handle_recommendation_domain_exception(
    request: Request,
    exc: RecommendationDomainException,
):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "message": exc.message,
        },
    )


# 라우터 등록
app.include_router(auth_router)
app.include_router(cookie_auth_router)
app.include_router(transition_auth_router)
app.include_router(recommend_router)
app.include_router(rooms_router)
app.include_router(room_operations_router)
app.include_router(rooms_websocket_router)
app.include_router(users_router)
app.include_router(profile_uploads_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
def health_check():
    return {"message": "WHATBOB 백엔드 서버가 정상 동작 중입니다."}


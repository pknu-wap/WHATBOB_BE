import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.auth.router import router as auth_router
from app.core.database import Base, engine

logger = logging.getLogger("uvicorn")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 기동 시 DB 테이블 생성 시도
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("데이터베이스 테이블 생성/확인 완료")
    except Exception as e:
        logger.warning(f"데이터베이스 연결 실패 (DB가 아직 실행되지 않았을 수 있습니다): {e}")
    yield


app = FastAPI(
    title="WHATBOB API",
    description="왓밥(WHATBOB) 백엔드 API 서버",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://whatbobfe-app.vercel.app",
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    ],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "ngrok-skip-browser-warning"],
)

# 라우터 등록
app.include_router(auth_router)


@app.get("/")
def health_check():
    return {"message": "WHATBOB 백엔드 서버가 정상 동작 중입니다."}

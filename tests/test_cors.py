import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize("origin", [
    "https://whatbobfe-app.vercel.app",
    "http://localhost:8081",
    "http://127.0.0.1:8081",
])
def test_preflight_allows_frontend_and_ngrok_header(origin):
    # lifespan을 시작하지 않아 DB 연결 없이 실제 미들웨어를 검증한다.
    response = TestClient(app).options("/api/auth/kakao/login", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers":
            "content-type, authorization, ngrok-skip-browser-warning",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "ngrok-skip-browser-warning" in response.headers[
        "access-control-allow-headers"
    ].lower()


def test_preflight_rejects_unlisted_origin():
    response = TestClient(app).options("/api/auth/kakao/login", headers={
        "Origin": "https://unlisted.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "ngrok-skip-browser-warning",
    })
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_actual_response_allows_vercel_origin():
    origin = "https://whatbobfe-app.vercel.app"
    response = TestClient(app).get("/", headers={
        "Origin": origin,
        "ngrok-skip-browser-warning": "true",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"

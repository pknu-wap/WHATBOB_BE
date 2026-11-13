from datetime import timedelta

import pytest
from starlette.websockets import WebSocketDisconnect

from app.auth.sessions import new_session
from app.core.security import create_access_token, decode_access_token
from tests.test_uuid_auth import api, start_user, bearer


def open_room(api, result):
    response = api.client.post("/api/rooms", json={"max_members": 3}, headers=bearer(result))
    assert response.status_code == 201, response.text
    return response.json()["room_id"]


def test_refresh_keeps_connection_membership_and_accepts_duplicate_ack_requests(api):
    result = start_user(api)
    room_id = open_room(api, result)
    claims = decode_access_token(result["access_token"])
    first = create_access_token({"sub": claims["sub"], "sid": claims["sid"]}, expires_delta=timedelta(minutes=20))
    with api.client.websocket_connect(f"/ws/rooms/{room_id}") as websocket:
        websocket.send_json({"type": "auth", "access_token": first})
        ready = websocket.receive_json()
        assert ready["type"] == "connection.ready"
        fresh = api.client.post("/api/auth/tokens").json()["access_token"]
        refresh = {"type": "auth.refresh", "request_id": "refresh-1", "access_token": fresh}
        websocket.send_json(refresh)
        acknowledged = websocket.receive_json()
        assert acknowledged["type"] == "auth.refreshed"
        assert acknowledged["request_id"] == "refresh-1" and acknowledged["expires_at"].endswith("Z")
        websocket.send_json(refresh)
        assert websocket.receive_json() == acknowledged
        websocket.send_json({"type": "auth.refresh", "request_id": "old", "access_token": first})
        stale = websocket.receive_json()
        assert stale["code"] == "AUTH_REFRESH_SUPERSEDED"
        assert stale["expires_at"] == acknowledged["expires_at"]
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}
        assert api.client.get(f"/api/rooms/{room_id}", headers=bearer(result)).status_code == 200


@pytest.mark.parametrize("case,code,close_code", [
    ("other_session", "AUTH_CONTEXT_MISMATCH", 4401),
    ("revoked", "SESSION_REVOKED", 4401),
    ("request_id", "INVALID_AUTH_MESSAGE", 4400),
    ("forged", "ACCESS_TOKEN_INVALID", 4401),
])
def test_refresh_rejects_different_context_revocation_and_invalid_messages(api, case, code, close_code):
    result = start_user(api)
    room_id = open_room(api, result)
    claims = decode_access_token(result["access_token"])
    with api.client.websocket_connect(f"/ws/rooms/{room_id}") as websocket:
        websocket.send_json({"type": "auth", "access_token": result["access_token"]})
        assert websocket.receive_json()["type"] == "connection.ready"
        token = result["access_token"]
        if case == "other_session":
            record, _ = new_session(result["user"]["id"])
            api.db.add(record)
            api.db.commit()
            token = create_access_token({"sub": claims["sub"], "sid": str(record.id)})
        elif case == "revoked":
            from app.auth.sessions import revoke_session
            revoke_session(api.db, int(claims["sid"]))
        elif case == "forged":
            token = "invalid-token"
        websocket.send_json({"type": "auth.refresh", "request_id": "invalid!" if case == "request_id" else "r1",
                             "access_token": token})
        assert websocket.receive_json()["code"] == code
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_json()
        assert error.value.code == close_code


def test_current_jwt_expiry_closes_idle_connection(api):
    result = start_user(api)
    room_id = open_room(api, result)
    claims = decode_access_token(result["access_token"])
    token = create_access_token({"sub": claims["sub"], "sid": claims["sid"]}, expires_delta=timedelta(seconds=2))
    with api.client.websocket_connect(f"/ws/rooms/{room_id}") as websocket:
        websocket.send_json({"type": "auth", "access_token": token})
        assert websocket.receive_json()["type"] == "connection.ready"
        assert websocket.receive_json()["code"] == "ACCESS_TOKEN_EXPIRED"
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_json()
        assert error.value.code == 4408


@pytest.mark.parametrize("origin", [None, "null", "https://evil.test"])
def test_websocket_origin_is_checked_before_accepting(api, origin):
    api.client.headers.clear()
    headers = {} if origin is None else {"Origin": origin}
    with pytest.raises(WebSocketDisconnect) as error:
        with api.client.websocket_connect("/ws/rooms/unknown", headers=headers):
            pass
    assert error.value.code == 4403

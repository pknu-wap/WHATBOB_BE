import logging
from dataclasses import dataclass
from functools import lru_cache
from threading import BoundedSemaphore
from typing import Any, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

GOOGLE_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_TEXT_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location"
)
GOOGLE_TEXT_SEARCH_MAX_RESULTS = 5
GOOGLE_LOCATION_BIAS_RADIUS_M = 100.0
# 외부 오류 메시지·metadata에는 키나 IP가 포함될 수 있어 알려진 사유 코드만 기록한다.
GOOGLE_SAFE_ERROR_REASONS = frozenset({
    "ACCESS_DENIED", "SERVICE_DISABLED", "BILLING_DISABLED", "API_KEY_INVALID",
    "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
    "API_KEY_HTTP_REFERRER_BLOCKED", "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED",
})


@dataclass(frozen=True)
class GooglePlaceCandidate:
    place_id: str
    display_name: str
    formatted_address: str
    latitude: float
    longitude: float


class GooglePlaceSearchClient(Protocol):
    enabled: bool

    def search_place_candidates(
        self,
        *,
        name: str,
        address: str | None,
        latitude: float,
        longitude: float,
    ) -> list[GooglePlaceCandidate]:
        ...


class GooglePlacesUnavailableError(Exception):
    """Google 장소 ID 보강에만 사용하는 내부 예외."""


class DisabledGooglePlaceSearchClient:
    enabled = False

    def search_place_candidates(self, **kwargs: Any) -> list[GooglePlaceCandidate]:
        return []


class GooglePlacesTextSearchClient:
    """Places API (New) Text Search 클라이언트.

    API 키, 검색어, 주소, 좌표는 로그에 기록하지 않는다.
    """

    enabled = True

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        max_concurrency: int,
        transport: httpx.BaseTransport | None = None,
    ):
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._limiter = BoundedSemaphore(max_concurrency)
        self._transport = transport

    def search_place_candidates(
        self,
        *,
        name: str,
        address: str | None,
        latitude: float,
        longitude: float,
    ) -> list[GooglePlaceCandidate]:
        if not self._api_key:
            return []

        query = " ".join(part for part in (name.strip(), (address or "").strip()) if part)
        if not query:
            return []

        payload = {
            "textQuery": query,
            "languageCode": "ko",
            "regionCode": "KR",
            "maxResultCount": GOOGLE_TEXT_SEARCH_MAX_RESULTS,
            "locationBias": {
                "circle": {
                    "center": {"latitude": latitude, "longitude": longitude},
                    "radius": GOOGLE_LOCATION_BIAS_RADIUS_M,
                }
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": GOOGLE_TEXT_SEARCH_FIELD_MASK,
        }

        acquired = self._limiter.acquire(timeout=self._timeout_seconds)
        if not acquired:
            raise GooglePlacesUnavailableError("Google Places concurrency limit timeout")
        try:
            with httpx.Client(timeout=self._timeout_seconds, transport=self._transport) as client:
                response = client.post(GOOGLE_TEXT_SEARCH_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Google 장소 ID 조회 요청 실패: %s", type(exc).__name__)
            raise GooglePlacesUnavailableError() from exc
        finally:
            self._limiter.release()

        if response.status_code != 200:
            logger.warning(
                "Google 장소 ID 조회 비정상 응답: status=%s reason=%s",
                response.status_code,
                _safe_error_reasons(response),
            )
            if response.status_code == 403:
                logger.warning("Google Places API (New) 활성화·결제 연결·키의 API/IP 제한을 확인하세요.")
            raise GooglePlacesUnavailableError()

        try:
            body = response.json()
        except ValueError as exc:
            logger.warning("Google 장소 ID 조회 응답 JSON 파싱 실패")
            raise GooglePlacesUnavailableError() from exc
        if not isinstance(body, dict):
            logger.warning("Google 장소 ID 조회 응답이 JSON 객체가 아님")
            raise GooglePlacesUnavailableError()

        places = body.get("places", [])
        if not isinstance(places, list):
            logger.warning("Google 장소 ID 조회 응답의 places 형식이 잘못됨")
            raise GooglePlacesUnavailableError()
        return [candidate for place in places if (candidate := _parse_candidate(place)) is not None]


def _safe_error_reasons(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "UNKNOWN"
    error = body.get("error") if isinstance(body, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return "UNKNOWN"
    reasons = set()
    for detail in details:
        reason = detail.get("reason") if isinstance(detail, dict) else None
        if isinstance(reason, str) and reason in GOOGLE_SAFE_ERROR_REASONS:
            reasons.add(reason)
    return ",".join(sorted(reasons)) or "UNKNOWN"


def _parse_candidate(value: Any) -> GooglePlaceCandidate | None:
    if not isinstance(value, dict):
        return None
    display_name = value.get("displayName")
    location = value.get("location")
    if not isinstance(display_name, dict) or not isinstance(location, dict):
        return None
    place_id = _text(value.get("id"))
    name = _text(display_name.get("text"))
    address = _text(value.get("formattedAddress"))
    try:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not place_id or not name or not address:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return GooglePlaceCandidate(place_id, name, address, latitude, longitude)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


@lru_cache(maxsize=1)
def get_google_place_search_client() -> GooglePlaceSearchClient:
    api_key = (settings.GOOGLE_PLACES_API_KEY or "").strip()
    if not api_key:
        logger.warning("GOOGLE_PLACES_API_KEY 미설정: Google 장소 ID 보강 비활성화")
        return DisabledGooglePlaceSearchClient()
    return GooglePlacesTextSearchClient(
        api_key,
        timeout_seconds=settings.GOOGLE_PLACES_REQUEST_TIMEOUT_SECONDS,
        max_concurrency=settings.GOOGLE_PLACES_MAX_CONCURRENCY,
    )

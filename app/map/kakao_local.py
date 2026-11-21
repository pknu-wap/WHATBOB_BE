import logging
from typing import Any, Protocol

import httpx

from app.core.config import settings
from app.map.exceptions import PlaceSearchUnavailableException

logger = logging.getLogger(__name__)

KAKAO_KEYWORD_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_RESTAURANT_CATEGORY_CODE = "FD6"
KAKAO_MAX_PAGE_SIZE = 15
KAKAO_REQUEST_TIMEOUT_SECONDS = 3.0


class PlaceSearchClient(Protocol):
    def search_restaurants(
        self,
        keyword: str,
        latitude: float,
        longitude: float,
        radius_m: int,
        size: int,
    ) -> list[dict[str, Any]]:
        ...


class KakaoLocalClient:
    """카카오 로컬 키워드 검색 API 클라이언트. 음식점(FD6)만 거리순으로 조회한다."""

    def __init__(self, rest_api_key: str, transport: httpx.BaseTransport | None = None):
        self._rest_api_key = rest_api_key.strip()
        self._transport = transport

    def search_restaurants(
        self,
        keyword: str,
        latitude: float,
        longitude: float,
        radius_m: int,
        size: int,
    ) -> list[dict[str, Any]]:
        params = {
            "query": keyword,
            "category_group_code": KAKAO_RESTAURANT_CATEGORY_CODE,
            "x": f"{longitude}",
            "y": f"{latitude}",
            "radius": radius_m,
            "sort": "distance",
            "size": min(size, KAKAO_MAX_PAGE_SIZE),
            "page": 1,
        }
        headers = {"Authorization": f"KakaoAK {self._rest_api_key}"}
        try:
            with httpx.Client(timeout=KAKAO_REQUEST_TIMEOUT_SECONDS, transport=self._transport) as client:
                response = client.get(KAKAO_KEYWORD_SEARCH_URL, params=params, headers=headers)
        except httpx.HTTPError as exc:
            # 좌표·API 키가 로그에 남지 않도록 예외 종류만 기록한다.
            logger.warning("카카오 장소 검색 요청 실패: %s", type(exc).__name__)
            raise PlaceSearchUnavailableException() from exc

        if response.status_code != 200:
            logger.warning("카카오 장소 검색 비정상 응답: status=%s", response.status_code)
            raise PlaceSearchUnavailableException()

        try:
            documents = response.json().get("documents")
        except ValueError as exc:
            logger.warning("카카오 장소 검색 응답 JSON 파싱 실패")
            raise PlaceSearchUnavailableException() from exc
        if not isinstance(documents, list):
            logger.warning("카카오 장소 검색 응답에 documents 목록이 없음")
            raise PlaceSearchUnavailableException()
        return [doc for doc in documents if isinstance(doc, dict)]


def get_place_search_client() -> PlaceSearchClient:
    return KakaoLocalClient(settings.KAKAO_REST_API_KEY)

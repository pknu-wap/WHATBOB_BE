import logging
from concurrent.futures import Future, ThreadPoolExecutor, wait

from app.map.google_places import GooglePlaceSearchClient
from app.map.place_matching import find_unique_google_place_id
from app.map.schemas import NearbyPlaceDTO

logger = logging.getLogger(__name__)


def enrich_google_place_ids(
    places: list[NearbyPlaceDTO],
    client: GooglePlaceSearchClient,
    *,
    max_concurrency: int,
    total_budget_seconds: float,
) -> None:
    """제한된 동시성·시간 예산 안에서 DTO에 Google 장소 ID를 보강한다."""
    if not places or not getattr(client, "enabled", True):
        return

    executor = ThreadPoolExecutor(
        max_workers=min(max_concurrency, len(places)),
        thread_name_prefix="google-place-id",
    )
    futures: dict[Future[str | None], NearbyPlaceDTO] = {
        executor.submit(_resolve_google_place_id, place, client): place for place in places
    }
    try:
        completed, pending = wait(futures, timeout=total_budget_seconds)
        for future in completed:
            place = futures[future]
            try:
                place.google_place_id = future.result()
            except Exception as exc:  # Google 실패는 카카오 검색 응답에 전파하지 않는다.
                logger.warning("Google 장소 ID 보강 실패: %s", type(exc).__name__)
        for future in pending:
            future.cancel()
        if pending:
            logger.warning("Google 장소 ID 보강 시간 예산 초과: pending_count=%s", len(pending))
    finally:
        # 이미 실행 중인 httpx 요청은 짧은 개별 타임아웃으로 종료되며 응답 반환을 기다리지 않는다.
        executor.shutdown(wait=False, cancel_futures=True)


def _resolve_google_place_id(
    place: NearbyPlaceDTO,
    client: GooglePlaceSearchClient,
) -> str | None:
    candidates = client.search_place_candidates(
        name=place.name,
        address=place.road_address or place.address,
        latitude=place.latitude,
        longitude=place.longitude,
    )
    return find_unique_google_place_id(place, candidates)

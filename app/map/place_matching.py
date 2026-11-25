import math
import re
import unicodedata

from app.map.google_places import GooglePlaceCandidate
from app.map.schemas import NearbyPlaceDTO

MAX_MATCH_DISTANCE_M = 100
EARTH_RADIUS_M = 6_371_000
_COMPANY_MARKERS = ("주식회사", "유한회사", "(주)", "(유)")
_KOREAN_REGION_SUFFIXES = ("특별자치도", "특별자치시", "광역시", "특별시")
_ROAD_ADDRESS_PATTERN = re.compile(
    r"(?P<road>[가-힣a-z0-9·.]+(?:대로|로|길)(?:\s*\d+번길)?)"
    r"\s*(?P<main>\d+)(?:\s*-\s*(?P<sub>\d+))?(?!\d)",
    re.IGNORECASE,
)


def find_unique_google_place_id(
    place: NearbyPlaceDTO,
    candidates: list[GooglePlaceCandidate],
) -> str | None:
    """이름·주소·좌표가 모두 일치하는 유일한 Google 후보만 연결한다."""
    matches: dict[str, GooglePlaceCandidate] = {}
    for candidate in candidates:
        if not _same_name(place.name, candidate.display_name):
            continue
        if not _same_address(place, candidate.formatted_address):
            continue
        if _distance_m(
            place.latitude,
            place.longitude,
            candidate.latitude,
            candidate.longitude,
        ) > MAX_MATCH_DISTANCE_M:
            continue
        matches[candidate.place_id] = candidate
    return next(iter(matches)) if len(matches) == 1 else None


def _same_name(left: str, right: str) -> bool:
    return _normalize_name(left) == _normalize_name(right)


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    for marker in _COMPANY_MARKERS:
        normalized = normalized.replace(marker, "")
    return "".join(character for character in normalized if character.isalnum())


def _same_address(place: NearbyPlaceDTO, google_address: str) -> bool:
    google_road_key = _road_address_key(google_address)
    kakao_road_key = _road_address_key(place.road_address or "")
    if google_road_key and kakao_road_key:
        return google_road_key == kakao_road_key

    google_key = _normalize_address(google_address)
    if not google_key:
        return False
    for kakao_address in (place.road_address, place.address):
        kakao_key = _normalize_address(kakao_address or "")
        if kakao_key and kakao_key == google_key:
            return True
    return False


def _road_address_key(value: str) -> tuple[str, int, int | None] | None:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    match = _ROAD_ADDRESS_PATTERN.search(normalized)
    if match is None:
        return None
    road_name = "".join(character for character in match.group("road") if character.isalnum())
    sub_number = match.group("sub")
    return road_name, int(match.group("main")), int(sub_number) if sub_number else None


def _normalize_address(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("대한민국", "")
    for suffix in _KOREAN_REGION_SUFFIXES:
        normalized = normalized.replace(suffix, "")
    normalized = re.sub(r"\b\d{5}\b", "", normalized)
    return "".join(character for character in normalized if character.isalnum())


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))

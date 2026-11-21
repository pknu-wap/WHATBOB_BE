class PlaceSearchException(Exception):
    """주변 음식점 검색 도메인 기본 예외"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PlaceSearchMenuNotFoundException(PlaceSearchException):
    """검색 기준 메뉴가 존재하지 않을 때 발생하는 예외"""

    def __init__(self, menu_id: int):
        super().__init__(message=f"메뉴(id={menu_id})를 찾을 수 없습니다.", status_code=404)


class PlaceSearchUnavailableException(PlaceSearchException):
    """외부 장소 검색 API 장애·타임아웃·쿼터 초과로 검색할 수 없을 때 발생하는 예외"""

    def __init__(self, message: str = "주변 음식점 검색을 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."):
        super().__init__(message=message, status_code=503)

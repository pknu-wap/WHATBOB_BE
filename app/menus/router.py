from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.menus.popular import get_popular_menus
from app.menus.schemas import PopularMenusResponse
from app.recommend.schemas import ApiResponse

router = APIRouter(prefix="/api/menus", tags=["Menus"])


@router.get("/popular", response_model=ApiResponse[PopularMenusResponse])
def get_popular_menus_endpoint(
    db: Session = Depends(get_db),
):
    """홈의 요즘 인기 메뉴 목록을 공개 조회한다."""
    return ApiResponse(data=get_popular_menus(db))

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.users import repository
from app.users.schemas import DislikedFoodResponse, DislikedFoodsResponse, DislikedFoodsUpdateRequest


def get_my_disliked_foods(
    db: Session, user_id: int, for_update: bool = False,
) -> DislikedFoodsResponse:
    items = [DislikedFoodResponse.model_validate(row)
             for row in repository.find_disliked_foods(db, user_id, for_update=for_update)]
    return DislikedFoodsResponse(items=items, total_count=len(items))


def _lock_preference_owner(db: Session, user_id: int) -> None:
    if repository.lock_user_for_preference_update(db, user_id) is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다.")


def replace_my_disliked_foods(
    db: Session, user_id: int, request: DislikedFoodsUpdateRequest,
) -> DislikedFoodsResponse:
    """비선호 목록만 원자적으로 교체하고 남아 있는 항목의 ID는 유지한다."""
    try:
        _lock_preference_owner(db, user_id)
        wanted = {keyword.casefold(): keyword for keyword in request.keywords}
        retained: set[str] = set()
        for row in repository.find_disliked_foods(db, user_id, for_update=True):
            key = row.keyword.strip().casefold()
            if key in wanted and key not in retained:
                row.keyword = wanted[key]
                retained.add(key)
            else:
                repository.remove_disliked_food(db, row)
        for key, keyword in wanted.items():
            if key not in retained:
                repository.add_disliked_food(db, user_id, keyword)
        db.flush()
        response = get_my_disliked_foods(db, user_id, for_update=True)
        db.commit()
        return response
    except (SQLAlchemyError, HTTPException):
        db.rollback()
        raise


def delete_my_disliked_food(db: Session, user_id: int, dislike_id: int) -> None:
    try:
        _lock_preference_owner(db, user_id)
        exclusion = repository.find_disliked_food(db, user_id, dislike_id)
        if exclusion is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="비선호 음식 항목을 찾을 수 없습니다.")
        repository.remove_disliked_food(db, exclusion)
        db.commit()
    except (SQLAlchemyError, HTTPException):
        db.rollback()
        raise

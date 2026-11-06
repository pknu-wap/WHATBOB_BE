from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.menus.display import format_menu_name
from app.menus.models import Menu, UserMeal
from app.recommend.common import FoodNotFoundException
from app.users import repository
from app.users.schemas import (
    DislikedFoodResponse,
    DislikedFoodsResponse,
    DislikedFoodsUpdateRequest,
    UserMealCreateRequest,
    UserMealItemResponse,
    UserMealMenuDTO,
    UserMealsResponse,
)


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_user_meal_menu_dto(menu: Menu) -> UserMealMenuDTO:
    return UserMealMenuDTO(
        menu_id=menu.id,
        food_code=menu.food_code,
        name=format_menu_name(menu.name) or menu.name,
        base_name=menu.base_name,
        category_large=menu.category_large,
        category_medium=menu.category_medium,
        meal_type=menu.meal_type,
        has_soup=menu.has_soup,
        cooking_style=menu.cooking_style,
        main_ingredient=menu.main_ingredient,
        flavor_intensity=menu.flavor_intensity,
        temperature=menu.temperature,
        heaviness=menu.heaviness,
        kcal=menu.kcal,
        protein_g=menu.protein_g,
        fat_g=menu.fat_g,
        carb_g=menu.carb_g,
        sugar_g=menu.sugar_g,
        sodium_mg=menu.sodium_mg,
    )


def _to_user_meal_item_response(meal: UserMeal, menu: Menu | None) -> UserMealItemResponse:
    return UserMealItemResponse(
        id=meal.id,
        menu_id=meal.menu_id,
        menu_name=meal.menu_name,
        eaten_at=_as_utc(meal.eaten_at),
        created_at=_as_utc(meal.created_at),
        menu=_to_user_meal_menu_dto(menu) if menu is not None else None,
    )


def record_my_meal(
    db: Session,
    user_id: int,
    request: UserMealCreateRequest,
) -> UserMealItemResponse:
    """로그인한 사용자의 식사 기록을 user_meals 테이블에 저장한다."""
    try:
        _lock_preference_owner(db, user_id)
        menu = repository.find_menu_by_id(db, request.menu_id)
        if menu is None:
            raise FoodNotFoundException(request.menu_id)

        now = datetime.now(timezone.utc)
        eaten_at = _as_utc(request.eaten_at) if request.eaten_at is not None else now
        display_name = format_menu_name(menu.name) or menu.name

        meal = repository.create_user_meal(
            db=db,
            user_id=user_id,
            menu_id=menu.id,
            menu_name=display_name,
            eaten_at=eaten_at,
            created_at=now,
        )
        db.commit()
        db.refresh(meal)
        return _to_user_meal_item_response(meal, menu)
    except (SQLAlchemyError, HTTPException, FoodNotFoundException):
        db.rollback()
        raise


def get_my_meals(
    db: Session,
    user_id: int,
    limit: int = 50,
    offset: int = 0,
) -> UserMealsResponse:
    """로그인한 사용자가 먹은 음식 기록을 식사 일시 최신순으로 조회한다."""
    total_count = repository.count_user_meals(db, user_id)
    rows = repository.find_user_meals_with_menu(db, user_id, limit=limit, offset=offset)
    items = [_to_user_meal_item_response(meal, menu) for meal, menu in rows]
    return UserMealsResponse(items=items, total_count=total_count)

from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.auth.sessions import as_utc_naive
from app.menus.models import (
    FoodKind,
    Menu,
    MenuIllustrationException,
    UserFoodExclusion,
    UserMeal,
)
from app.users.models import User

DISLIKE_EXCLUSION_TYPE = "DISLIKE"


def lock_user_for_preference_update(db: Session, user_id: int) -> User | None:
    # 같은 사용자의 목록 저장과 개별 삭제가 동시에 실행돼도 서로 덮어쓰지 않게 한다.
    return db.query(User).filter(User.id == user_id).with_for_update().one_or_none()


def find_disliked_foods(
    db: Session, user_id: int, for_update: bool = False,
) -> list[UserFoodExclusion]:
    query = (db.query(UserFoodExclusion)
             .filter(UserFoodExclusion.user_id == user_id,
                     UserFoodExclusion.exclusion_type == DISLIKE_EXCLUSION_TYPE)
             .order_by(UserFoodExclusion.id.asc()))
    if for_update:
        # MySQL REPEATABLE READ에서도 사용자 잠금 이후의 최신 목록을 읽는다.
        query = query.with_for_update()
    return query.all()


def find_disliked_food(db: Session, user_id: int, dislike_id: int) -> UserFoodExclusion | None:
    return (db.query(UserFoodExclusion)
            .filter(UserFoodExclusion.id == dislike_id,
                    UserFoodExclusion.user_id == user_id,
                    UserFoodExclusion.exclusion_type == DISLIKE_EXCLUSION_TYPE)
            .with_for_update().one_or_none())


def add_disliked_food(db: Session, user_id: int, keyword: str) -> None:
    db.add(UserFoodExclusion(user_id=user_id, exclusion_type=DISLIKE_EXCLUSION_TYPE, keyword=keyword))


def remove_disliked_food(db: Session, exclusion: UserFoodExclusion) -> None:
    db.delete(exclusion)


def find_exclusions_by_type(
    db: Session,
    user_id: int,
    exclusion_type: str,
    for_update: bool = False,
) -> list[UserFoodExclusion]:
    query = (
        db.query(UserFoodExclusion)
        .filter(
            UserFoodExclusion.user_id == user_id,
            UserFoodExclusion.exclusion_type == exclusion_type,
        )
        .order_by(UserFoodExclusion.id.asc())
    )
    if for_update:
        query = query.with_for_update()
    return query.all()


def add_catalog_exclusion(
    db: Session,
    user_id: int,
    exclusion_type: str,
    code: str,
    label: str,
    catalog_version: int = 1,
) -> None:
    db.add(
        UserFoodExclusion(
            user_id=user_id,
            exclusion_type=exclusion_type,
            keyword=label,
            catalog_code=code,
            catalog_version=catalog_version,
        )
    )


def find_user_meal(db: Session, user_id: int, meal_id: int) -> UserMeal | None:
    return (
        db.query(UserMeal)
        .filter(UserMeal.id == meal_id, UserMeal.user_id == user_id)
        .with_for_update()
        .one_or_none()
    )


def remove_user_meal(db: Session, meal: UserMeal) -> None:
    db.delete(meal)


def find_menu_by_id(db: Session, menu_id: int) -> Menu | None:
    return db.query(Menu).filter(Menu.id == menu_id).one_or_none()


def create_user_meal(
    db: Session,
    user_id: int,
    menu_id: int,
    menu_name: str,
    eaten_at: datetime,
    created_at: datetime,
) -> UserMeal:
    meal = UserMeal(
        user_id=user_id,
        menu_id=menu_id,
        menu_name=menu_name,
        eaten_at=eaten_at,
        created_at=created_at,
    )
    db.add(meal)
    return meal


def count_user_meals(db: Session, user_id: int) -> int:
    return db.query(UserMeal).filter(UserMeal.user_id == user_id).count()


def find_user_meals_with_menu(
    db: Session,
    user_id: int,
    limit: int,
    offset: int,
) -> list[tuple[UserMeal, Menu | None, str | None, int | None]]:
    return (
        db.query(UserMeal, Menu, FoodKind.representative_image_url, MenuIllustrationException.menu_id)
        .outerjoin(Menu, UserMeal.menu_id == Menu.id)
        .outerjoin(FoodKind, Menu.food_kind_id == FoodKind.id)
        .outerjoin(MenuIllustrationException, Menu.id == MenuIllustrationException.menu_id)
        .filter(UserMeal.user_id == user_id)
        .order_by(UserMeal.eaten_at.desc(), UserMeal.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def find_valid_user_meals_with_food_kind_chunk(
    db: Session,
    user_id: int,
    *,
    as_of: datetime,
    limit: int,
    cursor: tuple[datetime, int] | None = None,
) -> list[tuple[UserMeal, Menu, FoodKind | None, MenuIllustrationException | None]]:
    """현재 카탈로그 메뉴가 존재하고 조회 시각 이하인 사용자 식사 기록을 복합 커서 기준으로 한 묶음 조회한다."""
    as_of_naive = as_utc_naive(as_of)
    query = (
        db.query(UserMeal, Menu, FoodKind, MenuIllustrationException)
        .join(Menu, UserMeal.menu_id == Menu.id)
        .outerjoin(FoodKind, Menu.food_kind_id == FoodKind.id)
        .outerjoin(
            MenuIllustrationException,
            Menu.id == MenuIllustrationException.menu_id,
        )
        .filter(
            UserMeal.user_id == user_id,
            UserMeal.eaten_at <= as_of_naive,
        )
    )
    if cursor is not None:
        last_eaten_at, last_id = cursor
        last_eaten_at_naive = as_utc_naive(last_eaten_at)
        query = query.filter(
            or_(
                UserMeal.eaten_at < last_eaten_at_naive,
                and_(
                    UserMeal.eaten_at == last_eaten_at_naive,
                    UserMeal.id < last_id,
                ),
            )
        )
    return (
        query.order_by(UserMeal.eaten_at.desc(), UserMeal.id.desc())
        .limit(limit)
        .all()
    )

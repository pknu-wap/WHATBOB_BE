from datetime import datetime

from sqlalchemy.orm import Session

from app.menus.models import Menu, UserFoodExclusion, UserMeal
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
) -> list[tuple[UserMeal, Menu | None]]:
    return (
        db.query(UserMeal, Menu)
        .outerjoin(Menu, UserMeal.menu_id == Menu.id)
        .filter(UserMeal.user_id == user_id)
        .order_by(UserMeal.eaten_at.desc(), UserMeal.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

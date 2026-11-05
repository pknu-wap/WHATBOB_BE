from sqlalchemy.orm import Session

from app.menus.models import UserFoodExclusion
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

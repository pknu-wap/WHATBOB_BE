import random
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.menus.models import Menu
from app.recommend.common import (
    collect_user_exclusion_keywords,
    filter_available_menus,
)


def pick_random_menu(
    db: Session,
    user_id: int | None = None,
    extra_exclusions: Iterable[str] | None = None,
    rng: random.Random | None = None,
) -> tuple[Menu, int, int]:
    
    menus = (
        db.query(Menu)
        .filter(Menu.meal_type == "main_meal")
        .all()
    )

    exclusion_keywords = collect_user_exclusion_keywords(
        db=db,
        user_id=user_id,
        extra_exclusions=extra_exclusions,
    )
    available_menus, excluded_count = filter_available_menus(
        menus=menus,
        exclusion_keywords=exclusion_keywords,
    )

    random_generator = rng or random.SystemRandom()
    selected_menu = random_generator.choice(available_menus)

    return selected_menu, excluded_count, len(available_menus)

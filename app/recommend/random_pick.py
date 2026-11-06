import random
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.menus.models import Menu
from app.recommend.common import (
    collect_user_exclusion_keywords,
    filter_available_menus,
)
from app.recommend.variety import menu_group


@dataclass(frozen=True)
class RandomPickResult:
    menu: Menu
    excluded_count: int
    candidate_count: int
    menu_type_count: int


def pick_random_menu(
    db: Session,
    user_id: int | None = None,
    extra_exclusions: Iterable[str] | None = None,
    rng: random.Random | None = None,
) -> RandomPickResult:
    """정규화된 음식 종류마다 같은 확률로 실제 메뉴 하나를 선택한다."""
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

    menus_by_type: dict[str, list[Menu]] = {}
    for menu in available_menus:
        menus_by_type.setdefault(menu_group(menu), []).append(menu)

    random_generator = rng or random.SystemRandom()
    selected_type = random_generator.choice(list(menus_by_type))
    selected_menu = random_generator.choice(menus_by_type[selected_type])

    return RandomPickResult(
        menu=selected_menu,
        excluded_count=excluded_count,
        candidate_count=len(available_menus),
        menu_type_count=len(menus_by_type),
    )

from datetime import datetime, timezone
import re

from fastapi import HTTPException, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.errors import AuthError
from app.auth.operations import (
    acquire_or_inspect_operation,
    compute_payload_hash,
    get_user_session_id,
    lock_user_and_verify_session,
    normalize_idempotency_key,
)
from app.auth.sessions import as_utc_naive, utc_now
from app.menus.display import format_menu_name
from app.menus.models import Menu, UserMeal
from app.recommend.common import (
    ALLERGEN_BY_CODE,
    ALLERGEN_CATALOG_VERSION,
    FoodNotFoundException,
    RecommendationDomainException,
    UNABLE_BY_CODE,
    UNABLE_CATALOG_VERSION,
)
from app.users import repository
from app.users.models import User
from app.users.schemas import (
    CatalogExclusionItemResponse,
    CatalogExclusionsResponse,
    CatalogExclusionsUpdateRequest,
    DislikedFoodResponse,
    DislikedFoodsResponse,
    DislikedFoodsUpdateRequest,
    LegacyExclusionItemResponse,
    MAX_DISLIKED_FOODS,
    NicknameUpdateRequest,
    UserMealCreateRequest,
    UserMealItemResponse,
    UserMealMenuDTO,
    UserMealsResponse,
    UserResponse,
)

NICKNAME_ALLOWED_RE = re.compile(r"^[가-힣A-Za-z0-9 ]+$")


def validate_and_clean_nickname(raw_nickname: str) -> str:
    if not isinstance(raw_nickname, str):
        raise AuthError(
            "INVALID_NICKNAME",
            "닉네임은 앞뒤 공백 제거 후 2~20자의 한글, 영문, 숫자, 공백만 사용할 수 있습니다.",
            status.HTTP_400_BAD_REQUEST,
        )
    cleaned = raw_nickname.strip()
    if len(cleaned) < 2 or len(cleaned) > 20 or NICKNAME_ALLOWED_RE.fullmatch(cleaned) is None:
        raise AuthError(
            "INVALID_NICKNAME",
            "닉네임은 앞뒤 공백 제거 후 2~20자의 한글, 영문, 숫자, 공백만 사용할 수 있습니다.",
            status.HTTP_400_BAD_REQUEST,
        )
    return cleaned


def update_my_nickname(
    db: Session,
    current_user: User,
    request: NicknameUpdateRequest,
) -> UserResponse:
    cleaned = validate_and_clean_nickname(request.nickname)
    new_key = cleaned.encode("utf-8")
    try:
        locked_user = lock_user_and_verify_session(db, current_user)
        conflict = (
            db.query(User.id)
            .filter(User.nickname_key == new_key, User.id != locked_user.id)
            .first()
        )
        if conflict is not None:
            db.rollback()
            raise AuthError("NICKNAME_DUPLICATE", "이미 사용 중인 닉네임입니다.", status.HTTP_409_CONFLICT)

        try:
            with db.begin_nested():
                locked_user.nickname = cleaned
                locked_user.nickname_key = new_key
                locked_user.nickname_edited = True
                locked_user.nickname_source_edited = True
                if locked_user.kakao_id is None:
                    locked_user.nickname_source_uuid = locked_user.uuid
                    locked_user.nickname_source_id = locked_user.id
                    locked_user.nickname_source_created_at = (
                        as_utc_naive(locked_user.created_at) if locked_user.created_at else utc_now()
                    )
                db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise AuthError("NICKNAME_DUPLICATE", "이미 사용 중인 닉네임입니다.", status.HTTP_409_CONFLICT) from exc

        resp = UserResponse.model_validate(locked_user)
        db.commit()
        return resp
    except (SQLAlchemyError, HTTPException):
        db.rollback()
        raise



def get_my_disliked_foods(
    db: Session, user_id: int, for_update: bool = False,
) -> DislikedFoodsResponse:
    items = [
        DislikedFoodResponse.model_validate(row)
        for row in repository.find_disliked_foods(db, user_id, for_update=for_update)
    ]
    return DislikedFoodsResponse(items=items, total_count=len(items))


def _lock_preference_owner(db: Session, user_id: int, current_user: User | None = None) -> User:
    if current_user is not None:
        return lock_user_and_verify_session(db, current_user)
    locked = repository.lock_user_for_preference_update(db, user_id)
    if locked is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다.")
    return locked


def replace_my_disliked_foods(
    db: Session,
    user_id: int,
    request: DislikedFoodsUpdateRequest,
    current_user: User | None = None,
) -> DislikedFoodsResponse:
    """비선호 목록만 원자적으로 교체하고 남아 있는 항목의 ID는 유지한다. 병합으로 100개를 초과한 목록의 축소 편집도 허용한다."""
    try:
        _lock_preference_owner(db, user_id, current_user)
        existing_rows = repository.find_disliked_foods(db, user_id, for_update=True)
        allowed_max = max(MAX_DISLIKED_FOODS, len(existing_rows))
        if len(request.keywords) > allowed_max:
            db.rollback()
            raise RequestValidationError(
                [
                    {
                        "type": "too_long",
                        "loc": ("body", "keywords"),
                        "msg": f"비선호 음식은 최대 {allowed_max}개까지 저장할 수 있습니다.",
                        "input": request.keywords,
                    }
                ]
            )
        wanted = {keyword.casefold(): keyword for keyword in request.keywords}
        retained: set[str] = set()
        for row in existing_rows:
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
    except (SQLAlchemyError, HTTPException, RequestValidationError):
        db.rollback()
        raise


def delete_my_disliked_food(
    db: Session,
    user_id: int,
    dislike_id: int,
    current_user: User | None = None,
) -> None:
    try:
        _lock_preference_owner(db, user_id, current_user)
        exclusion = repository.find_disliked_food(db, user_id, dislike_id)
        if exclusion is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="비선호 음식 항목을 찾을 수 없습니다.",
            )
        repository.remove_disliked_food(db, exclusion)
        db.commit()
    except (SQLAlchemyError, HTTPException):
        db.rollback()
        raise


def _catalog_spec_for_type(exclusion_type: str) -> tuple[dict[str, dict], int]:
    exc_type = exclusion_type.upper()
    if exc_type == "ALLERGY":
        return ALLERGEN_BY_CODE, ALLERGEN_CATALOG_VERSION
    if exc_type == "UNABLE":
        return UNABLE_BY_CODE, UNABLE_CATALOG_VERSION
    raise ValueError(f"지원하지 않는 제외 유형입니다: {exclusion_type}")


def get_my_catalog_exclusions(
    db: Session,
    user_id: int,
    exclusion_type: str,
    for_update: bool = False,
) -> CatalogExclusionsResponse:
    exc_type = exclusion_type.upper()
    catalog_map, catalog_version = _catalog_spec_for_type(exc_type)
    rows = repository.find_exclusions_by_type(db, user_id, exc_type, for_update=for_update)

    items: list[CatalogExclusionItemResponse] = []
    legacy_items: list[LegacyExclusionItemResponse] = []
    codes: list[str] = []

    for row in rows:
        code = (row.catalog_code or "").strip()
        if code and code in catalog_map:
            spec = catalog_map[code]
            codes.append(code)
            items.append(
                CatalogExclusionItemResponse(
                    id=row.id,
                    code=code,
                    label=spec["name"],
                    keyword=row.keyword,
                    catalog_version=int(row.catalog_version or catalog_version),
                )
            )
        else:
            legacy_items.append(
                LegacyExclusionItemResponse(
                    id=row.id,
                    keyword=row.keyword,
                )
            )

    return CatalogExclusionsResponse(
        exclusion_type=exc_type,
        catalog_version=catalog_version,
        editable=len(legacy_items) == 0,
        codes=codes,
        items=items,
        legacy_items=legacy_items,
        total_count=len(items) + len(legacy_items),
    )


def replace_my_catalog_exclusions(
    db: Session,
    user_id: int,
    exclusion_type: str,
    request: CatalogExclusionsUpdateRequest,
    current_user: User | None = None,
) -> CatalogExclusionsResponse:
    exc_type = exclusion_type.upper()
    catalog_map, catalog_version = _catalog_spec_for_type(exc_type)
    try:
        _lock_preference_owner(db, user_id, current_user)
        existing_rows = repository.find_exclusions_by_type(db, user_id, exc_type, for_update=True)

        # 미매핑 레거시 자유 문자열 항목이 남아 있는 경우 전체 교체 차단 (409 EXCLUSION_MAPPING_REQUIRED)
        if any(not (row.catalog_code and row.catalog_code.strip() in catalog_map) for row in existing_rows):
            db.rollback()
            raise RecommendationDomainException(
                "표준 카탈로그 코드로 매핑되지 않은 기존 제외 항목이 있어 전체 교체를 수행할 수 없습니다. (EXCLUSION_MAPPING_REQUIRED)",
                status_code=409,
            )

        dedup_codes: list[str] = []
        seen_codes: set[str] = set()
        for raw_code in request.codes:
            cleaned = (raw_code or "").strip()
            if not cleaned or cleaned not in catalog_map:
                db.rollback()
                raise RecommendationDomainException(
                    f"유효하지 않은 카탈로그 코드입니다: {raw_code} (INVALID_CATALOG_CODE)",
                    status_code=400,
                )
            if cleaned not in seen_codes:
                seen_codes.add(cleaned)
                dedup_codes.append(cleaned)

        wanted_set = set(dedup_codes)
        retained: set[str] = set()
        for row in existing_rows:
            code = (row.catalog_code or "").strip()
            if code in wanted_set and code not in retained:
                spec = catalog_map[code]
                row.keyword = spec["name"]
                row.catalog_version = catalog_version
                retained.add(code)
            else:
                repository.remove_disliked_food(db, row)

        for code in dedup_codes:
            if code not in retained:
                spec = catalog_map[code]
                repository.add_catalog_exclusion(
                    db=db,
                    user_id=user_id,
                    exclusion_type=exc_type,
                    code=code,
                    label=spec["name"],
                    catalog_version=catalog_version,
                )

        db.flush()
        response = get_my_catalog_exclusions(db, user_id, exc_type, for_update=True)
        db.commit()
        return response
    except (SQLAlchemyError, HTTPException, RecommendationDomainException):
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
    current_user: User | None = None,
    idempotency_key: str | None = None,
) -> UserMealItemResponse:
    """로그인한 사용자의 식사 기록을 user_meals 테이블에 저장한다."""
    try:
        _lock_preference_owner(db, user_id, current_user)
        try:
            op_key = normalize_idempotency_key(idempotency_key)
        except ValueError as exc:
            raise RecommendationDomainException(str(exc), status_code=400) from exc

        payload_hash = compute_payload_hash(
            {
                "menu_id": int(request.menu_id),
                "eaten_at": request.eaten_at.isoformat() if request.eaten_at is not None else None,
            }
        )
        operation = None
        if op_key is not None:
            operation, is_existing = acquire_or_inspect_operation(
                db,
                user_id=user_id,
                auth_session_id=get_user_session_id(current_user) if current_user is not None else None,
                scope="USER_MEAL_CREATE",
                operation_key=op_key,
                payload_hash=payload_hash,
            )
            if is_existing:
                if operation.payload_hash != payload_hash:
                    db.rollback()
                    raise RecommendationDomainException(
                        "동일한 Idempotency-Key로 다른 식사 기록을 생성할 수 없습니다. (IDEMPOTENCY_KEY_REUSED)",
                        status_code=409,
                    )
                if operation.status == "SUCCEEDED" and operation.response_json:
                    db.rollback()
                    return UserMealItemResponse.model_validate(operation.response_json)

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
        db.flush()
        resp = _to_user_meal_item_response(meal, menu)
        if operation is not None:
            operation.status = "SUCCEEDED"
            operation.resource_id = str(meal.id)
            operation.response_status = 201
            operation.response_json = resp.model_dump(mode="json")
            operation.updated_at = utc_now()
        db.commit()
        return resp
    except (SQLAlchemyError, HTTPException, FoodNotFoundException, RecommendationDomainException):
        db.rollback()
        raise


def delete_my_meal(
    db: Session,
    user_id: int,
    meal_id: int,
    current_user: User | None = None,
) -> None:
    try:
        _lock_preference_owner(db, user_id, current_user)
        meal = repository.find_user_meal(db, user_id, meal_id)
        if meal is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="식사 기록을 찾을 수 없습니다.",
            )
        repository.remove_user_meal(db, meal)
        db.commit()
    except (SQLAlchemyError, HTTPException):
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

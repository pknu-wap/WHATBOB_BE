from datetime import datetime, timedelta, timezone
import secrets
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.credentials import (
    encrypt_kakao_token,
    shred_kakao_credential_tokens,
    shred_prepared_kakao_credential,
    shred_session_prepared_kakao_credentials,
)
from app.auth.errors import AuthError, database_errors
from app.auth.kakao import (
    exchange_kakao_authorization_code,
    get_kakao_access_token,
    get_kakao_user_info,
)
from app.auth.models import (
    AccountTransition,
    AuthSession,
    KakaoLinkGuard,
    KakaoSessionCredential,
    RequestOperation,
)
from app.auth.operations import (
    get_user_session_id,
    lock_user_and_verify_session,
    normalize_idempotency_key,
)
from app.auth.schemas import (
    PreparedKakaoTargetRead,
    TransitionCommitResponse,
    TransitionPrepareRequest,
    TransitionPrepareResponse,
    TransitionStatusResponse,
)
from app.auth.sessions import (
    DEFAULT_PROFILE_IMAGE,
    SESSION_SECONDS,
    as_utc_naive,
    authenticate_access_token,
    create_uuid_user,
    hash_oauth_state,
    hash_secret,
    require_secret,
    utc_now,
    validate_session,
)
from app.core.config import settings
from app.core.security import create_access_token
from app.map.models import PlaceSatisfaction, PlaceSelection
from app.menus.models import (
    OrderSheetHardFilterTrace,
    OrderSheetIssuance,
    QuestionSelectionState,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
    RecommendationItemSnapshot,
    UserFoodExclusion,
    UserMeal,
)
from app.rooms.models import Room, RoomMember, RoomMemberOrderSheet
from app.rooms.service import has_active_or_pending_room_activity
from app.rooms.state import prepare_room_notifications, close_pending_notifications
from app.users.models import User
from app.users.profile_image import delete_profile_image_if_unreferenced
from app.users.schemas import UserResponse


def _as_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_default_get_kakao_access_token = get_kakao_access_token


async def resolve_kakao_profile_from_code(code: str) -> dict:
    """카카오 인가 코드로 사용자 프로필을 조회한다. 원문 액세스 토큰은 반환·저장하지 않는다."""
    cleaned_code = (code or "").strip()
    if not cleaned_code:
        raise AuthError("KAKAO_CODE_REQUIRED", "카카오 인가 코드가 필요합니다.", 400)
    kakao_access_token = await get_kakao_access_token(cleaned_code)
    if isinstance(kakao_access_token, dict):
        kakao_access_token = kakao_access_token.get("access_token")
    if not kakao_access_token:
        raise AuthError("KAKAO_AUTH_FAILED", "카카오 액세스 토큰을 발급받지 못했습니다.", 400)
    profile = await get_kakao_user_info(kakao_access_token)
    return {
        "kakao_id": int(profile["kakao_id"]),
        "nickname": profile.get("nickname"),
        "profile_image": profile.get("profile_image"),
        "email": profile.get("email"),
    }


_default_resolve_kakao_profile_from_code = resolve_kakao_profile_from_code


def mask_kakao_email(email: str | None) -> str | None:
    if not email or not isinstance(email, str):
        return None
    cleaned = email.strip()
    if "@" not in cleaned:
        return None
    local, domain = cleaned.split("@", 1)
    local = local.strip()
    domain = domain.strip()
    if not local or not domain:
        return None
    prefix = local[0] if len(local) <= 2 else local[:2]
    return f"{prefix}***@{domain}"


def _extract_prepared_target_account(provider_context: dict | None) -> PreparedKakaoTargetRead | None:
    if not isinstance(provider_context, dict):
        return None
    raw_nick = provider_context.get("nickname")
    nickname = raw_nick.strip() if isinstance(raw_nick, str) and raw_nick.strip() else None
    masked_email = mask_kakao_email(provider_context.get("email"))
    if nickname is None and masked_email is None:
        return None
    return PreparedKakaoTargetRead(nickname=nickname, masked_email=masked_email)


async def _resolve_kakao_profile_and_tokens(
    code: str,
    *,
    redirect_uri: str | None = None,
) -> tuple[dict, dict | None]:
    cleaned_code = (code or "").strip()
    if not cleaned_code:
        raise AuthError("KAKAO_CODE_REQUIRED", "카카오 인가 코드가 필요합니다.", 400)

    if resolve_kakao_profile_from_code is _default_resolve_kakao_profile_from_code:
        if get_kakao_access_token is _default_get_kakao_access_token:
            try:
                raw_tokens = await exchange_kakao_authorization_code(
                    cleaned_code,
                    redirect_uri=redirect_uri,
                )
            except TypeError:
                raw_tokens = await exchange_kakao_authorization_code(cleaned_code)
        else:
            raw_res = await get_kakao_access_token(cleaned_code)
            raw_tokens = (
                raw_res
                if isinstance(raw_res, dict)
                else {
                    "access_token": raw_res,
                    "refresh_token": None,
                    "expires_in": None,
                    "refresh_token_expires_in": None,
                }
            )
        access_token = (raw_tokens or {}).get("access_token")
        if not access_token:
            raise AuthError("KAKAO_AUTH_FAILED", "카카오 액세스 토큰을 발급받지 못했습니다.", 400)
        profile = await get_kakao_user_info(access_token)
        clean_profile = {
            "kakao_id": int(profile["kakao_id"]),
            "nickname": profile.get("nickname"),
            "profile_image": profile.get("profile_image"),
            "email": profile.get("email"),
        }
        return clean_profile, {
            "access_token": access_token,
            "refresh_token": raw_tokens.get("refresh_token"),
            "expires_in": raw_tokens.get("expires_in"),
            "refresh_token_expires_in": raw_tokens.get("refresh_token_expires_in"),
        }

    raw = await resolve_kakao_profile_from_code(code)
    token_bundle: dict | None = None
    if isinstance(raw.get("kakao_tokens"), dict):
        token_bundle = dict(raw["kakao_tokens"])
    elif raw.get("access_token") or raw.get("refresh_token"):
        token_bundle = {
            "access_token": raw.get("access_token"),
            "refresh_token": raw.get("refresh_token"),
            "expires_in": raw.get("expires_in"),
            "refresh_token_expires_in": raw.get("refresh_token_expires_in"),
        }
    clean_profile = {
        "kakao_id": int(raw["kakao_id"]),
        "nickname": raw.get("nickname"),
        "profile_image": raw.get("profile_image"),
        "email": raw.get("email"),
    }
    return clean_profile, token_bundle


def _verify_auth_context_and_cookie(
    db: Session,
    current_user: User,
    expected_context: str | None,
    auth_cookie_secret: str | None,
) -> int:
    session_id = get_user_session_id(current_user)
    if session_id is None:
        raise AuthError("SESSION_NOT_FOUND", "유효한 인증 세션을 확인할 수 없습니다.", 401, bearer=True)

    if expected_context is not None and expected_context.strip():
        ctx = expected_context.strip()
        if ctx != str(session_id) and ctx != current_user.uuid:
            raise AuthError("AUTH_CONTEXT_CHANGED", "다른 탭에서 계정이 변경되었습니다.", 409)

    if auth_cookie_secret is not None:
        cookie_hash = hash_secret(auth_cookie_secret)
        cookie_session = (
            db.execute(select(AuthSession).where(AuthSession.secret_hash == cookie_hash))
            .scalar_one_or_none()
        )
        if cookie_session is None or int(cookie_session.id) != int(session_id):
            raise AuthError("AUTH_CONTEXT_CHANGED", "현재 브라우저 쿠키와 인증 세션이 일치하지 않습니다.", 409)

    return int(session_id)


def _acquire_kakao_link_guard(db: Session, kakao_id: int, now: datetime) -> KakaoLinkGuard:
    existing = db.get(KakaoLinkGuard, int(kakao_id))
    if existing is None:
        try:
            with db.begin_nested():
                db.add(KakaoLinkGuard(kakao_id=int(kakao_id), created_at=now))
                db.flush()
        except IntegrityError:
            pass
    return (
        db.execute(
            select(KakaoLinkGuard)
            .where(KakaoLinkGuard.kakao_id == int(kakao_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalar_one()
    )


def _cleanup_closed_room_references_for_origin_user(db: Session, origin_user_id: int, now: datetime) -> None:
    """
    이미 만료되었거나 CLOSED 상태인 방에 남아 있는 원본 사용자의 잔여 참조를 정리해
    원본 User 물리 삭제(R3A) 시 RESTRICT FK 충돌이 발생하지 않도록 한다.
    (활성·유예 중 방 참여는 이 함수 호출 전에 409 ROOM_PARTICIPATION_ACTIVE로 차단됨)
    """
    hosted_room_ids = [
        int(r[0])
        for r in db.query(Room.id).filter(Room.host_user_id == origin_user_id).with_for_update().all()
    ]
    member_ids = [
        int(m[0])
        for m in db.query(RoomMember.id)
        .filter(
            (RoomMember.user_id == origin_user_id)
            | (RoomMember.room_id.in_(hosted_room_ids) if hosted_room_ids else False)
        )
        .with_for_update()
        .all()
    ]
    if member_ids:
        db.query(RoomMemberOrderSheet).filter(
            RoomMemberOrderSheet.room_member_id.in_(member_ids)
        ).delete(synchronize_session=False)
        db.query(RoomMember).filter(
            RoomMember.id.in_(member_ids)
        ).delete(synchronize_session=False)
    from app.rooms.models import RoomRoundCandidateProposer, RoomRoundMember
    from app.rooms.rounds import delete_room_rounds_for_rooms
    origin_rm_ids = [
        int(rm[0])
        for rm in db.query(RoomRoundMember.id)
        .filter(RoomRoundMember.user_id == origin_user_id)
        .all()
    ]
    if origin_rm_ids:
        db.query(RoomRoundCandidateProposer).filter(
            (RoomRoundCandidateProposer.user_id == origin_user_id)
            | (RoomRoundCandidateProposer.round_member_id.in_(origin_rm_ids))
        ).delete(synchronize_session=False)
        db.query(RoomRoundMember).filter(
            RoomRoundMember.id.in_(origin_rm_ids)
        ).delete(synchronize_session=False)
    else:
        db.query(RoomRoundCandidateProposer).filter(
            RoomRoundCandidateProposer.user_id == origin_user_id
        ).delete(synchronize_session=False)
    if hosted_room_ids:
        delete_room_rounds_for_rooms(db, hosted_room_ids)
        for room_id in hosted_room_ids:
            close_pending_notifications(db, room_id, now)
            db.info.get("changed_rooms", {}).pop(room_id, None)
        db.query(Room).filter(
            Room.id.in_(hosted_room_ids)
        ).delete(synchronize_session=False)
    db.flush()


def _exclusion_dedup_keys(row: UserFoodExclusion) -> set[tuple[str, str, str]]:
    exc_type = (row.exclusion_type or "").upper()
    keys: set[tuple[str, str, str]] = set()
    kw = (row.keyword or "").strip().casefold()
    if kw:
        keys.add((exc_type, "KW", kw))
    if exc_type in {"ALLERGY", "UNABLE"} and row.catalog_code and row.catalog_code.strip():
        keys.add((exc_type, "CODE", row.catalog_code.strip()))
    return keys


def _merge_exclusions_lossless(db: Session, origin_user_id: int, target_user_id: int) -> None:
    target_rows = (
        db.query(UserFoodExclusion)
        .filter(UserFoodExclusion.user_id == target_user_id)
        .order_by(UserFoodExclusion.created_at.asc(), UserFoodExclusion.id.asc())
        .with_for_update()
        .all()
    )
    origin_rows = (
        db.query(UserFoodExclusion)
        .filter(UserFoodExclusion.user_id == origin_user_id)
        .order_by(UserFoodExclusion.created_at.asc(), UserFoodExclusion.id.asc())
        .with_for_update()
        .all()
    )
    seen_keys: set[tuple[str, str, str]] = set()
    for row in target_rows:
        seen_keys.update(_exclusion_dedup_keys(row))

    for row in origin_rows:
        row_keys = _exclusion_dedup_keys(row)
        if row_keys & seen_keys:
            db.delete(row)
        else:
            row.user_id = target_user_id
            seen_keys.update(row_keys)
    db.flush()


def _merge_question_selection_state(
    db: Session,
    origin_user_id: int,
    target_user_id: int,
    now: datetime,
) -> None:
    origin_key = f"user:{origin_user_id}"
    target_key = f"user:{target_user_id}"
    states = (
        db.query(QuestionSelectionState)
        .filter(QuestionSelectionState.scope_key.in_([origin_key, target_key]))
        .order_by(QuestionSelectionState.scope_key.asc())
        .with_for_update()
        .all()
    )
    by_key = {row.scope_key: row for row in states}
    origin_state = by_key.get(origin_key)
    if origin_state is None:
        return
    target_state = by_key.get(target_key)
    if target_state is None:
        db.add(
            QuestionSelectionState(
                scope_key=target_key,
                topic_counts=dict(origin_state.topic_counts or {}),
                last_clusters=list(origin_state.last_clusters or []),
                updated_at=origin_state.updated_at or now,
            )
        )
        db.delete(origin_state)
        db.flush()
        return

    merged_counts: dict[str, int] = {}
    for src in (target_state.topic_counts or {}, origin_state.topic_counts or {}):
        if isinstance(src, dict):
            for k, v in src.items():
                merged_counts[str(k)] = merged_counts.get(str(k), 0) + int(v or 0)

    origin_updated = as_utc_naive(origin_state.updated_at) if origin_state.updated_at else now
    target_updated = as_utc_naive(target_state.updated_at) if target_state.updated_at else now
    if (origin_updated, origin_user_id) >= (target_updated, target_user_id):
        chosen_last = list(origin_state.last_clusters or [])
        chosen_updated = origin_state.updated_at or now
    else:
        chosen_last = list(target_state.last_clusters or [])
        chosen_updated = target_state.updated_at or now

    target_state.topic_counts = merged_counts
    target_state.last_clusters = chosen_last
    target_state.updated_at = chosen_updated
    db.delete(origin_state)
    db.flush()


def _merge_request_operations(db: Session, origin_user_id: int, target_user_id: int) -> None:
    target_ops = (
        db.query(RequestOperation)
        .filter(RequestOperation.user_id == target_user_id)
        .with_for_update()
        .all()
    )
    target_keys = {(op.scope, op.operation_key) for op in target_ops}
    origin_ops = (
        db.query(RequestOperation)
        .filter(RequestOperation.user_id == origin_user_id)
        .with_for_update()
        .all()
    )
    for op in origin_ops:
        if (op.scope, op.operation_key) in target_keys:
            db.delete(op)
        else:
            op.user_id = target_user_id
            target_keys.add((op.scope, op.operation_key))
    db.flush()


def _merge_order_sheet_issuances(db: Session, origin_user_id: int, target_user_id: int) -> None:
    target_rows = (
        db.query(OrderSheetIssuance)
        .filter(OrderSheetIssuance.user_id == target_user_id)
        .with_for_update()
        .all()
    )
    target_keys = {row.issuance_key for row in target_rows if row.issuance_key is not None}
    origin_rows = (
        db.query(OrderSheetIssuance)
        .filter(OrderSheetIssuance.user_id == origin_user_id)
        .with_for_update()
        .all()
    )
    for row in origin_rows:
        if row.issuance_key is not None and row.issuance_key in target_keys:
            row.issuance_key = None
        elif row.issuance_key is not None:
            target_keys.add(row.issuance_key)
        row.user_id = target_user_id
    db.flush()


def _rebuild_feedback_states_for_recommendations(
    db: Session,
    target_user_id: int,
    recommendation_ids: list[int],
    now: datetime,
) -> None:
    if not recommendation_ids:
        return
    db.query(RecommendationFeedbackState).filter(
        RecommendationFeedbackState.recommendation_id.in_(recommendation_ids)
    ).delete(synchronize_session=False)
    db.flush()

    feedbacks = (
        db.query(RecommendationFeedback)
        .filter(RecommendationFeedback.recommendation_id.in_(recommendation_ids))
        .order_by(RecommendationFeedback.id.asc())
        .with_for_update()
        .all()
    )
    latest_by_rec_and_menu: dict[tuple[int, str], RecommendationFeedback] = {}
    for fb in feedbacks:
        mkey = fb.menu_key or (f"id:{fb.selected_menu_id}" if fb.selected_menu_id is not None else "unknown")
        latest_by_rec_and_menu[(int(fb.recommendation_id), mkey)] = fb

    for (rec_id, mkey), fb in latest_by_rec_and_menu.items():
        fb_created = as_utc_naive(fb.created_at) if fb.created_at else now
        db.add(
            RecommendationFeedbackState(
                user_id=target_user_id,
                recommendation_id=rec_id,
                selected_menu_id=fb.selected_menu_id,
                food_kind_id=fb.food_kind_id,
                exposure_rank=fb.exposure_rank,
                menu_key=mkey,
                menu_base_name=fb.menu_base_name or mkey,
                menu_main_ingredient=fb.menu_main_ingredient or "other",
                latest_feedback_id=fb.id,
                is_satisfied=bool(fb.is_satisfied),
                answer_match_results=dict(fb.answer_match_results or {}),
                created_at=fb_created,
                updated_at=fb_created,
            )
        )
    db.flush()


def _apply_profile_priority_on_merge(
    db: Session,
    origin_user: User,
    target_user: User,
    *,
    is_new_target: bool,
    kakao_email: str | None,
    now: datetime,
) -> list[str]:
    """
    필드별 프로필 선택 규칙:
    1. 기존 legacy 카카오 계정(is_legacy_kakao) 또는 카카오 계정에서 직접 수정한 필드(*_edited == True)는 최우선 보존한다.
    2. 그렇지 않으면 가장 먼저 생성된 이관 UUID 출처((source_created_at, source_id) 오름차순)의 값을 선택한다.
    3. 원본 User의 nickname_key는 같은 트랜잭션 안에서 먼저 NULL로 해제한 뒤 대상에 부여한다.
    반환값: 참조가 해제되어 commit 후 삭제 후보가 된 이전 이미지 URL 목록
    """
    origin_created = as_utc_naive(origin_user.created_at) if origin_user.created_at else now
    origin_nick_created = (
        as_utc_naive(origin_user.nickname_source_created_at)
        if origin_user.nickname_source_created_at
        else origin_created
    )
    origin_nick_id = int(origin_user.nickname_source_id or origin_user.id)
    origin_nick_uuid = origin_user.nickname_source_uuid or origin_user.uuid
    origin_nick_edited = bool(origin_user.nickname_source_edited or origin_user.nickname_edited)

    origin_img_created = (
        as_utc_naive(origin_user.profile_image_source_created_at)
        if origin_user.profile_image_source_created_at
        else origin_created
    )
    origin_img_id = int(origin_user.profile_image_source_id or origin_user.id)
    origin_img_uuid = origin_user.profile_image_source_uuid or origin_user.uuid
    origin_img_edited = bool(origin_user.profile_image_source_edited or origin_user.profile_image_edited)

    origin_nickname = origin_user.nickname
    origin_nickname_key = origin_user.nickname_key
    origin_profile_image = origin_user.profile_image or DEFAULT_PROFILE_IMAGE

    # 먼저 원본 사용자의 유일 닉네임 키를 해제해 대상 설정 시 UNIQUE 충돌을 방지
    origin_user.nickname_key = None
    db.flush()

    replaced_images: list[str] = []

    if is_new_target:
        target_user.nickname = origin_nickname
        target_user.nickname_key = origin_nickname_key
        target_user.nickname_edited = False
        target_user.nickname_source_edited = origin_nick_edited
        target_user.nickname_source_uuid = origin_nick_uuid
        target_user.nickname_source_id = origin_nick_id
        target_user.nickname_source_created_at = origin_nick_created

        target_user.profile_image = origin_profile_image
        target_user.profile_image_edited = False
        target_user.profile_image_source_edited = origin_img_edited
        target_user.profile_image_source_uuid = origin_img_uuid
        target_user.profile_image_source_id = origin_img_id
        target_user.profile_image_source_created_at = origin_img_created
    else:
        # 1. 닉네임 필드 독립 비교
        if not (target_user.is_legacy_kakao or target_user.nickname_edited):
            target_nick_created = (
                as_utc_naive(target_user.nickname_source_created_at)
                if target_user.nickname_source_created_at
                else None
            )
            target_nick_id = int(target_user.nickname_source_id or target_user.id)
            if (
                target_nick_created is None
                or (origin_nick_created, origin_nick_id) < (target_nick_created, target_nick_id)
            ):
                target_user.nickname = origin_nickname
                target_user.nickname_key = origin_nickname_key
                target_user.nickname_source_edited = origin_nick_edited
                target_user.nickname_source_uuid = origin_nick_uuid
                target_user.nickname_source_id = origin_nick_id
                target_user.nickname_source_created_at = origin_nick_created

        # 2. 프로필 이미지 필드 독립 비교
        old_target_img = target_user.profile_image
        if target_user.is_legacy_kakao or target_user.profile_image_edited:
            if origin_profile_image and origin_profile_image != old_target_img:
                replaced_images.append(origin_profile_image)
        else:
            target_img_created = (
                as_utc_naive(target_user.profile_image_source_created_at)
                if target_user.profile_image_source_created_at
                else None
            )
            target_img_id = int(target_user.profile_image_source_id or target_user.id)
            if (
                target_img_created is None
                or (origin_img_created, origin_img_id) < (target_img_created, target_img_id)
            ):
                if old_target_img and old_target_img != origin_profile_image:
                    replaced_images.append(old_target_img)
                target_user.profile_image = origin_profile_image
                target_user.profile_image_source_edited = origin_img_edited
                target_user.profile_image_source_uuid = origin_img_uuid
                target_user.profile_image_source_id = origin_img_id
                target_user.profile_image_source_created_at = origin_img_created
            else:
                if origin_profile_image and origin_profile_image != old_target_img:
                    replaced_images.append(origin_profile_image)

    if target_user.email is None and kakao_email:
        target_user.email = kakao_email

    db.flush()
    return replaced_images


def _merge_uuid_user_into_kakao_target(
    db: Session,
    origin_user: User,
    target_user: User | None,
    *,
    kakao_id: int,
    kakao_email: str | None,
    now: datetime,
) -> tuple[User, bool, list[str]]:
    is_new_target = target_user is None
    if target_user is None:
        target_user = User(
            kakao_id=int(kakao_id),
            email=kakao_email,
            is_legacy_kakao=False,
            profile_image=DEFAULT_PROFILE_IMAGE,
            created_at=now,
            updated_at=now,
        )
        db.add(target_user)
        db.flush()

    origin_id = int(origin_user.id)
    target_id = int(target_user.id)

    replaced_images = _apply_profile_priority_on_merge(
        db,
        origin_user,
        target_user,
        is_new_target=is_new_target,
        kakao_email=kakao_email,
        now=now,
    )

    # 1. 음식 제외 조건 타입별 합집합 병합 (100개 초과도 무손실 보존)
    _merge_exclusions_lossless(db, origin_id, target_id)

    # 2. 식사 기록 무손실 이동 (동일 메뉴·시각도 중복 제거하지 않고 원래 ID·시각 보존)
    db.query(UserMeal).filter(UserMeal.user_id == origin_id).update(
        {UserMeal.user_id: target_id},
        synchronize_session=False,
    )

    # 3. 추천 및 피드백 이력 무손실 이동 (원래 ID 및 평가 순서 보존)
    moved_rec_ids = [
        int(r[0])
        for r in db.query(Recommendation.id).filter(Recommendation.user_id == origin_id).all()
    ]
    db.query(Recommendation).filter(Recommendation.user_id == origin_id).update(
        {Recommendation.user_id: target_id},
        synchronize_session=False,
    )
    db.query(RecommendationFeedback).filter(RecommendationFeedback.user_id == origin_id).update(
        {RecommendationFeedback.user_id: target_id},
        synchronize_session=False,
    )
    db.query(RecommendationItemSnapshot).filter(
        RecommendationItemSnapshot.user_id == origin_id
    ).update(
        {RecommendationItemSnapshot.user_id: target_id},
        synchronize_session=False,
    )
    db.query(OrderSheetHardFilterTrace).filter(
        OrderSheetHardFilterTrace.user_id == origin_id
    ).update(
        {OrderSheetHardFilterTrace.user_id: target_id},
        synchronize_session=False,
    )
    db.query(RecommendationFeedbackState).filter(
        RecommendationFeedbackState.user_id == origin_id
    ).update(
        {RecommendationFeedbackState.user_id: target_id},
        synchronize_session=False,
    )
    db.query(PlaceSelection).filter(PlaceSelection.user_id == origin_id).update(
        {PlaceSelection.user_id: target_id},
        synchronize_session=False,
    )
    db.query(PlaceSatisfaction).filter(PlaceSatisfaction.user_id == origin_id).update(
        {PlaceSatisfaction.user_id: target_id},
        synchronize_session=False,
    )
    db.flush()
    _rebuild_feedback_states_for_recommendations(db, target_id, moved_rec_ids, now=now)

    # 4. 질문 선택 순환 상태 합산 후 원본 user:{origin_id} 행 삭제
    _merge_question_selection_state(db, origin_id, target_id, now=now)

    # 5. 작업 원장(request_operations) 및 주문서 발급 이력(order_sheet_issuances) 소유자 이동
    _merge_request_operations(db, origin_id, target_id)
    _merge_order_sheet_issuances(db, origin_id, target_id)

    # 6. 다른 PREPARED 전환 작업의 origin_user_id 참조 해제
    db.query(AccountTransition).filter(AccountTransition.origin_user_id == origin_id).update(
        {AccountTransition.origin_user_id: None},
        synchronize_session=False,
    )
    db.flush()

    return target_user, is_new_target, replaced_images


async def prepare_account_transition(
    db: Session,
    current_user: User,
    request: TransitionPrepareRequest,
    *,
    expected_context: str | None = None,
    auth_cookie_secret: str | None = None,
    idempotency_key_header: str | None = None,
    default_redirect_uri: str | None = None,
) -> tuple[TransitionPrepareResponse, str]:
    now = utc_now()
    session_id = _verify_auth_context_and_cookie(
        db, current_user, expected_context, auth_cookie_secret
    )

    raw_op_key = request.operation_key or idempotency_key_header
    try:
        op_key = normalize_idempotency_key(raw_op_key)
    except ValueError as exc:
        raise AuthError("INVALID_IDEMPOTENCY_KEY", str(exc), 400) from exc

    # 준비 응답 유실 등으로 동일 세션의 기존 PREPARED 전환을 재준비하는 경우(일회용 인가 코드 재소비 방지)
    if request.transition_id or op_key:
        with database_errors(db):
            locked_user = lock_user_and_verify_session(db, current_user)
            query = db.query(AccountTransition).filter(
                AccountTransition.origin_session_id == session_id,
                AccountTransition.action == request.action,
            )
            if request.transition_id:
                query = query.filter(AccountTransition.transition_id == request.transition_id)
            elif op_key:
                query = query.filter(AccountTransition.operation_key == op_key)
            existing = query.with_for_update().first()
            if existing is not None:
                if existing.status != "PREPARED" or now >= as_utc_naive(existing.prepare_expires_at):
                    if existing.status == "PREPARED" and now >= as_utc_naive(existing.prepare_expires_at):
                        existing.status = "EXPIRED"
                        existing.updated_at = now
                        shred_prepared_kakao_credential(
                            db, existing.transition_id, status="EXPIRED", now=now
                        )
                        db.commit()
                    else:
                        db.rollback()
                    raise AuthError(
                        "TRANSITION_ALREADY_FINALIZED",
                        "이미 처리되었거나 만료된 전환 작업입니다.",
                        409,
                    )
                check_ids = [locked_user.id]
                if request.action == "KAKAO_LINK" and isinstance(existing.provider_context_json, dict):
                    kid = existing.provider_context_json.get("kakao_id")
                    if kid is not None:
                        target_u = db.query(User).filter(User.kakao_id == int(kid)).first()
                        if target_u is not None:
                            check_ids.append(target_u.id)
                if has_active_or_pending_room_activity(db, check_ids, now=now):
                    db.rollback()
                    raise AuthError(
                        "ROOM_PARTICIPATION_ACTIVE",
                        "방 참여 또는 재접속 유예 중에는 계정을 전환할 수 없습니다.",
                        409,
                    )
                new_secret = secrets.token_urlsafe(32)
                secret_hash = hash_secret(new_secret)
                existing.prepare_secret_hash = secret_hash
                existing.recovery_secret_hash = secret_hash
                existing.prepare_expires_at = now + timedelta(seconds=int(settings.TRANSITION_PREPARE_SECONDS))
                existing.recovery_expires_at = now + timedelta(seconds=int(settings.TRANSITION_RECOVERY_SECONDS))
                existing.updated_at = now
                prepare_room_notifications(db)
                db.commit()
                return (
                    TransitionPrepareResponse(
                        transition_id=existing.transition_id,
                        action=existing.action,
                        status="PREPARED",
                        expires_at=_as_utc_aware(existing.prepare_expires_at),
                        target_account=(
                            _extract_prepared_target_account(existing.provider_context_json)
                            if existing.action == "KAKAO_LINK"
                            else None
                        ),
                    ),
                    new_secret,
                )
            db.rollback()

    provider_context: dict | None = None
    kakao_token_bundle: dict | None = None
    target_account: PreparedKakaoTargetRead | None = None
    if request.action == "KAKAO_LINK":
        if current_user.kakao_id is not None:
            raise AuthError("ALREADY_LINKED_ACCOUNT", "이미 카카오 계정이 연결된 사용자입니다.", 409)
        if request.expected_state is not None:
            if request.state is None or request.state != request.expected_state:
                raise AuthError("INVALID_OAUTH_STATE", "카카오 로그인 state 검증에 실패했습니다.", 400)

        origin_session_pre = db.get(AuthSession, session_id)
        if origin_session_pre is not None and origin_session_pre.oauth_state_hash is not None:
            if not request.state or not request.state.strip():
                raise AuthError("INVALID_OAUTH_STATE", "카카오 로그인 state 검증에 실패했습니다.", 400)
            if (
                origin_session_pre.oauth_state_expires_at is None
                or now >= as_utc_naive(origin_session_pre.oauth_state_expires_at)
            ):
                raise AuthError("INVALID_OAUTH_STATE", "카카오 로그인 state 유효 시간이 지났습니다.", 400)
            if not secrets.compare_digest(
                bytes(origin_session_pre.oauth_state_hash),
                hash_oauth_state(request.state),
            ):
                raise AuthError("INVALID_OAUTH_STATE", "카카오 로그인 state 검증에 실패했습니다.", 400)

        if not request.code or not request.code.strip():
            raise AuthError("KAKAO_CODE_REQUIRED", "카카오 인가 코드가 필요합니다.", 400)

        # 외부 HTTP 호출 전 현재 사용자의 방 참여 여부를 먼저 확인하고 트랜잭션을 비워 잠금을 보유하지 않는다.
        if has_active_or_pending_room_activity(db, [current_user.id], now=now):
            db.rollback()
            raise AuthError(
                "ROOM_PARTICIPATION_ACTIVE",
                "방 참여 또는 재접속 유예 중에는 계정을 전환할 수 없습니다.",
                409,
            )
        db.rollback()
        effective_redirect_uri = (
            request.redirect_uri or default_redirect_uri or settings.KAKAO_REDIRECT_URI
        ).strip()
        provider_context, kakao_token_bundle = await _resolve_kakao_profile_and_tokens(
            request.code,
            redirect_uri=effective_redirect_uri,
        )
        target_account = _extract_prepared_target_account(provider_context)
        if target_account is None:
            raise AuthError(
                "KAKAO_TARGET_UNVERIFIABLE",
                "연결할 카카오 계정의 닉네임이나 이메일을 확인할 수 없어 이관을 중단했습니다.",
                422,
            )

    now = utc_now()
    with database_errors(db):
        locked_user = lock_user_and_verify_session(db, current_user)
        check_user_ids = [locked_user.id]
        if request.action == "KAKAO_LINK":
            if locked_user.kakao_id is not None:
                db.rollback()
                raise AuthError("ALREADY_LINKED_ACCOUNT", "이미 카카오 계정이 연결된 사용자입니다.", 409)
            locked_session = (
                db.execute(
                    select(AuthSession)
                    .where(AuthSession.id == session_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                .scalar_one_or_none()
            )
            validate_session(locked_session)
            if locked_session.oauth_state_hash is not None:
                if (
                    not request.state
                    or not request.state.strip()
                    or locked_session.oauth_state_expires_at is None
                    or now >= as_utc_naive(locked_session.oauth_state_expires_at)
                    or not secrets.compare_digest(
                        bytes(locked_session.oauth_state_hash),
                        hash_oauth_state(request.state),
                    )
                ):
                    db.rollback()
                    raise AuthError("INVALID_OAUTH_STATE", "카카오 로그인 state 검증에 실패했습니다.", 400)
                locked_session.oauth_state_hash = None
                locked_session.oauth_state_expires_at = None
                db.flush()

            kakao_id = int(provider_context["kakao_id"])
            target_user = db.query(User).filter(User.kakao_id == kakao_id).first()
            if target_user is not None:
                check_user_ids.append(target_user.id)

        if has_active_or_pending_room_activity(db, check_user_ids, now=now):
            db.rollback()
            raise AuthError(
                "ROOM_PARTICIPATION_ACTIVE",
                "방 참여 또는 재접속 유예 중에는 계정을 전환할 수 없습니다.",
                409,
            )

        prepare_secret = secrets.token_urlsafe(32)
        secret_hash = hash_secret(prepare_secret)
        prepare_expires = now + timedelta(seconds=int(settings.TRANSITION_PREPARE_SECONDS))
        recovery_expires = now + timedelta(seconds=int(settings.TRANSITION_RECOVERY_SECONDS))

        transition = AccountTransition(
            transition_id=str(uuid4()),
            operation_key=op_key,
            action=request.action,
            status="PREPARED",
            origin_user_id=locked_user.id,
            origin_uuid=locked_user.uuid,
            origin_session_id=session_id,
            provider_context_json=provider_context,
            prepare_secret_hash=secret_hash,
            recovery_secret_hash=secret_hash,
            prepare_expires_at=prepare_expires,
            recovery_expires_at=recovery_expires,
            created_at=now,
            updated_at=now,
        )
        db.add(transition)
        db.flush()

        if request.action == "KAKAO_LINK":
            db.query(AccountTransition).filter(
                AccountTransition.origin_session_id == session_id,
                AccountTransition.action == "KAKAO_LINK",
                AccountTransition.status == "PREPARED",
                AccountTransition.transition_id != transition.transition_id,
            ).update(
                {AccountTransition.status: "CANCELLED", AccountTransition.updated_at: now},
                synchronize_session=False,
            )
            shred_session_prepared_kakao_credentials(
                db,
                session_id,
                status="CANCELLED",
                now=now,
                exclude_transition_id=transition.transition_id,
            )

        if request.action == "KAKAO_LINK" and kakao_token_bundle:
            enc_access = encrypt_kakao_token(kakao_token_bundle.get("access_token"))
            enc_refresh = encrypt_kakao_token(kakao_token_bundle.get("refresh_token"))
            if enc_access or enc_refresh:
                exp_in = kakao_token_bundle.get("expires_in")
                ref_exp_in = kakao_token_bundle.get("refresh_token_expires_in")
                access_exp_at = (
                    now + timedelta(seconds=int(exp_in)) if exp_in is not None else None
                )
                refresh_exp_at = (
                    now + timedelta(seconds=int(ref_exp_in)) if ref_exp_in is not None else None
                )
                db.add(
                    KakaoSessionCredential(
                        transition_id=transition.transition_id,
                        auth_session_id=None,
                        kakao_id=int(provider_context["kakao_id"]),
                        encrypted_access_token=enc_access,
                        encrypted_refresh_token=enc_refresh,
                        access_token_expires_at=access_exp_at,
                        refresh_token_expires_at=refresh_exp_at,
                        status="PREPARED",
                        attempt_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.flush()

        prepare_room_notifications(db)
        db.commit()
        return (
            TransitionPrepareResponse(
                transition_id=transition.transition_id,
                action=transition.action,
                status="PREPARED",
                expires_at=_as_utc_aware(prepare_expires),
                target_account=target_account,
            ),
            prepare_secret,
        )


def _resolve_logout_kakao_status(
    db: Session,
    transition: AccountTransition,
) -> str | None:
    if transition.action != "LOGOUT" or transition.status != "COMMITTED":
        return None
    cred = (
        db.query(KakaoSessionCredential)
        .filter(KakaoSessionCredential.logout_transition_id == transition.transition_id)
        .first()
    )
    if cred is None:
        return "SKIPPED_NO_CREDENTIALS"
    if cred.status == "PENDING_REVOCATION":
        return "PENDING"
    if cred.status in ("REVOKED", "FAILED", "SKIPPED_NO_CREDENTIALS"):
        return cred.status
    return "PENDING"


def _build_committed_response_from_transition(
    db: Session,
    transition: AccountTransition,
    now: datetime,
) -> TransitionCommitResponse:
    if now >= as_utc_naive(transition.recovery_expires_at):
        raise AuthError("TRANSITION_RECOVERY_EXPIRED", "전환 결과 복구 기한이 지났습니다.", 401)
    if transition.result_session_id is None or transition.result_user_id is None:
        raise AuthError("SESSION_UNAVAILABLE", "전환 결과 세션을 확인할 수 없습니다.", 401)

    result_session = db.get(AuthSession, int(transition.result_session_id))
    validate_session(result_session)
    result_user = db.get(User, int(transition.result_user_id))
    if result_user is None or int(result_session.user_id or 0) != int(result_user.id):
        raise AuthError("SESSION_UNAVAILABLE", "전환 대상 사용자를 확인할 수 없습니다.", 401)

    access_token = create_access_token({"sub": str(result_user.id), "sid": str(result_session.id)})
    kakao_logout_status = _resolve_logout_kakao_status(db, transition)
    transition.recovered_at = now
    transition.updated_at = now
    prepare_room_notifications(db)
    db.commit()
    return TransitionCommitResponse(
        transition_id=transition.transition_id,
        action=transition.action,
        status="COMMITTED",
        access_token=access_token,
        user=UserResponse.model_validate(result_user),
        is_new_user=bool(transition.result_is_new_user),
        requires_reauth=bool(getattr(result_session, "requires_reauth", False)),
        kakao_logout_status=kakao_logout_status,
    )


def commit_account_transition(
    db: Session,
    transition_id: str,
    *,
    prepare_secret: str | None,
    expected_context: str | None = None,
    bearer_token: str | None = None,
) -> tuple[TransitionCommitResponse, str]:
    if not prepare_secret:
        raise AuthError("TRANSITION_COOKIE_MISSING", "전환 준비 쿠키가 필요합니다.", 401)
    secret_hash = require_secret(prepare_secret)
    now = utc_now()

    for attempt in range(3):
        try:
            transition = (
                db.query(AccountTransition)
                .filter(AccountTransition.transition_id == transition_id)
                .first()
            )
            if transition is None:
                raise AuthError("TRANSITION_NOT_FOUND", "계정 전환 작업을 찾을 수 없습니다.", 404)

            if expected_context is not None and expected_context.strip():
                ctx = expected_context.strip()
                valid_contexts = {
                    str(transition.origin_session_id),
                    transition.origin_uuid,
                }
                if transition.result_session_id is not None:
                    valid_contexts.add(str(transition.result_session_id))
                if transition.result_uuid is not None:
                    valid_contexts.add(transition.result_uuid)
                if ctx not in valid_contexts:
                    raise AuthError("AUTH_CONTEXT_CHANGED", "다른 탭에서 계정이 변경되었습니다.", 409)

            if bearer_token:
                try:
                    _, payload = authenticate_access_token(db, bearer_token)
                    token_sid = int(payload["sid"])
                    allowed_sids = {int(transition.origin_session_id)}
                    if transition.result_session_id is not None:
                        allowed_sids.add(int(transition.result_session_id))
                    if token_sid not in allowed_sids:
                        raise AuthError("AUTH_CONTEXT_CHANGED", "다른 탭에서 계정이 변경되었습니다.", 409)
                except AuthError as exc:
                    if exc.code == "AUTH_CONTEXT_CHANGED":
                        raise
                    if transition.status != "COMMITTED":
                        raise

            # 이미 COMMITTED된 작업에 대해 동일 준비 비밀 쿠키로 재요청한 경우 결과 복구 (멱등 보장)
            if transition.status == "COMMITTED":
                if secret_hash != transition.recovery_secret_hash:
                    raise AuthError("INVALID_TRANSITION_SECRET", "전환 준비 비밀값이 일치하지 않습니다.", 401)
                return _build_committed_response_from_transition(db, transition, now=now), prepare_secret

            if transition.status != "PREPARED":
                raise AuthError(
                    "TRANSITION_NOT_PREPARED",
                    f"확정할 수 없는 전환 상태입니다: {transition.status}",
                    409,
                )
            if secret_hash != transition.prepare_secret_hash:
                raise AuthError("INVALID_TRANSITION_SECRET", "전환 준비 비밀값이 일치하지 않습니다.", 401)
            if now >= as_utc_naive(transition.prepare_expires_at):
                transition.status = "EXPIRED"
                transition.updated_at = now
                shred_prepared_kakao_credential(
                    db, transition.transition_id, status="EXPIRED", now=now
                )
                prepare_room_notifications(db)
                db.commit()
                raise AuthError("TRANSITION_PREPARATION_EXPIRED", "전환 준비 유효 시간이 지났습니다.", 401)

            replaced_images: list[str] = []

            if transition.action == "KAKAO_LINK":
                ctx = transition.provider_context_json or {}
                kakao_id = int(ctx["kakao_id"])
                kakao_email = ctx.get("email")

                # 1. 전역 잠금 순서: KakaoLinkGuard(kakao_id) -> User(id ASC) -> AuthSession(id ASC) -> Room/Task
                _acquire_kakao_link_guard(db, kakao_id, now)

                existing_target_row = (
                    db.query(User.id)
                    .filter(User.kakao_id == kakao_id)
                    .with_for_update(read=True)
                    .first()
                )
                user_ids_to_lock = sorted(
                    {
                        int(uid)
                        for uid in (
                            transition.origin_user_id,
                            existing_target_row[0] if existing_target_row else None,
                        )
                        if uid is not None
                    }
                )
                locked_users = (
                    db.execute(
                        select(User)
                        .where(User.id.in_(user_ids_to_lock))
                        .order_by(User.id.asc())
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    .scalars()
                    .all()
                ) if user_ids_to_lock else []
                users_by_id = {int(u.id): u for u in locked_users}

                origin_user = (
                    users_by_id.get(int(transition.origin_user_id))
                    if transition.origin_user_id is not None
                    else None
                )
                if origin_user is None or origin_user.uuid != transition.origin_uuid:
                    db.rollback()
                    raise AuthError("ORIGIN_USER_UNAVAILABLE", "원본 사용자를 확인할 수 없습니다.", 409)
                if origin_user.kakao_id is not None:
                    db.rollback()
                    raise AuthError("ALREADY_LINKED_ACCOUNT", "이미 카카오 계정이 연결된 사용자입니다.", 409)

                target_user = (
                    users_by_id.get(int(existing_target_row[0]))
                    if existing_target_row is not None
                    else None
                )

                # 2. 원본 AuthSession 잠금 및 유효성 재검증
                origin_sessions = (
                    db.execute(
                        select(AuthSession)
                        .where(AuthSession.user_id == origin_user.id)
                        .order_by(AuthSession.id.asc())
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    .scalars()
                    .all()
                )
                origin_session = next(
                    (s for s in origin_sessions if int(s.id) == int(transition.origin_session_id)),
                    None,
                )
                validate_session(origin_session)

                # 3. 원본 및 대상 계정의 방 참여·60초 유예·진행 중 방 작업 재검증
                check_user_ids = [int(origin_user.id)]
                if target_user is not None:
                    check_user_ids.append(int(target_user.id))
                if has_active_or_pending_room_activity(db, check_user_ids, now=now):
                    db.rollback()
                    raise AuthError(
                        "ROOM_PARTICIPATION_ACTIVE",
                        "방 참여 또는 재접속 유예 중에는 계정을 전환할 수 없습니다.",
                        409,
                    )

                _cleanup_closed_room_references_for_origin_user(db, int(origin_user.id), now)

                # 4. 전환 행 잠금 및 상태 재검증
                locked_transition = (
                    db.execute(
                        select(AccountTransition)
                        .where(AccountTransition.id == transition.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    .scalar_one()
                )
                if locked_transition.status == "COMMITTED":
                    return _build_committed_response_from_transition(db, locked_transition, now=now), prepare_secret
                if locked_transition.status != "PREPARED" or locked_transition.prepare_secret_hash != secret_hash:
                    db.rollback()
                    raise AuthError("TRANSITION_CONFLICT", "전환 작업 상태가 변경되었습니다.", 409)

                # 5. 무손실 이관·병합
                origin_id_to_delete = int(origin_user.id)
                target_user, is_new_user, replaced_images = _merge_uuid_user_into_kakao_target(
                    db,
                    origin_user,
                    target_user,
                    kakao_id=kakao_id,
                    kakao_email=kakao_email,
                    now=now,
                )

                # 6. 대상 계정에 대한 새 정식 AuthSession을 먼저 생성해 SQLite에서도 이전 sid가 재사용되지 않게 보장
                new_auth_session = AuthSession(
                    user_id=target_user.id,
                    secret_hash=secret_hash,
                    created_at=now,
                    expires_at=now + timedelta(seconds=SESSION_SECONDS),
                )
                db.add(new_auth_session)
                db.flush()

                prepared_cred = (
                    db.query(KakaoSessionCredential)
                    .filter(KakaoSessionCredential.transition_id == locked_transition.transition_id)
                    .with_for_update()
                    .first()
                )
                if prepared_cred is not None:
                    prepared_cred.auth_session_id = new_auth_session.id
                    prepared_cred.status = "ACTIVE"
                    prepared_cred.updated_at = now
                    db.flush()
                shred_session_prepared_kakao_credentials(
                    db,
                    int(transition.origin_session_id),
                    status="CANCELLED",
                    now=now,
                    exclude_transition_id=locked_transition.transition_id,
                )

                # 7. 원본 사용자의 모든 AuthSession 물리 제거 (RESTRICT FK 해제) 및 원본 User 물리 삭제 (R3A)
                for s in db.query(AuthSession).filter(AuthSession.user_id == origin_id_to_delete).all():
                    db.delete(s)
                db.flush()
                db.delete(origin_user)
                db.flush()

                locked_transition.status = "COMMITTED"
                locked_transition.origin_user_id = None
                locked_transition.result_user_id = target_user.id
                locked_transition.result_uuid = target_user.uuid
                locked_transition.result_session_id = new_auth_session.id
                locked_transition.result_is_new_user = is_new_user
                locked_transition.committed_at = now
                locked_transition.updated_at = now
                db.flush()

                resp = TransitionCommitResponse(
                    transition_id=locked_transition.transition_id,
                    action=locked_transition.action,
                    status="COMMITTED",
                    access_token=create_access_token(
                        {"sub": str(target_user.id), "sid": str(new_auth_session.id)}
                    ),
                    user=UserResponse.model_validate(target_user),
                    is_new_user=is_new_user,
                )
                prepare_room_notifications(db)
                db.commit()

                for img_url in replaced_images:
                    delete_profile_image_if_unreferenced(db, img_url)

                return resp, prepare_secret

            elif transition.action == "LOGOUT":
                # 1. 원본 User 및 현재 브라우저 AuthSession 잠금·검증
                if transition.origin_user_id is None:
                    db.rollback()
                    raise AuthError("ORIGIN_USER_UNAVAILABLE", "원본 사용자를 확인할 수 없습니다.", 409)
                origin_user = (
                    db.execute(
                        select(User)
                        .where(User.id == int(transition.origin_user_id))
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    .scalar_one_or_none()
                )
                if origin_user is None or origin_user.uuid != transition.origin_uuid:
                    db.rollback()
                    raise AuthError("ORIGIN_USER_UNAVAILABLE", "원본 사용자를 확인할 수 없습니다.", 409)

                origin_session = (
                    db.execute(
                        select(AuthSession)
                        .where(AuthSession.id == int(transition.origin_session_id))
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    .scalar_one_or_none()
                )
                validate_session(origin_session)

                # 2. 방 참여·60초 유예 확인
                if has_active_or_pending_room_activity(db, [int(origin_user.id)], now=now):
                    db.rollback()
                    raise AuthError(
                        "ROOM_PARTICIPATION_ACTIVE",
                        "방 참여 또는 재접속 유예 중에는 로그아웃할 수 없습니다.",
                        409,
                    )

                locked_transition = (
                    db.execute(
                        select(AccountTransition)
                        .where(AccountTransition.id == transition.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    .scalar_one()
                )
                if locked_transition.status == "COMMITTED":
                    return _build_committed_response_from_transition(db, locked_transition, now=now), prepare_secret
                if locked_transition.status != "PREPARED" or locked_transition.prepare_secret_hash != secret_hash:
                    db.rollback()
                    raise AuthError("TRANSITION_CONFLICT", "전환 작업 상태가 변경되었습니다.", 409)

                # 3. 현재 브라우저의 인증 세션만 폐기 (카카오 계정 데이터 및 다른 기기 세션은 그대로 유지)
                origin_session.revoked_at = now
                db.flush()
                shred_session_prepared_kakao_credentials(
                    db,
                    int(origin_session.id),
                    status="CANCELLED",
                    now=now,
                )

                origin_cred = (
                    db.query(KakaoSessionCredential)
                    .filter(KakaoSessionCredential.auth_session_id == origin_session.id)
                    .with_for_update()
                    .first()
                )
                if origin_cred is not None and (
                    origin_cred.encrypted_access_token or origin_cred.encrypted_refresh_token
                ):
                    origin_cred.status = "PENDING_REVOCATION"
                    origin_cred.logout_transition_id = locked_transition.transition_id
                    origin_cred.updated_at = now
                    kakao_logout_status = "PENDING"
                else:
                    if origin_cred is not None:
                        origin_cred.logout_transition_id = locked_transition.transition_id
                        shred_kakao_credential_tokens(
                            origin_cred,
                            status="SKIPPED_NO_CREDENTIALS",
                            now=now,
                            terminal_reason="NO_CREDENTIALS",
                        )
                    else:
                        db.add(
                            KakaoSessionCredential(
                                auth_session_id=origin_session.id,
                                logout_transition_id=locked_transition.transition_id,
                                kakao_id=origin_user.kakao_id,
                                encrypted_access_token=None,
                                encrypted_refresh_token=None,
                                status="SKIPPED_NO_CREDENTIALS",
                                attempt_count=0,
                                terminal_reason="NO_CREDENTIALS",
                                finished_at=now,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    kakao_logout_status = "SKIPPED_NO_CREDENTIALS"
                db.flush()

                # 4. 새 UUID 사용자 및 새 정식 AuthSession 발급 (로그아웃 직후 재연결 시 재인증 필요 표시)
                new_user = create_uuid_user(db)
                new_auth_session = AuthSession(
                    user_id=new_user.id,
                    secret_hash=secret_hash,
                    requires_reauth=True,
                    created_at=now,
                    expires_at=now + timedelta(seconds=SESSION_SECONDS),
                )
                db.add(new_auth_session)
                db.flush()

                locked_transition.status = "COMMITTED"
                locked_transition.result_user_id = new_user.id
                locked_transition.result_uuid = new_user.uuid
                locked_transition.result_session_id = new_auth_session.id
                locked_transition.result_is_new_user = True
                locked_transition.committed_at = now
                locked_transition.updated_at = now
                db.flush()

                resp = TransitionCommitResponse(
                    transition_id=locked_transition.transition_id,
                    action=locked_transition.action,
                    status="COMMITTED",
                    access_token=create_access_token(
                        {"sub": str(new_user.id), "sid": str(new_auth_session.id)}
                    ),
                    user=UserResponse.model_validate(new_user),
                    is_new_user=True,
                    requires_reauth=True,
                    kakao_logout_status=kakao_logout_status,
                )
                prepare_room_notifications(db)
                db.commit()
                return resp, prepare_secret

            else:
                db.rollback()
                raise AuthError("INVALID_TRANSITION_ACTION", "지원하지 않는 전환 작업입니다.", 400)

        except OperationalError as exc:
            db.rollback()
            err_code = getattr(exc.orig, "args", [None])[0]
            if err_code in (1205, 1213) and attempt < 2:
                now = utc_now()
                continue
            raise AuthError("AUTH_TEMPORARILY_UNAVAILABLE", "일시적으로 전환을 처리할 수 없습니다.", 503) from None
        except AuthError:
            db.rollback()
            raise
        except SQLAlchemyError:
            db.rollback()
            raise AuthError("AUTH_INTERNAL_ERROR", "계정 전환 처리 중 오류가 발생했습니다.", 500) from None

    raise AuthError("AUTH_TEMPORARILY_UNAVAILABLE", "일시적으로 전환을 처리할 수 없습니다.", 503)


def _authorize_transition_inspection(
    db: Session,
    transition: AccountTransition,
    *,
    prepare_secret: str | None,
    auth_cookie_secret: str | None,
    bearer_token: str | None,
) -> None:
    """
    작업 번호(transition_id)나 UUID만 아는 요청을 차단하고,
    준비 비밀 쿠키·대상 인증 쿠키·원본/대상 유효 JWT 중 하나로 소유권이 증명된 경우만 허용한다.
    """
    for candidate_secret in (prepare_secret, auth_cookie_secret):
        if candidate_secret:
            try:
                h = hash_secret(candidate_secret)
            except AuthError:
                continue
            if h == transition.prepare_secret_hash or h == transition.recovery_secret_hash:
                return

    if bearer_token:
        _, payload = authenticate_access_token(db, bearer_token)
        sid = int(payload["sid"])
        if sid == int(transition.origin_session_id) or (
            transition.result_session_id is not None and sid == int(transition.result_session_id)
        ):
            return

    raise AuthError("TRANSITION_FORBIDDEN", "전환 작업에 접근할 수 있는 인증 증명이 없습니다.", 401)


def get_account_transition_status(
    db: Session,
    transition_id: str,
    *,
    prepare_secret: str | None = None,
    auth_cookie_secret: str | None = None,
    bearer_token: str | None = None,
) -> TransitionStatusResponse:
    transition = (
        db.query(AccountTransition)
        .filter(AccountTransition.transition_id == transition_id)
        .first()
    )
    if transition is None:
        raise AuthError("TRANSITION_NOT_FOUND", "계정 전환 작업을 찾을 수 없습니다.", 404)

    _authorize_transition_inspection(
        db,
        transition,
        prepare_secret=prepare_secret,
        auth_cookie_secret=auth_cookie_secret,
        bearer_token=bearer_token,
    )

    now = utc_now()
    effective_status = transition.status
    if effective_status == "PREPARED" and now >= as_utc_naive(transition.prepare_expires_at):
        effective_status = "EXPIRED"

    expires = (
        transition.recovery_expires_at
        if effective_status == "COMMITTED"
        else transition.prepare_expires_at
    )
    return TransitionStatusResponse(
        transition_id=transition.transition_id,
        action=transition.action,
        status=effective_status,
        origin_uuid=transition.origin_uuid,
        result_uuid=transition.result_uuid,
        expires_at=_as_utc_aware(expires),
        target_account=(
            _extract_prepared_target_account(transition.provider_context_json)
            if transition.action == "KAKAO_LINK"
            else None
        ),
        kakao_logout_status=_resolve_logout_kakao_status(db, transition),
    )


def cancel_account_transition(
    db: Session,
    transition_id: str,
    *,
    prepare_secret: str | None = None,
    auth_cookie_secret: str | None = None,
    bearer_token: str | None = None,
    expected_context: str | None = None,
) -> TransitionStatusResponse:
    """
    연결 대상 확인 화면에서 사용자가 취소할 때 PREPARED 전환을 CANCELLED로 종료하고
    임시 보관된 카카오 토큰 원문을 즉시 파기한다.
    """
    transition = (
        db.query(AccountTransition)
        .filter(AccountTransition.transition_id == transition_id)
        .first()
    )
    if transition is None:
        raise AuthError("TRANSITION_NOT_FOUND", "계정 전환 작업을 찾을 수 없습니다.", 404)

    if expected_context is not None and expected_context.strip():
        ctx = expected_context.strip()
        if ctx not in {str(transition.origin_session_id), transition.origin_uuid}:
            raise AuthError("AUTH_CONTEXT_CHANGED", "다른 탭에서 계정이 변경되었습니다.", 409)

    _authorize_transition_inspection(
        db,
        transition,
        prepare_secret=prepare_secret,
        auth_cookie_secret=auth_cookie_secret,
        bearer_token=bearer_token,
    )

    now = utc_now()
    with database_errors(db):
        locked = (
            db.execute(
                select(AccountTransition)
                .where(AccountTransition.id == transition.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            .scalar_one()
        )
        if locked.status == "COMMITTED":
            db.rollback()
            raise AuthError(
                "TRANSITION_ALREADY_FINALIZED",
                "이미 확정된 전환 작업은 취소할 수 없습니다.",
                409,
            )
        if locked.status != "CANCELLED":
            locked.status = "CANCELLED"
            locked.updated_at = now
        shred_prepared_kakao_credential(
            db,
            locked.transition_id,
            status="CANCELLED",
            now=now,
        )
        prepare_room_notifications(db)
        db.commit()
        return TransitionStatusResponse(
            transition_id=locked.transition_id,
            action=locked.action,
            status="CANCELLED",
            origin_uuid=locked.origin_uuid,
            result_uuid=locked.result_uuid,
            expires_at=_as_utc_aware(locked.prepare_expires_at),
            target_account=(
                _extract_prepared_target_account(locked.provider_context_json)
                if locked.action == "KAKAO_LINK"
                else None
            ),
            kakao_logout_status=None,
        )


def recover_account_transition(
    db: Session,
    transition_id: str,
    *,
    prepare_secret: str | None = None,
    auth_cookie_secret: str | None = None,
) -> tuple[TransitionCommitResponse, str]:
    """
    COMMITTED 전환 응답이 네트워크에서 유실된 경우(원본 User/AuthSession 물리 삭제 후 포함),
    브라우저가 보유한 준비/복구 비밀 쿠키로 대상 인증(JWT + 정식 쿠키 + User DTO)을 복원한다.
    작업 ID나 UUID만으로는 절대 복구할 수 없으며, 재이관이나 새 UUID 추가 생성을 수행하지 않는다.
    """
    transition = (
        db.query(AccountTransition)
        .filter(AccountTransition.transition_id == transition_id)
        .first()
    )
    if transition is None:
        raise AuthError("TRANSITION_NOT_FOUND", "계정 전환 작업을 찾을 수 없습니다.", 404)

    matched_secret: str | None = None
    for candidate in (prepare_secret, auth_cookie_secret):
        if candidate:
            try:
                h = hash_secret(candidate)
            except AuthError:
                continue
            if h == transition.recovery_secret_hash:
                matched_secret = candidate
                break

    if matched_secret is None:
        raise AuthError("TRANSITION_SECRET_REQUIRED", "전환 결과 복구에는 유효한 준비 비밀 쿠키가 필요합니다.", 401)

    if transition.status != "COMMITTED":
        raise AuthError(
            "TRANSITION_NOT_COMMITTED",
            f"복구 가능한 완료 상태가 아닙니다: {transition.status}",
            409,
        )

    now = utc_now()
    with database_errors(db):
        resp = _build_committed_response_from_transition(db, transition, now=now)
        return resp, matched_secret

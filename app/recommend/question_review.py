"""Review score questions after repeated rejection of personal first lists."""

from collections import defaultdict
from datetime import datetime, timedelta
import logging

from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.sessions import as_utc_naive, utc_now
from app.core.config import settings
from app.menus.models import OrderSheetQuestionReview, Recommendation, RecommendationFeedback
from app.recommend.replay import initial_personal_list_filter, verify_and_load_recent_replay_snapshots

logger = logging.getLogger(__name__)

REVIEW_WINDOW_DAYS = 30
REVIEW_MAX_SHEETS = 20
REVIEW_MIN_REJECTIONS = 2
REVIEW_MIN_REJECTION_PERCENT = 30
REVIEW_USERS_PER_BATCH = 5


class QuestionImpact(BaseModel):
    question_id: int
    first_place_changed: bool
    entered_menu_ids: tuple[int, ...]
    left_menu_ids: tuple[int, ...]
    liked_original_only_menu_ids: tuple[int, ...]
    neutral_answer: bool


class SheetQuestionImpacts(BaseModel):
    recommendation_id: int
    questions: tuple[QuestionImpact, ...]


class QuestionReviewScore(BaseModel):
    question_id: int
    changed_sheet_count: int
    liked_sheet_count: int
    neutral_sheet_count: int


class QuestionReview(BaseModel):
    reviewed_at: datetime
    rejected_count: int
    shown_count: int
    eligible: bool
    ranked_questions: tuple[QuestionReviewScore, ...] = ()
    sheet_impacts: tuple[SheetQuestionImpacts, ...] = ()


def enqueue_question_review(db: Session, user_id: int, now: datetime) -> OrderSheetQuestionReview:
    query = (
        db.query(OrderSheetQuestionReview)
        .filter(OrderSheetQuestionReview.user_id == user_id)
        .with_for_update()
        .populate_existing()
    )
    work = query.one_or_none()
    if work is None:
        try:
            with db.begin_nested():
                work = OrderSheetQuestionReview(user_id=user_id, pending_at=now)
                db.add(work)
                db.flush()
            return work
        except IntegrityError:
            work = query.one()

    if work.pending_at is None:
        work.pending_at = now
    work.revision += 1
    return work


def merge_question_review_work(db: Session, origin_id: int, target_id: int, now: datetime) -> None:
    """Recompute review against the combined recommendation history after account linking."""
    origin = db.get(OrderSheetQuestionReview, origin_id)
    if origin is None and db.get(OrderSheetQuestionReview, target_id) is None:
        return
    target = enqueue_question_review(db, target_id, now)
    target.reviewed_at = None
    target.result_json = None
    if origin is not None:
        db.delete(origin)


def _review_user(db: Session, user_id: int, now: datetime) -> QuestionReview:
    rejected, shown = (
        db.query(
            func.count(case((Recommendation.all_menus_disliked_at <= now, Recommendation.id))),
            func.count(Recommendation.id),
        )
        .filter(
            Recommendation.user_id == user_id,
            Recommendation.created_at.between(now - timedelta(days=REVIEW_WINDOW_DAYS), now),
            Recommendation.recommended_menu_ids[0].as_integer().is_not(None),
            initial_personal_list_filter(),
        )
        .one()
    )
    eligible = rejected >= REVIEW_MIN_REJECTIONS and rejected * 100 >= shown * REVIEW_MIN_REJECTION_PERCENT
    review = QuestionReview(reviewed_at=now, rejected_count=rejected, shown_count=shown, eligible=eligible)
    if not eligible:
        return review

    replays = verify_and_load_recent_replay_snapshots(
        db,
        user_id,
        now=now,
        window_days=min(REVIEW_WINDOW_DAYS, settings.ORDER_SHEET_REPLAY_RETENTION_DAYS),
        max_sheets=min(REVIEW_MAX_SHEETS, settings.ORDER_SHEET_REPLAY_MAX_SHEETS),
        initial_personal_only=True,
    )
    originals = {
        item.baseline_replay.recommendation_id: item.baseline_replay.replayed_menu_ids
        for item in replays.verified_sheets
    }
    liked_menus: dict[int, set[int]] = defaultdict(set)
    if originals:
        like_events = (
            db.query(
                RecommendationFeedback.recommendation_id,
                RecommendationFeedback.selected_menu_id,
                RecommendationFeedback.exposure_rank,
            )
            .filter(
                RecommendationFeedback.recommendation_id.in_(originals),
                RecommendationFeedback.is_satisfied.is_(True),
                RecommendationFeedback.created_at <= now,
            )
            .distinct()
        )
        for rec_id, menu_id, rank in like_events:
            original = originals[rec_id]
            # The feedback foreign key is nulled on deletion. Exposure rank preserves the menu in the first list.
            if menu_id is None and rank is not None and 1 <= rank <= len(original):
                menu_id = original[rank - 1]
            if menu_id in original:
                liked_menus[rec_id].add(menu_id)

    sheet_impacts = []
    impacts_by_question: dict[int, list[QuestionImpact]] = defaultdict(list)
    for item in replays.verified_sheets:
        rec_id = item.baseline_replay.recommendation_id
        original = originals[rec_id]
        neutral_questions = {
            str(answer.get("question_id"))
            for answer in (item.snapshot.answers_json or [])
            if isinstance(answer, dict) and answer.get("option_id") == "any"
        }
        impacts = []
        for question_id, replay in item.question_replays.items():
            without_question = replay.replayed_menu_ids
            left = tuple(menu_id for menu_id in original if menu_id not in without_question)
            impact = QuestionImpact(
                question_id=question_id,
                first_place_changed=original[:1] != without_question[:1],
                entered_menu_ids=tuple(menu_id for menu_id in without_question if menu_id not in original),
                left_menu_ids=left,
                liked_original_only_menu_ids=tuple(menu_id for menu_id in left if menu_id in liked_menus[rec_id]),
                neutral_answer=str(question_id) in neutral_questions,
            )
            impacts.append(impact)
            impacts_by_question[question_id].append(impact)
        sheet_impacts.append(SheetQuestionImpacts(recommendation_id=rec_id, questions=tuple(impacts)))

    scores = [
        QuestionReviewScore(
            question_id=question_id,
            changed_sheet_count=sum(
                impact.first_place_changed or bool(impact.entered_menu_ids or impact.left_menu_ids)
                for impact in impacts
            ),
            liked_sheet_count=sum(bool(impact.liked_original_only_menu_ids) for impact in impacts),
            neutral_sheet_count=sum(impact.neutral_answer for impact in impacts),
        )
        for question_id, impacts in impacts_by_question.items()
    ]
    scores.sort(key=lambda score: (
        score.changed_sheet_count,
        score.liked_sheet_count,
        -score.neutral_sheet_count,
        score.question_id,
    ))
    return review.model_copy(update={"ranked_questions": tuple(scores), "sheet_impacts": tuple(sheet_impacts)})


def run_pending_question_reviews(
    db: Session, *, limit: int = REVIEW_USERS_PER_BATCH, now: datetime | None = None
) -> int:
    """Process a bounded batch on a worker session, publishing only unchanged work revisions."""
    if limit <= 0:
        return 0
    now = as_utc_naive(now) if now is not None else utc_now()
    pending = (
        db.query(OrderSheetQuestionReview.user_id, OrderSheetQuestionReview.revision)
        .filter(OrderSheetQuestionReview.pending_at <= now)
        .order_by(OrderSheetQuestionReview.pending_at, OrderSheetQuestionReview.user_id)
        .limit(limit)
        .all()
    )
    db.rollback()  # Read each user's history in a fresh transaction.
    published = 0
    for user_id, revision in pending:
        try:
            result = _review_user(db, user_id, now)
            # A concurrent rejection bumps the revision and leaves the row pending for the next run.
            updated = (
                db.query(OrderSheetQuestionReview)
                .filter(
                    OrderSheetQuestionReview.user_id == user_id,
                    OrderSheetQuestionReview.revision == revision,
                    OrderSheetQuestionReview.pending_at.is_not(None),
                )
                .update({
                    OrderSheetQuestionReview.pending_at: None,
                    OrderSheetQuestionReview.reviewed_at: now,
                    OrderSheetQuestionReview.result_json: result.model_dump(mode="json"),
                }, synchronize_session=False)
            )
            db.commit()
            published += updated
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Order-sheet question review failed for user_id=%s", user_id)
    return published


def _published_review(db: Session, user_id: int, now: datetime | None):
    now = as_utc_naive(now) if now is not None else utc_now()
    return db.query(OrderSheetQuestionReview).filter(
        OrderSheetQuestionReview.user_id == user_id,
        OrderSheetQuestionReview.reviewed_at > now - timedelta(days=REVIEW_WINDOW_DAYS),
        OrderSheetQuestionReview.reviewed_at <= now,
    )


def load_question_review(db: Session, user_id: int, *, now: datetime | None = None) -> QuestionReview | None:
    """Read the latest published review if it is within the review window."""
    payload = _published_review(db, user_id, now).with_entities(OrderSheetQuestionReview.result_json).scalar()
    return QuestionReview.model_validate(payload) if payload is not None else None


def load_ranked_question_ids(db: Session, user_id: int, *, now: datetime | None = None) -> tuple[int, ...]:
    """Return score question IDs in the published replacement review order."""
    scores = (
        _published_review(db, user_id, now)
        .with_entities(OrderSheetQuestionReview.result_json["ranked_questions"])
        .scalar()
    )
    return tuple(score["question_id"] for score in scores or ())

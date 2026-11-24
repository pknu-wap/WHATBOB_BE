from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.sessions import create_uuid_user, utc_now
from app.auth.transitions import _merge_uuid_user_into_kakao_target
from app.core.database import Base
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
)
from app.recommend.common import RecommendationDomainException, RecommendationNotFoundException
from app.recommend.feedback import (
    OUTCOME_FIRST_LIST_LIKED,
    OUTCOME_INDIVIDUAL_DISLIKED_ONLY,
    OUTCOME_LIST_REJECTED,
    OUTCOME_NO_RESPONSE,
    OUTCOME_ZERO_CANDIDATES,
    classify_recommendation_outcome,
    record_recommendation_feedback,
    reject_recommendation_list,
    summarize_first_list_reactions,
)
from app.recommend.order_sheet import create_order_sheet_recommendation
from app.recommend.schemas import (
    AnswerItemRequest,
    OrderSheetRecommendRequest,
    RecommendationFeedbackRequest,
)
from app.users.models import User


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _add_menu(db, name: str, *, food_code: str, has_soup: bool = False) -> Menu:
    menu = Menu(
        food_code=food_code,
        name=name,
        base_name=name,
        category_large="한식",
        has_soup=has_soup,
        cooking_style="rice",
        main_ingredient="pork",
        flavor_intensity="mild_clean",
        temperature="hot",
        heaviness="moderate",
        allergen_tags=[],
    )
    db.add(menu)
    db.flush()
    return menu


def _add_question(db, code: str = "q_soup_01", cluster: str = "has_soup") -> OrderSheetQuestion:
    q = OrderSheetQuestion(
        question_code=code,
        cluster_key=cluster,
        question_text="국물 있는 음식이 당기나요?",
        options_json=[
            {"option_id": "soup_yes", "label": "국물", "rules": [{"field": "has_soup", "op": "eq", "value": True}]},
            {"option_id": "soup_no", "label": "국물 없음", "rules": [{"field": "has_soup", "op": "neq", "value": True}]},
            {"option_id": "any", "label": "상관없어요", "rules": []},
        ],
        initial_score=0.75,
        importance_weight=0.75,
    )
    db.add(q)
    db.flush()
    return q


def test_menu_feedback_persists_exposure_rank_without_mutating_question_weights_and_counts_multi_like_as_one_list_success(db):
    user = User(nickname="리액션테스터")
    db.add(user)
    db.flush()

    m1 = _add_menu(db, "김치찌개", food_code="M001", has_soup=True)
    m2 = _add_menu(db, "된장찌개", food_code="M002", has_soup=True)
    m3 = _add_menu(db, "우동", food_code="M003", has_soup=True)
    q_soup = _add_question(db)
    db.commit()

    rec_resp = create_order_sheet_recommendation(
        db=db,
        request=OrderSheetRecommendRequest(
            answers=[AnswerItemRequest(question_id=q_soup.id, option_id="soup_yes")],
            top_k=3,
        ),
        user_id=user.id,
    )
    rec_id = rec_resp.recommendation_id
    shown_ids = [m.menu_id for m in rec_resp.recommended_menus]
    assert len(shown_ids) == 3

    first_menu_id = shown_ids[0]
    third_menu_id = shown_ids[2]

    fb1 = record_recommendation_feedback(
        db=db,
        recommendation_id=rec_id,
        request=RecommendationFeedbackRequest(selected_menu_id=first_menu_id, is_satisfied=True),
        user_id=user.id,
    )
    fb3 = record_recommendation_feedback(
        db=db,
        recommendation_id=rec_id,
        request=RecommendationFeedbackRequest(selected_menu_id=third_menu_id, is_satisfied=True),
        user_id=user.id,
    )

    # 단일 메뉴 좋아요를 주문서 질문 전체의 가중치/긍정 카운트로 전파하지 않는다.
    assert fb1.updated_weights == {}
    assert fb3.updated_weights == {}
    assert fb1.exposure_rank == 1
    assert fb3.exposure_rank == 3
    db.refresh(q_soup)
    assert q_soup.positive_count == 0
    assert float(q_soup.importance_weight) == pytest.approx(0.75)

    # 최초 노출 위치가 피드백 이력과 유효 상태 모두에 저장된다.
    fb_row_1 = db.get(RecommendationFeedback, fb1.feedback_id)
    fb_row_3 = db.get(RecommendationFeedback, fb3.feedback_id)
    assert fb_row_1.exposure_rank == 1
    assert fb_row_3.exposure_rank == 3

    states = (
        db.query(RecommendationFeedbackState)
        .filter(RecommendationFeedbackState.recommendation_id == rec_id)
        .order_by(RecommendationFeedbackState.exposure_rank.asc())
        .all()
    )
    assert [(s.selected_menu_id, s.exposure_rank, s.is_satisfied) for s in states] == [
        (first_menu_id, 1, True),
        (third_menu_id, 3, True),
    ]

    # 두 메뉴에 좋아요를 눌러도 메뉴 반응은 2건, 첫 목록 성공은 1건이다.
    outcome = classify_recommendation_outcome(db, rec_id)
    assert outcome.outcome == OUTCOME_FIRST_LIST_LIKED
    assert outcome.is_first_list_liked is True
    assert outcome.menu_reaction_count == 2
    assert outcome.liked_menu_count == 2
    assert outcome.disliked_menu_count == 0
    assert outcome.first_liked_exposure_rank == 1


def test_feedback_retry_deduplicates_with_or_without_idempotency_key_and_allows_reaction_change(db):
    user = User(nickname="재전송테스터")
    db.add(user)
    db.flush()

    m1 = _add_menu(db, "돈가스", food_code="R001")
    m2 = _add_menu(db, "우동", food_code="R002", has_soup=True)
    rec = Recommendation(
        user_id=user.id,
        recommended_menu_ids=[m1.id, m2.id],
        initial_candidate_count=2,
    )
    db.add(rec)
    db.commit()

    # 1) 동일 Idempotency-Key 재전송 -> 동일 feedback_id, 중복 행 없음
    r1 = record_recommendation_feedback(
        db=db,
        recommendation_id=rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m1.id, is_satisfied=True),
        user_id=user.id,
        idempotency_key="retry-key-1",
    )
    r2 = record_recommendation_feedback(
        db=db,
        recommendation_id=rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m1.id, is_satisfied=True),
        user_id=user.id,
        idempotency_key="retry-key-1",
    )
    assert r1.feedback_id == r2.feedback_id
    assert db.query(RecommendationFeedback).filter_by(recommendation_id=rec.id).count() == 1
    assert db.query(RecommendationFeedbackState).filter_by(recommendation_id=rec.id).count() == 1

    # 2) 동일 Idempotency-Key에 다른 payload -> 409
    with pytest.raises(RecommendationDomainException) as conflict_exc:
        record_recommendation_feedback(
            db=db,
            recommendation_id=rec.id,
            request=RecommendationFeedbackRequest(selected_menu_id=m1.id, is_satisfied=False),
            user_id=user.id,
            idempotency_key="retry-key-1",
        )
    assert conflict_exc.value.status_code == 409

    # 3) Idempotency-Key 없이 또는 새 키로 동일 클릭 재전송 -> 중복 집계 없음 (단일 반응 유지)
    r3 = record_recommendation_feedback(
        db=db,
        recommendation_id=rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m1.id, is_satisfied=True),
        user_id=user.id,
    )
    r4 = record_recommendation_feedback(
        db=db,
        recommendation_id=rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m1.id, is_satisfied=True),
        user_id=user.id,
        idempotency_key="retry-key-2",
    )
    assert r3.feedback_id == r1.feedback_id
    assert r4.feedback_id == r1.feedback_id
    assert db.query(RecommendationFeedback).filter_by(recommendation_id=rec.id).count() == 1
    assert db.query(RecommendationFeedbackState).filter_by(recommendation_id=rec.id).count() == 1

    # 4) 좋아요 -> 싫어요로 실제 변경하면 상태가 갱신되고 결과 분류도 바뀐다.
    r5 = record_recommendation_feedback(
        db=db,
        recommendation_id=rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m1.id, is_satisfied=False),
        user_id=user.id,
        idempotency_key="change-key-3",
    )
    assert r5.feedback_id != r1.feedback_id
    assert r5.is_satisfied is False
    assert db.query(RecommendationFeedback).filter_by(recommendation_id=rec.id).count() == 2
    assert db.query(RecommendationFeedbackState).filter_by(recommendation_id=rec.id).count() == 1

    outcome = classify_recommendation_outcome(db, rec.id)
    assert outcome.outcome == OUTCOME_INDIVIDUAL_DISLIKED_ONLY
    assert outcome.menu_reaction_count == 1
    assert outcome.liked_menu_count == 0
    assert outcome.disliked_menu_count == 1

    # 5) user_id=None 인 추천에서도 동일 클릭 재전송이 중복 기록되지 않는다.
    anon_rec = Recommendation(
        user_id=None,
        recommended_menu_ids=[m1.id, m2.id],
        initial_candidate_count=2,
    )
    db.add(anon_rec)
    db.commit()

    a1 = record_recommendation_feedback(
        db=db,
        recommendation_id=anon_rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m2.id, is_satisfied=True),
    )
    a2 = record_recommendation_feedback(
        db=db,
        recommendation_id=anon_rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m2.id, is_satisfied=True),
    )
    assert a1.feedback_id == a2.feedback_id
    assert a1.exposure_rank == 2
    assert db.query(RecommendationFeedback).filter_by(recommendation_id=anon_rec.id).count() == 1


def test_explicit_list_rejection_closes_sheet_is_idempotent_and_rejects_zero_candidates(db):
    owner = User(nickname="거절주인")
    other = User(nickname="다른사용자")
    db.add_all([owner, other])
    db.flush()

    m1 = _add_menu(db, "비빔밥", food_code="J001")
    m2 = _add_menu(db, "김치볶음밥", food_code="J002")
    rec = Recommendation(
        user_id=owner.id,
        recommended_menu_ids=[m1.id, m2.id],
        initial_candidate_count=2,
    )
    zero_rec = Recommendation(
        user_id=owner.id,
        recommended_menu_ids=[],
        initial_candidate_count=0,
    )
    db.add_all([rec, zero_rec])
    db.commit()

    # 타인 거절 시 404
    with pytest.raises(RecommendationNotFoundException):
        reject_recommendation_list(db=db, recommendation_id=rec.id, user_id=other.id)

    # 개별 싫어요를 먼저 남긴 뒤 목록 거절해도 개별 싫어요 기록은 유지되고 주문서는 종료 + 명시적 거절 1건
    prior_fb = record_recommendation_feedback(
        db=db,
        recommendation_id=rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m1.id, is_satisfied=False),
        user_id=owner.id,
        idempotency_key="prior-dislike-key",
    )

    rej1 = reject_recommendation_list(
        db=db,
        recommendation_id=rec.id,
        user_id=owner.id,
        idempotency_key="rej-key-1",
    )
    assert rej1.recommendation_id == rec.id
    assert rej1.is_list_rejected is True
    assert rej1.is_closed is True
    assert rej1.outcome == OUTCOME_LIST_REJECTED
    assert rej1.list_rejected_at is not None
    assert rej1.closed_at is not None

    # 동일 키 / 새 키 / 키 없음 재전송 모두 멱등하게 동일 시각 반환
    rej2 = reject_recommendation_list(
        db=db,
        recommendation_id=rec.id,
        user_id=owner.id,
        idempotency_key="rej-key-1",
    )
    rej3 = reject_recommendation_list(
        db=db,
        recommendation_id=rec.id,
        user_id=owner.id,
        idempotency_key="rej-key-2",
    )
    rej4 = reject_recommendation_list(
        db=db,
        recommendation_id=rec.id,
        user_id=owner.id,
    )
    assert rej2.list_rejected_at == rej1.list_rejected_at
    assert rej3.list_rejected_at == rej1.list_rejected_at
    assert rej4.list_rejected_at == rej1.list_rejected_at
    assert rej4.closed_at == rej1.closed_at

    # 종료된 주문서에 새 피드백은 409 거절, 단 종료 전 동일 Idempotency-Key 재생은 200 응답 유지
    with pytest.raises(RecommendationDomainException) as closed_exc:
        record_recommendation_feedback(
            db=db,
            recommendation_id=rec.id,
            request=RecommendationFeedbackRequest(selected_menu_id=m2.id, is_satisfied=True),
            user_id=owner.id,
        )
    assert closed_exc.value.status_code == 409

    replayed_prior = record_recommendation_feedback(
        db=db,
        recommendation_id=rec.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m1.id, is_satisfied=False),
        user_id=owner.id,
        idempotency_key="prior-dislike-key",
    )
    assert replayed_prior.feedback_id == prior_fb.feedback_id

    outcome = classify_recommendation_outcome(db, rec.id)
    assert outcome.outcome == OUTCOME_LIST_REJECTED
    assert outcome.is_list_rejected is True
    assert outcome.is_closed is True
    assert outcome.disliked_menu_count == 1

    # 최초 후보 0건 주문서에 대한 목록 거절은 409로 거절되어 zero_candidates와 혼동되지 않는다.
    with pytest.raises(RecommendationDomainException) as zero_exc:
        reject_recommendation_list(db=db, recommendation_id=zero_rec.id, user_id=owner.id)
    assert zero_exc.value.status_code == 409
    db.refresh(zero_rec)
    assert zero_rec.list_rejected_at is None
    assert zero_rec.closed_at is None
    assert classify_recommendation_outcome(db, zero_rec.id).outcome == OUTCOME_ZERO_CANDIDATES


def test_summarize_first_list_reactions_distinguishes_all_five_outcomes_and_reaction_window(db):
    user = User(nickname="요약테스터")
    other = User(nickname="타유저")
    db.add_all([user, other])
    db.flush()

    menus = [
        _add_menu(db, "우동", food_code="S001", has_soup=True),
        _add_menu(db, "돈가스", food_code="S002"),
        _add_menu(db, "비빔밥", food_code="S003"),
        _add_menu(db, "김치찌개", food_code="S004", has_soup=True),
    ]
    menu_ids = [m.id for m in menus]
    base_time = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    rec_zero = Recommendation(
        user_id=user.id,
        recommended_menu_ids=[],
        initial_candidate_count=0,
        created_at=base_time,
    )
    rec_liked = Recommendation(
        user_id=user.id,
        recommended_menu_ids=menu_ids,
        initial_candidate_count=4,
        created_at=base_time,
    )
    rec_rejected = Recommendation(
        user_id=user.id,
        recommended_menu_ids=menu_ids[:3],
        initial_candidate_count=3,
        created_at=base_time,
    )
    rec_disliked = Recommendation(
        user_id=user.id,
        recommended_menu_ids=menu_ids[:2],
        initial_candidate_count=2,
        created_at=base_time,
    )
    rec_no_resp = Recommendation(
        user_id=user.id,
        recommended_menu_ids=menu_ids[:3],
        initial_candidate_count=3,
        created_at=base_time,
    )
    other_rec = Recommendation(
        user_id=other.id,
        recommended_menu_ids=menu_ids[:2],
        initial_candidate_count=2,
        created_at=base_time,
    )
    db.add_all([rec_zero, rec_liked, rec_rejected, rec_disliked, rec_no_resp, other_rec])
    db.commit()

    # rec_liked: 2위, 4위 두 메뉴에 좋아요 -> 메뉴 반응 2건, 첫 목록 성공 1건
    record_recommendation_feedback(
        db=db,
        recommendation_id=rec_liked.id,
        request=RecommendationFeedbackRequest(selected_menu_id=menu_ids[1], is_satisfied=True),
        user_id=user.id,
    )
    record_recommendation_feedback(
        db=db,
        recommendation_id=rec_liked.id,
        request=RecommendationFeedbackRequest(selected_menu_id=menu_ids[3], is_satisfied=True),
        user_id=user.id,
    )

    # rec_rejected: 개별 싫어요 1건 후 명시적 목록 거절
    record_recommendation_feedback(
        db=db,
        recommendation_id=rec_rejected.id,
        request=RecommendationFeedbackRequest(selected_menu_id=menu_ids[0], is_satisfied=False),
        user_id=user.id,
    )
    reject_recommendation_list(db=db, recommendation_id=rec_rejected.id, user_id=user.id)

    # rec_disliked: 개별 싫어요 1건만 있고 목록 거절은 없음
    record_recommendation_feedback(
        db=db,
        recommendation_id=rec_disliked.id,
        request=RecommendationFeedbackRequest(selected_menu_id=menu_ids[0], is_satisfied=False),
        user_id=user.id,
    )

    summary = summarize_first_list_reactions(db=db, user_id=user.id)
    assert summary.total_recommendations == 5
    assert summary.shown_list_count == 4
    assert summary.zero_candidates_count == 1
    assert summary.first_list_liked_count == 1
    assert summary.list_rejected_count == 1
    assert summary.individual_disliked_only_count == 1
    assert summary.no_response_count == 1
    assert summary.explicit_reaction_count == 2
    assert summary.total_menu_reaction_count == 4
    assert summary.total_liked_menu_count == 2
    assert summary.total_disliked_menu_count == 2

    outcome_by_id = {o.recommendation_id: o for o in summary.outcomes}
    assert outcome_by_id[rec_zero.id].outcome == OUTCOME_ZERO_CANDIDATES
    assert outcome_by_id[rec_liked.id].outcome == OUTCOME_FIRST_LIST_LIKED
    assert outcome_by_id[rec_liked.id].first_liked_exposure_rank == 2
    assert outcome_by_id[rec_rejected.id].outcome == OUTCOME_LIST_REJECTED
    assert outcome_by_id[rec_disliked.id].outcome == OUTCOME_INDIVIDUAL_DISLIKED_ONLY
    assert outcome_by_id[rec_no_resp.id].outcome == OUTCOME_NO_RESPONSE

    # 24시간 집계 창 테스트: 제출 후 25시간 뒤에 들어온 좋아요는 24시간 창에서 무응답으로 분류된다.
    rec_late = Recommendation(
        user_id=user.id,
        recommended_menu_ids=menu_ids[:2],
        initial_candidate_count=2,
        created_at=base_time,
    )
    db.add(rec_late)
    db.commit()
    fb_late = record_recommendation_feedback(
        db=db,
        recommendation_id=rec_late.id,
        request=RecommendationFeedbackRequest(selected_menu_id=menu_ids[0], is_satisfied=True),
        user_id=user.id,
    )
    late_time = base_time + timedelta(hours=25)
    fb_row = db.get(RecommendationFeedback, fb_late.feedback_id)
    fb_row.created_at = late_time
    st_row = (
        db.query(RecommendationFeedbackState)
        .filter(RecommendationFeedbackState.recommendation_id == rec_late.id)
        .one()
    )
    st_row.created_at = late_time
    st_row.updated_at = late_time
    db.commit()

    within_24h = summarize_first_list_reactions(
        db=db,
        recommendation_ids=[rec_late.id],
        reaction_window_hours=24,
    )
    assert within_24h.first_list_liked_count == 0
    assert within_24h.no_response_count == 1
    assert within_24h.outcomes[0].outcome == OUTCOME_NO_RESPONSE


def test_uuid_to_kakao_transition_preserves_exposure_rank_food_kind_and_list_rejection(db):
    origin_user = create_uuid_user(db)
    db.flush()

    m1 = _add_menu(db, "돈가스", food_code="T001")
    m2 = _add_menu(db, "우동", food_code="T002", has_soup=True)
    rec_liked = Recommendation(
        user_id=origin_user.id,
        recommended_menu_ids=[m1.id, m2.id],
        initial_candidate_count=2,
    )
    rec_rejected = Recommendation(
        user_id=origin_user.id,
        recommended_menu_ids=[m1.id, m2.id],
        initial_candidate_count=2,
    )
    db.add_all([rec_liked, rec_rejected])
    db.commit()

    record_recommendation_feedback(
        db=db,
        recommendation_id=rec_liked.id,
        request=RecommendationFeedbackRequest(selected_menu_id=m2.id, is_satisfied=True),
        user_id=origin_user.id,
    )
    reject_recommendation_list(
        db=db,
        recommendation_id=rec_rejected.id,
        user_id=origin_user.id,
    )

    target_user, _, _ = _merge_uuid_user_into_kakao_target(
        db=db,
        origin_user=origin_user,
        target_user=None,
        kakao_id=888777666,
        kakao_email="merged@example.com",
        now=utc_now(),
    )
    db.commit()

    migrated_state = (
        db.query(RecommendationFeedbackState)
        .filter(RecommendationFeedbackState.recommendation_id == rec_liked.id)
        .one()
    )
    assert migrated_state.user_id == target_user.id
    assert migrated_state.exposure_rank == 2
    assert migrated_state.food_kind_id == m2.food_kind_id

    summary = summarize_first_list_reactions(db=db, user_id=target_user.id)
    assert summary.first_list_liked_count == 1
    assert summary.list_rejected_count == 1
    liked_outcome = next(o for o in summary.outcomes if o.recommendation_id == rec_liked.id)
    assert liked_outcome.first_liked_exposure_rank == 2

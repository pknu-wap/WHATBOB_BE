"""Spec #77 / Ticket #87: 하드필터로 후보가 줄어든 과정 추적 및 점검 단위·서비스 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.sessions import create_uuid_user, utc_now
from app.auth.transitions import _merge_uuid_user_into_kakao_target
from app.core.config import settings
from app.core.database import Base
from app.menus.models import (
    Menu,
    OrderSheetHardFilterTrace,
    OrderSheetQuestion,
    Recommendation,
)
from app.recommend.common import RecommendationNotFoundException
from app.recommend.feedback import (
    OUTCOME_LIST_REJECTED,
    OUTCOME_NO_RESPONSE,
    record_recommendation_feedback,
    reject_recommendation_list,
)
from app.recommend.hard_filter_audit import (
    get_order_sheet_hard_filter_audit,
    summarize_overall_satisfaction_with_hard_filter_audit,
    summarize_single_candidate_rejections,
)
from app.recommend.order_sheet import create_order_sheet_recommendation
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.ranking import resolve_question_role
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
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed_initial_questions(db: Session) -> dict[str, OrderSheetQuestion]:
    pool = build_candidate_question_pool()
    by_cluster: dict[str, OrderSheetQuestion] = {}
    for cluster in settings.ORDER_SHEET_INITIAL_CLUSTERS:
        raw = next(q for q in pool if q["cluster_key"] == cluster)
        q = OrderSheetQuestion(
            question_code=raw["question_code"],
            cluster_key=cluster,
            question_text=raw["question_text"],
            options_json=raw["options"],
            initial_score=0.75,
            importance_weight=0.75,
            is_active=True,
        )
        db.add(q)
        by_cluster[cluster] = q
    db.flush()
    return by_cluster


def _add_menu(
    db: Session,
    *,
    food_code: str,
    name: str,
    base_name: str,
    has_soup: bool = True,
    cooking_style: str = "rice",
    main_ingredient: str = "beef",
    sodium_mg: float = 400.0,
    heaviness: str = "moderate",
    flavor_intensity: str = "mild_clean",
    origin_type: str = "restaurant",
    carb_g: float = 40.0,
    protein_g: float = 18.0,
) -> Menu:
    menu = Menu(
        food_code=food_code,
        name=name,
        base_name=base_name,
        category_large="식사",
        meal_type="main_meal",
        has_soup=has_soup,
        cooking_style=cooking_style,
        main_ingredient=main_ingredient,
        sodium_mg=sodium_mg,
        heaviness=heaviness,
        flavor_intensity=flavor_intensity,
        origin_type=origin_type,
        carb_g=carb_g,
        protein_g=protein_g,
        temperature="hot",
        allergen_tags=[],
    )
    db.add(menu)
    db.flush()
    return menu


def test_order_sheet_persists_hard_filter_traces_and_audit_links_first_list_and_reaction(db: Session):
    user = User(kakao_id=870001, nickname="감사테스터")
    db.add(user)
    q_map = _seed_initial_questions(db)

    # 총 6개 상품(5종):
    # - 국물 없음 2개(2종): 비빔밥, 돈까스
    # - 국물 있음 & 면 1개(1종): 소고기우동
    # - 국물 있음 & 밥 & 해산물 1개(1종): 굴국밥
    # - 국물 있음 & 밥 & 소고기 2개(1종): 소고기국밥_특, 소고기국밥_보통
    _add_menu(db, food_code="A01", name="비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice", main_ingredient="beef")
    _add_menu(db, food_code="A02", name="돈까스", base_name="돈가스", has_soup=False, cooking_style="fried_pan", main_ingredient="pork")
    _add_menu(db, food_code="A03", name="소고기우동", base_name="우동", has_soup=True, cooking_style="noodle", main_ingredient="beef")
    _add_menu(db, food_code="A04", name="굴국밥", base_name="굴국밥", has_soup=True, cooking_style="rice", main_ingredient="seafood")
    best_beef_soup = _add_menu(
        db,
        food_code="A05",
        name="소고기국밥_특",
        base_name="소고기국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="beef",
        flavor_intensity="spicy_bold",
    )
    _add_menu(
        db,
        food_code="A06",
        name="소고기국밥_보통",
        base_name="소고기국밥",
        has_soup=True,
        cooking_style="rice",
        main_ingredient="beef",
        flavor_intensity="mild_clean",
    )
    db.commit()

    rec_resp = create_order_sheet_recommendation(
        db=db,
        user_id=user.id,
        request=OrderSheetRecommendRequest(
            answers=[
                AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
                AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="rice"),
                AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
                AnswerItemRequest(question_id=q_map["flavor_intensity"].id, option_id="spicy_bold"),
            ],
            top_k=5,
        ),
    )

    rec_id = rec_resp.recommendation_id
    assert [m.menu_id for m in rec_resp.recommended_menus] == [best_beef_soup.id]

    # 반응 전 감사 조회: 3개 하드필터가 순서대로 기록되고 전후 상품 수·종류 수가 정확해야 한다.
    audit_before = get_order_sheet_hard_filter_audit(db, rec_id)
    assert audit_before.recommendation_id == rec_id
    assert audit_before.user_id == user.id
    assert audit_before.recommended_menu_ids == [best_beef_soup.id]
    assert audit_before.applied_step_count == 3
    assert audit_before.initial_available_count == 6
    assert audit_before.initial_available_kind_count == 5
    assert audit_before.final_surviving_count == 2
    assert audit_before.final_surviving_kind_count == 1
    assert audit_before.is_single_candidate is True
    assert audit_before.is_single_candidate_rejection is False
    assert audit_before.outcome.outcome == OUTCOME_NO_RESPONSE

    assert [
        (
            t.step_order,
            t.question_id,
            t.question_code,
            t.cluster_key,
            t.option_id,
            t.before_count,
            t.after_count,
            t.before_kind_count,
            t.after_kind_count,
        )
        for t in audit_before.traces
    ] == [
        (1, q_map["has_soup"].id, q_map["has_soup"].question_code, "has_soup", "soup_yes", 6, 4, 5, 3),
        (2, q_map["cooking_style"].id, q_map["cooking_style"].question_code, "cooking_style", "rice", 4, 3, 3, 2),
        (3, q_map["main_ingredient"].id, q_map["main_ingredient"].question_code, "main_ingredient", "meat", 3, 2, 2, 1),
    ]

    # 명시적 목록 거절 후 감사 조회: 첫 목록과 거절 반응이 같은 주문서에 연결되어야 한다.
    reject_recommendation_list(db=db, recommendation_id=rec_id, user_id=user.id)
    audit_after = get_order_sheet_hard_filter_audit(db, rec_id)
    assert audit_after.outcome.outcome == OUTCOME_LIST_REJECTED
    assert audit_after.outcome.is_list_rejected is True
    assert audit_after.is_single_candidate is True
    assert audit_after.is_single_candidate_rejection is True
    assert audit_after.recommended_menu_ids == [best_beef_soup.id]

    # 존재하지 않는 추천 ID는 RecommendationNotFoundException
    with pytest.raises(RecommendationNotFoundException):
        get_order_sheet_hard_filter_audit(db, 999999)


def test_hard_filter_traces_skip_neutral_answers_and_rollback_atomically_with_recommendation(db: Session):
    user = User(kakao_id=870002, nickname="원자성테스터")
    db.add(user)
    q_map = _seed_initial_questions(db)
    _add_menu(db, food_code="B01", name="김치찌개", base_name="김치찌개", has_soup=True, cooking_style="soup_stew", main_ingredient="pork")
    _add_menu(db, food_code="B02", name="비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice", main_ingredient="beef")
    db.commit()

    # commit=False로 호출 후 롤백하면 Recommendation과 OrderSheetHardFilterTrace가 함께 롤백되어야 한다.
    rec_resp = create_order_sheet_recommendation(
        db=db,
        user_id=user.id,
        request=OrderSheetRecommendRequest(
            answers=[
                AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
                AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="any"),
                AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
            ],
        ),
        commit=False,
    )
    # flush된 상태에서는 2개(any 제외)가 트랜잭션 내 존재
    in_tx_rows = (
        db.query(OrderSheetHardFilterTrace)
        .filter(OrderSheetHardFilterTrace.recommendation_id == rec_resp.recommendation_id)
        .order_by(OrderSheetHardFilterTrace.step_order.asc())
        .all()
    )
    assert [(r.step_order, r.cluster_key, r.option_id) for r in in_tx_rows] == [
        (1, "has_soup", "soup_yes"),
        (2, "main_ingredient", "meat"),
    ]

    db.rollback()
    assert db.query(Recommendation).filter(Recommendation.id == rec_resp.recommendation_id).count() == 0
    assert (
        db.query(OrderSheetHardFilterTrace)
        .filter(OrderSheetHardFilterTrace.recommendation_id == rec_resp.recommendation_id)
        .count()
        == 0
    )


def test_summarize_single_candidate_rejections_groups_by_answer_combination_and_excludes_non_matching_cases(
    db: Session,
):
    u1 = User(kakao_id=870101, nickname="유저1")
    u2 = User(kakao_id=870102, nickname="유저2")
    db.add_all([u1, u2])
    q_map = _seed_initial_questions(db)

    # 조합 A (soup_yes + rice + meat) -> 1종(소고기국밥, 상품 2개)
    m_beef_soup = _add_menu(db, food_code="S01", name="소고기국밥_A", base_name="소고기국밥", has_soup=True, cooking_style="rice", main_ingredient="beef")
    _add_menu(db, food_code="S02", name="소고기국밥_B", base_name="소고기국밥", has_soup=True, cooking_style="rice", main_ingredient="beef")

    # 조합 B (soup_yes + noodle + non_meat) -> 1종(멸치국수)
    m_noodle = _add_menu(db, food_code="S03", name="멸치국수", base_name="국수", has_soup=True, cooking_style="noodle", main_ingredient="seafood")

    # 조합 C (soup_no + rice + meat) -> 2종(비빔밥, 불고기덮밥)
    _add_menu(db, food_code="S04", name="소고기비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice", main_ingredient="beef")
    _add_menu(db, food_code="S05", name="불고기덮밥", base_name="덮밥", has_soup=False, cooking_style="rice", main_ingredient="beef")
    db.commit()

    def _submit(user: User, answers: list[tuple[str, str]]):
        return create_order_sheet_recommendation(
            db=db,
            user_id=user.id,
            request=OrderSheetRecommendRequest(
                answers=[
                    AnswerItemRequest(question_id=q_map[cluster].id, option_id=opt_id)
                    for cluster, opt_id in answers
                ],
                top_k=5,
            ),
        )

    # 1) 조합 A (u1): 1종 + 목록 거절 -> 포함
    r_a1 = _submit(u1, [("has_soup", "soup_yes"), ("cooking_style", "rice"), ("main_ingredient", "meat")])
    reject_recommendation_list(db=db, recommendation_id=r_a1.recommendation_id, user_id=u1.id)

    # 2) 조합 A (u2, 제출 순서 다름): 1종 + 개별 싫어요 후 목록 거절 -> 같은 조합으로 묶여야 함
    r_a2 = _submit(u2, [("main_ingredient", "meat"), ("has_soup", "soup_yes"), ("cooking_style", "rice")])
    record_recommendation_feedback(
        db=db,
        recommendation_id=r_a2.recommendation_id,
        request=RecommendationFeedbackRequest(selected_menu_id=m_beef_soup.id, is_satisfied=False),
        user_id=u2.id,
    )
    reject_recommendation_list(db=db, recommendation_id=r_a2.recommendation_id, user_id=u2.id)

    # 3) 조합 B (u1): 1종 + 목록 거절 (1회) -> 포함
    r_b1 = _submit(u1, [("has_soup", "soup_yes"), ("cooking_style", "noodle"), ("main_ingredient", "non_meat")])
    reject_recommendation_list(db=db, recommendation_id=r_b1.recommendation_id, user_id=u1.id)

    # 4) 제외 케이스들:
    # - 1종 + 좋아요
    r_like = _submit(u1, [("has_soup", "soup_yes"), ("cooking_style", "rice"), ("main_ingredient", "meat")])
    record_recommendation_feedback(
        db=db,
        recommendation_id=r_like.recommendation_id,
        request=RecommendationFeedbackRequest(selected_menu_id=m_beef_soup.id, is_satisfied=True),
        user_id=u1.id,
    )
    # - 1종 + 개별 싫어요만(목록 거절 없음)
    r_dislike_only = _submit(u1, [("has_soup", "soup_yes"), ("cooking_style", "rice"), ("main_ingredient", "meat")])
    record_recommendation_feedback(
        db=db,
        recommendation_id=r_dislike_only.recommendation_id,
        request=RecommendationFeedbackRequest(selected_menu_id=m_beef_soup.id, is_satisfied=False),
        user_id=u1.id,
    )
    # - 1종 + 무응답
    _submit(u1, [("has_soup", "soup_yes"), ("cooking_style", "rice"), ("main_ingredient", "meat")])
    # - 2종 + 목록 거절 (후보가 1종이 아님)
    r_two_kinds = _submit(u1, [("has_soup", "soup_no"), ("cooking_style", "rice"), ("main_ingredient", "meat")])
    assert r_two_kinds.candidate_count == 2
    reject_recommendation_list(db=db, recommendation_id=r_two_kinds.recommendation_id, user_id=u1.id)
    # - 0종 (zero_candidates)
    r_zero = _submit(u1, [("has_soup", "soup_no"), ("cooking_style", "noodle"), ("main_ingredient", "meat")])
    assert r_zero.candidate_count == 0

    summary = summarize_single_candidate_rejections(db)
    assert summary.total_single_candidate_rejections == 3
    assert summary.unique_combination_count == 2
    assert summary.repeated_combination_count == 1
    assert len(summary.cases) == 3

    expected_combo_a = tuple(
        sorted([
            (q_map["has_soup"].question_code, "soup_yes"),
            (q_map["cooking_style"].question_code, "rice"),
            (q_map["main_ingredient"].question_code, "meat"),
        ])
    )
    expected_combo_b = tuple(
        sorted([
            (q_map["has_soup"].question_code, "soup_yes"),
            (q_map["cooking_style"].question_code, "noodle"),
            (q_map["main_ingredient"].question_code, "non_meat"),
        ])
    )

    assert [c.answer_combination for c in summary.combinations] == [expected_combo_a, expected_combo_b]
    combo_a, combo_b = summary.combinations
    assert combo_a.rejection_count == 2
    assert combo_a.distinct_user_count == 2
    assert set(combo_a.recommendation_ids) == {r_a1.recommendation_id, r_a2.recommendation_id}
    assert combo_a.rejected_menu_ids == [m_beef_soup.id]

    assert combo_b.rejection_count == 1
    assert combo_b.distinct_user_count == 1
    assert combo_b.recommendation_ids == [r_b1.recommendation_id]
    assert combo_b.rejected_menu_ids == [m_noodle.id]

    # min_occurrences=2 -> 반복된 조합 A만 반환
    repeated_only = summarize_single_candidate_rejections(db, min_occurrences=2)
    assert repeated_only.total_single_candidate_rejections == 2
    assert repeated_only.unique_combination_count == 1
    assert repeated_only.repeated_combination_count == 1
    assert repeated_only.combinations[0].answer_combination == expected_combo_a

    # user_id 필터링
    u2_only = summarize_single_candidate_rejections(db, user_id=u2.id)
    assert u2_only.total_single_candidate_rejections == 1
    assert u2_only.combinations[0].recommendation_ids == [r_a2.recommendation_id]

    # 기간(since_utc / until_utc) 필터링
    rec_a1_row = db.get(Recommendation, r_a1.recommendation_id)
    old_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    rec_a1_row.created_at = old_time
    db.commit()

    recent_summary = summarize_single_candidate_rejections(
        db,
        since_utc=datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert r_a1.recommendation_id not in [c.recommendation_id for c in recent_summary.cases]
    assert recent_summary.total_single_candidate_rejections == 2


def test_single_candidate_likes_and_rejections_included_in_overall_satisfaction_and_never_auto_replace_hard_filters(
    db: Session,
):
    user = User(kakao_id=870201, nickname="만족도테스터")
    db.add(user)
    q_map = _seed_initial_questions(db)

    m_single = _add_menu(db, food_code="OV01", name="소고기국밥", base_name="소고기국밥", has_soup=True, cooking_style="rice", main_ingredient="beef")
    m_multi1 = _add_menu(db, food_code="OV02", name="비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice", main_ingredient="beef")
    _add_menu(db, food_code="OV03", name="불고기덮밥", base_name="덮밥", has_soup=False, cooking_style="rice", main_ingredient="beef")
    db.commit()

    # 누적 전 하드필터 설정 및 질문 역할·가중치 스냅샷
    before_hf_clusters = tuple(settings.ORDER_SHEET_HARD_FILTER_CLUSTERS)
    before_role_overrides = dict(settings.ORDER_SHEET_QUESTION_ROLE_OVERRIDES)
    before_question_state = {
        q.id: (
            q.cluster_key,
            float(q.importance_weight),
            float(q.initial_score),
            int(q.positive_count),
            bool(q.is_active),
            resolve_question_role(q.id, q.cluster_key),
        )
        for q in db.query(OrderSheetQuestion).all()
    }

    def _submit(answers: list[tuple[str, str]]):
        return create_order_sheet_recommendation(
            db=db,
            user_id=user.id,
            request=OrderSheetRecommendRequest(
                answers=[
                    AnswerItemRequest(question_id=q_map[cluster].id, option_id=opt_id)
                    for cluster, opt_id in answers
                ],
                top_k=5,
            ),
        )

    single_answers = [("has_soup", "soup_yes"), ("cooking_style", "rice"), ("main_ingredient", "meat")]
    multi_answers = [("has_soup", "soup_no"), ("cooking_style", "rice"), ("main_ingredient", "meat")]
    zero_answers = [("has_soup", "soup_no"), ("cooking_style", "noodle"), ("main_ingredient", "meat")]

    # 1종 좋아요 1건
    r1 = _submit(single_answers)
    record_recommendation_feedback(
        db=db,
        recommendation_id=r1.recommendation_id,
        request=RecommendationFeedbackRequest(selected_menu_id=m_single.id, is_satisfied=True),
        user_id=user.id,
    )
    # 1종 목록 거절 3건 (반복 발생)
    rejected_single_ids: list[int] = []
    for _ in range(3):
        r_rej = _submit(single_answers)
        reject_recommendation_list(db=db, recommendation_id=r_rej.recommendation_id, user_id=user.id)
        rejected_single_ids.append(r_rej.recommendation_id)
    # 1종 개별 싫어요만 1건
    r_dis = _submit(single_answers)
    record_recommendation_feedback(
        db=db,
        recommendation_id=r_dis.recommendation_id,
        request=RecommendationFeedbackRequest(selected_menu_id=m_single.id, is_satisfied=False),
        user_id=user.id,
    )
    # 1종 무응답 1건
    _submit(single_answers)

    # 다수 후보(2종) 좋아요 2건
    for _ in range(2):
        r_ml = _submit(multi_answers)
        record_recommendation_feedback(
            db=db,
            recommendation_id=r_ml.recommendation_id,
            request=RecommendationFeedbackRequest(selected_menu_id=m_multi1.id, is_satisfied=True),
            user_id=user.id,
        )
    # 다수 후보(2종) 목록 거절 1건
    r_mr = _submit(multi_answers)
    reject_recommendation_list(db=db, recommendation_id=r_mr.recommendation_id, user_id=user.id)
    # 0종 1건
    _submit(zero_answers)

    # 감사 조회 및 집계 실행
    for rid in rejected_single_ids:
        audit = get_order_sheet_hard_filter_audit(db, rid)
        assert audit.is_single_candidate_rejection is True
    rej_summary = summarize_single_candidate_rejections(db, user_id=user.id)
    assert rej_summary.total_single_candidate_rejections == 3
    assert rej_summary.repeated_combination_count == 1

    sat = summarize_overall_satisfaction_with_hard_filter_audit(db, user_id=user.id)
    assert sat.overall.total_recommendations == 10
    assert sat.overall.zero_candidates_count == 1
    assert sat.overall.shown_list_count == 9
    assert sat.overall.first_list_liked_count == 3
    assert sat.overall.list_rejected_count == 4
    assert sat.overall.individual_disliked_only_count == 1
    assert sat.overall.no_response_count == 1

    assert sat.single_candidate_total_count == 6
    assert sat.single_candidate_liked_count == 1
    assert sat.single_candidate_rejected_count == 3
    assert sat.single_candidate_disliked_only_count == 1
    assert sat.single_candidate_no_response_count == 1

    assert sat.multi_candidate_total_count == 3
    assert sat.multi_candidate_liked_count == 2
    assert sat.multi_candidate_rejected_count == 1

    assert sat.overall.first_list_liked_count == sat.single_candidate_liked_count + sat.multi_candidate_liked_count
    assert sat.overall.list_rejected_count == sat.single_candidate_rejected_count + sat.multi_candidate_rejected_count
    assert sat.overall_like_rate == pytest.approx(3 / 9)
    assert sat.overall_rejection_rate == pytest.approx(4 / 9)

    # 점검 기록 누적·조회 후에도 하드필터 설정·질문 역할·가중치가 자동 교체/수정되지 않아야 한다.
    assert tuple(settings.ORDER_SHEET_HARD_FILTER_CLUSTERS) == before_hf_clusters
    assert dict(settings.ORDER_SHEET_QUESTION_ROLE_OVERRIDES) == before_role_overrides
    db.expire_all()
    after_question_state = {
        q.id: (
            q.cluster_key,
            float(q.importance_weight),
            float(q.initial_score),
            int(q.positive_count),
            bool(q.is_active),
            resolve_question_role(q.id, q.cluster_key),
        )
        for q in db.query(OrderSheetQuestion).all()
    }
    assert after_question_state == before_question_state

    # 같은 답변으로 다시 제출해도 하드필터 3개가 그대로 엄격 적용되어 여전히 1종만 반환한다.
    r_after = _submit(single_answers)
    assert r_after.candidate_count == 1
    assert [m.menu_id for m in r_after.recommended_menus] == [m_single.id]
    audit_after = get_order_sheet_hard_filter_audit(db, r_after.recommendation_id)
    assert [t.cluster_key for t in audit_after.traces] == ["has_soup", "cooking_style", "main_ingredient"]


def test_uuid_to_kakao_transition_migrates_hard_filter_traces_without_losing_user_ownership(
    db: Session,
):
    origin_user = create_uuid_user(db)
    db.flush()
    q_map = _seed_initial_questions(db)
    _add_menu(db, food_code="TR01", name="소고기국밥", base_name="소고기국밥", has_soup=True, cooking_style="rice", main_ingredient="beef")
    _add_menu(db, food_code="TR02", name="비빔밥", base_name="비빔밥", has_soup=False, cooking_style="rice", main_ingredient="beef")
    db.commit()

    rec_resp = create_order_sheet_recommendation(
        db=db,
        user_id=origin_user.id,
        request=OrderSheetRecommendRequest(
            answers=[
                AnswerItemRequest(question_id=q_map["has_soup"].id, option_id="soup_yes"),
                AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id="rice"),
                AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id="meat"),
            ],
            top_k=5,
        ),
    )
    reject_recommendation_list(
        db=db,
        recommendation_id=rec_resp.recommendation_id,
        user_id=origin_user.id,
    )

    target_user, _, _ = _merge_uuid_user_into_kakao_target(
        db=db,
        origin_user=origin_user,
        target_user=None,
        kakao_id=879999001,
        kakao_email="hf-audit-merged@example.com",
        now=utc_now(),
    )
    db.delete(origin_user)
    db.commit()

    migrated_traces = (
        db.query(OrderSheetHardFilterTrace)
        .filter(OrderSheetHardFilterTrace.recommendation_id == rec_resp.recommendation_id)
        .order_by(OrderSheetHardFilterTrace.step_order.asc())
        .all()
    )
    assert len(migrated_traces) == 3
    assert all(row.user_id == target_user.id for row in migrated_traces)

    audit = get_order_sheet_hard_filter_audit(db, rec_resp.recommendation_id)
    assert audit.user_id == target_user.id
    assert audit.is_single_candidate_rejection is True

    rej_summary = summarize_single_candidate_rejections(db, user_id=target_user.id)
    assert rej_summary.total_single_candidate_rejections == 1
    assert rej_summary.combinations[0].distinct_user_count == 1

    sat = summarize_overall_satisfaction_with_hard_filter_audit(db, user_id=target_user.id)
    assert sat.overall.list_rejected_count == 1
    assert sat.single_candidate_rejected_count == 1

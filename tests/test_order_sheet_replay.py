"""Spec #77 / Ticket #88: 과거 주문서의 첫 추천 순위 재현 단위·서비스 테스트."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.auth.models  # noqa: F401
from app.auth.dependencies import get_current_user
import app.map.models  # noqa: F401
from app.auth.transitions import _merge_uuid_user_into_kakao_target
from app.core.config import settings
from app.core.database import Base, get_db
from app.menus.models import (
    Menu,
    OrderSheetIssuance,
    OrderSheetQuestion,
    OrderSheetReplaySnapshot,
    Recommendation,
    RecommendationFeedback,
    UserMeal,
)
from app.recommend.order_sheet import (
    create_order_sheet_recommendation,
    issue_order_sheet_questions,
)
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.replay import (
    purge_expired_replay_snapshots,
    replay_first_list_from_snapshot,
    save_replay_snapshot_if_eligible,
    verify_and_load_recent_replay_snapshots,
)
from app.recommend.router import router as recommend_router
from app.recommend.schemas import AnswerItemRequest, OrderSheetRecommendRequest
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


def _seed_initial_nine_questions(db: Session) -> dict[str, OrderSheetQuestion]:
    pool = build_candidate_question_pool()
    by_cluster: dict[str, OrderSheetQuestion] = {}
    for idx, cluster in enumerate(settings.ORDER_SHEET_INITIAL_CLUSTERS, start=1):
        raw = next(q for q in pool if q["cluster_key"] == cluster)
        q = OrderSheetQuestion(
            question_code=raw["question_code"],
            cluster_key=cluster,
            question_text=raw["question_text"],
            options_json=raw["options"],
            initial_score=round(0.2 + 0.07 * idx, 4),
            importance_weight=round(0.2 + 0.07 * idx, 4),
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
    base_name: str | None = None,
    has_soup: bool = False,
    cooking_style: str = "rice",
    main_ingredient: str = "pork",
    sodium_mg: float | None = 500.0,
    heaviness: str = "moderate",
    flavor_intensity: str = "mild_clean",
    origin_type: str = "restaurant",
    carb_g: float | None = 40.0,
    protein_g: float | None = 15.0,
) -> Menu:
    menu = Menu(
        food_code=food_code,
        name=name,
        base_name=base_name or name,
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
    return menu


def _full_nine_answers(
    q_map: dict[str, OrderSheetQuestion],
    *,
    has_soup: str = "soup_no",
    cooking_style: str = "rice",
    main_ingredient: str = "any",
    sodium_level: str = "low_sodium",
    heaviness: str = "light",
    flavor_intensity: str = "spicy_bold",
    origin_type: str = "home_style",
    carb_level: str = "low_carb",
    protein_level: str = "high_protein",
) -> list[AnswerItemRequest]:
    return [
        AnswerItemRequest(question_id=q_map["sodium_level"].id, option_id=sodium_level),
        AnswerItemRequest(question_id=q_map["heaviness"].id, option_id=heaviness),
        AnswerItemRequest(question_id=q_map["has_soup"].id, option_id=has_soup),
        AnswerItemRequest(question_id=q_map["flavor_intensity"].id, option_id=flavor_intensity),
        AnswerItemRequest(question_id=q_map["origin_type"].id, option_id=origin_type),
        AnswerItemRequest(question_id=q_map["carb_level"].id, option_id=carb_level),
        AnswerItemRequest(question_id=q_map["protein_level"].id, option_id=protein_level),
        AnswerItemRequest(question_id=q_map["cooking_style"].id, option_id=cooking_style),
        AnswerItemRequest(question_id=q_map["main_ingredient"].id, option_id=main_ingredient),
    ]


def test_personal_order_sheet_saves_replay_snapshot_only_when_two_or_more_kinds_survive(db):
    assert settings.ORDER_SHEET_REPLAY_RETENTION_DAYS == 30
    assert settings.ORDER_SHEET_REPLAY_MAX_SHEETS == 20

    user = User(kakao_id=880001, nickname="재현유저1")
    db.add(user)
    q_map = _seed_initial_nine_questions(db)

    # 국물 없음(rice) 6종 + 같은 종류(비빔밥) 추가 상품 1개 = 총 7개 상품(6종)
    kind_names = ["비빔밥", "김치볶음밥", "제육덮밥", "오므라이스", "카레라이스", "알밥"]
    for idx, base in enumerate(kind_names, start=1):
        _add_menu(
            db,
            food_code=f"R00{idx}",
            name=f"{base}_기본",
            base_name=base,
            has_soup=False,
            cooking_style="rice",
            sodium_mg=200.0 if idx <= 3 else 600.0,
            heaviness="light" if idx <= 5 else "heavy",
            flavor_intensity="spicy_bold" if idx <= 4 else "mild_clean",
        )
    # 비빔밥 종류의 두 번째 상품
    _add_menu(
        db,
        food_code="R001_B",
        name="비빔밥_저점수",
        base_name="비빔밥",
        has_soup=False,
        cooking_style="rice",
        sodium_mg=800.0,
        heaviness="heavy",
        flavor_intensity="mild_clean",
    )
    # 국물 있음(rice) 단 1종 -> 1종 케이스 검증용
    _add_menu(
        db,
        food_code="S001",
        name="국밥_단독",
        base_name="국밥",
        has_soup=True,
        cooking_style="rice",
    )
    db.commit()

    # 1. 6종 통과 개인 주문서 -> 재현 스냅샷 저장 (6위 이하 및 동일 종류 비대표 상품까지 전체 후보 보존)
    issued = issue_order_sheet_questions(db, user_id=user.id, scope_key=f"user:{user.id}")
    resp_multi = create_order_sheet_recommendation(
        db=db,
        request=OrderSheetRecommendRequest(
            order_sheet_id=issued.order_sheet_id,
            answers=_full_nine_answers(q_map, has_soup="soup_no", cooking_style="rice"),
            top_k=5,
        ),
        user_id=user.id,
    )
    assert resp_multi.candidate_count == 6
    assert len(resp_multi.recommended_menus) == 5

    snap = (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.recommendation_id == resp_multi.recommendation_id)
        .one_or_none()
    )
    assert snap is not None
    assert snap.user_id == user.id
    issuance_row = (
        db.query(OrderSheetIssuance)
        .filter(OrderSheetIssuance.order_sheet_id == issued.order_sheet_id)
        .one()
    )
    assert snap.order_sheet_id == issuance_row.id
    assert snap.max_display_count == 5
    assert snap.shown_menu_ids_json == [m.menu_id for m in resp_multi.recommended_menus]
    assert len(snap.candidates_json) == 7  # 6종 + 비빔밥 2번째 상품 = 하드필터 통과 전체 후보 7개
    assert len(snap.score_question_ids_json) == 6
    assert snap.ranking_rule_version == settings.ORDER_SHEET_RANKING_RULE_VERSION
    assert snap.question_answer_version
    assert snap.expires_at - snap.completed_at == timedelta(days=30)
    assert isinstance(snap.tie_break_inputs_json, dict)

    # 2. 1종만 통과 개인 주문서 -> 재현 스냅샷 미저장
    resp_one = create_order_sheet_recommendation(
        db=db,
        request=OrderSheetRecommendRequest(
            answers=_full_nine_answers(q_map, has_soup="soup_yes", cooking_style="rice"),
            top_k=5,
        ),
        user_id=user.id,
    )
    assert resp_one.candidate_count == 1
    assert (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.recommendation_id == resp_one.recommendation_id)
        .first()
        is None
    )

    # 3. 0종 통과 개인 주문서 -> 재현 스냅샷 미저장
    resp_zero = create_order_sheet_recommendation(
        db=db,
        request=OrderSheetRecommendRequest(
            answers=_full_nine_answers(q_map, has_soup="soup_yes", cooking_style="noodle"),
            top_k=5,
        ),
        user_id=user.id,
    )
    assert resp_zero.candidate_count == 0
    assert (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.recommendation_id == resp_zero.recommendation_id)
        .first()
        is None
    )

    # 4. 익명(user_id=None) 또는 그룹 주문서(fixed_exclusion_keywords 지정) -> 재현 스냅샷 미저장
    resp_anon = create_order_sheet_recommendation(
        db=db,
        request=OrderSheetRecommendRequest(
            answers=_full_nine_answers(q_map, has_soup="soup_no", cooking_style="rice"),
            top_k=5,
        ),
        user_id=None,
    )
    assert (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.recommendation_id == resp_anon.recommendation_id)
        .first()
        is None
    )

    resp_group = create_order_sheet_recommendation(
        db=db,
        request=OrderSheetRecommendRequest(
            answers=_full_nine_answers(q_map, has_soup="soup_no", cooking_style="rice"),
            top_k=5,
        ),
        user_id=user.id,
        fixed_exclusion_keywords=[],
    )
    assert (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.recommendation_id == resp_group.recommendation_id)
        .first()
        is None
    )


def test_replay_reproduces_tiebreak_and_six_plus_after_menu_mutation_deletion_and_omit_question(db):
    user = User(kakao_id=880002, nickname="재현변동유저")
    db.add(user)
    q_map = _seed_initial_nine_questions(db)

    # 7종(+돈까스 비대표 2번째 상품) 구성:
    # - 1위(M01 비빔밥): 5점 (sodium, heaviness, flavor, origin, protein) + 0 = 5.0
    # - 2위(M02 제육덮밥): 4점 (sodium, heaviness, flavor, protein) + 0 = 4.0  [동점 앞 food_code="M02"]
    # - 3위(M03 김치볶음밥): 4점 (sodium, heaviness, flavor, protein) + 0 = 4.0 [동점 뒤 food_code="M03"]
    # - 4위(M04 카레라이스): 4점 + recent_meal_penalty(-1.0) = 3.0
    # - 5위(M05 돈까스): 3점 (sodium, heaviness, carb[Q6]) + long_term(+0.0) = 3.0 (food_code="M05" > "M04")
    # - 6위(M06 우동): 2점 (sodium, heaviness) + long_term(+0.25) = 2.25
    #   -> Q6(carb_level)를 제외하면 돈까스는 2.0점으로 내려가고, 원래 6위였던 우동(2.25점)이 5위로 올라온다!
    # - 7위(M07 오므라이스): 1점 (sodium) = 1.0
    m1 = _add_menu(
        db, food_code="M01", name="비빔밥", base_name="비빔밥",
        has_soup=False, cooking_style="rice",
        sodium_mg=200.0, heaviness="light", flavor_intensity="spicy_bold",
        origin_type="home_style", carb_g=50.0, protein_g=20.0,
    )
    m2 = _add_menu(
        db, food_code="M02", name="제육덮밥", base_name="제육덮밥",
        has_soup=False, cooking_style="rice",
        sodium_mg=200.0, heaviness="light", flavor_intensity="spicy_bold",
        origin_type="restaurant", carb_g=50.0, protein_g=20.0,
    )
    m3 = _add_menu(
        db, food_code="M03", name="김치볶음밥", base_name="김치볶음밥",
        has_soup=False, cooking_style="rice",
        sodium_mg=200.0, heaviness="light", flavor_intensity="spicy_bold",
        origin_type="restaurant", carb_g=50.0, protein_g=20.0,
    )
    m4 = _add_menu(
        db, food_code="M04", name="카레라이스", base_name="카레라이스",
        has_soup=False, cooking_style="rice",
        sodium_mg=200.0, heaviness="light", flavor_intensity="spicy_bold",
        origin_type="restaurant", carb_g=50.0, protein_g=20.0,
    )
    m5 = _add_menu(
        db, food_code="M05", name="돈까스", base_name="돈까스",
        has_soup=False, cooking_style="rice",
        sodium_mg=200.0, heaviness="light", flavor_intensity="mild_clean",
        origin_type="restaurant", carb_g=10.0, protein_g=5.0,
    )
    # 돈까스 같은 종류의 저점수 상품 (동일 종류 중복 제거 검증)
    _add_menu(
        db, food_code="M05_B", name="돈가스_저점수", base_name="돈가스",
        has_soup=False, cooking_style="rice",
        sodium_mg=800.0, heaviness="heavy", flavor_intensity="mild_clean",
        origin_type="restaurant", carb_g=80.0, protein_g=5.0,
    )
    m6 = _add_menu(
        db, food_code="M06", name="우동", base_name="우동",
        has_soup=False, cooking_style="rice",
        sodium_mg=200.0, heaviness="light", flavor_intensity="mild_clean",
        origin_type="restaurant", carb_g=50.0, protein_g=5.0,
    )
    m7 = _add_menu(
        db, food_code="M07", name="오므라이스", base_name="오므라이스",
        has_soup=False, cooking_style="rice",
        sodium_mg=200.0, heaviness="heavy", flavor_intensity="mild_clean",
        origin_type="restaurant", carb_g=50.0, protein_g=5.0,
    )
    db.commit()

    resp = create_order_sheet_recommendation(
        db=db,
        request=OrderSheetRecommendRequest(
            answers=_full_nine_answers(q_map, has_soup="soup_no", cooking_style="rice"),
            top_k=5,
        ),
        user_id=user.id,
        long_term_preference_scores={"우동": 0.25},
        recent_meal_penalties={m4.id: -1.0},
    )

    expected_top5 = [m1.id, m2.id, m3.id, m4.id, m5.id]
    assert [m.menu_id for m in resp.recommended_menus] == expected_top5

    snap = (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.recommendation_id == resp.recommendation_id)
        .one()
    )

    # 추천 이후 메뉴 속성 변경, 상위 메뉴 삭제, 새 식사 기록 및 새 피드백 추가
    m2.sodium_mg = 9999.0
    m2.heaviness = "heavy"
    m2.has_soup = True
    m6.carb_g = 1.0
    m6.flavor_intensity = "spicy_bold"
    m6.origin_type = "home_style"
    m6.protein_g = 40.0
    db.delete(m1)
    db.delete(m5)
    db.add(UserMeal(user_id=user.id, menu_id=m2.id, menu_name=m2.name))
    db.add(
        RecommendationFeedback(
            recommendation_id=resp.recommendation_id,
            user_id=user.id,
            selected_menu_id=m3.id,
            is_satisfied=False,
        )
    )
    db.commit()

    # 순수 재현은 DB 메뉴/이력 변경과 무관하게 원래 첫 목록과 순서를 정확히 재현한다
    baseline = replay_first_list_from_snapshot(snap)
    assert baseline.matches_shown is True
    assert baseline.replayed_menu_ids == expected_top5
    assert [e.menu_id for e in baseline.all_kind_entries] == [
        m1.id, m2.id, m3.id, m4.id, m5.id, m6.id, m7.id,
    ]
    assert [e.rank for e in baseline.replayed_entries] == [1, 2, 3, 4, 5]

    # Q6(carb_level) 가점만 0으로 제외하면 돈까스(5위, 3.0 -> 2.0)가 빠지고 우동(6위, 2.25)이 5위로 진입한다
    omitted = replay_first_list_from_snapshot(snap, omit_question_id=q_map["carb_level"].id)
    assert omitted.omit_question_id == q_map["carb_level"].id
    assert omitted.replayed_menu_ids == [m1.id, m2.id, m3.id, m4.id, m6.id]
    assert omitted.matches_shown is False
    assert omitted.all_kind_entries[5].menu_id == m5.id
    assert omitted.all_kind_entries[5].total_score == pytest.approx(2.0)


def test_duplicate_or_retry_save_preserves_first_snapshot_untouched(db):
    user = User(kakao_id=880003, nickname="재전송유저")
    db.add(user)
    q_map = _seed_initial_nine_questions(db)

    m1 = _add_menu(
        db, food_code="D001", name="비빔밥", base_name="비빔밥",
        has_soup=False, cooking_style="rice", sodium_mg=200.0,
    )
    m2 = _add_menu(
        db, food_code="D002", name="덮밥", base_name="덮밥",
        has_soup=False, cooking_style="rice", sodium_mg=800.0,
    )
    m3 = _add_menu(
        db, food_code="D003", name="볶음밥", base_name="볶음밥",
        has_soup=False, cooking_style="rice", sodium_mg=800.0,
    )
    db.commit()

    issued = issue_order_sheet_questions(db, user_id=user.id, scope_key=f"user:{user.id}")
    req = OrderSheetRecommendRequest(
        order_sheet_id=issued.order_sheet_id,
        answers=_full_nine_answers(q_map, has_soup="soup_no", cooking_style="rice"),
        top_k=5,
    )
    first_resp = create_order_sheet_recommendation(db=db, request=req, user_id=user.id)
    first_snap = (
        db.query(OrderSheetReplaySnapshot)
        .filter(OrderSheetReplaySnapshot.recommendation_id == first_resp.recommendation_id)
        .one()
    )
    original_id = first_snap.id
    original_shown = list(first_snap.shown_menu_ids_json)
    original_candidates = list(first_snap.candidates_json)
    original_completed_at = first_snap.completed_at

    # 1. 동일 recommendation_id로 save_replay_snapshot_if_eligible 재호출 시 최초 스냅샷 유지
    first_rec = db.get(Recommendation, first_resp.recommendation_id)
    from app.recommend.ranking import evaluate_order_sheet_candidates

    alt_eval = evaluate_order_sheet_candidates(
        menus=[m2, m3],
        exclusion_keywords=set(),
        validated_answers=[],
        top_k=5,
    )
    second_call = save_replay_snapshot_if_eligible(
        db=db,
        recommendation=first_rec,
        evaluation=alt_eval,
        answers_payload=[{"question_id": 999, "option_id": "tampered"}],
        order_sheet_id=first_snap.order_sheet_id,
        max_display_count=2,
        completed_at=original_completed_at + timedelta(hours=3),
    )
    db.commit()

    assert second_call is not None
    assert second_call.id == original_id
    assert second_call.shown_menu_ids_json == original_shown
    assert second_call.candidates_json == original_candidates
    assert second_call.completed_at == original_completed_at

    # 2. 메뉴 속성이 바뀐 뒤 동일 order_sheet_id로 요청이 재전송돼도 최초 스냅샷 1건이 그대로 유지된다
    m1.sodium_mg = 900.0
    m3.sodium_mg = 100.0
    db.commit()

    retry_resp = create_order_sheet_recommendation(db=db, request=req, user_id=user.id)
    assert retry_resp.recommendation_id != first_resp.recommendation_id

    all_snaps = db.query(OrderSheetReplaySnapshot).all()
    assert len(all_snaps) == 1
    assert all_snaps[0].id == original_id
    assert all_snaps[0].recommendation_id == first_resp.recommendation_id
    assert all_snaps[0].shown_menu_ids_json == original_shown
    assert all_snaps[0].candidates_json == original_candidates


def _make_synthetic_rec_and_snapshot(
    db: Session,
    *,
    user_id: int,
    completed_at: datetime,
    expires_at: datetime | None = None,
    shown_menu_ids: list[int] | None = None,
    candidates_json: list[dict] | None = None,
    with_snapshot: bool = True,
    initial_candidate_count: int = 2,
) -> tuple[Recommendation, OrderSheetReplaySnapshot | None]:
    shown = shown_menu_ids if shown_menu_ids is not None else [101, 102]
    rec = Recommendation(
        user_id=user_id,
        shown_question_ids=[1, 2],
        answers_json=[{"question_id": 1, "option_id": "low_sodium"}],
        recommended_menu_ids=shown,
        initial_candidate_count=initial_candidate_count,
        created_at=completed_at,
    )
    db.add(rec)
    db.flush()

    if not with_snapshot:
        return rec, None

    cands = (
        candidates_json
        if candidates_json is not None
        else [
            {
                "menu_id": 101,
                "food_code": "A001",
                "food_kind_key": "bibimbap",
                "question_scores": {"1": 1.0},
                "recent_meal_adjustment": 0.0,
                "personal_adjustment": 0.0,
                "other_score": 0.0,
                "tie_breaker": ["A001", 101],
            },
            {
                "menu_id": 102,
                "food_code": "A002",
                "food_kind_key": "udon",
                "question_scores": {"1": 0.0},
                "recent_meal_adjustment": 0.0,
                "personal_adjustment": 0.0,
                "other_score": 0.0,
                "tie_breaker": ["A002", 102],
            },
        ]
    )
    snap = OrderSheetReplaySnapshot(
        recommendation_id=rec.id,
        user_id=user_id,
        order_sheet_id=None,
        completed_at=completed_at,
        expires_at=expires_at if expires_at is not None else completed_at + timedelta(days=30),
        question_answer_version="spec77_qa_v1",
        ranking_rule_version="spec77_v1",
        max_display_count=5,
        shown_menu_ids_json=shown,
        score_question_ids_json=[1],
        answers_json=[{"question_id": 1, "option_id": "low_sodium"}],
        candidates_json=cands,
        tie_break_inputs_json={"sort_rule": "final_score_desc_food_code_asc_menu_id_asc"},
        created_at=completed_at,
    )
    db.add(snap)
    db.flush()
    return rec, snap


def test_verify_and_load_enforces_30_day_window_max_20_newest_and_user_isolation(db):
    now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    user = User(kakao_id=880004, nickname="최근20장유저")
    other_user = User(kakao_id=880005, nickname="타사용자")
    db.add_all([user, other_user])
    db.flush()

    # 23건: 최근 23일간 하루에 1건씩 (최신이 day_offset=1, 가장 오래된 것이 day_offset=23)
    in_window_recs: list[tuple[int, Recommendation]] = []
    for day_offset in range(23, 0, -1):
        ts = (now - timedelta(days=day_offset)).replace(tzinfo=None)
        rec, _ = _make_synthetic_rec_and_snapshot(db, user_id=user.id, completed_at=ts)
        in_window_recs.append((day_offset, rec))

    # 31일 전, 35일 전 -> 30일 윈도우 밖 (만료 포함)
    _make_synthetic_rec_and_snapshot(
        db, user_id=user.id, completed_at=(now - timedelta(days=31)).replace(tzinfo=None),
    )
    _make_synthetic_rec_and_snapshot(
        db, user_id=user.id, completed_at=(now - timedelta(days=35)).replace(tzinfo=None),
    )

    # 5일 전 완료했지만 명시적으로 만료된 스냅샷 -> 제외되어야 함
    _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(days=5)).replace(tzinfo=None),
        expires_at=(now - timedelta(minutes=1)).replace(tzinfo=None),
    )

    # 다른 사용자의 최신 스냅샷 -> 절대 섞이지 않아야 함
    _make_synthetic_rec_and_snapshot(
        db, user_id=other_user.id, completed_at=(now - timedelta(hours=1)).replace(tzinfo=None),
    )
    db.commit()

    batch = verify_and_load_recent_replay_snapshots(
        db,
        user.id,
        now=now,
        window_days=30,
        max_sheets=20,
    )
    assert batch.user_id == user.id
    assert batch.window_days == 30
    assert batch.max_sheets == 20
    assert len(batch.verified_sheets) == 20
    assert batch.excluded_sheets == []

    # 최신순(day_offset 1 -> 20) 정렬 확인
    expected_rec_ids = [
        rec.id for day_offset, rec in sorted(in_window_recs, key=lambda x: x[0])[:20]
    ]
    assert [v.snapshot.recommendation_id for v in batch.verified_sheets] == expected_rec_ids


def test_verify_and_load_excludes_mismatched_and_missing_snapshots_with_reasons_without_failing_batch(
    db, caplog,
):
    now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    user = User(kakao_id=880006, nickname="불일치검증유저")
    db.add(user)
    db.flush()

    rec_valid, _ = _make_synthetic_rec_and_snapshot(
        db, user_id=user.id, completed_at=(now - timedelta(hours=1)).replace(tzinfo=None),
    )
    # 저장된 첫 목록 순서가 [102, 101]인데 재계산 결과는 [101, 102] -> replay_mismatch
    rec_mismatch, snap_mismatch = _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(hours=2)).replace(tzinfo=None),
        shown_menu_ids=[102, 101],
    )
    # 후보 JSON 손상 -> replay_mismatch로 안전하게 제외
    rec_corrupt, snap_corrupt = _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(hours=3)).replace(tzinfo=None),
        candidates_json=[{"bad": "entry"}],
    )
    # 하드필터 통과 3종 개인 주문서이지만 스냅샷 누락 -> missing_snapshot
    rec_missing, _ = _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(hours=4)).replace(tzinfo=None),
        with_snapshot=False,
        initial_candidate_count=3,
    )
    # 하드필터 통과 1종 주문서(원래 스냅샷 저장 대상 아님) -> 제외 목록에도 오르지 않음
    _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(hours=5)).replace(tzinfo=None),
        shown_menu_ids=[101],
        with_snapshot=False,
        initial_candidate_count=1,
    )
    db.commit()

    with caplog.at_level(logging.WARNING, logger="app.recommend.replay"):
        batch = verify_and_load_recent_replay_snapshots(db, user.id, now=now)

    assert [v.snapshot.recommendation_id for v in batch.verified_sheets] == [rec_valid.id]

    excluded_by_rec = {e.recommendation_id: e for e in batch.excluded_sheets}
    assert set(excluded_by_rec.keys()) == {rec_mismatch.id, rec_corrupt.id, rec_missing.id}

    assert excluded_by_rec[rec_mismatch.id].reason == "replay_mismatch"
    assert excluded_by_rec[rec_mismatch.id].snapshot_id == snap_mismatch.id
    assert excluded_by_rec[rec_mismatch.id].expected_menu_ids == [102, 101]
    assert excluded_by_rec[rec_mismatch.id].replayed_menu_ids == [101, 102]

    assert excluded_by_rec[rec_corrupt.id].reason == "replay_mismatch"
    assert excluded_by_rec[rec_corrupt.id].snapshot_id == snap_corrupt.id
    assert excluded_by_rec[rec_corrupt.id].replayed_menu_ids is None
    assert "error" in excluded_by_rec[rec_corrupt.id].details

    assert excluded_by_rec[rec_missing.id].reason == "missing_snapshot"
    assert excluded_by_rec[rec_missing.id].snapshot_id is None

    assert any("replay_mismatch" in rec.message for rec in caplog.records)
    assert any("missing_snapshot" in rec.message for rec in caplog.records)


def test_review_budget_includes_missing_sheets_and_replays_each_question(db):
    now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    user = User(kakao_id=880008, nickname="review")
    db.add(user)
    db.flush()
    rec, _ = _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(hours=1)).replace(tzinfo=None),
    )
    missing, _ = _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(hours=2)).replace(tzinfo=None),
        with_snapshot=False,
    )
    _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(hours=3)).replace(tzinfo=None),
    )
    db.commit()

    batch = verify_and_load_recent_replay_snapshots(db, user.id, now=now, max_sheets=2)

    assert [sheet.snapshot.recommendation_id for sheet in batch.verified_sheets] == [rec.id]
    assert [sheet.recommendation_id for sheet in batch.excluded_sheets] == [missing.id]
    (sheet,) = batch.verified_sheets
    assert sheet.baseline_replay.replayed_menu_ids == [101, 102]
    assert set(sheet.question_replays) == {1}
    assert sheet.question_replays[1].replayed_menu_ids == [101, 102]
    assert sheet.question_replays[1].replayed_entries[0].total_score == 0.0


def test_http_first_list_replays_in_display_order_after_catalog_changes(db):
    user = User(kakao_id=880009, nickname="first-list")
    db.add(user)
    questions = _seed_initial_nine_questions(db)
    sodium_question_id = questions["sodium_level"].id
    names = ["비빔밥", "김치볶음밥", "제육덮밥", "오므라이스", "카레라이스", "알밥"]
    seeded_menus = [
        _add_menu(
            db,
            food_code=f"HTTP88_{i}",
            name=names[i],
            sodium_mg=900.0 if i == 0 else 100.0,
            has_soup=i % 2 == 0,
            cooking_style="rice" if i % 2 == 0 else "noodle",
            main_ingredient="pork" if i % 2 == 0 else "seafood",
            heaviness="light" if i % 2 == 0 else "heavy",
            flavor_intensity="spicy_bold" if i % 2 == 0 else "mild_clean",
            origin_type="home_style" if i % 2 == 0 else "restaurant",
            carb_g=10.0 if i % 2 == 0 else 60.0,
            protein_g=8.0 if i % 2 == 0 else 30.0,
        )
        for i in reversed(range(len(names)))
    ]
    menus = list(reversed(seeded_menus))
    db.commit()
    menu_ids = [menu.id for menu in menus]

    app = FastAPI()
    app.include_router(recommend_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user

    async def submit_issued_sheet():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            issued = await client.get("/api/recommendations/order-sheet/questions")
            assert issued.status_code == 200, issued.text
            sheet = issued.json()["data"]
            answers = [
                {
                    "question_id": question["id"],
                    "option_id": "low_sodium" if question["id"] == sodium_question_id else "any",
                }
                for question in sheet["questions"]
            ]
            response = await client.post(
                "/api/recommendations/order-sheet",
                json={"order_sheet_id": sheet["order_sheet_id"], "answers": answers},
            )
            assert response.status_code == 201, response.text
            return response.json()["data"]

    recommendation = asyncio.run(submit_issued_sheet())
    shown_ids = [entry["menu_id"] for entry in recommendation["recommended_menus"]]
    assert shown_ids == menu_ids[1:]

    menus[1].food_code = "UPDATED"
    menus[1].sodium_mg = 2000.0
    db.delete(menus[2])
    db.commit()

    review = verify_and_load_recent_replay_snapshots(db, user.id)
    (verified,) = review.verified_sheets
    assert verified.snapshot.recommendation_id == recommendation["recommendation_id"]
    assert verified.baseline_replay.replayed_menu_ids == shown_ids
    assert len(verified.question_replays) == 6
    assert verified.question_replays[sodium_question_id].replayed_menu_ids == menu_ids[:5]


def test_purge_expired_replay_snapshots_removes_only_expired_and_keeps_recommendations_and_feedback(db):
    now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    user = User(kakao_id=880007, nickname="만료정리유저")
    db.add(user)
    db.flush()

    rec_expired_1, _ = _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(days=35)).replace(tzinfo=None),
        expires_at=(now - timedelta(days=5)).replace(tzinfo=None),
    )
    rec_expired_exact, _ = _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(days=30)).replace(tzinfo=None),
        expires_at=now.replace(tzinfo=None),
    )
    rec_active, snap_active = _make_synthetic_rec_and_snapshot(
        db,
        user_id=user.id,
        completed_at=(now - timedelta(days=10)).replace(tzinfo=None),
        expires_at=(now + timedelta(days=20)).replace(tzinfo=None),
    )
    db.add(
        RecommendationFeedback(
            recommendation_id=rec_expired_1.id,
            user_id=user.id,
            is_satisfied=True,
        )
    )
    db.commit()

    deleted = purge_expired_replay_snapshots(db, now=now)
    assert deleted == 2

    remaining_snaps = db.query(OrderSheetReplaySnapshot).all()
    assert [s.id for s in remaining_snaps] == [snap_active.id]

    # 기존 추천 이력과 피드백은 만료 정리 후에도 그대로 남는다
    assert db.get(Recommendation, rec_expired_1.id) is not None
    assert db.get(Recommendation, rec_expired_exact.id) is not None
    assert (
        db.query(RecommendationFeedback)
        .filter(RecommendationFeedback.recommendation_id == rec_expired_1.id)
        .count()
        == 1
    )


def test_uuid_to_kakao_merge_migrates_replay_snapshots_losslessly(db):
    now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    now_naive = now.replace(tzinfo=None)

    uuid_user = User(uuid="uuid-replay-origin-1", nickname="임시유저")
    kakao_user = User(kakao_id=880999, nickname="카카오유저")
    db.add_all([uuid_user, kakao_user])
    db.flush()

    rec_uuid, snap_uuid = _make_synthetic_rec_and_snapshot(
        db, user_id=uuid_user.id, completed_at=now_naive - timedelta(hours=2),
    )
    rec_kakao, snap_kakao = _make_synthetic_rec_and_snapshot(
        db, user_id=kakao_user.id, completed_at=now_naive - timedelta(hours=1),
    )
    db.commit()

    target, is_new, _ = _merge_uuid_user_into_kakao_target(
        db=db,
        origin_user=uuid_user,
        target_user=kakao_user,
        kakao_id=880999,
        kakao_email="kakao@example.com",
        now=now_naive,
    )
    db.delete(uuid_user)
    db.commit()

    assert is_new is False
    assert target.id == kakao_user.id

    migrated = db.get(OrderSheetReplaySnapshot, snap_uuid.id)
    assert migrated is not None
    assert migrated.user_id == kakao_user.id

    batch = verify_and_load_recent_replay_snapshots(db, kakao_user.id, now=now)
    assert [v.snapshot.recommendation_id for v in batch.verified_sheets] == [
        rec_kakao.id,
        rec_uuid.id,
    ]
    assert batch.excluded_sheets == []





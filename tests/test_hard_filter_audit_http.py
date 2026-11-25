"""Spec #77 / Ticket #87: HTTP 주문서 제출·피드백·거절 흐름 기반 하드필터 감사 통합 테스트."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import handle_recommendation_domain_exception
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
)
from app.recommend.common import RecommendationDomainException
from app.recommend.feedback import OUTCOME_FIRST_LIST_LIKED, OUTCOME_LIST_REJECTED
from app.recommend.hard_filter_audit import (
    get_order_sheet_hard_filter_audit,
    summarize_overall_satisfaction_with_hard_filter_audit,
    summarize_single_candidate_rejections,
)
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.router import router as recommend_router
from app.users.models import User
from tests.auth_helpers import session_token


@pytest.fixture
def http_env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()

    user1 = User(kakao_id=871001, nickname="HTTP감사유저1")
    user2 = User(kakao_id=871002, nickname="HTTP감사유저2")
    db.add_all([user1, user2])

    pool = build_candidate_question_pool()
    q_map: dict[str, OrderSheetQuestion] = {}
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
        q_map[cluster] = q

    # 6개 상품(5종):
    # - 국물 있음 & 밥 & 소고기: 소고기국밥 2상품(1종)
    # - 국물 있음 & 밥 & 해산물: 굴국밥(1종)
    # - 국물 있음 & 면 & 소고기: 소고기우동(1종)
    # - 국물 없음 & 밥 & 소고기: 비빔밥(1종), 불고기덮밥(1종)
    menus = [
        Menu(
            food_code="H87_01",
            name="소고기국밥_특",
            base_name="소고기국밥",
            category_large="한식",
            meal_type="main_meal",
            has_soup=True,
            cooking_style="rice",
            main_ingredient="beef",
            flavor_intensity="spicy_bold",
            temperature="hot",
            heaviness="heavy",
            origin_type="restaurant",
            sodium_mg=500.0,
            carb_g=40.0,
            protein_g=22.0,
            allergen_tags=[],
        ),
        Menu(
            food_code="H87_02",
            name="소고기국밥_보통",
            base_name="소고기국밥",
            category_large="한식",
            meal_type="main_meal",
            has_soup=True,
            cooking_style="rice",
            main_ingredient="beef",
            flavor_intensity="mild_clean",
            temperature="hot",
            heaviness="moderate",
            origin_type="restaurant",
            sodium_mg=500.0,
            carb_g=40.0,
            protein_g=16.0,
            allergen_tags=[],
        ),
        Menu(
            food_code="H87_03",
            name="굴국밥",
            base_name="굴국밥",
            category_large="한식",
            meal_type="main_meal",
            has_soup=True,
            cooking_style="rice",
            main_ingredient="seafood",
            flavor_intensity="mild_clean",
            temperature="hot",
            heaviness="light",
            origin_type="home_style",
            sodium_mg=200.0,
            carb_g=12.0,
            protein_g=18.0,
            allergen_tags=[],
        ),
        Menu(
            food_code="H87_04",
            name="소고기우동",
            base_name="우동",
            category_large="일식",
            meal_type="main_meal",
            has_soup=True,
            cooking_style="noodle",
            main_ingredient="beef",
            flavor_intensity="mild_clean",
            temperature="hot",
            heaviness="light",
            origin_type="restaurant",
            sodium_mg=450.0,
            carb_g=50.0,
            protein_g=14.0,
            allergen_tags=[],
        ),
        Menu(
            food_code="H87_05",
            name="소고기비빔밥",
            base_name="비빔밥",
            category_large="한식",
            meal_type="main_meal",
            has_soup=False,
            cooking_style="rice",
            main_ingredient="beef",
            flavor_intensity="spicy_bold",
            temperature="hot",
            heaviness="moderate",
            origin_type="home_style",
            sodium_mg=220.0,
            carb_g=14.0,
            protein_g=20.0,
            allergen_tags=[],
        ),
        Menu(
            food_code="H87_06",
            name="불고기덮밥",
            base_name="덮밥",
            category_large="한식",
            meal_type="main_meal",
            has_soup=False,
            cooking_style="rice",
            main_ingredient="beef",
            flavor_intensity="mild_clean",
            temperature="hot",
            heaviness="heavy",
            origin_type="restaurant",
            sodium_mg=480.0,
            carb_g=55.0,
            protein_g=19.0,
            allergen_tags=[],
        ),
    ]
    db.add_all(menus)
    db.commit()

    headers1 = {"Authorization": f"Bearer {session_token(db, user1)}"}
    headers2 = {"Authorization": f"Bearer {session_token(db, user2)}"}

    app = FastAPI()
    app.add_exception_handler(RecommendationDomainException, handle_recommendation_domain_exception)
    app.include_router(recommend_router)
    app.dependency_overrides[get_db] = lambda: db

    with TestClient(app) as client:
        yield {
            "client": client,
            "db": db,
            "user1": user1,
            "user2": user2,
            "headers1": headers1,
            "headers2": headers2,
            "q_map": q_map,
            "menus": menus,
        }

    db.close()
    Base.metadata.drop_all(bind=engine)


def test_http_order_sheet_hard_filter_trace_rejection_audit_and_no_auto_replacement(http_env):
    client: TestClient = http_env["client"]
    db = http_env["db"]
    headers1 = http_env["headers1"]
    headers2 = http_env["headers2"]
    q_map = http_env["q_map"]
    best_beef_soup = http_env["menus"][0]

    before_hf_clusters = tuple(settings.ORDER_SHEET_HARD_FILTER_CLUSTERS)
    before_role_overrides = dict(settings.ORDER_SHEET_QUESTION_ROLE_OVERRIDES)

    def _build_answers(questions: list[dict], *, has_soup_opt: str, style_opt: str, ing_opt: str) -> list[dict]:
        overrides = {
            "has_soup": has_soup_opt,
            "cooking_style": style_opt,
            "main_ingredient": ing_opt,
            "flavor_intensity": "spicy_bold",
        }
        result = []
        for q in questions:
            cluster = q["cluster_key"]
            opt_id = overrides.get(cluster, "any")
            result.append({"question_id": q["id"], "option_id": opt_id})
        return result

    # 1) user1: 주문서 발급 -> 1종(소고기국밥)으로 축소 -> 명시적 목록 거절
    q_res1 = client.get("/api/recommendations/order-sheet/questions", headers=headers1)
    assert q_res1.status_code == 200
    sheet1 = q_res1.json()["data"]
    rec_res1 = client.post(
        "/api/recommendations/order-sheet",
        headers=headers1,
        json={
            "order_sheet_id": sheet1["order_sheet_id"],
            "answers": _build_answers(sheet1["questions"], has_soup_opt="soup_yes", style_opt="rice", ing_opt="meat"),
            "top_k": 5,
        },
    )
    assert rec_res1.status_code == 201
    rec1_data = rec_res1.json()["data"]
    assert rec1_data["candidate_count"] == 1
    assert [m["menu_id"] for m in rec1_data["recommended_menus"]] == [best_beef_soup.id]

    rej_res1 = client.post(
        f"/api/recommendations/{rec1_data['recommendation_id']}/all-menu-reject",
        headers=headers1,
    )
    assert rej_res1.status_code == 200
    assert rej_res1.json()["data"]["outcome"] == OUTCOME_LIST_REJECTED

    # 2) user2: 동일 하드필터 조합 -> 1종 -> 명시적 목록 거절 (반복 사례 생성)
    q_res2 = client.get("/api/recommendations/order-sheet/questions", headers=headers2)
    assert q_res2.status_code == 200
    sheet2 = q_res2.json()["data"]
    rec_res2 = client.post(
        "/api/recommendations/order-sheet",
        headers=headers2,
        json={
            "order_sheet_id": sheet2["order_sheet_id"],
            "answers": _build_answers(sheet2["questions"], has_soup_opt="soup_yes", style_opt="rice", ing_opt="meat"),
            "top_k": 5,
        },
    )
    assert rec_res2.status_code == 201
    rec2_id = rec_res2.json()["data"]["recommendation_id"]
    assert client.post(f"/api/recommendations/{rec2_id}/all-menu-reject", headers=headers2).status_code == 200

    # 3) user1: 1종 후보에 좋아요 (전체 만족도에 포함되어야 함)
    q_res3 = client.get("/api/recommendations/order-sheet/questions", headers=headers1)
    sheet3 = q_res3.json()["data"]
    rec_res3 = client.post(
        "/api/recommendations/order-sheet",
        headers=headers1,
        json={
            "order_sheet_id": sheet3["order_sheet_id"],
            "answers": _build_answers(sheet3["questions"], has_soup_opt="soup_yes", style_opt="rice", ing_opt="meat"),
            "top_k": 5,
        },
    )
    assert rec_res3.status_code == 201
    rec3_id = rec_res3.json()["data"]["recommendation_id"]
    fb_res3 = client.post(
        f"/api/recommendations/{rec3_id}/feedback",
        headers=headers1,
        json={"selected_menu_id": best_beef_soup.id, "is_satisfied": True},
    )
    assert fb_res3.status_code == 200

    # 4) user1: 2종 후보에 좋아요
    q_res4 = client.get("/api/recommendations/order-sheet/questions", headers=headers1)
    sheet4 = q_res4.json()["data"]
    rec_res4 = client.post(
        "/api/recommendations/order-sheet",
        headers=headers1,
        json={
            "order_sheet_id": sheet4["order_sheet_id"],
            "answers": _build_answers(sheet4["questions"], has_soup_opt="soup_no", style_opt="rice", ing_opt="meat"),
            "top_k": 5,
        },
    )
    assert rec_res4.status_code == 201
    rec4_data = rec_res4.json()["data"]
    assert rec4_data["candidate_count"] == 2
    fb_res4 = client.post(
        f"/api/recommendations/{rec4_data['recommendation_id']}/feedback",
        headers=headers1,
        json={"selected_menu_id": rec4_data["recommended_menus"][0]["menu_id"], "is_satisfied": True},
    )
    assert fb_res4.status_code == 200

    # 운영자 감사 확인 1: 주문서별 하드필터 축소 과정 + 첫 목록 + 거절 연결
    audit1 = get_order_sheet_hard_filter_audit(db, rec1_data["recommendation_id"])
    assert audit1.recommended_menu_ids == [best_beef_soup.id]
    assert audit1.outcome.outcome == OUTCOME_LIST_REJECTED
    assert audit1.is_single_candidate_rejection is True
    assert [(t.step_order, t.cluster_key, t.option_id, t.before_count, t.after_count, t.before_kind_count, t.after_kind_count) for t in audit1.traces] == [
        (1, "has_soup", "soup_yes", 6, 4, 5, 3),
        (2, "cooking_style", "rice", 4, 3, 3, 2),
        (3, "main_ingredient", "meat", 3, 2, 2, 1),
    ]

    audit3 = get_order_sheet_hard_filter_audit(db, rec3_id)
    assert audit3.outcome.outcome == OUTCOME_FIRST_LIST_LIKED
    assert audit3.is_single_candidate_liked is True
    assert audit3.is_single_candidate_rejection is False

    # 운영자 감사 확인 2: 반복되는 1종 거절 답변 조합 식별
    rej_summary = summarize_single_candidate_rejections(db, min_occurrences=2)
    assert rej_summary.total_single_candidate_rejections == 2
    assert rej_summary.unique_combination_count == 1
    assert rej_summary.repeated_combination_count == 1
    combo = rej_summary.combinations[0]
    assert combo.rejection_count == 2
    assert combo.distinct_user_count == 2
    assert combo.rejected_menu_ids == [best_beef_soup.id]
    assert combo.answer_combination == tuple(
        sorted([
            (q_map["has_soup"].question_code, "soup_yes"),
            (q_map["cooking_style"].question_code, "rice"),
            (q_map["main_ingredient"].question_code, "meat"),
        ])
    )

    # 운영자 감사 확인 3: 1종 좋아요·거절 모두 전체 만족도에 집계
    sat = summarize_overall_satisfaction_with_hard_filter_audit(db)
    assert sat.overall.shown_list_count == 4
    assert sat.overall.first_list_liked_count == 2
    assert sat.overall.list_rejected_count == 2
    assert sat.single_candidate_total_count == 3
    assert sat.single_candidate_liked_count == 1
    assert sat.single_candidate_rejected_count == 2
    assert sat.multi_candidate_total_count == 1
    assert sat.multi_candidate_liked_count == 1
    assert sat.overall_like_rate == pytest.approx(0.5)
    assert sat.overall_rejection_rate == pytest.approx(0.5)

    # 운영자 감사 확인 4: 점검 기록 누적 뒤에도 하드필터가 자동 교체되지 않고 후속 요청에서 그대로 동작
    assert tuple(settings.ORDER_SHEET_HARD_FILTER_CLUSTERS) == before_hf_clusters
    assert dict(settings.ORDER_SHEET_QUESTION_ROLE_OVERRIDES) == before_role_overrides
    q_res_after = client.get("/api/recommendations/order-sheet/questions", headers=headers1)
    assert q_res_after.status_code == 200
    sheet_after = q_res_after.json()["data"]
    rec_res_after = client.post(
        "/api/recommendations/order-sheet",
        headers=headers1,
        json={
            "order_sheet_id": sheet_after["order_sheet_id"],
            "answers": _build_answers(sheet_after["questions"], has_soup_opt="soup_yes", style_opt="rice", ing_opt="meat"),
            "top_k": 5,
        },
    )
    assert rec_res_after.status_code == 201
    assert rec_res_after.json()["data"]["candidate_count"] == 1
    assert [m["menu_id"] for m in rec_res_after.json()["data"]["recommended_menus"]] == [best_beef_soup.id]

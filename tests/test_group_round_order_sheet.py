"""Spec #77 / Ticket #85 (T08): 같이 고르기에 9문항 추천 적용 HTTP 테스트."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.auth.models  # noqa: F401
import app.menus.models  # noqa: F401
import app.rooms.models  # noqa: F401
import app.users.models  # noqa: F401
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import Base, get_db
from app.menus.models import Menu, OrderSheetQuestion, UserFoodExclusion
from app.recommend.question_pool import build_candidate_question_pool
from app.rooms.router import operations_router, router as rooms_router
from app.users.models import User


def seed_spec77_group_catalog(db: Session) -> list[Menu]:
    """모든 유효 주제가 후보를 구분할 수 있도록 다양한 메뉴와 질문 풀을 적재한다."""
    menus = [
        Menu(
            food_code="G001", name="김치찌개", base_name="김치찌개", category_large="찌개류",
            meal_type="main_meal", has_soup=True, cooking_style="soup_stew", main_ingredient="pork",
            flavor_intensity="spicy_bold", temperature="hot", heaviness="heavy",
            kcal=750.0, protein_g=22.0, carb_g=20.0, sugar_g=4.0, sodium_mg=900.0,
            origin_type="home_style", allergen_tags=["돼지고기"],
        ),
        Menu(
            food_code="G002", name="비빔밥", base_name="비빔밥", category_large="밥류",
            meal_type="main_meal", has_soup=False, cooking_style="rice", main_ingredient="veggie_tofu",
            flavor_intensity="spicy_bold", temperature="hot", heaviness="light",
            kcal=520.0, protein_g=14.0, carb_g=65.0, sugar_g=5.0, sodium_mg=480.0,
            origin_type="home_style", allergen_tags=[],
        ),
        Menu(
            food_code="G003", name="우동", base_name="우동", category_large="면류",
            meal_type="main_meal", has_soup=True, cooking_style="noodle", main_ingredient="seafood",
            flavor_intensity="mild_clean", temperature="hot", heaviness="light",
            kcal=480.0, protein_g=9.0, carb_g=70.0, sugar_g=2.0, sodium_mg=800.0,
            origin_type="restaurant", allergen_tags=["밀"],
        ),
        Menu(
            food_code="G004", name="두부샐러드", base_name="두부샐러드", category_large="샐러드",
            meal_type="main_meal", has_soup=False, cooking_style="raw_salad", main_ingredient="veggie_tofu",
            flavor_intensity="mild_clean", temperature="cold", heaviness="light",
            kcal=310.0, protein_g=8.0, carb_g=10.0, sugar_g=1.5, sodium_mg=180.0,
            origin_type="franchise", allergen_tags=[],
        ),
        Menu(
            food_code="G005", name="해물순두부찌개", base_name="순두부찌개", category_large="찌개류",
            meal_type="main_meal", has_soup=True, cooking_style="soup_stew", main_ingredient="seafood",
            flavor_intensity="spicy_bold", temperature="hot", heaviness="heavy",
            kcal=720.0, protein_g=18.0, carb_g=12.0, sugar_g=2.0, sodium_mg=760.0,
            origin_type="restaurant", allergen_tags=["해산물"],
        ),
        Menu(
            food_code="G006", name="연어포케", base_name="포케", category_large="밥류",
            meal_type="main_meal", has_soup=False, cooking_style="rice", main_ingredient="seafood",
            flavor_intensity="mild_clean", temperature="cold", heaviness="heavy",
            kcal=740.0, protein_g=25.0, carb_g=45.0, sugar_g=6.0, sodium_mg=220.0,
            origin_type="franchise", allergen_tags=["해산물"],
        ),
        Menu(
            food_code="G007", name="소고기불고기", base_name="불고기", category_large="구이류",
            meal_type="main_meal", has_soup=False, cooking_style="grill_stir", main_ingredient="beef",
            flavor_intensity="sweet_savory", temperature="hot", heaviness="heavy",
            kcal=780.0, protein_g=30.0, carb_g=18.0, sugar_g=8.0, sodium_mg=620.0,
            origin_type="home_style", allergen_tags=["쇠고기(소고기)"],
        ),
        Menu(
            food_code="G008", name="닭가슴살볶음", base_name="닭볶음", category_large="볶음류",
            meal_type="main_meal", has_soup=False, cooking_style="grill_stir", main_ingredient="poultry",
            flavor_intensity="mild_clean", temperature="hot", heaviness="light",
            kcal=430.0, protein_g=32.0, carb_g=11.0, sugar_g=1.0, sodium_mg=210.0,
            origin_type="cafeteria", allergen_tags=["닭고기"],
        ),
        Menu(
            food_code="G009", name="버섯들깨탕", base_name="버섯탕", category_large="국류",
            meal_type="main_meal", has_soup=True, cooking_style="soup_stew", main_ingredient="veggie_tofu",
            flavor_intensity="mild_clean", temperature="hot", heaviness="heavy",
            kcal=710.0, protein_g=16.0, carb_g=14.0, sugar_g=2.5, sodium_mg=230.0,
            origin_type="home_style", allergen_tags=[],
        ),
        Menu(
            food_code="G010", name="오이냉국밥", base_name="냉국밥", category_large="국류",
            meal_type="main_meal", has_soup=True, cooking_style="rice", main_ingredient="veggie_tofu",
            flavor_intensity="mild_clean", temperature="cold", heaviness="light",
            kcal=410.0, protein_g=7.0, carb_g=58.0, sugar_g=4.5, sodium_mg=490.0,
            origin_type="restaurant", allergen_tags=[],
        ),
        Menu(
            food_code="G011", name="소고기쌀국수", base_name="쌀국수", category_large="면류",
            meal_type="main_meal", has_soup=True, cooking_style="noodle", main_ingredient="beef",
            flavor_intensity="mild_clean", temperature="hot", heaviness="light",
            kcal=560.0, protein_g=24.0, carb_g=66.0, sugar_g=2.5, sodium_mg=710.0,
            origin_type="restaurant", allergen_tags=["쇠고기(소고기)"],
        ),
    ]
    db.add_all(menus)

    pool = build_candidate_question_pool()
    seen_clusters: set[str] = set()
    for idx, q in enumerate(pool):
        cluster = q["cluster_key"]
        if cluster in seen_clusters:
            continue
        seen_clusters.add(cluster)
        weight = round(1.0 - idx * 0.002, 4)
        db.add(
            OrderSheetQuestion(
                question_code=q["question_code"],
                cluster_key=cluster,
                question_text=q["question_text"],
                options_json=q["options"],
                initial_score=weight,
                importance_weight=weight,
                is_active=True,
            )
        )
    db.commit()
    return menus


def build_group_order_sheet_env(member_count: int = 3):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    db = TestingSession()
    menus = seed_spec77_group_catalog(db)
    users = [
        User(kakao_id=5000 + idx, nickname=f"그룹멤버{idx}")
        for idx in range(1, member_count + 1)
    ]
    db.add_all(users)
    db.commit()
    for u in users:
        db.refresh(u)

    state = {"current_user": users[0]}
    app = FastAPI()
    app.include_router(rooms_router)
    app.include_router(operations_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: state["current_user"]

    client = TestClient(app, headers={"Origin": settings.FRONTEND_BASE_URL.rstrip("/")})

    def as_user(user: User):
        state["current_user"] = user
        return client

    created = as_user(users[0]).post("/api/rooms", json={"max_members": 8}).json()
    room_id = created["room_id"]
    for u in users[1:]:
        joined = as_user(u).post(f"/api/rooms/{room_id}/join")
        assert joined.status_code == 201

    return client, db, users, menus, room_id, as_user


def test_single_member_wheat_allergy_omits_cooking_style_for_all_participants_and_fills_nine():
    _, db, users, _, room_id, as_user = build_group_order_sheet_env(member_count=3)
    host, wheat_member, clean_member = users

    # 참여자 1명(wheat_member)만 밀 알레르기를 보유
    db.add(
        UserFoodExclusion(
            user_id=wheat_member.id,
            exclusion_type="ALLERGY",
            keyword="밀",
            catalog_code="wheat",
        )
    )
    db.commit()

    start_res = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert start_res.status_code == 200
    start_body = start_res.json()

    viewer_qs = start_body["viewer_state"]["assigned_questions"]
    assert len(viewer_qs) == 9
    assert all("initial_score" in q and "importance_weight" in q for q in viewer_qs)

    for participant in (host, wheat_member, clean_member):
        res = as_user(participant).get(f"/api/rooms/{room_id}/rounds/current/questions")
        assert res.status_code == 200
        body = res.json()
        assert body["total_count"] == 9
        assert body["exploitation_count"] == 9
        assert body["exploration_count"] == 0
        assert all("info_gain_score" in q and "ml_weight" in q for q in body["questions"])

        clusters = [q["cluster_key"] for q in body["questions"]]
        assert len(clusters) == 9
        assert len(set(clusters)) == 9
        assert "cooking_style" not in clusters
        assert "meal_type" not in clusters
        # 생략된 cooking_style 자리는 남은 유효 주제(sugar_level 또는 temperature)로 채워진다.
        assert set(clusters) & {"sugar_level", "temperature"}


def test_multiple_allergies_across_different_members_combine_for_round_wide_omission():
    _, db, users, _, room_id, as_user = build_group_order_sheet_env(member_count=3)
    host, member_b, member_c = users

    # 서로 다른 참여자의 알레르기·섭취불가가 합쳐져 밀 + 육류 전체가 제외된다.
    db.add_all([
        UserFoodExclusion(user_id=host.id, exclusion_type="ALLERGY", keyword="밀", catalog_code="wheat"),
        UserFoodExclusion(user_id=member_b.id, exclusion_type="ALLERGY", keyword="돼지고기", catalog_code="pork"),
        UserFoodExclusion(user_id=member_b.id, exclusion_type="ALLERGY", keyword="쇠고기(소고기)", catalog_code="beef"),
        UserFoodExclusion(user_id=member_c.id, exclusion_type="UNABLE", keyword="닭·가금류", catalog_code="poultry"),
    ])
    db.commit()

    start_res = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert start_res.status_code == 200

    for participant in (host, member_b, member_c):
        q_res = as_user(participant).get(f"/api/rooms/{room_id}/rounds/current/questions")
        assert q_res.status_code == 200
        body = q_res.json()
        clusters = [q["cluster_key"] for q in body["questions"]]
        assert body["total_count"] == 9
        assert len(clusters) == 9
        assert len(set(clusters)) == 9
        assert "cooking_style" not in clusters
        assert "main_ingredient" not in clusters
        assert "meal_type" not in clusters
        assert {"sugar_level", "temperature"}.issubset(set(clusters))


def test_round_preserves_q8_four_options_from_legacy_rows_and_uses_server_question_count(monkeypatch):
    _, db, users, _, room_id, as_user = build_group_order_sheet_env(member_count=2)
    host, member = users

    # 기존 3선택지(rice_noodle/other_style/any) DB 행도 그룹 라운드 발급 시 4선택지로 정규화되어야 한다.
    q8_row = db.query(OrderSheetQuestion).filter_by(cluster_key="cooking_style").one()
    q8_row.options_json = [
        {
            "option_id": "rice_noodle",
            "label": "예",
            "rules": [{"field": "cooking_style", "op": "in", "value": ["rice", "noodle"]}],
        },
        {
            "option_id": "other_style",
            "label": "아니요",
            "rules": [{"field": "cooking_style", "op": "not_in", "value": ["rice", "noodle"]}],
        },
        {"option_id": "any", "label": "상관없어요", "rules": []},
    ]
    db.commit()

    start_res = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert start_res.status_code == 200

    viewer_q8 = next(
        q for q in start_res.json()["viewer_state"]["assigned_questions"]
        if q["cluster_key"] == "cooking_style"
    )
    assert [opt["option_id"] for opt in viewer_q8["options"]] == ["rice", "noodle", "other_style", "any"]
    assert [opt["label"] for opt in viewer_q8["options"]] == ["밥", "면", "그 외", "상관없어요"]

    for alias_path in (
        f"/api/rooms/{room_id}/rounds/current/questions",
        f"/api/rooms/{room_id}/order-sheet/questions",
        f"/api/rooms/{room_id}/questions",
    ):
        res = as_user(member).get(alias_path)
        assert res.status_code == 200
        q8 = next(q for q in res.json()["questions"] if q["cluster_key"] == "cooking_style")
        assert [opt["option_id"] for opt in q8["options"]] == ["rice", "noodle", "other_style", "any"]
        assert [opt["label"] for opt in q8["options"]] == ["밥", "면", "그 외", "상관없어요"]

    # 서버 설정 ORDER_SHEET_QUESTION_COUNT 변경이 그룹 라운드 발급·제출 검증에 그대로 반영된다.
    _, _, users2, _, room_id2, as_user2 = build_group_order_sheet_env(member_count=2)
    host2, member2 = users2
    monkeypatch.setattr(settings, "ORDER_SHEET_QUESTION_COUNT", 7)

    started7 = as_user2(host2).post(f"/api/rooms/{room_id2}/rounds/start")
    assert started7.status_code == 200
    assert len(started7.json()["viewer_state"]["assigned_questions"]) == 7

    host2_qs = as_user2(host2).get(f"/api/rooms/{room_id2}/rounds/current/questions").json()["questions"]
    assert len(host2_qs) == 7

    # 6개만 제출하면 400으로 거부된다.
    too_few = as_user2(host2).post(
        f"/api/rooms/{room_id2}/rounds/current/order-sheet",
        json={"answers": [{"question_id": q["id"], "option_id": "any"} for q in host2_qs[:6]]},
    )
    assert too_few.status_code == 400

    # 배정된 7개 전체 제출은 성공한다.
    ok_submit = as_user2(host2).post(
        f"/api/rooms/{room_id2}/rounds/current/order-sheet",
        json={"answers": [{"question_id": q["id"], "option_id": "any"} for q in host2_qs]},
    )
    assert ok_submit.status_code == 200
    assert ok_submit.json()["submitted"] is True


def _answers_from_map(questions: list[dict], option_by_cluster: dict[str, str]) -> list[dict]:
    return [
        {
            "question_id": q["id"],
            "option_id": option_by_cluster.get(q["cluster_key"], "any"),
        }
        for q in questions
    ]


def test_shared_exclusions_and_spec77_hard_filter_score_pipeline_work_across_all_aliases_into_voting():
    _, db, users, _, room_id, as_user = build_group_order_sheet_env(member_count=3)
    host, m1, m2 = users

    # 참여자 m2만 돼지고기 알레르기 보유 -> G001 김치찌개는 라운드 전원에게 공통 제외된다.
    db.add(
        UserFoodExclusion(
            user_id=m2.id,
            exclusion_type="ALLERGY",
            keyword="돼지고기",
            catalog_code="pork",
        )
    )
    db.commit()

    start_res = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert start_res.status_code == 200

    # 기존 질문 조회 경로 및 별칭 3종 모두 동작하고 동일 계약 반환
    host_q_res = as_user(host).get(f"/api/rooms/{room_id}/rounds/current/questions")
    m1_q_res = as_user(m1).get(f"/api/rooms/{room_id}/order-sheet/questions")
    m2_q_res = as_user(m2).get(f"/api/rooms/{room_id}/questions")
    assert host_q_res.status_code == m1_q_res.status_code == m2_q_res.status_code == 200
    host_qs = host_q_res.json()["questions"]
    m1_qs = m1_q_res.json()["questions"]
    m2_qs = m2_q_res.json()["questions"]
    assert len(host_qs) == len(m1_qs) == len(m2_qs) == 9

    # host: 국물O + 그 외(soup_stew) + 고기 상관없음 + 매운맛/든든함/짠맛/외식/저탄수/고단백
    # 공통 제외로 G001 김치찌개(돼지고기)가 제외되므로 G005 해물순두부찌개가 1위!
    host_answers = _answers_from_map(
        host_qs,
        {
            "has_soup": "soup_yes",
            "cooking_style": "other_style",
            "main_ingredient": "any",
            "flavor_intensity": "spicy_bold",
            "heaviness": "filling",
            "sodium_level": "savory_sodium",
            "origin_type": "eating_out",
            "carb_level": "low_carb",
            "protein_level": "high_protein",
        },
    )
    # m1: 국물X + 밥(Q8 4선택지!) + 비고기 + 매운맛/가벼움/집밥 -> G002 비빔밥이 1위!
    m1_answers = _answers_from_map(
        m1_qs,
        {
            "has_soup": "soup_no",
            "cooking_style": "rice",
            "main_ingredient": "non_meat",
            "flavor_intensity": "spicy_bold",
            "heaviness": "light",
            "origin_type": "home_style",
        },
    )
    # m2: 국물O + 면(Q8 4선택지!) + 고기 -> 하드필터 엄격 적용으로 G011 소고기쌀국수 1종만 남아 1위!
    m2_answers = _answers_from_map(
        m2_qs,
        {
            "has_soup": "soup_yes",
            "cooking_style": "noodle",
            "main_ingredient": "meat",
        },
    )

    # 기존 제출 경로 및 별칭 3종을 각각 사용해 제출
    s_host = as_user(host).post(
        f"/api/rooms/{room_id}/rounds/current/order-sheet",
        json={"answers": host_answers},
    )
    assert s_host.status_code == 200
    assert s_host.json()["round_status"] == "ANSWERING"

    s_m1 = as_user(m1).post(
        f"/api/rooms/{room_id}/order-sheet/submit",
        json={"answers": m1_answers},
    )
    assert s_m1.status_code == 200
    assert s_m1.json()["round_status"] == "ANSWERING"

    s_m2 = as_user(m2).post(
        f"/api/rooms/{room_id}/order-sheet",
        json={"answers": m2_answers},
    )
    assert s_m2.status_code == 200
    round_state = s_m2.json()["common_state"]["current_round"]
    assert round_state["status"] == "VOTING"
    assert round_state["all_orders_submitted"] is True

    candidates = round_state["candidates"]
    cand_by_code = {c["food_code"]: c for c in candidates}
    assert set(cand_by_code.keys()) == {"G005", "G002", "G011"}
    assert "G001" not in cand_by_code
    assert cand_by_code["G005"]["proposer_user_ids"] == [host.id]
    assert cand_by_code["G002"]["proposer_user_ids"] == [m1.id]
    assert cand_by_code["G011"]["proposer_user_ids"] == [m2.id]

    # 투표 완료 -> RESULT 전환 확인
    target_cid = cand_by_code["G002"]["candidate_id"]
    for u in (host, m1, m2):
        vote_res = as_user(u).post(
            f"/api/rooms/{room_id}/rounds/current/votes",
            json={"candidate_ids": [target_cid], "vote_round_no": 1},
        )
        assert vote_res.status_code == 200
    final_detail = as_user(host).get(f"/api/rooms/{room_id}").json()
    assert final_detail["current_round"]["status"] == "RESULT"
    assert [w["food_code"] for w in final_detail["current_round"]["winners"]] == ["G002"]


def test_group_round_missing_attribute_gets_zero_score_and_identical_top_menu_becomes_uncontested_result():
    _, db, users, _, room_id, as_user = build_group_order_sheet_env(member_count=2)
    host, member = users

    # 국물X + 밥 + 고기(beef) 후보 2종 추가:
    # A(food_code 더 앞섬)는 carb_g=None(미확인 -> 0점), B는 carb_g=10.0(저탄수 일치 -> +1점)
    db.add_all([
        Menu(
            food_code="A001", name="미확인소고기덮밥", base_name="미확인덮밥", category_large="밥류",
            meal_type="main_meal", has_soup=False, cooking_style="rice", main_ingredient="beef",
            flavor_intensity="mild_clean", temperature="hot", heaviness="light",
            kcal=500.0, protein_g=20.0, carb_g=None, sugar_g=2.0, sodium_mg=300.0,
            origin_type="restaurant", allergen_tags=[],
        ),
        Menu(
            food_code="Z999", name="저탄수소고기덮밥", base_name="저탄수덮밥", category_large="밥류",
            meal_type="main_meal", has_soup=False, cooking_style="rice", main_ingredient="beef",
            flavor_intensity="mild_clean", temperature="hot", heaviness="light",
            kcal=500.0, protein_g=20.0, carb_g=10.0, sugar_g=2.0, sodium_mg=300.0,
            origin_type="restaurant", allergen_tags=[],
        ),
    ])
    db.commit()

    assert as_user(host).post(f"/api/rooms/{room_id}/rounds/start").status_code == 200

    for u in (host, member):
        qs = as_user(u).get(f"/api/rooms/{room_id}/rounds/current/questions").json()["questions"]
        answers = _answers_from_map(
            qs,
            {
                "has_soup": "soup_no",
                "cooking_style": "rice",
                "main_ingredient": "meat",
                "carb_level": "low_carb",
            },
        )
        res = as_user(u).post(
            f"/api/rooms/{room_id}/rounds/current/order-sheet",
            json={"answers": answers},
        )
        assert res.status_code == 200

    detail = as_user(host).get(f"/api/rooms/{room_id}").json()
    current_round = detail["current_round"]
    assert current_round["status"] == "RESULT"
    assert current_round["is_uncontested"] is True
    assert [w["food_code"] for w in current_round["winners"]] == ["Z999"]
    assert set(current_round["winners"][0]["proposer_user_ids"]) == {host.id, member.id}


def test_room_stays_waiting_and_can_start_again_when_question_pool_was_insufficient():
    _, db, users, _, room_id, as_user = build_group_order_sheet_env(member_count=2)
    host, member = users

    # 5개 주제만 활성으로 남겨 9문항 구성 불가 상황을 만든다.
    all_qs = db.query(OrderSheetQuestion).order_by(OrderSheetQuestion.id.asc()).all()
    for q in all_qs[5:]:
        q.is_active = False
    db.commit()

    res = as_user(host).post(f"/api/rooms/{room_id}/rounds/start")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "INSUFFICIENT_QUESTION_POOL"
    detail = as_user(host).get(f"/api/rooms/{room_id}").json()
    assert detail["status"] == "WAITING"
    questions_url = f"/api/rooms/{room_id}/rounds/current/questions"
    assert as_user(member).get(questions_url).json()["detail"]["code"] == "ROUND_NOT_ACTIVE"

    for q in all_qs[5:]:
        q.is_active = True
    db.commit()
    assert as_user(host).post(f"/api/rooms/{room_id}/rounds/start").status_code == 200
    assert as_user(member).get(questions_url).json()["total_count"] == 9


def test_sparse_shared_menu_pool_uses_informative_questions_before_filling_nine():
    _, db, users, original_menus, room_id, as_user = build_group_order_sheet_env(member_count=2)
    host, member = users
    for menu in original_menus:
        db.delete(menu)
    db.flush()
    menus = [
        Menu(
            food_code="T1", name="김치찌개", base_name="김치찌개", category_large="찌개",
            meal_type="main_meal", has_soup=True, cooking_style="soup_stew", main_ingredient="pork",
            flavor_intensity="spicy_bold", temperature="hot", heaviness="heavy",
            kcal=650.0, protein_g=25.0, carb_g=35.0, sugar_g=4.0, sodium_mg=800.0,
            origin_type="home_style", allergen_tags=[],
        ),
        Menu(
            food_code="T2", name="연어초밥", base_name="초밥", category_large="일식",
            meal_type="main_meal", has_soup=False, cooking_style="rice", main_ingredient="seafood",
            flavor_intensity="mild_clean", temperature="cold", heaviness="light",
            kcal=520.0, protein_g=22.0, carb_g=60.0, sugar_g=6.0, sodium_mg=450.0,
            origin_type="restaurant", allergen_tags=[],
        ),
        Menu(
            food_code="T3", name="소고기국밥", base_name="국밥", category_large="국",
            meal_type="main_meal", has_soup=True, cooking_style="rice", main_ingredient="beef",
            flavor_intensity="mild_clean", temperature="hot", heaviness="light",
            kcal=580.0, protein_g=24.0, carb_g=62.0, sugar_g=2.5, sodium_mg=720.0,
            origin_type="restaurant", allergen_tags=[],
        ),
    ]
    db.add_all(menus)
    db.add(UserFoodExclusion(user_id=member.id, exclusion_type="ALLERGY", keyword="밀", catalog_code="wheat"))
    db.commit()

    assert as_user(host).post(f"/api/rooms/{room_id}/rounds/start").status_code == 200
    for user in (host, member):
        response = as_user(user).get(f"/api/rooms/{room_id}/rounds/current/questions")
        assert response.status_code == 200
        clusters = [q["cluster_key"] for q in response.json()["questions"]]
        assert len(clusters) == len(set(clusters)) == 9
        assert {"cooking_style", "meal_type"}.isdisjoint(clusters)
        assert {
            "heaviness",
            "has_soup",
            "flavor_intensity",
            "origin_type",
            "main_ingredient",
            "sugar_level",
            "temperature",
        }.issubset(clusters[:7])


def test_start_alias_omits_topics_that_become_degenerate_on_surviving_shared_menus():
    _, db, users, _, room_id, as_user = build_group_order_sheet_env(member_count=2)
    host, member = users

    # 국물 없는 메뉴(G002 비빔밥, G004 두부샐러드, G006 연어포케, G007 불고기, G008 닭볶음)를
    # 참여자 제외 조건으로 모두 제거하면 남은 공통 후보(G001/G003/G005/G009/G010/G011)는 모두 국물 있음(has_soup=True)이 된다.
    for kw in ("비빔밥", "두부샐러드", "포케", "불고기", "닭볶음"):
        db.add(UserFoodExclusion(user_id=member.id, exclusion_type="DISLIKE", keyword=kw))
    db.commit()

    # 기존 start 별칭(/api/rooms/{room_id}/start) 사용
    started = as_user(host).post(f"/api/rooms/{room_id}/start")
    assert started.status_code == 200

    for participant in (host, member):
        res = as_user(participant).get(f"/api/rooms/{room_id}/rounds/current/questions")
        assert res.status_code == 200
        clusters = [q["cluster_key"] for q in res.json()["questions"]]
        assert len(clusters) == 9
        assert len(set(clusters)) == 9
        assert "has_soup" not in clusters
        assert set(clusters) & {"sugar_level", "temperature"}

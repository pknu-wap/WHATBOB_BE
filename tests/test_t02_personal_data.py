from __future__ import annotations

import json
import math
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.menus.models import (
    Menu,
    OrderSheetQuestion,
    Recommendation,
    RecommendationFeedback,
    RecommendationFeedbackState,
    RecommendationItemSnapshot,
    UserFoodExclusion,
    UserMeal,
)
from app.recommend.common import inspect_menu_catalog_coverage, matches_unable_code
from app.recommend.personal_model import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    collect_training_samples_from_db,
    compute_artifact_checksum,
    evaluate_personal_adjustment,
    load_active_personal_model,
    publish_personal_model_artifact,
    train_personal_logistic_model,
    build_user_signal_summary,
)
from app.recommend.question_pool import build_candidate_question_pool
from app.recommend.variety import select_diverse_menus
from app.users import repository as user_repository
from app.users.exclusion_mapping import apply_legacy_exclusion_mapping, inspect_legacy_exclusions
from app.users.models import User
from tests.auth_helpers import session_token


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    artifact_path = tmp_path / "models" / "personal_logistic_active.json"
    monkeypatch.setattr("app.core.config.settings.PERSONAL_MODEL_ARTIFACT_PATH", str(artifact_path))
    overrides = app.dependency_overrides.copy()
    try:
        with Session(engine) as db:
            user = User(nickname="기본유저", nickname_key="기본유저".encode("utf-8"))
            other = User(nickname="타인유저", nickname_key="타인유저".encode("utf-8"))
            menus = [
                Menu(
                    food_code="M-PORK",
                    name="제육볶음",
                    base_name="제육볶음",
                    category_large="볶음류",
                    meal_type="main_meal",
                    has_soup=False,
                    cooking_style="grill_stir",
                    main_ingredient="pork",
                    allergen_tags=["돼지고기", "대두(콩)"],
                ),
                Menu(
                    food_code="M-BEEF",
                    name="소불고기",
                    base_name="불고기",
                    category_large="구이류",
                    meal_type="main_meal",
                    has_soup=False,
                    cooking_style="grill_stir",
                    main_ingredient="beef",
                    allergen_tags=["쇠고기(소고기)"],
                ),
                Menu(
                    food_code="M-CHICKEN",
                    name="삼계탕",
                    base_name="삼계탕",
                    category_large="국 및 탕류",
                    meal_type="main_meal",
                    has_soup=True,
                    cooking_style="soup_stew",
                    main_ingredient="chicken",
                    allergen_tags=["닭고기"],
                ),
                Menu(
                    food_code="M-SEAFOOD",
                    name="새우볶음밥",
                    base_name="볶음밥",
                    category_large="밥류",
                    meal_type="main_meal",
                    has_soup=False,
                    cooking_style="rice",
                    main_ingredient="seafood",
                    allergen_tags=["새우", "알류(달걀)"],
                ),
                Menu(
                    food_code="M-SALAD",
                    name="채소샐러드",
                    base_name="샐러드",
                    category_large="생채·무침류",
                    meal_type="main_meal",
                    has_soup=False,
                    cooking_style="raw_salad",
                    main_ingredient="veggie_tofu",
                    allergen_tags=[],
                ),
            ]
            source_q = next(q for q in build_candidate_question_pool() if q["cluster_key"] == "has_soup")
            q_soup = OrderSheetQuestion(
                question_code=source_q["question_code"],
                cluster_key="has_soup",
                question_text=source_q["question_text"],
                options_json=source_q["options"],
                initial_score=0.8,
                importance_weight=0.8,
            )
            db.add_all([user, other, *menus, q_soup])
            db.commit()

            app.dependency_overrides[get_db] = lambda: db
            yield SimpleNamespace(
                client=TestClient(app),
                db=db,
                user=user,
                other=other,
                menus={m.food_code: m for m in menus},
                q_soup=q_soup,
                headers={"Authorization": f"Bearer {session_token(db, user)}"},
                other_headers={"Authorization": f"Bearer {session_token(db, other)}"},
                artifact_path=artifact_path,
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        engine.dispose()


def test_t02_auth_required_and_request_body_user_id_forbidden(env):
    # 1) 비인증 호출은 모두 401
    assert env.client.get("/api/recommendations/random").status_code == 401
    assert env.client.get("/api/recommendations/order-sheet/questions").status_code == 401
    assert env.client.post(
        "/api/recommendations/order-sheet",
        json={"answers": [{"question_id": env.q_soup.id, "option_id": "any"}]},
    ).status_code == 401
    assert env.client.post(
        "/api/recommendations/1/feedback",
        json={"is_satisfied": True},
    ).status_code == 401

    # 2) 공개 카탈로그는 비인증 200
    r_all = env.client.get("/api/recommendations/allergens")
    r_un = env.client.get("/api/recommendations/unable-ingredients")
    assert r_all.status_code == 200 and r_all.json()["data"]["total_count"] == 19
    assert r_un.status_code == 200 and r_un.json()["data"]["total_count"] == 8

    # 3) 요청 본문에 user_id 포함 시 422 거절
    assert env.client.post(
        "/api/recommendations/order-sheet",
        headers=env.headers,
        json={
            "user_id": env.other.id,
            "answers": [{"question_id": env.q_soup.id, "option_id": "any"}],
        },
    ).status_code == 422
    assert env.client.get(
        "/api/recommendations/random",
        headers=env.headers,
        params={"user_id": env.other.id},
    ).status_code == 422

    # 4) 중복 question_id 제출 시 400 거절
    dup_resp = env.client.post(
        "/api/recommendations/order-sheet",
        headers=env.headers,
        json={
            "answers": [
                {"question_id": env.q_soup.id, "option_id": "soup_yes"},
                {"question_id": env.q_soup.id, "option_id": "soup_no"},
            ]
        },
    )
    assert dup_resp.status_code == 400


def test_t02_catalog_exclusions_crud_legacy_protection_and_m2a_rules(env, monkeypatch):
    # 1) 초기 조회
    res = env.client.get("/api/users/me/allergies", headers=env.headers)
    assert res.status_code == 200
    assert res.json()["data"]["editable"] is True
    assert res.json()["data"]["codes"] == []

    # 2) 다른 타입/타인 데이터 격리 및 전체 교체
    env.client.put("/api/users/me/dislikes", headers=env.headers, json={"keywords": ["소불고기"]})
    env.client.put("/api/users/me/allergies", headers=env.other_headers, json={"codes": ["beef"]})

    put_allergy = env.client.put(
        "/api/users/me/allergies",
        headers=env.headers,
        json={"codes": ["shrimp", "pork", "shrimp"]},
    )
    assert put_allergy.status_code == 200
    assert put_allergy.json()["data"]["codes"] == ["shrimp", "pork"]

    put_unable = env.client.put(
        "/api/users/me/unables",
        headers=env.headers,
        json={"codes": ["poultry"]},
    )
    assert put_unable.status_code == 200
    assert put_unable.json()["data"]["codes"] == ["poultry"]

    # 3) 미지 코드 포함 시 400 거절 및 기존 목록 유지
    bad_put = env.client.put(
        "/api/users/me/allergies",
        headers=env.headers,
        json={"codes": ["egg", "unknown_code"]},
    )
    assert bad_put.status_code == 400
    assert env.client.get("/api/users/me/allergies", headers=env.headers).json()["data"]["codes"] == ["shrimp", "pork"]

    # 4) 중간 DB 오류 시 롤백되어 이전 목록 유지
    def _fail_add(*args, **kwargs):
        raise SQLAlchemyError("simulated failure")

    monkeypatch.setattr(user_repository, "add_catalog_exclusion", _fail_add)
    with pytest.raises(SQLAlchemyError):
        env.client.put("/api/users/me/allergies", headers=env.headers, json={"codes": ["egg"]})
    monkeypatch.undo()
    assert env.client.get("/api/users/me/allergies", headers=env.headers).json()["data"]["codes"] == ["shrimp", "pork"]

    # 5) M2A: veggie_tofu 채소샐러드는 soybean 섭취불가에 의해 잘못 제외되지 않아야 함
    env.client.put("/api/users/me/unables", headers=env.headers, json={"codes": ["poultry", "soybean"]})
    assert matches_unable_code(env.menus["M-SALAD"], "soybean") is False
    rec_resp = env.client.post(
        "/api/recommendations/order-sheet",
        headers=env.headers,
        json={"answers": [{"question_id": env.q_soup.id, "option_id": "any"}]},
    )
    assert rec_resp.status_code == 201
    rec_names = [m["base_name"] for m in rec_resp.json()["data"]["recommended_menus"]]
    assert rec_names == ["샐러드"]

    # 6) 레거시 미매핑 행 존재 시 editable=False 및 PUT 409 차단 + 매핑 스크립트 검증
    env.db.add_all(
        [
            UserFoodExclusion(user_id=env.user.id, exclusion_type="ALLERGY", keyword="갑각류모호"),
            UserFoodExclusion(user_id=env.user.id, exclusion_type="ALLERGY", keyword="우유"),
        ]
    )
    env.db.commit()

    get_legacy = env.client.get("/api/users/me/allergies", headers=env.headers).json()["data"]
    assert get_legacy["editable"] is False
    assert {item["keyword"] for item in get_legacy["legacy_items"]} == {"갑각류모호", "우유"}
    blocked = env.client.put("/api/users/me/allergies", headers=env.headers, json={"codes": ["egg"]})
    assert blocked.status_code == 409

    check_before = inspect_legacy_exclusions(env.db)
    assert check_before["mappable"] == 1
    assert check_before["unmapped_preserved"] == 1
    map_res = apply_legacy_exclusion_mapping(env.db)
    assert map_res["mapped_count"] == 1 and map_res["preserved_unmapped_count"] == 1
    # 재실행 시 멱등성 보장
    map_res_again = apply_legacy_exclusion_mapping(env.db)
    assert map_res_again["mapped_count"] == 0 and map_res_again["deduplicated_count"] == 0

    coverage = inspect_menu_catalog_coverage(env.db.query(Menu).all())
    assert coverage["total_menus"] == 5
    assert coverage["allergen_code_counts"]["pork"] >= 1
    assert coverage["unable_code_counts"]["poultry"] >= 1


def test_t02_merged_dislikes_over_100_editable_and_meal_idempotency_and_delete(env):
    # 병합으로 105개 비선호 항목이 저장된 사용자도 102개로 축소 편집 가능해야 함
    for idx in range(105):
        env.db.add(UserFoodExclusion(user_id=env.user.id, exclusion_type="DISLIKE", keyword=f"비선호{idx}"))
    env.db.commit()

    reduced = [f"비선호{idx}" for idx in range(102)]
    res = env.client.put("/api/users/me/dislikes", headers=env.headers, json={"keywords": reduced})
    assert res.status_code == 200
    assert res.json()["data"]["total_count"] == 102

    # 식사 기록 Idempotency-Key 재시도 및 다른 본문 409, 삭제 204/404 검증
    m_id = env.menus["M-SALAD"].id
    headers_idem = {**env.headers, "Idempotency-Key": "meal-op-001"}
    r1 = env.client.post("/api/users/me/meals", headers=headers_idem, json={"menu_id": m_id})
    r2 = env.client.post("/api/users/me/meals", headers=headers_idem, json={"menu_id": m_id})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["data"]["id"] == r2.json()["data"]["id"]
    assert env.db.query(UserMeal).filter(UserMeal.user_id == env.user.id).count() == 1

    r_conflict = env.client.post(
        "/api/users/me/meals",
        headers=headers_idem,
        json={"menu_id": env.menus["M-BEEF"].id},
    )
    assert r_conflict.status_code == 409

    meal_id = r1.json()["data"]["id"]
    assert env.client.delete(f"/api/users/me/meals/{meal_id}", headers=env.other_headers).status_code == 404
    assert env.client.delete(f"/api/users/me/meals/{meal_id}", headers=env.headers).status_code == 204
    assert env.client.delete(f"/api/users/me/meals/{meal_id}", headers=env.headers).status_code == 404


def test_t02_feedback_idempotency_effective_single_vote_and_modification(env):
    rec_resp = env.client.post(
        "/api/recommendations/order-sheet",
        headers=env.headers,
        json={"answers": [{"question_id": env.q_soup.id, "option_id": "soup_yes"}], "top_k": 3},
    )
    assert rec_resp.status_code == 201
    rec_id = rec_resp.json()["data"]["recommendation_id"]
    chicken_id = env.menus["M-CHICKEN"].id

    # 타인 피드백 거절 (404)
    assert env.client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers=env.other_headers,
        json={"selected_menu_id": chicken_id, "is_satisfied": True},
    ).status_code == 404

    # 1) 같은 Idempotency-Key 재전송 -> 1이력, 전역 통계 중복 없음
    fb_headers = {**env.headers, "Idempotency-Key": "fb-key-1"}
    f1 = env.client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers=fb_headers,
        json={"selected_menu_id": chicken_id, "is_satisfied": True},
    )
    f2 = env.client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers=fb_headers,
        json={"selected_menu_id": chicken_id, "is_satisfied": True},
    )
    assert f1.status_code == 200 and f2.status_code == 200
    assert f1.json()["data"]["feedback_id"] == f2.json()["data"]["feedback_id"]
    assert env.db.query(RecommendationFeedback).filter_by(recommendation_id=rec_id).count() == 1
    assert env.db.query(RecommendationFeedbackState).filter_by(recommendation_id=rec_id).count() == 1
    env.db.refresh(env.q_soup)
    assert env.q_soup.positive_count == 1
    w_after_first = f1.json()["data"]["updated_weights"][env.q_soup.question_code]

    # 2) 같은 Idempotency-Key에 다른 payload -> 409
    f_conflict = env.client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers=fb_headers,
        json={"selected_menu_id": chicken_id, "is_satisfied": False},
    )
    assert f_conflict.status_code == 409

    # 3) 같은 값(True)을 새 작업 키로 전송 -> 이력은 남지만 유효 상태/전역 집계 불변
    f_same_val = env.client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers={**env.headers, "Idempotency-Key": "fb-key-2"},
        json={"selected_menu_id": chicken_id, "is_satisfied": True},
    )
    assert f_same_val.status_code == 200
    env.db.refresh(env.q_soup)
    assert env.q_soup.positive_count == 1
    assert f_same_val.json()["data"]["updated_weights"][env.q_soup.question_code] == w_after_first
    assert env.db.query(RecommendationFeedback).filter_by(recommendation_id=rec_id).count() == 2
    assert env.db.query(RecommendationFeedbackState).filter_by(recommendation_id=rec_id).count() == 1

    # 4) 좋아요 -> 싫어요 수정 -> 이력 보존, 유효 1표는 False로 갱신, positive_count 감소
    f_mod = env.client.post(
        f"/api/recommendations/{rec_id}/feedback",
        headers={**env.headers, "Idempotency-Key": "fb-key-3"},
        json={"selected_menu_id": chicken_id, "is_satisfied": False},
    )
    assert f_mod.status_code == 200
    env.db.refresh(env.q_soup)
    assert env.q_soup.positive_count == 0
    state = env.db.query(RecommendationFeedbackState).filter_by(recommendation_id=rec_id).one()
    assert state.is_satisfied is False
    assert env.db.query(RecommendationFeedback).filter_by(recommendation_id=rec_id).count() == 3


def test_t02_personal_model_training_validation_fallback_and_weighted_draw_distribution(env):
    # 1) Cold start (n=0): r=0, c=0, w=1
    signals_empty = build_user_signal_summary(env.db, env.user.id)
    res_cold = evaluate_personal_adjustment(
        rule_score=1.0,
        scale=1.0,
        menu_key="불고기",
        main_ingredient="beef",
        signals=signals_empty,
        model=None,
    )
    assert res_cold.reliability == 0.0 and res_cold.personal_adjustment == 0.0 and res_cold.draw_weight == 1.0

    # 2) 추천 스냅샷 저장 및 미평가 추천 제외, 시간순 학습/검증 파이프라인 확인
    for idx in range(14):
        rec_r = env.client.post(
            "/api/recommendations/order-sheet",
            headers=env.headers,
            json={"answers": [{"question_id": env.q_soup.id, "option_id": "any"}], "top_k": 5},
        )
        assert rec_r.status_code == 201
        r_id = rec_r.json()["data"]["recommendation_id"]
        # 마지막 2건은 미평가로 남겨 학습 데이터에서 제외되는지 확인
        if idx < 12:
            is_pos = (idx % 2 == 0)
            target_menu_id = env.menus["M-BEEF"].id if is_pos else env.menus["M-PORK"].id
            fb_r = env.client.post(
                f"/api/recommendations/{r_id}/feedback",
                headers=env.headers,
                json={"selected_menu_id": target_menu_id, "is_satisfied": is_pos},
            )
            assert fb_r.status_code == 200

    assert env.db.query(RecommendationItemSnapshot).count() == 14 * 5
    samples = collect_training_samples_from_db(env.db)
    assert len(samples) == 12

    artifact_dict = train_personal_logistic_model(samples, version="v1-test", min_samples=10)
    assert artifact_dict["validation_passed"] is True
    published_path = publish_personal_model_artifact(artifact_dict, active_path=env.artifact_path)
    loaded = load_active_personal_model(published_path)
    assert loaded is not None and loaded.version == "v1-test"

    # 3) 손상/체크섬 불일치/스키마 버전 불일치/NaN 계수 아티팩트 거절 및 폴백 확인
    bad_checksum = {**artifact_dict, "checksum": "0" * 64}
    bad_path = env.artifact_path.parent / "bad.json"
    bad_path.write_text(json.dumps(bad_checksum), encoding="utf-8")
    assert load_active_personal_model(bad_path) is None

    bad_schema = {**artifact_dict, "feature_schema_version": 999}
    bad_schema["checksum"] = compute_artifact_checksum(bad_schema)
    bad_path.write_text(json.dumps(bad_schema), encoding="utf-8")
    assert load_active_personal_model(bad_path) is None

    bad_nan = {**artifact_dict, "intercept": float("nan")}
    bad_path.write_text(json.dumps(bad_nan, allow_nan=True), encoding="utf-8")
    assert load_active_personal_model(bad_path) is None

    # 4) 보정 상한(|c| <= 0.05 * scale, 1.0 <= w <= 1.5) 및 M1/M3A 가중 추첨 분포 통계 검증
    custom_artifact = {
        "version": "v1-dist",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "intercept": 0.0,
        "weights": [0.0, 3.5, 0.0, 0.0, 0.0, 0.0],
        "means": [0.0 for _ in FEATURE_NAMES],
        "stds": [1.0 for _ in FEATURE_NAMES],
        "metrics": {"val_log_loss": 0.3, "val_brier_score": 0.1},
        "validation_passed": True,
    }
    custom_artifact["checksum"] = compute_artifact_checksum(custom_artifact)
    publish_personal_model_artifact(custom_artifact, active_path=env.artifact_path)

    # 최근 추천 노출 패널티(최근 3건)의 영향을 배제하기 위해 별도 사용자 생성 후 불고기 긍정 피드백 부여
    dist_user = User(nickname="분포검증유저", nickname_key="분포검증유저".encode("utf-8"))
    env.db.add(dist_user)
    env.db.flush()
    for _ in range(10):
        old_rec = Recommendation(user_id=dist_user.id, recommended_menu_ids=[])
        env.db.add(old_rec)
        env.db.flush()
        env.db.add(
            RecommendationFeedbackState(
                recommendation_id=old_rec.id,
                user_id=dist_user.id,
                selected_menu_id=env.menus["M-BEEF"].id,
                menu_key="불고기",
                menu_base_name="불고기",
                menu_main_ingredient="beef",
                is_satisfied=True,
            )
        )
    # 피자 메뉴를 30개 추가해도(M3A) 피자 그룹의 추첨 가중치가 부풀지 않는지 함께 확인
    pizza_menus = [
        Menu(
            food_code=f"M-PIZZA-{i}",
            name=f"피자_{i}",
            base_name="피자",
            category_large="양식",
            meal_type="main_meal",
            has_soup=False,
        )
        for i in range(30)
    ]
    env.db.add_all(pizza_menus)
    env.db.flush()
    for _ in range(10):
        old_rec = Recommendation(user_id=dist_user.id, recommended_menu_ids=[])
        env.db.add(old_rec)
        env.db.flush()
        env.db.add(
            RecommendationFeedbackState(
                recommendation_id=old_rec.id,
                user_id=dist_user.id,
                selected_menu_id=pizza_menus[0].id,
                menu_key="피자",
                menu_base_name="피자",
                menu_main_ingredient="other",
                is_satisfied=False,
            )
        )
    env.db.commit()

    candidates = [(1.0, ["q"], env.menus["M-BEEF"])] + [(1.0, ["q"], pm) for pm in pizza_menus]

    # (a) 모델 없을 때: 불고기(1개) vs 피자(30개) 두 그룹이 거의 50:50 균등 선택됨
    rng_uniform = random.Random(20261113)
    counts_no_model: Counter[str] = Counter()
    for _ in range(1000):
        picked = select_diverse_menus(
            env.db,
            candidates,
            dist_user.id,
            top_k=1,
            total_weight=1.0,
            rng=rng_uniform,
            artifact_path=env.artifact_path.parent / "nonexistent.json",
        )
        counts_no_model[picked[0][2].base_name] += 1
    assert 440 <= counts_no_model["불고기"] <= 560
    assert 440 <= counts_no_model["피자"] <= 560

    # (b) 모델 활성화 시: 불고기(w ~ 1.40)가 피자(w ~ 1.00)보다 유의미하게 높은 빈도로 선택됨
    rng_weighted = random.Random(20261113)
    counts_with_model: Counter[str] = Counter()
    for _ in range(1000):
        picked = select_diverse_menus(
            env.db,
            candidates,
            dist_user.id,
            top_k=1,
            total_weight=1.0,
            rng=rng_weighted,
            artifact_path=env.artifact_path,
        )
        counts_with_model[picked[0][2].base_name] += 1
    assert counts_with_model["불고기"] >= counts_no_model["불고기"] + 50

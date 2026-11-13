"""답변 적합도를 유지하면서 메뉴 종류·최근 노출·다중 신호 개인화 학습 모델을 반영한다."""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.menus.models import Menu, Recommendation
from app.recommend.personal_model import (
    PersonalScoreResult,
    build_user_signal_summary,
    evaluate_personal_adjustment,
    load_active_personal_model,
    normalize_menu_key,
)

RECENT_RECOMMENDATION_COUNT = 3
NEAR_SCORE_RATIO = 0.10
RECENT_EXPOSURE_PENALTY_RATIO = 0.10


def menu_group(menu: Menu) -> str:
    return normalize_menu_key(menu.base_name or menu.name)


@dataclass(frozen=True)
class SelectedCandidateDetail:
    rule_score: float
    matched_codes: list[str]
    menu: Menu
    final_score: float
    exposure_penalty: float
    personal_result: PersonalScoreResult


def _weighted_pick(rng: random.Random, items: list, weights: list[float]):
    if len(items) == 1:
        return items[0]
    if all(abs(w - weights[0]) <= 1e-9 for w in weights):
        return rng.choice(items)
    total = sum(weights)
    if total <= 0.0:
        return rng.choice(items)
    threshold = rng.random() * total
    acc = 0.0
    for item, w in zip(items, weights):
        acc += w
        if threshold <= acc:
            return item
    return items[-1]


def select_diverse_menus_with_snapshots(
    db: Session,
    candidates: list[tuple[float, list[str], Menu]],
    user_id: int | None,
    top_k: int,
    total_weight: float,
    *,
    rng: random.Random | None = None,
    artifact_path: str | Path | None = None,
) -> list[SelectedCandidateDetail]:
    scale = max(float(total_weight), 1.0)
    recent_groups: Counter[str] = Counter()
    if user_id is not None:
        recent = (
            db.query(Recommendation)
            .filter(Recommendation.user_id == user_id)
            .order_by(Recommendation.id.desc())
            .limit(RECENT_RECOMMENDATION_COUNT)
            .all()
        )
        menu_map = {item[2].id: item[2] for item in candidates}
        for record in recent:
            recent_groups.update(
                {
                    menu_group(menu_map[mid])
                    for mid in (record.recommended_menu_ids or [])
                    if mid in menu_map
                }
            )

    signals = build_user_signal_summary(db, user_id)
    model = load_active_personal_model(artifact_path)

    groups: dict[str, list[SelectedCandidateDetail]] = {}
    for rule_score, matched_codes, menu in candidates:
        group = menu_group(menu)
        exposure_penalty = scale * RECENT_EXPOSURE_PENALTY_RATIO * recent_groups[group]
        ps = evaluate_personal_adjustment(
            rule_score=rule_score,
            scale=scale,
            menu_key=group,
            main_ingredient=(menu.main_ingredient or "other").strip() or "other",
            signals=signals,
            model=model,
        )
        final_score = round(
            float(rule_score) - exposure_penalty + ps.recent_meal_adjustment + ps.personal_adjustment,
            6,
        )
        detail = SelectedCandidateDetail(
            rule_score=round(float(rule_score), 4),
            matched_codes=list(matched_codes),
            menu=menu,
            final_score=final_score,
            exposure_penalty=round(exposure_penalty, 6),
            personal_result=ps,
        )
        groups.setdefault(group, []).append(detail)

    selected: list[SelectedCandidateDetail] = []
    random_gen = rng or random.SystemRandom()

    while groups and len(selected) < top_k:
        # 각 그룹의 대표 메뉴는 final_score 최댓값, 동점 시 menu.id 오름차순으로 결정한다 (M3A).
        representatives: dict[str, SelectedCandidateDetail] = {
            g: max(rows, key=lambda d: (d.final_score, -(d.menu.id or 0)))
            for g, rows in groups.items()
        }
        best = max(rep.final_score for rep in representatives.values())
        eligible_groups = sorted(
            [
                g
                for g, rep in representatives.items()
                if rep.final_score >= best - scale * NEAR_SCORE_RATIO
            ]
        )
        group_weights = [representatives[g].personal_result.draw_weight for g in eligible_groups]
        chosen_group = _weighted_pick(random_gen, eligible_groups, group_weights)

        rows = groups.pop(chosen_group)
        group_best = representatives[chosen_group].final_score
        close_rows = sorted(
            [d for d in rows if d.final_score >= group_best - scale * NEAR_SCORE_RATIO],
            key=lambda d: (d.menu.id or 0),
        )
        item_weights = [d.personal_result.draw_weight for d in close_rows]
        chosen_detail = _weighted_pick(random_gen, close_rows, item_weights)
        selected.append(chosen_detail)

    return selected


def select_diverse_menus(
    db: Session,
    candidates: list[tuple[float, list[str], Menu]],
    user_id: int | None,
    top_k: int,
    total_weight: float,
    *,
    rng: random.Random | None = None,
    artifact_path: str | Path | None = None,
) -> list[tuple[float, list[str], Menu]]:
    details = select_diverse_menus_with_snapshots(
        db=db,
        candidates=candidates,
        user_id=user_id,
        top_k=top_k,
        total_weight=total_weight,
        rng=rng,
        artifact_path=artifact_path,
    )
    return [(d.rule_score, d.matched_codes, d.menu) for d in details]

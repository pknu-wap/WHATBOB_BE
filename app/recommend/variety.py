"""답변 적합도를 유지하면서 메뉴 종류·최근 노출·사용자 피드백을 반영한다."""

import random
from collections import Counter

from sqlalchemy.orm import Session

from app.menus.models import Menu, Recommendation, RecommendationFeedback

RECENT_RECOMMENDATION_COUNT = 3
NEAR_SCORE_RATIO = 0.10
RECENT_EXPOSURE_PENALTY_RATIO = 0.10
DISLIKE_PENALTY_RATIO = 0.35


def menu_group(menu: Menu) -> str:
    base = (menu.base_name or menu.name).strip()
    # 원본 데이터에서 같은 종류가 다른 기본 이름으로 저장된 경우를 합친다.
    return {"햄버거": "버거", "돈까스": "돈가스", "리조또": "리소토"}.get(base, base)


def select_diverse_menus(
    db: Session,
    candidates: list[tuple[float, list[str], Menu]],
    user_id: int | None,
    top_k: int,
    total_weight: float,
) -> list[tuple[float, list[str], Menu]]:
    scale = max(total_weight, 1.0)
    recent_groups: Counter[str] = Counter()
    disliked_ids: set[int] = set()
    if user_id is not None:
        recent = (db.query(Recommendation)
                  .filter(Recommendation.user_id == user_id)
                  .order_by(Recommendation.id.desc())
                  .limit(RECENT_RECOMMENDATION_COUNT).all())
        menu_map = {item[2].id: item[2] for item in candidates}
        for record in recent:
            recent_groups.update({menu_group(menu_map[mid])
                                  for mid in record.recommended_menu_ids or [] if mid in menu_map})
        # 가장 최근의 평가를 사용한다. 이후 좋아요를 누르면 이전 싫어요는 해제된다.
        feedbacks = (db.query(RecommendationFeedback)
                     .filter(RecommendationFeedback.user_id == user_id)
                     .order_by(RecommendationFeedback.id.desc()).all())
        seen_ids: set[int] = set()
        for feedback in feedbacks:
            mid = feedback.selected_menu_id
            if mid is not None and mid not in seen_ids:
                seen_ids.add(mid)
                if not feedback.is_satisfied:
                    disliked_ids.add(mid)

    groups: dict[str, list[tuple[float, tuple[float, list[str], Menu]]]] = {}
    for candidate in candidates:
        score, _, menu = candidate
        group = menu_group(menu)
        penalty = scale * RECENT_EXPOSURE_PENALTY_RATIO * recent_groups[group]
        if menu.id in disliked_ids:
            penalty += scale * DISLIKE_PENALTY_RATIO
        groups.setdefault(group, []).append((score - penalty, candidate))

    selected = []
    rng = random.SystemRandom()
    while groups and len(selected) < top_k:
        best_scores = {group: max(score for score, _ in rows) for group, rows in groups.items()}
        best = max(best_scores.values())
        # 종류부터 추첨해서 피자 상품이 수천 개여도 종류 하나의 확률만 갖게 한다.
        eligible = [group for group, score in best_scores.items()
                    if score >= best - scale * NEAR_SCORE_RATIO]
        group = rng.choice(eligible)
        rows = groups.pop(group)
        close_rows = [candidate for score, candidate in rows
                      if score >= best_scores[group] - scale * NEAR_SCORE_RATIO]
        selected.append(rng.choice(close_rows))
    return selected

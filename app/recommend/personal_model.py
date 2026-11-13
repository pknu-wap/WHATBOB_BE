from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.menus.models import (
    Menu,
    Recommendation,
    RecommendationFeedbackState,
    RecommendationItemSnapshot,
    UserMeal,
)

logger = logging.getLogger(__name__)

FEATURE_SCHEMA_VERSION = 1
HALF_LIFE_DAYS = 90.0
DECAY_LAMBDA = math.log(2.0) / HALF_LIFE_DAYS
SHRINKAGE_K = 5.0
PERSONAL_ALPHA = 0.05
PERSONAL_BETA = 0.50
RECENT_MEAL_COOLDOWN_DAYS = 3.0
RECENT_MEAL_COOLDOWN_RATIO = 0.08

FEATURE_NAMES: tuple[str, ...] = (
    "rule_score_norm",
    "menu_net_feedback",
    "ing_net_feedback",
    "menu_meal_long_decayed",
    "ing_meal_decayed",
    "recent_3d_same_menu",
)


def _to_utc_naive(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def decay_weight(event_time: datetime | None, reference_time: datetime | None = None) -> float:
    ref = _to_utc_naive(reference_time)
    ev = _to_utc_naive(event_time)
    delta_days = max(0.0, (ref - ev).total_seconds() / 86400.0)
    return math.exp(-DECAY_LAMBDA * delta_days)


def normalize_menu_key(base_name_or_name: str | None) -> str:
    base = (base_name_or_name or "").strip()
    return {"햄버거": "버거", "돈까스": "돈가스", "리조또": "리소토"}.get(base, base)


@dataclass(frozen=True)
class UserSignalSummary:
    menu_net_feedback: dict[str, float]
    ing_net_feedback: dict[str, float]
    menu_meal_long_decayed: dict[str, float]
    ing_meal_decayed: dict[str, float]
    recent_3d_menu_counts: dict[str, float]
    effective_sample_size: float
    total_feedback_events: int
    total_meal_events: int


@dataclass(frozen=True)
class PersonalModelArtifact:
    version: str
    feature_schema_version: int
    feature_names: tuple[str, ...]
    intercept: float
    weights: tuple[float, ...]
    means: tuple[float, ...]
    stds: tuple[float, ...]
    metrics: dict[str, float]
    validation_passed: bool
    checksum: str


@dataclass(frozen=True)
class PersonalScoreResult:
    probability: float
    reliability: float
    personal_adjustment: float
    recent_meal_adjustment: float
    draw_weight: float
    features: dict[str, float]


def build_user_signal_summary(
    db: Session,
    user_id: int | None,
    reference_time: datetime | None = None,
) -> UserSignalSummary:
    if user_id is None:
        return UserSignalSummary(
            menu_net_feedback={},
            ing_net_feedback={},
            menu_meal_long_decayed={},
            ing_meal_decayed={},
            recent_3d_menu_counts={},
            effective_sample_size=0.0,
            total_feedback_events=0,
            total_meal_events=0,
        )

    ref = _to_utc_naive(reference_time)
    states = (
        db.query(RecommendationFeedbackState)
        .filter(RecommendationFeedbackState.user_id == user_id)
        .all()
    )
    menu_net_fb: dict[str, float] = {}
    ing_net_fb: dict[str, float] = {}
    total_fb_decayed = 0.0
    fb_count = 0

    for st in states:
        ev_time = _to_utc_naive(st.updated_at or st.created_at)
        if reference_time is not None and ev_time > ref:
            continue
        w = decay_weight(ev_time, ref)
        sign = 1.0 if st.is_satisfied else -1.0
        mkey = normalize_menu_key(st.menu_key or st.menu_base_name)
        ing = (st.menu_main_ingredient or "other").strip() or "other"
        if mkey:
            menu_net_fb[mkey] = menu_net_fb.get(mkey, 0.0) + sign * w
        ing_net_fb[ing] = ing_net_fb.get(ing, 0.0) + sign * w
        total_fb_decayed += w
        fb_count += 1

    meal_rows = (
        db.query(UserMeal, Menu)
        .outerjoin(Menu, UserMeal.menu_id == Menu.id)
        .filter(UserMeal.user_id == user_id)
        .all()
    )
    menu_meal_long: dict[str, float] = {}
    ing_meal: dict[str, float] = {}
    recent_3d: dict[str, float] = {}
    total_meal_decayed = 0.0
    meal_count = 0

    for meal, menu in meal_rows:
        ev_time = _to_utc_naive(meal.eaten_at or meal.created_at)
        if reference_time is not None and ev_time > ref:
            continue
        delta_days = max(0.0, (ref - ev_time).total_seconds() / 86400.0)
        w = math.exp(-DECAY_LAMBDA * delta_days)
        base_str = menu.base_name if menu is not None else meal.menu_name
        mkey = normalize_menu_key(base_str)
        ing = (menu.main_ingredient if menu is not None else "other") or "other"

        if delta_days <= RECENT_MEAL_COOLDOWN_DAYS:
            if mkey:
                recent_3d[mkey] = recent_3d.get(mkey, 0.0) + 1.0
        else:
            if mkey:
                menu_meal_long[mkey] = menu_meal_long.get(mkey, 0.0) + w
        ing_meal[ing] = ing_meal.get(ing, 0.0) + w
        total_meal_decayed += w
        meal_count += 1

    eff_n = total_fb_decayed + 0.5 * total_meal_decayed
    return UserSignalSummary(
        menu_net_feedback=menu_net_fb,
        ing_net_feedback=ing_net_fb,
        menu_meal_long_decayed=menu_meal_long,
        ing_meal_decayed=ing_meal,
        recent_3d_menu_counts=recent_3d,
        effective_sample_size=round(eff_n, 6),
        total_feedback_events=fb_count,
        total_meal_events=meal_count,
    )


def extract_candidate_features(
    *,
    rule_score: float,
    scale: float,
    menu_key: str,
    main_ingredient: str,
    signals: UserSignalSummary,
) -> dict[str, float]:
    norm_scale = max(float(scale), 1.0)
    return {
        "rule_score_norm": round(float(rule_score) / norm_scale, 6),
        "menu_net_feedback": round(float(signals.menu_net_feedback.get(menu_key, 0.0)), 6),
        "ing_net_feedback": round(float(signals.ing_net_feedback.get(main_ingredient, 0.0)), 6),
        "menu_meal_long_decayed": round(float(signals.menu_meal_long_decayed.get(menu_key, 0.0)), 6),
        "ing_meal_decayed": round(float(signals.ing_meal_decayed.get(main_ingredient, 0.0)), 6),
        "recent_3d_same_menu": round(float(signals.recent_3d_menu_counts.get(menu_key, 0.0)), 6),
    }


def compute_artifact_checksum(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "checksum"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def parse_and_validate_artifact(raw: dict[str, Any]) -> PersonalModelArtifact | None:
    try:
        if not isinstance(raw, dict):
            return None
        if raw.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
            logger.warning("개인화 모델 feature_schema_version 불일치로 폴백합니다.")
            return None
        if raw.get("validation_passed") is not True:
            logger.warning("검증을 통과하지 못한 개인화 모델 아티팩트입니다.")
            return None
        expected_checksum = compute_artifact_checksum(raw)
        if raw.get("checksum") != expected_checksum:
            logger.warning("개인화 모델 아티팩트 체크섬 불일치로 폴백합니다.")
            return None

        feature_names = tuple(raw.get("feature_names") or ())
        if feature_names != FEATURE_NAMES:
            logger.warning("개인화 모델 feature_names 불일치로 폴백합니다.")
            return None

        intercept = raw.get("intercept")
        weights = raw.get("weights")
        means = raw.get("means")
        stds = raw.get("stds")
        if not _is_finite_number(intercept):
            return None
        if not all(isinstance(seq, list) and len(seq) == len(FEATURE_NAMES) for seq in (weights, means, stds)):
            return None
        if not all(_is_finite_number(x) for seq in (weights, means, stds) for x in seq):
            return None
        if any(float(s) <= 0.0 for s in stds):
            return None

        metrics = raw.get("metrics") or {}
        if not isinstance(metrics, dict) or not all(_is_finite_number(v) for v in metrics.values()):
            return None

        return PersonalModelArtifact(
            version=str(raw.get("version") or "v1"),
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            feature_names=feature_names,
            intercept=float(intercept),
            weights=tuple(float(w) for w in weights),
            means=tuple(float(m) for m in means),
            stds=tuple(float(s) for s in stds),
            metrics={str(k): float(v) for k, v in metrics.items()},
            validation_passed=True,
            checksum=str(raw["checksum"]),
        )
    except Exception as exc:
        logger.warning(f"개인화 모델 아티팩트 검증 실패: {exc}")
        return None


def load_active_personal_model(artifact_path: str | Path | None = None) -> PersonalModelArtifact | None:
    target = Path(artifact_path or settings.PERSONAL_MODEL_ARTIFACT_PATH)
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"개인화 모델 아티팩트 JSON 파싱 실패: {exc}")
        return None
    return parse_and_validate_artifact(raw)


def _sigmoid(z: float) -> float:
    if z >= 0.0:
        ez = math.exp(-min(z, 50.0))
        return 1.0 / (1.0 + ez)
    ez = math.exp(max(z, -50.0))
    return ez / (1.0 + ez)


def evaluate_personal_adjustment(
    *,
    rule_score: float,
    scale: float,
    menu_key: str,
    main_ingredient: str,
    signals: UserSignalSummary,
    model: PersonalModelArtifact | None,
) -> PersonalScoreResult:
    norm_scale = max(float(scale), 1.0)
    features = extract_candidate_features(
        rule_score=rule_score,
        scale=norm_scale,
        menu_key=menu_key,
        main_ingredient=main_ingredient,
        signals=signals,
    )
    eff_n = max(0.0, float(signals.effective_sample_size))
    reliability = eff_n / (eff_n + SHRINKAGE_K) if eff_n > 0.0 else 0.0

    if model is None or reliability <= 0.0:
        return PersonalScoreResult(
            probability=0.5,
            reliability=round(reliability, 6),
            personal_adjustment=0.0,
            recent_meal_adjustment=0.0,
            draw_weight=1.0,
            features=features,
        )

    logit = model.intercept
    for idx, fname in enumerate(FEATURE_NAMES):
        val = float(features[fname])
        std_val = (val - model.means[idx]) / model.stds[idx]
        logit += model.weights[idx] * std_val

    prob = _sigmoid(logit)
    # c = scale * alpha * r * (2p - 1), bounded within [-0.05 * scale, +0.05 * scale]
    personal_adj = norm_scale * PERSONAL_ALPHA * reliability * (2.0 * prob - 1.0)
    # w = 1 + beta * r * p, bounded within [1.0, 1.5]
    draw_w = 1.0 + PERSONAL_BETA * reliability * prob

    return PersonalScoreResult(
        probability=round(prob, 6),
        reliability=round(reliability, 6),
        personal_adjustment=round(personal_adj, 6),
        recent_meal_adjustment=0.0,
        draw_weight=round(max(1.0, min(1.0 + PERSONAL_BETA, draw_w)), 6),
        features=features,
    )


def train_personal_logistic_model(
    samples: list[tuple[dict[str, float], int]],
    *,
    version: str = "v1",
    l2_penalty: float = 0.1,
    learning_rate: float = 0.15,
    max_epochs: int = 400,
    tolerance: float = 1e-6,
    min_samples: int = 10,
) -> dict[str, Any]:
    """
    시간순 정렬된 (features_dict, label_0_or_1) 표본으로 L2 정규화 로지스틱 회귀를 학습하고,
    검증 지표 및 체크섬을 포함한 아티팩트 사전을 반환한다.
    같은 추천(recommendation_id)의 표본이 학습/검증 분할에 흩어지지 않도록 그룹 경계를 지킨다.
    """
    if len(samples) < min_samples:
        raise ValueError(f"학습 표본 수가 부족합니다: {len(samples)} < {min_samples}")

    split_idx = max(1, int(len(samples) * 0.8))
    if split_idx >= len(samples):
        split_idx = len(samples) - 1
    boundary_rec_id = samples[split_idx - 1][0].get("_recommendation_id")
    if boundary_rec_id is not None:
        while split_idx < len(samples) - 1 and samples[split_idx][0].get("_recommendation_id") == boundary_rec_id:
            split_idx += 1
    train_samples = samples[:split_idx]
    val_samples = samples[split_idx:]

    train_labels = [int(y) for _, y in train_samples]
    pos_count = sum(1 for y in train_labels if y == 1)
    neg_count = len(train_labels) - pos_count
    if pos_count == 0 or neg_count == 0:
        raise ValueError("학습 데이터에 긍정(1)과 부정(0) 표본이 모두 존재해야 합니다.")

    # Train-only standardization
    means: list[float] = []
    stds: list[float] = []
    for fname in FEATURE_NAMES:
        vals = [float(x.get(fname, 0.0)) for x, _ in train_samples]
        mean_v = sum(vals) / len(vals)
        var_v = sum((v - mean_v) ** 2 for v in vals) / len(vals)
        std_v = math.sqrt(var_v) if var_v > 1e-9 else 1.0
        means.append(round(mean_v, 8))
        stds.append(round(std_v, 8))

    def _standardize(feat: dict[str, float]) -> list[float]:
        return [
            (float(feat.get(fname, 0.0)) - means[i]) / stds[i]
            for i, fname in enumerate(FEATURE_NAMES)
        ]

    x_train = [_standardize(x) for x, _ in train_samples]
    y_train = [float(y) for _, y in train_samples]

    weights = [0.0 for _ in FEATURE_NAMES]
    base_p = min(0.99, max(0.01, pos_count / len(train_samples)))
    intercept = math.log(base_p / (1.0 - base_p))

    converged = False
    n_train = float(len(x_train))
    for _ in range(max_epochs):
        grad_b = 0.0
        grad_w = [0.0 for _ in FEATURE_NAMES]
        for xi, yi in zip(x_train, y_train):
            logit = intercept + sum(w * xv for w, xv in zip(weights, xi))
            pred = _sigmoid(logit)
            err = pred - yi
            grad_b += err
            for j in range(len(FEATURE_NAMES)):
                grad_w[j] += err * xi[j]

        grad_b /= n_train
        max_step = abs(learning_rate * grad_b)
        intercept -= learning_rate * grad_b
        for j in range(len(FEATURE_NAMES)):
            g = (grad_w[j] / n_train) + l2_penalty * weights[j]
            step = learning_rate * g
            if abs(step) > max_step:
                max_step = abs(step)
            weights[j] -= step

        if max_step < tolerance:
            converged = True
            break

    if not all(math.isfinite(v) for v in [intercept, *weights]):
        raise ValueError("학습 계수가 수렴하지 않았습니다 (NaN/Inf 발생).")

    def _eval_metrics(sub_samples: list[tuple[dict[str, float], int]], p_base: float) -> tuple[float, float, float, float]:
        loss_sum = 0.0
        brier_sum = 0.0
        base_loss_sum = 0.0
        base_brier_sum = 0.0
        eps = 1e-12
        for feat, y_int in sub_samples:
            z_vec = _standardize(feat)
            p = min(1.0 - eps, max(eps, _sigmoid(intercept + sum(w * zv for w, zv in zip(weights, z_vec)))))
            pb = min(1.0 - eps, max(eps, p_base))
            y = float(y_int)
            loss_sum += -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))
            brier_sum += (p - y) ** 2
            base_loss_sum += -(y * math.log(pb) + (1.0 - y) * math.log(1.0 - pb))
            base_brier_sum += (pb - y) ** 2
        m = float(len(sub_samples))
        return loss_sum / m, brier_sum / m, base_loss_sum / m, base_brier_sum / m

    val_log_loss, val_brier, base_log_loss, base_brier = _eval_metrics(val_samples, base_p)
    validation_passed = (
        math.isfinite(val_log_loss)
        and math.isfinite(val_brier)
        and val_log_loss <= base_log_loss + 0.05
        and val_brier <= base_brier + 0.02
    )

    payload: dict[str, Any] = {
        "version": version,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "intercept": round(intercept, 8),
        "weights": [round(w, 8) for w in weights],
        "means": means,
        "stds": stds,
        "metrics": {
            "train_count": float(len(train_samples)),
            "val_count": float(len(val_samples)),
            "converged": 1.0 if converged else 0.0,
            "val_log_loss": round(val_log_loss, 6),
            "val_brier_score": round(val_brier, 6),
            "baseline_log_loss": round(base_log_loss, 6),
            "baseline_brier_score": round(base_brier, 6),
        },
        "validation_passed": bool(validation_passed),
    }
    payload["checksum"] = compute_artifact_checksum(payload)
    return payload


def publish_personal_model_artifact(
    payload: dict[str, Any],
    *,
    active_path: str | Path | None = None,
) -> Path:
    validated = parse_and_validate_artifact(payload)
    if validated is None:
        raise ValueError("검증을 통과하지 못한 개인화 모델 아티팩트는 배포할 수 없습니다.")

    target = Path(active_path or settings.PERSONAL_MODEL_ARTIFACT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    versioned_path = target.parent / f"personal_logistic_{validated.version}.json"
    tmp_path = target.parent / f".tmp_{validated.version}_{os.getpid()}.json"

    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    versioned_path.write_text(serialized, encoding="utf-8")
    tmp_path.write_text(serialized, encoding="utf-8")
    os.replace(tmp_path, target)
    return target


def collect_training_samples_from_db(db: Session) -> list[tuple[dict[str, float], int]]:
    """
    Recommendation(created_at, id) 시간순으로 RecommendationFeedbackState와 RecommendationItemSnapshot을 조인해
    학습 표본 목록을 구성한다.
    """
    rows = (
        db.query(RecommendationFeedbackState, Recommendation, RecommendationItemSnapshot)
        .join(Recommendation, RecommendationFeedbackState.recommendation_id == Recommendation.id)
        .outerjoin(
            RecommendationItemSnapshot,
            (RecommendationItemSnapshot.recommendation_id == RecommendationFeedbackState.recommendation_id)
            & (RecommendationItemSnapshot.menu_key == RecommendationFeedbackState.menu_key),
        )
        .order_by(Recommendation.created_at.asc(), Recommendation.id.asc(), RecommendationFeedbackState.id.asc())
        .all()
    )
    samples: list[tuple[dict[str, float], int]] = []
    for state, rec, snapshot in rows:
        if (
            snapshot is None
            or snapshot.feature_schema_version != FEATURE_SCHEMA_VERSION
            or not snapshot.features_json
        ):
            continue
        feat = {**dict(snapshot.features_json), "_recommendation_id": int(rec.id)}
        label = 1 if state.is_satisfied else 0
        samples.append((feat, label))
    return samples

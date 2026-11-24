from dataclasses import replace

from app.recommend.experiment_metrics import ArmMetrics, decide_common_experiment, decide_personal_experiment


def test_personal_decision_waits_for_both_arms_and_accepts_exact_twenty_point_gain():
    control = ArmMetrics(20, 19, 6, 9, 15, 4, 1)
    variant = ArmMetrics(21, 20, 9, 6, 15, 5, 1)

    assert decide_personal_experiment(control, replace(variant, explicit_count=14)) is None
    assert decide_personal_experiment(control, variant) is True
    assert decide_personal_experiment(control, replace(variant, liked_count=8)) is False
    assert decide_personal_experiment(control, replace(variant, rejected_count=10)) is False


def test_common_decision_requires_significance_and_both_guardrails():
    control = ArmMetrics(100, 100, 40, 35, 75, 25, 0)
    variant = ArmMetrics(100, 100, 54, 30, 84, 16, 0)

    assert decide_common_experiment(control, replace(variant, submitted_count=99)) is None
    assert decide_common_experiment(control, replace(variant, liked_count=50)) is False
    assert decide_common_experiment(control, variant) is True
    assert decide_common_experiment(control, replace(variant, rejected_count=36)) is False
    assert decide_common_experiment(control, replace(variant, shown_count=98, zero_candidates_count=2)) is False
    assert decide_common_experiment(control, replace(variant, shown_count=99, zero_candidates_count=1)) is True
    assert decide_common_experiment(control, replace(variant, shown_count=0, liked_count=0)) is False

"""Decision rules for score-question experiments."""

from dataclasses import dataclass
from fractions import Fraction
from math import comb
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from app.recommend.feedback import FirstListReactionSummary


@dataclass(frozen=True)
class ArmMetrics:
    submitted_count: int
    shown_count: int
    liked_count: int
    rejected_count: int
    explicit_count: int
    no_response_count: int
    zero_candidates_count: int

    @classmethod
    def from_summary(cls, summary: "FirstListReactionSummary") -> "ArmMetrics":
        return cls(
            submitted_count=summary.total_recommendations,
            shown_count=summary.shown_list_count,
            liked_count=summary.first_list_liked_count,
            rejected_count=sum(outcome.is_all_menus_disliked for outcome in summary.outcomes),
            explicit_count=sum(
                outcome.is_first_list_liked or outcome.is_all_menus_disliked
                for outcome in summary.outcomes
            ),
            no_response_count=summary.no_response_count,
            zero_candidates_count=summary.zero_candidates_count,
        )


def decide_personal_experiment(control: ArmMetrics, variant: ArmMetrics) -> bool | None:
    """Return adoption, retention, or None while an arm lacks enough explicit feedback."""
    if min(control.explicit_count, variant.explicit_count) < settings.ORDER_SHEET_PERSONAL_MIN_EXPLICIT:
        return None

    like_gain = Fraction(variant.liked_count, variant.explicit_count) - Fraction(
        control.liked_count, control.explicit_count
    )
    return (
        like_gain >= Fraction(str(settings.ORDER_SHEET_PERSONAL_MIN_LIKE_GAIN))
        and Fraction(variant.rejected_count, variant.explicit_count)
        <= Fraction(control.rejected_count, control.explicit_count)
    )


def _fisher_like_p_value(control: ArmMetrics, variant: ArmMetrics) -> Fraction:
    """Conditional probability of at least this many variant likes with fixed margins."""
    total_shown = control.shown_count + variant.shown_count
    total_liked = control.liked_count + variant.liked_count
    total_other = total_shown - total_liked
    arm_size = variant.shown_count
    tail_weight = sum(
        comb(total_liked, likes) * comb(total_other, arm_size - likes)
        for likes in range(variant.liked_count, min(arm_size, total_liked) + 1)
    )
    return Fraction(tail_weight, comb(total_shown, arm_size))


def decide_common_experiment(
    control: ArmMetrics, variant: ArmMetrics, *, submissions_per_arm: int | None = None,
) -> bool | None:
    """Compare the fixed cohorts once each arm has its required submissions."""
    required = settings.ORDER_SHEET_COMMON_SUBMISSIONS_PER_ARM if submissions_per_arm is None else submissions_per_arm
    if min(control.submitted_count, variant.submitted_count) < required:
        return None
    if not control.shown_count or not variant.shown_count:
        return False

    like_gain = Fraction(variant.liked_count, variant.shown_count) - Fraction(
        control.liked_count, control.shown_count
    )
    zero_gain = Fraction(variant.zero_candidates_count, variant.submitted_count) - Fraction(
        control.zero_candidates_count, control.submitted_count
    )
    return (
        like_gain >= Fraction(str(settings.ORDER_SHEET_COMMON_MIN_LIKE_GAIN))
        and Fraction(variant.rejected_count, variant.shown_count)
        <= Fraction(control.rejected_count, control.shown_count)
        and zero_gain <= Fraction(str(settings.ORDER_SHEET_COMMON_MAX_ZERO_GAIN))
        and _fisher_like_p_value(control, variant) <= Fraction(str(settings.ORDER_SHEET_COMMON_MAX_P_VALUE))
    )

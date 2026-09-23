"""提醒引擎的边界与去重行为。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from astrbot_plugin_game_activity.services.reminder_service import (
    ActivityNotifyState,
    ReminderPolicy,
    apply_plan,
    collect_plans,
    current_tier,
    evaluate_activity,
    is_more_urgent,
    matches_filters,
    normalize_keywords,
    normalize_thresholds,
    reason_label,
)
from astrbot_plugin_game_activity.sources.base import Activity, GAME_TIMEZONE

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=GAME_TIMEZONE)


def make_activity(
    *,
    activity_id: str = "a1",
    name: str = "测试活动",
    start: timedelta | None = timedelta(days=-1),
    end: timedelta | None = timedelta(days=20),
    description: str = "",
    tags: tuple[str, ...] = (),
) -> Activity:
    return Activity(
        game_id="sr",
        activity_id=activity_id,
        name=name,
        start_time=None if start is None else NOW + start,
        end_time=None if end is None else NOW + end,
        description=description,
        tags=tags,
    )


POLICY = ReminderPolicy(thresholds=(7, 3, 1), notify_new=True, lookahead_days=45)


# ---- 档位计算 ----


@pytest.mark.parametrize(
    ("remaining_days", "expected"),
    [
        (30, ""),
        (8, ""),
        (7, "d7"),
        (6.9, "d7"),
        (3, "d3"),
        (2, "d3"),
        (1, "d1"),
        (0.5, "d1"),
        (0, ""),
        (-1, ""),
    ],
)
def test_current_tier_boundaries(remaining_days, expected):
    end = NOW + timedelta(days=remaining_days)
    assert current_tier(end, NOW, (7, 3, 1)) == expected


def test_current_tier_without_thresholds_is_empty():
    assert current_tier(NOW + timedelta(hours=1), NOW, ()) == ""


def test_current_tier_without_end_time_is_empty():
    assert current_tier(None, NOW, (7, 3, 1)) == ""


def test_is_more_urgent():
    assert is_more_urgent("d1", "d3")
    assert is_more_urgent("d7", "")
    assert not is_more_urgent("d7", "d3")
    assert not is_more_urgent("d3", "d3")
    assert not is_more_urgent("", "d3")


def test_reason_labels():
    assert reason_label("new") == "新活动"
    assert reason_label("d1") == "剩余不足24小时"
    assert reason_label("d3") == "剩余不足3天"


# ---- 入参解析 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7,3,1", (7, 3, 1)),
        ("7，3，1", (7, 3, 1)),
        ([1, 3, 7, 3], (7, 3, 1)),
        ("关闭", ()),
        ("", ()),
        ([], ()),
        ("5天,1天", (5, 1)),
        ("abc", ()),
        ("-3,0,2", (2,)),
        (None, (7, 3, 1)),
    ],
)
def test_normalize_thresholds(raw, expected):
    assert normalize_thresholds(raw) == expected


def test_normalize_keywords_deduplicates_and_trims():
    assert normalize_keywords(" 双倍 , ,双倍,签到 ") == ["双倍", "签到"]
    assert normalize_keywords(None) == []
    assert normalize_keywords(["a", "a", ""]) == ["a"]


# ---- 决策 ----


def test_new_activity_within_lookahead_is_reported():
    plan = evaluate_activity(make_activity(), NOW, None, POLICY)
    assert plan is not None
    assert plan.reasons == ("new",)
    assert plan.tier == ""
    assert plan.is_new


def test_new_activity_beyond_lookahead_is_skipped():
    activity = make_activity(start=timedelta(days=60), end=timedelta(days=90))
    assert evaluate_activity(activity, NOW, None, POLICY) is None


def test_already_notified_new_is_not_repeated():
    state = ActivityNotifyState(new_done=True)
    assert evaluate_activity(make_activity(), NOW, state, POLICY) is None


def test_ended_activity_is_ignored():
    activity = make_activity(start=timedelta(days=-10), end=timedelta(days=-1))
    assert evaluate_activity(activity, NOW, None, POLICY) is None


def test_countdown_fires_once_per_tier():
    activity = make_activity(end=timedelta(days=2))

    plan = evaluate_activity(activity, NOW, None, POLICY)
    assert plan is not None
    assert plan.tier == "d3"
    assert plan.reasons == ("new", "d3")

    state = ActivityNotifyState(new_done=True)
    apply_plan(state, plan)
    assert state.tier == "d3"

    # 同一档位不重复
    assert evaluate_activity(activity, NOW, state, POLICY) is None

    # 进入更紧急的档位时再次提醒
    later = NOW + timedelta(days=1, hours=12)
    urgent = evaluate_activity(activity, later, state, POLICY)
    assert urgent is not None
    assert urgent.tier == "d1"


def test_less_urgent_tier_never_regresses():
    state = ActivityNotifyState(new_done=True, tier="d1")
    earlier = NOW - timedelta(days=10)
    activity = make_activity(end=timedelta(days=5))
    assert evaluate_activity(activity, earlier, state, POLICY) is None


def test_jumping_straight_to_most_urgent_tier():
    activity = make_activity(start=timedelta(days=-1), end=timedelta(hours=5))
    plan = evaluate_activity(
        activity, NOW, ActivityNotifyState(new_done=True), POLICY
    )
    assert plan is not None
    assert plan.reasons == ("d1",)
    assert plan.tier == "d1"


def test_upcoming_activity_skips_countdown_by_default():
    activity = make_activity(start=timedelta(days=2), end=timedelta(days=5))
    state = ActivityNotifyState(new_done=True)
    assert evaluate_activity(activity, NOW, state, POLICY) is None


def test_upcoming_activity_counts_down_when_configured():
    policy = ReminderPolicy(
        thresholds=(7, 3, 1), notify_new=True, remind_only_active=False
    )
    activity = make_activity(start=timedelta(days=2), end=timedelta(days=5))
    plan = evaluate_activity(
        activity, NOW, ActivityNotifyState(new_done=True), policy
    )
    assert plan is not None
    assert plan.tier == "d7"


def test_notify_new_disabled_still_counts_down():
    policy = ReminderPolicy(thresholds=(7,), notify_new=False, lookahead_days=45)
    plan = evaluate_activity(make_activity(end=timedelta(days=5)), NOW, None, policy)
    assert plan is not None
    assert plan.reasons == ("d7",)


def test_disabled_policy_yields_nothing():
    policy = ReminderPolicy(thresholds=(), notify_new=False)
    assert evaluate_activity(make_activity(), NOW, None, policy) is None


def test_activity_without_end_time_only_reports_new():
    activity = make_activity(end=None)
    plan = evaluate_activity(activity, NOW, None, POLICY)
    assert plan is not None
    assert plan.reasons == ("new",)
    assert plan.remaining is None


def test_collect_plans_sorted_by_start_time():
    late = make_activity(activity_id="late", name="晚", start=timedelta(days=3))
    early = make_activity(activity_id="early", name="早", start=timedelta(days=1))
    plans = collect_plans([late, early], NOW, {}, POLICY)
    assert [plan.activity.name for plan in plans] == ["早", "晚"]


# ---- 关键词过滤 ----


def test_matches_filters_blacklist():
    activity = make_activity(name="位面分裂", description="限时双倍掉落")
    assert matches_filters(activity, blacklist=["双倍"]) is False
    assert matches_filters(activity, blacklist=["签到"]) is True


def test_matches_filters_whitelist():
    activity = make_activity(name="限定寻访·晨星", tags=("特许寻访",))
    assert matches_filters(activity, whitelist=["寻访"]) is True
    assert matches_filters(activity, whitelist=["签到"]) is False


def test_matches_filters_whitelist_then_blacklist():
    activity = make_activity(name="限定寻访·晨星", tags=("特许寻访",))
    assert matches_filters(activity, whitelist=["寻访"], blacklist=["晨星"]) is False


def test_matches_filters_is_case_insensitive():
    activity = make_activity(name="Special Event")
    assert matches_filters(activity, whitelist=["special"]) is True

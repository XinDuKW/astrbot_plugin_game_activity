"""订阅存储、状态初始化与裁剪。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from astrbot_plugin_game_activity.services.reminder_service import (
    ActivityNotifyState,
    ReminderPolicy,
)
from astrbot_plugin_game_activity.services.subscription_service import (
    SubscriptionService,
    normalize_session,
    resolve_policy,
)
from astrbot_plugin_game_activity.sources.base import Activity, GAME_TIMEZONE

from helpers import FakeKV

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=GAME_TIMEZONE)
UMO = "aiocqhttp:GroupMessage:123456"
POLICY = ReminderPolicy(thresholds=(7, 3, 1), notify_new=True, lookahead_days=45)


def make_service() -> tuple[SubscriptionService, FakeKV]:
    kv = FakeKV()
    service = SubscriptionService(kv.get, kv.put)
    return service, kv


def make_activity(
    activity_id: str = "a1",
    *,
    end: timedelta = timedelta(days=5),
    name: str = "活动",
) -> Activity:
    return Activity(
        game_id="sr",
        activity_id=activity_id,
        name=name,
        start_time=NOW - timedelta(days=1),
        end_time=NOW + end,
    )


# ---- 规范化 ----


def test_normalize_session_defaults():
    session = normalize_session(None)
    assert session["enabled"] is True
    assert session["games"] == []
    assert session["thresholds"] is None
    assert session["notify_new"] is None
    assert session["whitelist"] == []
    assert session["blacklist"] == []


def test_normalize_session_dedupes_games():
    session = normalize_session({"games": ["sr", "sr", "", "ys"]})
    assert session["games"] == ["sr", "ys"]


def test_normalize_session_disabled_thresholds_survive():
    session = normalize_session({"thresholds": []})
    assert session["thresholds"] == []


# ---- 订阅读写 ----


@pytest.mark.anyio
async def test_add_and_list_games():
    service, _ = make_service()
    await service.add_games(UMO, ["sr", "ys"])
    session = await service.get_session(UMO)
    assert session is not None
    assert session["games"] == ["sr", "ys"]
    assert session["enabled"] is True
    assert session["created_at"]

    await service.add_games(UMO, ["sr", "zzz"])
    session = await service.get_session(UMO)
    assert session["games"] == ["sr", "ys", "zzz"]


@pytest.mark.anyio
async def test_remove_games_clears_empty_session():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    assert await service.remove_games(UMO, ["sr"]) is None
    assert await service.get_session(UMO) is None


@pytest.mark.anyio
async def test_remove_session_reports_existence():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    assert await service.remove_session(UMO) is True
    assert await service.remove_session(UMO) is False


@pytest.mark.anyio
async def test_subscriptions_for_game_filters_disabled():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    await service.add_games("aiocqhttp:FriendMessage:1", ["ys"])
    assert [umo for umo, _ in await service.subscriptions_for_game("sr")] == [UMO]

    await service.update_session(UMO, {"enabled": False})
    assert await service.subscriptions_for_game("sr") == []


@pytest.mark.anyio
async def test_update_session_ignores_unknown_fields():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    session = await service.update_session(
        UMO, {"blacklist": ["双倍"], "unknown": "x"}
    )
    assert session is not None
    assert session["blacklist"] == ["双倍"]
    assert "unknown" not in session


@pytest.mark.anyio
async def test_update_session_missing_returns_none():
    service, _ = make_service()
    assert await service.update_session(UMO, {"enabled": True}) is None


@pytest.mark.anyio
async def test_clear_all():
    service, _ = make_service()
    await service.add_games(UMO, ["sr"])
    await service.add_games("aiocqhttp:FriendMessage:2", ["ys"])
    assert await service.clear_all() == 2
    assert await service.get_all() == {}


# ---- 策略合并 ----


def test_resolve_policy_uses_defaults_when_not_overridden():
    session = normalize_session({"games": ["sr"]})
    assert resolve_policy(session, POLICY) is POLICY


def test_resolve_policy_applies_overrides():
    session = normalize_session(
        {"thresholds": [14, 1], "notify_new": False}
    )
    policy = resolve_policy(session, POLICY)
    assert policy.thresholds == (14, 1)
    assert policy.notify_new is False
    # 未覆盖的字段保持默认
    assert policy.lookahead_days == POLICY.lookahead_days


def test_resolve_policy_disabled_countdown():
    session = normalize_session({"thresholds": []})
    policy = resolve_policy(session, POLICY)
    assert policy.thresholds == ()
    assert policy.countdown_enabled is False
    assert policy.enabled is True


# ---- 提醒状态 ----


def test_build_initial_states_marks_new_and_current_tier():
    activities = [
        make_activity("a1", end=timedelta(days=20)),
        make_activity("a2", end=timedelta(days=2)),
        make_activity("a3", end=timedelta(days=-1)),
    ]
    states = SubscriptionService.build_initial_states(activities, NOW, POLICY)

    assert states["sr|a1"]["new"] is True
    assert "tier" not in states["sr|a1"]

    assert states["sr|a2"]["new"] is True
    assert states["sr|a2"]["tier"] == "d3"

    # 已结束的活动不写入状态
    assert "sr|a3" not in states


def test_build_initial_states_with_countdown_disabled():
    policy = ReminderPolicy(thresholds=(), notify_new=True)
    states = SubscriptionService.build_initial_states(
        [make_activity("a1", end=timedelta(hours=2))], NOW, policy
    )
    assert states["sr|a1"]["new"] is True
    assert "tier" not in states["sr|a1"]


@pytest.mark.anyio
async def test_notify_state_round_trip():
    service, _ = make_service()
    data = {UMO: {"sr|a1": {"new": True, "tier": "d3", "end": "2026-09-20T12:00:00+08:00"}}}
    await service.save_notify_state(data)
    loaded = await service.get_notify_state()
    assert loaded[UMO]["sr|a1"]["tier"] == "d3"
    assert loaded[UMO]["sr|a1"]["end"].startswith("2026-09-20")


def test_record_plan_merges_state():
    data: dict = {}
    activity = make_activity("a1", end=timedelta(days=2))
    state = ActivityNotifyState(new_done=True, tier="d3")
    SubscriptionService.record_plan(data, UMO, activity, state)

    entry = data[UMO]["sr|a1"]
    assert entry["new"] is True
    assert entry["tier"] == "d3"
    assert entry["end"].startswith("2026-09-12")


def test_record_plan_drops_empty_entry():
    data = {UMO: {"sr|a1": {"new": True}}}
    SubscriptionService.record_plan(
        data, UMO, make_activity("a1"), ActivityNotifyState()
    )
    assert data[UMO] == {}


def test_record_plan_bucket_stays_addressable():
    """清空最后一个条目后，仍在轮询中持有的桶引用必须依然有效。"""
    data: dict = {}
    bucket = data.setdefault(UMO, {})
    activity = make_activity("a1")

    SubscriptionService.record_plan(data, UMO, activity, ActivityNotifyState())
    assert data[UMO] is bucket

    SubscriptionService.record_plan(
        data, UMO, activity, ActivityNotifyState(new_done=True)
    )
    assert bucket[activity.uid]["new"] is True
    assert data[UMO] is bucket


def test_prune_notify_state_removes_expired_entries():
    data = {
        UMO: {
            "sr|old": {"new": True, "end": (NOW - timedelta(days=60)).isoformat()},
            "sr|recent": {
                "new": True,
                "end": (NOW - timedelta(days=5)).isoformat(),
            },
            "sr|unknown": {"new": True},
        },
        "aiocqhttp:FriendMessage:9": {
            "sr|old": {"new": True, "end": (NOW - timedelta(days=90)).isoformat()}
        },
    }
    removed = SubscriptionService.prune_notify_state(data, NOW, retention_days=30)

    assert removed == 2
    assert set(data[UMO]) == {"sr|recent", "sr|unknown"}
    assert "aiocqhttp:FriendMessage:9" not in data


def test_drop_session_state():
    data = {UMO: {"sr|a1": {"new": True}}}
    SubscriptionService.drop_session_state(data, UMO)
    assert data == {}

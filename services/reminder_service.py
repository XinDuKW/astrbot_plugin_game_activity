"""提醒决策引擎。

本模块是纯逻辑，不依赖 astrbot，也不做任何 IO，便于完整覆盖测试。

提醒分为两类：

* **新活动**：某个活动第一次出现在本会话的视野中时推送一次。
* **倒计时**：活动结束前剩余时间进入某个档位时推送一次。档位按天数
  从大到小配置（默认 ``7, 3, 1``），1 表示剩余不足 24 小时。

同一个活动在**同一轮**里可能同时命中多类提醒（例如刚发现一个还剩 2 天
的活动），此时合并为一条消息推送，避免一次刷多条。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from collections.abc import Mapping, Sequence

from ..sources.base import Activity

REASON_NEW = "new"

DEFAULT_THRESHOLDS: tuple[int, ...] = (7, 3, 1)


@dataclass(frozen=True, slots=True)
class ReminderPolicy:
    """提醒策略，全局默认值与单个会话覆盖值都会归一化成它。"""

    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS
    notify_new: bool = True
    lookahead_days: int = 45
    remind_only_active: bool = True

    @property
    def countdown_enabled(self) -> bool:
        return bool(self.thresholds)

    @property
    def enabled(self) -> bool:
        return self.notify_new or self.countdown_enabled


@dataclass(frozen=True, slots=True)
class ReminderPlan:
    """一个活动本轮应当推送的提醒。"""

    activity: Activity
    reasons: tuple[str, ...]
    tier: str
    remaining: timedelta | None

    @property
    def is_new(self) -> bool:
        return REASON_NEW in self.reasons


@dataclass(slots=True)
class ActivityNotifyState:
    """某个会话对某个活动已完成的提醒记录。"""

    new_done: bool = False
    tier: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.new_done:
            payload["new"] = True
        if self.tier:
            payload["tier"] = self.tier
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> ActivityNotifyState:
        if not isinstance(payload, dict):
            return cls()
        return cls(
            new_done=bool(payload.get("new")),
            tier=str(payload.get("tier") or ""),
        )


def normalize_thresholds(value: Any) -> tuple[int, ...]:
    """把用户输入解析为去重降序的正整数档位。

    接受 ``"7,3,1"``、``[7, 3, 1]``、``"关闭"`` / ``""`` / ``[]`` 等形式。
    返回空元组表示关闭倒计时提醒。
    """
    if value is None:
        return DEFAULT_THRESHOLDS
    if isinstance(value, str):
        text = value.strip()
        if text in ("", "-", "关闭", "off", "none", "无"):
            return ()
        raw_items: Sequence[Any] = [
            item for item in text.replace("，", ",").replace(" ", ",").split(",")
        ]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]

    days: set[int] = set()
    for item in raw_items:
        if isinstance(item, str) and item.strip().endswith("天"):
            item = item.strip()[:-1]
        try:
            number = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if number > 0:
            days.add(number)
    return tuple(sorted(days, reverse=True))


def tier_days(tier: str) -> int | None:
    """从档位标识解析天数，``d7`` → 7。"""
    if not isinstance(tier, str) or not tier.startswith("d"):
        return None
    try:
        return int(tier[1:])
    except ValueError:
        return None


def tier_id(days: int) -> str:
    return f"d{int(days)}"


def current_tier(
    end_time: datetime | None,
    now: datetime,
    thresholds: Sequence[int],
) -> str:
    """返回活动当前所处的**最紧急**档位，未进入任何档位时返回空串。"""
    if end_time is None or not thresholds:
        return ""
    remaining = end_time - now
    if remaining <= timedelta(0):
        return ""
    crossed = [days for days in thresholds if remaining <= timedelta(days=days)]
    if not crossed:
        return ""
    return tier_id(min(crossed))


def is_more_urgent(candidate: str, current: str) -> bool:
    """判断 ``candidate`` 档位是否比已记录的 ``current`` 更紧急。"""
    candidate_days = tier_days(candidate)
    if candidate_days is None:
        return False
    current_days = tier_days(current)
    if current_days is None:
        return True
    return candidate_days < current_days


def reason_label(reason: str) -> str:
    """把提醒原因转换为推送标题里使用的中文标签。"""
    if reason == REASON_NEW:
        return "新活动"
    days = tier_days(reason)
    if days is None:
        return reason
    if days == 1:
        return "剩余不足24小时"
    return f"剩余不足{days}天"


def within_lookahead(
    activity: Activity,
    now: datetime,
    lookahead_days: int,
) -> bool:
    """新活动是否已经进入预告窗口。"""
    if lookahead_days <= 0:
        return True
    if activity.start_time is None:
        return True
    return activity.start_time <= now + timedelta(days=lookahead_days)


def evaluate_activity(
    activity: Activity,
    now: datetime,
    state: ActivityNotifyState | Mapping[str, Any] | None,
    policy: ReminderPolicy,
) -> ReminderPlan | None:
    """判断某个活动本轮是否需要推送，并返回决策结果。

    返回 ``None`` 表示无需推送。调用方在**成功发送**后应当用
    :func:`apply_plan` 更新状态；发送失败则保持原状态，下轮重试。
    """
    if not policy.enabled:
        return None
    if activity.is_ended(now):
        return None

    if not isinstance(state, ActivityNotifyState):
        state = ActivityNotifyState.from_dict(state)

    reasons: list[str] = []
    tier = ""

    if policy.notify_new and not state.new_done:
        if within_lookahead(activity, now, policy.lookahead_days):
            reasons.append(REASON_NEW)

    if policy.countdown_enabled:
        skip_countdown = policy.remind_only_active and activity.is_upcoming(now)
        if not skip_countdown:
            candidate = current_tier(activity.end_time, now, policy.thresholds)
            if candidate and is_more_urgent(candidate, state.tier):
                tier = candidate
                reasons.append(candidate)

    if not reasons:
        return None
    return ReminderPlan(
        activity=activity,
        reasons=tuple(reasons),
        tier=tier,
        remaining=activity.remaining(now),
    )


def apply_plan(
    state: ActivityNotifyState,
    plan: ReminderPlan,
) -> ActivityNotifyState:
    """把已成功发送的提醒写回状态对象。"""
    if plan.is_new:
        state.new_done = True
    if plan.tier and is_more_urgent(plan.tier, state.tier):
        state.tier = plan.tier
    return state


def collect_plans(
    activities: Sequence[Activity],
    now: datetime,
    states: Mapping[str, Any],
    policy: ReminderPolicy,
) -> list[ReminderPlan]:
    """对一批活动求提醒决策，按开始时间排序。"""
    plans: list[ReminderPlan] = []
    for activity in activities:
        plan = evaluate_activity(
            activity,
            now,
            states.get(activity.uid),
            policy,
        )
        if plan is not None:
            plans.append(plan)
    plans.sort(key=lambda plan: plan.activity.sort_key())
    return plans


def matches_filters(
    activity: Activity,
    *,
    whitelist: Sequence[str] = (),
    blacklist: Sequence[str] = (),
) -> bool:
    """按关键词判断活动是否应当推送。

    * 白名单非空时，活动必须命中至少一个白名单关键词；
    * 黑名单命中任一关键词即被排除；
    * 匹配对象为「活动名 + 描述 + 标签」，大小写不敏感。
    """
    haystack = activity.haystack().casefold()

    def hits(keywords: Sequence[str]) -> bool:
        return any(
            keyword.casefold() in haystack
            for keyword in keywords
            if isinstance(keyword, str) and keyword.strip()
        )

    if whitelist and not hits(whitelist):
        return False
    if blacklist and hits(blacklist):
        return False
    return True


def normalize_keywords(value: Any) -> list[str]:
    """把用户输入的关键词列表规范化（去空、去重、保序）。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("，", ",").replace(" ", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]

    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in result:
            continue
        result.append(text)
    return result

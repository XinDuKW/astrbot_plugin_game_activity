"""轮询调度：拉取活动、计算提醒、投递并持久化状态。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from collections.abc import Sequence

from astrbot.api import logger

from ..sources.base import Activity, GAME_TIMEZONE, Game, filter_relevant
from ..sources.registry import GameRegistry
from .delivery_service import ActivityDeliveryService
from .message_formatter import MessageSettings, build_overview_parts
from .reminder_service import (
    ActivityNotifyState,
    ReminderPlan,
    ReminderPolicy,
    apply_plan,
    collect_plans,
    matches_filters,
)
from .subscription_service import (
    SubscriptionService,
    resolve_message_settings,
    resolve_policy,
)


@dataclass(frozen=True, slots=True)
class PollingSettings:
    """轮询与推送的运行时设置。"""

    interval_minutes: int = 30
    lookahead_days: int = 45
    keep_past_days: int = 3
    max_new_per_cycle: int = 8
    notify_on_subscribe: bool = True
    retention_days: int = 30
    overview_memo_seconds: int = 300


@dataclass(slots=True)
class CycleReport:
    """一轮轮询的结果，用于日志与 ``/活动状态``。"""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    sessions: int = 0
    fetched_games: int = 0
    activities: int = 0
    messages: int = 0
    reminders: int = 0
    failures: dict[str, str] = field(default_factory=dict)
    pruned: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures


class ActivityPollingService:
    """负责把数据源、订阅与投递串起来。"""

    def __init__(
        self,
        registry: GameRegistry,
        subscriptions: SubscriptionService,
        delivery: ActivityDeliveryService,
        settings: PollingSettings,
        default_policy: ReminderPolicy,
        message_settings: MessageSettings,
    ) -> None:
        self.registry = registry
        self.subscriptions = subscriptions
        self.delivery = delivery
        self.settings = settings
        self.default_policy = default_policy
        self.message_settings = message_settings

        self._memo: dict[str, tuple[float, list[Activity]]] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self.last_report = CycleReport()

    # ---- 数据获取 ----

    async def fetch_activities(
        self,
        game_id: str,
        *,
        force: bool = False,
    ) -> list[Activity]:
        """获取某个游戏当前相关的活动，带短期内存缓存。"""
        now = datetime.now(GAME_TIMEZONE)
        memo_seconds = max(0, self.settings.overview_memo_seconds)
        cached = self._memo.get(game_id)
        if (
            not force
            and cached is not None
            and memo_seconds > 0
            and time.monotonic() - cached[0] < memo_seconds
        ):
            raw = cached[1]
        else:
            source = self.registry.source_for(game_id)
            if source is None:
                raise LookupError(f"未注册的游戏: {game_id}")
            raw = list(await source.fetch(game_id))
            self._memo[game_id] = (time.monotonic(), raw)

        return filter_relevant(
            raw,
            now,
            lookahead_days=self.settings.lookahead_days,
            keep_past_days=self.settings.keep_past_days,
        )

    def invalidate_memo(self, game_id: str | None = None) -> None:
        if game_id is None:
            self._memo.clear()
        else:
            self._memo.pop(game_id, None)

    # ---- 提醒决策 ----

    def _limit_new_plans(
        self,
        plans: list[ReminderPlan],
        limit: int,
    ) -> list[ReminderPlan]:
        """限制每轮「仅因为是新活动」而推送的条数，避免刷屏。

        带有倒计时提醒的活动不受此限制，因为它们具有时效性。
        """
        if limit <= 0:
            return plans
        urgent = [plan for plan in plans if plan.tier]
        new_only = [plan for plan in plans if plan.is_new and not plan.tier]
        if len(new_only) <= limit:
            return plans
        kept = urgent + new_only[:limit]
        kept.sort(key=lambda plan: plan.activity.sort_key())
        return kept

    def plan_for_session(
        self,
        activities: Sequence[Activity],
        now: datetime,
        states: dict[str, dict[str, Any]],
        policy: ReminderPolicy,
        *,
        whitelist: Sequence[str] = (),
        blacklist: Sequence[str] = (),
    ) -> list[ReminderPlan]:
        candidates = [
            activity
            for activity in activities
            if matches_filters(activity, whitelist=whitelist, blacklist=blacklist)
        ]
        plans = collect_plans(candidates, now, states, policy)
        return self._limit_new_plans(plans, self.settings.max_new_per_cycle)

    # ---- 主循环 ----

    async def run_cycle(self) -> CycleReport:
        """执行一轮完整的检查。同一时刻只会有一轮在跑。"""
        async with self._cycle_lock:
            return await self._run_cycle_locked()

    async def _run_cycle_locked(self) -> CycleReport:
        report = CycleReport(started_at=datetime.now(GAME_TIMEZONE))
        now = report.started_at
        assert now is not None

        sessions = await self.subscriptions.get_all()
        active_sessions = {
            umo: session
            for umo, session in sessions.items()
            if session.get("enabled") and session.get("games")
        }
        if not active_sessions:
            report.finished_at = datetime.now(GAME_TIMEZONE)
            self.last_report = report
            return report

        known_games = self.registry.games
        needed = sorted(
            {
                game_id
                for session in active_sessions.values()
                for game_id in session["games"]
            }
            & set(known_games)
        )

        fetched: dict[str, list[Activity]] = {}
        for game_id in needed:
            try:
                fetched[game_id] = await self.fetch_activities(game_id)
                report.fetched_games += 1
                report.activities += len(fetched[game_id])
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                report.failures[game_id] = detail
                logger.warning(
                    f"获取 {known_games[game_id].name} 活动数据失败: {detail}"
                )

        report.sessions = len(active_sessions)
        if not fetched:
            report.finished_at = datetime.now(GAME_TIMEZONE)
            self.last_report = report
            return report

        notify_state = await self.subscriptions.get_notify_state()
        dirty = False

        for umo, session in active_sessions.items():
            policy = resolve_policy(session, self.default_policy)
            if not policy.enabled:
                continue
            settings = resolve_message_settings(session, self.message_settings)
            bucket = notify_state.setdefault(umo, {})
            for game_id in session["games"]:
                activities = fetched.get(game_id)
                game = known_games.get(game_id)
                if activities is None or game is None:
                    continue

                plans = self.plan_for_session(
                    activities,
                    now,
                    bucket,
                    policy,
                    whitelist=session.get("whitelist") or (),
                    blacklist=session.get("blacklist") or (),
                )
                if not plans:
                    continue

                delivered = await self.delivery.deliver_plans(
                    umo, game, plans, now, settings
                )
                if delivered:
                    report.messages += 1
                    report.reminders += len(delivered)
                for plan in delivered:
                    state = ActivityNotifyState.from_dict(
                        bucket.get(plan.activity.uid)
                    )
                    apply_plan(state, plan)
                    self.subscriptions.record_plan(
                        notify_state,
                        umo,
                        plan.activity,
                        state,
                    )
                    dirty = True

        report.pruned = self.subscriptions.prune_notify_state(
            notify_state,
            now,
            self.settings.retention_days,
        )
        if dirty or report.pruned:
            await self.subscriptions.save_notify_state(notify_state)

        report.finished_at = datetime.now(GAME_TIMEZONE)
        self.last_report = report
        if report.reminders:
            logger.info(
                f"活动提醒已推送: 会话 {report.sessions} 个, "
                f"提醒 {report.reminders} 条"
            )
        if report.failures:
            logger.warning(f"本轮数据源异常: {report.failures}")
        return report

    # ---- 订阅后的初始概览 ----

    async def send_subscription_overview(
        self,
        umo: str,
        games: Sequence[Game],
    ) -> int:
        """订阅成功后推送当前活动概览，并初始化提醒状态。"""
        now = datetime.now(GAME_TIMEZONE)
        session = await self.subscriptions.get_session(umo) or {}
        policy = resolve_policy(session, self.default_policy)
        settings = resolve_message_settings(session, self.message_settings)
        notify_state = await self.subscriptions.get_notify_state()
        bucket = notify_state.setdefault(umo, {})

        sent = 0
        dirty = False
        for game in games:
            try:
                activities = await self.fetch_activities(game.game_id)
            except Exception as exc:
                logger.warning(f"订阅 {game.name} 时获取活动失败: {exc}")
                await self.delivery.send_text(
                    umo, f"⚠️ 暂时无法获取 {game.name} 的活动数据，稍后会自动重试"
                )
                continue

            initialized = SubscriptionService.build_initial_states(
                activities, now, policy
            )
            if not self.settings.notify_on_subscribe:
                bucket.update(initialized)
                dirty = True
                continue

            parts = build_overview_parts(
                game,
                activities,
                now,
                settings,
                source_name=self.registry.source_name(game.game_id),
            )
            if await self.delivery.send_parts(umo, parts, settings):
                sent += 1
                bucket.update(initialized)
                dirty = True

        if dirty:
            await self.subscriptions.save_notify_state(notify_state)
        return sent

    # ---- 生命周期 ----

    async def start(self, *, immediate: bool = True) -> None:
        if self._running:
            return
        self._running = True
        self._wakeup.clear()
        self._task = asyncio.create_task(self._loop(immediate=immediate))
        logger.info(
            f"活动轮询已启动，间隔 {self.settings.interval_minutes} 分钟"
        )

    async def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self, *, immediate: bool = True) -> None:
        if not immediate:
            await self._wait_next()
        while self._running:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"活动轮询出错: {type(exc).__name__}: {exc}")
            await self._wait_next()

    async def _wait_next(self) -> None:
        if not self._running:
            return
        try:
            await asyncio.wait_for(
                self._wakeup.wait(),
                timeout=max(1, self.settings.interval_minutes) * 60,
            )
        except (TimeoutError, asyncio.TimeoutError):
            pass
        finally:
            self._wakeup.clear()

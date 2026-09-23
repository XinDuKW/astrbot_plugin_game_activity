"""订阅与提醒状态的持久化服务。

两类数据分别存放在独立的 KV 项里：

* ``game_activity_subscriptions_v1``：按会话（UMO）保存订阅的游戏、过滤
  关键词、提醒档位与开关。
* ``game_activity_notify_state_v1``：按会话保存「某个活动已经推送过哪些
  提醒」，用于避免重复推送。

状态项会随活动结束被裁剪，不会无限增长。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from collections.abc import Awaitable, Callable, Iterable, Sequence

from ..sources.base import Activity, GAME_TIMEZONE, parse_datetime
from .message_formatter import COVER_MODES, MessageSettings
from .reminder_service import (
    ActivityNotifyState,
    ReminderPolicy,
    current_tier,
    normalize_keywords,
    normalize_thresholds,
)

SUBSCRIPTIONS_KEY = "game_activity_subscriptions_v1"
NOTIFY_STATE_KEY = "game_activity_notify_state_v1"

MAX_GAMES_PER_SESSION = 32
MAX_KEYWORDS = 50
MAX_KEYWORD_LENGTH = 40

GetKV = Callable[[str, Any], Awaitable[Any]]
PutKV = Callable[[str, Any], Awaitable[None]]


def _clean_keywords(value: Any) -> list[str]:
    keywords = [
        keyword[:MAX_KEYWORD_LENGTH]
        for keyword in normalize_keywords(value)
        if keyword
    ]
    return keywords[:MAX_KEYWORDS]


def _session_thresholds(value: Any) -> list[int] | None:
    """会话级档位覆盖值；``None`` 表示沿用全局默认。"""
    if value is None:
        return None
    return list(normalize_thresholds(value))


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_cover_mode(value: Any) -> str | None:
    """会话级封面模式；``None`` 表示沿用全局默认。

    只接受 ``image`` / ``link`` / ``off``，以及表示「跟随默认」的若干写法。
    无法识别的值一律当作「跟随默认」，避免把非法配置固化成覆盖值。
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in ("默认", "跟随", "default", "inherit", "none"):
        return None
    return text if text in COVER_MODES else None


def normalize_session(raw: Any) -> dict[str, Any]:
    """把任意来源的会话记录规范化为内部结构。"""
    if not isinstance(raw, dict):
        raw = {}

    games: list[str] = []
    for item in raw.get("games") or []:
        game_id = str(item or "").strip()
        if game_id and game_id not in games:
            games.append(game_id)

    return {
        "enabled": bool(raw.get("enabled", True)),
        "games": games[:MAX_GAMES_PER_SESSION],
        "thresholds": _session_thresholds(raw.get("thresholds")),
        "notify_new": _optional_bool(raw.get("notify_new")),
        "cover_mode": _optional_cover_mode(raw.get("cover_mode")),
        "whitelist": _clean_keywords(raw.get("whitelist")),
        "blacklist": _clean_keywords(raw.get("blacklist")),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
    }


def resolve_policy(
    session: dict[str, Any],
    default_policy: ReminderPolicy,
) -> ReminderPolicy:
    """把会话级覆盖值合并进全局默认策略。"""
    policy = default_policy
    thresholds = session.get("thresholds")
    if thresholds is not None:
        policy = replace(policy, thresholds=tuple(thresholds))
    notify_new = session.get("notify_new")
    if notify_new is not None:
        policy = replace(policy, notify_new=bool(notify_new))
    return policy


def resolve_message_settings(
    session: dict[str, Any],
    base: MessageSettings,
) -> MessageSettings:
    """把会话级封面模式合并进全局消息设置。

    只有 ``cover_mode`` 支持会话级覆盖；压缩参数与体积预算属于机器资源开销，
    统一由全局配置决定。
    """
    mode = session.get("cover_mode")
    if not mode or mode == base.cover_mode:
        return base
    return replace(base, cover_mode=mode)


class SubscriptionService:
    """订阅与提醒状态的读写入口，内部串行化所有写操作。"""

    def __init__(self, get_kv: GetKV, put_kv: PutKV) -> None:
        self._get_kv = get_kv
        self._put_kv = put_kv
        self._lock = asyncio.Lock()

    # ---- 订阅读取 ----

    async def get_all(self) -> dict[str, dict[str, Any]]:
        raw = await self._get_kv(SUBSCRIPTIONS_KEY, {})
        return self._normalize_all(raw)

    @staticmethod
    def _normalize_all(raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict):
            return {}
        sessions = raw.get("sessions")
        if not isinstance(sessions, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for umo, session in sessions.items():
            key = str(umo or "").strip()
            if not key:
                continue
            result[key] = normalize_session(session)
        return result

    async def get_session(self, umo: str) -> dict[str, Any] | None:
        sessions = await self.get_all()
        session = sessions.get(umo)
        return dict(session) if session else None

    async def subscriptions_for_game(
        self,
        game_id: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        """返回订阅了某个游戏且已启用推送的会话。"""
        sessions = await self.get_all()
        return [
            (umo, session)
            for umo, session in sessions.items()
            if session.get("enabled") and game_id in session.get("games", [])
        ]

    # ---- 订阅写入 ----

    async def _mutate(
        self,
        umo: str,
        mutator: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """在锁内执行「读取 → 修改 → 写回」。``mutator`` 返回 ``None`` 表示删除。"""
        async with self._lock:
            sessions = self._normalize_all(
                await self._get_kv(SUBSCRIPTIONS_KEY, {})
            )
            current = sessions.get(umo)
            updated = mutator(dict(current) if current else None)
            if updated is None:
                sessions.pop(umo, None)
            else:
                sessions[umo] = updated
            await self._put_kv(
                SUBSCRIPTIONS_KEY,
                {"sessions": sessions},
            )
            return dict(updated) if updated else None

    @staticmethod
    def _touch(session: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(GAME_TIMEZONE).isoformat()
        session["updated_at"] = now
        if not session.get("created_at"):
            session["created_at"] = now
        return normalize_session(session)

    async def add_games(
        self,
        umo: str,
        game_ids: Sequence[str],
    ) -> dict[str, Any]:
        wanted = [str(game_id).strip() for game_id in game_ids if str(game_id).strip()]

        def mutator(current: dict[str, Any] | None) -> dict[str, Any]:
            session = normalize_session(current)
            for game_id in wanted:
                if game_id not in session["games"]:
                    session["games"].append(game_id)
            session["games"] = session["games"][:MAX_GAMES_PER_SESSION]
            if not session.get("created_at"):
                session["enabled"] = True
            return self._touch(session)

        result = await self._mutate(umo, mutator)
        assert result is not None
        return result

    async def remove_games(
        self,
        umo: str,
        game_ids: Sequence[str],
    ) -> dict[str, Any] | None:
        wanted = {str(game_id).strip() for game_id in game_ids}

        def mutator(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if current is None:
                return None
            session = normalize_session(current)
            session["games"] = [
                game_id for game_id in session["games"] if game_id not in wanted
            ]
            if not session["games"]:
                return None
            return self._touch(session)

        return await self._mutate(umo, mutator)

    async def remove_session(self, umo: str) -> bool:
        async with self._lock:
            sessions = self._normalize_all(
                await self._get_kv(SUBSCRIPTIONS_KEY, {})
            )
            if umo not in sessions:
                return False
            sessions.pop(umo, None)
            await self._put_kv(SUBSCRIPTIONS_KEY, {"sessions": sessions})
            return True

    async def update_session(
        self,
        umo: str,
        changes: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按字段更新会话设置，只接受白名单内的键。"""

        def mutator(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if current is None:
                return None
            session = normalize_session(current)
            for key, value in changes.items():
                if key == "enabled":
                    session["enabled"] = bool(value)
                elif key == "thresholds":
                    session["thresholds"] = _session_thresholds(value)
                elif key == "notify_new":
                    session["notify_new"] = _optional_bool(value)
                elif key == "cover_mode":
                    session["cover_mode"] = _optional_cover_mode(value)
                elif key == "whitelist":
                    session["whitelist"] = _clean_keywords(value)
                elif key == "blacklist":
                    session["blacklist"] = _clean_keywords(value)
                elif key == "games":
                    session["games"] = normalize_session({"games": value})["games"]
            return self._touch(session)

        return await self._mutate(umo, mutator)

    async def clear_all(self) -> int:
        async with self._lock:
            sessions = self._normalize_all(
                await self._get_kv(SUBSCRIPTIONS_KEY, {})
            )
            count = len(sessions)
            await self._put_kv(SUBSCRIPTIONS_KEY, {"sessions": {}})
            return count

    # ---- 提醒状态 ----

    async def get_notify_state(self) -> dict[str, dict[str, dict[str, Any]]]:
        raw = await self._get_kv(NOTIFY_STATE_KEY, {})
        return self._normalize_notify_state(raw)

    @staticmethod
    def _normalize_notify_state(raw: Any) -> dict[str, dict[str, dict[str, Any]]]:
        if not isinstance(raw, dict):
            return {}
        sessions = raw.get("sessions")
        if not isinstance(sessions, dict):
            return {}
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for umo, entries in sessions.items():
            key = str(umo or "").strip()
            if not key or not isinstance(entries, dict):
                continue
            bucket: dict[str, dict[str, Any]] = {}
            for uid, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                payload = ActivityNotifyState.from_dict(entry).to_dict()
                end_value = entry.get("end")
                if isinstance(end_value, str) and end_value:
                    payload["end"] = end_value
                if payload:
                    bucket[str(uid)] = payload
            if bucket:
                result[key] = bucket
        return result

    async def save_notify_state(
        self,
        data: dict[str, dict[str, dict[str, Any]]],
    ) -> None:
        async with self._lock:
            await self._put_kv(
                NOTIFY_STATE_KEY,
                {
                    "sessions": {
                        umo: entries
                        for umo, entries in data.items()
                        if entries
                    }
                },
            )

    @staticmethod
    def record_plan(
        data: dict[str, dict[str, dict[str, Any]]],
        umo: str,
        activity: Activity,
        state: ActivityNotifyState,
    ) -> None:
        """把一次成功推送写入内存中的状态结构。

        若状态既没有「已推送新活动」也没有档位记录，说明这个活动从未成功
        推送过，此时不应留下只含结束时间的空条目，否则会让它被误判为已处
        理而永久丢失提醒。

        这里保留空的会话桶而不删除键，避免轮询过程中持有该桶引用的调用方
        把后续写入写到已经脱离结构的老对象上。
        """
        payload = state.to_dict()
        if not payload:
            bucket = data.get(umo)
            if bucket is not None:
                bucket.pop(activity.uid, None)
            return

        if activity.end_time is not None:
            payload["end"] = activity.end_time.isoformat()
        data.setdefault(umo, {})[activity.uid] = payload

    @staticmethod
    def build_initial_states(
        activities: Iterable[Activity],
        now: datetime,
        policy: ReminderPolicy,
    ) -> dict[str, dict[str, Any]]:
        """为刚订阅的会话生成初始状态。

        订阅瞬间已经存在的活动不应再触发「新活动」，但倒计时提醒要按当前
        所处档位初始化，这样之后更紧急的档位仍会正常提醒。
        """
        states: dict[str, dict[str, Any]] = {}
        for activity in activities:
            if activity.is_ended(now):
                continue
            state = ActivityNotifyState(new_done=True)
            if policy.countdown_enabled:
                state.tier = current_tier(
                    activity.end_time,
                    now,
                    policy.thresholds,
                )
            payload = state.to_dict()
            if activity.end_time is not None:
                payload["end"] = activity.end_time.isoformat()
            states[activity.uid] = payload
        return states

    @staticmethod
    def prune_notify_state(
        data: dict[str, dict[str, dict[str, Any]]],
        now: datetime,
        retention_days: int,
    ) -> int:
        """清理已经结束很久的活动状态，返回删除的条目数。"""
        cutoff = now - timedelta(days=max(0, retention_days))
        removed = 0
        for umo in list(data.keys()):
            bucket = data[umo]
            for uid in list(bucket.keys()):
                end_time = parse_datetime((bucket[uid] or {}).get("end"))
                if end_time is not None and end_time < cutoff:
                    bucket.pop(uid, None)
                    removed += 1
            if not bucket:
                data.pop(umo, None)
        return removed

    @staticmethod
    def drop_session_state(
        data: dict[str, dict[str, dict[str, Any]]],
        umo: str,
    ) -> None:
        data.pop(umo, None)

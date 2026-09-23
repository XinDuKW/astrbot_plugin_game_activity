"""游戏活动日历的 Plugin Pages 后端接口。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .services.message_formatter import COVER_MODES, build_overview_message
from .services.reminder_service import normalize_keywords, normalize_thresholds
from .services.subscription_service import (
    resolve_message_settings,
    resolve_policy,
)
from .sources import GAME_TIMEZONE

PLUGIN_NAME = "astrbot_plugin_game_activity"
MESSAGE_TYPES = {"GroupMessage", "FriendMessage", "OtherMessage"}
GROUP_LIST_TIMEOUT_SECONDS = 6
MAX_UMO_LENGTH = 512


class ActivityWebUIController:
    """注册并实现活动订阅管理页面所需的 Web API。"""

    def __init__(self, plugin: Any, context: Any):
        self.plugin = plugin
        self.context = context
        self._register_routes()

    def _register_routes(self) -> None:
        routes = (
            ("overview", self.overview, ["GET"], "游戏活动订阅概览"),
            ("session/update", self.update_session, ["POST"], "修改活动订阅设置"),
            ("session/remove", self.remove_session, ["POST"], "移除活动订阅"),
            ("preview", self.preview, ["POST"], "预览某个游戏的当前活动"),
            ("refresh", self.refresh, ["POST"], "立即执行一轮活动检查"),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                methods,
                description,
            )

    # ---- 通用工具 ----

    @staticmethod
    def _meta_value(meta: Any, field: str, default: str = "") -> str:
        if isinstance(meta, dict):
            return str(meta.get(field) or default)
        return str(getattr(meta, field, None) or default)

    @staticmethod
    def _parse_umo(value: Any) -> tuple[str, str, str] | None:
        if not isinstance(value, str) or not value or len(value) > MAX_UMO_LENGTH:
            return None
        if any(ord(char) < 32 for char in value):
            return None
        parts = value.split(":", 2)
        if len(parts) != 3 or not all(parts):
            return None
        platform_id, message_type, session_id = parts
        if message_type not in MESSAGE_TYPES:
            return None
        return platform_id, message_type, session_id

    def _platform_instances(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return []
        instances = getattr(manager, "platform_insts", None)
        if instances is None:
            get_insts = getattr(manager, "get_insts", None)
            instances = get_insts() if callable(get_insts) else []
        return list(instances or [])

    def _aiocqhttp_platforms(self) -> list[Any]:
        result = []
        for platform in self._platform_instances():
            try:
                meta = platform.meta()
            except Exception:
                continue
            if self._meta_value(meta, "name").casefold() == "aiocqhttp":
                result.append(platform)
        return result

    async def _fetch_platform_groups(self, platform: Any) -> dict[str, str]:
        """返回 ``{group_id: group_name}``，失败时返回空字典。"""
        try:
            meta = platform.meta()
            platform_id = self._meta_value(meta, "id")
            if not platform_id:
                return {}
            client = platform.get_client()
            response = await asyncio.wait_for(
                client.call_action(action="get_group_list"),
                timeout=GROUP_LIST_TIMEOUT_SECONDS,
            )
            groups = (
                response.get("data", response.get("groups", []))
                if isinstance(response, dict)
                else response
            )
            if not isinstance(groups, (list, tuple)):
                return {}
            result: dict[str, str] = {}
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_id = str(group.get("group_id") or "").strip()
                if not group_id:
                    continue
                result[group_id] = str(group.get("group_name") or "").strip()
            return result
        except Exception as exc:
            logger.debug(f"获取 QQ 群列表失败: {exc}")
            return {}

    async def _group_names(self) -> dict[str, str]:
        platforms = self._aiocqhttp_platforms()
        if not platforms:
            return {}
        results = await asyncio.gather(
            *(self._fetch_platform_groups(platform) for platform in platforms),
            return_exceptions=True,
        )
        names: dict[str, str] = {}
        for item in results:
            if isinstance(item, dict):
                names.update(item)
        return names

    @staticmethod
    def _session_label(
        message_type: str,
        session_id: str,
        group_names: dict[str, str],
    ) -> str:
        if message_type == "GroupMessage":
            name = group_names.get(session_id) or ""
            return name or f"群聊 {session_id}"
        if message_type == "FriendMessage":
            return f"私聊 {session_id}"
        return f"会话 {session_id}"

    # ---- 接口实现 ----

    async def overview(self):
        try:
            return json_response(await self._build_overview())
        except Exception as exc:
            logger.error(f"读取活动订阅数据失败: {exc}")
            return error_response("读取订阅数据失败", status_code=500)

    async def _build_overview(self) -> dict[str, Any]:
        plugin = self.plugin
        sessions, group_names = await asyncio.gather(
            plugin.subscriptions.get_all(),
            self._group_names(),
        )

        rows: list[dict[str, Any]] = []
        for umo, session in sessions.items():
            parsed = self._parse_umo(umo)
            if parsed is None:
                platform_id, message_type, session_id = "", "Unknown", umo
            else:
                platform_id, message_type, session_id = parsed

            policy = resolve_policy(session, plugin.default_policy)
            message_settings = resolve_message_settings(
                session, plugin.message_settings
            )
            names = []
            for game_id in session.get("games") or []:
                game = plugin.registry.get(game_id)
                names.append(game.name if game else game_id)

            rows.append(
                {
                    "umo": umo,
                    "platform_id": platform_id,
                    "session_type": message_type,
                    "session_id": session_id,
                    "session_name": self._session_label(
                        message_type, session_id, group_names
                    ),
                    "enabled": bool(session.get("enabled")),
                    "games": list(session.get("games") or []),
                    "game_names": names,
                    "thresholds": list(policy.thresholds),
                    "thresholds_overridden": session.get("thresholds") is not None,
                    "notify_new": policy.notify_new,
                    "notify_new_overridden": session.get("notify_new") is not None,
                    "cover_mode": session.get("cover_mode"),
                    "effective_cover_mode": message_settings.cover_mode,
                    "cover_mode_overridden": session.get("cover_mode") is not None,
                    "whitelist": list(session.get("whitelist") or []),
                    "blacklist": list(session.get("blacklist") or []),
                }
            )

        rows.sort(key=lambda row: (row["session_type"], row["session_name"]))

        games = [
            {
                "id": game.game_id,
                "name": game.name,
                "short_name": game.short_name,
                "source_id": game.source_id,
                "source_name": plugin.registry.source_name(game.game_id),
            }
            for game in plugin.registry.ordered()
        ]

        report = plugin.polling.last_report
        cycle = {
            "finished_at": (
                report.finished_at.strftime("%Y-%m-%d %H:%M:%S")
                if report.finished_at
                else ""
            ),
            "fetched_games": report.fetched_games,
            "activities": report.activities,
            "sessions": report.sessions,
            "reminders": report.reminders,
            "failures": dict(report.failures),
        }

        return {
            "games": games,
            "sessions": rows,
            "cycle": cycle,
            "defaults": {
                "thresholds": list(plugin.default_policy.thresholds),
                "notify_new": plugin.default_policy.notify_new,
                "lookahead_days": plugin.default_policy.lookahead_days,
                "interval_minutes": plugin.interval_minutes,
                "cover_mode": plugin.message_settings.cover_mode,
            },
            "sources": [
                {
                    "id": "sra",
                    "name": "SRA 公共 API",
                    "enabled": plugin.enable_sra,
                    "note": plugin.sra_api_base,
                },
                {
                    "id": "akedata",
                    "name": "AKEData（终末地）",
                    "enabled": plugin.enable_akedata,
                    "note": plugin.akedata_api_base,
                },
                {
                    "id": "prts",
                    "name": "PRTS（明日方舟）",
                    "enabled": plugin.enable_prts,
                    "note": plugin.prts_api_base,
                },
            ],
        }

    async def update_session(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)

        umo = payload.get("umo")
        if self._parse_umo(umo) is None:
            return error_response("会话 UMO 格式不正确", status_code=400)

        unknown_games = [
            game_id
            for game_id in payload.get("games", [])
            if self.plugin.registry.get(str(game_id)) is None
        ]
        if unknown_games:
            return error_response(
                f"未知的游戏: {'、'.join(map(str, unknown_games))}",
                status_code=400,
            )

        changes: dict[str, Any] = {}
        if "games" in payload:
            games = [str(item) for item in payload.get("games") or []]
            if not games:
                return error_response(
                    "至少要保留一个订阅游戏，如需清空请使用移除", status_code=400
                )
            changes["games"] = games
        if "enabled" in payload:
            changes["enabled"] = bool(payload.get("enabled"))
        if "notify_new" in payload:
            changes["notify_new"] = bool(payload.get("notify_new"))
        if "cover_mode" in payload:
            raw = payload.get("cover_mode")
            if raw in (None, "", "default", "默认", "跟随"):
                changes["cover_mode"] = None
            else:
                # 这里不能用 normalize_cover_mode：它会把未知值静默回退成默认，
                # 导致非法输入被固化成一个看起来合法的覆盖值。
                mode = str(raw).strip().lower()
                if mode not in COVER_MODES:
                    return error_response(
                        "封面形式只能是 image / link / off / default",
                        status_code=400,
                    )
                changes["cover_mode"] = mode
        if "thresholds" in payload:
            raw = payload.get("thresholds")
            if raw is None:
                changes["thresholds"] = None
            else:
                changes["thresholds"] = list(normalize_thresholds(raw))
        if "whitelist" in payload:
            changes["whitelist"] = normalize_keywords(payload.get("whitelist"))
        if "blacklist" in payload:
            changes["blacklist"] = normalize_keywords(payload.get("blacklist"))

        if not changes:
            return error_response("没有可保存的设置", status_code=400)

        session = await self.plugin.subscriptions.update_session(umo, changes)
        if session is None:
            return error_response("没有找到这个会话的订阅", status_code=404)
        return json_response({"saved": True, "umo": umo})

    async def remove_session(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        umo = payload.get("umo")
        if self._parse_umo(umo) is None:
            return error_response("会话 UMO 格式不正确", status_code=400)

        removed = await self.plugin.subscriptions.remove_session(umo)
        if not removed:
            return error_response("没有找到这个会话的订阅", status_code=404)

        notify_state = await self.plugin.subscriptions.get_notify_state()
        self.plugin.subscriptions.drop_session_state(notify_state, umo)
        await self.plugin.subscriptions.save_notify_state(notify_state)
        return json_response({"removed": True, "umo": umo})

    async def preview(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)

        game_id = str(payload.get("game_id") or "").strip()
        game = self.plugin.registry.get(game_id)
        if game is None:
            return error_response("未知的游戏", status_code=404)

        try:
            activities = await self.plugin.polling.fetch_activities(game_id)
        except Exception as exc:
            logger.warning(f"预览 {game_id} 活动失败: {exc}")
            return error_response(f"获取活动数据失败: {exc}", status_code=502)

        now = datetime.now(GAME_TIMEZONE)
        return json_response(
            {
                "game_id": game_id,
                "game_name": game.name,
                "count": len(activities),
                "text": build_overview_message(
                    game,
                    activities,
                    now,
                    self.plugin.message_settings,
                    source_name=self.plugin.registry.source_name(game_id),
                ),
            }
        )

    async def refresh(self):
        self.plugin.polling.invalidate_memo()
        report = await self.plugin.polling.run_cycle()
        return json_response(
            {
                "fetched_games": report.fetched_games,
                "activities": report.activities,
                "sessions": report.sessions,
                "reminders": report.reminders,
                "failures": dict(report.failures),
            }
        )

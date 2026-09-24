"""AstrBot 游戏活动日历插件。

聚合 SRA 公共 API、AKEData（终末地）与 PRTS（明日方舟）的活动数据，按会话
订阅推送到群聊 / 私聊，支持新活动与剩余 7 天 / 3 天 / 24 小时分级提醒。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .services.cover_service import CoverResolver
from .services.delivery_service import ActivityDeliveryService
from .services.message_formatter import (
    COVER_MODE_IMAGE,
    COVER_MODE_LINK,
    COVER_MODE_OFF,
    DEFAULT_ACTIVITY_SEPARATOR,
    DEFAULT_COVER_MAX_TOTAL_MB,
    DEFAULT_COVER_MAX_WIDTH,
    DEFAULT_COVER_MODE,
    MessageSettings,
    build_overview_parts,
    normalize_cover_mode,
)
from .services.polling_service import ActivityPollingService, PollingSettings
from .services.reminder_service import ReminderPolicy, normalize_thresholds
from .services.subscription_service import (
    SubscriptionService,
    resolve_message_settings,
    resolve_policy,
)
from .sources import (
    GAME_TIMEZONE,
    AkedataSource,
    JsonHttpClient,
    PrtsSource,
    SraSource,
)
from .sources.registry import GameRegistry

try:  # pragma: no cover - 取决于 AstrBot 版本
    from .activity_webui import ActivityWebUIController
except ModuleNotFoundError as exc:  # pragma: no cover
    if exc.name != "astrbot.api.web":
        raise
    ActivityWebUIController = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_game_event_due"

USAGE_SUBSCRIBE = (
    "用法: /活动订阅 <游戏名> [游戏名 ...]\n"
    "可用游戏请查看 /活动游戏"
)
USAGE_THRESHOLD = (
    "用法: /活动阈值 <天数...>\n"
    "例如 /活动阈值 7 3 1 表示剩余不足 7 天 / 3 天 / 24 小时各提醒一次；\n"
    "/活动阈值 关闭 关闭倒计时提醒；/活动阈值 默认 恢复全局默认。"
)
USAGE_FILTER = (
    "用法:\n"
    "/活动过滤 黑名单 <关键词...>  排除命中关键词的活动\n"
    "/活动过滤 白名单 <关键词...>  只推送命中关键词的活动\n"
    "/活动过滤 清空                清空全部关键词"
)

BLACKLIST_TOKENS = {"黑名单", "blacklist", "black"}
WHITELIST_TOKENS = {"白名单", "whitelist", "white"}
RESET_TOKENS = {"默认", "default", "重置", "reset"}
CLEAR_TOKENS = {"清空", "clear", "none"}
ON_TOKENS = {"开启", "开", "on", "true", "1"}
OFF_TOKENS = {"关闭", "关", "off", "false", "0"}

COVER_IMAGE_TOKENS = {"图片", "图", "image", "pic"}
COVER_LINK_TOKENS = {"链接", "link", "文字", "url"}
COVER_MODE_LABELS = {
    COVER_MODE_IMAGE: "直接发送图片",
    COVER_MODE_LINK: "只发图片链接",
    COVER_MODE_OFF: "不附带封面",
}

USAGE_COVER = (
    "用法: /活动封面 <图片|链接|关闭|默认>\n"
    "图片 = 直接发送活动封面（默认）；链接 = 只发封面链接文本；\n"
    "关闭 = 完全不附带封面；默认 = 跟随插件全局配置。"
)

# 分隔符过长会撑爆消息，这里做一个上限保护
MAX_SEPARATOR_LENGTH = 40


class GameActivityPlugin(Star):
    """游戏活动日历插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.interval_minutes = max(1, self._cfg_int("basic", "poll_interval_minutes", 30))
        self.refresh_on_start = self._cfg_bool("basic", "refresh_on_start", True)
        self.request_timeout = max(
            5, self._cfg_int("basic", "request_timeout_seconds", 20)
        )
        self.proxy = str(self._cfg_str("basic", "proxy", "") or "").strip()
        self.cache_dir = self._resolve_cache_dir(
            self._cfg_str("basic", "cache_dir", "")
        )

        self.enable_sra = self._cfg_bool("sources", "enable_sra", True)
        self.sra_api_base = self._cfg_str(
            "sources",
            "sra_api_base",
            "https://starrailassistant.top/api/v1/activity",
        )
        self.enable_akedata = self._cfg_bool("sources", "enable_akedata", True)
        self.akedata_api_base = self._cfg_str(
            "sources", "akedata_api_base", "https://data.akedata.wiki"
        )
        self.akedata_include_pools = self._cfg_bool(
            "sources", "akedata_include_gacha_pools", True
        )
        self.enable_prts = self._cfg_bool("sources", "enable_prts", True)
        self.prts_api_base = self._cfg_str(
            "sources", "prts_api_base", "https://prts.wiki/api.php"
        )
        self.prts_min_interval = max(
            3,
            self._cfg_int("sources", "prts_min_request_interval_seconds", 20),
        )
        self.prts_past_days = max(0, self._cfg_int("sources", "prts_past_days", 7))

        self.default_policy = ReminderPolicy(
            thresholds=normalize_thresholds(
                self._cfg_str("reminder", "default_thresholds", "7,3,1")
            ),
            notify_new=self._cfg_bool("reminder", "notify_new_activity", True),
            lookahead_days=max(
                0, self._cfg_int("reminder", "new_activity_lookahead_days", 45)
            ),
            remind_only_active=self._cfg_bool(
                "reminder", "remind_only_active", True
            ),
        )
        self.message_settings = MessageSettings(
            include_description=self._cfg_bool(
                "message_format", "include_description", False
            ),
            cover_mode=normalize_cover_mode(
                self._cfg("message_format", "cover_mode", DEFAULT_COVER_MODE)
            ),
            include_tags=self._cfg_bool("message_format", "include_tags", True),
            include_extra_times=self._cfg_bool(
                "message_format", "include_extra_times", True
            ),
            single_message_per_game=self._cfg_bool(
                "message_format", "single_message_per_game", True
            ),
            use_forward_node=self._cfg_bool(
                "message_format", "use_forward_node", False
            ),
            cover_compress=self._cfg_bool(
                "message_format", "cover_compress", True
            ),
            cover_max_width=max(
                0,
                self._cfg_int(
                    "message_format", "cover_max_width", DEFAULT_COVER_MAX_WIDTH
                ),
            ),
            cover_max_total_mb=self._cfg_int(
                "message_format",
                "cover_max_total_mb",
                DEFAULT_COVER_MAX_TOTAL_MB,
            ),
            activity_separator=self._cfg_str(
                "message_format",
                "activity_separator",
                DEFAULT_ACTIVITY_SEPARATOR,
            ).strip("\r\n")[:MAX_SEPARATOR_LENGTH],
        )

        self.http_client = JsonHttpClient(
            proxy=self.proxy or None,
            timeout=float(self.request_timeout),
            cache_dir=self.cache_dir,
        )
        self.registry = GameRegistry(self._build_sources())
        if not self.registry.games:
            logger.warning(
                "所有数据源均已关闭，插件不会提供任何游戏。"
                "请在插件配置中启用至少一个数据源。"
            )

        self.polling_settings = PollingSettings(
            interval_minutes=self.interval_minutes,
            lookahead_days=self.default_policy.lookahead_days,
            keep_past_days=3,
            max_new_per_cycle=max(
                0, self._cfg_int("reminder", "max_new_per_cycle", 8)
            ),
            notify_on_subscribe=self._cfg_bool(
                "reminder", "notify_on_subscribe", True
            ),
            retention_days=max(
                1, self._cfg_int("reminder", "state_retention_days", 30)
            ),
        )

        self.subscriptions = SubscriptionService(
            self._get_kv_data, self._put_kv_data
        )
        self.cover_resolver = CoverResolver(
            compress=self.message_settings.cover_compress,
            max_width=self.message_settings.cover_max_width,
            timeout=float(self.request_timeout),
            proxy=self.proxy or None,
        )
        self.delivery = ActivityDeliveryService(
            context, self.message_settings, self.cover_resolver
        )
        self.polling = ActivityPollingService(
            self.registry,
            self.subscriptions,
            self.delivery,
            self.polling_settings,
            self.default_policy,
            self.message_settings,
        )

        self._webui_controller = None
        register_web_api = getattr(context, "register_web_api", None)
        if ActivityWebUIController is not None and callable(register_web_api):
            try:
                self._webui_controller = ActivityWebUIController(self, context)
            except Exception as exc:  # pragma: no cover
                logger.warning(f"游戏活动管理 WebUI 注册失败: {exc}")
        else:
            logger.info("当前 AstrBot 版本不支持 Plugin Pages，跳过后台管理页")

    # ---- 配置读取辅助 ----

    def _cfg(self, block: str, key: str, default: Any) -> Any:
        block_config = self.config.get(block)
        if isinstance(block_config, dict):
            value = block_config.get(key)
            if value is not None:
                return value
        return default

    def _cfg_int(self, block: str, key: str, default: int) -> int:
        try:
            return int(self._cfg(block, key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置 {block}.{key} 不是整数，已回退为 {default}")
            return default

    def _cfg_bool(self, block: str, key: str, default: bool) -> bool:
        value = self._cfg(block, key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ON_TOKENS

    def _cfg_str(self, block: str, key: str, default: str) -> str:
        value = self._cfg(block, key, default)
        return default if value is None else str(value)

    def _resolve_cache_dir(self, configured: str) -> Path | None:
        text = str(configured or "").strip()
        if text:
            try:
                path = Path(text).expanduser()
                path.mkdir(parents=True, exist_ok=True)
                return path
            except OSError as exc:
                logger.warning(f"缓存目录不可用（{text}）：{exc}，回退为插件数据目录")

        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir
        except Exception as exc:  # pragma: no cover
            logger.warning(f"无法获取插件数据目录，缓存已禁用: {exc}")
            return None

    def _build_sources(self) -> list[Any]:
        sources: list[Any] = []
        if self.enable_sra:
            sources.append(
                SraSource(self.http_client, api_base=self.sra_api_base)
            )
        if self.enable_akedata:
            sources.append(
                AkedataSource(
                    self.http_client,
                    api_base=self.akedata_api_base,
                    include_gacha_pools=self.akedata_include_pools,
                    cache_dir=self.cache_dir,
                )
            )
        if self.enable_prts:
            sources.append(
                PrtsSource(
                    self.http_client,
                    api_base=self.prts_api_base,
                    min_request_interval_seconds=self.prts_min_interval,
                    past_days=self.prts_past_days,
                )
            )
        return sources

    # ---- KV 委托 ----

    async def _get_kv_data(self, key: str, default: Any) -> Any:
        return await self.get_kv_data(key, default)

    async def _put_kv_data(self, key: str, value: Any) -> None:
        await self.put_kv_data(key, value)

    # ---- 生命周期 ----

    async def initialize(self):
        logger.info(
            f"游戏活动日历插件初始化中，已启用 {len(self.registry.games)} 款游戏"
        )
        await self.polling.start(immediate=self.refresh_on_start)
        logger.info("游戏活动日历插件初始化完成")

    async def terminate(self):
        await self.polling.stop()
        await self.http_client.close()
        await self.cover_resolver.close()
        logger.info("游戏活动日历插件已停止")

    # ---- 通用辅助 ----

    def _session_games(
        self,
        session: dict[str, Any] | None,
        tokens: Sequence[str],
    ) -> tuple[list[Any], list[str]]:
        """解析目标游戏；未给出参数时使用当前会话已订阅的游戏。"""
        if tokens:
            return self.registry.resolve_many(tokens)

        games = []
        for game_id in (session or {}).get("games") or []:
            game = self.registry.get(game_id)
            if game is not None:
                games.append(game)
        return games, []

    @staticmethod
    def _format_policy(policy: ReminderPolicy) -> str:
        if policy.thresholds:
            threshold_text = "、".join(f"{days}天" for days in policy.thresholds)
        else:
            threshold_text = "已关闭"
        new_text = "开启" if policy.notify_new else "关闭"
        return f"倒计时提醒: {threshold_text}\n新活动提醒: {new_text}"

    @staticmethod
    def _format_filters(session: dict[str, Any]) -> str:
        whitelist = session.get("whitelist") or []
        blacklist = session.get("blacklist") or []
        white_text = "、".join(whitelist) if whitelist else "（未设置）"
        black_text = "、".join(blacklist) if blacklist else "（未设置）"
        return f"白名单: {white_text}\n黑名单: {black_text}"

    def _format_cover_mode(self, session: dict[str, Any]) -> str:
        effective = resolve_message_settings(
            session, self.message_settings
        ).cover_mode
        label = COVER_MODE_LABELS.get(effective, effective)
        origin = "本会话设置" if session.get("cover_mode") else "全局默认"
        return f"封面形式: {label}（{origin}）"

    def _render_session_summary(
        self,
        session: dict[str, Any],
        *,
        title: str = "当前活动订阅",
    ) -> str:
        names = []
        for game_id in session.get("games") or []:
            game = self.registry.get(game_id)
            names.append(game.name if game else game_id)
        games_text = "、".join(names) if names else "（无）"

        policy = resolve_policy(session, self.default_policy)
        status = "开启" if session.get("enabled") else "关闭"
        return (
            f"📋 {title}\n"
            f"推送状态: {status}\n"
            f"订阅游戏: {games_text}\n"
            f"{self._format_policy(policy)}\n"
            f"{self._format_cover_mode(session)}\n"
            f"{self._format_filters(session)}"
        )

    # ---- 指令 ----

    @filter.command("活动订阅", alias={"game_sub", "活动关注"})
    async def subscribe_games(self, event: AstrMessageEvent):
        """订阅游戏活动，格式: /活动订阅 <游戏名> [游戏名 ...]。"""
        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            yield event.plain_result(USAGE_SUBSCRIBE)
            return

        games, unknown = self.registry.resolve_many(tokens)
        if not games:
            yield event.plain_result(
                f"没有识别到游戏: {'、'.join(unknown)}\n{USAGE_SUBSCRIBE}"
            )
            return

        umo = event.unified_msg_origin
        await self.subscriptions.add_games(
            umo, [game.game_id for game in games]
        )
        names = "、".join(game.name for game in games)
        message = f"✅ 已订阅: {names}"
        if unknown:
            message += f"\n⚠️ 未识别: {'、'.join(unknown)}"
        yield event.plain_result(message)

        await self.polling.send_subscription_overview(umo, games)

    @filter.command("活动退订", alias={"game_unsub", "活动取消订阅"})
    async def unsubscribe_games(self, event: AstrMessageEvent):
        """退订游戏活动，格式: /活动退订 <游戏名...> 或 /活动退订 全部。"""
        tokens = event.message_str.strip().split()[1:]
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        if not session:
            yield event.plain_result("当前会话没有订阅任何游戏")
            return

        # 清空是破坏性操作，必须显式写出「全部」，避免裸指令误删订阅
        if any(token in ("全部", "all") for token in tokens):
            await self.subscriptions.remove_session(umo)
            notify_state = await self.subscriptions.get_notify_state()
            self.subscriptions.drop_session_state(notify_state, umo)
            await self.subscriptions.save_notify_state(notify_state)
            yield event.plain_result("✅ 已清空当前会话的全部活动订阅")
            return

        if not tokens:
            yield event.plain_result(
                "用法: /活动退订 <游戏名...>\n"
                "如需清空全部订阅请使用 /活动退订 全部"
            )
            return

        games, unknown = self.registry.resolve_many(tokens)
        if not games:
            yield event.plain_result(
                f"没有识别到游戏: {'、'.join(unknown)}\n"
                "用法: /活动退订 <游戏名...> 或 /活动退订 全部"
            )
            return

        result = await self.subscriptions.remove_games(
            umo, [game.game_id for game in games]
        )
        names = "、".join(game.name for game in games)
        message = f"✅ 已退订: {names}"
        if unknown:
            message += f"\n⚠️ 未识别: {'、'.join(unknown)}"
        if result is None:
            message += "\n当前会话已没有订阅的游戏，订阅记录已清除。"
        yield event.plain_result(message)

    @filter.command("活动列表", alias={"game_list", "活动订阅列表"})
    async def list_subscriptions(self, event: AstrMessageEvent):
        """查看当前会话的活动订阅与设置。"""
        session = await self.subscriptions.get_session(
            event.unified_msg_origin
        )
        if not session:
            yield event.plain_result(
                "当前会话没有订阅任何游戏\n" + USAGE_SUBSCRIBE
            )
            return
        yield event.plain_result(self._render_session_summary(session))

    @filter.command("活动查询", alias={"game_query"})
    async def query_activities(self, event: AstrMessageEvent):
        """查看当前活动，格式: /活动查询 [游戏名...]。"""
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        tokens = event.message_str.strip().split()[1:]
        games, unknown = self._session_games(session, tokens)

        if not games:
            if unknown:
                yield event.plain_result(
                    f"没有识别到游戏: {'、'.join(unknown)}\n{USAGE_SUBSCRIBE}"
                )
            else:
                yield event.plain_result(
                    "当前会话没有订阅任何游戏\n" + USAGE_SUBSCRIBE
                )
            return

        now = datetime.now(GAME_TIMEZONE)
        settings = resolve_message_settings(session or {}, self.message_settings)
        for game in games:
            try:
                activities = await self.polling.fetch_activities(game.game_id)
            except Exception as exc:
                logger.warning(f"查询 {game.name} 活动失败: {exc}")
                yield event.plain_result(
                    f"⚠️ 暂时无法获取 {game.name} 的活动数据: {exc}"
                )
                continue
            parts = build_overview_parts(
                game,
                activities,
                now,
                settings,
                source_name=self.registry.source_name(game.game_id),
            )
            yield event.chain_result(
                await self.delivery.build_event_chain(parts, settings)
            )

    @filter.command("活动过滤", alias={"game_filter", "活动关键词"})
    async def filter_keywords(self, event: AstrMessageEvent):
        """设置活动关键词过滤，格式: /活动过滤 [黑名单|白名单] <关键词...>。"""
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        if not session:
            yield event.plain_result(
                "当前会话没有订阅任何游戏\n" + USAGE_SUBSCRIBE
            )
            return

        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            yield event.plain_result(
                self._format_filters(session) + "\n\n" + USAGE_FILTER
            )
            return

        action = tokens[0]
        if action in CLEAR_TOKENS:
            session = (
                await self.subscriptions.update_session(
                    umo, {"whitelist": [], "blacklist": []}
                )
                or session
            )
            yield event.plain_result(
                "✅ 已清空关键词过滤\n" + self._format_filters(session)
            )
            return

        if action in BLACKLIST_TOKENS:
            field = "blacklist"
        elif action in WHITELIST_TOKENS:
            field = "whitelist"
        else:
            yield event.plain_result(USAGE_FILTER)
            return

        keywords = tokens[1:]
        if not keywords:
            yield event.plain_result(
                f"请提供关键词，例如: /活动过滤 {action} 双倍掉落\n\n{USAGE_FILTER}"
            )
            return

        session = (
            await self.subscriptions.update_session(umo, {field: keywords})
            or session
        )
        label = "黑名单" if field == "blacklist" else "白名单"
        yield event.plain_result(
            f"✅ 已更新{label}: {'、'.join(keywords)}\n"
            + self._format_filters(session)
        )

    @filter.command("活动封面", alias={"game_cover", "活动图片"})
    async def set_cover_mode(self, event: AstrMessageEvent):
        """设置本会话的封面形式: /活动封面 图片|链接|关闭|默认。"""
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        if not session:
            yield event.plain_result(
                "当前会话没有订阅任何游戏\n" + USAGE_SUBSCRIBE
            )
            return

        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            yield event.plain_result(self._format_cover_mode(session) + "\n\n" + USAGE_COVER)
            return

        token = tokens[0].lower()
        if token in RESET_TOKENS:
            mode: str | None = None
        elif token in COVER_IMAGE_TOKENS:
            mode = COVER_MODE_IMAGE
        elif token in COVER_LINK_TOKENS:
            mode = COVER_MODE_LINK
        elif token in OFF_TOKENS:
            mode = COVER_MODE_OFF
        else:
            yield event.plain_result(USAGE_COVER)
            return

        session = (
            await self.subscriptions.update_session(umo, {"cover_mode": mode})
            or session
        )
        yield event.plain_result("✅ 已更新\n" + self._format_cover_mode(session))

    @filter.command("活动开关", alias={"game_toggle", "活动推送"})
    async def toggle_push(self, event: AstrMessageEvent):
        """开启或关闭当前会话的活动推送: /活动开关 开启|关闭。"""
        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            yield event.plain_result("用法: /活动开关 开启 或 /活动开关 关闭")
            return

        action = tokens[0].lower()
        if action in ON_TOKENS:
            enabled = True
        elif action in OFF_TOKENS:
            enabled = False
        else:
            yield event.plain_result("用法: /活动开关 开启 或 /活动开关 关闭")
            return

        session = await self.subscriptions.update_session(
            event.unified_msg_origin, {"enabled": enabled}
        )
        if session is None:
            yield event.plain_result(
                "当前会话没有订阅任何游戏\n" + USAGE_SUBSCRIBE
            )
            return
        yield event.plain_result(
            f"✅ 活动推送已{'开启' if enabled else '关闭'}"
        )

    @filter.command("活动阈值", alias={"game_threshold", "活动提醒时间"})
    async def set_thresholds(self, event: AstrMessageEvent):
        """设置倒计时提醒档位: /活动阈值 7 3 1 / 关闭 / 默认。"""
        umo = event.unified_msg_origin
        session = await self.subscriptions.get_session(umo)
        if not session:
            yield event.plain_result(
                "当前会话没有订阅任何游戏\n" + USAGE_SUBSCRIBE
            )
            return

        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            policy = resolve_policy(session, self.default_policy)
            yield event.plain_result(
                self._format_policy(policy) + "\n\n" + USAGE_THRESHOLD
            )
            return

        if tokens[0] in RESET_TOKENS:
            session = (
                await self.subscriptions.update_session(umo, {"thresholds": None})
                or session
            )
            policy = resolve_policy(session, self.default_policy)
            yield event.plain_result(
                "✅ 已恢复全局默认档位\n" + self._format_policy(policy)
            )
            return

        if tokens[0] in CLEAR_TOKENS or tokens[0] in OFF_TOKENS:
            session = (
                await self.subscriptions.update_session(umo, {"thresholds": []})
                or session
            )
            yield event.plain_result(
                "✅ 已关闭倒计时提醒\n" + self._format_policy(resolve_policy(session, self.default_policy))
            )
            return

        thresholds = normalize_thresholds(tokens)
        if not thresholds:
            yield event.plain_result(USAGE_THRESHOLD)
            return

        session = (
            await self.subscriptions.update_session(
                umo, {"thresholds": list(thresholds)}
            )
            or session
        )
        yield event.plain_result(
            "✅ 已更新提醒档位\n"
            + self._format_policy(resolve_policy(session, self.default_policy))
        )

    @filter.command("活动游戏", alias={"game_games", "活动支持"})
    async def list_games(self, event: AstrMessageEvent):
        """列出支持的游戏。"""
        games = self.registry.ordered()
        if not games:
            yield event.plain_result("当前没有启用任何数据源，请在插件配置中开启")
            return

        grouped: dict[str, list[Any]] = {}
        for game in games:
            grouped.setdefault(game.source_id, []).append(game)

        lines = [f"📋 支持的游戏（{len(games)}）", ""]
        for items in grouped.values():
            source_name = self.registry.source_name(items[0].game_id)
            lines.append(f"▎{source_name}")
            for game in items:
                lines.append(f"• {game.name}（{game.game_id}）")
            lines.append("")
        lines.append("用法: /活动订阅 <游戏名或代号>")
        yield event.plain_result("\n".join(lines).rstrip())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("活动状态", alias={"game_status"})
    async def show_status(self, event: AstrMessageEvent):
        """查看数据源与轮询状态（管理员）。"""
        report = self.polling.last_report
        sessions = await self.subscriptions.get_all()
        lines = [
            "📊 游戏活动日历状态",
            f"轮询间隔: {self.interval_minutes} 分钟",
            f"订阅会话: {len(sessions)} 个",
            f"已启用游戏: {len(self.registry.games)} 款",
        ]
        if report.finished_at is not None:
            lines.append(
                f"上轮完成: {report.finished_at:%Y-%m-%d %H:%M:%S}"
            )
            lines.append(
                f"上轮结果: 数据源 {report.fetched_games} 个 / "
                f"活动 {report.activities} 条 / 提醒 {report.reminders} 条"
            )
        else:
            lines.append("上轮结果: 尚未执行")

        if report.failures:
            lines.append("")
            lines.append("⚠️ 数据源异常:")
            for game_id, detail in report.failures.items():
                game = self.registry.get(game_id)
                lines.append(f"• {game.name if game else game_id}: {detail}")

        sources = self._build_sources_status()
        if sources:
            lines.append("")
            lines.append("数据源:")
            lines.extend(sources)

        yield event.plain_result("\n".join(lines))

    def _build_sources_status(self) -> list[str]:
        lines: list[str] = []
        for source_id, enabled, note in (
            ("sra", self.enable_sra, self.sra_api_base),
            ("akedata", self.enable_akedata, self.akedata_api_base),
            ("prts", self.enable_prts, self.prts_api_base),
        ):
            mark = "🟢" if enabled else "⚪"
            label = {"sra": "SRA 公共 API", "akedata": "AKEData", "prts": "PRTS"}[
                source_id
            ]
            lines.append(f"{mark} {label}: {note}")
        return lines

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("活动刷新", alias={"game_refresh"})
    async def refresh_now(self, event: AstrMessageEvent):
        """立即执行一轮活动检查（管理员）。"""
        yield event.plain_result("正在检查活动数据，请稍候…")
        self.polling.invalidate_memo()
        report = await self.polling.run_cycle()
        lines = [
            "✅ 检查完成",
            f"数据源 {report.fetched_games} 个 / 活动 {report.activities} 条 / "
            f"会话 {report.sessions} 个 / 提醒 {report.reminders} 条",
        ]
        if report.failures:
            lines.append("⚠️ 异常:")
            for game_id, detail in report.failures.items():
                lines.append(f"• {game_id}: {detail}")
        yield event.plain_result("\n".join(lines))

"""活动推送消息的构建。

本模块不依赖 astrbot，纯字符串与数据结构处理，便于单独测试与复用。

消息按「片段」构建，而不是直接拼成字符串：``MessagePart`` 要么是一段文本，
要么是一张封面图。这样发送层既可以把图片作为真正的图片组件发出，也可以在
图片发送失败时退化成纯文本（把图片片段渲染成链接），而格式化逻辑只写一份。

本模块**不决定**哪些封面真的作为图片发送——那取决于图片体积，由
``cover_service`` + ``delivery_service`` 按总体积预算决定。这里只负责把
带封面的活动渲染成「文本 + 图片片段」。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..sources.base import Activity, Game, format_datetime, format_remaining
from .reminder_service import ReminderPlan, reason_label

MAX_ACTIVE_IN_OVERVIEW = 15
MAX_UPCOMING_IN_OVERVIEW = 8
MAX_DESCRIPTION_LENGTH = 180

# 封面压缩与体积预算的默认值。它们属于「消息格式设置」，所以定义在这里；
# cover_service 只是复用它们作为构造参数默认值，这样 message_formatter 不必
# 反向依赖 cover_service（后者需要 astrbot 运行时）。
DEFAULT_COVER_MAX_WIDTH = 720
DEFAULT_COVER_MAX_TOTAL_MB = 5

PART_TEXT = "text"
PART_IMAGE = "image"

# 一条消息里同时推送多个活动时，插在相邻活动之间的视觉分隔线
DEFAULT_ACTIVITY_SEPARATOR = "━━━━━━━━━━━━"

COVER_MODE_OFF = "off"
COVER_MODE_LINK = "link"
COVER_MODE_IMAGE = "image"
COVER_MODES = (COVER_MODE_OFF, COVER_MODE_LINK, COVER_MODE_IMAGE)
DEFAULT_COVER_MODE = COVER_MODE_IMAGE


@dataclass(frozen=True, slots=True)
class MessagePart:
    """消息的一个片段：文本或图片。"""

    kind: str
    value: str

    @property
    def is_image(self) -> bool:
        return self.kind == PART_IMAGE


@dataclass(frozen=True, slots=True)
class MessageSettings:
    """推送消息的内容开关。"""

    include_description: bool = False
    cover_mode: str = DEFAULT_COVER_MODE
    include_tags: bool = True
    include_extra_times: bool = True
    single_message_per_game: bool = True
    use_forward_node: bool = False
    cover_compress: bool = True
    cover_max_width: int = DEFAULT_COVER_MAX_WIDTH
    cover_max_total_mb: int = DEFAULT_COVER_MAX_TOTAL_MB
    activity_separator: str = DEFAULT_ACTIVITY_SEPARATOR

    @property
    def cover_max_total_bytes(self) -> int:
        """单条消息封面总体积预算；``0`` 表示不限制。"""
        try:
            megabytes = int(self.cover_max_total_mb)
        except (TypeError, ValueError):
            return DEFAULT_COVER_MAX_TOTAL_MB * 1024 * 1024
        if megabytes <= 0:
            return 0
        return megabytes * 1024 * 1024


def normalize_cover_mode(value: object) -> str:
    """把配置值规范化为封面模式，并兼容旧版布尔开关。"""
    if isinstance(value, bool):
        return COVER_MODE_IMAGE if value else COVER_MODE_OFF
    text = str(value or "").strip().lower()
    legacy = {
        "true": COVER_MODE_IMAGE,
        "false": COVER_MODE_OFF,
        "on": COVER_MODE_IMAGE,
        "图片": COVER_MODE_IMAGE,
        "链接": COVER_MODE_LINK,
        "关闭": COVER_MODE_OFF,
    }
    text = legacy.get(text, text)
    return text if text in COVER_MODES else DEFAULT_COVER_MODE


def truncate(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}…"


def format_moment(moment: datetime, now: datetime) -> str:
    """格式化时间；跨年时补上年份，避免出现「开始 07-17 / 结束 03-10」的歧义。"""
    return format_datetime(moment, with_year=moment.year != now.year)


def _remaining_suffix(activity: Activity, now: datetime) -> str:
    remaining = activity.remaining(now)
    if remaining is None or remaining <= timedelta(0):
        return ""
    return f"（剩余 {format_remaining(remaining)}）"


def build_activity_block(
    activity: Activity,
    reasons: Sequence[str],
    now: datetime,
    settings: MessageSettings,
    *,
    include_remaining: bool = True,
) -> list[MessagePart]:
    """构建单个活动的消息片段。

    封面按 ``settings.cover_mode`` 处理：``link`` 时写成文本行，``image`` 时作为
    独立的图片片段返回，``off`` 时完全不附带。
    """
    labels = " · ".join(reason_label(reason) for reason in reasons)
    headline = f"【{labels}】{activity.name}" if labels else activity.name

    lines = [headline]
    if activity.start_time is not None:
        lines.append(f"开始：{format_moment(activity.start_time, now)}")
    if activity.end_time is not None:
        suffix = _remaining_suffix(activity, now) if include_remaining else ""
        lines.append(f"结束：{format_moment(activity.end_time, now)}{suffix}")

    if settings.include_extra_times:
        for label, moment in activity.extra_times:
            lines.append(f"{label}：{format_moment(moment, now)}")

    if settings.include_tags and activity.tags:
        lines.append(f"标签：{'、'.join(activity.tags)}")

    if settings.include_description and activity.description:
        lines.append(f"说明：{truncate(activity.description, MAX_DESCRIPTION_LENGTH)}")

    cover = str(activity.cover or "").strip()
    if cover and settings.cover_mode == COVER_MODE_LINK:
        lines.append(f"封面：{cover}")
    if activity.url:
        lines.append(f"详情：{activity.url}")

    parts = [MessagePart(PART_TEXT, "\n".join(lines))]
    if cover and settings.cover_mode == COVER_MODE_IMAGE:
        parts.append(MessagePart(PART_IMAGE, cover))
    return parts


def _assemble(
    header: str,
    segments: Sequence[str | Sequence[MessagePart]],
    separator: str = "",
) -> list[MessagePart]:
    """把头部、文本行与活动块按顺序合并为一条消息。

    ``segments`` 里每个元素要么是一行独立文本（分段标题、来源说明），要么是一个
    活动块（文本 + 可选封面图）。所有片段之间统一用一个空行分隔。

    ``separator`` 非空时会插在**相邻活动块之间**。紧跟分段标题的那个活动块不加分隔线，
    否则标题会和它自己的第一批活动被割开。

    这里会为每个带封面的活动保留一个图片片段；具体哪些封面真的发得出去，由发送层
    按体积预算决定（见 ``cover_service.select_within_budget``）。相邻的文本片段会
    合并成一段，避免产生大量零碎的消息组件。
    """
    parts: list[MessagePart] = []
    buffer = header
    blocks_seen = 0
    after_heading = False

    for segment in segments:
        if isinstance(segment, str):
            buffer += f"\n\n{segment}"
            after_heading = True
            continue

        prefix = "\n\n"
        if blocks_seen and separator and not after_heading:
            prefix = f"\n\n{separator}\n\n"
        blocks_seen += 1
        after_heading = False

        for part in segment:
            if part.is_image:
                if buffer:
                    parts.append(MessagePart(PART_TEXT, buffer))
                    buffer = ""
                parts.append(part)
            else:
                buffer += f"{prefix}{part.value}"
                prefix = "\n\n"

    if buffer:
        parts.append(MessagePart(PART_TEXT, buffer))
    return parts


def parts_to_text(parts: Sequence[MessagePart]) -> str:
    """把片段渲染为纯文本，图片片段渲染为封面链接。

    文本片段本身可能以空行开头（图片之后的续接文本），此时不再额外补分隔符，
    否则会出现多出一个空行。
    """
    text = ""
    for part in parts:
        chunk = f"封面：{part.value}" if part.is_image else part.value
        if not chunk:
            continue
        if text and not chunk.startswith("\n"):
            text += "\n"
        text += chunk
    return text


def build_reminder_parts(
    game: Game,
    plans: Sequence[ReminderPlan],
    now: datetime,
    settings: MessageSettings,
) -> list[MessagePart]:
    """把一个游戏本轮的全部提醒合并为一条消息。"""
    blocks = [
        build_activity_block(plan.activity, plan.reasons, now, settings)
        for plan in plans
    ]
    return _assemble(
        f"🎮 {game.name} · 活动提醒",
        blocks,
        settings.activity_separator,
    )


def build_single_reminder_parts(
    game: Game,
    plan: ReminderPlan,
    now: datetime,
    settings: MessageSettings,
) -> list[MessagePart]:
    """只包含一个活动的提醒消息。"""
    return _assemble(
        f"🎮 {game.name} · 活动提醒",
        [build_activity_block(plan.activity, plan.reasons, now, settings)],
        settings.activity_separator,
    )


def build_overview_parts(
    game: Game,
    activities: Sequence[Activity],
    now: datetime,
    settings: MessageSettings,
    *,
    source_name: str = "",
) -> list[MessagePart]:
    """构建「当前活动一览」，用于订阅确认与手动查询。"""
    active = sorted(
        (item for item in activities if item.is_active(now)),
        key=lambda item: item.end_time or item.sort_key()[0],
    )
    upcoming = sorted(
        (item for item in activities if item.is_upcoming(now)),
        key=lambda item: item.start_time or item.sort_key()[0],
    )

    segments: list[str | list[MessagePart]] = []

    if not active and not upcoming:
        segments.append("当前没有进行中或即将开始的活动。")
    else:
        if active:
            segments.append(f"▎进行中（{len(active)}）")
            for item in active[:MAX_ACTIVE_IN_OVERVIEW]:
                segments.append(build_activity_block(item, (), now, settings))
            if len(active) > MAX_ACTIVE_IN_OVERVIEW:
                segments.append(
                    f"……另有 {len(active) - MAX_ACTIVE_IN_OVERVIEW} 个进行中的活动"
                )

        if upcoming:
            segments.append(f"▎即将开始（{len(upcoming)}）")
            for item in upcoming[:MAX_UPCOMING_IN_OVERVIEW]:
                segments.append(
                    build_activity_block(
                        item,
                        (),
                        now,
                        settings,
                        include_remaining=False,
                    )
                )
            if len(upcoming) > MAX_UPCOMING_IN_OVERVIEW:
                segments.append(
                    f"……另有 {len(upcoming) - MAX_UPCOMING_IN_OVERVIEW} 个即将开始的活动"
                )

    if source_name:
        segments.append(f"（数据来源：{source_name}）")

    return _assemble(
        f"🎮 {game.name} · 当前活动",
        segments,
        settings.activity_separator,
    )


# ---- 纯文本便捷封装（WebUI 预览、测试与降级输出使用） ----


def build_reminder_message(
    game: Game,
    plans: Sequence[ReminderPlan],
    now: datetime,
    settings: MessageSettings,
) -> str:
    return parts_to_text(build_reminder_parts(game, plans, now, settings))


def build_single_reminder_message(
    game: Game,
    plan: ReminderPlan,
    now: datetime,
    settings: MessageSettings,
) -> str:
    return parts_to_text(build_single_reminder_parts(game, plan, now, settings))


def build_overview_message(
    game: Game,
    activities: Sequence[Activity],
    now: datetime,
    settings: MessageSettings,
    *,
    source_name: str = "",
) -> str:
    return parts_to_text(
        build_overview_parts(game, activities, now, settings, source_name=source_name)
    )

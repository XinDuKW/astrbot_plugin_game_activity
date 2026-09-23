"""活动提醒的消息发送层。

只负责把已经构建好的消息片段（文本 + 封面）送到会话。发送失败的提醒必须
**不**被标记为已推送，因此 :meth:`ActivityDeliveryService.deliver_plans` 返回
的是本轮真正送达的提醒列表，由轮询层据此更新状态。

封面处理链路：
``MessagePart(图片)`` → ``CoverResolver`` 下载并按需压缩 → ``select_within_budget``
按总体积预算挑选 → ``Comp.Image.fromBytes/fromURL``。超出预算的封面降级为文本链接，
含图消息整体发送失败时再降级一次（全部图片换成链接）重试。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp

from ..sources.base import Game
from .cover_service import CoverResolver, select_within_budget
from .message_formatter import (
    MessagePart,
    MessageSettings,
    build_reminder_parts,
    build_single_reminder_parts,
)
from .reminder_service import ReminderPlan

NODE_NAME = "游戏活动日历"

SEND_TEXT = "text"
SEND_IMAGE = "image"


@dataclass(frozen=True, slots=True)
class SendItem:
    """已经决定好发送形态的一个片段。"""

    kind: str
    text: str = ""
    url: str = ""
    data: bytes | None = None

    @property
    def is_image(self) -> bool:
        return self.kind == SEND_IMAGE


class ActivityDeliveryService:
    """把活动提醒投递到目标会话。"""

    def __init__(
        self,
        context: Any,
        settings: MessageSettings,
        resolver: CoverResolver | None = None,
    ) -> None:
        self.context = context
        self.settings = settings
        self.resolver = resolver

    # ---- 片段 → 发送项 ----

    @staticmethod
    def _text_only(parts: Sequence[MessagePart]) -> list[SendItem]:
        return [
            SendItem(
                SEND_TEXT,
                text=f"封面：{part.value}" if part.is_image else part.value,
            )
            for part in parts
        ]

    async def resolve_parts(
        self,
        parts: Sequence[MessagePart],
        settings: MessageSettings | None = None,
    ) -> list[SendItem]:
        """决定每个封面是真发图片还是降级成链接。"""
        effective = settings or self.settings
        urls = [part.value for part in parts if part.is_image]
        if not urls or self.resolver is None:
            return [
                SendItem(SEND_TEXT, text=part.value)
                if not part.is_image
                else SendItem(SEND_IMAGE, url=part.value)
                for part in parts
            ]

        ordered = list(dict.fromkeys(urls))
        payloads = await self.resolver.resolve(ordered)
        chosen, dropped = select_within_budget(
            ordered,
            payloads,
            effective.cover_max_total_bytes,
        )
        if dropped:
            logger.info(
                f"封面总体积超出 {effective.cover_max_total_mb}MB 预算，"
                f"{len(dropped)} 张降级为链接"
            )
        inline = {payload.url: payload for payload in chosen}

        items: list[SendItem] = []
        for part in parts:
            if not part.is_image:
                items.append(SendItem(SEND_TEXT, text=part.value))
                continue
            payload = inline.get(part.value)
            if payload is None:
                items.append(SendItem(SEND_TEXT, text=f"封面：{part.value}"))
                continue
            items.append(
                SendItem(SEND_IMAGE, url=part.value, data=payload.data)
            )
        return items

    # ---- 发送项 → 消息链 ----

    @staticmethod
    def _build_image(item: SendItem) -> Any | None:
        try:
            if item.data is not None:
                from_bytes = getattr(Comp.Image, "fromBytes", None)
                if callable(from_bytes):
                    return from_bytes(item.data)
            from_url = getattr(Comp.Image, "fromURL", None)
            if callable(from_url):
                return from_url(item.url)
        except Exception as exc:
            logger.warning(f"构建图片组件失败，回退为链接 {item.url}: {exc}")
        return None

    def build_chain(
        self,
        items: Sequence[SendItem],
        *,
        images_as_links: bool = False,
    ) -> list[Any]:
        """把发送项转换成 AstrBot 消息组件列表。"""
        chain: list[Any] = []
        for item in items:
            if not item.is_image:
                chain.append(Comp.Plain(item.text))
                continue
            if images_as_links:
                chain.append(Comp.Plain(f"封面：{item.url}"))
                continue
            component = self._build_image(item)
            chain.append(
                component if component is not None else Comp.Plain(f"封面：{item.url}")
            )
        return chain

    async def build_event_chain(
        self,
        parts: Sequence[MessagePart],
        settings: MessageSettings | None = None,
    ) -> list[Any]:
        """供指令回复使用：解析封面后返回可直接 yield 的消息链。"""
        return self.build_chain(await self.resolve_parts(parts, settings))

    # ---- 发送 ----

    async def _try_send(self, umo: str, chain: Sequence[Any]) -> bool:
        if not chain:
            return True

        node_class = getattr(Comp, "Node", None)
        nodes_class = getattr(Comp, "Nodes", None)
        if self.settings.use_forward_node and node_class and nodes_class:
            try:
                node = node_class(content=list(chain), name=NODE_NAME)
                await self.context.send_message(
                    umo,
                    MessageChain(chain=[nodes_class([node])]),
                )
                return True
            except Exception as exc:
                logger.warning(f"合并转发发送失败，回退为普通消息: {exc}")

        try:
            await self.context.send_message(
                umo,
                MessageChain(chain=list(chain)),
            )
            return True
        except Exception as exc:
            logger.warning(f"活动推送发送失败 {umo}: {exc}")
            return False

    async def send_parts(
        self,
        umo: str,
        parts: Sequence[MessagePart],
        settings: MessageSettings | None = None,
    ) -> bool:
        """发送消息片段；失败时逐级降级，尽量把内容送达。"""
        if not parts:
            return True

        try:
            items = await self.resolve_parts(parts, settings)
        except Exception as exc:
            logger.warning(f"封面解析失败，改为只发文字与封面链接: {exc}")
            items = self._text_only(parts)

        if await self._try_send(umo, self.build_chain(items)):
            return True

        if any(item.is_image for item in items):
            logger.warning(f"含封面的消息未能送达 {umo}，改为发送封面链接重试")
            if await self._try_send(
                umo, self.build_chain(items, images_as_links=True)
            ):
                return True

        logger.error(f"活动推送最终发送失败 {umo}")
        return False

    async def send_text(self, umo: str, text: str) -> bool:
        if not text:
            return True
        return await self._try_send(umo, [Comp.Plain(text)])

    async def deliver_plans(
        self,
        umo: str,
        game: Game,
        plans: list[ReminderPlan],
        now,
        settings: MessageSettings | None = None,
    ) -> list[ReminderPlan]:
        """推送一个游戏本轮的提醒，返回实际送达的提醒。

        合并发送时要么全部成功要么全部失败；逐条发送时只把成功的那些返回，
        失败的会在下一轮重试且不会与已送达的重复。
        """
        if not plans:
            return []

        effective = settings or self.settings

        if effective.single_message_per_game:
            parts = build_reminder_parts(game, plans, now, effective)
            return list(plans) if await self.send_parts(umo, parts, effective) else []

        delivered: list[ReminderPlan] = []
        for plan in plans:
            parts = build_single_reminder_parts(game, plan, now, effective)
            if await self.send_parts(umo, parts, effective):
                delivered.append(plan)
        return delivered

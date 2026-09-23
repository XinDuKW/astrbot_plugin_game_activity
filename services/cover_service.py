"""封面图解析：下载、压缩，并按体积预算决定每张图的实际发送形态。

为什么需要压缩：活动封面来自不同 CDN，单张从 140 KB 到 1.7 MB 不等，同一款
游戏的活动数量又可能到十几条。压缩后单张通常只剩几到几十 KB，配合总体积预算
既能保证「尽量都带图」，又能避免一次推送发出十几 MB。

依赖 ``astrbot.api.logger``，需要在 AstrBot 运行时下加载；脱离运行时的测试由
``tests/conftest.py`` 注入最小存根。Pillow 缺失或单张下载/压缩失败时会逐张降级，
不影响整条消息。
"""

from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .message_formatter import DEFAULT_COVER_MAX_WIDTH

DEFAULT_QUALITY = 82
DEFAULT_MAX_CACHE_ENTRIES = 96

MAX_DOWNLOAD_BYTES = 24 * 1024 * 1024
UNKNOWN_SIZE_ESTIMATE = 512 * 1024
MAX_CONCURRENT_DOWNLOADS = 6
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class CoverPayload:
    """一张封面的解析结果。"""

    url: str
    size: int
    data: bytes | None = None
    compressed: bool = False
    note: str = ""

    @property
    def inline(self) -> bool:
        """是否以压缩后的字节内联发送（否则交给适配器按 URL 拉取）。"""
        return self.data is not None


def compress_image(data: bytes, max_width: int, quality: int) -> bytes:
    """把图片等比缩放到 ``max_width`` 以内并重新编码为 JPEG。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as source:
        source.load()
        target = source
        if max_width > 0 and source.width > max_width:
            height = max(1, round(source.height * max_width / source.width))
            target = source.resize((max_width, height), Image.LANCZOS)
        if target.mode != "RGB":
            target = target.convert("RGB")
        buffer = io.BytesIO()
        target.save(buffer, format="JPEG", quality=quality, optimize=True)
        return buffer.getvalue()


class CoverResolver:
    """并发下载封面、按需压缩，并缓存解析结果。"""

    def __init__(
        self,
        *,
        compress: bool = True,
        max_width: int = DEFAULT_COVER_MAX_WIDTH,
        quality: int = DEFAULT_QUALITY,
        timeout: float = 20.0,
        proxy: str | None = None,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.compress = bool(compress)
        self.max_width = max(0, int(max_width))
        self.quality = min(95, max(30, int(quality)))
        self.timeout = max(5.0, float(timeout))
        self.proxy = proxy or None
        self.max_cache_entries = max(1, int(max_cache_entries))
        self.user_agent = user_agent

        self._client: Any = None
        self._cache: OrderedDict[str, CoverPayload] = OrderedDict()
        self._pillow_available: bool | None = None
        self._pillow_warned = False
        self.notes: list[str] = []

    # ---- 生命周期 ----

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.user_agent,
                "Accept": "image/*,*/*;q=0.8",
            },
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    # ---- Pillow 可用性 ----

    def _ensure_pillow(self) -> bool:
        if self._pillow_available is not None:
            return self._pillow_available
        try:
            import PIL  # noqa: F401
        except Exception as exc:  # pragma: no cover - 依赖缺失时的兜底
            self._pillow_available = False
            if not self._pillow_warned:
                self._pillow_warned = True
                logger.warning(
                    f"未能导入 Pillow（{exc}），封面将以原图链接形式发送，不做压缩"
                )
        else:
            self._pillow_available = True
        return self._pillow_available

    @property
    def compressing(self) -> bool:
        return self.compress and self._ensure_pillow()

    # ---- 缓存 ----

    def _store(self, payload: CoverPayload) -> None:
        self._cache[payload.url] = payload
        self._cache.move_to_end(payload.url)
        while len(self._cache) > self.max_cache_entries:
            self._cache.popitem(last=False)

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---- 解析 ----

    async def resolve(self, urls: Iterable[str]) -> dict[str, CoverPayload]:
        """解析全部封面，返回 ``{url: CoverPayload}``。

        同一批次内并发下载；此前解析过的 URL 直接命中内存缓存。
        """
        wanted = [str(url).strip() for url in urls if str(url or "").strip()]
        unique = list(dict.fromkeys(wanted))
        if not unique:
            return {}

        pending = [url for url in unique if url not in self._cache]
        if pending:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
            if self.compressing:
                tasks = [self._fetch_and_compress(url, semaphore) for url in pending]
            else:
                tasks = [self._probe(url, semaphore) for url in pending]
            for payload in await asyncio.gather(*tasks):
                self._store(payload)

        return {url: self._cache[url] for url in unique if url in self._cache}

    async def _fetch_and_compress(
        self,
        url: str,
        semaphore: asyncio.Semaphore,
    ) -> CoverPayload:
        async with semaphore:
            try:
                client = await self._ensure_client()
                response = await client.get(url)
                response.raise_for_status()
                raw = response.content
            except Exception as exc:
                logger.warning(f"下载封面失败，回退为原图 URL: {url}（{exc}）")
                return CoverPayload(
                    url=url,
                    size=UNKNOWN_SIZE_ESTIMATE,
                    note=f"下载失败: {exc}",
                )

        if not raw or len(raw) > MAX_DOWNLOAD_BYTES:
            return CoverPayload(
                url=url,
                size=len(raw) or UNKNOWN_SIZE_ESTIMATE,
                note="图片过大或为空，回退为原图 URL",
            )

        try:
            compressed = await asyncio.to_thread(
                compress_image, raw, self.max_width, self.quality
            )
        except Exception as exc:
            logger.warning(f"压缩封面失败，回退为原图 URL: {url}（{exc}）")
            return CoverPayload(
                url=url,
                size=len(raw),
                note=f"压缩失败: {exc}",
            )

        if len(compressed) >= len(raw):
            # 压缩没有收益（本来就是小图），直接用原始字节
            return CoverPayload(url=url, size=len(raw), data=raw, compressed=False)

        return CoverPayload(
            url=url,
            size=len(compressed),
            data=compressed,
            compressed=True,
        )

    async def _probe(self, url: str, semaphore: asyncio.Semaphore) -> CoverPayload:
        """压缩关闭时只探测体积，不下载正文。"""
        async with semaphore:
            try:
                client = await self._ensure_client()
                response = await client.head(url)
                length = response.headers.get("content-length")
                if response.status_code < 400 and length:
                    return CoverPayload(url=url, size=int(length))
                if response.status_code >= 400:
                    raise ValueError(f"HTTP {response.status_code}")
            except Exception as exc:
                logger.debug(f"探测封面体积失败，按估值计入预算: {url}（{exc}）")
            return CoverPayload(
                url=url,
                size=UNKNOWN_SIZE_ESTIMATE,
                note="体积未知，按估值计入预算",
            )

    def describe(self) -> str:
        """诊断用的一句话状态描述。"""
        if not self.compress:
            return "封面压缩：已关闭（仅探测体积）"
        if not self._ensure_pillow():
            return "封面压缩：不可用（缺少 Pillow，退化为原图）"
        return f"封面压缩：{self.max_width}px / JPEG q{self.quality}"


def select_within_budget(
    urls: Sequence[str],
    payloads: dict[str, CoverPayload],
    budget_bytes: int,
) -> tuple[list[CoverPayload], list[str]]:
    """按顺序挑选能塞进预算的封面。

    返回 ``(选中的封面, 超出预算而降级的 URL)``。``budget_bytes <= 0`` 表示不限。
    """
    selected: list[CoverPayload] = []
    dropped: list[str] = []
    used = 0

    for url in urls:
        payload = payloads.get(url)
        size = payload.size if payload is not None else UNKNOWN_SIZE_ESTIMATE
        if budget_bytes > 0 and used + size > budget_bytes:
            dropped.append(url)
            continue
        used += size
        selected.append(
            payload if payload is not None else CoverPayload(url=url, size=size)
        )

    return selected, dropped

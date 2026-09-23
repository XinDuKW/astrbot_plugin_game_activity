"""封面压缩与体积预算选择。"""

from __future__ import annotations

import io
import random
from pathlib import Path

import pytest
from PIL import Image

from astrbot_plugin_game_activity.services.cover_service import (
    UNKNOWN_SIZE_ESTIMATE,
    CoverPayload,
    CoverResolver,
    compress_image,
    select_within_budget,
)

random.seed(20260910)


def noise_png(width: int, height: int) -> bytes:
    """生成随机噪声 PNG：PNG 压不动，便于断言压缩收益。"""
    data = bytes(random.getrandbits(8) for _ in range(width * height * 3))
    image = Image.frombytes("RGB", (width, height), data)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def solid_png(width: int, height: int, mode: str = "RGB", color=None) -> bytes:
    if color is None:
        # P / L 模式需要整数取值，不能传 RGB 元组
        color = {
            "RGBA": (10, 20, 30, 128),
            "P": 3,
            "L": 128,
        }.get(mode, (10, 20, 30))
    image = Image.new(mode, (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def open_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# ---- compress_image ----


def test_compress_resizes_and_shrinks():
    source = noise_png(1600, 900)
    result = compress_image(source, 720, 82)

    assert result[:2] == b"\xff\xd8"  # JPEG 魔数
    assert len(result) < len(source)
    with open_image(result) as image:
        assert image.width == 720
        assert image.height == 405
        assert image.mode == "RGB"


def test_compress_does_not_upscale():
    source = noise_png(320, 200)
    result = compress_image(source, 720, 82)
    with open_image(result) as image:
        assert image.width == 320
        assert image.height == 200


def test_compress_without_width_limit_keeps_size():
    source = noise_png(900, 300)
    result = compress_image(source, 0, 82)
    with open_image(result) as image:
        assert image.width == 900


def test_compress_converts_rgba_and_palette():
    for source in (
        solid_png(800, 400, "RGBA"),
        solid_png(800, 400, "P"),
        solid_png(800, 400, "L"),
    ):
        result = compress_image(source, 720, 82)
        with open_image(result) as image:
            assert image.mode == "RGB"
            assert image.width == 720


def test_compress_lower_quality_gives_smaller_output():
    source = noise_png(1200, 800)
    high = compress_image(source, 720, 92)
    low = compress_image(source, 720, 45)
    assert len(low) < len(high)


def test_compress_invalid_input_raises():
    # Pillow 对无法识别的数据抛 UnidentifiedImageError，它是 OSError 的子类
    with pytest.raises(OSError):
        compress_image(b"not an image", 720, 82)


# ---- select_within_budget ----


def payload(url: str, size: int) -> CoverPayload:
    return CoverPayload(url=url, size=size)


def test_budget_zero_means_unlimited():
    urls = ["a", "b", "c"]
    payloads = {url: payload(url, 10 * 1024 * 1024) for url in urls}
    chosen, dropped = select_within_budget(urls, payloads, 0)
    assert [item.url for item in chosen] == urls
    assert dropped == []


def test_budget_keeps_prefix_and_drops_rest():
    urls = ["a", "b", "c"]
    payloads = {
        "a": payload("a", 2 * 1024 * 1024),
        "b": payload("b", 2 * 1024 * 1024),
        "c": payload("c", 2 * 1024 * 1024),
    }
    chosen, dropped = select_within_budget(urls, payloads, 5 * 1024 * 1024)
    assert [item.url for item in chosen] == ["a", "b"]
    assert dropped == ["c"]


def test_budget_exact_fit_is_kept():
    payloads = {"a": payload("a", 1024)}
    chosen, dropped = select_within_budget(["a"], payloads, 1024)
    assert [item.url for item in chosen] == ["a"]
    assert dropped == []


def test_budget_one_byte_short_drops():
    payloads = {"a": payload("a", 1025)}
    chosen, dropped = select_within_budget(["a"], payloads, 1024)
    assert chosen == []
    assert dropped == ["a"]


def test_budget_uses_estimate_for_unknown_payload():
    chosen, dropped = select_within_budget(["a"], {}, 5 * 1024 * 1024)
    assert [item.url for item in chosen] == ["a"]
    assert chosen[0].size == UNKNOWN_SIZE_ESTIMATE

    chosen, dropped = select_within_budget(["a"], {}, 1024)
    assert chosen == []
    assert dropped == ["a"]


def test_budget_can_drop_everything_when_too_small():
    payloads = {"a": payload("a", 4096), "b": payload("b", 4096)}
    chosen, dropped = select_within_budget(["a", "b"], payloads, 1000)
    assert chosen == []
    assert dropped == ["a", "b"]


# ---- CoverResolver ----


class FakeResponse:
    def __init__(self, content: bytes = b"", status_code: int = 200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, get_map=None, head_map=None):
        self.get_map = get_map or {}
        self.head_map = head_map or {}
        self.get_calls: list[str] = []
        self.head_calls: list[str] = []

    async def get(self, url):
        self.get_calls.append(url)
        item = self.get_map.get(url)
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise RuntimeError("connection refused")
        return item

    async def head(self, url):
        self.head_calls.append(url)
        item = self.head_map.get(url)
        if isinstance(item, Exception):
            raise item
        return item if item is not None else FakeResponse(status_code=404)

    async def aclose(self):
        return None


def attach(resolver: CoverResolver, http: FakeHttp) -> None:
    async def fake_ensure():
        return http

    resolver._ensure_client = fake_ensure  # type: ignore[method-assign]


@pytest.mark.anyio
async def test_resolve_empty_returns_empty():
    resolver = CoverResolver()
    assert await resolver.resolve([]) == {}
    assert await resolver.resolve(["", "   "]) == {}


@pytest.mark.anyio
async def test_resolve_compresses_and_caches():
    url = "https://example.com/a.png"
    http = FakeHttp(get_map={url: FakeResponse(noise_png(1600, 900))})
    resolver = CoverResolver(compress=True, max_width=720)
    attach(resolver, http)

    first = await resolver.resolve([url])
    assert set(first) == {url}
    assert first[url].inline
    assert first[url].compressed
    assert first[url].size == len(first[url].data or b"")
    assert len(http.get_calls) == 1

    # 第二次命中缓存，不再发起请求
    second = await resolver.resolve([url])
    assert second[url] == first[url]
    assert len(http.get_calls) == 1


@pytest.mark.anyio
async def test_resolve_deduplicates_urls():
    url = "https://example.com/a.png"
    http = FakeHttp(get_map={url: FakeResponse(noise_png(800, 400))})
    resolver = CoverResolver()
    attach(resolver, http)

    result = await resolver.resolve([url, url, url])
    assert set(result) == {url}
    assert len(http.get_calls) == 1


@pytest.mark.anyio
async def test_resolve_falls_back_to_url_when_download_fails():
    url = "https://example.com/broken.png"
    http = FakeHttp(get_map={url: RuntimeError("timeout")})
    resolver = CoverResolver()
    attach(resolver, http)

    result = await resolver.resolve([url])
    assert result[url].data is None
    assert result[url].inline is False
    assert result[url].size == UNKNOWN_SIZE_ESTIMATE
    assert "下载失败" in result[url].note


@pytest.mark.anyio
async def test_resolve_without_compression_probes_size():
    url = "https://example.com/a.png"
    http = FakeHttp(head_map={url: FakeResponse(headers={"content-length": "123456"})})
    resolver = CoverResolver(compress=False)
    attach(resolver, http)

    result = await resolver.resolve([url])
    assert result[url].size == 123456
    assert result[url].data is None
    assert http.head_calls == [url]
    assert http.get_calls == []


@pytest.mark.anyio
async def test_resolve_probe_failure_uses_estimate():
    url = "https://example.com/a.png"
    resolver = CoverResolver(compress=False)
    attach(resolver, FakeHttp())

    result = await resolver.resolve([url])
    assert result[url].size == UNKNOWN_SIZE_ESTIMATE
    assert "体积未知" in result[url].note


@pytest.mark.anyio
async def test_cache_is_bounded():
    urls = [f"https://example.com/{index}.png" for index in range(5)]
    http = FakeHttp(
        get_map={url: FakeResponse(noise_png(200, 100)) for url in urls}
    )
    resolver = CoverResolver(max_cache_entries=2)
    attach(resolver, http)

    await resolver.resolve(urls)
    assert len(resolver._cache) == 2
    assert list(resolver._cache) == urls[-2:]

    resolver.clear_cache()
    assert resolver._cache == {}


def test_resolver_describe_reports_state():
    assert "已关闭" in CoverResolver(compress=False).describe()
    assert "720px" in CoverResolver(compress=True).describe()

    class Broken(CoverResolver):
        def _ensure_pillow(self) -> bool:
            return False

    assert "不可用" in Broken(compress=True).describe()


# ---- 日志器来源（审查硬性要求） ----


def test_cover_service_uses_astrbot_logger():
    """日志器必须来自 astrbot.api，不能使用 Python 内置 logging。"""
    import astrbot.api

    from astrbot_plugin_game_activity.services import cover_service

    assert cover_service.logger is astrbot.api.logger


def test_no_plugin_module_imports_builtin_logging():
    """全插件范围内不得出现 import logging / logging.getLogger。"""
    import re

    plugin_dir = Path(__file__).resolve().parent.parent
    pattern = re.compile(
        r"^\s*(?:import\s+logging\b|from\s+logging\b)|logging\.getLogger"
    )

    offenders: list[str] = []
    for path in sorted(plugin_dir.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(plugin_dir)}:{number}")

    assert offenders == [], f"以下位置使用了内置 logging: {offenders}"


@pytest.mark.anyio
async def test_download_failure_is_logged(astrbot_logs):
    if astrbot_logs is None:
        pytest.skip("当前环境已安装真实 AstrBot，跳过存根日志断言")

    url = "https://example.com/broken.png"
    resolver = CoverResolver()
    attach(resolver, FakeHttp(get_map={url: RuntimeError("timeout")}))

    await resolver.resolve([url])
    assert any(
        "下载封面失败" in text for text in astrbot_logs.messages("warning")
    )

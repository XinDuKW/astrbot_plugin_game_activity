"""测试用的假数据源客户端与构造辅助。"""

from __future__ import annotations

from typing import Any

from astrbot_plugin_game_activity.sources.base import HttpResult


class FakeClient:
    """按 URL 关键字分发预置响应的 JsonHttpClient 替身。"""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = dict(responses or {})
        self.cache: dict[str, Any] = {}
        self.calls: list[str] = []
        self.raises: dict[str, Exception] = {}

    async def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
        use_etag: bool = True,
    ) -> HttpResult:
        self.calls.append(url)
        for marker, error in self.raises.items():
            if marker in url:
                raise error
        for marker, payload in self.responses.items():
            if marker in url:
                if isinstance(payload, Exception):
                    raise payload
                return HttpResult(data=payload)
        raise AssertionError(f"未预置响应: {url}")

    def read_cache(self, cache_key: str) -> Any:
        return self.cache.get(cache_key)

    def write_cache(self, cache_key: str, data: Any, etag: str = "") -> None:
        self.cache[cache_key] = data

    async def close(self) -> None:
        return None


class FakeKV:
    """内存版 KV 存取，接口与 AstrBot 的 get_kv_data/put_kv_data 一致。"""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    async def put(self, key: str, value: Any) -> None:
        self.store[key] = value

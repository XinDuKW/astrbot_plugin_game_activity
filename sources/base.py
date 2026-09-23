"""数据源抽象、游戏定义、活动模型与共享 HTTP 工具。

本模块不导入 ``astrbot``，因此可以在没有 AstrBot 运行时的环境下单独
测试数据源的解析逻辑。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence

# 国内游戏活动时间统一使用东八区。中国自 1991 年起不再使用夏令时，
# 因此固定偏移与 Asia/Shanghai 在所有相关日期上等价；固定偏移还能避免
# Windows 上缺少 tzdata 时 zoneinfo 无法加载的问题。
GAME_TIMEZONE = timezone(timedelta(hours=8), "CST")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_TOKEN_STRIP_PATTERN = re.compile(r"[^\w]+", re.UNICODE)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def normalize_token(value: Any) -> str:
    """把用户输入的游戏名/别名规范化为可比较的形态。

    仅保留字母、数字、下划线与中日韩文字，因此 ``崩坏：星穹铁道``、
    ``崩坏:星穹铁道``、``崩坏 星穹铁道`` 与 ``崩坏星穹铁道`` 等价。
    """
    if value is None:
        return ""
    return _TOKEN_STRIP_PATTERN.sub("", str(value).strip().lower())


def strip_html(value: Any) -> str:
    """移除富文本标签并折叠空白，用于清洗数据源返回的描述。"""
    if value is None:
        return ""
    text = _HTML_TAG_PATTERN.sub("", str(value))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def make_activity_id(*parts: Any) -> str:
    """由稳定字段派生活动 ID，保证同一活动跨轮询保持一致。"""
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def parse_datetime(value: Any) -> datetime | None:
    """尽力解析多种时间格式，统一返回东八区 aware datetime。

    支持 ISO 8601（含 ``Z`` 与 ``+08:00`` 偏移）、``YYYY-MM-DD HH:MM:SS``、
    ``YYYY/MM/DD HH:MM:SS``、``YYYY-MM-DD HH:MM`` 以及纯日期。
    无法识别时返回 ``None``，调用方应把它们视为「时间未知」。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None

        parsed = _parse_iso_like(text)
        if parsed is None:
            parsed = _parse_plain_formats(text)
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=GAME_TIMEZONE)
    return parsed.astimezone(GAME_TIMEZONE)


def _parse_iso_like(text: str) -> datetime | None:
    candidate = text
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _parse_plain_formats(text: str) -> datetime | None:
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_datetime(value: datetime | None, *, with_year: bool = False) -> str:
    """把时间格式化为推送里使用的紧凑文本。"""
    if value is None:
        return "未知"
    fmt = "%Y-%m-%d %H:%M" if with_year else "%m-%d %H:%M"
    return value.astimezone(GAME_TIMEZONE).strftime(fmt)


def format_remaining(delta: timedelta | None) -> str:
    """把剩余时间格式化为「3天4小时」这样的中文描述。"""
    if delta is None:
        return ""
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "已结束"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分"


@dataclass(frozen=True, slots=True)
class Game:
    """一个可订阅的游戏。"""

    game_id: str
    name: str
    source_id: str
    aliases: tuple[str, ...] = ()
    short_name: str = ""

    def __post_init__(self) -> None:
        if not self.game_id or not self.name or not self.source_id:
            raise ValueError("Game 的 game_id/name/source_id 不能为空")

    @property
    def display_name(self) -> str:
        return self.short_name or self.name

    @property
    def label(self) -> str:
        return f"{self.name}（{self.game_id}）"

    def matches(self, token: Any) -> bool:
        """判断用户输入是否指向这个游戏。"""
        wanted = normalize_token(token)
        if not wanted:
            return False
        candidates = {self.game_id, self.name, self.short_name, *self.aliases}
        return any(normalize_token(item) == wanted for item in candidates if item)


@dataclass(frozen=True, slots=True)
class Activity:
    """一条活动记录。"""

    game_id: str
    activity_id: str
    name: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    description: str = ""
    cover: str = ""
    url: str = ""
    tags: tuple[str, ...] = ()
    extra_times: tuple[tuple[str, datetime], ...] = ()

    @property
    def uid(self) -> str:
        """跨会话与跨轮询的唯一标识。"""
        return f"{self.game_id}|{self.activity_id}"

    def haystack(self) -> str:
        """关键词过滤匹配用的文本（活动名 + 描述 + 标签）。"""
        parts = [self.name, self.description, " ".join(self.tags)]
        return "\n".join(part for part in parts if part)

    def is_ended(self, now: datetime) -> bool:
        return self.end_time is not None and self.end_time <= now

    def is_active(self, now: datetime) -> bool:
        if self.is_ended(now):
            return False
        return self.start_time is None or self.start_time <= now

    def is_upcoming(self, now: datetime) -> bool:
        return self.start_time is not None and self.start_time > now

    def remaining(self, now: datetime) -> timedelta | None:
        if self.end_time is None:
            return None
        return self.end_time - now

    def sort_key(self) -> tuple[datetime, str]:
        return (
            self.start_time or datetime.min.replace(tzinfo=GAME_TIMEZONE),
            self.name,
        )

    def to_cache_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "activity_id": self.activity_id,
            "name": self.name,
            "start_time": self.start_time.isoformat() if self.start_time else "",
            "end_time": self.end_time.isoformat() if self.end_time else "",
            "description": self.description,
            "cover": self.cover,
            "url": self.url,
            "tags": list(self.tags),
            "extra_times": [
                [label, moment.isoformat()] for label, moment in self.extra_times
            ],
        }

    @classmethod
    def from_cache_dict(cls, payload: Any) -> Activity | None:
        if not isinstance(payload, dict):
            return None
        game_id = str(payload.get("game_id") or "")
        activity_id = str(payload.get("activity_id") or "")
        name = str(payload.get("name") or "")
        if not game_id or not activity_id or not name:
            return None

        extra_times: list[tuple[str, datetime]] = []
        for item in payload.get("extra_times") or []:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            moment = parse_datetime(item[1])
            if moment is not None:
                extra_times.append((str(item[0]), moment))

        raw_tags = payload.get("tags") or []
        tags = tuple(str(tag) for tag in raw_tags if str(tag).strip())

        return cls(
            game_id=game_id,
            activity_id=activity_id,
            name=name,
            start_time=parse_datetime(payload.get("start_time")),
            end_time=parse_datetime(payload.get("end_time")),
            description=str(payload.get("description") or ""),
            cover=str(payload.get("cover") or ""),
            url=str(payload.get("url") or ""),
            tags=tags,
            extra_times=tuple(extra_times),
        )


@dataclass(frozen=True, slots=True)
class HttpResult:
    """一次 HTTP 请求的结果，携带缓存回退信息。"""

    data: Any = None
    stale: bool = False
    not_modified: bool = False


class JsonHttpClient:
    """带 ETag 与磁盘缓存的 JSON 请求客户端。

    磁盘缓存有两个作用：数据源短暂不可用时回退到上次成功结果，以及
    在插件重启后仍能立即提供数据。``stale`` 表示本次结果来自缓存。
    """

    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: float = 20.0,
        cache_dir: Path | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.proxy = proxy or None
        self.timeout = max(5.0, float(timeout))
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.user_agent = user_agent
        self._client: Any = None
        self._etags: dict[str, str] = {}
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
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

    # ---- 缓存读写 ----

    @staticmethod
    def cache_filename(cache_key: str) -> str:
        """把缓存键转换为安全的文件名。"""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", cache_key)[:120] or "cache"
        return f"{safe}.json"

    def _cache_path(self, cache_key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / self.cache_filename(cache_key)

    def read_cache(self, cache_key: str) -> Any:
        path = self._cache_path(cache_key)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        etag = payload.get("etag")
        if isinstance(etag, str) and etag:
            self._etags[cache_key] = etag
        return payload.get("data")

    def write_cache(self, cache_key: str, data: Any, etag: str = "") -> None:
        path = self._cache_path(cache_key)
        if path is None:
            return
        payload = {"etag": etag, "saved_at": time.time(), "data": data}
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    async def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
        use_etag: bool = True,
    ) -> HttpResult:
        """请求 JSON。成功时写入缓存；失败时回退缓存并标记 ``stale``。

        当服务端返回 304 时返回 ``not_modified=True``，调用方应保留上一次
        的内存数据。请求彻底失败且没有缓存时抛出异常。
        """
        client = await self._ensure_client()
        request_headers = dict(headers or {})
        etag = self._etags.get(cache_key or "")
        if use_etag and cache_key and etag:
            request_headers["If-None-Match"] = etag

        try:
            response = await client.get(url, headers=request_headers)
        except Exception:
            cached = self.read_cache(cache_key) if cache_key else None
            if cached is None:
                raise
            return HttpResult(data=cached, stale=True)

        if response.status_code == 304:
            return HttpResult(not_modified=True)

        try:
            response.raise_for_status()
        except Exception:
            cached = self.read_cache(cache_key) if cache_key else None
            if cached is None:
                raise
            return HttpResult(data=cached, stale=True)

        try:
            data = response.json()
        except ValueError as exc:
            cached = self.read_cache(cache_key) if cache_key else None
            if cached is None:
                raise ValueError(f"响应不是合法 JSON: {url}") from exc
            return HttpResult(data=cached, stale=True)

        if cache_key:
            new_etag = response.headers.get("ETag", "")
            if new_etag:
                self._etags[cache_key] = new_etag
            self.write_cache(cache_key, data, new_etag)
        return HttpResult(data=data)


class RateLimiter:
    """在多次请求之间强制最小间隔的异步限速器。"""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._last_at = 0.0

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        import asyncio

        elapsed = time.monotonic() - self._last_at
        remaining = self.min_interval - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_at = time.monotonic()

    def mark(self) -> None:
        self._last_at = time.monotonic()


class ActivitySource(ABC):
    """活动数据源接口。"""

    source_id: str = ""
    display_name: str = ""

    @abstractmethod
    def games(self) -> tuple[Game, ...]:
        """返回该数据源提供的全部游戏。"""

    @abstractmethod
    async def fetch(self, game_id: str) -> Sequence[Activity]:
        """拉取某个游戏当前可用的活动列表。

        失败时应抛出异常，调用方会跳过该游戏并保留上一轮状态。
        """


def filter_relevant(
    activities: Iterable[Activity],
    now: datetime,
    *,
    lookahead_days: int = 0,
    keep_past_days: int = 2,
) -> list[Activity]:
    """保留「进行中 / 即将开始 / 刚刚结束」的活动。

    ``keep_past_days`` 让刚结束不久的活动短暂保留，避免提醒状态在活动
    结束瞬间被清理掉。
    """
    horizon = None
    if lookahead_days > 0:
        horizon = now + timedelta(days=lookahead_days)
    past_limit = now - timedelta(days=max(0, keep_past_days))

    kept: list[Activity] = []
    for activity in activities:
        if activity.end_time is not None and activity.end_time <= past_limit:
            continue
        if horizon is not None and activity.start_time is not None:
            if activity.start_time > horizon:
                continue
        kept.append(activity)
    return kept

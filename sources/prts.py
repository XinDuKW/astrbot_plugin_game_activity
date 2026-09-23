"""PRTS（明日方舟中文 Wiki）数据源。

PRTS 使用 Semantic MediaWiki，活动页通过 ``{{活动信息}}`` 模板写入语义属性
（``活动开始时间`` / ``活动结束时间`` / ``兑换结束时间`` / ``名称`` / ``分类``）。
因此可以用 MediaWiki 的 ``action=ask`` 接口**一次请求**取回全部活动及其
起止时间，无需逐页抓取：:

    [[分类:有活动信息的页面]][[活动结束时间::>2026-09-15]]
    |?活动开始时间|?活动结束时间|?兑换结束时间|?名称|?名称缩短|?分类

时间字段返回 Unix 时间戳（UTC 秒），本模块统一转换为东八区。

注意：prts.wiki 部署了较严格的 WAF，连续请求会被临时返回 403。本数据源
内置最小请求间隔限速、ETag 缓存与磁盘缓存回退，因此请保持默认的低频轮询。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from collections.abc import Sequence
from urllib.parse import urlencode

from .base import (
    Activity,
    ActivitySource,
    GAME_TIMEZONE,
    Game,
    JsonHttpClient,
    RateLimiter,
    make_activity_id,
)

DEFAULT_PRTS_API_BASE = "https://prts.wiki/api.php"

ACTIVITY_CATEGORY = "分类:有活动信息的页面"
CATEGORY_PREFIX = "分类:"

# PRTS 的活动页同时属于若干「维护类」分类（例如「使用Tabber扩展的标签页」
# 「正在编辑的页面」），它们不是活动分类，推送时应当丢弃。
IGNORED_CATEGORY_MARKERS = (
    "使用",
    "页面",
    "模板",
    "含有",
    "正在编辑",
    "编辑中",
    "文件",
    "重定向",
)

PRTS_ASK_FIELDS = (
    "?活动开始时间",
    "?活动结束时间",
    "?兑换结束时间",
    "?名称",
    "?名称缩短",
    "?分类",
    "?标题图文件名",
    "?官网链接",
)
PRTS_PAGE_LIMIT = 100
PRTS_MAX_PAGES = 5

ARKNIGHTS_GAME = Game(
    "arknights",
    "明日方舟",
    "prts",
    ("舟", "方舟", "ak", "arknights", "明日方舟"),
    "明日方舟",
)


def parse_smw_timestamp(value: Any) -> datetime | None:
    """解析 SMW 返回的时间戳字段（Unix 秒，UTC）。"""
    if isinstance(value, dict):
        raw = value.get("timestamp")
    else:
        raw = value
    if raw in (None, ""):
        return None
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(
            GAME_TIMEZONE
        )
    except (OverflowError, OSError, ValueError):
        return None


def _first_text(printouts: dict, key: str) -> str:
    values = printouts.get(key)
    if not isinstance(values, list) or not values:
        return ""
    first = values[0]
    if isinstance(first, dict):
        return str(first.get("fulltext") or first.get("text") or "").strip()
    return str(first or "").strip()


def _first_time(printouts: dict, key: str) -> datetime | None:
    values = printouts.get(key)
    if not isinstance(values, list) or not values:
        return None
    return parse_smw_timestamp(values[0])


def _category_names(printouts: dict) -> tuple[str, ...]:
    values = printouts.get("分类")
    if not isinstance(values, list):
        return ()
    names: list[str] = []
    for item in values:
        if isinstance(item, dict):
            text = str(item.get("fulltext") or item.get("text") or "")
        else:
            text = str(item or "")
        text = text.strip()
        if text.startswith(CATEGORY_PREFIX):
            text = text[len(CATEGORY_PREFIX) :]
        if not text or text == ACTIVITY_CATEGORY[len(CATEGORY_PREFIX) :]:
            continue
        if any(marker in text for marker in IGNORED_CATEGORY_MARKERS):
            continue
        if text not in names:
            names.append(text)
    return tuple(names)


class PrtsSource(ActivitySource):
    """明日方舟活动数据源（Semantic MediaWiki 查询）。"""

    source_id = "prts"
    display_name = "PRTS"

    def __init__(
        self,
        client: JsonHttpClient,
        *,
        api_base: str = DEFAULT_PRTS_API_BASE,
        min_request_interval_seconds: float = 20.0,
        past_days: int = 7,
    ) -> None:
        self._client = client
        self._api_base = (api_base or DEFAULT_PRTS_API_BASE).strip()
        self._limiter = RateLimiter(min_request_interval_seconds)
        self._past_days = max(0, int(past_days))
        self.notes: dict[str, str] = {}
        self.versions: dict[str, str] = {}

    def games(self) -> tuple[Game, ...]:
        return (ARKNIGHTS_GAME,)

    async def fetch(self, game_id: str) -> Sequence[Activity]:
        cutoff = datetime.now(GAME_TIMEZONE) - timedelta(days=self._past_days)
        query = "".join(
            [
                f"[[{ACTIVITY_CATEGORY}]]",
                f"[[活动结束时间::>{cutoff.strftime('%Y-%m-%d')}]]",
                "|",
                "|".join(PRTS_ASK_FIELDS),
            ]
        )

        results: dict[str, Any] = {}
        offset = 0
        stale = False
        for page in range(PRTS_MAX_PAGES):
            params: dict[str, Any] = {
                "action": "ask",
                "format": "json",
                "query": query,
            }
            if offset:
                params["offset"] = offset
            url = f"{self._api_base}?{urlencode(params)}"

            await self._limiter.wait()
            result = await self._client.get_json(
                url,
                cache_key=f"prts_ask_{offset}",
                headers={"Referer": "https://prts.wiki/"},
                use_etag=False,
            )
            stale = stale or result.stale
            payload = result.data if isinstance(result.data, dict) else {}
            page_results = (payload.get("query") or {}).get("results")
            if not isinstance(page_results, dict) or not page_results:
                break
            results.update(page_results)

            if page >= PRTS_MAX_PAGES - 1:
                break
            continuation = payload.get("query-continue-offset")
            if continuation in (None, ""):
                break
            try:
                offset = int(continuation)
            except (TypeError, ValueError):
                break

        self.notes[game_id] = "使用缓存数据" if stale else ""
        return self._parse(results)

    def _parse(self, results: dict[str, Any]) -> list[Activity]:
        activities: list[Activity] = []
        for page_title, entry in results.items():
            if not isinstance(entry, dict):
                continue
            printouts = entry.get("printouts")
            if not isinstance(printouts, dict):
                continue

            start_time = _first_time(printouts, "活动开始时间")
            end_time = _first_time(printouts, "活动结束时间")
            if end_time is None:
                # 没有结束时间就无法做倒计时提醒
                continue

            short_name = _first_text(printouts, "名称缩短")
            full_name = _first_text(printouts, "名称")
            name = short_name or full_name or str(page_title)

            extra_times: list[tuple[str, datetime]] = []
            exchange_end = _first_time(printouts, "兑换结束时间")
            if exchange_end is not None:
                extra_times.append(("兑换结束", exchange_end))

            fullurl = str(entry.get("fullurl") or "").strip()
            if fullurl.startswith("//"):
                fullurl = f"https:{fullurl}"
            elif fullurl.startswith("/"):
                fullurl = f"https://prts.wiki{fullurl}"

            activities.append(
                Activity(
                    game_id=ARKNIGHTS_GAME.game_id,
                    activity_id=make_activity_id("prts", page_title),
                    name=name,
                    start_time=start_time,
                    end_time=end_time,
                    description=full_name if full_name and full_name != name else "",
                    url=fullurl,
                    tags=_category_names(printouts),
                    extra_times=tuple(extra_times),
                )
            )
        return activities

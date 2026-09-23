"""SRA（StarRailAssistant）公共 API 数据源。

接口：``GET https://starrailassistant.top/api/v1/activity/{game}.json``

该接口无需认证，返回结构统一：版本信息 + ``activities`` 列表，每条活动
都带 ``name`` / ``description`` / ``startTime`` / ``endTime`` / ``cover``。
"""

from __future__ import annotations

from typing import Any
from collections.abc import Sequence

from .base import (
    Activity,
    ActivitySource,
    Game,
    JsonHttpClient,
    make_activity_id,
    parse_datetime,
    strip_html,
)

DEFAULT_SRA_API_BASE = "https://starrailassistant.top/api/v1/activity"

SRA_GAMES: tuple[Game, ...] = (
    Game(
        "sr",
        "崩坏：星穹铁道",
        "sra",
        ("崩铁", "星铁", "星穹铁道", "starrail", "hsr"),
        "星穹铁道",
    ),
    Game("ys", "原神", "sra", ("genshin", "genshinimpact"), "原神"),
    Game("zzz", "绝区零", "sra", ("zenless", "zenlesszonezero"), "绝区零"),
    Game("ww", "鸣潮", "sra", ("wuwa", "wutheringwaves"), "鸣潮"),
    Game("nte", "异环", "sra", ("neverness", "nevernesstoeverness"), "异环"),
    Game(
        "ba-cn",
        "蔚蓝档案（国服）",
        "sra",
        ("蔚蓝档案", "碧蓝档案", "ba", "蔚蓝国服", "ba国服"),
        "蔚蓝档案国服",
    ),
    Game(
        "ba-global",
        "蔚蓝档案（国际服）",
        "sra",
        ("蔚蓝档案", "碧蓝档案", "蔚蓝国际服", "ba国际服", "bluearchiveglobal"),
        "蔚蓝档案国际服",
    ),
    Game(
        "ba-jp",
        "蔚蓝档案（日服）",
        "sra",
        ("蔚蓝档案", "碧蓝档案", "蔚蓝日服", "ba日服", "bluearchivejp"),
        "蔚蓝档案日服",
    ),
)


class SraSource(ActivitySource):
    """SRA 公开活动接口数据源，一个数据源覆盖多款游戏。"""

    source_id = "sra"
    display_name = "SRA 公共 API"

    def __init__(
        self,
        client: JsonHttpClient,
        *,
        api_base: str = DEFAULT_SRA_API_BASE,
    ) -> None:
        self._client = client
        self._api_base = (api_base or DEFAULT_SRA_API_BASE).strip().rstrip("/")
        self._cache: dict[str, list[Activity]] = {}
        self.versions: dict[str, str] = {}
        self.notes: dict[str, str] = {}

    def games(self) -> tuple[Game, ...]:
        return SRA_GAMES

    async def fetch(self, game_id: str) -> Sequence[Activity]:
        cache_key = f"sra_{game_id}"
        url = f"{self._api_base}/{game_id}.json"
        result = await self._client.get_json(url, cache_key=cache_key)

        if result.not_modified:
            cached = self._cache.get(game_id)
            if cached is None:
                raw = self._client.read_cache(cache_key)
                cached = self._parse(raw, game_id) if raw is not None else []
                self._cache[game_id] = cached
            return cached

        activities = self._parse(result.data, game_id)
        self._cache[game_id] = activities
        self._record_meta(result.data, game_id)
        self.notes[game_id] = "使用缓存数据" if result.stale else ""
        return activities

    def _record_meta(self, data: Any, game_id: str) -> None:
        if not isinstance(data, dict):
            return
        version = str(data.get("version") or "").strip()
        version_name = strip_html(data.get("versionName"))
        if version:
            self.versions[game_id] = (
                f"{version} {version_name}".strip() if version_name else version
            )

    def _parse(self, data: Any, game_id: str) -> list[Activity]:
        if not isinstance(data, dict):
            return []

        activities: list[Activity] = []
        for item in data.get("activities") or []:
            if not isinstance(item, dict):
                continue
            name = strip_html(item.get("name"))
            if not name:
                continue

            start_time = parse_datetime(item.get("startTime"))
            end_time = parse_datetime(item.get("endTime"))
            if end_time is None:
                # 没有结束时间就无法做倒计时提醒，且会被长期保留在相关活动集合里
                continue
            anchor = start_time or end_time
            activities.append(
                Activity(
                    game_id=game_id,
                    activity_id=make_activity_id(
                        name,
                        anchor.isoformat() if anchor else "",
                    ),
                    name=name,
                    start_time=start_time,
                    end_time=end_time,
                    description=strip_html(item.get("description")),
                    cover=str(item.get("cover") or "").strip(),
                )
            )
        return activities

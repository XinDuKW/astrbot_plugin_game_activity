"""AKEData（明日方舟：终末地）数据源。

数据来自 ``https://data.akedata.wiki``：先读 ``manifest.json`` 找到当前
游戏版本，再按版本目录读取 TableCfg。活动名通过 ``I18nTextTable_CN``
按文本 ID 反查，活动时间来自 ``TimeRangeTable``。

体积控制：``I18nTextTable_CN.json`` 约 11MB，``CharacterTable.json`` 约
10MB。因此本数据源
* 只在游戏大版本变化时重新下载表数据（hotfix 版本号变化不触发）；
* 只下载活动与卡池相关的表，不下载 ``CharacterTable.json``（仅影响卡池
  的 UP 角色名，卡池本身的时间与名称不受影响）；
* 解析后只保留精简的活动列表并落盘缓存。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from collections.abc import Sequence
from urllib.parse import quote

from .base import (
    Activity,
    ActivitySource,
    Game,
    JsonHttpClient,
    make_activity_id,
    parse_datetime,
    strip_html,
)

DEFAULT_AKEDATA_API_BASE = "https://data.akedata.wiki"

ACTIVITY_IMAGE_PATH = (
    "public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/activity"
)

RESOLVED_CACHE_KEY = "akedata_resolved"
CACHE_KEY_PREFIX = "akedata_"

POOL_TYPE_NAMES = {
    0: "特许寻访",
    1: "新手寻访",
    2: "常驻寻访",
    3: "联合寻访",
}
DEFAULT_POOL_TYPE_NAME = "角色寻访"

ENDFIELD_GAME = Game(
    "endfield",
    "明日方舟：终末地",
    "akedata",
    ("终末地", "zmd", "endfield", "明日方舟终末地"),
    "终末地",
)


def _resolve_text(reference: Any, text_table: dict, fallback: str) -> str:
    """把 ``{"id": ..., "text": ""}`` 形式的文本引用解析为可读文本。"""
    if isinstance(reference, dict):
        inline = str(reference.get("text") or "").strip()
        if inline:
            return inline
        key = reference.get("id")
        if key is not None:
            found = text_table.get(str(key))
            if found not in (None, ""):
                return strip_html(found) or fallback
        return fallback
    if reference is None:
        return fallback
    return strip_html(reference) or fallback


def _first_time_range(
    record: dict,
    time_ranges: dict,
    time_key: str,
    default_time_id: str = "",
) -> dict:
    """取出记录对应的第一个时间段。"""
    time_id = str(record.get(time_key) or default_time_id or "")
    if not time_id:
        return {}
    entry = time_ranges.get(time_id)
    if not isinstance(entry, dict):
        return {}
    ranges = entry.get("timeRangeList")
    if not isinstance(ranges, list) or not ranges:
        return {}
    first = ranges[0]
    return first if isinstance(first, dict) else {}


class AkedataSource(ActivitySource):
    """终末地活动与寻访卡池数据源。"""

    source_id = "akedata"
    display_name = "AKEData"

    def __init__(
        self,
        client: JsonHttpClient,
        *,
        api_base: str = DEFAULT_AKEDATA_API_BASE,
        include_gacha_pools: bool = True,
        cache_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._api_base = (api_base or DEFAULT_AKEDATA_API_BASE).strip().rstrip("/")
        self._include_gacha_pools = bool(include_gacha_pools)
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._version_id = ""
        self._game_version = ""
        self._activities: list[Activity] | None = None
        self._source_updated_at = ""
        self.notes: dict[str, str] = {}
        self.versions: dict[str, str] = {}

    def games(self) -> tuple[Game, ...]:
        return (ENDFIELD_GAME,)

    async def fetch(self, game_id: str) -> Sequence[Activity]:
        try:
            manifest = await self._client.get_json(
                f"{self._api_base}/manifest.json",
                cache_key="akedata_manifest",
            )
        except Exception:
            cached = self._load_resolved_cache()
            if cached is None:
                raise
            self.notes[game_id] = "manifest 获取失败，使用缓存数据"
            return cached

        version = self._pick_version(manifest.data)
        if version is None:
            cached = self._load_resolved_cache()
            if cached is None:
                raise ValueError("AKEData manifest 缺少可用版本信息")
            self.notes[game_id] = "manifest 内容异常，使用缓存数据"
            return cached

        version_id = str(version.get("id") or "")
        if (
            version_id
            and version_id == self._version_id
            and self._activities is not None
        ):
            self.notes[game_id] = ""
            return self._activities

        if not version_id:
            cached = self._load_resolved_cache()
            if cached is None:
                raise ValueError("AKEData manifest 缺少版本 ID")
            self.notes[game_id] = "manifest 内容异常，使用缓存数据"
            return cached

        try:
            activities = await self._fetch_version(version)
        except Exception:
            cached = self._load_resolved_cache()
            if cached is None:
                raise
            self.notes[game_id] = "表数据获取失败，使用缓存数据"
            return cached

        self._version_id = version_id
        self._game_version = str(version.get("gameVersion") or "")
        self._activities = activities
        self._source_updated_at = str(manifest.data.get("updatedAt") or "")
        self.versions[game_id] = (
            f"{self._game_version} @ {version_id}" if self._game_version else version_id
        )
        self.notes[game_id] = "使用缓存数据" if manifest.stale else ""
        self._save_resolved_cache()
        self._cleanup_cache()
        return activities

    # ---- 版本与表数据 ----

    @staticmethod
    def _pick_version(manifest: Any) -> dict | None:
        if not isinstance(manifest, dict):
            return None
        latest = str(manifest.get("latest") or "").strip()
        versions = manifest.get("versions")
        if not isinstance(versions, list):
            return None
        for item in versions:
            if not isinstance(item, dict):
                continue
            if latest and str(item.get("id") or "") != latest:
                continue
            if item.get("tableCfgPath"):
                return item
        for item in versions:
            if isinstance(item, dict) and item.get("tableCfgPath"):
                return item
        return None

    async def _fetch_version(self, version: dict) -> list[Activity]:
        version_id = str(version.get("id") or "")
        game_version = str(version.get("gameVersion") or "") or version_id
        table_root = (
            f"{self._api_base}/{str(version['tableCfgPath']).strip('/')}"
        )

        activities_data = await self._table(
            table_root, version_id, "ActivityTable.json"
        )
        time_ranges = await self._table(
            table_root, version_id, "TimeRangeTable.json"
        )
        activity_tags = await self._table(
            table_root, version_id, "ActivityTagTable.json"
        )
        # 文本表在同一大版本内按 gameVersion 共享缓存，避免每次 hotfix
        # 都重新下载约 11MB。
        text_table = await self._table(
            table_root,
            f"text_{game_version}",
            "I18nTextTable_CN.json",
        )

        pools: dict = {}
        if self._include_gacha_pools:
            pools = await self._table(
                table_root, version_id, "GachaCharPoolTable.json"
            )

        resolved = self._build_activities(
            activities_data,
            time_ranges,
            activity_tags,
            text_table,
        )
        if self._include_gacha_pools:
            resolved.extend(self._build_pools(pools, time_ranges, text_table))
        return resolved

    async def _table(self, table_root: str, scope: str, filename: str) -> Any:
        result = await self._client.get_json(
            f"{table_root}/{filename}",
            cache_key=f"{CACHE_KEY_PREFIX}table_{scope}_{filename}",
        )
        return result.data if result.data is not None else {}

    # ---- 解析 ----

    def _build_activities(
        self,
        activities: Any,
        time_ranges: Any,
        activity_tags: Any,
        text_table: Any,
    ) -> list[Activity]:
        if not isinstance(activities, dict):
            return []
        time_ranges = time_ranges if isinstance(time_ranges, dict) else {}
        activity_tags = activity_tags if isinstance(activity_tags, dict) else {}
        text_table = text_table if isinstance(text_table, dict) else {}

        records: list[Activity] = []
        for activity_id, activity in activities.items():
            if not isinstance(activity, dict):
                continue
            time_range = _first_time_range(
                activity,
                time_ranges,
                "timeId",
                default_time_id=f"time_{activity_id}",
            )
            end_time = parse_datetime(time_range.get("closeTime"))
            if end_time is None:
                # 常驻内容没有结束时间，不参与提醒
                continue
            start_time = parse_datetime(time_range.get("openTime"))

            tag_names = []
            for tag_id in activity.get("tagIds") or []:
                tag = activity_tags.get(str(tag_id))
                if isinstance(tag, dict):
                    tag_names.append(
                        _resolve_text(tag.get("name"), text_table, str(tag_id))
                    )

            tab_img = str(activity.get("tabImg") or "").strip()
            cover = (
                f"{self._api_base}/{ACTIVITY_IMAGE_PATH}/{quote(tab_img, safe='')}.png"
                if tab_img
                else ""
            )

            records.append(
                Activity(
                    game_id=ENDFIELD_GAME.game_id,
                    activity_id=make_activity_id(
                        "activity",
                        activity_id,
                        start_time.isoformat() if start_time else "",
                    ),
                    name=_resolve_text(
                        activity.get("name"), text_table, str(activity_id)
                    ),
                    start_time=start_time,
                    end_time=end_time,
                    description=_resolve_text(
                        activity.get("desc"), text_table, ""
                    ),
                    cover=cover,
                    tags=tuple(tag for tag in tag_names if tag),
                )
            )
        return records

    def _build_pools(
        self,
        pools: Any,
        time_ranges: Any,
        text_table: Any,
    ) -> list[Activity]:
        if not isinstance(pools, dict):
            return []
        time_ranges = time_ranges if isinstance(time_ranges, dict) else {}
        text_table = text_table if isinstance(text_table, dict) else {}

        records: list[Activity] = []
        for pool_id, pool in pools.items():
            if not isinstance(pool, dict):
                continue
            time_range = _first_time_range(
                pool,
                time_ranges,
                "clientTopTimeId",
                default_time_id=f"time_{pool_id}",
            )
            end_time = parse_datetime(time_range.get("closeTime"))
            if end_time is None:
                continue
            start_time = parse_datetime(time_range.get("openTime"))
            type_name = POOL_TYPE_NAMES.get(
                pool.get("type"), DEFAULT_POOL_TYPE_NAME
            )

            up_characters = [
                str(item) for item in (pool.get("upCharIds") or []) if item
            ]
            records.append(
                Activity(
                    game_id=ENDFIELD_GAME.game_id,
                    activity_id=make_activity_id(
                        "pool",
                        pool_id,
                        start_time.isoformat() if start_time else "",
                    ),
                    name=_resolve_text(pool.get("name"), text_table, str(pool_id)),
                    start_time=start_time,
                    end_time=end_time,
                    description="，".join(up_characters) if up_characters else "",
                    tags=(type_name,),
                )
            )
        return records

    # ---- 缓存 ----

    def _load_resolved_cache(self) -> list[Activity] | None:
        if self._activities is not None:
            return self._activities
        raw = self._client.read_cache(RESOLVED_CACHE_KEY)
        if not isinstance(raw, dict):
            return None
        self._version_id = str(raw.get("version_id") or "")
        self._game_version = str(raw.get("game_version") or "")
        self._source_updated_at = str(raw.get("source_updated_at") or "")
        activities = [
            activity
            for activity in (
                Activity.from_cache_dict(item)
                for item in (raw.get("activities") or [])
            )
            if activity is not None
        ]
        if not activities:
            return None
        self._activities = activities
        return activities

    def _save_resolved_cache(self) -> None:
        if self._activities is None:
            return
        self._client.write_cache(
            RESOLVED_CACHE_KEY,
            {
                "version_id": self._version_id,
                "game_version": self._game_version,
                "source_updated_at": self._source_updated_at,
                "activities": [
                    activity.to_cache_dict() for activity in self._activities
                ],
            },
        )

    def _cleanup_cache(self) -> None:
        """删除除当前版本外的 AKEData 表缓存，避免 11MB 文本表堆积。"""
        if self._cache_dir is None or not self._cache_dir.exists():
            return
        keep = (
            JsonHttpClient.cache_filename("akedata_manifest"),
            JsonHttpClient.cache_filename(RESOLVED_CACHE_KEY),
            JsonHttpClient.cache_filename(
                f"{CACHE_KEY_PREFIX}table_{self._version_id}_"
            ),
            JsonHttpClient.cache_filename(
                f"{CACHE_KEY_PREFIX}table_text_{self._game_version}_"
            ),
        )
        for path in self._cache_dir.glob(f"{CACHE_KEY_PREFIX}*.json"):
            if any(path.name.startswith(prefix) for prefix in keep):
                continue
            try:
                path.unlink()
            except OSError:
                continue

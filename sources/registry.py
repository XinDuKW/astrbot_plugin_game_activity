"""游戏注册表：聚合各数据源提供的游戏，并支持按用户输入解析。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .base import ActivitySource, Game, normalize_token


class GameRegistry:
    """把多个数据源提供的游戏汇总成统一的查询入口。"""

    def __init__(self, sources: Iterable[ActivitySource] = ()) -> None:
        self._games: dict[str, Game] = {}
        self._sources: dict[str, ActivitySource] = {}
        for source in sources:
            self.register(source)

    def register(self, source: ActivitySource) -> None:
        for game in source.games():
            self._games[game.game_id] = game
            self._sources[game.game_id] = source

    @property
    def games(self) -> dict[str, Game]:
        return dict(self._games)

    def get(self, game_id: str) -> Game | None:
        return self._games.get(str(game_id or "").strip())

    def source_for(self, game_id: str) -> ActivitySource | None:
        return self._sources.get(str(game_id or "").strip())

    def source_name(self, game_id: str) -> str:
        source = self.source_for(game_id)
        return source.display_name if source else ""

    def ordered(self) -> list[Game]:
        """按数据源注册顺序返回游戏列表。"""
        return list(self._games.values())

    def resolve(self, token: str) -> list[Game]:
        """把用户输入解析为游戏列表。

        支持 game_id、完整名称与别名；无法精确匹配时会退化为子串匹配，
        例如 ``档案`` 可以匹配到蔚蓝档案系列。
        """
        wanted = normalize_token(token)
        if not wanted:
            return []

        exact = [game for game in self._games.values() if game.matches(token)]
        if exact:
            return exact

        return [
            game
            for game in self._games.values()
            if wanted in normalize_token(game.name)
            or wanted in normalize_token(game.short_name)
            or any(wanted in normalize_token(alias) for alias in game.aliases)
        ]

    def resolve_many(self, tokens: Sequence[str]) -> tuple[list[Game], list[str]]:
        """批量解析，返回 ``(命中的游戏, 未识别的输入)``。"""
        matched: list[Game] = []
        seen: set[str] = set()
        unknown: list[str] = []
        for token in tokens:
            found = self.resolve(token)
            if not found:
                unknown.append(token)
                continue
            for game in found:
                if game.game_id not in seen:
                    seen.add(game.game_id)
                    matched.append(game)
        return matched, unknown

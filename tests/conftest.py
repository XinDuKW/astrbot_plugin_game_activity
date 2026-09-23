"""测试初始化。

1. 把插件目录的父目录加入 ``sys.path``，以便按包名导入；
2. 未安装 AstrBot 时注入最小存根（只提供 ``astrbot.api.logger``），让脱离运行时的
   单元测试也能导入依赖 logger 的模块。**真实环境不会被覆盖** —— 只有在
   ``import astrbot.api`` 失败时才注入，因此插件在 AstrBot 里跑测试时用的仍是真日志器。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PARENT_DIR = PLUGIN_DIR.parent

if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))


class StubLogger:
    """记录日志调用的极简日志器，便于断言某条降级路径确实被记录。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(
        self,
        level: str,
        message: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        text = str(message)
        if args:
            try:
                text = text % args
            except (TypeError, ValueError):
                pass
        self.records.append((level, text))

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("debug", message, *args, **kwargs)

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("info", message, *args, **kwargs)

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("warning", message, *args, **kwargs)

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("error", message, *args, **kwargs)

    def critical(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("critical", message, *args, **kwargs)

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("error", message, *args, **kwargs)

    def messages(self, level: str | None = None) -> list[str]:
        return [
            text
            for recorded_level, text in self.records
            if level is None or recorded_level == level
        ]

    def clear(self) -> None:
        self.records.clear()


STUB_LOGGER: StubLogger | None = None


def _install_astrbot_stub() -> None:
    """仅在真实 AstrBot 无法导入时注入存根。"""
    global STUB_LOGGER

    try:
        import astrbot.api  # noqa: F401
    except Exception:
        pass
    else:
        return

    stub = StubLogger()

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = stub  # type: ignore[attr-defined]
    astrbot_module.api = api_module  # type: ignore[attr-defined]

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    STUB_LOGGER = stub


_install_astrbot_stub()


@pytest.fixture
def astrbot_logs() -> StubLogger | None:
    """返回存根日志器；已安装真实 AstrBot 时返回 ``None``，相关断言应跳过。"""
    if STUB_LOGGER is None:
        return None
    STUB_LOGGER.clear()
    return STUB_LOGGER

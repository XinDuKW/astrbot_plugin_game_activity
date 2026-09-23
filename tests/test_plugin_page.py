"""Plugin Pages 前端资源的结构约束。

AstrBot 返回页面 HTML 时会做两件事，页面写错其中任何一件都会导致
``window.AstrBotPluginPage`` 拿不到、管理页直接显示「未连接 Dashboard」：

1. 把页面内的相对资源地址重写成插件页专用地址；
2. 若 HTML 中不含 bridge SDK 地址，就把 ``<script src="/api/plugin/page/bridge-sdk.js">``
   插到**第一个** ``</body>`` 之前。

因为注入点在页面自身脚本之后，页面脚本必须是 ``type="module"``（默认 defer），
否则经典脚本会在解析到该行时立即执行，那时 bridge 还没定义。同时注释里不能出现
文档结束标签的字面量，否则注入点会被注释抢先命中，SDK 被塞进注释而失效。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE_DIR = Path(__file__).resolve().parent.parent / "pages" / "activity"
BRIDGE_URL = "/api/plugin/page/bridge-sdk.js"
BRIDGE_TAG = f'<script src="{BRIDGE_URL}"></script>'
COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)


def read_html() -> str:
    return (PAGE_DIR / "index.html").read_text(encoding="utf-8")


def emulate_bridge_injection(html: str) -> str:
    """复刻 plugin_page_service.rewrite_plugin_page_html 的注入行为。"""
    if BRIDGE_URL in html:
        return html
    if "</body>" in html:
        return html.replace("</body>", f"{BRIDGE_TAG}</body>", 1)
    return html + BRIDGE_TAG


def strip_comments(html: str) -> str:
    return COMMENT_PATTERN.sub("", html)


def test_page_files_exist():
    assert (PAGE_DIR / "index.html").is_file()
    assert (PAGE_DIR / "app.js").is_file()
    assert (PAGE_DIR / "style.css").is_file()


def test_page_script_is_module():
    """必须是 module（defer），否则会在 bridge SDK 注入前执行。"""
    scripts = re.findall(r"<script\b[^>]*>", read_html())
    assert len(scripts) == 1, f"页面应只有一个 script 标签，实际 {scripts}"
    assert 'type="module"' in scripts[0]
    assert "./app.js" in scripts[0]


def test_comments_do_not_contain_closing_tags():
    """注释里的结束标签会被 AstrBot 当成注入点。"""
    for comment in COMMENT_PATTERN.findall(read_html()):
        assert "</body>" not in comment
        assert "</html>" not in comment


def test_bridge_injected_after_page_script():
    injected = emulate_bridge_injection(read_html())

    page_script_at = injected.find('<script type="module"')
    bridge_at = injected.find(BRIDGE_TAG)
    assert page_script_at >= 0
    assert bridge_at >= 0
    assert page_script_at < bridge_at, "bridge 必须排在页面脚本之后"


def test_bridge_injection_not_swallowed_by_comment():
    injected = emulate_bridge_injection(read_html())
    bridge_at = injected.find(BRIDGE_TAG)
    spans = [match.span() for match in COMMENT_PATTERN.finditer(injected)]
    assert not any(start < bridge_at < end for start, end in spans)


def test_structure_after_stripping_comments():
    stripped = strip_comments(emulate_bridge_injection(read_html()))
    assert f"{BRIDGE_TAG}</body>" in stripped
    assert stripped.count("</body>") == 1
    assert stripped.count("</html>") == 1


def test_no_inline_event_handlers():
    """module 作用域下的函数不会挂到 window，内联事件处理器会失效。"""
    html = read_html()
    assert "onclick=" not in html
    assert "onload=" not in html


def test_app_js_uses_bridge_api():
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert "window.AstrBotPluginPage" in js
    assert "bridge.ready()" in js
    assert 'apiGet("overview")' in js
    assert "apiPost(" in js


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="未安装 node，跳过 ES module 语法校验",
)
def test_app_js_is_valid_module():
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    result = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=js,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

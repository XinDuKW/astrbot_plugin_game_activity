"""推送消息的格式与图片片段。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from astrbot_plugin_game_activity.services.message_formatter import (
    COVER_MODE_IMAGE,
    COVER_MODE_LINK,
    COVER_MODE_OFF,
    DEFAULT_ACTIVITY_SEPARATOR,
    PART_IMAGE,
    PART_TEXT,
    MessagePart,
    MessageSettings,
    build_overview_message,
    build_overview_parts,
    build_reminder_message,
    build_reminder_parts,
    build_single_reminder_message,
    build_single_reminder_parts,
    normalize_cover_mode,
    parts_to_text,
    truncate,
)
from astrbot_plugin_game_activity.services.reminder_service import ReminderPlan
from astrbot_plugin_game_activity.sources.base import Activity, GAME_TIMEZONE
from astrbot_plugin_game_activity.sources.sra import SRA_GAMES

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=GAME_TIMEZONE)
GAME = next(game for game in SRA_GAMES if game.game_id == "sr")
SETTINGS = MessageSettings()
COVER = "https://example.com/cover.png"


def images_of(parts) -> list[str]:
    return [part.value for part in parts if part.is_image]


def make_activity(
    name: str = "测试活动",
    *,
    start: timedelta | None = timedelta(days=-1),
    end: timedelta | None = timedelta(days=5),
    activity_id: str = "a1",
    **kwargs,
) -> Activity:
    return Activity(
        game_id="sr",
        activity_id=activity_id,
        name=name,
        start_time=None if start is None else NOW + start,
        end_time=None if end is None else NOW + end,
        **kwargs,
    )


def make_plan(activity: Activity, reasons=("new",), tier: str = "") -> ReminderPlan:
    return ReminderPlan(
        activity=activity,
        reasons=tuple(reasons),
        tier=tier,
        remaining=activity.remaining(NOW),
    )


def test_truncate_collapses_and_limits():
    assert truncate("  多个   空格  ", 100) == "多个 空格"
    long_text = "字" * 50
    assert len(truncate(long_text, 10)) == 10
    assert truncate(long_text, 10).endswith("…")


def test_cross_year_end_time_shows_year():
    activity = make_activity(
        "跨年活动",
        start=timedelta(days=-100),
        end=timedelta(days=200),
    )
    assert activity.end_time is not None
    assert activity.start_time is not None
    assert activity.end_time.year != activity.start_time.year

    text = build_reminder_message(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert f"开始：{activity.start_time:%m-%d %H:%M}" in text
    assert f"结束：{activity.end_time:%Y-%m-%d %H:%M}" in text


def test_same_year_times_omit_year():
    activity = make_activity("当年活动", start=timedelta(days=-2), end=timedelta(days=3))
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert activity.start_time is not None and activity.end_time is not None
    assert f"开始：{activity.start_time:%m-%d %H:%M}" in text
    assert f"结束：{activity.end_time:%m-%d %H:%M}" in text
    assert str(NOW.year) not in text


def test_cross_year_extra_time_shows_year():
    activity = make_activity(
        "活动",
        start=timedelta(days=-2),
        end=timedelta(days=5),
        extra_times=(("兑换结束", NOW + timedelta(days=150)),),
    )
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, SETTINGS)
    exchange = NOW + timedelta(days=150)
    assert f"兑换结束：{exchange:%Y-%m-%d %H:%M}" in text


def test_build_reminder_message_contains_header_and_labels():
    activity = make_activity(
        "方寸大冒险",
        start=timedelta(days=1),
        end=timedelta(days=3, hours=6),
        cover="https://example.com/c.png",
        tags=("限时活动",),
        extra_times=(("兑换结束", NOW + timedelta(days=7)),),
        description="一段描述",
    )
    text = build_reminder_message(
        GAME, [make_plan(activity, ("new", "d3"), "d3")], NOW, SETTINGS
    )

    assert text.startswith("🎮 崩坏：星穹铁道 · 活动提醒")
    assert "【新活动 · 剩余不足3天】方寸大冒险" in text
    assert "开始：" in text
    assert "结束：" in text
    assert "剩余 3天6小时" in text
    assert "标签：限时活动" in text
    assert "兑换结束：" in text
    assert "封面：https://example.com/c.png" in text


def test_d1_uses_24_hour_label():
    activity = make_activity(end=timedelta(hours=5))
    text = build_single_reminder_message(
        GAME, make_plan(activity, ("d1",), "d1"), NOW, SETTINGS
    )
    assert "【剩余不足24小时】" in text
    assert "剩余 5小时0分" in text


def test_single_reminder_message_repeats_header():
    activity = make_activity()
    text = build_single_reminder_message(
        GAME, make_plan(activity), NOW, SETTINGS
    )
    assert text.count("🎮") == 1


def test_settings_can_disable_optional_fields():
    settings = MessageSettings(
        include_description=False,
        cover_mode=COVER_MODE_OFF,
        include_tags=False,
        include_extra_times=False,
    )
    activity = make_activity(
        "活动",
        cover=COVER,
        tags=("限时活动",),
        description="描述",
        extra_times=(("兑换结束", NOW + timedelta(days=1)),),
    )
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, settings)

    assert "封面" not in text
    assert "标签" not in text
    assert "兑换结束" not in text
    assert "说明" not in text


def test_description_included_when_enabled():
    settings = MessageSettings(include_description=True)
    activity = make_activity("活动", description="很长的描述" * 100)
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, settings)
    assert "说明：" in text
    assert "…" in text


def test_url_rendered_as_detail_link():
    activity = make_activity("活动", url="https://prts.wiki/w/测试")
    text = build_reminder_message(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert "详情：https://prts.wiki/w/测试" in text


def test_overview_groups_active_and_upcoming():
    activities = [
        make_activity("进行中", activity_id="a", start=timedelta(days=-2), end=timedelta(days=3)),
        make_activity("即将开始", activity_id="b", start=timedelta(days=5), end=timedelta(days=20)),
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS, source_name="SRA 公共 API")

    assert "🎮 崩坏：星穹铁道 · 当前活动" in text
    assert "▎进行中（1）" in text
    assert "▎即将开始（1）" in text
    assert "（数据来源：SRA 公共 API）" in text
    assert text.index("进行中") < text.index("即将开始")


def test_overview_shows_remaining_only_for_active():
    activities = [
        make_activity("即将开始", start=timedelta(days=5), end=timedelta(days=20)),
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS)
    assert "剩余" not in text


def test_overview_empty_state():
    text = build_overview_message(GAME, [], NOW, SETTINGS)
    assert "当前没有进行中或即将开始的活动。" in text


def test_overview_orders_active_by_end_time():
    activities = [
        make_activity("晚结束", activity_id="a", start=timedelta(days=-2), end=timedelta(days=9)),
        make_activity("早结束", activity_id="b", start=timedelta(days=-2), end=timedelta(days=2)),
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS)
    assert text.index("早结束") < text.index("晚结束")


def test_overview_limits_list_length():
    activities = [
        make_activity(
            f"活动{i}",
            activity_id=f"a{i}",
            start=timedelta(days=-2),
            end=timedelta(days=10 + i),
        )
        for i in range(20)
    ]
    text = build_overview_message(GAME, activities, NOW, SETTINGS)
    assert "另有 5 个进行中的活动" in text


# ---- 封面模式 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("image", COVER_MODE_IMAGE),
        ("IMAGE", COVER_MODE_IMAGE),
        ("link", COVER_MODE_LINK),
        ("off", COVER_MODE_OFF),
        (True, COVER_MODE_IMAGE),
        (False, COVER_MODE_OFF),
        ("true", COVER_MODE_IMAGE),
        ("false", COVER_MODE_OFF),
        ("图片", COVER_MODE_IMAGE),
        ("链接", COVER_MODE_LINK),
        (None, COVER_MODE_IMAGE),
        ("乱七八糟", COVER_MODE_IMAGE),
    ],
)
def test_normalize_cover_mode(raw, expected):
    assert normalize_cover_mode(raw) == expected


def test_default_mode_emits_image_part():
    activity = make_activity("活动", cover=COVER)
    parts = build_reminder_parts(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert images_of(parts) == [COVER]
    # 图片模式下正文里不再重复封面链接
    assert COVER not in "".join(p.value for p in parts if not p.is_image)


def test_link_mode_emits_text_line_only():
    settings = MessageSettings(cover_mode=COVER_MODE_LINK)
    activity = make_activity("活动", cover=COVER)
    parts = build_reminder_parts(GAME, [make_plan(activity)], NOW, settings)
    assert images_of(parts) == []
    assert f"封面：{COVER}" in parts_to_text(parts)


def test_off_mode_has_no_cover_at_all():
    settings = MessageSettings(cover_mode=COVER_MODE_OFF)
    activity = make_activity("活动", cover=COVER)
    parts = build_reminder_parts(GAME, [make_plan(activity)], NOW, settings)
    assert images_of(parts) == []
    assert COVER not in parts_to_text(parts)


def test_activity_without_cover_produces_no_image():
    activity = make_activity("无封面活动", cover="")
    parts = build_reminder_parts(GAME, [make_plan(activity)], NOW, SETTINGS)
    assert images_of(parts) == []


def test_all_covers_become_parts_without_a_count_cap():
    """图片数量不在这里限制，改由发送层的体积预算决定。"""
    plans = [
        make_plan(
            make_activity(
                f"活动{i}",
                activity_id=f"a{i}",
                cover=f"https://example.com/{i}.png",
            )
        )
        for i in range(6)
    ]
    parts = build_reminder_parts(GAME, plans, NOW, SETTINGS)
    assert images_of(parts) == [
        f"https://example.com/{i}.png" for i in range(6)
    ]
    # 图片模式下正文不出现封面链接
    assert "封面：" not in "".join(p.value for p in parts if not p.is_image)


def test_cover_budget_settings_are_exposed():
    assert MessageSettings().cover_max_total_bytes == 5 * 1024 * 1024
    assert MessageSettings(cover_max_total_mb=0).cover_max_total_bytes == 0
    assert MessageSettings(cover_max_total_mb=-1).cover_max_total_bytes == 0
    assert MessageSettings(cover_max_total_mb=12).cover_max_total_bytes == 12 * 1024 * 1024
    assert MessageSettings(cover_max_total_mb="abc").cover_max_total_bytes == 5 * 1024 * 1024


def test_cover_compression_defaults_on():
    settings = MessageSettings()
    assert settings.cover_compress is True
    assert settings.cover_max_width == 720


def test_assemble_merges_adjacent_text_and_keeps_block_order():
    plans = [
        make_plan(make_activity("甲", activity_id="a", cover="https://e.com/1.png")),
        make_plan(make_activity("乙", activity_id="b", cover="https://e.com/2.png")),
    ]
    parts = build_reminder_parts(GAME, plans, NOW, SETTINGS)

    kinds = [part.kind for part in parts]
    assert kinds == [PART_TEXT, PART_IMAGE, PART_TEXT, PART_IMAGE]
    # 图片之后的文本块以空行开头，保证换行排版
    assert parts[2].value.startswith("\n\n")
    assert "甲" in parts[0].value and "乙" in parts[2].value


def test_single_reminder_parts_structure():
    activity = make_activity("活动", cover=COVER)
    parts = build_single_reminder_parts(
        GAME, make_plan(activity, ("d1",), "d1"), NOW, SETTINGS
    )
    assert [part.kind for part in parts] == [PART_TEXT, PART_IMAGE]
    assert "【剩余不足24小时】" in parts[0].value


def test_overview_parts_carry_images_and_section_titles():
    activities = [
        make_activity("进行中", activity_id="a", cover="https://e.com/a.png"),
        make_activity(
            "即将开始",
            activity_id="b",
            start=timedelta(days=5),
            end=timedelta(days=20),
            cover="https://e.com/b.png",
        ),
    ]
    parts = build_overview_parts(GAME, activities, NOW, SETTINGS, source_name="SRA")
    assert images_of(parts) == ["https://e.com/a.png", "https://e.com/b.png"]

    text = parts_to_text(parts)
    # 分段标题必须紧挨着它自己那一批活动，不能被挤到消息开头
    assert "▎进行中（1）\n\n进行中" in text
    assert "▎即将开始（1）\n\n即将开始" in text
    assert text.index("▎进行中（1）") < text.index("▎即将开始（1）") < text.index("（数据来源：SRA）")


def test_overview_parts_without_source_name():
    parts = build_overview_parts(GAME, [], NOW, SETTINGS)
    assert "数据来源" not in parts_to_text(parts)
    assert "当前没有进行中或即将开始的活动。" in parts_to_text(parts)


def test_parts_to_text_renders_images_as_links():
    parts = [
        MessagePart(PART_TEXT, "文本"),
        MessagePart(PART_IMAGE, COVER),
        MessagePart(PART_TEXT, "结尾"),
    ]
    assert parts_to_text(parts) == f"文本\n封面：{COVER}\n结尾"
    assert parts_to_text([]) == ""


def test_parts_to_text_does_not_double_blank_line_after_image():
    """图片后的续接文本自带空行，不应再多补一个换行。"""
    parts = [
        MessagePart(PART_TEXT, "第一段"),
        MessagePart(PART_IMAGE, COVER),
        MessagePart(PART_TEXT, "\n\n第二段"),
    ]
    assert parts_to_text(parts) == f"第一段\n封面：{COVER}\n\n第二段"
    assert "\n\n\n" not in parts_to_text(parts)


def test_parts_to_text_skips_empty_chunks():
    parts = [
        MessagePart(PART_TEXT, "文本"),
        MessagePart(PART_TEXT, ""),
    ]
    assert parts_to_text(parts) == "文本"


# ---- 活动之间的分隔线 ----


def make_plans(names: list[str], *, with_cover: bool = False) -> list[ReminderPlan]:
    return [
        make_plan(
            make_activity(
                name,
                activity_id=f"id-{name}",
                cover=f"https://e.com/{name}.png" if with_cover else "",
            )
        )
        for name in names
    ]


def test_separator_inserted_between_activities():
    parts = build_reminder_parts(GAME, make_plans(["甲", "乙"]), NOW, SETTINGS)
    text = parts_to_text(parts)
    assert DEFAULT_ACTIVITY_SEPARATOR in text
    # 分隔线紧跟在「甲」那一块之后，紧挨着「乙」的标题
    assert f"\n\n{DEFAULT_ACTIVITY_SEPARATOR}\n\n【新活动】乙" in text


def test_separator_not_before_first_activity():
    parts = build_reminder_parts(GAME, make_plans(["甲", "乙"]), NOW, SETTINGS)
    text = parts_to_text(parts)

    header, _, rest = text.partition("\n\n")
    assert header == "🎮 崩坏：星穹铁道 · 活动提醒"
    # 头部之后直接就是第一个活动，中间没有分隔线
    assert rest.startswith("【新活动】甲")
    assert DEFAULT_ACTIVITY_SEPARATOR not in text.split("【新活动】甲")[0]


def test_separator_count_matches_gaps():
    parts = build_reminder_parts(
        GAME, make_plans(["甲", "乙", "丙", "丁"]), NOW, SETTINGS
    )
    text = parts_to_text(parts)
    assert text.count(DEFAULT_ACTIVITY_SEPARATOR) == 3


def test_single_activity_has_no_separator():
    parts = build_reminder_parts(GAME, make_plans(["独苗"]), NOW, SETTINGS)
    assert DEFAULT_ACTIVITY_SEPARATOR not in parts_to_text(parts)

    single = build_single_reminder_parts(
        GAME, make_plan(make_activity("独苗")), NOW, SETTINGS
    )
    assert DEFAULT_ACTIVITY_SEPARATOR not in parts_to_text(single)


def test_empty_separator_falls_back_to_blank_line():
    settings = MessageSettings(activity_separator="")
    parts = build_reminder_parts(GAME, make_plans(["甲", "乙"]), NOW, settings)
    text = parts_to_text(parts)
    assert DEFAULT_ACTIVITY_SEPARATOR not in text
    assert "\n\n【新活动】乙" in text


def test_custom_separator_is_used():
    settings = MessageSettings(activity_separator="------")
    parts = build_reminder_parts(GAME, make_plans(["甲", "乙"]), NOW, settings)
    assert "\n\n------\n\n【新活动】乙" in parts_to_text(parts)


def test_separator_lands_after_the_previous_cover_image():
    parts = build_reminder_parts(
        GAME, make_plans(["甲", "乙"], with_cover=True), NOW, SETTINGS
    )
    kinds = [part.kind for part in parts]
    assert kinds == [PART_TEXT, PART_IMAGE, PART_TEXT, PART_IMAGE]
    # 第二个文本片段（图片之后）必须以分隔线开头
    assert parts[2].value.startswith(f"\n\n{DEFAULT_ACTIVITY_SEPARATOR}\n\n")


def test_separator_not_inserted_after_section_heading():
    activities = [
        make_activity("进行中1", activity_id="a1"),
        make_activity("进行中2", activity_id="a2"),
        make_activity(
            "即将开始1",
            activity_id="b1",
            start=timedelta(days=5),
            end=timedelta(days=20),
        ),
    ]
    parts = build_overview_parts(GAME, activities, NOW, SETTINGS, source_name="SRA")
    text = parts_to_text(parts)

    # 分段标题紧跟它自己的第一个活动，不能被分隔线割开
    assert "▎进行中（2）\n\n进行中1" in text
    assert "▎即将开始（1）\n\n即将开始1" in text
    # 只有「进行中1 / 进行中2」这一处插入分隔线
    assert text.count(DEFAULT_ACTIVITY_SEPARATOR) == 1
    assert f"\n\n{DEFAULT_ACTIVITY_SEPARATOR}\n\n进行中2" in text


def test_separator_no_extra_blank_lines():
    parts = build_reminder_parts(
        GAME, make_plans(["甲", "乙"], with_cover=True), NOW, SETTINGS
    )
    text = parts_to_text(parts)
    assert "\n\n\n" not in text

# AGENTS.md

## 项目说明

这是一个 AstrBot 游戏活动日历插件：聚合 SRA 公共 API、AKEData（终末地）与
PRTS（明日方舟）的活动数据，按会话订阅推送到群聊 / 私聊，提供新活动与剩余
7 天 / 3 天 / 24 小时分级提醒，并带关键词过滤和 Plugin Pages 管理页。

修改代码前先读相关模块与测试，优先复用已有服务，不要把解析、决策或发送逻辑
重复实现一遍。

## 代码结构

- `main.py`：配置读取、服务组装、插件生命周期、AstrBot 指令入口。
- `activity_webui.py`：Plugin Pages 后端接口。
- `sources/base.py`：活动模型、游戏定义、时间解析、带 ETag 与磁盘缓存的 HTTP 客户端、限速器。
- `sources/sra.py`：SRA 公共 API（8 款游戏）。
- `sources/akedata.py`：AKEData（终末地活动与寻访卡池）。
- `sources/prts.py`：PRTS Semantic MediaWiki 查询（明日方舟）。
- `sources/registry.py`：游戏注册表与用户输入解析。
- `services/reminder_service.py`：提醒决策纯逻辑（档位、去重、关键词过滤）。
- `services/subscription_service.py`：订阅与提醒状态的 KV 持久化。
- `services/message_formatter.py`：推送文本格式化与封面默认值（纯字符串处理）。
- `services/cover_service.py`：封面下载、压缩与体积预算挑选。
- `services/delivery_service.py`：消息发送（把片段转成 AstrBot 消息组件）。
- `services/polling_service.py`：轮询编排（拉取 → 决策 → 投递 → 落盘）。
- `pages/` 与 `.astrbot-plugin/`：管理页前端与 i18n。
- `tests/`：回归测试，不参与插件运行。

不要把 Service 的业务逻辑堆回 `main.py`。指令参数解析与 `AstrMessageEvent`
结果生成可以留在主类里。

## 分层与可测试性

`astrbot` 只在 `main.py`、`activity_webui.py`、`services/cover_service.py`、
`services/delivery_service.py` 与 `services/polling_service.py` 中出现。
**新增逻辑时优先放进不依赖 astrbot 的模块**；纯逻辑模块（`reminder_service`、
`subscription_service`、`message_formatter`、`sources/*`）不得引入 astrbot。

`services/__init__.py` 刻意不做统一再导出：一旦它导入了 `delivery_service`，
所有 `services.*` 子模块的导入都会连带要求 astrbot 存在。

`tests/conftest.py` 在 `import astrbot.api` 失败时注入只含 `logger` 的最小存根，
因此 `pytest` 既能在 AstrBot 环境里跑，也能在裸 Python 里跑；**真实环境不会被覆盖**。

`DEFAULT_COVER_MAX_WIDTH` / `DEFAULT_COVER_MAX_TOTAL_MB` 定义在 `message_formatter`
（它们属于消息格式设置的默认值），`cover_service` 复用它们。不要把这两个常量搬回
`cover_service`——那会让纯逻辑的 `message_formatter` 反向依赖需要 astrbot 的模块。

## 日志

**日志器必须且只能从 `astrbot.api` 导入（`from astrbot.api import logger`），严禁使用
Python 内置 `logging`。** 这是上架审查的硬性要求，所有模块（含数据层与工具层）都要遵守。

`tests/test_cover_service.py::test_no_plugin_module_imports_builtin_logging` 扫描全插件
源码兜住这条规则，`test_cover_service_uses_astrbot_logger` 断言 `cover_service.logger`
就是 `astrbot.api.logger`。测试自身的存根日志器（`tests/conftest.py`）不受此限制。

## 提醒语义（改动前必读）

- 「新活动」以**会话**为粒度：活动第一次出现在该会话视野中才算新。订阅瞬间已存在
  的活动由 `SubscriptionService.build_initial_states` 标记为已知，不得重复推送。
- 「倒计时」按结束时间计算，档位只能**单向变紧急**（`is_more_urgent`）。跳跃式发现
  （例如首次看到时只剩 5 小时）只推最紧急档位，不补发宽松档位。
- 同一活动同一轮的多类提醒必须合并成一条消息（`ReminderPlan.reasons`）。
- **只有成功送达的提醒才能写入状态**。`deliver_plans` 返回实际送达的列表，
  调用方据此更新状态；失败的要留到下一轮重试。
- `record_plan` 不得用只含 `end` 的空条目占位，否则活动会被误判为已处理。
- 数据源失败时必须整轮跳过该游戏，保留上一轮状态，不能因为拿不到数据就清理提醒状态。

## 数据源约定

- 所有时间统一转成东八区 aware datetime（`GAME_TIMEZONE`，固定 +08:00 偏移，
  避免 Windows 缺少 tzdata 时 `zoneinfo` 加载失败）。
- 活动必须有 `end_time` 才进入提醒流程；没有结束时间的常驻内容一律跳过。
- `activity_id` 必须跨轮询稳定，且能让「同名但不同档期」的周期活动区分开
  （SRA 用「名称 + 开始时间」，PRTS 用页面标题，AKEData 用配置表 ID + 开始时间）。
- 新增数据源要继承 `ActivitySource`，在 `main.py` 的 `_build_sources()` 注册，
  并补充解析测试（用 `tests/helpers.py` 的 `FakeClient`，不要打真实网络）。
- 不得引入除 `httpx` 与 `pillow` 以外的新依赖——这两个都是 AstrBot 本体已经依赖的
  （见 AstrBot 的 `requirements.txt`），新增依赖要先确认这一点。

## 体积与限流

- AKEData 的文本表约 11.7 MB，只在**游戏大版本**变化时下载；hotfix 不触发。
  不要为了省事改成每轮全量拉取。
- PRTS 有 WAF，连续请求会被 403。必须保留最小请求间隔与缓存回退，不要提高轮询频率。
- 任何新的外部请求都要走 `JsonHttpClient`，以复用缓存与回退逻辑。

## Plugin Pages 前端约束（踩过坑，务必遵守）

AstrBot 返回页面 HTML 时会做两件事（见 `astrbot/dashboard/services/plugin_page_service.py`）：

1. 把页面内的相对资源地址重写成插件页专用地址；
2. 若 HTML 中不含 bridge SDK 地址，就把
   `<script src="/api/plugin/page/bridge-sdk.js"></script>` 插到**第一个**
   `</body>` 之前。

由此产生两条硬性约束，违反会导致管理页显示「未连接 Dashboard」：

- **页面脚本必须写成 `<script type="module">`**。注入点在页面自身脚本**之后**，
  模块脚本默认 defer，会等文档解析完、SDK 执行后再运行；写成普通经典脚本会在解析
  到该行时立即执行，此时 `window.AstrBotPluginPage` 尚未定义。
- **HTML 注释里不得出现 `</body>` 或 `</html>` 字面量**。AstrBot 用「替换首个
  `</body>`」定位注入点，注释里先出现的那一个会被命中，SDK 会被塞进注释而失效。

`tests/test_plugin_page.py` 复刻了 AstrBot 的注入逻辑来做回归校验，改动 `pages/`
下任何文件后都要跑它，不要绕过或删除这些断言。

前端还应注意：module 作用域下的函数不会挂到 `window`，因此不能使用
`onclick=` / `onload=` 这类内联事件处理器，必须用 `addEventListener`。

## 消息构建与图片

消息按**片段**（`MessagePart`）构建，而不是直接拼字符串：

- `message_formatter` 只产出 `MessagePart` 列表（文本 / 图片），是纯逻辑，**不决定**
  哪些封面真的发得出去；每个带封面的活动都会得到一个图片片段；
- `cover_service` 负责下载、压缩（Pillow，720px JPEG）与体积预算挑选；
- `delivery_service` 把片段解析成发送项再转成 AstrBot 组件
  （`Comp.Plain` / `Comp.Image.fromBytes` / `Comp.Image.fromURL`），是唯一碰组件的地方。

由此得到几条必须保持的性质：

- **图片模式下正文不得再重复封面链接**；链接只出现在 `cover_mode=link`、封面超出体积
  预算、或图片组件构建/发送失败时。
- **超出 `cover_max_total_mb` 预算的封面必须降级为链接，不能静默丢弃**
  （`select_within_budget`）。
- **含图消息发送失败要退化为「文字 + 链接」重试一次**（`send_parts`）。提醒是否算
  送达以这次重试的结果为准，失败就必须留到下一轮。
- **压缩是"全部带图"能落地的前提**：不做压缩时星铁一并发 7 张就是 6.5 MB。不要为了
  省一次下载就砍掉压缩。
- Pillow 缺失或单张下载/压缩失败时必须**逐张降级**（该张改用远程 URL 或链接），
  不能让整条消息失败。
- `parts_to_text()` 是 WebUI 预览与测试用的文本渲染，图片渲染成 `封面：<url>`。
  续接文本片段自带前导空行，渲染时不要再补分隔符，否则会多出空行。
- 分段标题（`▎进行中（N）`）必须紧挨它自己那批活动。`_assemble` 的 `segments`
  参数就是为了保住这个顺序：字符串是独立文本行，列表是活动块。
- `activity_separator` 只插在**相邻活动块之间**：`after_heading` 为真的那一块（紧跟标题的
  第一个活动）以及整条消息的第一个活动块都不加线。改动 `_assemble` 时不要破坏这两条，
  否则标题会和自己的活动被割开，或消息开头出现一条悬空的分隔线。

## 会话级覆盖

只有三项设置支持会话级覆盖，都在 `normalize_session` 里白名单化：

- `thresholds` → `resolve_policy`
- `notify_new` → `resolve_policy`
- `cover_mode` → `resolve_message_settings`

`None` 表示「跟随全局默认」，必须与「显式设为关闭/空」区分开（`thresholds=[]` 是关闭，
`None` 是跟随）。**压缩参数与体积预算不支持会话级覆盖**——那是机器资源开销，由全局
配置统一决定。

Web API 校验枚举值时**不要用 `normalize_cover_mode`**：它会把未知值静默回退成默认，
使非法输入被固化成看似合法的覆盖值。应当直接比对 `COVER_MODES` 并返回 400。

## 改名 / 迁移的坑（踩过，务必检查）

AstrBot 的 `cmd_config.json` 里有一项 `plugin_set`（后台「配置 → 插件配置 → 可用插件」），
它是**「哪些插件允许响应」的白名单**，在唤醒检查阶段就按它过滤处理器
（`core/pipeline/waking_check/stage.py`）。值为 `["*"]` 表示全部启用。

**插件改名后，如果用户的 `plugin_set` 是显式列表，里面仍会是旧插件名**，新名字不在列表中
→ 该插件的所有指令**静默失效**：不回复、不报错，而日志里插件显示 `Loading plugin ...`
且初始化输出一切正常（极易误判成插件 bug）。`plugin_set` 同时被 `core/astr_main_agent.py`
与 `core/cron/manager.py` 使用，因此 LLM 工具与定时任务也会一起被过滤。

**重命名插件时必须同步提醒用户更新 `plugin_set`**；README 的常见问题里已记录该排查项。

## 兼容要求

- 保持现有配置键、默认值与分组结构，除非任务明确要求破坏性调整。
- 修改公开配置时同步更新 `_conf_schema.json` 与 README。
- 保持 KV 键（`game_event_due_subscriptions_v1` / `game_event_due_notify_state_v1`）
  与数据结构，除非提供迁移方案。
- 缺少 Plugin Pages API 时插件主体仍应能加载（`activity_webui` 导入失败要降级）。

## 修改原则

- 保持改动范围紧凑，不顺便做无关重构。
- 优先修行为问题，再考虑抽象与风格。
- 新增用户功能时补充正常路径、失败路径与兼容路径的测试。
- 版本号与 CHANGELOG 仅在任务明确要求发布时修改。

## 输出语言

- 所有审查结论、问题说明、风险分析与修改建议使用简体中文。
- 文件路径、代码标识符、API 名称、配置键、指令与日志内容保持原文。

## 验证

Python、配置或业务逻辑变化时运行：

```bash
pytest -q
ruff check --select F,E9,B main.py activity_webui.py sources services tests
python -m compileall -q main.py activity_webui.py sources services tests
python -m json.tool _conf_schema.json
```

涉及数据源解析或提醒语义的改动，除单元测试外还应联网跑一次真实数据校验，确认
三个数据源都能返回预期的活动条数与起止时间。测试失败时不要宣称修改已完成。

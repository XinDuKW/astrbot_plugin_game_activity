"""游戏活动日历插件的服务层。

各子模块按职责拆分，其中不依赖 astrbot 的纯逻辑模块（``reminder_service``、
``subscription_service``、``message_formatter``、``polling_service`` 的决策部分）
可以脱离 AstrBot 运行时单独测试，因此本文件不在这里做统一再导出。
"""

# 第 3 批：请求上下文

验收通过：相关 pytest、静态检查及实际本地请求验证后独立合并。不做性能对照。

唯一时钟来源为执行主机操作系统，明确提供 UTC、本地日期/时间、偏移和时区来源。
未假定用户所在地，未把时区缩写冒充 IANA 地区。历史用户消息、工具结果与源日期不重写。

API 计数前加入一个 request-only system 观察；每个生成捕获一次，安全重试复用。
本地模型、Zen、Responses、工具续轮、评审和 compaction 经过原 ApiProvider。
浏览器共用相同 renderer，各条新发消息携带观察，原远端历史保持不变。
没有在任务 prompt、工具观察与 provider 同时注入三份日期。

参考 Codex `codex-rs/core/src/session/time_reminder.rs:104` 的时钟读取，以及
`codex-rs/core/src/context/world_state/environment.rs:35` 的日期/时区字段；
没有搬入其提醒状态机。本项目在已有最终 payload 计数边界完成封装。
OpenCode `packages/core/src/session/system-prompt.ts:9` 按可见工具产生指导的做法，
Codey 已由冻结 TurnSnapshot 与 kernel_prompt 实现，本批不再增加第二份工具名单。

验证：

- 初始请求、工具续轮的 Completions/Responses payload 各只有一次时钟观察；计数与
  发送完全相同，历史日期保留。评审、compaction 和浏览器发送有覆盖。
- 第一组相关 14 文件：78 passed。最终 API/浏览器/压缩相关选择：234 passed、
  5 subtests passed；选择有重叠，不相加。
- Ruff、mypy（513 源码文件）、Pyrefly、ty 通过，目标 Python 3.11；Pyrefly 的
  29 抑制项与 80 未展示警告保留。
- 实机原生/文本各一次重定向读取通过；共 6 条实际生成请求，初始与续轮均只有一个
  `<request_context>`。原始记录与校验在
  `artifacts/request-context-live-20261024-01/clock-check.json`。

现有 provider 单测的纯字符串/数组索引断言已更新为显式检验时钟观察和原文同时存在；
调用次数、timeout、工具 call/result 顺序和 compaction 指令隔离断言仍保留。

# API 请求可靠性边界

本轮把请求可靠性拆成共享 API 传输层和 Zen 合作策略层。共享层不导入 Zen 类型；Zen 只提供请求登记、并发、冷却和合作接口错误分类。

## 生命周期

一次逻辑请求在 `_exchange()` 的网络准备完成后登记一次，由同一个 `PendingRequest` 覆盖排队、实际尝试、冷却等待、协议解码和最终结算。每个 HTTP 尝试单独租借 `RequestLease`，释放名额后才进入下一次等待。逻辑请求结束时才注销登记，因此重试不会绕过 `max_pending`。

`AttemptBudget` 同时限制外层 HTTP 尝试和发送前 TLS 重连。请求开始写入后若结果未知，传输层会保留未知状态并禁止自动重发。

## 取消与正文

`AttemptControl` 登记连接和响应对象，覆盖建连、请求写入、响应头和正文。Stop 会关闭当前 socket；连接工作槽有界，取消不会无限留下 DNS/建连线程。正文读取通过 `BoundedBodyReader`，按实际收到的 bytes 更新 `last_byte_at`，并分别受响应总 deadline 与正文静默期限约束。进入正文阶段会重置计时；HTTP 错误正文也使用同一控制器。

SSE 必须满足既有终止条件。收到 `[DONE]` 或 Responses 的完成事件只表示协议答案完整；HTTP framing 未消费完时连接不会回池，但已解码答案不会因此重发。

## Zen 冷却与分类

Zen 的共享协调器按 base URL、合作身份和协议版本建立作用域。额度、资格和未知错误关闭当前请求；只有合作接口明确的 `RateLimitError` 短期限流组合允许在同一模型上按 `Retry-After` 继续。`FreeUsageLimitError` 等日/生命周期额度错误不会按 429 子串猜测为可重试。

provider 最后一个引用释放时，未到期或已到期但尚未完成 probe 的冷却状态仍保留，后续批次先进行单次探测。探测成功只结算匹配版本，迟到的旧成功不能清除更新后的冷却。连接池空闲连接在最后一个 session 引用释放时关闭。

## 连接池和代理

Zen 的 `ApiHttpSession` 按目标、代理、身份和 TLS 配置隔离连接。完整 `Content-Length` 或 chunked framing 才能复用；`Connection: close`、取消、超时、断流和排空失败直接销毁。环境代理发现与绕过规则沿用标准库；普通 HTTP 代理使用绝对目标，HTTPS 代理认证只放在 CONNECT 请求，绝不转发到目标服务。

Local 默认不创建共享池，仍使用原有传输语义。删除 Zen 时，共享取消、正文保护和请求预算仍可由其他 API 使用；没有消费者的 Zen 专用注册和资源可以一起删除。

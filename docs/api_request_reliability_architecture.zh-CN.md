# API 请求可靠性边界

共享 API 层拥有逻辑请求、尝试预算、取消和有界正文读取；连接器提供自己的传输、身份和错误策略。共享层不导入 Zen，也不根据模型名称切换厂商行为。

## 逻辑请求与物理尝试

一次逻辑请求在 `_exchange()` 的网络准备完成后登记，由同一个 `PendingRequest` 覆盖排队、尝试、冷却、协议解码和结算。每次物理尝试单独租借 `RequestLease`，结束后释放；重试不会绕过 `max_pending`。`AttemptBudget` 限制物理尝试及获准的提交前重连。

Gate recorder 区分逻辑发送、物理尝试和重试；同一路径的嵌套 recorder 不重复包裹。HTTP 状态、上游 request ID 和原因码进入有界失败事实，不记录请求密钥或完整头。

## 取消、截止时间与正文

`AttemptControl` 登记连接、原始 socket、响应或可关闭的进程桥接对象。Stop 关闭当前资源；工作槽有界，排队和响应头等待也受截止时间约束。普通 HTTP 不加载 Windows TLS 证书库；HTTPS 真正使用时才准备 TLS context，避免短 HTTP 测试的预算被冷启动证书加载消耗。

正文通过 `BoundedBodyReader` 按实际 bytes 更新静默时钟，同时遵守总生成 deadline。持续到达的零碎 bytes 不能无限延长 JSON/SSE 等待；错误正文同样有长度、framing 和时间限制。

异步交接后，未收到 submitted 标记不等于没有发送。没有确定证明时保持 unknown；只有明确的提交前连接失败可以判定 not_sent。截断、取消和发送后结果未知都不自动重放。

## SSE 调用身份

`api_stream.py` 独立拥有 SSE 解码。显式 index、完整 call ID 或唯一未歧义的已有调用可以定位片段；完整 ID 不拼接。并行调用无法明确定位时失败关闭，不猜编号。协议完成事件与 HTTP framing 的完整消费是两种证据，不能互相替代。

## Zen 自有传输与策略

`zen/node_http.py` 和 `node_http.mjs` 通过 Node fetch 执行一次物理 POST，并用有界 IPC 交付响应头、正文和提交证据。Node 使用现有 Playwright 附带运行时或 PATH 中的 Node；子进程归连接器所有，取消后终止，不执行项目/Shell 工具。当前每个尝试使用独立进程，不宣称复用桌面版长期连接池。

`zen/connection.py` 注入通用 opener 和 `request_id_headers`，包括 `X-OpenCode-Log-Id`。合作身份、目录、usage 解释与重试分类都在 Zen 包内。connection revision 不再附日期，但冻结选择的版本校验仍保留；旧版本任务不会被静默改写。

Zen 协调器按连接、身份和协议建立作用域，保留并发、冷却和单次探测规则。策略最多六次物理尝试（包括首次），并受逻辑请求总预算约束。只允许明确短期限流、已证实的提交前网络失败，以及完整 HTTP 429 / server_error / 空 code / 精确消息 `Upstream request failed: Endpoint is unavailable.` 的有界重试。额度、资格、其他未知 429 和发送后未知结果不自动重试；遵守 Retry-After 和有限退避。错误被允许重试不证明模型一定可用。

## 共享 Python 传输与可移除边界

`ApiHttpSession` 仍是共享 Python 传输可显式使用的连接池；当前 Zen Node opener 不通过该池发送。Python 池只有完整消费、可复用 framing 才回池，取消、断流、超时或脏连接销毁；标准库代理发现及 CONNECT 身份隔离仍属于共享层。

Local 默认保持 Python HTTP，不因 Zen 增加 Node 依赖。未来在线 API 使用自己的连接器配置和策略，可复用同一共享层。MJS 打包和 CI 语法检查通用发现实际资源；删除 Zen 注册/目录时同时清理其专用测试、gate、清单和文档，不修改共同工具执行器。

## 验证与限制

见 [当前测试报告](../TEST_REPORT.md)、[传输/CI 诊断](provider_ci_diagnosis_2026-10-13.zh-CN.md)和[维护审计](ui_maintenance_audit_2026-10-13.zh-CN.md)。测试包含真实回环 socket、零碎帧、Stop、Node 提交标记延迟、明确连接拒绝和无 Zen 的 fresh-process Local 协议。

实机免费矩阵初次 36/42 通过；六项复测中四项恢复，两项 Ling thinking=true 仍未解决。不能把客户端差异直接等同于模型永久故障，也不能用确定性回归宣称全部 free 模型已通过。

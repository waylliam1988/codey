# Zen 合作连接诊断与修复（2026-11-05）

用户确认 Codey 与 OpenCode 继续使用 `public` 合作接口。此次排查没有更换账号、密钥、
模型或合作认证身份。不能把 `FreeUsageLimitError` 文案直接当作用户真实额度耗尽的证明。

## 对照结果

全部生成探针使用简单的 OK 请求或独立测试目录，没有发送用户项目源码。

| 对照 | 实际结果 |
| --- | --- |
| 原 Codey：Node fetch、短系统提示词 | 429 `FreeUsageLimitError` |
| 官方 CLI 2.2.5、现有 VPN 出口 | 同样返回 429 |
| 官方 CLI 2.2.5、本机物理网卡直连 | 200，回复 OK |
| 同一成功请求的头和正文，经 Node fetch 发出 | 429 |
| 同一成功请求经 Bun fetch 发出 | 200，回复 OK |
| Bun、Codey 原有提示词和工具声明 | 429 |
| Bun、Codey 原提示词加真实 `<env>` 信息，工具声明保持原样 | 200，回复 OK |
| 上一组合保留原 `opencode/1.19.3` 合作身份 | 仍然成功 |

这些是当前接口的控制实验，不是上游私有资格判断代码的完整说明，也不能证明某个具体
TLS 字段就是唯一原因。它们确认了传输兼容、环境封装与网络出口三个独立因素。
上游的 `Retry-After` 也不能保证只靠等待恢复。

## 生产变更

- Zen 的单次 fetch 进程改用 Bun，保留共享 API 协议、流解析、计量、取消与调用身份。
  安装 Bun 后可由 PATH 或标准 `~/.bun/bin` 目录发现；缺少运行时明确报未提交，
  不回退到已实测不兼容的 Node。CI 使用固定 Bun 1.4.4。
- 系统提示词补充实际运行平台和工作区解释。它不复制其他客户端的工具目录、工作目录
  或角色声明，不把宿主进程 cwd 冒充用户工作区，不增加任务授权。
- 安装中的旧 Zen 包装层还缺少内核已在使用的 `tool_call_observations`、
  `tool_call_history_groups` 和 `output_token_limit` 透传，会在网络请求前报 TypeError。
  已同步当前包装层的透传；默认的非强制调用不传递旧 API 运行时尚不支持的
  `require_tool_call=False`，显式强制调用仍需支持它的运行时，不会静默降级。
- Windows 每个逻辑请求先使用现有 VPN/网络。只有完整的
  `429 FreeUsageLimitError` 或确认未提交的网络连接错误，才允许在同一尝试预算内回退一次。
  已返回内容、结果不明、认证失败、缺少运行时均不获得这项回退资格。
- 直连使用临时、带随机认证的 loopback CONNECT 隧道。它只接受该请求的批准 OpenCode
  主机和 443 端口，TLS 保持端到端；DNS 与 TCP socket 绑定所选物理网卡。
  网卡配置通过只读查询取得，短期缓存；不调用 VPN 控制接口，不写 VPN 配置，不改
  系统代理、DNS 或路由。逻辑请求结束、取消或失败时关闭隧道及连接。
- `RequestLease` 仅增加通用的逐次传输选项。回退仍经过原请求的队列、预算、期限和
  HTTP 观察入口；没有另起模型任务，也不会重复提交历史。
- 连接 revision 与请求 profile 更新；旧的冻结连接不能被静默改写。

诊断中曾临时添加的 Clash 定向规则已按用户要求撤回。运行配置、应用生成配置和订阅规则
三份文件均已验证与修改前的原始字节一致。最终实现不依赖这些规则或额外的 Mihomo 进程。

## 验证范围

本地原始证据位于忽略目录 `.e2e-artifacts/zen-diagnosis-20261105/`：

- `minimal-production-controls.json`：Bun/Node 与环境封装的生产 provider 对照。
- `fixed-production-live.json`：Big Pickle 与 DeepSeek 的真实回复。
- `routing-fix.json`：临时规则的完整撤回验证。
- `native-fallback-production-live.json`：正常 VPN 首次拒绝、原生直连回退成功。
- `vpn-first-production-gate/`：正式 headless 聊天、读取 fixture、结果回传与完成记录；
  聊天 1 个逻辑请求、2 次 HTTP 尝试；读取 2 个逻辑请求、4 次 HTTP 尝试，均通过。
- `active-service-smoke.json`：当前运行的修复版服务实际完成 Big Pickle 聊天，
  状态 `completed`、答案 `ZEN_OK`、无待清理操作。原设置仅启用 Beam；
  Beam 的直连请求被上游以 `403 RegionError` 拒绝，因此保留 Beam 并另行启用 Big Pickle。
  连接不会在用户不知情的情况下自动替换所选模型。

确定性回归覆盖 VPN 成功不直连、每请求重新优先 VPN、一次回退后停止、结果不明不重发、
认证错误不回退、取消及子进程关闭、隧道认证和主机限制、DNS fake-IP 拒绝及可选 Zen 拆除。
这不是全模型发布矩阵，也不保证任意地区、任意后续上游策略或未来网络环境下持续可用。

# Cline 集成实施进度

开始：2026-11-05。用户授权实现、相关范围回归、实机免费模型测试、分批提交与推送 GitHub；不运行全量 pytest。

## 约束与决策

- 以 `cline-integration-implementation-plan.zh-CN.md` 为设计基准，实际接口差异在本文记录。
- Cline 生产逻辑只在独立包；共享层通过注册、可选认证/codec/stream 扩展调用。
- Windows 凭证安全存储，不明文回退；测试注入独立存储，不写生产账号。
- 真实免费目录与模型能力逐项核对；未验证能力不虚构思考档位。
- 根因分类区分生产缺陷、旧测试假设和环境/上游限制。

## 阶段

1. [完成] 核对仓库和 Cline 协议、通用连接操作与状态生命周期。
2. [完成] Cline 凭证/OAuth/目录/准入/运行链路。
3. [完成] codec/SSE/工具调用/压缩和取消，扩大共享回归。
4. [完成] 设置页认证与思考三态，清理与缺包验收。
5. [完成] 实机测试、相关回归、ruff、三种 Python 类型检查、前端检查与构建。
6. [完成] 用户文档、验收报告与脱敏摘要已同步；实现提交已推送并核对 origin/master。

## 已检查

- 初始分支 master，origin 为 waylliam1988/codey；仅用户方案文档未跟踪。
- 仓库无适用 AGENTS.md；现有 Zen 缺包测试使用新进程 import blocker + Local 两协议 + 内核工具/审批 + 历史读取。
- 方案方向成立；需落实跨进程 refresh 互斥与 logout/admission 同门控。

## 验证记录

- 第一批：连接操作、冷启动状态、模型设置、SSE 工具身份与终止取消、请求策略、上下文预算、operator HTTP 鉴权，71 passed。
- 失败根因：重构默认 SSE accumulator 时改变了已有工具错误文案，4 个现有断言失败。属于本次共享层兼容回归，已恢复原诊断，未放宽测试。
- `ruff check codey tests/test_connection_actions.py` 通过；类型检查进行中。
- 在线免费目录：Beam、Nemotron 3.5 Super VL、Nemotron 3.5 Lightning、Solar Mini 4。精确映射 OpenRouter 能力，不采用 Cline SDK 的通用 128K 回退。
- Cline 专属认证/目录/运行链路：56 passed；完整清理与 Cline/Zen 缺包：7 passed；扩大共享协议、压缩、账本与偏好回归：110 passed。
- 实机授权已完成，凭证在 Windows Credential Manager。`.e2e-artifacts/cline-live-20261105-b/summary.json`：Solar Mini 4 JSON 两轮、SSE 两轮、真实 headless chat/read/edit/shell_approve 全部通过。
- 实机根因：Cline 非流式返回 success/data 包装；包内 codec/usage 解包修复，已加入真实形态回归。
- 新增取消竞态测试揭示共享生产缺陷：取消事件在 decode 完成前到达时，原 commit 只检查 generation。现已在提交门检查 `_is_cancelled()`；测试证实拒绝迟到写入。
- 测试假设修正：通用 live gate 原假定所有非 Local provider 都有 Zen 的 `.runtime` 包装，改为通用可选包装解析；另一个新测试漏算请求级时钟消息，改为断言语义历史，保留生产时钟上下文。
- mypy 全生产树 597 文件通过；Pyrefly/ty 指出的 codec 参数名契约和空容器注解已修正，三检查器全部复跑通过，无新增豁免。
- 相关 pytest：Cline/通用登录/live gate 93 passed，共享 Local/Zen/历史/压缩/审批/鉴权/打包 214 passed，真实浏览器模型/思考 9 passed，设置页布局/保存范围与实际 wheel 打包 32 passed，共 348；没有运行全量 pytest。
- 前端 `npm run check`：TypeScript、Biome 和 293 项 Vitest 通过；Vite 构建与随包前端发布/一致性检查通过。
- Ruff `codey tests tools` 全通过。新增费用阻断重启持久化、损坏缓存拒绝准入、清理失败待办、浏览器迟到失败隔离、外部账号版本更新及磁盘 reasoning_details 往返测试。
- 缺包探针仅移除注册并阻止导入，不修改生产逻辑；Local 双协议、Zen、辅助生成、正式独立审查、工具审批、历史、真实 CLI 与 headless 子进程均通过。补审查夹具时漏传 Local 必需 base_url，按现有契约修正测试，未放宽生产入场检查。
- 实机新增：Beam JSON/SSE 两轮和 read；Nemotron Super JSON/SSE 两轮、read/edit；Lightning JSON/SSE 两轮、read，全部通过。
- 当前公布的 17 个思考选项请求均被服务端接受；这不能证明模型内部实际预算执行程度。原 Beam 上游 Invalid request 本轮未复现，不宣称所有任务均已修复。
- 收尾发现认证版本在后端重启后归零会被仍打开的前端误判为迟到响应；改为 JavaScript 安全整数范围内的微秒时间版本与进程递增计数，避免 Windows 同一时钟刻度内重建 runtime 导致版本倒退。补重启/退出及存储失败/恢复版本断言。加强 OAuth 完成自动发现新模型时仍保留未保存选择的前端测试，293 项复跑通过。

## 提交与后续接续点

- `30dfd2ce`：通用连接操作、状态/codec/SSE/认证扩展边界与初始方案。
- `ae141d4f`：Cline 后端、隔离凭证/刷新/清理/缺包测试和实机工具。
- `0d24feac`：认证重启版本及 Windows 时钟刻度修复，存储错误/恢复版本回归。
- `ec67a625`：通用登录状态 UI、三态思考、草稿保护、浏览器/Vitest 回归与发布前端产物。
- `41a47f8a`：README 双语、能力说明双语、职责地图双语、设计规则、模型管理/清理、请求可靠性、用量、测试索引、TEST_REPORT、变更日志、实施状态及脱敏实机报告。
- 2026-11-05：实现与文档已推送 origin/master；`git ls-remote` 确认远端 HEAD 与本地 `41a47f8a3e41f34ea91f91727c02010e360f9268` 一致，工作树干净。本回执另随最终文档提交同步。
- 收尾只读核对 Cline 认证仍为 ready；未清除用户实机账号。所有清理验证只使用隔离目标。

## 后续核对：Zen 共享取消与 VPN（2026-11-05）

- 用户询问四项诊断文案失败是否涉及 prompt，并要求核对 Zen 是否受共享取消竞态影响。
- Git 差异确认：Cline 实施没有修改 `codey/agents/`、`codey/toolchain/` 或 Zen 包生产代码；四项断言针对 SSE 的 `malformed streamed tool call` 异常字符串，原文案已保留。
- [完成] 新增 Zen 生产工厂、真实 Bun HTTP/两协议 JSON/SSE 的 decode 期间取消回归，覆盖文本、工具调用、解码异常共 12 项。只在隔离 pytest 进程内恢复旧提交条件，12 项全部复现失败；正式代码通过。先前历史和 ledger 版本保持不变，generation 不变，证明测试覆盖任务 Stop 事件而非仅 abandon。
- 新增 VPN 配额响应时 Stop 不启动回退，以及物理直连响应解码期间 Stop 不提交历史且关闭隧道的用例。相关传输、重试、工具权限和审批扩大回归 **70 passed（23.35s）**，Ruff 通过；未运行全量 pytest。
- 实机 DeepSeek V4.1 Flash：当前网络完整 429 FreeUsageLimitError → 物理网卡请求级回退 → 正常返回 ZEN_OK（6.359s）；路由状态清空。Beam 相同回退后返回 403 RegionError，属于该模型上游区域拒绝，未继续换出口或伪造成功。
- 实机前后只读对比：网卡状态、IPv4 路由、DNS 服务端和系统代理快照一致，进程代理环境不变；没有关闭/修改系统 VPN。脱敏证据见 `docs/reports/zen-cancellation-and-routing-20261105.json`。
- 本次只新增测试与更新说明，没有再次修改生产代码或 prompt；提交与推送回执以本轮最终答复为准。

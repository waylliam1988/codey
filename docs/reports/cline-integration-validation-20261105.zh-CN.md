# Cline 集成验收记录

日期：2026-11-05，Windows，本地源码环境。当前可用性以服务端最新免费目录与账号资格为准。

## 实际交付

实现了[实施方案](../cline-integration-implementation-plan.zh-CN.md)的独立 Cline 包，
注册只增加 `API_CONNECTIONS` 一项。共享层使用通用登录操作、动态认证头、codec/usage/
SSE 扩展，不直接导入 Cline，不复制 Cline CLI 的代理系统提示词，不改变 Codey 工具授权。

Settings → Cline → Sign in 在系统浏览器授权，凭证整条写入 Windows Credential Manager。
重复登录复用 attempt；取消、退出和新 attempt 拒绝迟到写入。十线程及两个全新进程的刷新
测试证实轮换只进行一次；账号 epoch 变化拒绝旧任务。登录成功刷新目录，但必须另行勾选并
Save changes，不能自动开启来源。轮询只在设置页存在且 attempt 活跃时执行，保留未保存草稿。

免费资格只看 Cline 目录 `free`，五分钟刷新；能力只看精确 ID/显式别名，一小时缓存。
未知预算的模型不可准入。请求前重新检查资格与冻结能力合同；下架不偷偷换模型。
输入框继续是 `[模型名称] [思考等级]`，三态 default/on/off 保留 null/true/false。
真实协议使用 `reasoning.effort` 或 `reasoning.enabled`，默认不发送覆盖值。

非流式 success/data 包装由包内 codec 和 usage 处理；SSE 合并结构化 reasoning_details、
工具调用和签名。同一协议窗口完整保留元数据，显示只使用可读思考文本，语义重放剥除
不透明字段。辅助压缩保留连接、账号、动态凭证与 codec。缺少 DONE、取消或结果未知不会
提交完整历史，也不重放未知请求。完整 401 只刷新重试一次，429 有界等待，Invalid request 不重试。

意外正数用户 `usage.cost` 阻断后续请求并持久化，重启不能绕过；不把上游内部成本当用户费用。
不新增费用 UI。服务端缺失 token 用量仍为 unknown，不伪造为零。

## 实机结果

用户已完成设备授权。实机请求走生产 Cline 工厂、共享 ApiProvider，以及实际 headless/
工作区内核；代码任务在隔离项目中修改真实文件并运行原测试，没有用假模型替代。

| 当前免费模型 | JSON 两轮 | SSE 两轮 | 生产工具链 | 额外结果 |
|---|---|---|---|---|
| `cline-free/solar-mini4` | 通过 | 通过 | read、edit、shell_approve | 修复折扣计算并运行 unittest；审批后同一运行继续完成 |
| `reflection/beam-free` | 通过 | 通过 | read | 原 Invalid request 未复现 |
| `cline-free/nemotron-3.5-super-vl` | 通过 | 通过 | read、edit | 真实代码修复及验证通过 |
| `nvidia/nemotron-3.5-lightning-free` | 通过 | 通过 | read | 未扩大到该模型的编辑/审批矩阵 |

当前公布的 17 个思考选项逐一被接受：Beam max/xhigh/high/medium/low；
Super 和 Lightning 各 default/on/off；Solar max/xhigh/high/medium/low/minimal。
这证明已公布设置可编译且请求被接受，不证明内部实际推理预算或回答质量。

原始本地证据位于忽略的 `.e2e-artifacts/`：
`cline-live-20261105-b`、`cline-beam-20261105`、`cline-nemotron-super-20261105`、
`cline-nemotron-lightning-20261105`、`cline-reasoning-20261105`。
提交的[脱敏摘要](cline-integration-validation-20261105.json)只保留模型、用例、通过状态和必要断言，
不包含授权材料、账号 epoch、完整请求或回答。

## 根因分类与修复

| 发现 | 分类 | 修复与证据 |
|---|---|---|
| JSON 200 但无法按普通 choices 解码 | 生产适配缺陷 | 解开 Cline success/data；真实返回形态两轮与用量回归 |
| decode 期间取消仍能写历史 | 共享生产竞态 | 提交门检查取消事件；迟到解码测试拒绝历史写入 |
| 后端重启认证版本归零，旧页面保留错误状态 | 生产状态版本缺陷 | 微秒时间版本加进程递增，避免 Windows 同刻度重建时倒退；存储失败和恢复也更新版本 |
| 默认 SSE 重构改变工具身份错误文案 | 本次生产兼容回归 | 恢复原诊断，已有四项断言不放宽 |
| live gate 对所有 API 直接取 `.runtime` | 测试工具沿用 Zen 假设 | 支持有/无包装 provider，真实 Cline headless 编码通过 |
| 新测试把请求时钟上下文当持久消息 | 新夹具错误 | 按语义历史断言，保留生产 request_context |
| 缺包审查夹具没传 Local base_url | 新夹具不满足现有准入合同 | 补正确选择字段，保留生产防止静默换连接检查 |

没有排除失败用例来获得绿色结果；没有把上游未知错误改成成功或无条件重试。

## 自动化验证

按改动依赖分组执行，未运行全量 pytest。当前覆盖 348 项相关 Python/浏览器测试：

- 93：Cline 认证/目录/协议/清理/真实 HTTP/跨进程刷新/缺包、通用操作及 live gate 工具。
- 214：共享 SSE/请求策略/预算、Local/Zen、历史/压缩、模型偏好、审批、路由鉴权、打包和审查。
- 9：真实浏览器模型菜单、每聊天设置、新聊天继承及 default/on/off/default，随包前端字节。
- 32：实际设置页模型管理、两种保存范围、来源操作布局与真实 wheel 打包。

前端 `npm run check` 通过：TypeScript、Biome、53 文件/293 项 Vitest。
Vite 构建、`tools.publish_frontend` 和 `--check` 通过。
Ruff `codey tests tools` 全通过；mypy（597 个生产文件）、Pyrefly、ty 的 Python 3.11
目标均通过。Pyrefly 保留仓库已有 35 suppressed/81 warnings，本次没有增加类型豁免。

清理测试使用内存凭证或独立 Windows 测试目标，不退出生产账号。覆盖凭证、目录、偏好和
显示缓存清理、费用阻断重启保持、损坏缓存阻断、部分失败待办和刷新迟到不回填。
reasoning_details/签名经过磁盘 ledger 写入与读取验证，规范重放不带不透明字段。

缺包验收在新进程仅删除注册并禁止 Cline 导入，不修改生产行为。Local 两协议、工具与
规范化 usage、审批、Zen、辅助生成、正式独立代码审查、历史读取、CLI 启动及真实 headless
子进程通过，父/子进程均没有尝试导入被删包。

## 完整拆除

先停止桌面/backend，运行 `python -m tools.cline_clear_data`。确认成功；若返回
`pending_cleanup`，先处理列出的失败项目。成功才删除独立包与唯一注册项，再清理专属测试、
工具和文档。通用操作、codec/SSE 扩展和 UI 保留。详见[逐项拆除说明](../model-management.md#cline-cleanup-before-removal)。

操作清除 `Codey/Cline/default` 系统凭证、目录/能力和费用阻断缓存、来源偏好及显示缓存。
聊天和运行历史保留，原模型不能继续发送。文件锁载体没有密钥，不与凭证/目录数据混为一谈。
仅删除 Python 包不能证明系统凭证已删除。此次保留真实登录，没有用生产账号演练破坏性清理。

## 与原设计的差异和验证边界

- 首版安全存储仅 Windows；其他平台返回不支持安全存储，不写明文文件。
- 完整清理是通用 clear_data API 加停服维护工具；设置页只提供登录、取消、重开和退出。
- 费用安全阻断增加 `blocked.json` 持久化，纳入退出/清理。
- 压缩、恢复、取消/退出竞态、故障/额度响应依赖确定性测试；未人为耗尽账号真实额度，
  未声称所有模型/任务或上游持续可用，也未以绿色单测代替整个项目的 release gate。
- 本轮没有运行全量 pytest/全项目覆盖率；保留已有 release/其他模型限制，不宣称全仓零缺陷。

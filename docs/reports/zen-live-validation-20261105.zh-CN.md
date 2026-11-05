# Zen 实机验证与两项 P2 修复（2026-11-05）

这次沿用 Cline 的生产工厂与正式 headless 内核验证方式。DeepSeek V4.1 Flash 和
Nemotron 3.5 Super VL 均实际修复独立 fixture 项目、通过原测试，并完成同一 run 的
Shell 审批续接。8 个非区域阻断模型在前半段通过 SSE 两轮记忆验证。
**整张矩阵没有全部通过**：非流式 JSON 被拒绝，后半段 SSE 也开始被拒绝。

脱敏逐项事实、源码指纹及原始摘要哈希见 [JSON 证据](zen-live-validation-20261105.json)。
原始 worker、provider、项目、状态和对照脚本保留在忽略目录
`.e2e-artifacts/zen-live-20261105/`，不提交对话正文或本机地址。

## 当前目录和实测范围

每批强制刷新官方免费目录。当前 10 个模型均为 `openai-completions`；历史上的
Muse、Space Bunny、Ling 已不在本轮可用目录，旧报告不能代表当前可用性。

| 模型 | SSE 两轮 | JSON 两轮 | 真实读取 | 真实修复 / 审批 | 思考设置回复 |
|---|---|---|---|---|---|
| DeepSeek V4.1 Flash | 通过 | 429 | 通过 | 均通过 | 默认、low、high、max：4/4 |
| Nemotron 3.5 Super VL | 通过 | 429 | 通过 | 均通过 | 默认、minimal、low、medium、high、xhigh：6/6 |
| Big Pickle | 通过 | 429 | 通过 | 未测 | 默认：1/1 |
| Flint 40B | 通过 | 429 | 通过 | 未测 | 默认至 xhigh：5/6；max 返回 429 |
| GLM 5.3 Flash | 首次通过，后续 429 | 429 | 429 | 未测 | 0/4，均 429 |
| MiMo V2.6 Flash | 首次通过，后续 429 | 429 | 429 | 未测 | 0/1，429 |
| Nemotron 3.5 Lightning | 首次通过，后续 429 | 429 | 429 | 未测 | 0/1，429 |
| Pulse Alpha | 首次通过，后续 429 | 429 | 429 | 未测 | 0/4，均 429 |
| Beam | 区域拒绝后未继续发送 | 403 RegionError | 未测 | 未测 | 未测 |
| Crouton Alpha | 区域拒绝后未继续发送 | 403 RegionError | 未测 | 未测 | 未测 |

共保留 66 项结果：34 通过、30 被拒绝/失败、2 因先前区域拒绝未执行。重复验证分别
保留，没有用后一次成功覆盖失败。思考矩阵发出 27 项，16 项得到非空回复；区域阻断的
两个模型共 13 种设置未测。接受设置不能证明内部思考预算的执行程度。

两个修复任务都要求定位 `LIVE_SMOKE_BUG`，读取实现与原测试，将折扣的 `+` 修正为
`-`，通过原有 unittest；没有修改测试凑通过。耗时分别为 107.688s、125.250s。
Shell 审批各执行一次指定命令，记录实际输出，同一 run 续接，不重复执行。
这些是受控 fixture 的编码证据，不能外推到任意复杂项目。

## 为什么先能聊天，又出现 FreeUsageLimitError

先固定相同模型、身份、提示、工具声明、预算和正文哈希，仅切换
`stream` / `stream_options`，每项都走当前网络→物理网卡的原生产回退流程：

| 对照 | 结果 |
|---|---|
| SSE，含 include_usage | 有效回复 |
| JSON，含 include_usage | 429 FreeUsageLimitError |
| SSE，不含 include_usage | 有效回复 |
| JSON，不含 include_usage | 429 FreeUsageLimitError |
| 再次 SSE，含 include_usage | 有效回复 |

前半段的非流式拒绝与请求形态有关，不能说是聊天耗尽了额度。原先把错误直接称为
“额度限制”不够准确。Zen 正式选择默认 `stream=True`；本轮没有把失败的 JSON 探针
偷偷切成 SSE，也没有修改生产重试规则。

后半段从 Flint max 开始，后续多个模型的 SSE 也返回相同错误；最终单次 DeepSeek SSE
控制请求同样返回 429。缺少服务端剩余额度/资格事实，无法进一步区分真实配额、匿名
资格或其他服务策略。记录当前可用性限制，停止本轮生成。Beam/Crouton 的 403 是明确
区域拒绝，没有为这些模型继续尝试新的出口。缺失用量保持未知，不把它视为零消耗。

22:54—22:55 的[后续原因定位](zen-rootcause-followup-20261105.zh-CN.md)补齐了响应头与
官方 CLI 对照：官方客户端在同一物理出口同样 429，Retry-After 目标与 Codey 一致；
将输出上限降到 32 仍被拒绝。当前共同限制定位在上游，精确计数规则仍需服务端事实。

全程只读比较网卡、IPv4 路由、DNS、系统代理及进程代理环境，前后均相同；4 个处于
Up 状态的虚拟网卡仍在。请求级隧道被关闭，没有关闭系统 VPN 或改写网络设置。

## 两项 P2 的根因与修复

**认证：生产缺陷。** 修改前，新增 denied/expired/cancelled 三项测试均复现
`reauth_required → 新登录失败 → ready`。旧实现仅根据凭证存在恢复 ready，最新
OAuth 尝试状态覆盖了凭证已失效的事实。

安全凭证记录现独立保留向后兼容的 `reauth_required` 标记。刷新明确拒绝、或刷新
重试后仍收到 401 时，按账号 epoch 与 token version 标记当前凭证；不污染替换账号
或更新令牌。失败、超时、取消及后端重启不能恢复旧凭证准入。新认证成功写入记录才
恢复可用；另一个进程成功登录的新账号记录也能被观察。存储失败明确报错，当前进程
仍保留阻断状态，没有明文回退。测试使用隔离存储，保留用户实机登录。

Zen 使用 public 合作身份，Local 使用配置的 API 连接，都没有 Cline 这条设备 OAuth
重新登录状态机，不复制 Cline 专属认证逻辑到它们。

**失效思考等级：共享前端生产缺陷。** 修改前实际浏览器分别复现 Cline、Zen、Local
在控件隐藏后继续返回 `effort: high`。现在按当前目录核对等级与布尔轴，清理失效字段
及对应缓存，持久化 `reasoning_reselect_required`，展示 “Thinking options changed”
并阻止点击、Enter 和任务提交。重新选择模型或有效思考选项后恢复。标记只属于 UI
状态，不进入推理参数；历史模型身份和草稿保留。覆盖非当前聊天、等级被移除、
等级↔开关转换、持久状态往返和能力恢复后不复活旧缓存。

## 测试与生产的边界

- 旧 live gate 对公布 efforts 与 toggle 的模型发送布尔值，违背生产的等级轴优先契约。
  修正测试选择，新增分批范围、两轮、edit、等级矩阵、失败检查点和区域阻断记录。
- 首个 DeepSeek 批次在保存第三项结果后，打印 Unicode 回复触发 Windows GBK 异常。
  修正工具 stdout，以真实 GBK 文本流回归；原结果均保留。
- 新测试最初把 `UiState.current()` 的清理快照当成可变的生产 session；改用真实 session
  验证非当前聊天缓存。取消事务原本就抛 TimeoutError，测试按原契约断言。
- 扩大浏览器测试发现新恢复路径缺少发送状态刷新，补上有效思考选择后的
  `Composer.updateSend()`。Default 按钮与菜单项同名的定位改为明确菜单范围。
- 三个旧标题/离线用例给无思考能力的 fixture 保存 low 并要求永远保留，与新需求
  冲突。标题夹具改用有效默认值；新增三来源用例严格断言失效等级被清理和发送阻断。
  没有放宽后端能力准入或把上游失败记为通过。

验证结果与提交回执见 [TEST_REPORT](../../TEST_REPORT.md) 和
[进度记录](../cline-integration-progress.zh-CN.md)。没有运行全量 pytest。

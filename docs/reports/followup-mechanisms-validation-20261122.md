# 后续机制审查与验证（2026-11-22）

本轮接续 `a0b94b22` 上未提交的 diff，沿同一个生产 kernel、CodeMode、durable effect/receipt、ChangeTracker 和研究 ledger 实现。没有复制 worktree，没有运行全量 pytest，没有接入跨供应商或第二个模型。

## 实现及边界

| 项目 | 本轮能力 |
| --- | --- |
| 多文件 Patch | `edit({files: [...]})` 支持 2–8 个文件；整组检查路径、权限、读取版本、替换锚点和 Python 语法回退。通过原编辑执行器写入，每次写前重新核对原始字节版本。部分失败保留已写回执，继续缺少的编辑。 |
| 只读 LSP | 使用安装在项目外、PATH 可发现的 `pyright-langserver` 查询 Python 定义。带文档生命周期、超时、取消、UTF-16 坐标和查询后版本检查；过滤工作区外定义。不替代正式检查。 |
| 快照比较 | `workspace_diff` 复用任务首写基线和 ChangeTracker，只比较本任务写入文件，外部变更会拒绝比较。没有另建影子 Git 或变更事实副本。 |
| 浏览器 Flow | `browser_snapshot` 的只读编排重载，最多 6 步；整组先验参，复用原导航、快照、等待、读取、标签页工具。新快照同步更新宿主引用；动作仍走原授权和投递回执，不是自动重放动作的宏。 |
| 研究补搜 | 已保存的冲突证据触发宿主选择的有界搜索，沿原搜索工具、采集配额和调查回执去重，在报告冻结前执行。普通续跑与原生协议关闭共用投递处理；不伪造模型工具调用 ID。 |
| 派生数字 | 核验显式同单位加减算式，包含负值与 Unicode 减号，记录两侧 passage 和偏移。算术正确不免除实体、条件、分母、时间的证据复核。不支持的计算仍待复核，不声称通用数学验证。 |
| 减少抄写 | 复用 `target: "task"` 与原来源选择器。`save_finding` 的冻结采样 schema 从已读回执提供真实 passage 和 `source_id` 枚举。本任务明确声明了比较成功谓词时，从原谓词枚举尚未完成的比较参数；不为普通任务猜测这些字段。运行时仍要求规范 ID，不将 `p1`、`e1` 或猜错的网址改写成有效证据。 |

## 实机

全部实机使用本机 `http://127.0.0.1:5001/v1` 的 Gemma、固定 seed，经过 Codey 真实 headless 入口、工具循环、浏览器和回执链路。网页及搜索结果使用受控夹具，不能代表公网搜索质量。请求、原始响应、事件、工具回执和源码摘要保存在 `.e2e-artifacts/followup-audit-20261122/`。

- `project-live-first/summary.json`：两文件 Patch、LSP 定位、任务快照比较、Python 与 npm 检查通过；两个文件各写入一次。
- `project-conflict-first/summary.json`：第二文件写前注入外部注释，首次 Patch 按版本冲突停止，随后只补剩余编辑；注释保留，两个文件仍各写入一次。原失败和最终结果保存在同一目录。
- `browser-live-first/flow-summary.json`：Flow 通过，实际点击一次，读回业务值并完成。
- `research-live-first/`：模型已读两份资料，却连续填入不存在的 `p1`，以 `no_progress` 失败。原请求和回执保留；这是模型输出错误。随后增加从原回执生成的选项枚举，未放宽 ID 合同。
- `research-live-choices-first/`：新增菜单第一次错误地使用网址，而入口实际要求 `s1/s2`。这是本轮新增代码的确定性错误，已改成直接读取原回执的 `source_id`。回归还发现菜单未经过 `freeze_spec_parameters` 会破坏快照序列化等价性，已修复并增加 roundtrip 检查。
- `research-live-choices-second/`：真实 ID 选择成功，模型却遗漏第二项比较；报告完成不等于本夹具要求满足，验收仍判失败。`research-live-required-first/` 将夹具的两项比较要求编码成已有成功谓词，模型仍重复保存未带比较参数的 finding，以 `no_progress` 失败。
- `research-live-required-second/`：从原成功谓词提供比较参数后，自主研究通过，但有 3 次额外的未带比较参数的保存，作为模型效率问题保留。最终源码上的 `research-live-final/followup-summary.json` 为 **通过**：真实模型 4 次调用，2 项比较与各自来源匹配，宿主自动搜索 1 次，`31 kg - 24 kg = 7 kg` 两侧来源及计算核验通过，未使用脚本准备证据，未多保存无比较字段的 finding。

故障注入区分两个断点：第二次写入尚未登记时，冷恢复复用第一份文件已结算的回执；写入已登记却没有回执时，保留“结果未知”并停止重放，不能猜测后重复写入。

此前的 `followup-research-first-20261122` 为请求取消，`followup-research-retry-20261122` 的响应包含虚构 ID、错误 `source_ref` 和重复字符串直至截断；`followup-research-prepared-20261122` 是固定证据准备后由真实模型答题。环境失败、模型错误与受控准备通过不能混算为完全自主研究成功。

## 回归与类型检查

扩大回归首次 **179 通过、1 失败**。唯一失败案例已完成业务动作，但测试期间仍在修改生产源码，`source_before != source_after` 触发源码稳定性门禁。原始记录保存在 `regression-source-drift-first/`，日志为 `regression-second.log`。冻结源码后的定向回归为 **112 通过、3 项按平台跳过**，见 `regression-final.log`。新增引用菜单后研究相关回归 **53 通过**，见 `finding-regression-final.log`；最后补齐声明参数菜单、负值及旧报告回执兼容后，受影响回归 **38 通过**，见 `finding-regression-complete.log`。这些是有交集的回归批次，不相加声称唯一用例数。

新增补搜回归揭示了原生协议关闭时的混合调用 ID bug，首次失败见 `followup-search-second.log`，修复后见 `followup-search-third.log`；后一次成功未覆盖首次失败。

全树 `ruff check .`、`python -m mypy codey --python-version 3.11`（745 个生产模块）、`pyrefly check codey --python-version 3.11`、`ty check codey --python-version 3.11` 及 `tools.kernel_type_precision_gate` **全部通过**，日志为 `*-complete.log`；本轮 9 个测试和工具文件的严格 mypy 也通过。Pyrefly 原有 35 个 suppression、168 个 warning 与零错误分开记录；本轮没有增加关闭检查、`cast` 或 `type: ignore`。旧报告未包含计算核验时仍沿原数量诊断规则复核，避免新增负值支持使旧回执失效。

## 七项目同题对照

**尚未完成，不能给出七项目胜率。** `seven-project-readiness.json` 保存实际启动检查、参考提交、命令及诊断：OpenCode 缺 Windows CLI 包，Pi 缺内部 `pi-ai` 包，Agent-Browser 缺 Windows 二进制，Agent-Desktop 没有已构建的 Windows 程序，HyperResearch 缺 `typer`。可启动的 Codex 是已安装版本，不能冒充指定源码构建；DeepSeek Harness 启动器可用，不等于七项目统一任务验收完成。

有效对照需要固定可运行的参考构建、同一模型和连接协议、权限、资料、任务、预算及成功判定。工具与 harness 的任务面不同；本轮没有把启动失败计成产品胜负，也没有用 Codey 自身前后对照替代七项目对照。

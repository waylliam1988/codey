# 模型契约一致性检查（2026-10-24）

基线：`88dcd4a8`。检查范围包括共用内核的初始提示、文本示例、原生 schema、CodeMode 子工具目录、项目审计入口及冻结契约的渲染。不把静态怀疑或某次模型回答不好直接认定为生产 bug。

## 已复现并修改的六类问题

| 问题 | 确定性复现 | 修改 |
| --- | --- | --- |
| 原生模式显示不能调用的批处理工具 | `prepare_kernel_turn(native=True)` 的 Visible tools 包含 `read_files`、`parallel`，实际 schema 没有；原生调用被解析器拒绝 | 在快照选择时按协议收窄；`tools_from_specs` 仅投影传入的冻结定义，不再另行隐藏工具 |
| CodeMode 文本示例非法 | 对模型实际收到的示例执行 `json.loads` 和正式文本解码器，均因内层引号失败 | 同一个 JavaScript 示例用 `json.dumps` 生成外层 JSON，同时供原生说明使用 |
| CodeMode 目录暴露无法执行的文本包装器 | 文件真实存在时，目录仍暴露两种包装器；通过正式子调用入口执行得到 `unknown tool` | 目录、原生快照和文本降级解析共用 `is_text_batch_tool` 分类，包装器只保留在文本计划入口 |
| CodeMode 说明与真实写权限冲突 | 说明明确声称只能只读，但正式生产构造器在授权写模式下能够通过 `tools.edit` 实际创建文件 | 说明改为冻结目录及原有授权约束，并说明真实 `{ok, value}` 返回结构；没有扩大权限 |
| 冻结文本契约回查实时注册表 | 冻结后撤销注册，原 schema 仍在，文本声明却消失；令注册表读取抛错也可稳定复现 | `json_contract_text` 只接收冻结定义，不再接收 policy 或可选 live registry 分支 |
| 审计入口重复携带旧 JSON 契约 | 实际 `run_project_audit_advisor` 的原生请求既要求 native 调用，又在任务文本中强制 JSON，并重复列出文本批处理工具 | 删除审计入口的工具示例、工具名单和协议指令，只保留审查要求；两种协议统一消费内核契约 |

复现记录：`.e2e-artifacts/contract-consistency/before.xml` 为前五类的 7 项失败，`audit-before.xml` 为审计入口的 2 项失败。另以 `churn-before.xml` 复现注册表在真实快照构建中变化后，实际发出的提示仍列出工具，却丢失对应文本声明；修改后同一路径通过。修复后这些测试通过，并增加了两种协议下照抄文档示例、通过真实 Node worker 与生产子工具读取实际文件的正向验证，共 12 项。

## 如何保持一个事实来源

- 任务授权和本轮事实仍生成原有 `TurnSnapshot`；工具名单、文本示例、原生 schema、解析与执行消费该冻结输入。
- 删除 `kernel_prompt._snapshot_names` 的二次选择入口。提示渲染必须显式接收快照名单，不能缺省时另建一份名单。
- 删除旧 `build_read_only_catalog`。它没有生产调用方，旧测试却一直验证它；现在测试直接覆盖生产使用的 `build_codemode_catalog`。
- 删除 JSON 渲染的 live/frozen 双入口及其空字符串兜底。没有新增长期兼容分支、权限日志、持久状态、模型专用修复或第二套执行循环。
- CodeMode 写入继续经过原有 `execute_turn`、授权、工作区变更与回执链路。目录筛选修复没有放宽 shell、进程、直接文件系统或网络访问。

## 实机结果及限制

使用相同本地 Gemma 模型、相同任务及参数，运行原有生产连接、内核、Node worker、SQLite、完成门和恢复路径。测试驱动仅创建临时工作区、声明验收要求并观察请求，没有替换执行器。

严格验收要求：输出正确、指定 CodeMode 子调用真实发生、正式完成门通过、恢复后完成判断一致、重复恢复稳定。只生成了正确文字或通过其他工具完成不算流程通过。

| 场景 | 修改版结果 | 说明 |
| --- | --- | --- |
| 原生模式用 CodeMode 读取两个文件 | 未通过 | 模型反复尝试禁止的 `require('fs')`，随后改用普通读取；完成门拒绝把指定步骤算成功。原版同样未通过 |
| 原生模式用 CodeMode 创建文件 | 通过 | 确有 `codemode → edit` 子调用、文件内容正确、恢复完成判断通过。原版走普通 edit，指定 CodeMode 步骤未完成 |
| 文本模式用 CodeMode 读取两个文件 | 通过 | 两次真实子读取、JSON 最终输出正确，恢复稳定 |
| 文本模式用 CodeMode 创建文件 | 通过 | 确有 CodeMode edit 子调用，文件内容正确，恢复稳定 |

修改版两种原生场景的 Visible tools 与实际 HTTP schema 完全相同；基线两种原生场景均存在上述名单矛盾。完整记录在 `artifacts/contract-consistency-live-20261024-02`，原版原生对照在 `artifacts/contract-consistency-baseline-20261024-01`。这证明契约矛盾已消除，不证明模型总能遵守步骤或编写正确 JavaScript。

首轮探针的文本写入曾只检查输出、文件及 CodeMode 成功记录，漏查写入是否为 CodeMode 子调用。已补强验收并保留 `-01/strict-reassessment.json`，该首轮结果不作为写入流程通过证据；后续按严格条件重新运行。

## 相关旧测试的归因

- `test_review_repro_lock.py` 的交替调用场景在未修改基线也失败：任务要求来源，但测试没有把 ledger 交给正式结果投影和完成上下文。只修正 fixture 的依赖装配，同一个 ledger 由正式结果投影更新，另加最终 URL 断言；没有在生产完成门添加 session/text fallback。
- 真实入口提示 hash 测试在基线也失败：旧期望 `aea42…`，实际基线为 `7b439…`。已记录差异审查：将修改版提示的唯一 CodeMode 契约行换回原行，恰好重建基线实际 hash；其余初始提示不变。新期望为 `b54d4…`，结果和修复提示的原 hash 保留。

最终相关 pytest 为 **342 通过、15 个子测试通过**，记录在 `.e2e-artifacts/contract-consistency/acceptance.xml`；没有运行全量 pytest。Ruff、mypy、Pyrefly、ty 及 runtime 清单检查通过；Pyrefly 原有 29 suppressed / 80 hidden warnings 未作为新错误。

本批独立提交并合并到本地 `master`，不推送远端。合并依据是六类已复现缺陷修复、相关边界测试通过，且剩余模型流程失败在原版也能复现；不以“全部模型场景通过”为结论。

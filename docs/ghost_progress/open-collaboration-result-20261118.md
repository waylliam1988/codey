# 开放协作学习实机收尾：正常入口提取未通过

状态：`EXPERIMENT_COMPLETE / NOT_PROMOTED`。本轮没有上线自动偏好写回，没有实现用户期待的四步自主学习闭环。

## 实际做过什么

实现并离线验证了一个候选：普通 `done` 同次可选候选 → 引用校验 → 现有 observation/Inbox 事务与事件 → 独立 ContextSource/admission → 后续正常入口。内容和情境使用用户原话，不枚举温暖、直接等词；支持 user/project/session、turn-only、显式时间到期、纠正、遗忘和绑定已曝光实际回答的反馈。没有本地 HF、额外生成、第二遍回答、第二套数据库或双写。

原型回归验证了来源、过期、scope、禁用、撤回、删除、重建、重启、并发幂等、错误隔离和反馈归因；但 fixture 的成功只证明程序链路，不能代替模型提取收益。实机未通过后，所有候选生产接线和自动写回模块均撤出当前生产树，保留为[可审查实验补丁](../../tests/manual/ghost_memory_routing/open_collaboration_candidate.patch)。补丁使用现有正常 kernel 和持久化组件，没有另写 runtime。脚本一起归档，避免主树遗留调用不到的学习开关或模块。

冻结提交：`35bf44dd`（任务、配置、scorer）、`2997b200`（保留首次失败、v2 提示复验）、`39cd47f7`（v3 最终指引 seed）。各次差异见[冻结协议](open-collaboration-protocol-20261118.md)。本轮未重跑历史 Hopfield、World Model 或旧排序收益实验。

## 实机结果与归因

模型为 KoboldCpp `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，temperature=0，output=4096，context=262144/reserve=32768/keep_recent=32000；正常 Chat/durable task 入口。两轮完整矩阵分别使用独立 state，每轮非流式/流式各 13 回合。每回合重启 provider/AppContext，一次 HTTP 生成，不自动重试。

| 运行 | 完整回合 | 请求 | 接受的开放偏好 | 条件化开头与后续学习 |
|---|---:|---:|---:|---|
| v1：工具描述中可选字段 | 26/26 | 26 | 0 | seed 遗漏字段；跨会话两个条件均未恢复 |
| v2：同一说明加入 task guidance | 26/26 | 26 | 0 | 再次全部遗漏，未形成修订/反馈闭环 |
| v3：说明移到最终 done 指引后 | 1 个 seed | 1 | 0 | 按预定规则停止完整矩阵 |
| 自然语言显式“请记住”诊断 | 1 | 1 | 0 | 口头承诺，无结构化记录 |
| 既有兼容文本工具入口诊断 | 1 | 1 | 0 | 同样未形成记录 |
| 单工具、明确指示各字段的能力诊断 | 1 | 1 | 1 个合规候选输出，未写生产状态 | 仅证明接口和模型能传字段，不是自然学习收益 |

合计 56 次生成，52 格完整矩阵与四个独立诊断均保留。没有本轮服务故障，没有补发或用成功重跑覆盖失败。原始请求确认 schema 和相应说明确实发送；原始回执只含 summary，缺少可选候选。持久化正确地没有补造记录。这是受测模型在完整任务提示下遗漏提取，不是存储 bug，也不能归咎于 API 必然丢字段。

v1/v2 非流式实际回合延迟中位数分别 5.30/6.91 秒，流式 5.25/5.91 秒；其中包含运行入口与本地维护，不称纯推理延迟。两轮各 13 个非流式回执有 usage，各 13 个工具流未上报 usage（请求已含 include_usage），完整 tokens 成本仍未知。两轮流式各保存 39 个显示快照，未显示私有字段；因为模型没有产生真实 metadata，这一项只证明当前回复路径，真实字段隔离另由流式回归验证。

临时指令、无旧偏好时的遗忘探测等若答对，也不能记作学习成功。修订后的某些答复与同会话历史相符，仍可能来自历史检索；没有被接受的独立学习状态，不能把这种结果归因给新机制。此实验也没有证明 Hebbian、向量或 Hopfield 的相对优劣。

## 保留的独立生产修复

1. 无项目 Chat 的物理工具目录不再替代逻辑项目身份。补充个人上下文读取已接收 durable task 的实际 project/session；没有权威任务或 scope 不匹配就不补。修复后全局已确认偏好可走现有入口，项目目标不因此泄漏。Research 合成夹具补齐实际任务，保留禁用/删除/撤回断言。
2. 已有 API 流式显示可从原生 `done` 的顶层 summary 提前投影正文，支持 Chat Completions/Responses，处理部分 JSON、转义和 Unicode，私有字段及其他工具参数不展示。最终完成与权限仍由原来的解析和结算负责。

以上修复在正常路径默认生效；不是自动学习晋级。没有扩大偏好白名单、改变模型配置、增加模型调用或改变完成合同。回滚对应修复提交即可；原有 Ghost 控制开关保持适用。本轮未改前端，无需发布静态资源。

生产修复分别提交为 `20326131`（逻辑身份）和 `88e93d8e`（流式正文）。最终另做 1 次真实模型边界探测：在项目中以既有人工确认入口建立用户级偏好与项目目标，再用无项目的新 Chat/流式/output4096 请求。任务完成、实际 HTTP 生成 1 次；冻结的真实请求仅有独立 `ghost_directive`，没有项目目标来源。随后另有一次本地 close_reply 结算，未发 HTTP。原始请求、SSE、终态和逐 effect 来源留在 `D:/codey-local-evidence/no-project-scope-production-live`；该探测验证发送边界，不评分温暖程度，也不并入前面 56 次学习实验。

## 验证与剩余限制

原型尚在生产树时：283 项回归与 25 个子测试通过，另有浏览器 B3 `timeout_ms` 必填断言失败；撤出候选后独立复现相同失败，未放宽断言或修改浏览器生产。该项不属于本轮 scope，待所有者审查 `tests/test_protocol_surface.py:300` 与当前 browser_wait 默认值合同。

原型阶段全仓 Ruff、完整 codey mypy/Pyrefly/ty 和 kernel precision 均通过。累计 15 个修改文件额外 Any 审计零诊断；完整额外严格命令仍含 329 个未修改依赖文件的 11828 条既有诊断，单独留档，不能称全仓 Any 清零。

撤出原型后的最终相关回归：264 项及 25 个子测试通过，仍有上述 1 条浏览器失败；没有运行全量 pytest。新增 Chat 回归另用 HEAD 原实现注入复现失败，再用修复实现通过，确认不是原型带来的假象。流式专门复验 5 项通过。最终全仓 Ruff、完整 `codey` 的 mypy（707 文件）/Pyrefly/ty、kernel precision，以及修改测试的三个专项类型检查均通过；Pyrefly 全树另有既有 suppressed/warnings，并非零警告。最终 5 个生产/测试文件额外 Any 审计零诊断；该额外命令的未修改依赖仍有 330 文件/11829 条诊断，未扩大清理范围，也不更改检查配置。

完整命令：`python -m ruff check .`、`python -m mypy codey --python-version 3.11`、`pyrefly check codey --python-version 3.11`、`ty check codey --python-version 3.11`、`python -m tools.kernel_type_precision_gate`。专项检查包含 `personal_context.py`、`display_stream.py`、`summary_stream.py`、`test_done_summary_stream.py` 和 `test_ghost_portable_context.py`，另加 `--disallow-any-expr --disallow-any-explicit --disallow-any-generics --disallow-any-unimported` 核对 owned 诊断；不把导入依赖的失败命令称为整体通过。

最终回归命令：

```powershell
python -m pytest tests/test_done_summary_stream.py tests/test_ghost_portable_context.py tests/test_permission_profiles.py tests/test_conversation_display_stream.py tests/test_kernel_protocol_validator_failclosed.py tests/test_ghost_context_admission.py tests/test_ghost_language_production.py tests/test_ghost_experience_role_budget.py tests/test_project_goal_production.py tests/test_ghost_manual_memory.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_history_delete_guards.py tests/test_ghost_controls.py tests/test_coldstart_native_local_receipts.py tests/test_api_chat_native_turns_and_history.py tests/test_api_request_context_protocols.py tests/test_protocol_surface.py::test_b3_frozen_permissions_descriptions_and_no_implicit_b1_defaults -q --maxfail=5
```

原始请求、SSE、回执、显示快照、Inbox/observations 和完整 state 留在 `D:/codey-local-evidence/open-collaboration-*`。仓库只保留脚本/补丁、协议及[脱敏指标](../../tests/manual/ghost_memory_routing/reports/open-collaboration-20261118.json)。

复现原型前先审查补丁，`git apply --check tests/manual/ghost_memory_routing/open_collaboration_candidate.patch`；在干净 checkout 中按需应用并运行冻结命令。无需 worktree。脚本的 `open-collaboration-v1` 表示冻结的任务/scorer 协议；当前补丁对应最后 v3 提示实现。补丁实现与当前已确认偏好/经历消费者分开审查，不将归档视作默认关闭的生产功能。

下一步仍需新的、可解释的同次结构化出口或支持该出口的实际模型证据；不继续堆叠相同模型的提示措辞，不恢复第二次生成，不从口头承诺正则提取来掩盖遗漏。当前四步闭环中的自动理解和可靠写回仍缺，World Model 的历史状态不变。

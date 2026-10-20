# Ghost 实验进度记录：Context Epoch Admission Wiring

- 实验名称：Context Epoch Admission Wiring
- 状态：INCOMPLETE
- 负责人：Codex
- 开始日期：2026-10-21
- 最后更新：2026-10-21
- 关联 Roadmap 验收门：0.6.1
- 实验 commit：待提交后填写
- 生产合并结论：EXPERIMENT_ONLY

## 本次目标

验证 Ghost directive、continuity、experiences 和 research topic continuity 能否沿着
`ContextSource/PromptEnvelope → Context Epoch admission → provider-send` 进入 shared kernel，
并假设缺少 source、超预算或 scope 不匹配时，provider 不应收到 Ghost 文本。

## 实际改动

- 新增中性的 `codey/workspace/context_admission.py`，定义 Ghost context source、scope、预算、
  byte-presence 校验、拒绝原因和 ContextEpoch projection；`codey/ghost/admission.py` 仅保留兼容导出。
- `KernelTransportDeps` 携带 Ghost source metadata；shared kernel 在首次 provider-send 前 fail closed，
  后续 repair/tool turns 只记录实际仍在 payload 中的 source。
- task entry 用 `PromptEnvelope` 组装 Ghost sections；project adapter 和 research iteration 传递相应 source metadata。
- ContextAdmission、RenderedContextSource 和 trace PromptSectionTrace 保存 bounded `scope`；trace 使用
  admission 生成的同一 epoch id。

## 验收命令与结果

- `python -m pytest tests/test_ghost_context_admission.py tests/test_context_epoch.py tests/test_prompt_envelope.py tests/test_task_entry_cutover.py tests/test_task_entry_kernel_prod.py -q`：116 passed，11 subtests passed。
- `python -m pytest tests/test_run_trace.py tests/test_trace_split_golden.py tests/test_task_entry_run_trace.py tests/test_fullred_trace_ghost.py -q`：83 passed。
- `python -m pytest tests/test_architecture.py -q`：98 passed，401 subtests passed。
- `python -m pytest tests/test_chat_workspace_tools_and_answer_completion.py -q`：22 passed。
- provider-send capture fixture：admitted Ghost bytes 被 fake provider 捕获；缺失 source 时 provider 调用次数为 0；预算和 scope rejection 均 fail closed。

## 实验配置

- model / suite / endpoint：shared kernel fake provider capture；未运行 KoboldCpp live suite
- tasks / cells / reps：admitted capture、missing source、over-budget、scope mismatch、research continuity path；每项 deterministic unit/integration fixture
- on/off 对照：source metadata present vs omitted/rejected；默认 prompt 文本在 admitted case 保持一致

## 结果与限制

真实 shared-kernel provider-send 边界已经有可捕获的拒绝行为，且 source ref、digest、budget、scope 和 epoch
来自同一 admission projection。正常 admitted case 不改 prompt 字节。尚未完成真实 KoboldCpp live smoke、真实
provider payload 对照、成本/延迟测量、四个独立 Ghost 开关和完整 scope/expiry matrix，因此不能标记
`EXPERIMENT_COMPLETE`，也不能晋级为生产合并。

研究 continuity 已通过中性 workspace admission contract 接入；它仍然不是 Evidence，不生成 citation，
不授予权限，也不触发 provider/tool call。

## 已知问题和回滚方式

- `project_adapter` 目前只把 permission profile 实际渲染的 directive/continuity 作为 admitted source；
  experiences 在该 builder profile 中仍未开放，task entry 路径保留 experiences 的直接 section。
- 旧的 `prepend_ghost_directive` / `owner_prompt_with_ghost_directive` helper 仍保留 API，但当前 Ghost
  task entry 不再使用它们。
- 回滚实验 commit 会移除 provider-send gate 和 metadata；既有 Ghost stores、Evidence、Permission、
  CompletionProof 不受影响。

## 下一步

1. 增加真实 provider-send capture/fault-injection harness，验证 outbound bytes、trace sections 和 epoch record 完全一致。
2. 记录一个 KoboldCpp model/config 的最小 live smoke；若服务故障立即停止重试并标记阻塞，不把故障算成模型阴性。
3. 进入 0.6.2：四个独立开关、scope、过期和禁用路径；完成后再做 language 有条件 shadow。

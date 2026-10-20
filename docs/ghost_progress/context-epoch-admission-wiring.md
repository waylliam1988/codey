# Ghost 实验进度记录：Context Epoch Admission Wiring

- 实验名称：Context Epoch Admission Wiring
- 状态：INCOMPLETE
- 负责人：Codex
- 开始日期：2026-10-21
- 最后更新：2026-10-21
- 关联 Roadmap 验收门：0.6.1
- 实验 commit：`9b9b1999` (`feat(ghost): wire context epoch admission gate`); follow-up compatibility fix `d1c5c3db`; resume harness `待提交`
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
- live smoke 前置健康检查：`GET http://127.0.0.1:5001/v1/models` 于 2026-10-21 02:12（Asia/Taipei）返回 HTTP 200，model 状态为 `loaded`。
- live smoke（未完成）：命令使用 suite `admission_smoke_20261021`、cell `lang_on_d2`、model `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M` 和 endpoint `http://127.0.0.1:5001/v1`；2026-10-21 02:13（Asia/Taipei）等待约 90 秒无新 stdout 后停止。本地隔离 JSONL 有 14/20 个 `done` rows，6 个 cell 缺失。该故障和缺失数据不计入模型行为阴性，也未重复重试。
- live smoke 恢复：先确认同一 endpoint/model 返回 `loaded`，使用 resume harness 只追加 index `14,15,16,17,18,19`；6 个缺失 cell 均 `done`，最终 `admission_smoke_20261021/lang_on_d2` 为 20/20、20/20 judge pass。脱敏结果见 `tests/manual/ghost_memory_routing/reports/REPORT4-admission-live.md`。

## 实验配置

- model / suite / endpoint：shared kernel fake provider capture；KoboldCpp `modelA` / `admission_smoke_20261021` / `http://127.0.0.1:5001/v1`
- tasks / cells / reps：admitted capture、missing source、over-budget、scope mismatch、research continuity path；每项 deterministic unit/integration fixture
- on/off 对照：source metadata present vs omitted/rejected；默认 prompt 文本在 admitted case 保持一致

## 结果与限制

真实 shared-kernel provider-send 边界已经有可捕获的拒绝行为，且 source ref、digest、budget、scope 和 epoch
来自同一 admission projection。正常 admitted case 不改 prompt 字节。真实 KoboldCpp smoke 已恢复并完成
20/20，但它是单 cell admission smoke，没有 on/off 对照；尚未完成真实 provider payload capture/fault injection、
成本/延迟测量、四个独立 Ghost 开关和完整 scope/expiry matrix，因此不能标记
`EXPERIMENT_COMPLETE`，也不能晋级为生产合并。

研究 continuity 已通过中性 workspace admission contract 接入；它仍然不是 Evidence，不生成 citation，
不授予权限，也不触发 provider/tool call。

## 已知问题和回滚方式

- `project_adapter` 目前只把 permission profile 实际渲染的 directive/continuity 作为 admitted source；
  experiences 在该 builder profile 中仍未开放，task entry 路径保留 experiences 的直接 section。
- 旧的 `prepend_ghost_directive` / `owner_prompt_with_ghost_directive` helper 仍保留 API，但当前 Ghost
  task entry 不再使用它们。
- KoboldCpp/runtime 故障记录：endpoint `http://127.0.0.1:5001/v1`，model
  `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，suite
  `admission_smoke_20261021`，cell `lang_on_d2`；健康检查正常，运行到 14/20 后约 90 秒无新输出，已停止。
  已按恢复规则只重跑缺失的 6 个 index；原始 JSONL/state 仍只保留在本地隔离目录。
- 回滚实验 commit 会移除 provider-send gate 和 metadata；既有 Ghost stores、Evidence、Permission、
  CompletionProof 不受影响。

## 下一步

1. 增加真实 provider-send capture/fault-injection harness，验证 outbound bytes、trace sections 和 epoch record 完全一致。
2. 进入 0.6.2：四个独立开关、scope、过期和禁用路径；完成后再做 language 有条件 shadow。
3. 继续把 modelA admission smoke 与完整 language on/off 评估分开，不能把 20/20 smoke 写成晋级收益。

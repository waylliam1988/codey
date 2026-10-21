# Ghost 实验：Scoped Control Failure Closure

- 实验名称：Scoped Control Failure Closure
- 状态：EXPERIMENT_COMPLETE（仅本实验边界）
- 负责人：Codex
- 开始 / 更新日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1 / 0.6.2
- 实验 commit：`23c6e21fd74212baacf39d6c31edb532dd076de9`（先复现）；
  `cc7e48b1f3b8387262650f99f698fe4d659e7958`（fixture 与实际 HTTP）；
  `44172e709bf39e047e18042530802c21581140f8`（post-turn fixture）
- 生产 commit：`34e201dc43235a74e467e17683917f9af7b9cdfc`
- 生产合并结论：PRODUCTION_MERGED（scoped 读取与写入故障关闭修复）

## 目标和假设

原 `ghost_control_enabled` 在 scoped reader 抛 TypeError 后重试无 scope 读取，
reader 缺失时改读旧 `learning_enabled`。假设两条 fallback 会把不可确认的控制误开为 True。
冷启动没有旧 API 兼容需求，应只调用生产的 scoped `control_enabled`，故障只拒绝，不降级。

## 实验配置

- model / suite / endpoint：synthetic-admission、固定 native provider fixture /
  scoped_control_failure_closure / 临时 loopback HTTP；无 live 模型。
- tasks / cells / reps：四个 control × scoped TypeError / 旧 name-only 签名 / 只有 learning API；
  正常 scoped bool 对照；actual task entry 的 admit_hints 故障/正常 on/off capture。
  control contract 25 项；native entry capture 3 项；实际 ApiProvider HTTP 3 项，各一次。
- 每项 deterministic 一次；唯一变量为 control reader 的契约或故障，不修改 scorer 或门槛。
- 默认有效 prompt 保持；不新增 provider call、Evidence、Permission 或 CompletionProof 权限。
- 跟随当前生产 API 修改轻量 fixture，不恢复 learning/unscoped fallback。
- 进一步审计发现 settlement 仍读不带 scope 的 learning API，post-turn 缺 inbox 默认 True。
  在写修复前增加真实 GhostInboxStore 的 project/session deny 与其他 scope 仍可写对照，
  缺控制 authority 不写观察、post-turn 不学、旧 learning reader 不授权。

## 本次完成 / 验证命令和结果

Red：`python -m pytest tests/test_ghost_control_contract.py tests/test_chat_workspace_tools_and_answer_completion.py -k 'contract or downgrades' -q --tb=short`
为 18 failed、10 passed、22 deselected。原始日志只归档到 ignored
`artifacts/ghost_memory_routing/scoped-control-red.log`。
已确认 scoped TypeError 降级会把 hint 送入 actual entry 的 send_turn；真实 store 的项目/会话 deny
无法阻止 settlement 写入，缺 authority 的 post-turn/settlement 也会开启。

修复只读 scoped `control_enabled`，不重试、不转读旧 learning API；删除未使用的
`_ghost_learning_enabled` 别名。缺 authority 不写 observation、不运行 post-turn learning；
settlement 的控制异常或非 bool 保持 `settings_error`，正常关闭为 `skipped`。
settlement 直接调用同一 store 契约，以保留故障与正常禁用的 health 区别，不新增 validator。

```powershell
python -m pytest tests/test_ghost_control_contract.py tests/test_chat_workspace_tools_and_answer_completion.py tests/test_learning_switch_fail_closed.py tests/test_ghost_projection_exception_boundaries.py tests/test_agent_effect_sandwich.py -q --tb=short
python -m pytest tests/test_ghost_provider_send_boundary.py -k scoped_hint_control -q --tb=short
```

结果：84 passed；实际 HTTP 3 passed / 33 deselected。故障/关闭仍可正常回答，但 payload 无 hint；
开启时有 hint。每项恰好一次 HTTP，scoped_error 恰好一次 scoped read，没有无 scope 重试。

适度扩大到 50 个文件，包含 Ghost、入口、恢复、settlement 和 kernel/research parity：

```powershell
$ghostTests = @(rg --files tests | Where-Object { $_ -match '^tests[\\/]test_(ghost_|learning_switch|kernel_legacy_parity|kernel_parity_regressions|task_entry_|chat_workspace_|agent_effect_sandwich|.*settlement)' } | Sort-Object)
python -m pytest @ghostTests -q --tb=short
```

结果：1350 passed、141 subtests passed、1 failed（291.36s）。唯一失败是旧 continuity fault fixture
设置 `ghost_inbox=None`，现在正确跳过写入，不能再期待写入异常。fixture 改为提供 scoped authority，
并断言确实调用 sync 与带 scope 的控制读取；生产保持 fail closed。没有重跑整批并宣称全绿。

```powershell
python -m pytest tests/test_ghost_post_turn.py tests/test_ghost_control_contract.py -q --tb=short
python -m pytest tests/test_ghost_control_contract.py tests/test_ghost_controls.py tests/test_ghost_projection_exception_boundaries.py tests/test_ghost_post_turn.py tests/test_ghost_post_turn_affinity.py tests/test_ghost_post_turn_work_queue.py tests/test_ghost_post_turn_shared_workspace_and_explicit_workflows.py tests/test_ghost_provider_send_boundary.py tests/test_learning_switch_fail_closed.py -q --tb=short
```

修正后结果：26 passed；最终受影响组合 111 passed（71.78s）。这些集合有重叠，不相加计数。
早期 5 文件组合曾有 recovered-tool-outcomes 用例未进入 agent 的一次失败；单项、同组合
84 项和扩大组合均未复现。保留本地日志与 assertion 的 event 诊断，不虚构根因或放宽预期。

```powershell
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
```

结果：mypy 495 files clean；Pyrefly 0 errors、29 suppressed、74 warnings；ty All checks passed。
改动文件 Ruff 和 `git diff --check` 通过。全部原始日志保留在 ignored
`artifacts/ghost_memory_routing/scoped-control-*.log`；本进度文件即离线报告，无模型行为评分。

## 当前生产边界

- 是否经过真实 provider-send：是，实际 task entry → builder → kernel → ApiProvider → loopback HTTP；非 KoboldCpp。
- 是否改变默认 prompt：不改模板或正常开关行为；缺 authority、关闭和控制故障时移除 hint/拒绝写入。
- 支持模型 / 任务 / 配置：生产 scoped control 契约，不作语言效果推广。
- 生产合并结论：PRODUCTION_MERGED，仅 `34e201dc` 的安全修复。

## 收益、成本与回滚

收益是消除 scoped 控制错误的全局放行和观察写入绕过。四个独立控制不再由旧 learning reader 授权。
有效路径仍一次 scoped 读取；故障路径从最多两次读取降为一次，不新增模型请求或 projection。
没有 live 模型成本/延迟测量，不作吞吐、token 或语言收益结论。无 state/schema 迁移。
回滚可单独 revert `34e201dc`，保留实验/测试提交；这会恢复已知越权行为，应先关闭 Ghost hints/事件消费。

## 未完成或阻塞 / 下一步

本轮不覆盖运行中撤回、已有 provider history、codec/compaction 以及完整四开关 acceptance。
整个 0.6.1 / 0.6.2 仍 INCOMPLETE。下一实验已确认 session 开关首轮后关闭仍在第二次 HTTP
重发 hint，见 [Provider Control Revocation](provider-control-revocation.md)。优先与
[Provider History Expiry](provider-history-expiry.md) 合并研究唯一最终 payload 准入宿主。
World Model 继续 SHADOW，不进入默认 prompt。

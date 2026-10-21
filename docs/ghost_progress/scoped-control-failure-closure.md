# Ghost 实验：Scoped Control Failure Closure

- 实验名称：Scoped Control Failure Closure
- 状态：INCOMPLETE
- 负责人：Codex
- 开始 / 更新日期：2026-10-21
- 关联 Roadmap 验收门：0.6.1 / 0.6.2
- 实验 commit：待记录
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

当前 `ghost_control_enabled` 在 scoped reader 抛 TypeError 后重试无 scope 读取，
reader 缺失时改读旧 `learning_enabled`。假设两条 fallback 会把不可确认的控制误开为 True。
冷启动没有旧 API 兼容需求，应只调用生产的 scoped `control_enabled`，故障只拒绝，不降级。

## 实验配置

- model / suite / endpoint：固定 native provider fixture / scoped_control_failure_closure / 无 live 模型。
- tasks / cells / reps：四个 control × scoped TypeError / 旧 name-only 签名 / 只有 learning API；
  正常 scoped bool 对照；actual task entry 的 admit_hints 故障/正常 on/off capture。
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
无法阻止 settlement 写入，缺 authority 的 post-turn/settlement 也会开启。尚未修改生产。

## 当前生产边界

- 是否经过真实 provider-send：计划 actual task entry 的 send_turn capture，非 KoboldCpp。
- 是否改变默认 prompt：计划否；故障时剔除 hint。
- 支持模型 / 任务 / 配置：生产 scoped control 契约，不作语言效果推广。
- 生产合并结论：EXPERIMENT_ONLY。

## 未完成或阻塞 / 下一步

先复现，然后移除两条 fallback；扩大 Ghost/control/entry 回归并检查类型，不跑全量 pytest。
不替代 Provider History Expiry 的完整 payload 生命周期研究。

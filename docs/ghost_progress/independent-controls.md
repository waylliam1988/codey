# Ghost independent-controls 进度记录

- 实验名称：Independent Ghost controls
- 状态：`INCOMPLETE`
- 负责人：Codex
- 开始日期：2026-10-21
- 最后更新：2026-10-21
- 关联 Roadmap 验收门：0.6.2 scope 与四个开关
- 实验 commit：`496247fc` (`feat(ghost): add independent control gates`); scoped projection/send gate `89622a8c`; lifecycle/expiry fixture `c3701ab4`; follow-up boundary fix `6f18485e`; architecture audit `a7797877`
- 生产合并结论：`SHADOW_ONLY`

## 本次目标

验证 `accept_events`、`retrieve_projections`、`run_maintenance` 和
`admit_hints` 四个控制可以独立持久化、读取和 fail closed，并在真实 runtime
边界分别阻止事件接收、投影检索、后台维护和 provider-send 前 hint admission。
假设是：控制面可以先独立于内容收益和默认 prompt 验证；关闭一个控制不会意外
改变另外三个控制。

## 实际改动

- `GhostInboxStore` 将四个控制写入现有 learning settings，并保留旧的
  `enable/disable` 全开/全关兼容行为；旧 settings 缺少新字段时按
  `learning_enabled` 迁移读取。
- 控制 projection 支持 user/project/session 覆盖；继承按 user → project →
  session 计算，任何上层 deny 都不能被更窄 scope 的 allow 覆盖。
- scope delete 会删除对应 project/session override；user delete 恢复四个控制默认值，
  并在重启后保持删除结果。
- prompt-side directive、continuity 和 experience retrieval 使用
  `retrieve_projections`；post-turn ingest/continuity/work queue 使用
  `accept_events`；sleep kick 使用 `run_maintenance`。
- task entry 在构造 Ghost sources 时检查 `admit_hints`；admission API 也支持
  显式 `admit_hints=False`，拒绝原因是 `hints_disabled`。
- admission source 支持显式 `expires_at`，用 `as_of` 做 deterministic expiry 判断；
  过期或非法时间拒绝，不进入 provider payload。
- 控制面 summary 和 action 增加 controls 映射，CLI 增加
  `codey ghost control <name> on|off`。
- 新增 deterministic control fixture tests；没有修改默认 prompt、排序策略或
  language scorer。

## 验收命令与结果

```powershell
python -m pytest tests/test_ghost_controls.py tests/test_learning_switch_fail_closed.py tests/test_ghost_context_admission.py tests/test_context_epoch.py -q
```

结果：`34 passed, 11 subtests passed`。

```powershell
python -m pytest tests/test_ghost_round4_resume.py -q
git diff --check
```

结果：resume harness `5 passed`；工作树 diff check 通过。

```powershell
python -m pytest tests/test_ghost_controls.py tests/test_ghost_context_admission.py -q
python -m pytest tests/test_learning_switch_fail_closed.py -q
```

结果：controls/admission `15 passed`；kernel transport 全集 `1002 passed`。

## 实验配置

- 模型 / suite / endpoint：无模型；离线临时 state fixture；无 endpoint
- tasks / cells / reps：四个控制各 1 个独立开关案例；legacy 全关/恢复案例 1；
  user/project/session inheritance 1；restart/concurrent writers 1；
  retrieval/maintenance boundary 1；admission on/off provider capture 1；control surface 1
- on/off 对照：单控制切换与其它控制保持开启；legacy `disable_updates` 作为
  全关兼容对照；admission `admit_hints=True/False`

## 结果与限制

- 四个控制可独立写入和读取；旧 settings 可以继续读取。
- retrieval、maintenance、admission 和 event acceptance 已在代码边界接线；
  scope inheritance、restart/concurrent settings、expiry 和真实 kernel provider
  capture 已有 deterministic evidence。
- 仍尚未完成 scope export/reset 的控制 manifest、并发 runtime
  请求矩阵、成本/延迟测量和完整 0.6.2 acceptance battery。
- 本实验没有 live/offline 模型行为对照，也没有证明任何 language 或 command
  收益；不能把它当作 Ghost language 晋级。
- 这是控制面 shadow 研究，未进入真实 provider-send 的生产启用决策，默认 prompt
  内容没有改变。

## 已知问题和回滚方式

- `learning_enabled` 仍是旧 API 名称；后续可以在完成迁移矩阵后拆分 UI 文案，
  但暂不删除兼容入口。
- 只需回滚实验 commit `496247fc` 即可恢复上一版单一 learning switch；不需要
  删除本地 state。实验 state 不提交仓库。

## 下一步

1. 为四个控制补充 expiry、scope delete/export/reset 和 control projection replay
   fixture。
2. 在 shared kernel/provider capture 中继续验证 `admit_hints` 关闭时实际 outbound
   bytes 不含 Ghost source，开启时 epoch/digest 与 trace 一致，并记录成本/延迟。
3. 保持 0.6.2 `INCOMPLETE`，完成后再决定是否进入更窄的 shadow runtime。
4. World Model 继续保持 shadow：只研究 bounded event/prediction review 和
   projection admission，不进入默认 prompt。

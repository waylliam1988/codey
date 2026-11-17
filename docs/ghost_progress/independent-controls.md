# Ghost independent-controls 进度记录

- 实验名称：Independent Ghost controls
- 状态：`INCOMPLETE`
- 负责人：Codex
- 开始日期：2026-10-21
- 最后更新：2026-11-18（维护撤回与 setter 兼容；历史测量不变）
- 关联 Roadmap 验收门：0.6.2 scope 与四个开关
- 实验 commit：`496247fc` (`feat(ghost): add independent control gates`); scoped projection/send gate `89622a8c`; lifecycle/expiry fixture `c3701ab4`; expiry trace `e8127019`; scoped export fixture `44fb29cf`; scoped health fix `91fe542f`; admit gate propagation `85efe9fb`; architecture audits `a7797877`, `84473e15`; follow-up boundary fix `6f18485e`
- 生产合并结论：`PRODUCTION_MERGED`（四开关的现有生产接线；完整 0.6.2 仍为 `INCOMPLETE`）

## 本次目标

2026-11-18：[维护撤回与单项控制兼容](maintenance-control-revocation-20261118.md)已生产默认修复。
Sleep `06c8cfc4` 同事务复查触发 scope 控制，18 格9→18；setter `5407430f` 固定旧
fallback，96格92→96，单项 accept_events 不再意外切换其他控制。44项追加、29文件
733 passed/2子测试及全部指定静态门通过，五owned Any零诊断。完整另一scope触发的
全局维护过滤、pending/硬退出和其余生命周期未由此证明，0.6.2仍INCOMPLETE。

2026-11-17 核对：四开关已分别用于生产事件接收、检索、维护和发送准入；缺省设置开启，显式关闭继续
生效。本次修正文档中遗留的 shadow 描述，不重新开启用户已经关闭的控制，也不宣称整个 0.6.2 验收完成。

2026-10-21 后续边界更新：`34e201dc` 已移除 scoped TypeError 后无 scope 重试、旧 learning reader
降级及 post-turn 缺 authority 放行；settlement 改为真实 scoped accept_events，保留 settings_error
健康状态。见 [Scoped Control Failure Closure](scoped-control-failure-closure.md)，该安全修复单独
PRODUCTION_MERGED，不代表整个四开关验收完成。新 [Provider Control Revocation](provider-control-revocation.md)
六项实际 entry→HTTP 对照确认：首轮后 session 关闭 admit_hints，两个协议均重发历史 hint。
整个 0.6.2 仍 INCOMPLETE。以下早期实验的兼容说明不代表当前 runtime 仍可无 scope 放行。

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

结果：controls/admission `15 passed`（最新 scoped export fixture 单独 `11 passed`）；
kernel transport 全集 `1002 passed`。

```powershell
python -m pytest tests/test_kernel_legacy_parity.py -q
```

结果：当时 `694 passed`。无 state 时默认放行的实现已被后续
[Research State Contract](research-state-contract.md) 撤销；当前 fixture 必须显式提供生产 state，
control 缺失拒绝。不得恢复该 fallback。

## 实验配置

- 模型 / suite / endpoint：无模型；离线临时 state fixture；无 endpoint
- tasks / cells / reps：四个控制各 1 个独立开关案例；legacy 全关/恢复案例 1；
  user/project/session inheritance 1；restart/concurrent writers 1；
  retrieval/maintenance boundary 1；admission on/off provider capture 1；control surface/export 1
- on/off 对照：单控制切换与其它控制保持开启；legacy `disable_updates` 作为
  全关兼容对照；admission `admit_hints=True/False`

## 结果与限制

- 四个控制可独立写入和读取；旧 settings 可以继续读取。
- retrieval、maintenance、admission 和 event acceptance 已在代码边界接线；
  scope inheritance、restart/concurrent settings、expiry、export/delete 和真实 kernel
  provider capture 已有 deterministic evidence。
- 仍尚未完成 scope export/reset 的控制 manifest、并发 runtime
  请求矩阵、成本/延迟测量和完整 0.6.2 acceptance battery。
- 本实验没有 live/offline 模型行为对照，也没有证明任何 language 或 command
  收益；不能把它当作 Ghost language 晋级。
- 四开关已有真实生产消费者；控制接线本身不新增模型可见内容。历史离线实验的验证范围与
  完整 0.6.2 未完成项继续保留。

## 已知问题和回滚方式

- `learning_enabled` 仍是旧 API 名称；后续可以在完成迁移矩阵后拆分 UI 文案，
  但暂不删除兼容入口。
- `496247fc` 的早期回滚说明不适用于当前多次接线后的生产树；本轮可分别 revert
  `06c8cfc4`、`5407430f` 后空闲重启，保留 SQLite、显式控制值与原始历史。

## 下一步

1. 为四个控制补充 expiry、scope delete/export/reset 和 control projection replay
   fixture。
2. 初始 `admit_hints` 关闭、scoped 读取失败和首轮后关闭的实际 entry→HTTP 已验证；当前
   Attempt Gate 已拒绝历史重发。下一步覆盖正常发送、native results/repair/receipt/resume/
   compaction，保留拒绝后的恢复语义，再测成本/延迟。
3. 保持完整 0.6.2 `INCOMPLETE`，现有生产控制继续生效；后续扩展单独验收。
4. World Model 未验证候选保持 shadow；两个重复当前事实的实机候选已终止。新候选必须
   增加预测信息，经独立 ContextSource/admission、单模型收益与生产边界验收后默认接线；
   不将 shadow 理解为永久不晋级，也不重跑已完成的重复事实候选。

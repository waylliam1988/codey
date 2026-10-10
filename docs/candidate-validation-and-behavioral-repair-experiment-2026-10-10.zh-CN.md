# 停滞候选验证与行为修复隔离实验

2026-10-10。本文保留最初的隔离实验边界，同时记录已经合入工作区的生产实现与实机结果。当前工作区尚未 commit 或 push。

**隔离实验结论：候选流程在 16 个确定性用例中全部通过，原实现为 11 通过、5 失败。随后生产实现完成了真实回放验证；隔离脚本本身仍不代表模型胜率。**
它证明停滞后的候选验证可以闭合，并能守住修复后证据要求；没有证明真实模型必然遵循修复提示，
也不能当作 KoboldCpp 实机 A/B 或 20/20 agent 成绩。

## 实际运行的边界

- 经过 `run_task_submission()`、真实 project adapter/kernel 和真实工具执行。
- 真实运行 `python -m unittest discover -v`，最初测试针对原始实现失败。
- 真实 exact replacement；正确修改后，三次过期 SEARCH 均未应用。
- 真实固定 Python 行为 worker、受管输出、工作区身份、review snapshot、完成门与持久化状态。
- 模型输出由公开的脚本序列替代；reviewer 的批准故意可以是错误的。
- `current` 不覆盖生产逻辑，目标行为缺失导致真实断言失败，不使用 xfail。
- `candidate` 只在当前 pytest 测试进程临时覆盖相关函数，fixture 结束即还原。
- 两种最终 variant 使用相同实验脚本指纹；没有访问模型或消耗 API token。

## 红 → 隔离候选 → 绿

初次 13 用例基线为 4 失败、9 通过。扩充行为缺失、拒绝 review、语义错误候选后，
最终基线为 **5 失败、11 通过**。

第一版候选仍有 2 失败、11 通过：持久化状态明确拒绝从 `no_progress` 重启 Writer，
提示 `writer restart requires a new attempt after done before final proof`。
该失败保留在原始实验报告中，未通过伪造 `done` 或跳过 mutation 来消除。

随后在测试原型中增加一次明确准入：已有实际修改、首次 Writer 停滞、无取消、授权读写与验证、
至少剩余两轮、尚无最终 proof。持久化转移必须与这一准入、attempt 和预算相符；
原 `no_progress` 事实保留在历史记录中。最终候选 **16 通过**。

| 验收 | 当前实现 | 隔离候选 |
|---|---|---|
| 正确补丁 + 三次过期 SEARCH → 新测试、行为观察、review、完成 proof | `no_progress`，失败 | `done`，通过 |
| 取消、禁止验证、禁止修改、剩余预算为零（4 用例） | 不能完成，通过 | 不能完成，通过 |
| 普通测试仍失败的停滞候选 | 不能完成，通过 | 真实重测失败，不能完成，通过 |
| 已有实际反例 + 需要明确操作提示的脚本策略 | 请求临时命令，`approval`，失败 | 复用反例，修复并重验，通过 |
| 已有正常行为修复链 | 新测试、新观察、新 review，通过 | 同样通过 |
| 修复后缺少新测试或新行为观察（2 用例） | 不能完成，通过 | 不能完成，通过 |
| 修复后 review 缺失、过期或拒绝（3 用例） | 仍 `done`，失败 | 不能完成，通过 |
| 即使收到提示，仍申请未经批准的临时探测 | `approval`，通过 | 保留 `approval`，通过 |
| 可见测试和 reviewer 都通过，但行为仍错误 | 不能完成，通过 | 不能完成，通过 |
| 可见测试通过、行为错误的停滞候选 | 不能完成，通过 | 不能完成，通过 |

负向用例不接受 `error` 或 `provider_failure` 冒充保护成功。
正向用例核对实际 exit code、最终源码哈希、新行为输出引用、review 的源码与最终 satisfied proof。

## 这次额外锁定的要求缺口

生产 review 协调器目前采用 best-effort 语义：未获得 review 或 snapshot 已过期时返回原 Writer 结果。
这解释了三条新失败。它与“修复后的候选必须获得新 review 才放行”这一目标不一致。

隔离候选仅约束续接候选，不把整个产品所有首次任务改成强制 review。
生产落地时应由既有候选完成流程明确表达该要求，而不是建立另一套 review 存储或完成状态机。

## 可以合入什么，尚不能证明什么

1. **停滞候选收尾有确定性可行性。** 一次获准验证后再进入现有行为检查、review 与完成门，
   不需要放宽 exact replacement、不需要提高停滞次数、不需要把停滞直接标成完成。
2. **修复后证据链可以锁住。** 旧测试、旧行为观察、缺失/过期/拒绝 review 不能替代当前证据。
3. **修复提示收益仍需实机验证。** `needs_guidance` 用例明确按提示选择路径；这是测试策略，
   不是 LLM。另一个用例故意忽略提示，结果仍然停在审批，证明没有偷偷放开命令。

不能将测试原型直接当生产实现复制：它使用测试进程 admission 集合和固定 fixture 验证 provider。
正式实现必须绑定本任务已准入的验证命令，并把有界准入落实到既有 runtime mutation 边界。
合入后应对真实模型的两个失败场景回放；在此之前不能声称实机不稳定性已消除。

## 生产实现与实机回放

生产代码现在通过 `codey/operations/project_candidate_validation.py` 共享一次运行时验证机会。
执行前被策略拒绝的探测保留审计但不写入已执行验证；实际执行但退出结果缺失仍阻止完成。
行为修复会保留真实反例，先取得修复后的新行为观察，再取得当前快照 review 和完成证明。
operation state 保持严格 schema 1，冷启动不做迁移；review 重启保留已经消耗的 turns。

生产 KoboldCpp A/B 第八轮记录在
`artifacts/candidate-recovery-production-ab-20261010-08/result.json`：新版本四个场景全部通过，
基线三个通过，双方误完成均为零。新版本结果为：test-first 59.748 秒、normalize-name
64.648 秒、stale-search-replay 40.714 秒、counterexample-repair-replay 100.015 秒。
两个原生场景和两个记录前缀场景分开解释；这只覆盖两个已授权任务契约，不能宣称通用需求理解
或效率优势。第六轮 stale 超时、拒绝探测证据污染和所有边界失败记录均保留。

最终全量为 **8392 passed、7 skipped、1504 subtests**；机器契约为 **1008 passed、14 subtests**。

## 提交后定向回放

预算和 schema 修正后的第九轮 stale-search 新版本在 42.412 秒通过。第九、十轮反例回放都取得了
正确源码、真实测试通过和新行为观察，但 Local Reviewer 返回 `changes_requested`，意见承认实现正确
却提出假设性风险；完成门按契约阻塞。两轮均无误完成。这说明 review 输出随机性仍然存在，不能用
放宽 review 或外部 oracle 补齐完成证明。

## 检查与复现

相关旧回归 **29 通过（8.28 秒）**：行为完成修复、失效观察、只读停滞、修复上下文与审批暂停。
新增脚本 Ruff、compileall、diff 检查通过。未运行全量 pytest、mypy、Pyrefly 或实机模型 A/B。
本轮未更新全量 `TEST_REPORT.md` 或发布 changelog，避免把隔离实验写成已交付的产品功能。

```powershell
python -m tests.manual.candidate_validation_and_behavioral_repair_experiment --variant current --report artifacts/candidate-recovery-current.json
python -m tests.manual.candidate_validation_and_behavioral_repair_experiment --variant candidate --report artifacts/candidate-recovery-proposal.json
```

`current` 预期退出码 1，`candidate` 预期退出码 0。文件名不匹配默认 `test_*.py`，
只有显式运行才会执行实验；不会让默认全量 pytest 因预期测红而失败。

- [实验脚本](../tests/manual/candidate_validation_and_behavioral_repair_experiment.py)
- [仅限测试的候选原型](../tests/manual/candidate_recovery_test_proposal.py)
- [完整机器可读结果与来源指纹](reports/candidate-validation-and-behavioral-repair-experiment-2026-10-10.json)

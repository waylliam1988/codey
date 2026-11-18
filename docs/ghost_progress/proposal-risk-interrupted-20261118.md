# 条件接受率与纠正成本：用户中断批次收尾

2026-11-18，状态 `INCOMPLETE`，停止原因 `USER_INTERRUPTED`。
冻结提交 `ede91617`；[冻结协议](proposal-risk-protocol-20261118.md)保持原样。
生产决策 `NO_PROMOTION_INCOMPLETE`，不是阴性或已完成的效果实验。
本记录提交可用 `git log --grep="close interrupted conditional acceptance trial"` 定位。

用户先暂停目标，随后要求把当前批次收尾、更新文档后停下。本次只完成证据核对、
相关检查与文档交接，不继续生成，不开启下一实验。

## 已完成和缺失

冻结矩阵为20条合成经历流、三阶段四臂，共240格。暂停时完成155格，
留下156条物理尝试请求记录；第156条为stream12/phase2/retrieval，
有request-1.json和durable SQLite，但没有reply.json、result.json或完整usage。
服务端最终结果未知，不把被中断请求算作有效样本，也不将其成本补成0。
另84格从未启动；12条流完整，第13条流完成两个四臂阶段及第三阶段的三臂。

按运行前的缺失数据与禁止重试规则，这批保持INCOMPLETE。
不补发中断格，不选取完整子集重算通过率，不调用完整矩阵summarize，
不调整主基线、评分、样本或门槛。未来要重启这个方向，须先明确本次中断导致的
证据缺口及新批次设计；不能将本批追加请求伪装为一次未中断的冻结矩阵。

新增消费者是用相同情境/策略的历史明确接受或拒绝，估计未来接受概率与拒绝纠正成本，
影响推荐哪个任务方案或先澄清。无额外Ghost生成、HF或embedding。
原始历史、条件计数和概率/效用预测各有独立ContextSource与现有admission；
预测不生成用户事实、evidence_refs、权限或完成依据。
这只是实验入口，尚无经过收益和生命周期验收的正常生产消费者。

## 不完整数据的描述统计

以下仅描述已保存的完整回复，分母不一致，不能据此判定收益、阴性或成本门：

| 臂 | 完成格 | 正确格 | 合法标签 | 已报告输入tokens | 已报告输出tokens | 部分样本p95毫秒 |
|---|---:|---:|---:|---:|---:|---:|
| none |39|8|31|56420|632|2781|
| retrieval |38|22|38|163924|646|4406|
| counts |39|30|39|172328|663|4547|
| forecast |39|30|39|184438|663|4641|

已保存38个完整四臂阶段的76条review：hit45、miss7、unjudged24。
52条可判定观测的描述性Brier均值0.1435642722；协议要求80条，因此预测质量门未评估。
动作正确性是冻结数学决策策略的代理，合成反馈不是实测真实用户满意度或校准。
非流式p95是kernel返回时间，不是网络首token或流式体验。

13份已保存事件JSONL合计501275字节、单流最大43269字节；本地live目录核对时
60304150字节，包含SQLite、请求、回复、预测和review审计。完整静态诊断另留contract目录。
这些均是中断时的局部成本，不能宣称完整20流成本门通过。

## 证据核对与工作区边界

逐一核对155格的单次请求、temperature0/output8192、reported usage、wire SHA256、
实际summary与发送前保存的精确动作评分，全部一致。没有重试。
冻结实验脚本及测试的源字节未改变。脱敏报告见
[proposal-risk-20261118.json](../../tests/manual/ghost_memory_routing/reports/proposal-risk-20261118.json)。

原始本地证据：

- `.e2e-artifacts/proposal-risk-20261118-live/paused.json`：原始用户暂停时点与计数。
- `completed.json`、各格request/reply/result、before/review、JSONL、SQLite：完整留存。
- `.e2e-artifacts/proposal-risk-20261118-contract/interrupted-audit.json`：逐格核对及源hash漂移。
- 同目录`audit-interrupted.ps1`与检查日志：本次收尾证据。

收尾期间工作区出现本批之外的生产/前端修改，涉及typed_fields、ghost_context、
project_adapter、project_experiences、project_goals、permissions及local_context_drawer。
这些修改正在并行发生，未由本批编写、回滚或纳入本批提交；原tar删除及两份HTML也保留。
因此不能声称当前整个工作区与实机源快照一致，亦不替外部修改确认新的生产默认范围。
本批只记录冻结实验未改变和逐格证据完整性，不覆盖另一批的晋级或回滚判断。

## 验证

运行前相关契约155项通过，全仓Ruff通过；两个本批文件Pyrefly/ty零错误，
额外mypy Any审计owned零诊断。完整额外mypy命令exit1，11995项/333个未修改依赖诊断
原样保留，不能把过滤后的owned零诊断写成完整命令通过。

收尾仍串行执行，不跑全量pytest，不为规避并行修改而缩小既定命令：

```powershell
python -m pytest tests/test_proposal_risk_trial.py tests/test_semantic_goal_trial.py tests/test_interaction_feedback_trial.py tests/test_project_goal_production.py tests/test_project_experience_production.py tests/test_ghost_experience_role_budget.py -q
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

下表记录各次检查实际加载/读取的快照。检查后仍持续出现并行修改（包括共享fixture、
kernel_request_builder及新的personal_context）；部分缺口可能随后被所属批次调整。
本记录保留原始结果，不将旧失败描述成新源字节的最终判断，也不跟随另一批修改循环验收。
当前工作区最终集成验证由该批次接手。

| 检查 | 完整命令退出码 | 实际结果 |
|---|---:|---|
| 六文件相关pytest，较早进程 |0|249 passed；进程加载代码后工作区继续变化，不能代表最终源字节 |
| 同一六文件pytest，当前复验 |2|6项collection错误，共同依赖已被外部修改删除的常量 |
| 全仓Ruff |1|project_goals.py import格式I001 |
| 完整705文件mypy |1|ghost_post_turn.py:71，AffinityHint元组赋给推断为tuple[()]的变量 |
| 完整生产Pyrefly |0|0 errors，35 suppressed/159 warnings另列 |
| 完整生产ty |0|通过 |
| kernel precision |0|通过；不代表全仓Any清零 |
| 累计24文件常规mypy |1|274错误/11文件：累计owned4项，依赖270项 |
| 累计24文件Pyrefly |1|4 errors，11 warnings另列 |
| 累计24文件ty |1|4 diagnostics |
| 累计24文件额外mypy Any |1|14053错误/352文件：累计owned8项，依赖14045项 |

累计owned清单及每条命令完整参数在报告validation中，原始输出与exit code留
`static-final-status.json`和对应未过滤日志。本批两个冻结Python文件在常规mypy、
Pyrefly、ty及额外Any输出中均无自身诊断；**不能据此称累计24文件或当前全树通过**。
前次依赖269/14107等数字属于前次源快照，当前并行修改已改变诊断集合，未冒充不变基线。

收尾检查时阻塞的具体位置与归属：

- `codey/operations/project_experiences.py`删去`VERIFIED_PROJECT_EXPERIENCE_MODEL`，
  `tests/test_ghost_experience_role_budget.py:27`仍导入；六个回归文件共享这一fixture，全部在收集阶段失败。
- `codey/operations/project_goals.py`删去`VERIFIED_PROJECT_GOAL_MODEL`及`_configuration_enabled`，
  `tests/test_project_goal_production.py:424/428/433`仍引用；三个专项检查器均定位到这四项接口缺口。
- `ghost_context.py`的并行类型收窄使既有`ghost_post_turn.py:71`的空元组推断错误显现；
  这是全树mypy当前错误，不由本批实验改动引入。
- `project_goals.py`的新import布局导致Ruff I001。

这些文件正在由其他工作修改；本批不覆盖它们、不恢复已删除的模型限制、不删除旧断言，
也不替另一批调整消费者契约。交接给修改所属批次修复并重验；本批仅提交中断记录与索引，
**检查未全通过，不能宣布当前生产树交付通过或新的生产晋级**。

## 停止与交接

本批未新增生产消费者、开关、权限或默认预测来源，不撤回此前已晋级功能。
完整有收益的World Model、自由文本反馈可靠写回及更广泛个人适应仍未完成。
本批提交后按用户要求停下，不自动续跑中断矩阵、重跑旧负结果或创建下一候选。

# 人工确认项目目标：生产接入与验收

2026-11-18，验收通过，等待独立生产提交进入master；尚未标记`PRODUCTION_MERGED`。
用户可见行为：用户明确确认或纠正存储设计优先级后，新会话将其用于新的方案取舍。
沿用[冻结实机结果](semantic-goal-result-20261118.md)，20/20完整三阶段流对无历史0/20，
与相同经历检索持平；不称人工状态或World Model额外准确率收益。方案预测收益门失败，不进入产品。

## 正常入口与默认范围

正常项目任务走App→task entry→project completion/writer→project adapter→shared kernel→HTTP。
无需新开关：有合法人工确认目标、项目scope和现有retrieve/admit控制开启时，受测范围默认投影。
只在普通`coding_writer`、任务含`storage`/`存储`/`儲存`时消费`storage_design`目标。
planning/research/chat及其他任务不投递这个来源；保留本地状态不等于其他模型行为已通过。

唯一已测Gemma4-12B Q4_K_M，loopback HTTP `/v1`、OpenAI Completions/native、temperature0、
非流式，window/output/safety/keep_recent=262144/8192/24576/32000，input_limit=None。
模型、端点、协议、tools、temperature、stream或预算不匹配均回退；不扩大原output128经历来源。
源历史3200字符、确认目标600字符，超预算整组回退，不截断后冒充完整经历。
永久目标无TTL，读取受现有committed消费窗口、输入receipt和来源退役约束；不是任意长期保留承诺。

## 明确的写回信号

现有`POST /api/ghost/action`增加有限的`propose_project_goal`动作，不是自动理解或自动接受。
例如先保存一条正常已完成的项目请求，再提交：

```json
{
  "action": "propose_project_goal",
  "id": "已提交的目标说明run_id",
  "session_id": "该说明的session_id",
  "project": "项目绝对路径",
  "scope": "project",
  "priority": "offline_access",
  "evidence_quote": "该已保存用户请求中的原话"
}
```

另一个有限值为`shared_editing`。动作仅建候选；随后沿用已有审核界面的accept/reject，
或同一API的`accept_candidate`及返回candidate id。没有新增前端表单，本批信号入口是现有API加已有审核。
初始确认后，每次纠正/维持还必须给`reply_run_id`与`reply_digest`（目标已保存实际assistant文本的SHA256），
指向本项目中比该说明更早的已完成实际回答。不能绑定当前/未来、其他项目、已删、截断或变更的回答。
超过保存的assistant上限或无法完整绑定时拒绝，不猜测完整正文。

提案及接受均尊重scoped accept_events；缺引文、错误priority/scope/身份或不完整归因返回400，
真实存储失败按既有500隔离。读与写同一Ghost事务，失败不新增candidate或事件。
较新的目标已审核后拒绝接受旧来源，避免延迟审核回滚已纠正理解。

同值维持也按run保留独立候选和反馈绑定，不把新依据合并覆盖旧run身份。
确认当前目标读取inbox，**不强化到通用Hebbian图/continuity**，不增加通用typed-field allowlist，
因此不能经其他family绕过模型/config边界。它也不显示为缺图节点的修复任务。

## 来源权威、恢复与删除

`ghost_project_goal_history`和`ghost_confirmed_project_goal`为两个独立ContextSource，
只有coding_writer profile许可；任何恢复后的实际transport来源仍由既有发送前callback逐份复查。
新入口的合法目标不能为恢复的另一份旧来源背书。装配之后撤回、删除、纠正或配置改变，
旧请求必须在HTTP前被拒绝；新请求重新装配合法来源。

投影每次重新读committed观察，检查原始引文、完整已保存source digest、目标run/reply digest、
scope及canonical sequence顺序（支持同秒发生的回合）；不只相信candidate或图中的摘要。
源/目标退役或离开窗口后不能继续使用该绑定。旧superseded目标不借最新来源缺失而恢复为当前目标。
合法的新确认仍可在历史来源退役后使用自己的合法新依据；没有第二套持久journal或新store。

用户可关闭retrieve_projections/admit_hints；关闭accept_events只停写回，不撤回既有合法读取。
reject当前目标或delete_scope/reset可撤回；删除沿用现有Manifest七store调度和持久retirement，
重启或候选/图replay不恢复被退役来源。逻辑删除与磁盘原始审计的retention/purge是不同事实。
故障时新请求省略投影；已装配请求如果失去权威则阻止旧payload发送，不自动补发。

## 验收、失败归属和回滚

冻结前92项契约通过，实机240请求完整。16文件相关回归**369 passed /154 subtests**；
之后补齐图隔离及新依据替换旧退役来源，最终目标/控制面/中文三文件**84 passed**。
相关normal App→HTTP覆盖提案待审、初始/反转/维持、
重启、确定性inbox重放、并发同来源提案、scope/context、两个读取控制与单独写控制、
拒绝/删除/reset、来源及target变更/退役/窗口、预算、模型/config回退、装配后撤回及恢复旧来源。

最终五个全树门均exit0，串行执行，未跑全量pytest：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m pytest tests/test_project_goal_production.py tests/test_ghost_control_surface.py tests/test_ghost_language_production.py -q
```

累计22个owned Python文件（含脚本/测试）三个检查器及额外mypy
`--disallow-any-expr --disallow-any-explicit`均无owned错误；无显式Any/cast/ignore。
专项mypy完整命令exit1：未修改依赖269错误/8文件；额外Any命令exit1：14107/351文件。
与上一批依赖诊断逐条相同，仅输出顺序变化，未过滤或改成成功状态。
Pyrefly生产0 errors、35 suppressed/159 warnings，owned 0 errors/11 warnings；ty通过。
705个生产文件和11个owned测试/脚本共716份最终源码hash留本地；这些不是全仓Any清零声明。
命令清单、脱敏验收摘要见[生产报告](../../tests/manual/ghost_memory_routing/reports/project-goal-production-20261118.json)，
完整日志/exit ledger在`.e2e-artifacts/semantic-goal-20261118-contract/`。

首轮测试遇到正常项目的外部浏览器/顾问发现等待；诊断栈已保存，一次诊断进程出现Windows
access violation。单模型HTTP fixture明确提供“无外部顾问”，仍走原consensus和项目内核；
共享测试helper统一拥有这个外部边界，生产发现逻辑未改。没有把外部等待计作模型负结果。
旧fixture误用不存在的inbox公开rebuild入口，改为验证既有事件replay函数并写回同一投影；
七次任务的请求计数修为严格七次，保留“Ghost不增加生成”断言。
无依据引文/已退役来源的500已修为400；控制关闭已有事件与失败写回事件分开比较，原失败日志保留。

立即回滚可关闭项目retrieve/admit控制；代码回滚只撤本批生产提交，状态仍留在原inbox/观察中，
未知typed pair不会被旧通用directive渲染，不重放为模型事实。不需要第二模型或新默认关闭实验壳。
完整自动理解/临时目标、任意长期容量、流式、顾问多模型组合与有收益World Model生产闭环仍未完成。

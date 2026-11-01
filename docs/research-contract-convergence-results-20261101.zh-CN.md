# Research 合同收敛：分批实现与实机验证

本轮结合 OpenScience 对照与第一性原理复查实施。约束：沿用同一 kernel、原计划与结算回执、原 KnowledgeStore/effect、唯一冻结报告；保存不等于 answered。内容验收未通过，不推广自主检索和完整 WAL，不强制迁移新 strict 任务。

## 第一批：当前冻结合同贯穿提示、工具与反馈

- 从同一 TurnSnapshot 判断局部模式；首轮、native 协议修复、完成修复、输出上限续行均使用局部提交路径。
- 局部提示不再带全局 coverage、编码测试和启发式领域方法；普通全局模式保留。
- save_finding 成功只回传事实，不硬编码全局 done；候选仍 unreviewed。
- “转移数据”不再触发临床指导；局部计数缺口的正文与 allowed_actions 一致。
- 恢复沿冻结请求，不改历史请求正文或旧协议定义。

验证：79 项相关 pytest 通过（local contract、answer focus、local scope、relation targets、deterministic completion、read projection），包含已有进程中断恢复。另批提示所有权、请求投影及 findings 测试已通过；新测试首次两处构造参数错误已修正。mypy、pyrefly、ty 的 Python 3.11 检查及改动文件 ruff 通过。

实机：`.e2e-artifacts/research-contract-convergence-20261101/batch1-clean-contract/`。原 minimal 五题、74/75/76 seed、原材料顺序、原评分文件、固定模型可见时钟；未切换模型或采样参数。matrix.json 保存逐项人工四维评分、原始响应与请求定位。

| 原始完整问题 | 内容通过 | 观察 |
| --- | --- | --- |
| 单写者及原因 | 3/3 | 每次正确保存后立即选择 |
| 只读完整条件 | 3/3 | 版本、三条件与 OR 保留 |
| checkpoint 停止及原因 | 2/3 | seed 74 漏 end mark 边界 |
| checkpoint 记录与继续 | 0/3 | 两个所问事实正确，但主动附加错误停止边界 |
| 缺直接证据 | 0/3 | 两次最终 unresolved 前曾错误 answered；一次重复读取后 no_progress |

共 8/15 内容通过。done 仍不能代表内容正确。缺口评分保留“首次错误肯定”与“最终结束”差别，不能以父 gate 最终拒绝来算模型答对。清理合同后，直接证据四题没有重复保存或错误空提交；这只是本次样本观察，不能据此证明因果贡献。原固定 minimal 为 7/15，后端相同 seed 也非严格确定，不能把一题差异解释为稳定提升。

下一批只在同一固定证据层继续：缩小所问关系，保留完整原文与原整题标准；之后独立试验显式的新 finding 与局部选择合并提交。两者都不降低内容门槛。

## 第二批：缩小生成关系，材料保持完整

局部指导明确：只表达当前所问关系，不主动添加邻近机制、背景和推测；父问题用于理解而非扩写。条件、否定、例外及完整 OR 列表仍须保留。没有自动按标点拆题，也没有新增阶段或调度状态。

固定证据实验增加两个受控拆分：checkpoint 停止边界/停止原因，以及进度记录位置/后续继续机制。它们仍属于同一个原 q1；只读三个替代条件不拆。仅实验工具允许这些固定拆分，未强制替换默认计划。

34 项相关 pytest 通过，mypy/pyrefly/ty 的 Python 3.11 检查及 ruff 通过。实机路径分别是本轮 artifact 根目录下的 `batch2-focused-minimal/` 和 `batch2-split-relations/`；原题、材料、时钟、seed、评分保持不变，分别隔离提示收窄和显式关系拆分。

原 minimal 内容 12/15：四道直接证据题各 3/3；缺证据题 0/3，结束分别为错误 done、max_turns、no_progress。恢复题仍加了未问的停止描述，但这次与原文一致；不能将“不要扩写”当作已可靠遵守。错误 done 说明现有父 gate 仍可能接收真实引用上的错误因果解释，须保留这个盲区，不能称为语义认证。

额外拆分六次按原整题评分为 4/6：停止 3/3，恢复 1/3。后两次恢复在第二单位又附加错误停止边界。相比这一批未拆分的对应六题 6/6，未见收益；重复结论和调用数反而增加。因此保留显式 work_units 能力和诊断 fixture，不自动推广此种更细拆分。所有请求均携带同一固定时钟，评分文件未改。第 6 步仍未通过。

## 第三批：同一提交中明确声明正文与采用

原 `question_details` 增加显式可选 `submission_mode=inline`；缺省仍为 select。模式归属于原计划，随后冻结在 scope v3 中，没有环境开关或可写游标。scope v1/v2 保持旧字段、旧选择形式；旧冻结请求不会自动获得新字段。

局部提交只允许三种明确选择：

```json
{"state":"answered","finding":{"claim":"一条完整的、有原文依据的本题回答","passage_ref":"p..."}}
```

或者 `state=answered/partial, findings=[已有精确 fN]`，或者 `state=unresolved, findings=[]`。新 finding 与已有选择两种形式不能混用。状态由模型明确声明。新 finding 通过原 `_execute_research_write → save_finding` 保存；原 submit effect 的同一结算回执携带原知识 staging 材料和规范选择。选择记录只保存精确引用，不复制答案正文；不修改冻结 fN 集合来绕过校验。合并模式隐藏独立 save 动作，候选仍 unreviewed。

恢复实测发现并修复了真实缺口：ToolSpec 标为 safe，但原 replay 白名单及 research pending 分支不允许 submit_research_answer，准入后未结算会被记为结果未知。补齐该原动作的精确 JSON 重放形状和原 reducer 分支；仍不重放研究网络抓取。接受前的 staging、原知识幂等身份及 effect 结算继续各司其职，未声称跨库原子事务。

定点：71 项测试及 43 项子测试通过，之后另加的已有候选精确版本与非法重放形状 8 项也通过。包括十个真实子进程退出边界：请求、响应、工具准入、写入后未落回执、已结算交付、kernel 接受、控制、发布前、发布后未落收据及发布收据。另覆盖 text/native 实际 ApiProvider 载荷、partial、空缺口、长度拒绝后局部替换、恢复不重复发布及接受后不重编译。三种类型检查器对 Python 3.11/3.12/3.13 的生产树检查均通过。

实机 `batch3-inline-minimal/`：原五题、原时钟/材料/rubric、原三 seed，未与细粒度拆分合并。内容 11/15：单写者、只读、停止各 3/3，恢复 2/3，缺证据 0/3。恢复 seed 74 仍附加错误停止边界。缺口 seed 74 先提交错误 partial 后改 unresolved；另外两次反复提交 partial+空选择被拒后 no_progress。

合并直接证据题大多一次响应结束；writer seed 74 额外填了未声明的嵌套 question_ref，拒绝后正确修复。调用减少不代表内容提高：上一批相同题形为 12/15，本批 11/15，后端也非严格确定。仍不能通过第 6 步，不推进推广验收。

对应十二个直接证据运行：两步提交共 24 次请求、73,091 输入 token、135.43 秒；合并提交为 13 次、37,237 token、93.85 秒（含一次协议修复；耗时含进程开销）。只比较这十二题，不混入缺口早停。此组支持动作/成本减少，不支持语义正确率提高。

扩大回归：69 个相关文件，**785 passed、43 subtests passed，666.19 秒**；文件清单与日志为 artifact 根目录 `expanded-pytest-files.txt`、`expanded-pytest.log`，未运行全量 pytest。另补两处 native 真实中断恢复通过；reducer 补验 research 的普通 pending read 仍拒绝重放，16 项及 30 项子测试通过。旧 scope 编解码兼容不代表跨任意实现版本执行兼容；原 implementation_id 检查继续保留。

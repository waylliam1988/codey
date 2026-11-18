# 历史投递、来源去重与复核（2026-11-18）

本批修复用户要求的确定性投递边界，不恢复失败的开放协作学习运行时。普通消费者不再通过 Ghost 重贴旧助手答案；项目 writer 保留明确标记的助手历史。原始 observation、既有删除体系和正常模型调用继续复用。

实验冻结与首轮界面负结果提交 `2beb2910`；生产修复提交 `e47f9388`（master）。脚本、后续完整指标和索引维护单独提交；不将本次模型对照记作效果优越性晋级。

## 实际改动与默认入口

- `ghost_context` 的 session 消费采用 requests 视图：只用用户字段检索，只投递完整的已保存用户文本、run 和内容摘要身份。项目 writer 的 project 消费继续采用 dialogue 视图，用户原话与助手输出分开标记，助手输出不代表用户选择或验证。共享个人上下文补充采用 requests 视图。
- 排序、预算适配、呈现和发送前重读采用相同视图。requests 视图不使用旧的双方混合 embedding；使用本地词法检索，避免用助手命中选出不相关用户原话。项目 dialogue 的显式向量选项保留。没有新增 embedding 缓存或 HF。
- 每个角色文本整段保留或省略；在既有存储长度上限处无法证明完整时也省略，不把存储前缀当完整指令。项目可保留短助手建议并明确标出省略的长用户文本；反过来也一样。原始 observation 不修改，不额外生成历史摘要。
- 来源身份为 canonical run、角色、完整投递文本的内容摘要。共享纯选择器在请求冻结前处理来源；已确认目标的历史依据与目标成对保留，优先于重复历史。保留各来源上限，并增加合计 6000 字符（含来源间分隔符）的上限，发送 admission 同样检查。该值是有界组合策略，不宣称最优，不是模型名或输出 token 白名单。
- 去重只依据真实已表示内容：当前协议窗口内的已验证历史行；或 canonical input entry ID 加完整用户消息字节。目标历史中的原话和已绑定的旧助手输出也使用同一身份规则。相同 session、相似文字、导出摘要均不能证明已表示。
- 原始来源快照先复核，再授权选出的快照；去重到零条也保留校验。每次物理发送还检查被省略的角色确实存在于实际 payload，压缩或恢复丢失时拒绝旧请求，需重新组装；不在发送途中替换 prompt 或自动重试。正常主动关闭仍能走基线。
- 人工目标的提案、确认与回复评价继续分开：提案不生效，明确审核才生效；关联旧回复不认证其全部事实正确。泛泛表扬、仅给新一句要求、分析引用提示词，都不会自动产生或强化偏好。现行生产没有通用反馈学习事件流，本轮没有补造第二套记录或奖励权重。

这些是正常入口的默认投递与边界修复，沿用用户先前授权的 API/模型/流式通用入口；未加新的模型开关。它们不代表自动理解、学习或 World Model 已晋级。

## 实机协议与结果

冻结协议见 [实施前协议及独立提示修订](history-projection-protocol-20261118.md)，脚本为 `tests/manual/ghost_memory_routing/history_projection_trial.py`。模型与 `/models` 前后核验一致：本地 KoboldCpp 的 Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M，temperature=0、native tools、output8192、context262144、reserve32768、keep32000、单次请求。

first：8 个合成历史、两种传输、三组，共 48 次。精确 task scorer 全部失败；原始请求与回执显示模型写出了正确参考/建议的完整句子。初始提示未明确枚举 scorer 期望的短标签，评估接口约定不充分；不能解释为生产错误、事实全错或环境故障。保留所有回复和原分数，没有放宽 scorer。

labels-v2：另行冻结更明确的标签提示，同一 scorer、数据、对照和配置，另跑完整 48 次；不是覆盖 first 的重跑。结果如下（两种传输合计）：

| 任务 | 无 Ghost | 冻结的旧原文 | 用途投递 |
|---|---:|---:|---:|
| 当前参考优先的事实题 | 8/8 | 8/8 | 8/8 |
| 项目建议、理由及归属 | 0/8 | 8/8 | 8/8 |

48 格字段格式及归属均通过；每格一次请求，无服务故障、无 Ghost 额外生成。这里是两种模板的 8 个独立状态实例，不是 8 种真实任务，也不能将流式/非流式当独立语义样本。评估工具的强格式结果不能代替自然聊天学习收益。

正常生产入口另外测了 Chat/项目各流式与非流式，共 4 次；全部 done、正确消费、每格一次请求。确认目标来源改为带身份呈现后，另测现成 labels-v3 正常项目入口各传输一格，共 2 次；结果见独立原始记录。总计 102 次真实生成，合成历史 seed 不调用模型。

**新方案没有证明最终回答优于旧投递**：事实题两组均零旧错误，项目正确率相同；冻结的“事实错误减少”效果门未满足。因此不作算法优越性或新学习晋级声明。生产调整依据是用户明确要求的投递契约修复（普通 Ghost 不发送旧助手答案、不预算截断条件、撤回后不放行旧快照），不是将本次对照宣称为净收益成功。

新来源身份有成本：短事实历史 246→361 字符，短项目历史 333→596 字符；非流式平均输入分别 521→584.25、541→686.25 tokens。短例没有节省输入，延迟也未证明改善。非流式实报 tokens 已保存；流式请求明确要求 include_usage，服务仍未返回用量，保持未知。记录首个 JSON 回执和总延迟，不把工具事件时间称作用户可见首字时间。

脱敏分层指标：[metrics](../../tests/manual/ghost_memory_routing/history_projection_metrics_20261118.json)。原始请求、回复、state 和首轮失败留在：

```text
D:/codey-local-evidence/history-projection-20261118-first
D:/codey-local-evidence/history-projection-20261118-labels-v2
D:/codey-local-evidence/history-projection-20261118-normal-first
D:/codey-local-evidence/history-projection-20261118-confirmed-first
```

复现命令（新 evidence 目录，不覆盖失败）：

```powershell
python -m tests.manual.ghost_memory_routing.history_projection_trial --evidence D:/codey-local-evidence/new-first
python -m tests.manual.ghost_memory_routing.history_projection_trial --protocol labels-v2 --evidence D:/codey-local-evidence/new-v2
python -m tests.manual.ghost_memory_routing.history_projection_trial --normal --evidence D:/codey-local-evidence/new-normal
python -m tests.manual.ghost_memory_routing.history_projection_trial --confirmed --evidence D:/codey-local-evidence/new-confirmed
python -m tests.manual.ghost_memory_routing.history_projection_trial --summarize --evidence D:/codey-local-evidence/new-v2
```

## 回归与失败归属

正常入口、关闭、scope、跨会话、过期、撤回、删除、重建、重启、故障恢复、实际 payload、压缩遗漏和恢复快照拒绝均做定向验证，不运行全量 pytest。

最终相关回归 **244 passed、2 subtests passed**；另运行回执/请求合同 **15 passed**，合计259项。命令：

```powershell
python -m pytest -q tests/test_ghost_experience_role_budget.py tests/test_ghost_confirmation_boundaries.py tests/test_ghost_source_selection.py tests/test_ghost_context_admission.py tests/test_ghost_portable_context.py tests/test_project_goal_production.py tests/test_ghost_observation_consumption_view.py tests/test_ghost_attempt_gate_lifecycle.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_history_delete_guards.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_received_input_lifetime.py tests/test_context_source.py
python -m pytest -q tests/durable/test_provider_response_receipts.py tests/test_production_kernel_calls_use_typed_request.py
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

回归后五个静态门全部通过，mypy覆盖709个生产文件；Pyrefly保留既有35 suppressed/160 warnings，与0 errors分开记录。本轮三个测试文件及实机脚本的针对性mypy/Pyrefly/ty均通过。15个本轮源文件、测试和脚本额外使用mypy的disallow-any-expr/explicit/generics与完整签名检查，owned诊断为0；传递导入的旧模块严格诊断另存，未宣称整个依赖树的额外Any审计通过，未添加忽略或豁免。日志保存在 `D:/codey-local-evidence/history-projection-final-*.txt`、`history-projection-regression-final.txt`、`history-projection-any-audit-final.txt`。

保留的中间失败：早期空来源 validator 把主动关闭误判为撤回，属于本轮确定性生产 bug，已修复；ty 发现闭包中可选 sink 未收窄，改为捕获明确的 session 字符串。其余旧断言将整个对话 payload 当成 Ghost 块、预期去重后仍重复注入、在同一非空窗口重新绑定不同 provenance，已按有效接口更新并保留行为断言。新增撤回测试曾在前置 token 计数时提前删除，改为组装后注入，并检查真实 dedup refs；Chat 拦截按既有 provider_failure 终态验证，没有改生产迁就测试。

## 限制与下一步

1. 正常对话窗口仍可能含旧助手回答；本轮不重写用户对话历史，也不保证模型不再犯旧错误。项目 dialogue 仍有历史事实误用风险。普通入口没有新增“按自然语言请求专门回顾旧助手答案”的语义分类器。
2. 真实“去知乎看看”仍需任务/来源回执证明读取；本轮使用 fixture 参考材料，未证明真实知乎访问或对原福州错误的因果改善。
3. 缺省 requests 视图是词法检索；混合 embedding 未被冒充为用户专属表示。长原话被整段省略可能降低接续，需要后续长任务评估。
4. 去重仅覆盖可验证的完整表示，不把摘要或推断当完整来源。压缩造成表示消失时 fail closed，恢复需要新组装，未增加自动恢复运行时。
5. 自主协作偏好提取/反馈学习仍未通过原实机门，本轮不恢复已归档候选；World Model 状态不变。

回滚：用户可关闭 retrieve_projections/admit_hints；开发回滚本批生产提交无需迁移 observation 数据。旧历史投递格式不是数据库合同。不要为了“更聪明”恢复自动接受模型候选或将 done 当正确性证据。

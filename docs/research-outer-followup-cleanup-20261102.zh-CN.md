# Research 外层补查支线清理

日期：2026-11-02。基线：`b7992d19`。本轮处理用户指出的 `research_flow → investigate → pipeline` 悬空接线。生产代码新增 31 行、删除 2247 行，净减少 **2216 行**，删除七个生产模块。

## 删除依据与范围

产品 research 入口通过 `build_task_policy_for_entry` 强制 strict；旧 pipeline 在 `strict_writer_owns_followup` 处分流返回，无法进入外层调查。flow 却仍创建调查回调、轮次计数和额外合成包装。生产中只有这一处 pipeline 构造点，其他生成分支调用方是旧实验或强行打开分支的测试。

- 删除 flow 的调查回调、额外合成包装和传参；删除 pipeline 的外层执行、候选选择、补丁合并，以及对应配置。
- 删除 `operations/research_investigation.py`、`research_synthesis.py`、`evidence_followup.py`，以及 `research/plan_executor.py`、`evidence_rules.py`、`record_merge.py`、`followup_selection.py`。
- 删除旧 invocation 生成工厂、材料采集方法和只有测试调用的材料解码／恢复方法。后两者没有当前生产恢复消费者，不能仅凭“历史兼容”名称继续当作有效能力保留。
- 删除随支线退役而失去消费者的 `ResearchContext.should_stop` 和外层迭代输入参数。实际 writer 的 `stop_flag` 保留。
- 退役三个旧生成实验脚本及其专属测试。离线历史报告分析、实验 gate、共用评分器和对应独立测试继续保留；公共实验夹具中指向旧执行器的网络补丁也已删除。

当前 pipeline 执行一次原 writer，再投影最终审计、证据账本和知识变更。产品 strict 路径的诊断原因及输出字段形状继续兼容；旧外层计数为零。非产品的 nonstrict 直接调用明确返回 `external_followup_retired`，不再生成外层补查。

## 继续保留的有效链条

原任务与跨会话背景 → 原 kernel 内搜索／读取 → 原文窗口与精确版本引用 → 整篇提交 → 原唯一发布事务 → 证据、答案和显式 `knowledge_updates` 积累 → 后续召回与读取。

原 kernel、真实原文账本恢复、版本及归属校验、完整正文修复、重复错误止损、发布幂等和取消恢复均保留。proof review、findings、gaps、来源信任与 query plan 保留为审计投影；有调用方不等于会自动驱动补查，本轮没有把这些审计结果重新接入生成。

通用 kernel 中用于旧合同识别的 `evidence_followup` 分类及持久化字符串没有机械删除，不将这些兼容标识称作当前独立补查能力。

## 测试迁移与失败归因

原恢复夹具通过 monkeypatch 强行打开已不可达的外层分支，现改为真实产品策略、实际原文读取、`submit_research_answer` 和原发布事务。在八个崩溃位置验证原请求、一次读取、一次答案发布和恢复不重发；同时保留取消、冻结输入篡改和 provider 结果未知时停止重发的检查。已知未发送的请求保留前一轮待送达回执位置，未知结果保留第二轮请求位置，测试按原持久化语义分别断言。

保留独立的暂存知识、归属、链接、回滚及账本错误检查。增强审计测试：即使产生可执行的缺口与查询计划，也只调用一次 writer，不触发外层搜索／抓取，账本、变更与 trace 仍正常投影。

AST 清单记录 **86 个消失或替换的测试函数名**，其中包含旧能力退役、恢复用例改名和架构检查迁移，不能把这个数字理解为全部删除且没有替代。逐项清单及最终选集见配套 JSON。

首次扩大检查为 1,044 passed、502 subtests passed、8 项失败，保留原 `expanded.txt/xml`。其中七项在独立 `b7992d19` worktree 原样复现：一个旧夹具没有非空任务和已登记问题；三项仍检查已删除的 `_coerce_*`；合同集合漏掉当前四个研究工具；中立章节渲染已依赖同层 coverage 叶模块；文件行数快照过期。已修正对应夹具与边界断言，删除不存在辅助函数的专属测试，保留混合用例中其他活跃模块的数值防护断言。

另一项失败源于本轮曾在扩大检查运行时修改测试文件：父进程仍持有已加载的旧路径列表，读取已删除的执行器失败。最终重跑同时固定生产与选中测试的字节摘要，避免将运行中改动后的检查结果作为最终证据。

行数门禁有实际结构债务：清理前已有五个文件超过旧上限。本轮将其固定到原提交精确尺寸（API provider 1147、transport 1053、browser search 1233、research tools 1027、operation state 1473 行），不增加余量。**这是门禁快照修订，仍允许原版本已经存在的超长文件；不表示这些文件已完成结构重构。** 本轮未修改这五个生产文件。

## 最终验证

| 检查 | 结果与边界 |
| --- | --- |
| 最终扩大相关回归 | **1106 passed，512 个 subtest 通过，0 失败**；65 个文件或节点选择分两组完整运行，未运行全仓 pytest |
| 生产与测试固定 | 两组结束后，全部生产 Python 文件及选中测试的 SHA256 与开始时一致 |
| 受控模型界面 | 20 组清理前后逐字一致，涵盖整篇、旧 strict、hybrid、chat、兼容补查，初始／已观察状态与 native／text；只证明这些固定场景 |
| Mypy | `codey --python-version 3.11 / 3.12 / 3.13` 均通过，各检查 575 个生产源文件 |
| Pyrefly | 三个 Python 目标均 0 errors；仍有 35 suppressed、79 warnings |
| ty | 三个 Python 目标均通过 |
| 剩余导入与 Ruff | `codey / tests / tools` AST 搜索没有已删除模块的导入；`ruff check codey tests` 通过 |

本轮没有调用本地研究模型。依据是分支对产品不可达、固定界面一致，以及真实当前路径与恢复测试；不宣称研究正确率提高，也不据此保证整个仓库再无死代码。

[机器可读报告](reports/research-outer-followup-cleanup-20261102.json) 保存最终选集、分组结果、类型检查、删除清单、接口指纹和行数快照。原始日志位于 `.e2e-artifacts/research-outer-followup-cleanup-20261102/`，包括首次失败、原版本复现、最终完整回归及源码固定记录。

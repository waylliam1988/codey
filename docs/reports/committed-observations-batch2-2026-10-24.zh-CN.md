# 第 2 批：提交与恢复

按相关测试及无新增功能回归准入。没有运行全量 pytest，也没有增加性能对照。

## 收敛后的路径

采集到临时 ledger → 同一观察投影函数校验临时视图 → 原有完整结果回执提交 → 发布。
恢复从已提交回执进入同一 `project_result_observation`，保留整批原子发布及 effect 去重。
没有新增日志、持久状态、运行时或旧签名回退。session.executed 是提交后发布的进程索引，
不再把它误称为 durable receipt；来源事实的恢复依据仍是受管完整回执。

参考 DeepSeek Harness `packages/core/agent-loop/src/tool-calls.ts:146` 的 `commitReady`：
第 156 行先 appendToolResult，第 157 行再接受 additionalContexts。本批借鉴提交先于
上下文可见这一边界，继续使用 Codey 原有 KernelEffectSink 和 durable 恢复。

## 故障与回归

- 新测试通过真实 SQLite sink 在提交前和提交后注入中断；两者均不提前发布正式事实。
- 提交后中断的搜索和长来源可以恢复，保留最终 URL/完整正文，重复恢复不累计、不重采集。
- 两个合法连续搜索的实时和恢复事实一致；失败采集临时记录不能污染正式 ledger。
- 知识采集视图由已恢复 ledger 重建，不信任陈旧的 sources_read 缓存。
- 新投影暴露了“未执行 run 被记为失败验证”的差异，已通过原有未执行标记修复，保留
  原测试 `verifications == []`。没有放宽它。
- 旧单测把内存字典当成 durable receipt，并要求提交失败后仍将其视为成功；该断言改为
  不发布，且用真实提交后中断/恢复测试保留无重复执行的要求。
- 相关 20 文件选择：164 passed（33.25 s）。最后缓存修正后的相关选择：79 passed
  （14.27 s）。选择重叠，不相加。前者 JUnit 在
  `.e2e-artifacts/committed-observations/related.xml`。
- Ruff、mypy（512 源码文件，Python 3.11）、Pyrefly、ty 通过。Pyrefly 现有 29 个
  抑制项与 80 个未展示警告保留，不宣称零警告。

## 实机

沿用第一批真实 Gemma、本地连接和生产内核运行器，受控来源 backend。
`artifacts/committed-observations-live-20261024-01/` 中，原生/文本协议各执行重定向
读取与读取失败，共 4/4 通过；实际请求、已提交结果及 proof 保留。恢复事实一致，
重复恢复稳定且没有新增 backend 调用。这不是互联网质量或操作系统 kill 测试。

外部知识文件写入与 edit 的实际副作用不能通过丢弃临时视图回滚，本批未伪造此保证。

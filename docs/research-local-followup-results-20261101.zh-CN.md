# Research 局部工作流复查后的分批修复与验证

基线：`ef3904f5`。继续使用同一个 kernel、原计划和结算回执、原 KnowledgeStore 与唯一接受后的 FindingReport。本文区分协议/恢复验证与人工内容验收；不把工具执行成功当作语义正确。

## 第一批：修复类型边界

为 ResearchIssue 的既有序列化结构声明 TypedDict；冻结 scope 逐字段编码、校验后显式构造，保留 v1/v2 格式。可空计划在调用边界明确拒绝，控制层复用已收窄的计划变量；补齐消息与预算容器类型。没有新增忽略规则或弱化类型检查。

验证：Mypy、Pyrefly、ty 的 Python 3.11 全生产树检查全部通过。局部 scope、关系目标、报告要求、实际请求投影、provider 回执五个相关测试文件共 73 项通过（115.06 秒），包含旧格式往返、非法字段、缺计划和进程中断恢复；ruff 与 diff 检查通过。

实机：原 12B、原 writer_limit 材料与 seed 74，经生产入口得到 done，回答包含单写者及唯一 WAL 文件原因。原件：`.e2e-artifacts/research-local-followup-20261101/batch1-writer-smoke/`。本次是类型修复后的冒烟，尚未固定时钟，不用于与历史结果作因果比较。

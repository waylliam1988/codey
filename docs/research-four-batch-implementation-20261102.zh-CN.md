# Research 四批实施与验收记录

实施基线：`751c6f46`，2026-11-02。用户授权每批一个 commit，定向及相关扩展回测、Gemma4 12B 实机验证；不跑全量 pytest。原有未跟踪审计文档保留，不混入生产改动。

## 固定验证边界

使用已有 `tests/manual/research_framework_comparison.py`、透明 wire 代理和原 headless 入口。模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`；temperature=0.3、top_p=0.9、seed=74、thinking=false、输出至多 4096 token、上下文 32768。每个目录冻结 runtime/harness/material 指纹；矩阵运行期间不改生产源码。

原始数据在 `.e2e-artifacts/research-convergence-20261102/{baseline,batch-a,batch-b,batch-c,batch-d}`。不是新研究运行时；这些目录仅保存隔离测评状态。复用历史冻结题，不能作为未见材料泛化证明；单 seed 不证明稳定优于旧版。正文完成、内容正确、知识积累分别判断。

修改前两题：WAL 3 次请求、asyncio 2 次请求，均发布。WAL 将“只读打开替代条件”误解成“WAL 替代回滚日志”，漏掉三个 OR 条件，另有性能外推；asyncio 保留取消传播不受 return_exceptions 影响，但对子任务取消不连带取消其他任务的表达仍不够明确。不能把发布成功算内容全对。

## A：版本邻居与历史格式

- `KnowledgeRecall.neighbors`：入边严格匹配目标 revision；按 `(id,revision,relation)` 去重，沿用原权限和来源 head 策略。
- `restore_result_receipt`：按 out_/artifact_ 明确分发。旧格式先验证原 JSON 字节摘要、session/run 存储归属、effect/tool 身份和非截断，再走共同内容校验。新写仍用 JSON artifact。
- `KernelResult`：新写显式 schema_version=2，严格读取已知旧六字段、七字段；旧结果只补 research_report=None，不补成功证明。
- 保留 `751c6f46` 的个人/项目归档与历史材料通道；已有权限测试继续执行。

验证：新增兼容测试 13 项通过；扩展命令 83 项通过；另一个涉及原决策、回执和严格字段的命令 20 项通过。命令有重叠，不相加成独立用例总数。新测试最初误用不存在的 KnowledgeStore 上下文管理器，修正测试后通过；未为通过测试放宽生产权限。

实机：WAL/asyncio/RLock/长手册/跨来源共 5 例均交付，请求数分别 3/2/2/3/3。RLock 正确区分内部解锁与普通 release；长手册实际读到尾部，必要恢复关系完整。WAL 仍误解第二问，跨来源仍漏 24 小时 TTL（沿用旧评分，题目未直接问期限的局限保留）。本批证明确定性修复未阻断研究，不声称提升内容能力。一次使用错误 case 名 `long-manual` 被参数检查拒绝、未进入推理；按真实冻结 ID `long-recovery-manual` 补测，原记录保留。

## B、C、D

后续逐批记录，未完成前不视为验收。

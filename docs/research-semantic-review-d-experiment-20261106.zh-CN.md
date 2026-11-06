# D 实验工作记录

`7a5032e8` 冻结了 6 份自然 Research 答案、三组处理合同、模型请求和评分边界。后续分析没有重跑模型，只对冻结结果修正了一个评分代理问题：原先把“不能证明本机速度更快”误识别为错误模式；修正后的评分另存于 `research-semantic-review-d-analysis-20261106.json`，不改变任何请求或答案。

运行命令：

```powershell
python -m tests.manual.research_semantic_review_d --phase prepare
python -m tests.manual.research_semantic_review_d --phase run
python -m tests.manual.research_semantic_review_d --phase export
python -m tests.manual.research_semantic_review_d_analysis
```

D 是离线实验，不导入生产 Research 执行路径。控制组不调用模型；自查和 B 反馈各调用审查一次、修订一次。输入中没有金标、修订评分或新搜索结果。

结果：两种修订都补回 `known_url` 与 `two_errors` 的候选 claim；B 反馈另外修复了部分 `source_discovery` 引用表达。两组关键要求均为 62/68，控制组 58/68。不能据此证明 B 反馈优于普通自查，也不能据小样本证明完整语义修订收益。

最终检查：D 相关 12 项合同/评分测试通过；Ruff 通过；生产代码无变更。原始运行、响应和 ZIP 校验和保留。

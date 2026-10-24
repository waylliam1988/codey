# 第 5 批：跨领域对照及发现问题的修复

验收通过。比较的是原提交 dd51049e 与前四批加本批协议修复，采用功能收益且无新增
回归的准入，不运行全量 pytest，不进行性能对照。

## 固定条件

九个场景各运行原生/文本两种协议，每版 18 条：科学样本、软件迁移、历史统计、
长来源、零结果、读取失败、解释、中文创作、固定 JSON。来源为受控夹具，判分要求
预定字段、实际打开来源、指定步骤、引用最终 URL 和 JSON 格式；没有模型评委。
不是互联网检索质量或开放世界全部事实断言的评测。

模型为本机 koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M。
实际请求均为 temperature=0.3、max_tokens=1024、thinking=false，没有提交 seed。
窗口 32768、输出预留 1024、保留上下文 12000；这些配置在两份相同运行器中一致。
不是确定性采样或统计显著性的宣称。

题集 SHA256：a090426a7f587ab03b943f91776284644adcbb5d90c414ca94f92a8228af82b0。
比较器还校验运行器 hash、每个结果与摘要一致、完整场景集合及实际生成参数。

## 本批确实发现并修复的回归

首次候选的原生固定 JSON 未完成，而基线通过。原始响应为
`tool_calls: [{"product":323,"id":"..."}]`，不是合法函数调用。
原修复提示只要求工具调用，模型连续重发同类坏对象直到轮次耗尽。

修复的是共享 kernel_prompt 的原生协议修复分支：最终答案可以通过已声明 done 的
summary 字符串重新提交，summary 保留用户格式。没有把坏对象解析成成功答案，没有
猜函数名，没有放宽原生 schema，没有为乘法、股票或具体题目写生产规则。
脚本化真实 ApiProvider 测试要求坏响应后必须出现合法 done；实机修复记录中确实先有
坏响应，随后才出现合法 done，最终得到 `{"product":323}`。

## 最终结果

| 指标 | 原版 | 最终候选 |
| --- | --- | --- |
| 预设回答/来源/格式检查 | 18/18 | 18/18 |
| 恢复及重复恢复一致 | 16/18 | 18/18 |
| 无需来源场景的不必要工具调用 | 0 | 0 |
| 未完成场景 | 0 | 0 |

两个明确收益是长来源在原生、文本协议下从恢复失败变为成功。最终比较的 regressions
为空，accepted=true。原版、首次候选失败、修复单场景及最终完整重跑全部保留：

- `artifacts/cross-domain-baseline-20261024-01/`
- `artifacts/cross-domain-candidate-20261024-01/`（17/18 回答检查，不能合并的首次结果）
- `artifacts/cross-domain-comparison-20261024-01.json`（accepted=false）
- `artifacts/cross-domain-json-repair-20261024-01/`
- `artifacts/cross-domain-candidate-20261024-02/`
- `artifacts/cross-domain-comparison-20261024-02.json`（accepted=true）

最终候选 manifest 中 revision 是父提交 0d6c64dc；它另外包含本批 kernel_prompt 修复。
该生产 diff 与摘要保留在最终候选的 production.patch/source-state.json，不能把首次
候选与最终候选误说成同一份生产代码重复运行后挑选成功结果。

最终相关 pytest：98 passed，13.56 s；Ruff、mypy（513 源码文件）、Pyrefly、ty 通过，
目标 Python 3.11。前端发布一致性检查通过。Pyrefly 的现有警告及抑制项保留。

## 复现

在两个 checkout 中使用完全相同的 tests/manual/cross_domain_evaluation.py：

```powershell
python -m tests.manual.cross_domain_evaluation --model MODEL_ID --output OUTPUT_DIRECTORY
python -m tests.manual.cross_domain_compare BASELINE_DIRECTORY CANDIDATE_DIRECTORY --output REPORT_JSON
```

运行器直接使用生产内核与真实回执，不实现自己的模型/工具循环。原版的长来源导致
运行器退出 1 是已记录的失败，不是整个比较未运行。

这支持“修改版事实记录和恢复可靠性有明确改善，固定相关验证没有新增回归”。
不支持“消除了幻觉”或“已在整体能力上胜过三个参考项目”。open_hit 的原有跳过行为
仍保留为已知限制；历史上已经裁坏的回执也不能凭空补回正文。

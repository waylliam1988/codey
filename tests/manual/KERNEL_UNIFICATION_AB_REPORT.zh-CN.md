# Codey 统一 Kernel 前后本地实机 A/B 报告

日期：2026-09-30  
基线提交：`958bcb485bf05d0ae8232763681d1df5ecee1d34`  
模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`  
任务：只修改 `app.py`，实现 `normalize_name` 的小写化、去标点、空白规整和连字符连接，并运行 `python -m unittest discover -v`。

## 实验边界

本实验比较的是 coding/research kernel 统一前后的执行稳定性，不比较模型推理能力。两边使用同一模型、同一任务、temperature `0`、`max_tokens=2048`、独立 fixture 项目和进程外验证器。r1-r4 默认关闭 native tools，专门覆盖文本工具协议；请求和响应由 proxy 留档在对应结果目录。

指标包括 task success、patch correctness、独立测试是否通过、终态是否误报、重复工具调用、mutation 次数、耗时和 token usage。没有注入中断，因此 recovery success 在这些轮次中未测量。

## 结果

| 轮次 | 版本 | task success | patch | 测试 | stop reason | 重复调用 | mutation | wall time | tokens |
|---|---|---:|---:|---:|---|---:|---:|---:|---:|
| r1 | 统一前 | 否 | 是 | 是 | blocked | 0 | 1 | 15.941s | 10588 |
| r1 | 统一后 | 否 | 否 | 否 | protocol | 0 | 0 | 5.296s | 3368 |
| r2 | 统一前 | 否 | 是 | 是 | error | 0 | 1 | 148.982s | 9358 |
| r2 | 统一后 | 否 | 是 | 是 | error | 0 | 1 | 146.515s | 9176 |
| r3 | 统一前 | 否 | 是 | 是 | error | 0 | 1 | 260.208s | 11406 |
| r3 | 统一后 | 是 | 是 | 是 | done | 0 | 1 | 51.657s | 9528 |
| r4 | 统一前 | 否 | 是 | 是 | blocked | 0 | 1 | 13.025s | 10588 |
| r4 | 统一后 | 是 | 是 | 是 | done | 1 | 1 | 26.421s | 9765 |

### r1 暴露的真实缺口

r1 的旧版模型输出标准 JSON，旧版完成了正确修改；统一后模型输出了完整的 Gemma 文本帧：

```text
<|tool_call>call:tool:read_file{args:{path:"app.py"}}<tool_call|>
```

统一 kernel 按协议边界拒绝了该 provider markup，任务以 `protocol` 结束。这是 provider adapter 缺口，不是 kernel 应该认识 Gemma 模板的理由。

### 修复后的边界

新增 `codey/providers/local_response_codec.py`，仅由 local provider 的 `normalize_reply()` 使用：

```text
local provider raw response
  -> local response codec
  -> AssistantTurn / ProviderToolCall
  -> unified kernel ToolSpec/policy validation
```

codec 只接受完整 frame，使用安全字面量解析并生成稳定的 local call id；半截帧、前后多余文本、非法表达式保持为普通文本。未知工具仍由 kernel 的 ToolSpec/policy 校验拒绝。Qwen 或其他模板以后应增加 provider codec 的独立解析分支，不能把模板标记塞进 kernel。

r4 在修复后完成了真实 text-mode 任务，独立 unittest 通过且只落地一次 mutation。该轮模型主要返回 JSON/fence，因此 Gemma frame 的确定性覆盖由 `tests/test_local_response_codec.py` 锁定；r1 则提供了实际本地模型输出该 frame 的回归样本。

## 判断

统一 kernel 在 r1 暴露了旧版隐藏的 provider 适配缺口，但拒绝未知模板的行为本身是正确的信任边界。补上 adapter codec 后，统一 kernel 可以同时保留严格协议和本地模型可用性：模型模板由 provider 负责归一化，kernel 继续只处理标准 turn。

r3/r4 的成功不能单独证明统一 kernel 在所有任务上更快或更强；它们说明在同一模型和任务下，统一后的执行链能够完成真实修改、独立验证和结构化终态。r2 的两边均因模型输出截断失败，属于 provider/model 响应预算问题，不能归因于 kernel 统一。

## 验证

- codec 与 provider/kernel 边界定向套件：`77 passed, 5 subtests passed`
- `ruff check codey tests tools`、`compileall`、`git diff --check`：全绿
- 最终全量：`5092 passed, 32 skipped, 1471 subtests passed in 353.14s`
- 未发布

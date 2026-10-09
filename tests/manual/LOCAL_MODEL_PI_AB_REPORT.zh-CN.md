# Codey vs Pi 本地实机稳定性 A/B 报告

2026-10-09 注：本文为历史结果，旧脚本现已重命名为
`codey_vs_pi_agent_stability_ab.py`。当前多场景比较见
[新报告](../../docs/codey-vs-pi-agent-stability-2026-10-09.zh-CN.md)，以下测量保持原记录。

日期：2026-09-30  
模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`  
任务：在 `app.py` 中实现 `normalize_name`：小写化、去标点、去除首尾空白、合并连续空白并用连字符连接；不得修改测试；运行 `python -m unittest discover -v`，测试通过后才能完成。

## 1. 实验目的

本实验比较的是**同一个模型下两个 agent/kernel 的执行稳定性**，不是模型推理能力。模型、任务、temperature 和 token budget 尽可能一致；Pi 和 Codey 使用各自标准 provider/agent 工具调用接口。

关注指标：

- task success
- patch correctness
- tests actually passing
- false completion
- repeated tool calls
- duplicate mutation
- wall time
- token usage
- recovery success

## 2. 控制条件

- 模型：同一 KoboldCpp Gemma 12B
- temperature：`0`
- max token budget：`2048`
- 每轮使用独立 fixture 项目
- 测试由 harness 在 agent 进程外再次执行，避免只相信 agent 自报结果
- Codey 使用 native tools
- Pi 使用其标准 provider 工具接口
- 实验记录保存在 `tests/manual/results/real-local-ab-12b-r1..r7` 对应目录中；结果目录被 pytest 排除，不参与自动测试收集

## 3. 逐轮结果

`correct patch` 表示独立验证器确认 patch 正确且测试通过。`task success` 还要求 agent 进程和最终完成状态都正常。

| 轮次 | Pi task success | Codey task success | Pi correct patch | Codey correct patch | Pi repeated calls | Codey repeated calls | Pi wall time | Codey wall time |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| r1 | 否 | 否 | 否 | 否 | 0 | 0 | 12.150s | 6.239s |
| r2 | 否 | 否 | 否 | 否 | 1 | 1 | 280.692s | 23.734s |
| r3 | 否 | 否 | 否 | 是 | 0 | 1 | 35.673s | 17.826s |
| r4 | 否 | 否 | 否 | 否 | 15 | 0 | 124.788s | 5.105s |
| r5 | 否 | 否 | 否 | 否 | 0 | 0 | 12.175s | 95.375s |
| r6 | 否 | 否 | 否 | 是 | 0 | 0 | 12.425s | 116.332s |
| r7 | 是 | 是 | 是 | 是 | 0 | 0 | 144.415s | 50.807s |

### Mutation 结果

- r7：Pi 和 Codey 都只执行 1 次成功 mutation，均无 duplicate mutation。
- r2/r3：Codey 早期版本出现 2 次 mutation 和 duplicate mutation；这是测试过程中发现并修复的真实 kernel/完成路径问题，不能从最终 r7 结果中隐去。
- Pi 在失败轮次没有形成成功 mutation；r4 出现 15 次重复工具调用但没有正确 patch。

### Token 结果

- Codey token usage：r1-r7 分别为 `3216、9348、7581、3358、8846、8667、9492`。
- Pi 的 KoboldCpp streaming 响应没有提供 usage 字段，因此 token usage 保持 unavailable，没有进行估算。
- 因此 token efficiency 不能做严格的 Pi/Codey 数值比较。

### Recovery 结果

r1-r7 没有注入中断、进程崩溃或重连故障，因此 `recovery_success` 未测量。不能把未执行的恢复场景写成成功或失败。

## 4. 最终可比结果：r7

r7 是当前修复后的稳定版本在同一模型和同一任务上的最终实机对比：

- 两边都正确修改了 `app.py`。
- 两边独立测试都通过。
- 两边都没有 false completion。
- 两边都只执行一次 mutation，没有重复 mutation。
- Codey 用时 `50.807s`，Pi 用时 `144.415s`，Codey 约为 Pi 的 35%，约快 2.84 倍。
- Codey 产生了可信的 `task_done`、verification receipt 和 completion 状态；Pi 本轮没有提供同等结构化的 token/完成 receipt。

## 5. 结论

### 5.1 不能得出的结论

本实验不能证明 Codey 的模型推理能力强于 Pi。两边使用同一个模型，模型本身负责主要的理解和代码推理能力。

本实验也不能证明 Codey 在所有任务、所有模型或所有硬件上都更快。r1-r6 是连续调试和修复轮次，不是相互独立的统计样本。

### 5.2 可以得出的结论

在这组本地 Gemma 12B 条件下，Codey 最终表现出更好的**执行稳定性**：

1. **更少的无效工具调用**：r1-r7 Codey 重复调用累计 2 次，Pi 累计 16 次；Pi 的峰值出现在 r4，为 15 次。
2. **更强的完成约束**：Codey 在 r3/r6 已得到正确 patch 但验证证据或 provider 状态不完整时选择 blocked/error，而不是把任务伪装成成功。这降低了 false completion 风险，但会牺牲部分表面成功率。
3. **更可审计**：Codey 的完成状态包含 verification receipt、变更摘要和可信状态；这次 A/B 中 Pi 没有提供同等结构化的完成证据。
4. **最终执行更快**：r7 Codey 在相同模型和任务下明显快于 Pi，但 token usage 只能获得 Codey 一侧，因此速度优势不能直接解释为 token efficiency 优势。
5. **副作用控制更明确**：经过 r2/r3 暴露并修复 duplicate mutation 后，r7 两边都只产生一次 mutation；Codey 对这类约束有内核级 receipt、settlement 和幂等检查。

## 6. 工程判断

如果目标是轻量、短交互，Pi 的实现更简单，可能已经足够。

如果目标是需要真实修改、测试证明、避免重复副作用、失败可解释、后续可恢复的编码任务，当前证据支持优先使用 Codey。Codey 的优势不是“更会想”，而是把模型输出转换成受约束、可验证、可恢复的执行过程。

下一步要补的是**真实中断恢复 A/B**：在同一任务中注入 provider timeout、进程崩溃和 SSE reconnect，然后比较两边是否能恢复、是否重复 mutation，以及恢复后的最终 receipt 是否一致。

## 7. canonical old/new 协议后的复测（2026-10-02）

本节是协议收敛到唯一 `replacements[{old_string,new_string}]` 后的独立复测。模型仍为同一 KoboldCpp Gemma 12B，temperature 为 `0`，Pi 与 Codey 使用同一任务和同一 `max_tokens`，每个 arm 使用独立临时项目。

| 预算 | Pi | Codey | 说明 |
|---:|---|---|---|
| 2048 | patch/测试通过，task success | patch/测试通过，但 `finish_reason=length`，task failure | Codey 已完成 1 次 edit 和测试，末轮未生成完成调用；严格终态为 provider failure |
| 4096 | patch/测试通过，task success | patch/测试通过，但仍 `finish_reason=length` | Codey 仍无重复 mutation；不能把截断当成完成 |
| 8192 | 未形成完整结果 | 未形成完整结果 | KoboldCpp 生成持续增长，客户端断开；该轮未写入 `result.json`，不计入统计 |

2048 与 4096 的完整记录位于 `artifacts/real-local-ab-20261001-final` 和 `artifacts/real-local-ab-20261002-final`。Codey 的事件日志显示：两轮都只读两次、编辑一次、运行一次，没有重复 mutation、没有重复工具调用、没有 false completion；失败发生在最后的结束响应被模型截断之后。该行为说明当前内核正确保留了“未收到合法完成调用就不能宣布成功”的约束。

这次复测没有证明旧 kernel 更好。旧 kernel 对工具参数形状更宽松，冷启动时更容易把 schema 漂移隐藏起来；当前 kernel 使用单一 canonical old/new 协议，并把模型模板解析留在 provider 层，协议错误和截断会显式失败。对于本地 Gemma，当前剩余限制是 provider/model 的结束调用和输出预算，不应通过 kernel fallback 放宽。

## 8. Provider 归一化与有界截断续轮（2026-10-02 TDD）

本轮先写红测，再实现 provider 层协议：

```text
模型原始响应 -> provider codec -> AssistantTurn / ProviderToolCall -> kernel
```

Gemma/KoboldCpp 实际输出的：

```text
[TOOLCALL REASONING]: {"reasoning":"...","final_decision":"yes|no","tool_name":"..."}
```

被记录为 `AssistantTurn.raw.provider_metadata`。`final_decision` 和 `tool_name` 不会
生成工具调用，也不会使普通文本完成任务。Ollama 原生 `/api/chat` 的 `message.tool_calls`
通过独立 codec 归一化；缺失 call id 时使用稳定的 provider 层 id。kernel 没有加入
Gemma、Qwen、Ollama 或 DeepSeek 字段。

`finish_reason=length` 的策略是一次续轮：续轮消耗正常 turn budget，模型必须自己发出
合法工具调用或 `done`。再次 `length` 直接是 `provider_failure`；“Tests passed”之类的
普通文本不会进入 completion gate，也不能完成任务。

### 本轮实机证据

| Probe | 结果 |
|---|---|
| `real_local_done_probe.py`，Gemma 12B，`max_tokens=512` | 1 次 native `done` 请求，1 个 `task_done`，Codey `stop_reason=done`；同一服务也观测到普通文本 `finish_reason=length`，没有被当成完成 |
| `real_local_ab.py`，Gemma 12B，temperature 0，`max_tokens=2048` | Pi：patch/测试通过，task success；Codey：patch/测试通过，1 次 edit、无 duplicate mutation，但末轮第二次 `length`，严格终态 `provider_failure` |

这说明 Pi 捕获的是 provider 标准 `stopReason=length`，不是 Gemma 的
`final_decision` 字段。Codey 采用同一原则，同时保留一次有界续轮和严格完成门。

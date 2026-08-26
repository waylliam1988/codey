# Codey Tool Protocol Portability Plan

## 定位

这份文档记录 Codey 面对一个长期风险时的工具协议策略：

```text
模型厂商用自家 coding/research harness 做大量后训练
  -> 模型更熟悉厂商原生 tool schema / agent loop
  -> 第三方 harness 如果发明大量专有工具语言，会额外损失模型能力
```

Codey 的长期原则是：

```text
Own the semantics, minimize the syntax.
```

也就是：Codey 掌握执行语义、权限、安全、证据、验证和完成判定；模型可见的工具语法尽量少、稳定、通用，并允许未来按 provider 做很薄的协议适配。

这不是 0.4.13 的主线功能。0.4.13 仍然以 Verified Completion Enforcement + Repair Context Admission v1 为主。本计划只规定哪些预留可以在 0.4.13 安全完成，哪些必须留到 0.5 之后。

截至 0.4.15，0.4.13 的 trace-only protocol telemetry 已落地，0.4.15 又闭合了
`run` 命令 argv 文件系统 operand 的项目边界。本文档中 0.5 之后的部分仍是后续
protocol portability 的有效计划；0.4.13 小节保留为已完成版本的设计边界记录。

## 核心结论

coding 和 research 都要纳入 tool protocol portability，但不能强行统一模型可见工具名，也不需要为了“看起来统一”新增一套 semantic taxonomy。

```text
应该统一：trace、telemetry、contract hash、repair 统计、A/B 指标
不该统一：把 research open_url 改名成 read，或把 knowledge_write 改名成 write
```

原因是 coding 和 research 的资源边界不同：

```text
coding read_file          -> read project_file
research open_url         -> read web_source
coding edit               -> write project_file
research knowledge_write  -> record knowledge_note
coding run                -> verify project_command
research done             -> finish research_answer
```

名字强行统一会制造歧义；额外 taxonomy 如果没有直接消费者，也只是第二套隐形工具语言。0.4.13 更应该复用现有边界：`phase`、`tool_name`、`protocol_error_kind`、`model_tool_contract_hash` 和 repair 次数。

### Research 与 Coding 的弥合边界

Research 工具也要做 portability，但解决方式不是把 research 工具改造成 coding 工具名，也不是新增一套跨域语义映射表。更稳的做法是：模型可见工具保持领域清晰，trace/A-B 使用现有字段弥合。

```text
model-visible tool name:
  coding:   read_file / grep / edit / run / done
  research: web_search / open_url / knowledge_write / done

shared observability:
  phase: writer / research
  tool_name: exact model-visible or runtime tool name
  protocol_error_kind: no_json / unknown_tool / native_tool_denial / native_search_leak / ...
  model_tool_contract_hash: exact tool contract seen by the model
```

这样 Codey 可以在 trace、A/B、repair、policy 层统一回答：

```text
这个 provider 在 writer 还是 research 阶段更容易协议失败？
它失败在 no_json、unknown_tool，还是 native_search_leak？
某次回归是模型变化，还是 tool contract hash / prompt hash 变化？
它是否倾向绕过 local Research web_search，改用 provider native search？
```

模型仍然看到领域清晰的工具名。`open_url` 比 `read` 更能表达“读取网页来源并进入 research source ledger”，`knowledge_write` 比 `write` 更能表达“记录知识笔记而不是改项目文件”。这和 `Own the semantics, minimize the syntax` 不冲突：Codey 最小化的是专有动作语言，不是抹掉不同领域的工具边界。

## 参考项目结论

Pi、OpenCode、OpenScience 的共同模式不是“每个模型写一整套大方言框架”，而是：

```text
canonical IR 稳定
wire/provider 边界各自适配
参数错误在边界修正或给模型明确修复文案
只有某个模型族确实有明显母语优势时，才条件暴露不同动作语言
验证、证据和完成契约留在 runtime 内部
```

对 Codey 来说，最值得吸收的是：

- Pi：工具原语少而正交，消息和 tool call 在 provider 边界归一。
- OpenCode：按模型族做窄工具投影，例如某些 GPT/Codex 路径更适合 patch，其他模型仍用 edit/write。
- OpenScience：模型可见 schema 只是描述，runtime 侧必须重新校验；evidence、artifact、completion contract 不变成模型工具。

## Codey 当前基础

Coding 已经有适合承载该计划的分层：

```text
ChatProvider
  -> codey.protocols.ProtocolCodec
  -> ToolCall(name, args)
  -> ToolDefinition
  -> AgentToolFns / tool_runtime
  -> ExecutionEvidence / Verification / CompletionProof / RunTrace
```

关键文件：

```text
codey/protocols/base.py
codey/protocols/json_codec.py
codey/models.py
codey/tool_definition.py
codey/agent_tools.py
codey/tool_runtime.py
codey/agent.py
codey/run_trace.py
```

Research 有独立但相似的协议边界：

```text
Research JsonToolCodec
  -> ToolCall(name, args)
  -> research tool contract validation
  -> ResearchRunner dispatch
  -> Research ledger / source / quality review / completion gate
```

关键文件：

```text
codey/research/tool_contract.py
codey/research/protocols.py
codey/research/controller.py
codey/research/runner.py
codey/run_trace.py
```

当前主要短板：

- Coding 生产路径主要依赖 `JsonToolCodec`，所有模型都被要求在正文中输出 Codey JSON。
- Coding 兼容逻辑散在 `JsonToolCodec._tool_call()`，还没有成为所有 codec 共用的参数修复边界。
- Research 已经有更严格的 typed contract，但协议摩擦还没有稳定进入 RunTrace。
- Coding 和 research 的协议摩擦还没有进入同一套 bounded telemetry，无法比较“模型卡在工具语言哪里”。

### Action / Command 语义边界

0.4.15 已经先闭合 `run` 命令中带文件系统语义的 argv operand 边界。后续
protocol portability 不能倒退这条规则：

```text
command allowed != command safe
cwd inside project != argv inside project
model-visible bash/run dialect != permission to execute arbitrary filesystem operands
```

因此未来任何 provider dialect、参数修复、structured tool path 或 native function
calling，都必须先 lower 到 Codey 的 canonical action 语义，再进入 ActionPolicy /
ToolRuntime：

```text
provider output
  -> ProtocolCodec / args repair
  -> canonical ToolCall / canonical RunCommand
  -> referenced_paths / cwd / command class analysis
  -> ActionPolicy
  -> ToolRuntime execution
```

Policy 和 executor 必须看到同一份 canonical argv / cwd / referenced_paths。不能只因为
命令名在 allowlist，或 cwd 已在项目内，就忽略 argv 内的脚本路径、pytest path
operand、`-c` config、`--rootdir`、`--confcutdir`、`--basetemp`，以及
`-o addopts=...` / `-oaddopts=...` 这类二层 argv。

## 0.4.13 可以做什么

0.4.13 的协议预留必须满足：

```text
不改变模型可见工具面
不新增工具
不改变 parser 接受范围
不改变 provider 选择
不影响 done enforcement 行为
只增加 bounded trace / docs / tests / internal metadata
```

### 必须守住

1. 不新增 `repair_failed_completion`、`run_completion_check`、`submit_completion_proof` 之类专有工具。
2. Repair Context 用通用行动语言描述事实，例如“the relevant verification failed”，不要要求模型理解 Codey 内部对象名。
3. Evidence、ContextEpoch、CompletionProof、RepairContext 保持 runtime-owned，不暴露成模型可调用工具。
4. `bash`/`run` 语义不因未来 native dialect 改变；所有命令仍走 allowlist 或 user-approved shell。
5. Research 工具名不强行改成 coding 工具名；只在内部 metadata 和 trace 层做语义弥合。

### P0a: 通用协议 Telemetry

这是 0.4.13 最值得做的额外预留。它不改变行为，只记录 bounded metadata。

建议文件：

```text
codey/run_trace.py
tests/test_run_trace.py
tests/test_agent.py
tests/test_research.py
```

#### RunTrace 函数级修改

在 `codey/run_trace.py`：

1. 新增常量：

```python
MAX_PROTOCOL_PHASES = 8
MAX_PROTOCOL_ERROR_KINDS = 16
MAX_PROTOCOL_UNKNOWN_TOOLS = 16
MAX_PROTOCOL_TURNS = 64
```

2. 在 `RunTraceManifest` 增加字段：

```python
protocol_telemetry: dict[str, object] = field(default_factory=dict)
```

约束：

```text
只有 RunTraceRecorder.record_protocol_*() 可以写入 protocol_telemetry。
只有 _bounded_protocol_telemetry() 可以把它投影到 manifest payload。
其他代码不要直接拼写 protocol_telemetry 内部结构。
```

推荐 payload 结构：

```json
{
  "phases": {
    "writer": {
      "codec_name": "json",
      "model_tool_contract_hash": "sha256:...",
      "first_valid_tool_turn": 1,
      "valid_tool_turns": [1, 2],
      "repair_prompt_count": 1,
      "protocol_error_counts": {
        "native_tool_denial": 1
      },
      "unknown_tools": []
    },
    "research": {
      "codec_name": "research_json",
      "first_valid_tool_turn": 2,
      "repair_prompt_count": 1,
      "protocol_error_counts": {
        "native_search_leak": 1
      }
    }
  }
}
```

3. 在 `RunTraceManifest.to_payload()` 输出：

```python
"protocol_telemetry": _bounded_protocol_telemetry(self.protocol_telemetry),
```

4. 在 `RunTraceRecorder` 增加方法：

```python
def record_protocol_codec(
    self,
    codec_name: object,
    *,
    phase: str = "",
    model_tool_contract_hash: object = "",
) -> None: ...

def record_protocol_error(
    self,
    kind: object,
    *,
    phase: str = "",
    turn: int = 0,
    tool_name: object = "",
) -> None: ...

def record_protocol_repair_prompt(
    self,
    kind: object = "",
    *,
    phase: str = "",
    turn: int = 0,
) -> None: ...

def record_protocol_valid_turn(
    self,
    turn: int,
    *,
    phase: str = "",
) -> None: ...
```

5. 私有 helper：

```python
def _protocol_phase(value: object) -> str:
    return _identifier(value, 40) or "unknown"

def _protocol_kind(value: object) -> str:
    return _identifier(value, 80) or "unknown"

def _protocol_tool_name(value: object) -> str:
    ...

def _protocol_bucket(manifest: RunTraceManifest, phase: str) -> dict[str, object]:
    ...
```

`_protocol_tool_name()` 不直接输出原始 unknown tool name，推荐输出 digest + sanitized short label：

```json
{
  "label": "str_replace",
  "digest": "sha256:..."
}
```

要求：

- 不保存 raw assistant reply。
- 不保存 raw prompt。
- 不保存 `plan.protocol_error` 原文。
- unknown tool 默认保存 digest + sanitized short label；不要保存原始 unknown tool name。模型可能把路径、用户内容或别的敏感片段塞进假工具名。
- 所有写入 fail-open，不能让 trace 失败影响任务执行。

#### Coding 接入点

在 `codey/agent.py::run()`：

1. `codec = codec or JsonToolCodec(...)` 后，记录 codec：

```python
codec_name = str(getattr(codec, "name", "") or codec.__class__.__name__)
contract_hash = codec.model_tool_contract_hash()
trace.call(
    "record_protocol_codec",
    codec_name,
    phase="writer",
    model_tool_contract_hash=contract_hash,
)
trace.call("record_tool_contract_hash", contract_hash, phase="writer")
```

2. 当前 `PromptEnvelopeSection` 的 trace source ref 可以从硬编码改成 trace-only 动态值：

```python
source_refs=(f"protocol:{codec_name}",)
```

这不改变 prompt 内容，只改变 trace metadata。

3. 在 parse 后：

```python
plan = parse_reply(reply, codec)
if plan.protocol_error:
    trace.call(
        "record_protocol_error",
        plan.protocol_error_kind,
        phase="writer",
        turn=turn,
        tool_name=_unknown_tool_from_error(plan.protocol_error),
    )
    ...
    repair = _protocol_repair_prompt(...)
    trace.call(
        "record_protocol_repair_prompt",
        plan.protocol_error_kind,
        phase="writer",
        turn=turn,
    )
else:
    if plan.calls or plan.control is not None:
        trace.call("record_protocol_valid_turn", turn, phase="writer")
```

4. 在 “no valid JSON tool call; nudging the model” 分支，也可以记录：

```python
trace.call("record_protocol_error", PROTOCOL_NO_JSON, phase="writer", turn=turn)
trace.call("record_protocol_repair_prompt", PROTOCOL_NO_JSON, phase="writer", turn=turn)
```

这里仍然不改变 parser 行为，只补 trace。

#### Research 接入点

在 `codey/research/protocols.py::JsonToolCodec`：

```python
class JsonToolCodec:
    name = "research_json"
```

在 `codey/research/runner.py`：

1. 初始化/开始运行时，在已有 contract hash 记录附近补：

```python
codec_name = str(getattr(self.codec, "name", "") or self.codec.__class__.__name__)
self.prompt_trace.call(
    "record_protocol_codec",
    codec_name,
    phase="research",
    model_tool_contract_hash=model_contract_hash,
)
```

2. 在 `plan = ...` 后：

```python
if plan.protocol_error and not plan.calls and plan.control is None:
    self.prompt_trace.call(
        "record_protocol_error",
        plan.protocol_error_kind,
        phase="research",
        turn=turn,
    )
    ...
    message = _protocol_repair_prompt(...)
    self.prompt_trace.call(
        "record_protocol_repair_prompt",
        plan.protocol_error_kind,
        phase="research",
        turn=turn,
    )
    continue

if plan.calls or plan.control is not None:
    self.prompt_trace.call("record_protocol_valid_turn", turn, phase="research")
```

3. Research 重点观察的 kind：

```text
no_json
direct_answer
native_search_leak
unknown_tool
invalid_args
disallowed_tool
too_many_tools
```

其中 `native_search_leak` 是 research 最关键的模型协议摩擦指标。

#### P0a 测试

新增或扩展：

```text
tests/test_run_trace.py
tests/test_agent.py
tests/test_research.py
```

覆盖：

- `record_protocol_codec()` 只记录 bounded codec metadata。
- `record_protocol_error()` 按 phase/kind 计数。
- `record_protocol_repair_prompt()` 递增 repair count。
- `record_protocol_valid_turn()` 只保留第一轮和 bounded valid turns。
- coding native tool denial 会进入 writer telemetry。
- research native search leak 会进入 research telemetry。
- payload 不包含 raw prompt、raw reply、raw protocol error text。

### P0b: 文档和架构测试

0.4.13 可以增加文档/架构测试，确保底线不被破坏：

```text
CompletionProof 不出现在 ToolDefinition / TOOL_CONTRACTS
Evidence / EvidenceLedger 不作为模型可调用工具出现
research 工具不被重命名成 coding read/write/search
JsonToolCodec parser 行为未扩大
```

## 0.4.13 不应该做什么

以下内容全部后移：

- 新增 `ClaudeLikeCodec`、`OpenAIFunctionCodec`、`QwenNativeCodec`。
- 新增 `ProtocolRegistry`、`DialectManager`、`ToolNegotiator`。
- 把 `read_files` / `parallel` 移除或改名。
- 把 research `open_url` 改名成 `read`，或把 `knowledge_write` 改名成 `write`。
- 新增 `ToolSemantic`、`action_kind`、`resource_kind` 之类跨域 taxonomy，除非已经有明确 runtime 消费者。
- 引入 native function calling provider path。
- 按 provider 自动切换 tool prompt。
- 新增 `apply_patch` 工具或 patch runtime。
- 大改 `JsonToolCodec` 或 Research system prompt。
- 新增 provider-native web browsing/search 工具。

原因：这些都会改变模型可见 action language 或 parser 行为，必须用独立 A/B 证明收益，不能混进 0.4.13 的 completion enforcement 证据。

## 0.5 之后的演进计划

### P1: Shared Argument Repair Shim

目标：把 coding 里散在 `JsonToolCodec` 的参数别名和宽容处理，收成所有 coding codec 共用的薄层。

建议文件：

```text
codey/tool_args_repair.py
tests/test_tool_args_repair.py
```

职责：

```text
provider/dialect args
  -> repair/normalize
  -> canonical ToolCall args
```

例子：

```text
search/replace/replacement -> old_string/new_string
write_file/content -> edit content
single replacement object -> replacements[...]
JSON string edits -> parsed replacements, fail closed on invalid JSON
numeric strings for offset/limit -> bounded ints
```

注意：

- P1 会改变 parser 接受范围，不进 0.4.13。
- Research 已经有 `validate_tool_args()`，不要盲目合并进 coding shim。
- Runtime 侧仍要重新校验 canonical args，不能信任 codec 已经校验过。

### P2: Tool Prompt Decoupling

目标：让工具描述从 `JsonToolCodec` 的大段硬编码里下沉到 tool definitions 或独立模板，减少未来多 codec 漂移。

建议文件：

```text
codey/tool_prompt.py
codey/tool_definition.py
codey/research/tool_contract.py
tests/test_tool_prompt.py
tests/test_protocols.py
```

要求：

- 每个 coding 工具自己提供 prompt snippet / guideline。
- Research contract 保留领域工具名，但也能导出 model-visible contract description。
- Codec 汇总当前 permission profile 或 research controller 允许的工具。
- `model_tool_contract_hash` 覆盖最终模型可见文本。

P2 可以和 P1 同期做；不适合插入 0.4.13。

### P3: Static Codec Selector

目标：先有安全插槽，但默认不改变任何行为。

建议文件：

```text
codey/protocols/select.py
tests/test_protocol_selection.py
```

初始实现：

```python
def select_protocol_codec(
    provider_id: str,
    *,
    permission_profile: str = "coding_writer",
    model_id: str = "",
    mode: str = "auto",
) -> ProtocolCodec:
    return JsonToolCodec(permission_profile=permission_profile)
```

当前 writer 路径稳定拥有 `provider_id` 和 `permission_profile`；`model_id` 主要留给未来 API/native provider。只有 A/B 证明某 provider 或模型族更适合某个 codec 后，才扩展规则。

P3 可作为 0.5.0 的第一步，不进 0.4.13。

### P4: Conditional Tool Projection

目标：借鉴 OpenCode，仅当某模型族确实更擅长某种动作语言时，暴露替代工具面，但内部执行语义不变。

可能方向：

```text
GPT/Codex-like: apply_patch-like model-facing form
Claude-like: str_replace/view/create-like model-facing form
Minimal: Pi-like read/write/edit/bash surface
Research-minimal: search/open/record/done surface, only if A/B proves useful
```

约束：

- 单次 prompt 只展示一种方言。
- Parser 可以宽容接收少量别名，但 prompt 不混用多套语言。
- 替代工具必须 lower 到 canonical ToolCall。
- `bash` 永远不绕过 Codey run/shell policy。
- Research native search/browse 不能绕过 source ledger、open_url、evidence gate。
- Patch 只有在 Codey 有可验证、安全、原子 patch parser 后才能启用。

P4 只在 P0/P1/P2/P3 完成并有 A/B 数据后启动。

### P5: Native Function Calling / Structured Provider Path

目标：对真正支持原生 tool calling 的 API provider，走 provider-native tool channel，而不是正文 JSON。

注意：这不能只靠 `ProtocolCodec` 完成。当前 web provider 主要是：

```text
send(prompt: str) -> reply_text
```

原生 function calling 需要新的可选 provider 能力：

```text
send_structured(messages, tools) -> assistant_message_with_tool_calls
```

所以 P5 必须作为 API provider 路径的长期改造，不能影响 DeepSeek/Qwen/GLM/MiMo/StepFun 等网页 provider 的稳定性。

P5 仅在 API provider 成为核心路径或实测收益明显时做。

## A/B 计划

未来新增：

```text
tests/manual/tool_protocol_portability_ab.py
```

Coding arms：

```text
codey_json_current
minimal_primitives
provider_native_like
conditional_projection
```

Research arms：

```text
research_json_current
research_controller_current
research_minimal_surface
provider_native_search_like
```

Coding cases：

```text
read-edit-run-done
create-file
exact-replacement
multi-file-read
grep-then-read
failed-test-then-repair
approval-required-command
premature-done
```

Research cases：

```text
web-search-open-url-knowledge-write-done
native-search-leak-repair
direct-answer-repair
source-search-disabled
knowledge-write-before-open-url
done-before-evidence
quality-review-followup
controller-disallowed-tool
```

指标：

```text
first_valid_tool_rate
protocol_error_rate
repair_turns
unknown_tool_count
native_tool_denial_count
native_search_leak_count
alias_rewrite_count
edit_success
verification_success
research_quality_review_success
completion_proof_status
turns
latency
sent_chars
```

只有当某个方言在真实 provider live smoke 或 A/B 中稳定降低 protocol errors，并且不提高 false completion / unsafe action / evidence bypass，才能进入生产默认。

## 成功标准

这条路线成功时，Codey 应该保持：

```text
模型可以说不同工具方言
Codey 内部只有一套执行 IR / research contract 边界
coding 与 research 的协议摩擦通过 phase/tool_name/error_kind/contract_hash 可比较
安全、证据、验证、完成判定不随方言变化
协议选择由数据驱动
没有新增专有 completion/evidence 工具
```

失败信号：

```text
为了适配某模型复制一整套 Agent runtime
CompletionProof 变成模型工具
Evidence / EvidenceLedger 变成模型可编辑对象
同一 prompt 同时展示多套工具语言
provider-specific hacks 进入 tool_runtime
research 工具被改成模糊的 coding 工具名
没有 A/B 就默认切换 provider 方言
```

## 当前决策

0.4.13：

```text
做：
  completion enforcement
  repair context
  通用文案
  P0a trace-only protocol telemetry（coding + research）
  P0b 文档和架构测试

不做：
  dialect codec
  native function calling
  dynamic tool projection
  apply_patch 工具
  research 工具重命名
  semantic taxonomy / action_kind / resource_kind
  大改 prompt / parser
```

0.5.0 之后：

```text
先 P1/P2/P3
再用 A/B 决定 P4
最后只在 API provider 需要时考虑 P5
```

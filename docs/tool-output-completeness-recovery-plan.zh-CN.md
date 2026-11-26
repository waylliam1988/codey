# 工具输出完整性、分页与回执恢复：函数级实施方案

本方案依据当前工作区源码编写。它描述一次完整落地的目标实现，不拆分发布批次。本文中的“新增函数”是建议名称；实施时可以按现有模块命名惯例调整，但职责、数据来源和验收边界必须保留。

本次交付包括全部十类工具、最终模型提示、嵌套子回执、持久化恢复和对应验证。它不包含 Culler 前端实现。

## 1. 先固定架构约束

### 1.1 单一事实来源的具体含义

| 事实 | 唯一权威来源 | 可以生成的派生视图 |
| --- | --- | --- |
| 工具调用身份及参数 | 已冻结的 `ToolCall`、effect intent | 下一步调用示例、回执标签 |
| 文件读取范围 | `canonical.value` 中的起止行、总行数、`text_complete` | 页尾、EOF 提示、完整文件校验 |
| 搜索返回页及扫描覆盖 | `canonical.value` 中的 matches、分页字段、coverage | 下一页、越界、覆盖提示 |
| 命令执行事实 | `ExecutionReceipt` | 执行状态说明、是否仍在执行 |
| 实际保留的命令输出 | `ExecutionOutputStore`；已有旧式输出使用 `managed_output` 所指向的存储 | `ResolvedOutput`、回执读取页面 |
| 知识对象和版本 | 已有 knowledge repository / documents | 当前内容页、引用和续读参数 |
| 浏览器观察 | 已有 observation / `canonical.value` | snapshot display、截断提示 |
| 来源搜索范围 | 本次搜索绑定的 `source_snapshot` | PDF 实际扫描页范围、原文是否完整 |
| CodeMode 子调用及副作用 | 原始 child effects / receipts | `repair_of` 重放、失败说明 |
| CodeMode 大返回值 | 已有 JSON artifact 及其摘要 | 有界预览、`read_tool_result(view="value")` 页面 |
| 工具可用性 | 当前冻结的 `TurnSnapshot` | 直接调用或已有 CodeMode 中的合法调用示例 |

“单一事实来源”不禁止正文、UI 和 JSON 页面有不同显示形式。要求是这些形式都由同一份结构化事实生成，不能各自维护、猜测或反向解析另一份显示文本。

### 1.2 明确禁止的实现

- 不新建 output runtime、recovery runner、独立恢复队列或第二套工具执行入口。
- 不新建与现有 receipt / artifact 并行的输出数据库。
- 不为“提示去重”翻转现有 `ToolOutcome.truncated` / `ToolResult.truncated`。
- 不新增一套需要所有工具同步维护的通用 `complete / recoverable / next_call` 状态表。
- 不把提示正文、`presentation` 的有界投影或 `audit` 的显示字段作为完整性和授权依据。
- 不在 Python、JS 和提示层分别维护一份 repair 错误白名单。
- 不给 `list_dir`、`find_references` 或排序型 `source_search` 增加分页运行时。
- 不保留被替代的实现、永远到不了的分支、无人调用的兼容包装和新增的 `noqa` 豁免。

### 1.3 本次保留和新增的边界

- 保留已有分页坐标：`read_file` / `grep` 从 1 开始；知识和回执文本字符偏移从 0 开始。
- 保留 `read_file` / `grep` 当前使用的终止哨兵 `0`，知识和回执的新页面终止统一使用其接口允许的 `null`。不为了统一外观改机器契约。
- CodeMode 默认 worker 返回硬上限仍为 `200_000` UTF-8 字节；默认内联阈值改为 `32_000` 字节。
- 因此 `32_000 < 返回值字节数 <= 200_000` 使用现有 artifact；超过硬上限使用已有修复重放。没有放大 worker 资源预算。
- `truncated` 继续保留现有机器消费者的判断意义；不同工具具体的不完整范围必须看该工具的结构化事实。

## 2. 把提示注入从数据模型中移走

涉及文件：

- `codey/runtime/core/models.py`
- `codey/toolchain/outcome.py`
- `codey/operations/kernel_result.py`
- `codey/operations/kernel_prompt.py`
- `codey/operations/task_guidance.py`
- `codey/operations/kernel/kernel_context.py`
- `codey/operations/kernel_control.py`

### 2.1 `model_text_with_audit_markers()`

修改现有函数，而不是增加第二个构造流程：

1. 删除依据 `truncated` 自动追加 `TRUNCATED_RESULT_NOTICE` 的分支。
2. 删除此函数的 `truncated` 参数，更新全部调用点和测试。
3. 保留确实仍在使用的 managed-output 审计 footer。它只描述留存对象，不给模型安排重跑或续页动作。
4. 删除不再使用的 `TRUNCATED_RESULT_NOTICE` 常量及导入。

`ToolOutcome.__post_init__()` 和 `ToolResult.__post_init__()` 仍负责 JSON 规范化和既有审计投影。它们不再决定读哪个工具、是否重跑以及怎么翻页。

`ToolOutcome.__post_init__()` 中 `audit["truncated"] = True` 属于现有审计行为，本次不借提示改造删除它。机器消费者也继续使用原布尔值。

### 2.2 `result_from_outcome()`

保留这个唯一的 outcome → result 适配器。它继续无损传递 `truncated`、canonical、presentation、audit，不在这里增加按工具名拼接的恢复正文。

### 2.3 `_receipt_result_context()`

保留回执身份、显示 handle 和已有 source / receipt 坐标区分说明。删除以下无条件推断：

```python
if result.truncated:
    # 一律声称 read_tool_result 能恢复缺失内容
```

同时删除普通非截断工具后面无条件追加的 “If more detail is needed, use read_tool_result.”。是否有可读的额外内容必须有具体依据。

它改为调用一个纯提示函数，例如放在同文件的：

```python
def _output_recovery_context(
    result: ToolResult,
    session: TaskSession,
    *,
    result_ref: str,
    receipt_recovery: bool,
    tool_names: tuple[str, ...] | None,
    nested_tool_names: tuple[str, ...],
) -> str:
    ...
```

该函数只读既有事实、返回字符串；不执行工具、不读取新页面、不提交状态、不持久化恢复策略。按工具语义使用短小辅助函数，避免把所有情况堆进现有大函数。

分派规则：

| 对象 | 输出的恢复说明 |
| --- | --- |
| `read_file` 普通页 | 使用工具自身的范围和下一页 / EOF；不补通用告警 |
| `read_file` 超长行预览 | 明确该行内容被裁剪，不能用作完整替换依据 |
| `grep` | 下一页由真实分页字段决定；裁剪长行可读当前回执的 value |
| `list_dir` / `find_references` | 缩小实际 path；不生成 offset 或虚构的遗漏项恢复 |
| `run` / `shell` | 按真实留存情况读原输出，不建议为取输出重复执行 |
| `browser_snapshot` | 分开处理文本和控件截断，并检查工具可用性 |
| `source_search` | 使用搜索 observation 的覆盖说明 |
| `knowledge_read` | 使用知识页自己的对象版本和续读范围 |
| `read_tool_result` | 继续原始回执，不读取当前分页工具的新回执 |
| CodeMode 外置返回值 | 读取同一个 parent result_ref 的 `view="value"` |
| 未识别的不完整结果 | 仅说明观察不完整，不能当成完整覆盖；不编造恢复动作 |

完整性事实和可执行恢复动作是两个问题。即便 `read_tool_result` 当前不可用，也应保留真实的范围说明。

### 2.4 `_result_context()` 和可用工具上下文

给现有提示链传入真实的 `nested_tool_names`；不要把直接工具和嵌套工具合并成一个集合后丢失调用方式。

更新这些入口：

- `task_result_context()`；
- kernel context 的 `prepare_turn()`、`provider_delivery()`、`deliver_results()` 中调用它的地方；
- `kernel_control.py` 调用 `_result_context()` 的地方；
- 其余用 `rg` 找到的调用点和测试 helper。

数据直接来自当前冻结的 snapshot，不创建另一份 capability registry。

新增一个纯格式化 helper，例如 `_available_followup_call()`：

1. 工具在 `tool_names`：生成直接调用。
2. 工具只在 `nested_tool_names`，且 `codemode` 是直接工具：通过现有 CodeMode 入口示例化该调用。
3. 工具没有暴露：不给出该调用。
4. 参数必须符合现有冻结 schema；不以提示绕过授权和输入限制。

同一个动作只生成一种可用调用形式。未知工具集不能按“默认所有工具可用”来输出示例。

### 2.5 嵌套子回执的正文

检查并调整：

- `_browser_read_context()`；
- `_browser_followup_context()`；
- `_child_failure_context()`；
- CodeMode 失败详情及已有子回执读取提示。

嵌套显示复用同一套恢复提示函数，不复制工具规则。

父回执因显示预算再次裁剪 child 正文时，`display_omitted` 由实际切片计算。这是派生的显示事实，不是 child 的原始 `truncated`。若指向 managed output，续读从该输出的实际坐标开始；若指向原回执文本，才使用原回执文本偏移。保留当前对这两类坐标的区分。

### 2.6 历史回执和投影

不重写已交付的历史消息，不通过修改保存的正文修复旧提示，也不破坏已有摘要验证。新结果构造和后续生成的恢复说明使用新规则。

旧回执中的范围事实仍通过已有恢复链读取。历史正文如果含旧提示，属于已保存的历史显示内容；它不能成为当前恢复动作的判断来源。不要通过对所有文件正文做字符串替换来“清理”它。

本方案不承诺把已交付历史文本中的旧英文句子抹掉；它保证新产生的正文不再注入矛盾指令，并保证恢复不丢原始事实。

## 3. `read_file` 与 `read_data` 完整性

涉及文件：`codey/toolchain/read_tools.py`、`codey/research/calculation.py`。

### 3.1 `_format_read_file_page()`

保留流式读取、取消检查、字节版本摘要、返回字段和函数现有主要签名。

具体分支：

1. 空文件：保留成功空内容，`start_line=1, end_line=0, total_lines=0, next_offset=0, text_complete=True`。
2. 单次完整读到全文：保持 `truncated=False`，不添加截断警告。
3. 中间页：保持 `truncated=True`，保留精确下一次 `read_file` 调用，包含 path、offset、limit。
4. 后半段末页：保持 `truncated=True`，追加明确的 `end of file`，不再给下一页。
5. 超长行预览：保留 `text_complete=False, truncated=True`。若已到文件最后一行，同时说明“到达文件末尾”和“这一行只是预览”。这两件事并不矛盾。
6. 超出文件末尾：继续返回范围错误，不建议回执恢复未读取的文件内容。

末页正文示例：

```text
[read_file page: lines 220-396 of 396; end of file]
```

超长末行正文示例：

```text
[read_file page: line 396 of 396 exceeds the text budget;
 preview only, not a complete old_string; end of file]
```

`_read_page_outcome()` 仍是构造 canonical 页的唯一地方，不额外保存 `eof=True` 或另一份 next_call。EOF 由现有行范围和 `next_offset` 派生。

### 3.2 新增完整文件校验 helper

放在 `calculation.py`，例如：

```python
def _require_complete_file_receipt(result: ToolResult) -> JsonObject:
    ...
```

校验内容：

- 成功且调用名为 `read_file`；
- canonical value 符合现有 `read_file_output_schema()`；
- `text_complete is True`；
- `result.truncated is False`；
- `start_line == 1`；
- `end_line == total_lines`；
- `next_offset == 0`。

复用已有 schema validator，避免 Python 的 `True == 1` 混入坐标校验。空文件的合法 `1/0/0` 范围应通过这一层；空内容能否解析为 CSV/JSON 由原来的解析器决定。

### 3.3 `_read_file_reference()` 和 `read_data()`

两条入口都调用上述 helper：

- path 入口仍选择当前语义下最近一次对应路径的回执；不要擅自跳过新半页、回退到旧完整版本。
- result_ref 入口补齐目前缺失的显式范围检查。

如果 path helper 和 `read_data` 会先后验证同一回执，可以把选择函数改成返回 `(ref, validated_data)`，避免相同 schema 重复解析；不得因此改变选择规则。

不把“累计读完几页”视为“一张完整文件回执”。本次不新增跨页拼接和版本拼接运行时。

## 4. `knowledge_read`：实际分页与版本绑定

涉及文件：

- `codey/research/tools.py`
- `codey/research/knowledge_materials.py`
- `codey/research/tool_output.py`
- `codey/operations/research/execution.py`
- `codey/research/tool_declarations.py`

### 4.1 一个分页函数负责普通内容切片

在 `knowledge_materials.py` 增加纯 helper，例如 `_content_window(text, offset, limit)`。

规则：

- `type(offset) is int`，不接受 bool，且 offset 非负。
- `type(limit) is int`，范围 `500..12000`。
- 空文本仅接受 offset=0，返回空的终止页。
- 非空文本要求 offset 小于文本长度；超界报错并显示实际长度。
- `end = min(len(text), offset + limit)`。
- `next_offset = end if end < len(text) else None`。
- `content_complete = offset == 0 and end == len(text)`。

返回窗口坐标和当前切片。不要把整份知识对象的 JSON 序列化后再切断，否则会得到不可解析 JSON。

### 4.2 `ResearchTools.knowledge_read()` 的非 source/evidence 分支

删除固定的：

```python
payload["content_complete"] = len(content) <= 12000
payload["content"] = content[:12000]
```

改为调用窗口 helper，并返回完整有效的 JSON envelope：

```json
{
  "id": "实际对象 ID",
  "revision": 7,
  "content": "当前内容页",
  "offset": 6000,
  "end_offset": 12000,
  "limit": 6000,
  "available_length": 15000,
  "next_offset": 12000,
  "content_complete": false
}
```

保留原有对象类型、provenance、references 等字段。下一次参数由这个 envelope 派生，不另存一份 next_call。

续读必须使用已解析的真实对象 ID 和确切 revision，不沿用可能重新绑定的短别名，也不省略 revision 重新读取 latest。

`whole_answer` 继续只影响引用指导；不作为绕过分页预算的开关。

### 4.3 `finding_ref` 绑定

`claim / inference / observation` 仍仅在本次回执覆盖整个对象内容时调用 `bind_version()`。

短对象以非零 offset 读到最后，也不满足完整绑定条件。`next_offset is None` 只代表没有后续页，不代表当前页包含全文。

### 4.4 `read_material()` 的 source 分支

保留 `describe_captured_window(..., coherent=True)` 以及已有 passage 身份生成。

- `next_offset` 使用实际返回 passages 的末端，而不是盲用 `offset + limit`。
- 只有实际末端仍小于文本长度时才返回下一页。
- 到达存储文本末端返回 `None`。
- 不把“存储文本读完”误写成“原始网页 / PDF 全文完整”；原采集截断仍以 source observation 为准。
- 当前页完整性根据真实 passages 覆盖范围判断，不用 `next_offset` 单独推断。

### 4.5 `read_material()` 的 evidence 分支

保留 evidence 为已保存、绑定 source revision 的完整摘录这一语义，不把它变成一个隐式的“读取后面的整个 source”接口。

- 默认读回原来的整段证据，仍受现有 12000 字符上限约束。
- `next_offset` 固定为 `None`，无论证据在 source 的中间还是末尾。
- 非零 offset 不再被静默忽略：明确拒绝，并提供已知 source ID / revision 及其源文本位置，供调用者读取 source。
- 说明 evidence 是原子摘录，source 和普通对象才是可续页内容；在工具声明中写清这一点。
- 不改变原有 `source_version`、`evidence_version` 和 passage_ref 绑定方式。

这里不新增 evidence 的第二套片段身份，也不因修分页而改变证据粒度。

### 4.6 legacy document note 分支

避免结构化对象修好了、legacy note 仍忽略相同分页参数。

1. 使用已有 `store.documents.get(note_id, revision)` 读取 `(note, path, resolved_revision)`。
2. 不新建另一套 note repository，也不在 documents 外部复制版本查询 SQL。
3. 用实际 `resolved_revision` 读取 note references。
4. 保留现有访问控制、preface、claim 状态和 adoption 规则。
5. 将 `note.to_markdown()` 作为该分支的可分页文本对象，使用同一个 `_content_window()`。
6. 用有效 JSON envelope 返回 note_id、revision、当前内容、范围和 references。不要通过拼接不完整 JSON 实现分页。
7. 下一页固定 note ID 和 resolved revision，避免页间读到不同版本。

这是明确的返回显示调整；同步更新 legacy note 的现有消费测试和工具描述。

### 4.7 把知识页事实送进 canonical

当前 `ResearchToolOutput` 的知识正文只有字符串，不能让提示层靠解析显示字符串判断分页。

为其增加一个可选的 `knowledge_page: JsonObject` 字段，默认空；仅知识读取成功时填写。这个字段承载上述 envelope，而不是另存重复的完整性状态。

成功结果的 `model_text` 从同一个 envelope 序列化生成。`_read_research_knowledge()` 将同一 envelope 放进 `canonical["knowledge_page"]`，保留已有 knowledge_read_versions、read_passages 和 research_observation。

不要顺便改成 `canonical["value"]`，否则会改变现有知识工具在 CodeMode 中的成功返回 schema。原有文本成功契约继续保留。

`knowledge_page` 是此页事实；原知识全文仍只在 knowledge store 中。显示字符串不再反向解析为机器事实。

## 5. `read_tool_result`：原始回执续读

涉及文件：`codey/operations/tool_result_reader.py`、`codey/operations/kernel_prompt.py`。

### 5.1 `read_tool_result()` 的分页逻辑

保留 identity 解析、scope 授权、tool_task / handoff、执行回执验证和禁止因读取失败重跑的边界。

在现有读取结果后，提取一个纯 helper 负责字符窗口，例如 `_receipt_window(text, offset, limit, query)`。

无 query：

```python
end = min(len(text), offset + limit)
excerpt = text[offset:end]
next_offset = end if end < len(text) else None
```

有 query：

1. 从请求 offset 开始做 literal search，保持现有大小写敏感语义。
2. 没有匹配时，返回空窗口、`match_offset=None, next_offset=None`。
3. 有匹配时保留现有有界前文窗口；返回实际显示 offset。
4. 下一页搜索起点不得倒退，使用当前实际页末。
5. 只有从该位置还能找到 query 时，`next_offset` 才有值。
6. 这仍是“寻找下一段匹配窗口”，不是正则工具，也不是逐个列出所有重叠匹配的新搜索运行时。

### 5.2 `canonical.receipt_page`

构造一次页面数据 `data`，至少包括：

- 原始 result_ref；
- view；
- 实际 offset；
- limit；
- query（仅请求时包含）；
- next_offset；
- 当前 text 和现有完整性 / 留存字段。

`model_text` 由 `data` 序列化；canonical 中的 receipt_page 使用同一份页面事实。避免再维护一个只含三个字段、会丢 query/limit 的手工摘要。

### 5.3 新增 `receipt_continuation_args(page)`

这个纯函数是回执下一页参数的唯一组装处，提示层调用它。

```python
if page["next_offset"] is None:
    return None

args = {
    "result_ref": page["result_ref"],
    "view": page["view"],
    "offset": page["next_offset"],
    "limit": page["limit"],
}
if "query" in page:
    args["query"] = page["query"]
return args
```

不能把 `if next_offset` 用作通用终止判断；本接口明确使用 `None`。

`_receipt_result_context()` 删除自己重组 `{result_ref, view, offset}` 的旧分支，使用该 helper。永远继续原始回执，不把当前 `read_tool_result` 的 effect ID 当下一页目标。

### 5.4 `_value_output()` 的错误退路

canonical value 确实不存在时继续明确报错。错误附带的输出读取参数应保留 query 和 limit，不让异常分支再次丢失用户搜索条件。

显示正文不能被当成不存在的结构化 value 返回。

## 6. `grep`：范围、越界和命中居中的长行

涉及文件：

- `codey/toolchain/rg_search.py`
- `codey/toolchain/search_page.py`
- `codey/toolchain/result_schema.py`
- `codey/operations/kernel_prompt.py`

### 6.1 `SearchPage` 和 `search_output_schema()`

不新增查询参数。为当前结果页增加一个可选的 `observed_match_count` 字段，作为本次执行已观察到的匹配行数。

- 不命名为 `total_matches`，因为搜索可能提前停止或覆盖受限。
- 字段由 `search()` 的 `seen` 一次产生。
- schema 同步声明此字段；为兼容旧回执，不把它加入历史页面必须具有的字段集合。
- `SearchPage` 的类型使用 `NotRequired[int]` 或等价的明确可选表示。
- 机器结果仍由 `validate_output()` 校验，不绕过严格 schema。

若最终实现决定仅在正文使用这个计数，也应把计数作为 `_render()` 的显式输入；不要把它藏进 audit 再解析。本方案选择 canonical 可选字段，便于最终上下文测试和恢复。

### 6.2 `search()`

保留当前选文件、分批 rg、offset、limit、取消和预算控制。

- `has_more=True` 时，计数是已观察下界，不是全量总数。
- 页为空且 `has_more=False` 时，可以描述已准入文件范围内观察到的全部匹配数量。
- 若 discovery、文件大小、扫描字节、不可读文件等限制存在，仍不能声称整个工作区只有这些匹配。
- 不为得到“精确总数”再执行一次完整搜索。

保留现有 `truncated` / `scan_complete` 语义，不借此重新定义 `SearchEvidence.complete`。

### 6.3 `page_footer()`

删除 `try offset=1`。

正常末页：

```text
[grep page: results 41-57; end of matches in admitted files]
```

越界且扫描完整：

```text
[grep page: offset 80 is beyond 57 matches in admitted files; no next page]
```

越界但覆盖受限：

```text
[grep page: no matches at offset 80; observed 57 matches in admitted files;
 scan coverage is incomplete; narrow path/include to inspect omitted files]
```

将计数和覆盖状态作为显式参数传给 footer，不让 footer 通过解析上一段正文推测。

`next_call_hint()` 继续保留 query、path、literal、case_sensitive、include、exclude、before_context、after_context 和 limit。

### 6.4 `_render()` 和长行 helper

新增纯函数，例如 `_match_preview(text, column_byte, width=240)`。

实现步骤：

1. 输入必须是未经 `strip()` 改变坐标的原始匹配行。
2. `column_byte - 1` 是 UTF-8 字节位置，先转换为字符位置。
3. 使用该字符位置确定显示窗口，使匹配开始位置位于窗口内部，而不是永远只展示行首。
4. 左侧或右侧被省略时分别添加省略标记；预算包括这些标记。
5. 展示清理在坐标确定之后进行，不移动命中依据。
6. 不修改 canonical 的完整 `matches[].text` 和 `column_byte`。
7. context 行没有匹配位置，使用现有有界显示，不伪造 column。

UTF-8 转换可以先编码，再对真实边界之前的字节解码求字符数。正常 `rg` 产生的列在字符边界；异常边界按现有 malformed-output 处理，不引入全局容错。

`_render()` 不再提前把 match 压成一个丢失 column 的 tuple。让匹配行在调用 preview helper 时仍能取得原始 column。

长行显示被裁剪并不意味着漏掉了新的搜索结果页。保留两条不同说明：

- 更多匹配：调用下一页 grep。
- 当前命中行的完整文本：读取当前回执的 `view="value"`；若该工具当前不可用，使用已观察到的路径和行号定点读。

不为了长行预览改变现有搜索完整性布尔值。

## 7. `run` / `shell`：显示裁剪与不可恢复丢失

涉及文件：

- `codey/runtime/core/execution_receipt.py`
- `codey/operations/tool_result_reader.py`
- `codey/operations/kernel_prompt.py`
- `codey/storage/managed_outputs.py`

### 7.1 `resolve_result_output()` 是唯一内容解析器

继续沿已有优先顺序解析：

1. 有 execution output reference：验证当前授权 origin，再读 ExecutionOutputStore。
2. 否则已有 managed_output：读该留存对象并验证摘要。
3. 否则只有原回执正文。

不要因上一级引用存在但失效，就静默回退到看起来完整的显示片段。读取失败必须保留失败事实。

`ResolvedOutput` 继续是派生读视图，不再把它复制成新的持久化恢复状态。

### 7.2 统一采集与留存完整性的推导

完整性优先使用 `ExecutionReceipt`，旧式无执行回执结果才使用当前支持的 audit / managed-output 事实。

可以在现有模块提取一个纯计算 helper，供 resolver 和提示代码共同使用。它只接收已经存在的过程状态、capture 截断和 storage 截断事实，不读文件、不执行工具、不存储第三份状态。

关键真值表：

| 执行已结束 | 采集丢失 | 留存完整 | 能否恢复原始完整输出 |
| --- | --- | --- | --- |
| 是 | 否 | 是 | 是 |
| 是 | 是 | 是 | 否；存下来的只是已捕获片段 |
| 是 | 否 | 否 | 否 |
| 否 | 否 | 当前有留存 | 只能读当前片段，不能声称最终输出完整 |
| 未知 | 未知 | 未证实 | 不承诺完整恢复 |

现有 `ExecutionReceipt.full_output_recoverable` 继续由原始执行事实派生；不要在 kernel_prompt 又手写另一个含义不同的判断式。存储实际读取发现不完整时，以 resolver 验证后的结果为准。

### 7.3 命令提示

只发生显示裁剪且完整输出可恢复：

```text
Displayed output is clipped. Read the retained output with
read_tool_result({"result_ref":"...","view":"output","offset":0,"limit":4000}).
Do not repeat execution to recover output.
```

采集或存储已丢失：

```text
The original output is incomplete. The receipt contains only retained fragments;
discarded bytes cannot be recovered from it. Read retained fragments if useful.
Do not repeat a command solely to recover its previous output.
```

失败命令原有“修复后重新执行检查”的提示保留。这里必须区分“为取旧输出重跑”和“修复后做新验证”，不能把禁止重复执行写成永远不允许重测。

### 7.4 `run_command_with_managed_output()`

保留 `if store is None or not projected.truncated` 这一既有归档触发条件；本方案不通过翻转 truncated 让归档路径失效。

`managed_output_failed`、capture_truncated、stored_truncated 都要进入最终文字的实际分支。footer 不再与另一层的 rerun 指令冲突。

shell 的异步 / tool_task 输出继续通过已有 `_execution_origin()`、`owned_tool_task()` 解析，不增加另一个 shell 恢复器。

## 8. `browser_snapshot`：文本与控件分别提示

涉及文件：`codey/automation/browser_runtime.py`、`codey/operations/kernel_prompt.py`。

### 8.1 `BrowserRuntime._observe()`

保留 `collect_snapshot()`、delta 和完整 observation 保存。

移除现有只由 `elements_truncated` 驱动、硬编码动作名称的 hint。普通正文继续是 `snapshot_display(value)` 的 JSON；具体可执行动作交给同一个模型提示函数处理。

runtime 的 canonical observation 已经有 `text_truncated`、`elements_truncated`、scope、max_text 等事实，不再新增一套截断原因字段。

### 8.2 snapshot 提示函数

分别判断：

- elements 截断：引导对实际已观察到的容器 scope_ref 或唯一 selector 缩小观察范围。
- text 截断：明确正文未完整显示；有可用 `browser_read` 时提示通过已有读取通道分页读取。
- 两者都截断：两种事实各说明一次。
- 两者均完整：不加截断告警。

`browser_read` 目前支持 offset / limit，不能给它编造 selector 或 scope_ref 参数。scope_ref 属于 `browser_snapshot`。

如果 browser_read 没暴露，就只提供当前可用的 scoped snapshot / max_text 方式；不能盲目让 max_text 超过 schema 上限。

子 frame 的截断也必须检查实际 frame 观察；不能只看顶层标志。使用 frame 的实际身份和现有观察接口，不把子 frame 的控制引用当顶层引用。

不因为补提示修改 `ToolResult.truncated`、`complete` 或浏览器完成判定。它们由原来的观察逻辑决定。

## 9. `list_dir` / `find_references`

涉及函数：`read_tools.list_directory()`、`read_tools.find_references()`。

### 9.1 `list_directory()`

保留当前两层概览和扫描预算。

- subdir 达到上限时，在该 subdir 的现有停止说明后明确可用更具体 path 列出它。
- 根目录达到上限时，说明根目录概览受限，不虚构 offset。
- unreadable 和达到数量上限分开说明：缩小 path 不保证解决权限或元数据读取失败。
- 如果当前根本没有可进一步缩小的可见路径，诚实说明该接口不能保证枚举遗漏的根级项目，不自动安排一个新 shell 扫描。

`incomplete or limited` 的 truncated 不变。不要把忽略的条目保存到另一个隐藏列表并假装回执可以恢复。

### 9.2 `find_references()`

保留扫描器已有 output 和 report，使用 `render_scan_coverage()` 的事实，不解析字符串猜原因。

结果或覆盖受限时说明这是引用线索，并要求缩小 path 后再查。不要声称静态引用提示等于完整语义调用图。

不新增 offset，不重复用 grep 执行一次以便补全它，也不为遗漏引用生成 `read_tool_result` 指令。

## 10. `source_search`：让覆盖说明穿过完整 observation 链

涉及文件：

- `codey/research/source_search.py`
- `codey/research/source_gateway.py`
- `codey/research/receipt_types.py`
- `codey/research/ledger.py`
- `codey/research/ledger_receipts.py`
- `codey/research/source_locators.py`
- `codey/research/tools.py`
- `codey/operations/research/execution.py`

### 10.1 `_candidate_offsets()`

当前每 token 最多 64 个候选，这是覆盖边界，不只是输出 limit。

让函数同时返回候选 offsets 和 `candidate_limited`。达到上限后额外做一次 find，确认真的还有候选才标受限；不因恰好有 64 个就声称遗漏。

不再启动第二遍全文扫描统计总数。

### 10.2 `search_text()` / `search_pages()`

使用一个明确的内部结果类型，例如：

```python
@dataclass(frozen=True)
class SourceSearchResult:
    hits: tuple[SourceSearchHit, ...]
    has_more: bool
    candidate_limited: bool
```

直接更新现有函数返回类型及所有调用点；不保留只为兼容内部旧签名而存在的无用 wrapper。

共用 `_rank_hits()`。内部请求 `limit + 1` 个去重后的候选，保留前 limit 项，额外项判断 `has_more`。

注意：不能通过公开 `bounded_limit(limit + 1)` 实现，因为公开上限 12 会把 13 再裁回 12。内部排名与公开输入上限是两个边界。

`has_more` 只表示已生成候选中仍有额外独立 locator；`candidate_limited` 说明候选生成本身受限。两者都不能冒充全文命中总数。

### 10.3 `ResearchSourceGateway.search_inside()`

本次选中的 snapshot 是唯一搜索内容范围：

- HTML 使用该 snapshot 的捕获文本。
- PDF 补扫成功时，使用实际扫描生成的 snapshot。
- PDF 补扫失败时，只报告原来已有的实际 page map；不能声称扫描过配置允许的全部页面。

把搜索结果的三个新事实记录进 observation 的 coverage：

```json
{"limit":6,"has_more":true,"candidate_limited":false}
```

不要在 coverage 里再次保存 page_count、pages_read、source_truncated；这些已存在于同一个 source_snapshot，渲染时派生。

`SearchedInside` 可以改为携带这份已生成的 observation，替换其独立的 hits 显示副本。`record_source_search()` 返回刚记录的 observation，调用者复用同一对象，不重新拼一次。

### 10.4 `_pdf_search_snapshot()`

不改当前采集预算、不额外打开未扫描页。

以实际成功提取的 page_texts 为扫描页集合，保留失败 / 跳过的缺口。不要把 `min(page_count, MAX_PAGES)` 直接当作成功扫描终点。

原有快照已足够承载页信息；不新增另一张 PDF scan 状态表。

### 10.5 `SourceSearchObservation` 和 `source_search_observation()`

新增一个可选 coverage 字段及明确 TypedDict。

解码器继续严格：

- 允许旧字段集合；
- 或旧字段集合加 coverage；
- 拒绝其他未知字段；
- coverage 存在时严格验证 limit 和 exact bool；
- coverage 缺失代表旧回执覆盖状态未知，不填假 `has_more=False`。

这只是同一 observation codec 的历史格式支持，不是第二套搜索执行逻辑。

### 10.6 `render_results()` / `build_hit_mapping()`

扩展现有共同渲染入口，让以下两处使用同一个 coverage 渲染函数：

- `ResearchTools.source_search()` 的直接结果；
- `source_locators.build_hit_mapping()` 生成 hN 后的最终结果。

`operations/research/execution.py` 继续走现有 observation → `_build_hit_mapping()`，不再额外 append 一份容易重复或被覆盖的提示。

提示必须说明：

- 返回数量和配置 limit；
- 是否还有已发现的独立 locator；
- 是否触及 token 候选上限；
- PDF 的实际搜索页集合 / 范围和已知总页数；
- 非连续页集合不能写成完整的 `1-N`；
- snapshot 原文本身截断时，检索范围也不等于原始全文；
- 无命中时仍保留覆盖说明。

后续动作是更具体查询、打开已有 locator，或在现有工具允许的情况下读取指定页。没有 source_search 分页游标，也不承诺增大 limit 可以找回候选生成时已遗漏的所有命中。

## 11. CodeMode：可读的大结果和原回执修复

涉及文件：

- `codey/codemode/output.py`
- `codey/codemode/kernel.py`
- `codey/codemode/replay.py`
- `codey/operations/tool_result_reader.py`
- `codey/storage/managed_outputs.py`（复用，不另建存储器）

### 11.1 `externalize_value()` 的唯一输出表示

在现有 `output.py` 直接修改该函数的返回契约；必要时重命名为 `project_value()`，并删除旧函数及 import，不能两份实现并存。

它返回供 canonical 使用的互斥字段之一：

内联：

```json
{"value": "原始 JSON 返回值"}
```

外置：

```json
{
  "value_artifact": {
    "session_id":"...",
    "run_id":"...",
    "handle":"artifact_...",
    "sha256":"...",
    "schema_version":1
  }
}
```

两者不能同时存在。不要再同时保存 `value={artifact:ref}` 和 `value_artifact=ref` 两份权威引用。

不要通过检查任意业务返回值是否长得像 `{artifact: ...}` 来自动解引用；普通脚本有权返回这种业务对象。只有执行器写入的顶层 canonical 存储字段表示外置值。

函数仍使用现有紧凑 JSON 编码和 `write_json_artifact()`，不截断 JSON 字节，也不另建文件协议。

### 11.2 `CodeModeKernelExecutor.__init__()`

默认 `max_inline_bytes=32_000`，worker `max_output_bytes=200_000` 保持原值。

为了支持调用方自定义很小的硬上限，默认阈值可以用 `None` 表示“由当前 limits 派生”，实际取 `min(32_000, max_output_bytes)`；显式指定阈值继续严格检查 exact int、正数和不超过硬上限。

默认生产配置下外置分支可达。自定义将阈值设为硬上限是明确配置，不把整个系统称为没有外置能力。

不修改 guest 和 host 的双重硬上限校验；它们是同一执行协议的两个边界检查，不是两套执行器。

### 11.3 `CodeModeKernelExecutor.execute()`

worker 成功后：

1. 调用同一个 value projection 函数。
2. 内联时保持当前成功显示和 `canonical.value`。
3. 外置时只保存 `canonical.value_artifact`，生成有界的已保存说明；真正的读取调用由拿得到 parent result_ref 的提示层生成。
4. 不把大 value 再完整复制进 model_text 或另一份 audit。
5. 不因为外置就假装内容丢失；沿用当前成功 CodeMode 的机器状态，用明确的外置字段驱动读取。

存储失败需要明确的结构化错误。新增一个很小的输出边界异常，例如 `CodeModeOutputStorageError`，区分没有 store、写入失败与返回值编码失败。

- 存储失败：`audit.error_code="output_storage"`。
- 编码失败：保留已有 `result_encoding`。
- 子回执仍归原来的 parent，不撤销已发生的副作用。

不要用一个宽泛 catch 把这些失败都变成没有 error_code 的字符串。

### 11.4 `_value_output()` 接入现有 artifact reader

给它增加授权 origin 的 `ReceiptReaderContext` 参数，并从 `read_tool_result()` 传入 `_receipt_origin()` 已解析的原始 origin。

统一处理：

- 仅 value 存在：保持原来的 `{"value": ...}` 紧凑序列化。
- 仅 value_artifact 存在：验证引用完整、schema 受支持，并严格匹配授权 origin 的 session_id / run_id；随后调用现有 `read_json_artifact()`。
- 两者都存在：拒绝含糊表示。
- 两者都不存在：保持 canonical value 不存在的明确错误。

artifact 读取后也统一序列化为 `{"value": 原始值}`，再交给相同的字符分页 / literal query helper。内联和外置使用同一套页面算法。

注意 `read_json_artifact()` 验证的是“引用自称的作用域与文件元数据一致”，调用方仍必须额外检查该作用域是否就是当前授权 origin。只把 ref 传进去并不等于完成授权。

不接受用户直接把 artifact handle 当 result_ref 绕过回执授权。查询入口仍是原始 parent result_ref。

### 11.5 统一可用视图判断

在现有 reader 模块提取 `available_result_views(result)`，供 reader 和 kernel prompt 共用。

有内联 value 或外置 value_artifact 都暴露 value 视图；output 仍代表原回执显示 / 命令输出。

外置值的 output 是摘要，不能把 `full_output_recoverable` 的值拿来暗示 output 视图包含原始 JSON。完整 JSON 的恢复动作明确指定 `view="value"`。

### 11.6 沿用 `result_receipt_fields()` / `KernelEffectSink.settle()` 的原结算

不为外置值新增第二次 artifact 注册或结算。

现有 `result_receipt_fields()` 已经无损保存整个 canonical；`value_artifact` 因而会随原 ToolResult 一起保存并验证摘要。JSON artifact 的字节先由已有 writer 原子保存并验证，随后同一个 parent receipt 保存引用。

读取授权仍由原始 receipt 的归属和 `_receipt_origin()` 决定，不能把“磁盘上存在这个 handle”当成授权。

相同内容在同一个 run 下会得到同一个 content-addressed handle，重复写入相同字节是幂等的。多个原始回执可以指向该内容，无需再给同一个 handle 创建多个 owner。

这点需要明确：当前 durable `_write_artifacts()` 是普通 INSERT，没有通用的重复 handle 合并。机械地对每个 CodeMode 大结果调用 `json_artifact_record()` 并重复注册，会制造主键冲突。因此本方案不添加这条冗余索引路径，也不扩大数据库 schema 来补救一个不必要的注册。

验证原结算恢复即可；生产上的 `KernelEffectSink.settle()`、`result_receipt_fields()` 若已无损保留新字段，不应为了“完整落地”而产生没有必要的修改。

### 11.7 repair 错误集合只有一份

在 `replay.py` 定义一个实际被使用的常量，例如：

```python
RETURN_REPAIR_FAILURES = frozenset({
    "missing_return",
    "result_encoding",
    "shape_mismatch",
    "output_limit",
    "output_storage",
})
```

`kernel.py` 的修复 metadata 生成处和 `resolve_repair_context()` 的授权处都引用它。

`shape_mismatch` 额外要求只读且已结算成功的原有保护必须保留；共享集合不等于抹掉特殊保护。

`output_storage` 纳入，是因为启用外置后写文件失败同样可能发生在子调用已经写入之后。否则会留下新死路。

### 11.8 `build_execute_turn_executor()` / `run_parent()`

修复资格来自原始失败类别和已结算子回执，继续生成原 repair_metadata。

保留现有：

- session / run 绑定；
- 原工具 catalog 摘要；
- 子调用位置和参数摘要；
- 执行 trace、delivery 顺序；
- state 写入限制；
- token 使用约束；
- 子回执完整性检查。

修复脚本仍必须消费原来的子调用顺序。它不是“完全删掉所有子调用，只写一个 return”的自由重跑。

对模型实际无法编辑 JavaScript、也没有暴露 repair_of 的固定模板，不能输出不可用的修复指令。检查当前冻结输入契约；这类失败继续报告已结算事实及可用的原回执读取方式。不要临时生成第二套模板执行器。

### 11.9 `_simplified_failure()`

同步识别新的输出错误类别，避免精确的 output_limit / output_storage 又被简化成无信息的普通失败。

普通 CodeMode 输入可以收到有效 repair token；固定模板继续明确报告模板 / 输出处理失败及已发生的副作用，不向模型开放未授权的脚本修改。

### 11.10 硬上限以上的验收情景

原程序：执行一次写入，再返回超过 200000 字节的值。

期望：

- worker 返回 output_limit；
- parent 明确列出原已结算 child；
- 可编辑 CodeMode 得到 repair_of；
- 修复脚本复用相同子调用、只改变后续值处理和 return；
- 实际写入计数一直为 1；
- 换参数、加子调用、跳过原序列或跨 run 使用 token 被拒绝。

## 12. 单一事实来源的代码审查要求

实施完成时逐条核对：

1. **执行入口唯一**：所有恢复读取不调用任何 execute / run / worker 启动路径。
2. **内容解析唯一**：命令内容由现有 resolver 选择；JSON artifact 由现有 store 验证；知识全文由现有 repository/documents 读取。
3. **提示规则唯一**：通用恢复动作在一个纯提示入口生成；嵌套显示调用它，不抄规则。
4. **参数组装唯一**：原回执的续读参数由 `receipt_continuation_args()` 生成；不存在另一份手写 query / limit 逻辑。
5. **repair 资格唯一**：metadata 生成和 token 验证使用同一个常量。
6. **结构化存储互斥**：CodeMode 的 value 和 value_artifact 不同时存在。
7. **范围事实不重复保存**：EOF、PDF 页范围等能从原始结构推出的字段不再额外持久化。
8. **显示不授信**：完整性与授权不从 model_text、presentation 或错误字符串推断。
9. **旧路径删除**：旧常量、旧 externalize 返回包装、被替代的续读拼接、runtime 硬编码 browser hint 一并移除。
10. **可达性验证**：默认配置至少有一条真实执行测试进入外置分支，也有测试证明硬上限失败进入原 repair 分支。

允许纯派生视图和格式兼容 codec；不允许它们执行新动作、维护长期状态或变成第二个控制器。

## 13. 验收矩阵

测试应使用真实 ToolOutcome → `result_from_outcome()` → ToolResult → 最终 result context。不能只用手工 ToolResult 字符串绕过触发问题的构造层。

最终交付还要分别覆盖 text protocol、native tool result，以及实际 provider 请求中使用的正文。

| 编号 | 情景 | 必须断言 |
| --- | --- | --- |
| F01 | read_file 完整文件 | 内容完整，无通用截断 / 回执重读指令 |
| F02 | 首个半页 | 保持 truncated=True，有准确下一次 read_file |
| F03 | 220–396 / 396 末页 | 保持 truncated=True，含 EOF，无虚构下一页 |
| F04 | 超长最后一行 | EOF 和 preview-only 同时存在，text_complete=False |
| F05 | 空文件 | 读取成功，页范围合法 |
| F06 | read_data 直接 result_ref 首个半页 | 拒绝，即使测试构造 truncated=False 也拒绝 |
| F07 | read_data 直接 result_ref 末尾半页 | 拒绝，即使 next_offset=0 也拒绝 |
| F08 | read_data path / result_ref 完整页 | 解析结果一致；版本未变 |
| K01 | 普通知识 >12000 字符 | offset/limit 生效，所有页拼接等于原内容 |
| K02 | 普通知识末页 | next_offset=None，不生成越界调用 |
| K03 | 首页 / 非零 offset 短对象 | 只对真正完整的一张回执绑定 finding_ref |
| K04 | 页间对象 latest 更新 | 续读仍绑定原 revision |
| K05 | source 最后一页 | 使用 passage 实际末端，next_offset=None |
| K06 | evidence 位于 source 中部 | 原子摘录结束，不导航到其他 source 内容 |
| K07 | evidence 非零 offset | 明确拒绝并指出可读 source，不静默忽略 |
| K08 | legacy note 分页 | 相同窗口规则，固定 note revision，references 不漂移 |
| K09 | 空文本、负 offset、bool、越界 limit | 明确验证；空文本 offset=0 可终止 |
| R01 | 回执普通分页 | 下一次保留原 result_ref / view / limit |
| R02 | 回执 query 分页 | query 完整保留，不变成无过滤读取 |
| R03 | 后面没有匹配但仍有文本 | next_offset=None |
| R04 | query 无匹配 | 空窗口，终止，无循环 |
| R05 | 读取工具自己的回执 | 提示继续原目标，不形成 receipt-of-receipt 链 |
| R06 | 缺 canonical value | 错误明确，不能用显示正文冒充 value |
| G01 | grep 正常第一页 / 中间页 / 末页 | 参数完整，结束语义正确 |
| G02 | offset 越界且覆盖完整 | 已观察数准确，不含 try offset=1 |
| G03 | offset 越界但覆盖受限 | 不声称整个工作区精确总数 |
| G04 | 压缩 JS 行尾命中 | 显示窗口中包含命中开始处 |
| G05 | 中文前缀加长行 | 按字节列正确定位，不以字符下标误用 column_byte |
| G06 | 长行完整恢复 | canonical 原行未变，value 视图可以找到预览外文本 |
| E01 | 命令仅显示裁剪 | 指向原 output 视图，不建议为取输出重跑 |
| E02 | capture_truncated=True | 说明原始内容丢失，不声称全文可恢复 |
| E03 | stored_truncated / managed-output 失败 | 不显示虚假的完整输出留存 |
| E04 | 执行未结束 | 只能读取当前输出，不称为完整最终结果 |
| E05 | 命令失败后修复检查 | 允许修复后的新验证，同时禁止为取旧输出盲目重跑 |
| B01 | 仅 text_truncated | 存在正文不完整说明 |
| B02 | 仅 elements_truncated | 存在缩小控件观察范围说明 |
| B03 | 两者均截断 | 各解释一次，无互相覆盖 |
| B04 | browser_read 不可用 / 仅嵌套可用 | 不生成不可执行的直接调用 |
| B05 | delta、child frame、CodeMode 子 snapshot | 截断事实没有在嵌套显示中丢失 |
| D01 | list_dir 数量受限 | 缩小 path，无 offset / 假回执恢复 |
| D02 | list_dir unreadable | 不保证缩小 path 能修复读取权限 |
| D03 | find_references 扫描受限 | 保留覆盖说明，不改变机器完整性 |
| S01 | 来源命中达到 limit 且还有 locator | 最终 hN 正文含限制说明 |
| S02 | token 候选上限 | 明确候选受限，不声称全文搜索完整 |
| S03 | PDF 仅扫描前 N 页 | 最终正文显示真实页范围与已知总页数 |
| S04 | PDF 提取有缺口 / 补扫失败 | 不把未成功扫描页写成已扫描 |
| S05 | 来源无命中 | 覆盖说明仍保留 |
| S06 | 旧 observation / 新 observation | 同一 codec 可读；旧缺字段为未知，未知字段仍拒绝 |
| C01 | 默认配置 32000 以下 | 原内联 value 行为 |
| C02 | 默认配置 32000 以上且 <=200000 | 真正进入 artifact，不返回死句柄 |
| C03 | 外置值读回并拼页 | view=value 还原原 JSON，query 可搜索 |
| C04 | 普通值碰巧包含 artifact 字段 | 不自动解引用业务数据 |
| C05 | 丢文件 / 摘要错 / schema 错 | 明确拒绝，不重跑原程序 |
| C06 | 跨 session/run / 未授权 handoff | 拒绝；授权 origin 的 handoff 正常 |
| C07 | 写入后 output_limit | repair 成功后写入次数仍为 1 |
| C08 | 写入后 output_storage | 有可用的原回执修复或真实失败说明，无重写 |
| C09 | 改子参数 / 增加调用 / 跳序列 | repair 拒绝，副作用不增加 |
| C10 | durable restore 后读取 / repair | 使用原 artifact 和子回执，不重新执行 |
| C11 | 固定模板无 repair_of | 不输出模型无法使用的修复入口 |
| C12 | 相同大结果多次出现 | 幂等内容保存、多个原回执读回正常，无重复 ledger INSERT |

“不存在错误提示”的断言必须限制到框架生成的控制说明，不能因为用户文件本身包含某个英文句子就删掉正文。

## 14. 测试落点与执行命令

优先扩展已有测试文件：

- `tests/test_read_file_page_split.py`
- `tests/test_calculation_receipts.py`
- `tests/test_rg_search_contract.py`
- `tests/test_search_scan_split.py`
- `tests/test_execution_claims_and_output_recovery.py`
- `tests/test_bounded_capture_and_context.py`
- `tests/test_tool_result_read_scope_and_integrity.py`
- `tests/test_browser_snapshot_recovery.py`
- `tests/test_browser_partial_continuation.py`
- `tests/test_research_source_search_diversity.py`
- `tests/test_source_search_hit_targets_persist_in_canonical.py`
- `tests/test_codemode_repair_replay.py`
- `tests/test_codemode_output_storage_failure.py`
- `tests/test_tool_result_delivery.py`
- `tests/test_coldstart_native_local_receipts.py`

新增按行为组织的测试文件即可，例如：

- `tests/test_tool_output_recovery_guidance.py`：最终模型正文与工具可用性；
- `tests/test_knowledge_read_pagination.py`：普通对象 / source / evidence / legacy note；
- `tests/test_codemode_value_artifact_readback.py`：外置值、授权和持久化读回。

不要为每个私有 helper 写一份复制实现的测试。重点验证行为、数据完整性和副作用次数。

现有 `test_managed_output_wording_is_generic()` 明确锁定了错误的 `narrower offsets` 通用提示；应改成新规则测试，而不是保留旧常量只为让测试通过。

建议验证命令（实际实施后执行，本方案编写时没有运行这些测试）：

```powershell
python -m pytest tests/test_read_file_page_split.py tests/test_calculation_receipts.py tests/test_rg_search_contract.py tests/test_search_scan_split.py tests/test_execution_claims_and_output_recovery.py tests/test_bounded_capture_and_context.py tests/test_tool_result_read_scope_and_integrity.py

python -m pytest tests/test_browser_snapshot_recovery.py tests/test_browser_partial_continuation.py tests/test_research_source_search_diversity.py tests/test_source_search_hit_targets_persist_in_canonical.py tests/test_codemode_repair_replay.py tests/test_codemode_output_storage_failure.py tests/test_tool_result_delivery.py tests/test_coldstart_native_local_receipts.py

python -m pytest tests/test_tool_output_recovery_guidance.py tests/test_knowledge_read_pagination.py tests/test_codemode_value_artifact_readback.py

python -m ruff check codey tests
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

核心结果模型的修改还需执行仓库现行 CI 要求的其余相关检查。现有工作区包含多处未提交修改；实施报告应区分本次引入的问题与原有失败，不通过放宽校验或改快照掩盖差异。

纯提示和分页修改没有改变 Go 执行语义时，不应机械刷新 kernel parity fixtures。只有契约检查证明结果结构确实属于共享契约时，才按原生成器更新对应产物并解释差异。

如修改了 guest_runner.js 或 worker 协议，必须执行项目现有 JS 和运行时身份检查；本方案默认不需要改它们。

## 15. 完成定义

全部满足后才算完整落地：

1. 新生成的 read_file / grep / directory / references 结果没有错误的通用 offset / 回执恢复指令。
2. EOF 与全文完整性分离，数据解析和编辑依据不接受半页。
3. 知识与回执分页真正前进、固定版本、保留查询参数，并正确终止。
4. 命令不把丢失字节说成可恢复全文，也不为取旧输出重执行。
5. 浏览器、来源搜索和嵌套子回执的覆盖事实到达实际模型正文。
6. 默认 CodeMode 大结果外置分支可达，读回有授权和摘要校验。
7. 超过硬上限或存储失败后的修复复用原副作用，不产生第二次写入。
8. 所有恢复动作都使用既有入口、既有存储、既有持久化结算和既有重放机制。
9. 被替代实现、重复白名单、重复参数拼接和新增死代码均已清理。
10. 报告提供最终正文样例、测试结果、真实读取 / 写入计数，以及历史已交付文本不被改写这一兼容边界。

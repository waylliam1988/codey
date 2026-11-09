# 福州美食聊天异常：原始会话核验、确定性复现与修改建议

日期：2026-11-09（Asia/Taipei）。本次仅诊断与隔离实验，未修改生产代码、模型配置、用户会话或运行中的服务。

## 结论

用户贴出的三个现象有三个直接原因，不能归为一个“换模型后工具协议不兼容”：

| 现象 | 已核实的直接原因 | 性质 |
|---|---|---|
| `福州美食.md / Tool failed / ERROR: not a file` | Solar 在完成网页搜索后主动调用 `read_file`，读取没有创建过的本地文件 | 模型选错操作；文件检查正确拒绝 |
| 回答前出现九个空 `-` 和大量空白 | Chromium 复制折叠 Thinking 的富文本时，留下九个空 `<li>` 及其他空结构 | 富文本复制缺陷；与最终回答格式独立 |
| 原始 `fuzhou_foods` JSON | Nemotron 把 JSON 文本主动填入 `done.summary`，应用原样发布 | 模型误选输出格式；通用提示存在诱导面 |

两次任务均实际搜索、打开了网页。不存在本次证据支持的“网页工具完全没有执行”“前端把正常回答转换成 JSON”或“工具参数解析失败导致所有症状”。

## 1. 原始证据与核对范围

只读打开 `C:/Users/Administrator/.codey/durable.sqlite3`，核对 `tasks`、`entries`、`effects`、`artifacts`；读取已有 provider request/response 产物及 run trace。没有重新发送历史请求或调用收费模型。

| 项目 | 文件报错会话 | JSON 会话 |
|---|---|---|
| Conversation | `6hv0lqae` | `j1jgsz2r` |
| Run | `run_f7dd91d229164e079b23c0f985547a2a` | `run_0b3ee39d2aa548c49ba960977cc71b2f` |
| 模型 | `cline-free/solar-mini4` | `nvidia/nemotron-3.5-lightning-free` |
| Provider / 协议 | `cline` / `openai-completions` | 相同 |
| Native tools | 开启 | 开启 |
| 用户原句 | 你帮我上网看看福州有什么好吃的 | 相同 |

两次都从新的受管 workspace 开始。原始 initial listing 为 `(empty)`，用户没有要求生成文件，也没有要求 JSON。

当前激活 release 的 `codey/operations/kernel_prompt.py`、`codey/toolchain/runtime.py`、`codey/web/assets/app.css` 与当前工作树对应文件逐字节 hash 相同。原始请求也确实包含下文分析的 JSON 格式说明，因而不只是在尚未部署的源码中发现相似文案。

## 2. 文件错误：模型臆造读取目标，路径修复保留了错误操作

原始调用顺序：四次 `web_search`，两次 `open_result`，随后两次 `read_file`，最后 `done`。打开过福州市文旅局页面和 EastChinaTrip 美食页面。

第一次读取在第 7 轮，参数是受管 workspace 下 `福州美食.md` 的绝对路径。合同拒绝绝对路径，返回：

```text
paths must be relative to the project root: absolute drive paths are not allowed
Correct read_file; required arguments: path.
```

第 8 轮模型改用：

```json
{"path":"福州美食.md"}
```

此时路径合法，但文件不存在，因此实际执行返回：

```text
ERROR: not a file: 福州美食.md
```

两次读取前没有创建文件的调用。第 9 轮模型仍正常交付了美食推荐，说明这次本地读取并非回答所需的前置步骤。

代码位置：[read_file](../../codey/toolchain/runtime.py:814)、[最终文件检查](../../codey/toolchain/runtime.py:290)。界面中的 `Tool failed` 是失败工具行的通用标签，并非一个单独的联网错误。

在全新临时空目录中调用生产 `read_file`，已确定性复现完全相同的错误文本。不能从现有记录判断模型为何第一次想到这个文件名；能够确认的是该调用来自模型，不是网页 provider 自动要求读取本地报告。

应用的放大因素是普通 Chat 同时暴露编码工具和 workspace 指引，以及 [repair prompt](../../codey/operations/kernel_prompt.py:172) 优先要求修正已选调用。记录证明路径修复后发生了第二次读取；尚未做配对模型试验，不能声称删掉某一句提示就一定消除误调用。

## 3. JSON：模型误解了条件式格式说明，发布通道没有改写回答

原始 Nemotron 调用仅为 `web_search → open_result → done`。最后原生调用的 `arguments.summary` 与持久化 `assistant_message.text` 完全一致，包括格式错误。

通用提示有两处相关文字：

- [_native_final_rule](../../codey/operations/kernel_prompt.py:45)：`Requested JSON is a bare document without Markdown fences inside summary, not the outer tool-call object.`
- [通用任务规则](../../codey/operations/kernel_prompt.py:139)：`Preserve the user's requested final format, including JSON if requested.`

本意是“用户要求 JSON 时如何包装”。本次原始 provider reasoning 明确表明模型注意到用户未要求 JSON，却仍把这段协议说明当作选择 JSON 的依据。由此能确认本次格式误读与通用格式提示有关；没有证据证明它是跨模型、跨任务的唯一成因，也没有确认改写文案后的成功率。

另外，最终字符串本身不是合法 JSON。Python `json.loads` 在字符偏移 1929 报 `Expecting ',' delimiter`；多个菜品的字段也未正确组织成独立数组元素。实际外层工具参数是合法的 `{"summary":"..."}`，内层 summary 作为普通回答文本不需要通过 JSON 解析，因此合同接收成功。

用原始 `done` 参数重新进入生产 Chat Completions decoder，summary 仍逐字保留。用生产 Markdown renderer 回放，最终回答得到一个普通段落、零个列表。不是 decoder 把九项正常内容压扁，也不是 Markdown 丢掉了一个已有的正常菜品列表。

不能通过“所有 summary 都解析成 JSON”来修复；普通自然语言、Markdown、代码等都是合法回答。也不能默认把看起来像 JSON 的文本自动转成推荐卡片。

## 4. 九个空列表：复现于 text/html 剪贴板

[process.ts 的 thinking()](../../frontend/src/process.ts:260) 创建一个折叠的 `<details class="thinking">`，后续各轮内容持续追加到同一位置。因此最终轮的列表结构在 DOM 中位于第一次搜索之前，与用户粘贴的排列相符。

原始 Nemotron 三轮 reasoning 长度分别为 448、309、6598 字符，其中有九条美食列表。生产 renderer 回放后，Thinking DOM 中恰有九个非空 `<li>`；最终回答没有 `<li>`。

将回放生成的真实 DOM 与当前 `tokens.css/app.css` 放入隔离页面，使用 Playwright 启动 Chromium，实际执行 `Ctrl+A → Ctrl+C`，同时读取剪贴板 `text/plain` 和 `text/html`：

| 页面状态 | DOM 中 Thinking 列表 | 剪贴板 HTML 的空列表项 | 纯文本的空 `-` |
|---|---:|---:|---:|
| 外层过程折叠 | 9 条非空 | 9 | 0 |
| 外层展开、Thinking 折叠 | 9 条非空 | 9 | 0 |
| 外层展开、Thinking 展开 | 9 条非空 | 0 | 0 |

折叠时剪贴板 HTML 实际包含以下结构：

```html
<ul class="md-list"><li><p class="md-p"></p></li><!-- 共九项 --></ul>
```

同时还保留了若干空段落。富文本接收端转 Markdown 时，这种空列表结构会表现为空 `-`。因此这是复制后产生空结构的问题；本次隔离页面截图没有显示九条空列表，普通 `text/plain` 也没有九条空列表。没有对用户实际粘贴接收端做自动化，能直接确认的边界是 Chromium 已产出九个空 `<li>`。

隔离页面加入以下候选 CSS 后，再次实际复制：

```css
.thinking:not([open]) > .thinking-body {
  display: none;
}
```

两种折叠状态的剪贴板空列表项都由 **9 降为 0**；Thinking 展开时仍复制九条完整列表。另一个同时显式隐藏折叠 `.process-body` 的候选结果相同。本次可确认最小 CSS 对目标复制缺陷有效，尚未修改生产样式。

## 5. 建议的修改顺序

1. **先修复制缺陷。** 在既有 [Thinking 样式](../../frontend/public/assets/app.css:439) 中显式隐藏折叠 body；检查其他过程 disclosure 是否有相同问题。保留现有“复制最终答案”按键从 canonical 文本复制的路径。回归必须使用真实 Chromium 的 `text/html`，仅 jsdom `textContent` 看不见此问题。测试折叠/展开、真实非空列表、纯文本、富文本，以及跨消息选择。

2. **再简化普通 Chat 的完成提示。** 明确 `done.summary` 是完整的用户可读回答；未指定格式时，默认以用户语言自然回答，可用 Markdown。用户明确指定其他格式时尊重要求。合并重复的格式说明，避免在所有普通问题中突出“裸 JSON”。先用两个涉事模型分别跑自然问答、显式 JSON、显式表格、代码/多行字符串、网页读取等配对用例；验证收益后再改通用提示，不能把本次日志关联当作已验证的普遍修复效果。

3. **减少无关本地工具调用。** 在普通 Chat 工作指引中说明：网页工具返回正文不代表已创建本地文件；本地读取应与用户任务和实际 workspace 有关。评估协议修复是否允许模型放弃已经不必要的调用，而不只是修正参数。保留文件存在检查与真实错误。不要遇到文件不存在就创建空文件、假装读取成功，或隐藏失败记录；也不建议仅凭“上网”等关键词切换到一套禁止文件工具的新执行流程，因为混合网页/编码任务仍需要这些能力。

普通 Chat 的完成检查可证明执行合同满足，不能证明自由文本一定达到推荐质量。本次 `done` 成功不应被当成内层 JSON 有效、菜品事实全部正确或格式完全符合用户意图的证明。

## 6. 本次验证与本地产物

本次是**真实失败记录的确定性回放**，不是让模型重新随机生成一次，也不是新一轮多模型成功率评测。

- 只读历史核验：两次原始模型、实际参数、网页读取、错误回执与最终回答一致。
- 生产 `read_file` 最小复现：返回相同 `ERROR: not a file: 福州美食.md`。
- 生产 native decoder 回放：两次 summary 均逐字保持；内层 JSON 语法错误独立确认。
- 临时 Vitest probe：使用真实 `Render`、`Process`，通过 1/1；原始与回放 DOM 结构一致。
- Chromium 实际复制：两种折叠状态各产生九个空 `<li>`；两个候选 CSS 均消除，展开仍保留九项。

原始会话派生夹具、回放脚本、DOM HTML、截图和完整统计留在忽略目录 `.e2e-artifacts/chat-food-diagnosis-20261109/`：`reproduce.py`、`reproduction.json`、`render.probe.test.ts`、`dom-reproduction.json`、`browser_probe.py`、`browser-reproduction.json`。这些夹具含该两次会话的原始显示记录，未加入版本控制。

复跑后端和浏览器探针：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python .e2e-artifacts/chat-food-diagnosis-20261109/reproduce.py
python .e2e-artifacts/chat-food-diagnosis-20261109/browser_probe.py
```

重新生成 DOM 时，将产物目录中的 `render.probe.test.ts` 临时复制为 `frontend/src/chat_food_diagnosis.probe.test.ts`，在 frontend 执行 `npx vitest run src/chat_food_diagnosis.probe.test.ts`，完成后移除该临时文件即可。生产文件与用户数据未修改；只新增本报告。

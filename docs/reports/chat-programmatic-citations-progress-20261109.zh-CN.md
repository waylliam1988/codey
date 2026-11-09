# Chat 程序化引用实施进度

日期：2026-11-09（Asia/Taipei）。接续基线：`c0b1f2e7`。

## 约束与设计

复用已提交来源回执及其 `TaskSession.source_ids` / `search_results` 投影，分别代表成功读取的来源与搜索线索。不新增来源表、运行时、持久化路径或完成协议。后端在现有完成检查内将正文显式 `[sN]` / `[rN]` 解析为 Markdown 链接；原结果与最终消息链路保存这一份最终文本，显示、复制、导出、历史回放均读取它。原始模型回复继续保存在原有 provider 回执，不双写来源绑定。

只解析明确的正文引用；代码块、行内代码与 JSON 原文保持不变。未知编号反馈给模型，不猜 URL，不将搜索线索当作已读正文，不附上未选择的来源。不自动推断句子与来源的语义关系。

删除第三批“原样复制 URL”提示，改为来源编号协议。保留网页不生成文件、普通错误调用可重新判断必要性的指引；这些约束解决不同的问题。

## 当前接续点

实现、定向测试、实机和代码检查已完成，随本文件独立提交。前面三批提交不变；本批用来源编号协议替换第三批的 URL 复制提示。未改用户模型配置、原始会话或打包配置，未部署服务。全部实机使用本地 KoboldCpp，不调用额度耗尽的 Solar 或其他云模型。

## 实现位置与唯一数据路径

- `operations/chat_citations.py`：只编译显式正文编号，将既有绑定中的 URL 编码为合法 Markdown 目标；支持连续编号、逗号组与重复引用，不补未选择来源。未知编号、非法编号及模型自行给保留编号指定链接目标，交给现有完成失败反馈修复。
- `operations/completion_gate.py`：只在普通、允许网页读取的 Chat 完成检查中调用；Research 和项目任务保留所属协议。最终 `GateVerdict.final_text` 经既有 `DoneDecision → KernelResult → assistant_message` 交付。恢复读取已提交 decision/result，无须重新生成链接。
- `operations/task_guidance.py`：仅说明正文使用 `[sN]`、搜索入口使用 `[rN]`，程序生成 URL。网页不生成本地文件、取证边界和可放弃无关错误调用的既有指引继续保留。
- `utils/citation_scanner.py`：共享代码区域遮罩并保留原文偏移，Chat 与 Research 共用；删除 `research/answer_contract.py` 内重复的遮罩实现。覆盖围栏、未闭合围栏、行内/跨行代码、缩进代码及列表/引用中的代码。
- `policies/output_requirement.py`：在相关 JSON 边界使用既有 `JsonObject` / `JsonValue`，删除该文件全部 `Any`，签名改为明确的 `Mapping[str, object]`，不改变运行行为，不增加强制转换或忽略规则。

有效 JSON 逐字保留；显式 JSON 要求即使暂时输出无效 JSON，也由原格式检查负责反馈。代码区域不解析编号。未自动改写普通命名链接或用户给的 URL；程序只保证所选编号的链接身份，不判断每个结论是否得到来源支持。

## 测试与失败归因

新增 `test_chat_programmatic_citations.py` 和 `test_chat_citations_delivery.py`，更新既有任务指引回归。交付测试使用真实 headless/kernel/provider 协议入口与确定性 API 回复夹具，覆盖 native/text 两种工具协议、未知编号拒绝后的完整修复、JSON/代码保留、关闭重开后的唯一最终消息。Chromium 打开实际应用资源，验证链接 href、按钮复制 Markdown、Ctrl+A/C 富文本和刷新回放。

实现过程保留失败并按所有者修复：

- 新编译器最初把连续 `[s1][s2]` 当作手写 Markdown 目标而拒绝；先补回归得到 1 失败 / 5 通过，再修复相邻编号识别。正文后接冒号正常，独立来源定义仍拒绝。
- 新共享遮罩最初未正确区分列表续行正文、嵌套代码与引用围栏；新增嵌套回归后修复遮罩，并补跑直接受影响的 Research 引用/完整答案测试。没有新增第二个 Markdown 运行时。
- 交付夹具先后暴露了模拟连接未使用既有 admission fixture、JSON 协议未关闭 thinking、AppContext 并非上下文管理器、历史按钮身份和 hover 前无法命中等测试接线问题。修正夹具以使用真实接口与 UI 交互，没有调整生产来迁就测试。

最终命令：

```text
python -m pytest -q tests/test_chat_programmatic_citations.py tests/test_chat_citations_delivery.py tests/test_chat_tool_reconsideration.py tests/test_chat_final_format_guidance.py tests/test_task_input_and_output_contracts.py tests/test_citation_scanner.py tests/test_research_whole_answer.py tests/test_research_full_prose_repair.py tests/test_research_finding_citations.py tests/test_chat_web_turn_contract.py
```

结果：**119 通过，39.21 秒**。之所以包含 Research 回归，是本批共享了代码区域遮罩；没有运行完整 pytest、覆盖率报告或安装包测试。九个改动 Python 文件 Ruff、mypy（`--explicit-package-bases --follow-imports=silent --python-version 3.11`）、Pyrefly、ty 均无错误；`kernel_type_precision_gate` 通过，受保护范围动态类型计数为零。Pyrefly 有 10 条警告，展开确认全部位于未改动的 `completion_gate.py` 既有 `str()` / `int()` 转换语句；未把“无错误”描述为“无警告”。改动文件没有 `Any` 类型、cast 或 type-ignore。

## KoboldCpp 实机

后端：`http://127.0.0.1:5001/v1`，KoboldCpp `1.117.1`，`Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。沿用原有该后端兼容协议、关闭 thinking，不改持久配置。实验复用已有生产 headless/gate 工具，没有另写 agent loop。

受控来源矩阵（真实模型生成，网页为明确的实验材料）：

| 场景 | 结果 | 秒 |
| --- | --- | --- |
| 本地文件 | 2 轮完成，核验码/偏好正确，工作区未修改 | 10.875 |
| 本地文件 + 网页 | 4 轮完成，read_file → web_search → open_url；草稿只有 `[s1]`，最终生成真实绑定链接 | 22.969 |
| 网页问答 | 3 轮完成，草稿只有 `[s1]`，最终链接匹配 | 22.016 |
| 显式 JSON | 1 轮完成，原文保持 | 7.094 |
| Python 代码 | 1 轮完成，原文保持 | 4.953 |

五项既有任务断言和新增“最终文本等于原始草稿按既有绑定编译结果”检查均通过；网页/混合草稿均未拼写 URL。产物：`.e2e-artifacts/chat-programmatic-citations-20261109/controlled/review.json`。未知编号完整修复由确定性真实入口测试覆盖，不声称模型在实机中自然产生了同样错误。

最终代码另跑真实互联网原问题：

```text
python tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/chat-programmatic-citations-20261109/live-final --cases food_citations --variants baseline --repeats 1 --mode live --timeout 180
```

**3 轮 done，1 次搜索、1 次正文读取，46.656 秒；prompt/completion tokens 为 21,176 / 682。** provider 原始 done 草稿有 10 个 `[s1]` 且不含 URL，最终十处均生成 `https://www.sohu.com/a/881456447_529342` 链接，结构条件通过，无本地文件错误调用。`baseline` 表示没有安装实验 candidate，运行的是当前生产代码。较早一次同题实机也通过（49.313 秒），产物保留在 `live-initial`，没有覆盖。

限制：模型仍沿用单篇旅游文章的“最新/十大”与餐厅推荐，未独立核验店铺现况；受控材料答案也有“经典/清淡”等扩展措辞。程序保证链接绑定与拼写，不把结构通过等同事实全部正确，也不声称已证明所有模型都遵循编号协议。

# 普通 Chat 格式、复制与工具纠错：三批实施进度

日期：2026-11-09（Asia/Taipei）。授权：三批分别实施、补测试、每批跑对应 KoboldCpp 实机、lint/类型检查、更新文档后独立提交。允许修复过程中确认的确定性生产缺陷。

## 当前接续点

- 基线：`aef756dd`。初始工作树只有上一轮新增、尚未提交的诊断报告。
- 第 1 批已提交：`fcc5fe7c`，折叠 Thinking 的富文本复制修复。
- 第 2 批已提交：`aff736a8`，完成提示默认自然回答，保留用户显式格式；86 条定向测试与 20 次本地模型配对通过。
- 第 3 批已提交（本文件所属提交）：网页/本地文件语义、可放弃无关错误调用、来源 URL 精度指引；78 条定向回归、最终 KoboldCpp 实机与类型/lint 门禁通过。
- 不部署服务、不修改用户配置和原始会话。

## 验证边界

优先运行直接相关的测试，不运行完整 pytest、覆盖率或安装包构建测试。前端样式需要经过现有前端构建与资源同步，使真实浏览器测试验证实际交付的资源；这不是安装包构建或应用发布。

每批保留实机输入、provider 原始记录、事件、摘要和人工审阅结论。KoboldCpp 当前预检为 `127.0.0.1:5001` / `1.117.1`，加载 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。沿用已有该版本的工具协议兼容路径，不改变持久配置。

新增代码使用明确类型，不新增显式或隐式动态类型；修改范围内发现类型问题时在所属边界解决。Python 运行对应文件的 Ruff、mypy/Pyrefly/ty（Python 3.11 目标）及现有类型精度门禁；前端运行 TypeScript 与相关浏览器/单元回归。失败先分类为生产问题、测试滞后或实验/环境问题，再修复其所有者，不放宽断言掩盖生产问题。

## 第 1 批：复制

计划：先用实际应用资源和 Chromium 剪贴板补稳定回归，确认基线失败；显式隐藏折叠的 Thinking body，验证折叠不带空列表、展开保留列表、跨消息最终回答保留。KoboldCpp 跑普通网页回答，并核对其结果可正常交付与复制。原始故障涉及的隐藏 reasoning 用独立浏览器夹具覆盖；本地 Gemma 兼容路径关闭 thinking，不能声称实机自然生成了相同 reasoning。

结果：生产样式增加折叠 `.process-body` 与 `.thinking-body` 的显式 `display:none`，同步现有 `codey/web` 资源。新增真实应用/Chromium 回归覆盖四种内外层组合、富文本/纯文本与跨两轮答案的列表复制。

- 基线先运行新测试：3 失败 / 1 通过。两个失败确认为九个空 `<li>`；内层展开、外层折叠还暴露了富文本保留隐藏内容的问题，因此同时处理外层容器。修复后四种组合均通过；测试明确核对真实折叠状态，避免点击 summary 意外改变状态。
- `python -m pytest -q tests/test_chat_clipboard_browser.py`：4 通过。
- `npx vitest run src/process_thinking_dom.test.ts src/answer_copy.test.ts src/streaming_markdown.test.ts`：19 通过。
- `npm run build`（包含 TypeScript），`python -m tools.publish_frontend` 与 `--check`：通过。只同步仓库内前端资源，没有发布应用或运行安装包测试。
- `python -m ruff check tests/test_chat_clipboard_browser.py`，以及该文件的 mypy / Pyrefly / ty（Python 3.11）：通过。`python -m tools.kernel_type_precision_gate`：通过，既有受保护范围的动态类型计数为零。
- KoboldCpp 生产入口：`tools/chat_web_recovery_experiment.py --cases food_citations --variants baseline --repeats 1 --mode live`。原问题在 3 轮内 done，1 次搜索、1 次正文打开；回答使用自然 Markdown 并给出实际来源，没有本地文件调用，结构条件通过。耗时 57.578 秒，prompt/completion tokens 为 20,761 / 807。人工审阅确认主题相关和来源已读，未独立核验所有餐厅现况；不把结构通过当作所有推荐事实已认证。

实机产物：`.e2e-artifacts/chat-food-fixes-20261109/batch1-live/r1-food_citations-baseline/`。没有更改完成提示或纠错指引。

## 第 2 批：完成格式

计划：合并普通完成规则，默认自然语言/Markdown，明确用户格式优先；不改变工具参数编码和严格 Research 提交合同。补自然问答、显式 JSON/表格、多行代码与原生/文本协议的定向测试。用生产 headless 入口做 KoboldCpp 基线/候选配对，再验证最终生产版本。

用户补充：Solar mini4 额度耗尽，实机改用本地模型。本轮各批均只用本地 KoboldCpp，不调用云端模型；原涉事 Solar/Nemotron 的跨模型收益不在本轮已验证范围内。

结果：`kernel_prompt.py` 移除 native 完成规则中的“裸 JSON”示例说明；通用完成提示明确用户格式优先、默认使用用户语言自然回答及适当 Markdown。原生工具参数编码、文本工具调用 JSON、显式 JSON 输出检查与结构化 Research 合同均保留。

- 新增 `tools/chat_answer_format_gate.py`：每个独立 worker 使用生产 headless/kernel/provider，旧提示只在基线 worker 临时还原两个已审阅片段；不更改源响应或模型回复。网页获取统一使用明确标记的受控来源，真实模型温度 0，第二轮反转两版本顺序。
- 五类任务各两轮配对：自然介绍、显式 JSON、Markdown 表格、多行 Python 代码、原始网页问题；旧/新各 10/10 格式条件通过。总轮数 18 / 17，总 prompt tokens 100,299 / 94,229，单任务耗时中位数 7.429 / 7.5465 秒；小样本不证明稳定提速或 Nemotron 故障已消失。
- 另跑未经实验 patch 的真实联网原问题：3 轮 done，1 搜索 + 1 正文读取，带实际链接，结构条件通过，54.469 秒。产物 `.e2e-artifacts/chat-food-fixes-20261109/batch2-live/`。
- 定向 pytest：`test_chat_final_format_guidance`、`test_native_final_answer_protocol_repair`、`test_kernel_runtime_guidance_for_verified_and_blocked_tasks`、`test_kernel_prompt_task_guidance_ownership`、`test_answer_observation_requirements`、`test_contract_rendering_and_scoped_repair`、`test_native_tool_prompts_use_wire_schemas_without_text_wrappers`，共 86 通过。实验工具的表格顺序判断改为允许任意菜品顺序后，新文件 7 条重跑通过。
- 本批三个 Python 文件的 Ruff、mypy / Pyrefly / ty（Python 3.11）和受保护类型精度门禁通过，未新增动态类型。mypy 首次将 tools 同时识别为裸模块与命名空间模块，修正为 `--explicit-package-bases`；实验搜索夹具的切片推断为 `list[dict[str,str]]`，通过声明真实 `list[JsonObject]` 在其所有者修复，未放宽类型。Pyrefly 报 0 error、2 条未展开的 warning；不宣称无 warning。

人工审阅：自然介绍任务有时只读搜索摘要，旧/新表格都曾把肉燕解释为普通肉馅或加入牛肉。此为模型内容准确性与来源使用不足，不是本批格式规则或网页抽取的确定性错误；不把机械格式通过宣传为事实准确。前期非 web 任务 manifest 的 `source_mode:none` 描述不精确，实际网络工具也使用同一受控夹具；工具已改为 `controlled_if_called`，原始产物保留，分析以事件和 provider 请求为准。

配对产物：`.e2e-artifacts/chat-food-fixes-20261109/batch2-format/`，含计划、20 次完整 provider/事件、逐项答案、`usage-review.json`。用户要求改用本地模型，因此未运行 Solar/Nemotron 云端配对。

## 第 3 批：工具选择与纠错

计划：只在现有 Chat 指引说明网页材料与本地文件的区别；普通协议错误允许重新判断是否需要该动作，保留已选继续修复时的参数精度要求。未知/禁止工具和 Research 提交约束保留。补任务范围与纠错路径回归；KoboldCpp 运行原始问题、真实文件读取和无关错误调用的恢复用例。

结果：`task_guidance.py` 在普通 Chat/web 作用域说明网页返回的是工具数据，不会生成本地文件；本地工具用于用户任务涉及的真实工作区文件。`kernel_prompt.py` 的 native 错误详情从命令式 Correct 改为 Rejected；非 Research、已识别的非 done 普通动作可先判断必要性，无须继续无关调用。未知/禁止工具、完成参数错误与 Research 提交仍保留原约束。

新增 `test_chat_tool_reconsideration.py`。首次运行发现两个夹具接线问题：Research 夹具没有 `knowledge.write` 提交权限；文件测试直接调用 execute_turn 却未提供文件执行器。已修正为真实授权和文件运行时入口，不改变生产来迎合夹具。修正夹具后基线 3 失败 / 13 通过，对应待新增的网页/纠错指引；生产修改后连同 frozen schema、工具授权、完成修复、Research 完整草稿修复等直接相关回归共 75 通过。

新增明确类型的 `tools/chat_tool_recovery_gate.py`：真实本地/混合文件任务复用生产 headless；受控故障探针复用生产 snapshot / repair prompt / provider / decoder，验证单次真实模型决策，不另写 agent loop。受控故障是实验输入，不声称模型自然复现了原来的错误。

实机中间结果（保留失败，不覆盖原产物）：

- 原问题真实联网 3 轮 done，1 搜索 + 1 正文读取，无本地文件调用，结构通过，57.875 秒。
- `batch3-tools`：真实文件 2 轮完成，核验码和偏好正确，工作区未改；混合任务只搜索未读正文，并加入未观察细节，证据质量不通过。这是模型行为不足，未发现工具结果丢失或执行器故障。
- 首次受控错误探针：模型放弃虚构文件，改选 web_search。原判定只接受 done，误报失败；按本批设计允许选择其他必要动作，已修正实验判定并补回归。必要文件探针正确选相对路径 notes.md。均为真实模型单次决策，不是完整恢复循环。
- `batch3-tools-explicit-body`：混合任务明确要求打开正文后，实际顺序 read_file → web_search → open_url → done，工具与参数正确；但模型原始 done 将来源 URL 中的连字符改为下划线，最终链接检查失败。provider 原始回复已经包含错误 URL，排除解码/UI 改写。将现有 Chat 来源句改为“Copy acquired source URLs exactly”，不新增链接重写或自动修复。待验证最终版本。
- 抽取实验判定 helper 时多写一个右括号导致 pytest 收集/Ruff 失败，已在实验工具修复；属于新增实验代码错误，未改生产来适配。

最终验证：

- `python -m tools.chat_tool_recovery_gate --run-dir .e2e-artifacts/chat-food-fixes-20261109/batch3-tools-final`：4 项结构条件通过。本地文件 2 轮 done（10.750 秒）；混合任务 4 轮 done（21.750 秒），read_file → web_search → open_url，最终链接逐字匹配，核验码与偏好正确，两工作区均未修改。网页为受控材料，本地模型生成和文件执行是真实的。答案仍有“经典/清淡/细腻”等超出网页事实的修辞，结构通过不等同所有内容都已证实。
- 无关错误读取探针改选 web_search（3.484 秒），必要错误读取探针选择 read_file(notes.md)（1.329 秒）；两者均通过 frozen 解析/授权。只证明下一步选择，不声称探针已跑完整恢复后的最终答案。
- 最终生产指引的真实联网原问题：`tools/chat_web_recovery_experiment.py --run-dir .e2e-artifacts/chat-food-fixes-20261109/batch3-live-final --cases food_citations --variants baseline --repeats 1 --mode live --timeout 180`，3 轮 done，1 搜索 + 1 正文读取，无本地文件调用，链接正确，结构通过；53.969 秒，prompt/completion tokens 21,089 / 828。variant 名称 baseline 表示没有安装实验 candidate，并非还原本批生产代码。单一旅游页面不足以独立认证全部店铺现况或“最新/十大”等措辞。
- 最终相关 pytest：`test_chat_tool_reconsideration`、`test_chat_web_turn_contract`、`test_contract_rendering_and_scoped_repair`、`test_native_tool_prompts_use_wire_schemas_without_text_wrappers`、`test_frozen_tool_views`、`test_kernel_prompt_task_guidance_ownership`、`test_native_final_answer_protocol_repair`、`test_research_full_prose_repair`，共 78 通过；新增文件 19 条。
- 四个修改/新增 Python 文件：Ruff 通过；mypy `--explicit-package-bases --follow-imports=silent --python-version 3.11`、Pyrefly、ty 通过，Pyrefly 0 error / 2 warning（未展开）；`kernel_type_precision_gate` 通过。未新增动态类型、类型忽略或强制转换。新代码的 JSON 容器不变性、联合类型收窄与模块导出检查问题均在其所属代码中修正。
- 本批未发现另一个确定性生产执行器或解码缺陷；实机暴露的漏读正文、URL 拼写与超出来源细节分别按模型行为记录。保留所有失败产物，不用重跑成功覆盖失败结论。未扩大到完整 pytest、覆盖率或安装包测试。

## 后续建议：Chat 引用由程序生成

用户提出 URL 可由程序生成以减少模型负担。代码核对确认：`kernel_facts.bind_source_identity` / `_record_open_fact` 将已读来源的 `sN → final_url` 放入 receipt 和 session；`kernel_prompt._result_context` 已向模型提供来源编号。Research 的 `answer_report.render_answer` 已按模型选择的 `[eN]` 与冻结来源绑定生成引用来源列表；普通 Chat 尚未接入同类交付机制。

建议 Chat 复用现有来源身份：模型仅选择来源编号，后端从本任务真实来源绑定中解析 URL、校验未知编号、生成引用链接，并将结构化引用随最终答案持久化，UI/复制/导出/历史回放共用。应区分搜索线索与成功读取的正文；只渲染模型选择的来源，避免把所有浏览记录都冒充答案依据。显式 JSON/代码输出需保留用户格式，不能在原文末尾无条件附 Markdown。程序保证链接身份和拼写；来源是否支持具体结论仍属于模型取证和判断。

这一建议属于后续交付协议改造，本轮三批未实施；当前“原样复制 URL”是已验证的提示改进，不宣称提供程序级确定性。

## 证据

- [原始失败诊断](chat-food-model-failure-diagnosis-20261109.zh-CN.md)。其中“未修改生产”的描述属于诊断阶段，不代表三批实施后的状态。
- 本轮运行产物统一放入忽略目录 `.e2e-artifacts/chat-food-fixes-20261109/`，后续在各批结果中补准确路径、命令、统计与提交。

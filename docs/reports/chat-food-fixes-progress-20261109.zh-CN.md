# 普通 Chat 格式、复制与工具纠错：三批实施进度

日期：2026-11-09（Asia/Taipei）。授权：三批分别实施、补测试、每批跑对应 KoboldCpp 实机、lint/类型检查、更新文档后独立提交。允许修复过程中确认的确定性生产缺陷。

## 当前接续点

- 基线：`aef756dd`。初始工作树只有上一轮新增、尚未提交的诊断报告。
- 第 1 批已完成，待本批提交：折叠 Thinking 的富文本复制修复。
- 第 2 批待办：完成提示默认自然回答，保留用户显式格式。
- 第 3 批待办：网页/本地文件语义与放弃无关错误调用的纠错指引。
- 尚未创建本轮提交；不部署服务、不修改用户配置和原始会话。

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

结果：待填。

## 第 3 批：工具选择与纠错

计划：只在现有 Chat 指引说明网页材料与本地文件的区别；普通协议错误允许重新判断是否需要该动作，保留已选继续修复时的参数精度要求。未知/禁止工具和 Research 提交约束保留。补任务范围与纠错路径回归；KoboldCpp 运行原始问题、真实文件读取和无关错误调用的恢复用例。

结果：待填。

## 证据

- [原始失败诊断](chat-food-model-failure-diagnosis-20261109.zh-CN.md)。其中“未修改生产”的描述属于诊断阶段，不代表三批实施后的状态。
- 本轮运行产物统一放入忽略目录 `.e2e-artifacts/chat-food-fixes-20261109/`，后续在各批结果中补准确路径、命令、统计与提交。

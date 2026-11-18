# 默认网页能力：修复、对照和剩余失败

日期：2026-11-18。工作区：`D:\codey`，`master`；检查基线 HEAD：`a813e2e7`。
未创建、复制 worktree；未停止、重启用户服务；未运行全量 pytest。

## 结论

1. **本轮未提交修改确实造成了搜索入口退化，已修复。** 默认开放浏览器后，原有聚合逻辑把搜索、打开网址和文件工具一起藏进 CodeMode；模型直接调用 `web_search` 时会被当前工具快照拒绝。这不是单纯的模型能力问题。
2. **现在生产入口能够搜索，也能够打开正文。** 生产回归覆盖 Chat、项目和 Hybrid 的搜索→打开→按正文交付；真实模型在受控知乎内容测试中完成了这个链条。真实福州城市查询执行了搜索，但没有打开正文。真实知乎美食查询仍未完成，不能宣布自然上网任务已可靠解决。
3. **历史通过、今天失败不能统一归因。** 有本轮引入的工具面问题，也有默认权限改变后的旧断言、既有测试替身不符合当前资源合同、修改前同样失败的旧测试，以及真实模型输出错误。以下分别记录，不用重跑通过覆盖首次失败。
4. **用户最新 Ghost 错误存在独立的确定性触发点，已修复。** 审批暂停更新了近期关注，恢复请求重建的 Ghost 内容与冻结来源不一致。现在仅成功完成时更新，不关闭来源复核。

## 生产修改

- 前端移除普通对话和项目对话的 Browser 按钮、开关状态及提交时的显式开关字段；已构建并发布仓库内静态资源。
- `task_policy.py` 是新任务默认权限的唯一来源。Chat、project、hybrid、auto、research 默认具备 `web.read` 和现有全部浏览器能力。明确禁网仍优先；恢复旧任务保留原冻结权限。只读规划仅搜索、观察，不放开交互及写入。
- `kernel_protocol.py` 保留原搜索、阅读、知识和文件的外层工具入口；浏览器叶子仍通过原 CodeMode 和原跟进调用机制执行。任务明确要求 CodeMode only 时继续聚合，不新增执行器。
- 严格研究尚未登记问题时，不构造需要 `question_ref` 的工具，避免空元组索引异常；外层工具列表同步过滤为实际冻结工具，避免后续找不到工具。
- `planning_flow.py` 保留只读观察权限，按该权限绑定原浏览器资源，修复入口和冻结授权不一致；本文件同步清理原动态类型。
- `ghost_post_turn.py` 只有 `stop_reason=done` 才同步 continuity；审批、提问、失败、取消不将未完成任务写成新经历。
- 实机记录工具修复 generation 返回类型、事件回调类型以及 JSON/反射边界的隐式动态类型，没有更改生产完成合同来接受错误答案。

## 用户最新错误的原始证据

用户任务：`帮我上网看看福州是什么城市`。

- run：`run_b946e0685a57449887038d26ad3d93db`；session：`qlkdgquc`。
- 13:29:09 首次请求；13:29:14 等待批准；13:29:20 恢复。
- 恢复后打印命令执行成功，13:29:22 下一次发送被 `ghost_sources:source_snapshot_changed` 拦截。打印文字并不是搜索成功。
- 原请求外层工具有 CodeMode、run、shell、read_tool_result、done、ask_user，缺少直接搜索入口。
- 初始 Ghost 冻结了上一轮“你好”的关注；审批暂停又写入当前任务关注，恢复时发生冲突。
- 五份原始 artifact/ledger/trace 保留于下述证据根的 `user-fuzhou-approval-first/`。

回归通过真实入口先完成 seed，再分别测试热恢复、冷恢复、撤回及过期。热恢复保留应用上下文，但按生产生命周期重新连接已关闭的 provider。真正的撤回/过期仍阻断发送，没有跳过 admission。

## 自动验证

以下计数有覆盖重叠，不能相加作为独立测试总数。

| 验证 | 结果 | 说明 |
| --- | --- | --- |
| 最终定向生产回归 | 147 passed | 默认权限、工具快照、生产搜索阅读链、入口限制、Ghost 恢复/撤回/过期、来源 admission、浏览器完成合同 |
| 发布 UI 的 Chromium 回归 | 4 passed | 普通/项目 × Research 开/关；没有 Browser 按钮，提交交由后端默认授权 |
| 前端检查 | 65 files、408 tests passed | TypeScript、格式、Vitest |
| 全树 Ruff | 通过 | 原仓库配置 |
| 生产 mypy | 709 files 通过 | Python 3.11 目标 |
| 生产 Pyrefly | 0 errors | 另有既有 35 suppressed、160 warnings，不等于没有历史抑制和告警 |
| 生产 ty | 通过 | Python 3.11 目标 |
| kernel 类型精度门 | 通过 | 未关闭检查 |
| 前端发布一致性、diff 空白检查 | 通过 | `publish_frontend --check`、`git diff --check` |
| 修改模块加强 Any 检查 | 13 files 通过 | 在原严格 mypy 配置上，额外启用 `disallow_any_expr` 和 `disallow_any_explicit` |

其余三个修改测试文件也做了加强类型审核：目标文件没有诊断，但导入的五个未修改测试依赖报 154 个错误，因此这次检查整体没有通过。没有关闭导入检查，也没有为消除这组错误扩改无关的大测试模块。不能声称全测试树类型检查通过。

扩大回归首次得到 **12 failed / 246 passed**：其中三个测试断言“普通入口默认不能操作浏览器”，与本次明确要求冲突，已改为显式 denied 后验证禁止提权，最终定向回归通过。其余九个失败用修改前的三个相关模块作同条件对照，**同样失败**（对照 9 failed / 3 passed）。包括旧等待故障注入、旧文本/JSON 解析假设、CodeMode only 跟进断言，以及预取后读取次数为 3 而旧断言为 2 等。没有把它们算作已修复。

另一个入口/规划组首次得到 **7 failed / 118 passed**。其中有旧知识库替身不符合正式类型、旧执行器假设，以及仅支持 search/fetch 的搜索替身无法绑定当前浏览器能力。默认浏览器让后一类旧替身问题暴露出来；没有为测试替身增加第二套运行时或放宽正式资源合同。新增加的搜索阅读回归使用真实 `WebSourceProvider` 资源，只替换网站 search/fetch 返回，验证生产执行链。

因此，本次是相关修复回归通过，**不是所有相关历史测试全部变绿**。

## 实机结果

使用原本地模型 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，原 endpoint `http://127.0.0.1:5001/v1`，temperature 0、seed 123456789。保留实际请求、回执、完成提案和模型输出。相同 seed 不代表不同时间的请求完全相同。

### 原参数 17 项 full-surface 矩阵

`fixed-full-surface-first/`：**15/17 通过**。复用原 `_case`，未减少原覆盖或更改 `declare_required_tools`。

通过：basic、structured、legacy_structured、reread、hybrid、hybrid_codemode、stale_edit、upload、download、research、research_sequence、source_failure、fixed_failure、research_codemode、stale_ref_injected。

未通过：

- `codemode`：max_turns。正式回执显示程序在找到 Locality 后 `break`，未继续寻找 Load record；返回按钮缺失后，模型反复提交相同程序，未填写或阅读。不是换行语法错误。
- `stale_ref`：任务交付结束，但模型没有执行测试要求的故意使用旧引用负例，未覆盖验收分支；不能视为生产接受了错误引用。单独注入的 `stale_ref_injected` 通过。

### 修改前同条件对照

通过内存加载 HEAD 中 `task_policy.py`、`tool_contract.py`、`kernel_protocol.py` 原字节进行对照；没有改磁盘源码，也没有复制工作区。这只是三个相关模块的修改前对照，不能声称完整旧版本 A/B。

- 当前 Research 与原模块 Research 都通过。
- 原模块 CodeMode 同样 max_turns，首次模型程序与当前失败首答完全相同。
- 两次 CodeMode schema SHA 相同：`43c38a74688a8949e5f3b0904912e8af4dabf1db9b97bb3e8ba32cbdada0ee6c`。
- 记录：`codemode-request-comparison.json`。历史成功的模型首答与今天不同，因此不能仅凭模型名、seed 和“同一个测试名”认定完全同请求。

完整矩阵完成后做过纯类型清理；不能把那次源码摘要称为最终源码摘要。以下四入口补验在最终生产源码上运行，前后摘要相同。

### 默认能力四入口补验

`default-four-final/`：空 `requested_capabilities`，`declare_required_tools=False`，**2/4 通过**。这些条件与上述原 17 项矩阵不同，不能互换结论。

- Chat：通过。
- Hybrid：通过，包含网页操作及项目写入。
- project：未完成，no_progress。先把控件名字当 ref；随后拿到正确 ref，填写后继续用失效按钮 ref；最后 edit 缺失外层读取凭据 `base_result_ref`。正式工具均按原合同拒绝，无成功写入。页面被打开/读取过，不代表已成功取得动态查询结果。
- strict Research：未完成，max_turns。已经打开页面，反复 snapshot→点击 Locality 输入框，没有执行填写及后续正文读取；没有工具拒绝可解释其停滞。

这些失败都保留，不用修改提示或加轮数后通过来替换首轮。

### 普通 Research 自主查询

`ordinary-query-first/`：使用原 `research_query_delivery_live_gate` 的 ordinary case 和参数，**通过**。2 次模型调用，1 次点击，1 次动态查询读取，答案引用实际结果。只补验此项，没有声称重跑了历史所有自主查询子场景。

### 用户自然问法

`natural-search-first/`：复用现有行动探针和生产入口，不预声明必须打开的工具。

| 输入与环境 | 观察 | 判定 |
| --- | --- | --- |
| 去知乎看福州美食，受控正文 | 1 搜索、1 打开、3 模型请求；回答含仅正文出现的信息 | 完整链通过；不是实际访问知乎 |
| 上网看福州是什么城市，真实网络 | 1 搜索、0 打开、2 请求，引用搜索摘要 `[rN]` | 搜索可用；未证明正文阅读 |
| 去知乎看福州美食，真实网络 | 4 搜索、0 打开、5 请求，结果跑题后仍声称知乎推荐 | 用户任务失败；真实性与自主行动仍有限制 |

另行不经过模型，用生产 `BrowserSearchProvider` 独立查相同中文关键词及 `site:zhihu.com 福州 美食`：最终 Bing URL 与输入框保留正确查询，但结果 DOM 含百科、旅游站、政府网站等非指定来源链接。保存 URL、输入框值、结果 DOM 和完整 HTML。说明当前搜索页面确实返回跑题结果，不能只归因于模型生成了错误 query；尚未证明是 Bing 本身、网络环境还是页面处理造成。未增加语义过滤来伪造相关结果，也未因此修改生产搜索合同。

## 证据位置与使用限制

主证据根：`D:/codey-local-evidence/browser-default-20261118-resume/`；此前首轮在 `D:/codey-local-evidence/browser-default-20261118-first/`，没有删除或覆盖。

自动检查日志在 `D:/codey-local-evidence/`：

- `browser-resume-final-targeted.log`、`browser-resume-ui-final.log`、`browser-resume-frontend-final.log`。
- `browser-resume-production-focused-final.log`、`browser-resume-old-regression-comparison.log`、`browser-resume-regression-first.log`。
- `browser-resume-mypy-final.log`、`browser-resume-pyrefly-final.log`、`browser-resume-ty-final.log`、`browser-resume-ruff-final.log`。
- `browser-resume-kernel-precision-final.log`、`browser-resume-precision-scoped-fifth.log`、`browser-resume-imported-test-audit-final.log`、`browser-resume-publish-final.log`。
- 实机 `browser-resume-full-surface-first.log`、`browser-resume-default-four-final.log`、`browser-resume-ordinary-query-first.log`。
- 独立搜索 `independent-search-page-first.json`、`independent-search-dom-first.json` 在主证据根。

证据目录是本机原始记录，没有随提交复制整个状态库或用户会话。代码提交不等于用户正在运行的旧服务已经加载新代码；本轮没有操作该服务。已冻结的旧任务权限也不会被新默认值追溯扩大。

本次修复了工具接入和该审批恢复缺陷。仍未解决自然任务中“只搜索不打开”、错误理解工具结果、循环错误动作、虚构来源推荐等可靠性问题；也没有完成真实知乎登录接管与用户可见窗口流程的验收。

# 任务 URL 免抄入口补齐与验证

日期：2026-11-21，Asia/Taipei。

## 根因

用户指出的失败有确定性的接入遗漏，不能只归因于模型抄错地址。

原 CodeMode 的 `tools.browser_navigate({target:"task"})` 已能从冻结请求读取原 URL；错误页面恢复提示也已提供该选择器。但直接 `browser_navigate` 的模型输入合同仍要求 `url`，没有接入免抄绑定。

核对 `.e2e-artifacts/report-tail-natural-repair-first/provider.jsonl`，模型在第 3、5、8、10、12、14 轮均直接提交 `browser_navigate({url:"https://example.com/production_browser_record"})`。实际 HTTP schema 要求 `url`，这些调用没有经过 CodeMode 的 task selector。404 来自错拼地址，不能认作正确目标站点的环境故障。仅提供一条可选提示不能消除这条入口的抄写负担。

任务原文要求先检查项目，需核对记录时才打开来源。原有条件来源延迟预读应保留；不能为了消除 URL 错误就把所有任务改成先浏览。需要让模型选择访问时机，程序负责提供准确地址。

## 修改

1. 新增共用导航 selector 定义与解析，供外层 ToolSpec、CodeMode 目录及原 Host 使用。唯一任务来源已绑定时，直接导航输入为 `{"target":"task"}`，不再接受模型手抄 `url`。真实 URL 仍只来自冻结请求，原有查询参数、编码和 fragment 原样保留。
2. 直接导航降低到既有 CodeMode 调用，再由原 Host 转成物理 `browser_navigate({url:原地址})`；权限、来源检查、worker、回执、完成门继续使用原链路，没有另一套导航执行器或 URL 状态库。
3. 初始提示和错误页面恢复建议使用同一直接 selector 合同。程序生成的导航建议若精确等于绑定的任务原地址，改为 task selector；其他授权地址仍可由原 CodeMode 的 `{url:...}` 分支访问。
4. 新冻结快照版本为 12。旧版本 11 恢复原 URL 输入展示，按原合同执行；不在恢复时改写旧模型请求。修改版本却不匹配原展示文本的载荷继续拒绝。更旧的已退出版本仍要求重新开始请求。
5. 多来源无法唯一绑定时继续使用原 URL 合同；已撤销的导航权限不能因 selector 重新进入目录。错误 target、混合 target/url、空参数及直接手抄 URL 都在派发前拒绝。

这修复了入口遗漏。没有对任意手写 JavaScript 中的 URL 做猜测性纠错；CodeMode 仍可访问其他授权页面，因此任意 raw URL 不能无条件替换成任务原地址。唯一来源 selector 也不等于任意多来源任务都已经免抄。

## 原减负机制的核对

| 机制 | 本轮实际证据 |
| --- | --- |
| 任务 URL 选择器 | 修复型实机第 3 轮 HTTP schema 仅有 target，模型实际提交 target=task；真实导航回执为原 hyphen URL |
| 语义控件选择 | 两次实机实际使用 role/name 的 textbox、button 候选，原节点复核仍生效 |
| 正式来源读取与复制 | 研究、组合实机在来源字段充分后提供原 edit(path) 绑定；实际编辑只提交 result.json 路径 |
| 版本化文件修改 | 编辑继续由原固定程序完整读取文件，使用外层读取回执及原编辑器；keep 字段保留 |
| 实际检查与完成门 | 首次失败和修复后 exit 0 均有真实 run 回执；没有伪造检查或放宽完成条件 |

免抄 URL 的能力原先存在，但直接入口没有用上。固定复制、语义控件和文件版本保护没有被删除；本轮还通过实际请求和回执确认了它们的使用，而非仅检查函数是否存在。

## 回归与首败

未运行全量 pytest。下列批次有重叠，不相加作为独立覆盖数量。

| 日志（位于 .e2e-artifacts） | 结果 |
| --- | --- |
| task-navigation-unit-first.log | 19 passed，2 deselected |
| task-navigation-wire-first.log | 2 failed：新测试仅导航便声明完成，缺少正式来源读取；补全测试操作，保留生产完成门 |
| task-navigation-core-second.log | 72 passed，5 failed：旧任务类型隔离、派生目录比较及旧快照断言；新 text/native wire 用例已通过 |
| task-navigation-legacy-migration-first.log | 上述迁移专项 5 passed，51 deselected |
| task-navigation-regression-first.log | 343 passed，4 failed；覆盖导航、Host、冻结请求、依赖、研究交付、报告尾部和直接浏览器步骤 |
| task-navigation-legacy-regression-second.log | 178 passed，2 failed；导航恢复及原绑定文件完整通过，首次检查文件尚有旧错误层级断言 |
| task-navigation-initial-navigation-third.log | 该受影响文件完整重跑，84 passed |

扩大回归的两项失败要求恢复提示仍是旧 CodeMode 包装，迁移到实际直接 selector 合同。另两项要求旧回复在读文件前就被“文件菜单”阻断；实际回执表明读文件成功，随后的错误读取回执被原 JSON 编辑器拒绝，未派发 edit，文件原值未变。更新测试，分别检查读取成功、错误回执拒绝、无编辑效果及文件未变，没有改动生产保护。

第二次仍失败的断言把这一工具执行失败当成协议拒绝。按原始记录改为核对 `first_tool_rejection` 和失败父回执；未给实验工具修改统计口径，首次和再次失败日志均保留。原 fixture 的回复摘要继续核对，不改写模型坏回复来通过测试。

实施中曾误将新增测试写入已有同名文件；已从 HEAD 原样恢复原测试，再把新增覆盖放入独立 `test_task_navigation_selector.py`。原绑定文件只保留有依据的合同迁移，原覆盖没有被替换。

## 实机

两次均使用原任务、本地 Gemma4 模型、seed=123456789、temperature=0、14 轮上限及真实 Chromium/生产 kernel；原请求、响应、事件和回执保留。

**修复型自然任务：`.e2e-artifacts/task-navigation-natural-repair-first/`。** 第 3 轮实际请求的 browser_navigate 输入仅有 target，模型提交 `{"target":"task"}`，物理导航准确打开 `https://example.com/production-browser-record`，没有进入 underscore 404。第 10 轮使用 edit(path)，文件为 173/QUARTZ_851，keep 保留，第 11 轮 unittest exit 0，第 14 轮完成门接受。但模型第 3 轮就浏览，第 9 轮才首次运行检查，违反原任务先检查的顺序，`natural_task_closed=false`。**不能计作完整自然任务通过。** 报告还把原 code 值 unknown 写成空字符串，这是模型事实表述错误，URL 修复不能证明此类文字错误已经解决。

**研究与编码组合自然任务：`.e2e-artifacts/task-navigation-natural-composition-first/`。** 8 轮，报告先于编辑；第 6 轮使用 edit(path)，两个字段正确、keep 保留，第 7 轮 unittest exit 0，整体完成被接受，`natural_task_closed=true`。原基础 gate 的 `passed=false` 保留：它还比较固定工具清单，不是该自然任务的原验收判据。没有把该字段改为 true。

因此本轮确认的是：曾失败的直接导航入口已实际免抄，原复制与版本化修改链路继续有效；组合任务通过，修复型任务的前置顺序仍未通过。没有通过反复重跑来隐去失败，也未擅自为自然语言前置步骤生成 shell 命令或强制重排所有任务。

## 静态验证与限制

- 全树 Ruff 通过。
- 全生产树 codey 的 mypy、pyrefly、ty 通过，目标 Python 3.11；mypy 检查 735 个文件。生产源码在这轮检查后未再修改。
- 本轮 11 个 Python 文件三种类型检查通过；整文件 precision 检查无 Any、cast、类型忽略、裸泛型、开放 callback 或未标注函数。没有修改检查配置或新增豁免。
- 扩大到 codey/tests/tools 仍失败：mypy **18,059 errors / 1,266 files**，检查 2,563 文件；pyrefly **21,148 errors**；ty **4,947 diagnostics**。本轮文件诊断为 0，历史测试和工具类型债仍未清零。全生产树及修改文件通过不能称为全仓类型通过。
- 实机是受控页面、真实浏览器和本地模型验证，不证明任意网站、任务措辞或模型都能成功。修复型自然任务顺序、报告事实表述和历史全仓类型错误仍是剩余项。

原始失败、重跑结果、实际 wire 合同、模型动作、物理导航和源码摘要见[验证清单](data/codey-task-navigation-selector-validation-20261121.json)。`.e2e-artifacts` 中的原始记录保留，清单提供路径、文件大小和 SHA256。只提交本轮源文件、相关测试及报告；没有创建或复制 worktree。

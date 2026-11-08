# Chat 真实浏览器搜索与跨任务产物冲突修复

日期：2026-11-08。起点：`4d4533d9`。本报告随实现提交；实机 manifest 的 HEAD 是运行时起点，运行内容包含本批未提交修改。用户授权切换搜索、修复遇到的确定性缺陷、定向测试、KoboldCpp 实机、类型检查、文档与提交。

## 已交付行为

普通 Chat 的 `ChatWebProvider.search` 使用现有 `BrowserSearchProvider(headless=False, isolated=False, http_fallback=False)`。搜索经过真实 Edge 页面及 DOM 提取；不再请求 Bing RSS，也不在浏览器失败后退回 HTTP 搜索。复用现有 Research 浏览器配置、持久 profile 和 CDP 生命周期，默认不抢前台焦点。

查询原样传递，包括中文、空格、加号、`&` 和“附近”。没有增加地点词表、删“附近”或按地点改写查询。搜索浏览器惰性初始化，搜索与 SPA 渲染共享一个 provider；普通静态页面/PDF 仍优先通过既有 HTTP 获取，只有需要渲染的页面升级浏览器，保留原有总时限、取消和获取轨迹。关闭工具会释放浏览器资源。

继续使用原有 gateway、回执、kernel 与任务恢复。显式注入的 provider 仍优先；未增加模型裁判、Chat 调度循环、隐藏重试器或新的授权规则。未修改用户 runtime.json、打包配置或依赖。

## 原问题的根因与证据边界

1. **“东百中心”返回“东”字典**：历史请求中查询包含完整的“东百中心 福州 …”，并未被 Codey 截成一个“东”。RSS 结果本身明显偏题，模型又连续改写查询得到相同结果。真实网页也曾出现偏题，同期 RSS 后来又恢复相关结果；因此不能证明“附近”会确定性触发截断，或浏览器能保证相关性。手工输入和自动查询的时间、查询形态、profile、搜索引擎处理方式均可能不同，当前没有服务端证据确定其中哪一个造成差异。
2. **`stopped after repeated tool results without progress`**：原运行 `run_362aaeacabf34a449979dea4fb674214` 的终止原因是 `no_progress`，重复无进展达到 4/4。它是运行器自动停止，记录不支持用户按了 Stop。切换搜索没有放宽这条保护。
3. **`CommitConflict: artifact handle has conflicting content or ownership`**：页面已经获取成功，保存并登记回执时才失败。旧文本句柄只含任务内计数与内容哈希，例如 `out_0001_3b74f6e4c1ee`；文件目录按任务隔离，但 SQLite 的 artifacts 表以 handle 为全局主键。同一网页在两个任务都是第一次输出时，句柄相同，owner 不同，触发真实冲突。JSON 产物仅按内容生成句柄，同样存在此问题。上层显示 `provider failed` 容易误导为模型/网络故障，实际失败点在产物登记。

新文本与 JSON 句柄加入会话和任务的联合哈希命名空间：`sha256(json.dumps((session_id, run_id)))[:24]`。同一任务内 JSON 去重保持不变；跨任务的相同正文得到不同句柄、相同内容哈希。旧句柄继续按原格式读取，无需改数据库 schema 或迁移已有文件。

历史归属检查显示：RSS 入口来自 `7ec6608a`；全局产物登记和冲突校验分别可追溯到 `d4214119`、`f0c348d7`，产物命名与全局登记范围不一致的问题早于本日最后几次收敛提交。不能将两个现象都归因于最近的统一入口修改。

此前静态评审中的“关闭联网但必需 URL 仍生效”是另一条配置冲突路径；这里已经实际执行搜索与下载，不能解释本例的偏题或句柄冲突。“普通 Chat 引用主要由提示词约束”则与本轮仍输出内部 `[rN]` 的现象一致；本批没有声称解决普通答案引用与语义质量。

## 定向测试与静态检查

最终同一条 pytest 命令：**188 passed**。范围包含本批 provider、SPA 获取、文本/JSON 产物、持久化登记、请求恢复、回执、审批恢复、结果读取及浏览器恢复。因句柄由这些实际消费者共同读取，扩大到对应恢复测试；没有运行完整 pytest、覆盖率或安装包构建。

```powershell
python -m pytest tests/test_chat_web_provider.py tests/test_chat_spa_acquisition.py tests/durable/test_artifact_metadata.py tests/test_managed_outputs.py tests/test_managed_output_metadata_visibility.py tests/test_kernel_receipt_integrity.py tests/test_execution_claims_and_output_recovery.py tests/test_tool_result_read_scope_and_integrity.py tests/durable/test_prepared_provider_request.py tests/durable/test_provider_response_receipts.py tests/durable/test_approval_records.py tests/test_browser_search_recovery.py tests/test_web_execution_without_knowledge_store.py -q
```

新增检查覆盖：默认选择有界真实浏览器且不搜索 RSS、中文/符号/“附近”完整传递、取消/异常传播、共享实例与惰性加载、关闭释放；内存与 SQLite × 文本与 JSON × 同/不同会话的跨任务相同内容登记、SQLite 重开、同任务 JSON 去重，以及旧文本/JSON 句柄读取。

扩展测试初次有一条旧审批断言失败：旧测试要求结果等于裸文本 `hello`，生产早已统一输出结构化 `ExecutionReceipt`。核对恢复契约后，更新测试验证未知状态、退出码与实际输出，保留 legacy payload 无 status 时的未知状态；该测试不需要调度器，构造时明确 `start_scheduler=False`。未把生产回执改回旧文本格式。最终 pytest 退出码 0；RAM 盘对超长路径夹具的清理另有 WinError 3 警告，不是断言或运行时失败。

- 所有本批 Python 文件 Ruff 通过。
- 全生产树 mypy（657 files）、Pyrefly、ty 通过。Pyrefly 保留已有 35 suppressed、133 warnings，未新增豁免。
- provider/产物相关迁移测试及新实机记录器单独 mypy `--follow-imports silent` 通过（4 files）。
- `python -m tools.kernel_type_precision_gate` 通过：受检 kernel 范围 Any 计数为零，契约逃逸检查通过。新增边界与记录器明确标注类型，未引入 Any；不声称历史全仓库已无 Any。

## KoboldCpp 实机：原始两轮搜索

模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`；`http://127.0.0.1:5001/v1`，temperature=0，最多 8 轮，请求超时 600 秒。经过生产 headless/kernel/provider，真实网络与默认有界浏览器，没有注入搜索结果或历史。

[原始两轮记录](../../.e2e-artifacts/chat-browser-20261108/live-conversation/summary.json)：

| 用户原句 | 实际搜索 | 获取与结束 |
|---|---|---|
| 你帮我上网看看福州有什么好吃的 | 福州美食推荐 | 相关美食网页，2 轮 done |
| 东百中心 ？你帮我搜索一下，这里附近有什么好玩的 | 东百中心 附近有什么好玩的 | 相关东百中心网页，2 轮 done |

第二轮保留前轮问题与实际回答，`prior_task_and_answer_in_next_request=true`。本次没有重现字典结果、重复搜索无进展或产物冲突。

**完整搜索→正文→回答验收仍未通过**：两轮均只使用搜索摘要，没有 open_url，最终使用内部 `[rN]` 而没有实际链接。原门禁 `acquisition_ok=false`、进程退出码 1 原样保留；没有把“搜到相关结果”等同于“已阅读并核实答案”。未强制每次普通搜索打开页面，也未修改门禁以获得通过。

## KoboldCpp 实机：同一正文跨任务登记

新增[定向记录器](../../tools/chat_browser_repeated_read_gate.py)，明确要求打开原冲突网页并总结美食。两个生产任务共享隔离 state_home，各有自己的会话，检查指定 URL 的正文实际进入下一次模型请求、文本产物登记、归属及内容哈希。此门禁专门验证获取和产物，要求另行审阅回答质量。

[首次记录](../../.e2e-artifacts/chat-browser-20261108/repeated-live-read/summary.json)：两个任务都 done；首任务第一次请求遭遇 SSL EOF，模型下一轮重新打开成功，并继续 offset=5735 读取。第二任务成功打开相同正文。两个任务的文本产物都是 **28,141 字节**、SHA-256 **`3b74f6e4c1ee77477d2b9bd9f310c430e9b95e125393c28ab1b921128494937e`**，与历史触发冲突的内容相同；句柄不同、owner 正确，无 CommitConflict。

记录器拆出每任务函数并完成 Ruff/类型检查、增加指定 URL 交付校验后，又运行了[最终记录](../../.e2e-artifacts/chat-browser-20261108/repeated-live-read-final/summary.json)：

- `run_6cafa11f4caa4c89bce07d0d631a8be4` 与 `run_0f298caa1f174849815f5c2b376de04e` 均 done，实际正文交付成功。
- `artifact_handles_disjoint=true`，`identical_text_output_registered_twice=true`，产物归属全部正确，定向获取/登记门禁退出码 0。
- **答案质量失败仍保留**：最终两次都把“闽江夜游”列入美食。请求记录显示模型拿到了 offset=0 到 5735 的正文，工具明确提示全文 11135 字符、后续页偏移及可继续读取；模型未继续打开美食章节就结束。获取没有丢失正文或伪造读完事实。现有“指定 URL 已打开”完成条件不能保证模型阅读所有相关章节、正确归类自由文字。

本批结论限定为：默认 Chat 已改用真实浏览器搜索；相同内容跨任务产物冲突已由回归测试和原冲突正文实机验证修复。回答引用、续读选择及语义准确性尚未通过，不能宣称整套联网回答质量已解决。

## 复现命令

以下命令须使用新目录，产物仅含运行记录，未检出另一份源码。浏览器相关性受实时环境影响，不设固定搜索排名断言。

```powershell
python tools/shell_web_original_prompt_gate.py --case conversation --run-dir .e2e-artifacts/chat-browser-recheck/conversation --timeout 600
python tools/chat_browser_repeated_read_gate.py --run-dir .e2e-artifacts/chat-browser-recheck/repeated-read --timeout 600
```

交互浏览器前期对照另保存在 `artifacts/visible-browser-search-check-20261108/summary.json` 及截图。所有实机数据位于忽略目录，未纳入源码提交；没有删除失败记录。

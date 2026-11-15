# 普通 Hybrid 前置导航流程修复

原问题已经定向接住：无 `required_tools` 清单时，明确要求的导航也由运行时在首次请求模型之前执行；预取不再替代这条真实导航回执。历史三条模型回复逐字重放后完成，未替模型改写回复。

## 生产修改与边界

- 在现有 `explicit_web_intent` 中识别 `browser_navigate`、`Navigate to`、`导航到／至`；沿用现有引用、示例、否定和 URL 歧义处理。支持工具名和“打开”同句，以及工具列表另句表达；生产代码没有页面、净重或校验码常量。
- 后续明确的禁止导航会撤销前面的导航标记，支持同句及分句；之后明确重新允许也可恢复。48 组中英文及顺序组合检查均通过。
- `initial_navigation` 复用冻结的任务 URL、授权清单和原执行链，为普通 Chat／Hybrid 安排前置导航。唯一 URL 不明确、禁止导航、权限不足、已有浏览器执行、专用输入流程或 strict Research 不会因此新增普通前置导航。
- 初始访问由这条导航拥有，省去同页默认 `open_url` 预取。单独声明的 `open_url` 及专用输入绑定所需的初始正文仍保留。所有结果仍由原工具执行、记账和交付，未把 `open_url` 伪装成 `browser_navigate`。

这次补的是已识别的明确前置动作。没有增加一套工具运行时或修改现有交付验收；任意自然语言、复杂步骤次序和文件字段语义仍需对应合同或模型判断。

## 原始失败轨迹重放

夹具：[三条原回复及 SHA256](../../tests/fixtures/hybrid_omitted_navigation_20261115.json)。原始 provider 日志 SHA256 为 `a3a208d9ff45c25e8b75de7e25e5bfedb28eabd7fc71a2a0409f81cd50ebba06`，生成报告时再次核对一致。修复前新回归中的两组历史回放均复现“文件写对、缺少导航回执”。

最终回放通过生产入口、真实 Chromium 和快照往返恢复：

| 请求 | 结果 | 导航 | open_url | 点击／正式读页／文件读取／编辑 |
| --- | --- | --- | --- | --- |
| 原普通 Hybrid，`required_tools=[]` | 通过 | 1 | 0 | 各 1 次 |
| 另行声明 `open_url` | 通过 | 1 | 1 | 各 1 次 |

两组首次模型请求中均已包含成功导航回执；模型仍使用原来的三条回复，写入 `{"amount":173,"code":"QUARTZ_851"}`。证据：[最终持久化回放目录](../../.e2e-artifacts/navigation-flow-history-complete/)、[最终回归测试](../../tests/test_browser_initial_navigation.py)。较早回放也保留在 `navigation-flow-history-after`。

## 完整 full-surface 实机矩阵

三轮完整矩阵各完成 13 场景，分别为 **11/13、10/13、11/13**。最终轮在完成否定覆盖修复后执行。每轮开始、结束的生产源码摘要一致；各轮分开保存，没有合并通过结果来覆盖失败。

| 场景 | 首轮 | 同句解析修复后 | 最终轮 | 说明 |
| --- | --- | --- | --- | --- |
| basic | 通过 | 通过 | 失败 | 模型服务响应体以 HTTP 头开头，provider 判为 GenerationUnknownError；wire unknown 640 字节、仅一次请求，未重发。 |
| structured | 失败 | 失败 | 通过 | 中间轮失败仍保留；最终轮通过 |
| legacy_structured | 通过 | 通过 | 通过 |  |
| reread | 通过 | 通过 | 通过 |  |
| stale_ref | 失败 | 失败 | 失败 | 模型采用新引用，未触发所要求的 stale-ref 错误；业务完成，覆盖未完成。 |
| hybrid | 通过 | 失败 | 通过 | 中间轮失败仍保留；最终轮通过 |
| stale_edit | 通过 | 通过 | 通过 |  |
| upload | 通过 | 通过 | 通过 |  |
| download | 通过 | 通过 | 通过 |  |
| research | 通过 | 通过 | 通过 |  |
| research_sequence | 通过 | 通过 | 通过 |  |
| source_failure | 通过 | 通过 | 通过 | 来源失败场景按预期 blocked |
| fixed_failure | 通过 | 通过 | 通过 | 来源失败场景按预期 blocked |

普通 Hybrid 三轮首次模型请求都已有成功导航回执，均只导航一次、正式读页一次。中间轮的整体失败来自模型把 `amount` 写成字符串 `"173"`：原始编辑请求和落盘文件一致，生产没有这个字段的类型合同，`done` 未拦截，外部实机验收失败。这项仍归为“需要模型修正或补显式合同”，不能因最终轮通过就记作已接住。

structured 前两轮的错误由生产完成检查拦住，但模型没有补正；stale-ref 未故意提交旧引用，属于覆盖未触发，不能算恢复通过。原请求、模型回复和回执均已检查，未发现这些失败由新的导航调度确定性错误引起，也没有为通过模型输出而放松合同。

最终轮 basic 的第三次上游请求遇到响应体以 HTTP 头开头的异常。provider wire 记录为 `unknown`、640 字节、attempt 1；读取 `_json_object` 的前缀判断代码后确认其检测的是响应体开头，归为模型服务回包环境问题，未重发。完整原始异常响应体没有落盘，保留原请求、字节数和异常诊断。完整矩阵后单独复验 basic：**通过**，目录为 [basic 复验](../../.e2e-artifacts/navigation-flow-live-basic-retry/)，最终矩阵中的首次失败继续保留。

另有一轮诊断运行在发现同句否定覆盖问题后主动中止，原目录及中止原因保留在 `navigation-flow-live-verified/interrupted.json`，不计为完整矩阵，也不算通过。

模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`，temperature `0`。网页受控，浏览器、生产入口、CodeMode、授权和回执链真实运行。目录：[首轮](../../.e2e-artifacts/navigation-flow-live-first/)、[中间完整轮](../../.e2e-artifacts/navigation-flow-live-final/)、[最终完整轮](../../.e2e-artifacts/navigation-flow-live-complete/)。具体失败动作与分类见[机器报告](hybrid-initial-navigation-20261115.json)。

## 回归与静态检查

- 22 个相关回归模块首轮 461 通过、2 失败，失败暴露“browser_navigate 后接 open 动词”解析遗漏，已修复。受影响的入口、URL 绑定、预取和快照恢复组在完整否定覆盖修复后最终 **243 通过**；其余 16 个模块的 **268 项**已在首轮通过，覆盖完成检查、浏览器跟进、复制、上传、JSON 编辑、回执重放和 Research。按模块取最新结果，共 511 项通过；没有运行全量 pytest。
- 单独保存的最终历史重放 **2 通过**；82 项边界快速检查覆盖两种工具协议，不与上述 511 项重复累加。
- 全树 Ruff、mypy `codey --python-version 3.11`（688 文件）、Pyrefly、ty 均通过；Pyrefly 为 0 errors，既有 35 suppressed／158 warnings。新测试模块额外通过三种类型检查，类型精度门也通过；生产修改文件没有显式 Any、cast、type: ignore 或新增抑制。
- 初始失败、解析修复前失败、否定覆盖修复前失败、实机首次与后续轮次日志均保留。快速检查曾因未加边界的 URL 尾随逗号而失败，随后改为 `<URL>` 测试表达，保留 URL 歧义拒绝规则；初始 strict 测试构造问题也已修正。部分成功 pytest 退出时出现 RAM 盘目录清理 WinError 3 警告，测试本身通过，归为测试环境清理问题。

完整日志：[navigation-flow-20261115](../../.e2e-artifacts/navigation-flow-20261115/)。最终生产源码摘要：`3dbbed1688a793fde699b20319e1c150769ab5d5b8c0f3a0dd643b6ce9841b88`。

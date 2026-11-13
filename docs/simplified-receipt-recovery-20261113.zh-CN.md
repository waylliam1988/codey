# 简化入口的失败恢复与当前可见工具

本轮在 `2159b41c` 上修复审查提出的两个恢复问题、冻结合同文本的一致性，以及回执提示与上下文压缩的可见性。沿用原 CodeMode、kernel、浏览器执行器、持久化回执与压缩归档；没有开放新的浏览器入口或 `read_tool_result`，没有创建或复制 worktree。

## 生产行为

- 简化失败提示读取真实子回执，分别说明写入完成、文件版本冲突拒绝、浏览器来源过期和交付不确定。版本冲突允许用原来源重试，固定程序继续先读文件再编辑；写入完成要求停止重复写入。不让模型修补系统生成的 JavaScript。
- 浏览器复制接受失败父调用下已成功提交的完整观察。复用前检查父执行标识、完整子引用集合、唯一性、顺序、子调用归属和原子观察完整性。成功观察的引用集合不完整、归属错误或正文截断时明确停止，不能静默重新获取可能变化的来源。后续编辑仍使用原有文档身份、页面数据和文件版本检查。
- 失败父调用的结果上下文交付已验证观察的实际数据，包括 JSON 源正文；不只告知有一条无法补读的子回执。
- 恢复快照时，保存的合同文本必须与冻结的模型定义一致。检查仅对工具签名中的对象字段排列和签名空白做规范化，保留字段与类型、必填性、约束的对应关系；说明、示例和声明仍逐项检查。真实 `sort_keys=True` JSON 落盘往返可以恢复，无关文本和类型变化会被拒绝。继续拒绝旧请求版本，不增加兼容执行分支。
- Research 最终收窄先于正文和初始提示的生成；修复请求也继续复用同一收窄逻辑。通用结果提示和 `read_tool_result` 的续读提示均服从当前可见工具。
- 驱动器从持久化的最终请求快照确定可补读回执集合。原有确定性省略只能使用这个集合，语义压缩也不能删除当前无法补读的、带运行时出处的正文。旧视图若已省略正文，则从现有压缩归档恢复，保留原文本结果跨度或原生调用身份；不重新读取网页。
- 确定性压缩在原归档上增加视图摘要索引，正文仍只保存在原有 `source` 中。恢复可追溯“先省略、后总结”的归档链，不保存第二份正文事实。后台压缩提交前后均核对当前可补读集合，防止可见性改变后提交旧视图。正文仍不能放入预算时，沿用发送前的明确溢出失败。
- 按追加要求，在原 `parse_codemode_args` 边界增加单行注释的执行前诊断。复用已有 Tree-sitter，增加固定版本 JavaScript 语法包（依赖锁由原 uv 命令重新生成，只新增该包）。只有真实 `//` 注释内，字面 `\n`、`\r` 或 `\r\n` 后紧接 `const`、`let`、`var`、`await`、`return`、`throw` 时拒绝，返回行列、修正方向与“没有子动作执行”。不替换源码，不把字符串、模板正文、正则或块注释里的示例当动作；模板表达式中的真实注释仍检查。CodeMode 正常归一化、kernel 和直接 runtime 入口共享此边界，JavaScript 的编译执行仍由原 QuickJS 完成。上传、网页复制的固定调用生成路径未新增或改写。

## 回归与类型

相关测试覆盖真实 Chromium 生产复制、观察成功后父失败、写入成功后父失败、实际文件版本冲突、来源过期、不可变绑定、排序后快照恢复、非法合同、原生及文本结果、历史视图恢复、后台提交竞争和发送前预算拒绝。

首次较大范围回归为 **228 passed / 12 failed**，原记录保留在 `.e2e-artifacts/receipt-recovery-regressions-first.txt`：

- 8 项来自上次已取消的旧请求版本恢复期望。测试现在用当前格式覆盖显式声明的执行事实校验，并单独确认旧版本拒绝；没有恢复旧版本兼容。
- 2 项进程重启回归在开发期间源码变动后触发实现标识不兼容。保留失败，源码冻结后重验。
- 2 项 Research 旧引用修复暴露漏接的修复请求收窄路径，已修复。修复及重启定向复验 **42 passed**，合同与回执复验 **125 passed**。

追加诊断前稳定源码的最终定向回归 **330 passed**（244.51 秒），日志为 `.e2e-artifacts/receipt-recovery-regressions-final.txt`。追加诊断的首批定向回归 **67 passed**，包括整段程序执行前拒绝、修正后沿原执行路径成功、文本 JSON 与原生参数一致、字符串与注释边界。追加诊断后的最终相关回归为 **397 passed**（278.83 秒），日志为 `.e2e-artifacts/receipt-recovery-regressions-preflight.txt`，未运行全量 pytest。测试进程退出时有一条 Chromium cache 文件仍被占用、RAM 盘临时目录清理失败的环境警告，测试结果未失败；没有强制删除占用目录。

Ruff 已通过；追加诊断后 `python -m mypy codey --python-version 3.11` 覆盖 **679 个生产文件**通过。对本轮 **19 个 Python 文件**额外运行 `--disallow-any-expr`，筛选本轮路径后 **0 项诊断**。新增测试首次审计发现两处嵌套字典表达式的隐式 Any，补充 `JsonObject` 类型后归零；首次与复验日志都保留。全依赖树仍有历史诊断，未以关闭检查、`cast` 或 `type: ignore` 掩盖，也不据此宣称全仓库无 Any。

## 实机记录

完整矩阵首轮目录：`.e2e-artifacts/receipt-recovery-full-surface-20261113-first/`。使用本机 `http://127.0.0.1:5001/v1` 的 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，场景脚本沿用原 seed、授权和生产入口。矩阵期间不修改生产代码；各场景保存原始 provider 请求、响应、回执、首次拒绝和汇总。

已确认的首轮 `structured` 失败：原始 JavaScript 把字面 `\\n` 放在单行注释中，吞掉 `newSnapshot` 声明和等待语句；原子回执没有成功的 `browser_wait`。模型之后反复提交完成，并给出不符合输出合同的报告。属于模型输出错误，未改生产合同来接受它。

首轮完成 basic、legacy_structured、reread 共三场通过；structured 和 stale_ref 失败，后者先漏写 return，再错误地对普通元素调用 unwrap，没有触发实际 stale-ref 拒绝。stale_edit 运行中被环境重启打断，余下未开始，根汇总不存在，不能作为完整矩阵。原始 provider 和子回执已检查。

重启后目录 `.e2e-artifacts/receipt-recovery-full-surface-20261113-after-restart/` 中，basic、reread 通过；structured 的原始服务响应体混入 HTTP headers，被已有 `GenerationUnknownError` 拒绝，属于环境问题；legacy_structured 多次生成把 `return` 写成 JSON 的 `\r` 加 `eturn` 的无效源码，原编译器报告语法错误且没有执行子动作，属于模型输出错误。用户要求新增诊断后，在 stale_ref 进行中主动停止此轮并保留所有工件。

追加诊断后的完整矩阵目录：`.e2e-artifacts/receipt-recovery-full-surface-20261113-preflight/`，**7/11 通过**。源码摘要前后一致：`f8f74a2a00f362c2514f0c95355edf039f5fbabb50e3abfff7c858e06ddfc6b1`。

| 场景 | 本轮完整矩阵 | 原始请求与回执核查 |
| --- | --- | --- |
| basic / Chat | 通过 | 导航、填写、点击、等待、读回均有成功回执。 |
| structured / Chat | 通过 | 包括成功等待、token 和标签页 URL；输出 schema 和字段校验通过。此次直接生成合法代码，没有触发新增诊断，不能单据此归因诊断提升。 |
| legacy_structured / Chat | 失败，max_turns | 多次将 `return` 错写成 JSON `\r` 加 `eturn`，原编译器在副作用前拒绝。后续完成交互和读取，但最终回答仍不符合原输出合同。模型错误。 |
| reread / Chat | 失败，no_progress | 成功恢复 6 个快照。模型对第二次快照结果直接访问 `.elements`，漏解包 Result，明确提示后仍重复相同程序。模型错误；尚未完成重复读取全流程。 |
| stale_ref / Chat | 通过 | 首次程序第 24 行含字面 `\nconst`，新诊断在任何子动作前拒绝；模型修正后执行，真实 stale-ref 拒绝 1 次，刷新引用并完成。 |
| stale_edit / Hybrid | 严格场景失败，任务 done | 文件最终为正确值。首次 JSON 编辑把解包后的 file 当 read，被原合同拒绝；随后重新读文件成功写入，绕过了预定注入冲突，实际 stale-edit 冲突 0 次，不能计为冲突恢复通过。 |
| upload / Chat | 失败，no_progress | 固定程序成功观察、选择上传附件及读回，文件名、正文标记与 hash 正确。模型反复省略 read 动作的 `upload.wait_for_text`，缺少要求的成功 browser_wait，已有完成守卫拒绝。模型错误；没有自动补做等待或放宽合同。 |
| download / Chat | 通过 | 原下载链与落盘回执通过。 |
| research / strict Research | 通过 | 交互、等待、读回和 submit_research_answer 完成。 |
| source_failure / Chat | 通过，预期 blocked | 来源失败按原合同阻断。 |
| fixed_failure / Chat | 通过，预期 blocked | 固定来源失败保留尝试回执，按原合同阻断。 |

这 11 场均运行至结束。四场失败的原始 `provider.jsonl`、`receipts.json`、`events.jsonl` 和 `summary.json` 保留，未在矩阵中修改源码。后续单场复验单独记录，不覆盖本轮 7/11 结论。

四场各复验一次，**3/4 通过**，仍使用相同生产代码、授权、原场景与原 seed。各自根 `summary.json` 的源码摘要前后均与上述值相同：

| 单场目录后缀（均位于 `.e2e-artifacts/receipt-recovery-full-surface-20261113-` 下） | 结果 | 核查 |
| --- | --- | --- |
| `reread-retry/` | 通过 | 恢复 5 个快照，实际两次读取正文，完成交互、等待和输出。 |
| `stale-edit-retry/` | 严格场景仍失败 | 再次把解包后的 file 传给 JSON edit，被原合同拒绝；随后重新读到 `externally_changed` 版本并正确写入。真实 stale 冲突仍为 0，不计作冲突恢复覆盖。 |
| `upload-retry/` | 通过 | 使用简化 read 动作的 wait_for_text，固定程序产生成功 browser_wait 和 browser_read，再获准完成。确认的仍是附件选择和页面读回，未将其宣称为服务端接收成功。 |
| `legacy-structured-retry/` | 通过 | 保留一次 missing_return 的失败父调用与已提交导航子回执，后续继续完成所有必需动作和既有输出合同。 |

真实模型的新诊断恢复证据在完整矩阵 `stale_ref/summary.json` 的 `first_rejection`，可追溯到原始 proposal、request、decision 工件；随后 `receipts.json` 包含真实 stale-ref 拒绝及恢复的原子回执。首轮 structured 的原始失败程序也单独用新边界复验，记录在 `.e2e-artifacts/receipt-recovery-original-source-preflight.json`，定位第 16 行第 46 列。两者分别证明原问题可被拦截，以及实机模型有一次依据诊断修正后成功继续的记录，不代表每次都能恢复。

## 边界

这是恢复和交付边界的修复，没有把一般 CodeMode 任务全部改成简化调用，也不能消除 12B 的一般 JavaScript、引用选择或回答格式错误。旧待执行请求仍拒绝恢复；缺失所需原始观察或归档索引时，明确停止而不猜测正文。真实模型实验与确定性生产回归分别报告。

新增注释诊断是一条明确且有限的输入规则，不是完整 JavaScript 静态分析器：未覆盖的吞语句形式仍由原编译器、运行期回执和完成守卫处理。需要展示相同模式的注释示例可使用块注释；输入数据保持原样。全矩阵与单场复验均已结束，但 Hybrid 的真实模型冲突恢复仍存在验收缺口；相关确定性 Chromium 生产回归中的 `changed_file` 已触发一次真实冲突并验证同来源、重新读文件后的恢复，不以它替代未通过的模型场景。

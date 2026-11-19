# 小模型工具减负：实施与验证

日期：2026-11-19。基于 `5d59e41b`，直接修改当前工作目录，没有创建或复制 worktree。

## 最终实现

1. **按名称替换 Python 局部区域**：`edit` 使用 `path`、`base_result_ref`、`region`、`content`；例如 `region="Store.place_order"`。模型只提供新函数，程序从原读取版本解析函数/类及装饰器的边界，保留其余文本。读取展示与执行使用同一个 AST 解析器，没有区域注册表或第二份文件存储。
2. **显式补整块缩进**：`relative_indent=true` 只统一外层缩进，不猜内部层级。替换内容中的 Tab、多行字符串以及调整后的无效 Python 都会拒绝；重名/未知区域、过期/不完整/跨文件读取也会拒绝。旧函数体内的字符串不会妨碍整函数替换，因为程序并不重缩进旧正文。仍通过原版本检查、权限、字段绑定校验、原子写入和变更跟踪；没有新增写入器。普通精确替换和整文件更新仍按其原合同执行。
3. **减少结果与恢复负担**：`task_output` 的正式结果保留退出码和执行回执引用，采集输出只展示一份；没有采集输出时保留错误正文。需要继续读取时直接给出各流游标和下一次调用。已观察终态的后台任务不再一直提示“已接纳，继续查询”。
4. **具体失败指引与证据展示**：编辑失败给出有效的重读调用；相同命令在当前文件上失败时，提供其原正式回执的读取调用；原输出包含 `FAILED ` 时，直接提供该字面量查询。现有声明要求与验证证据继续从原策略/回执派生，不另建任务清单。针对某个测试的失败即使不覆盖推荐全套命令，也会提示先检查失败，而不是立即推荐未修改重跑。
5. **修复后台进度判断**：新任务编号本身不再算新信息。只规范已知接纳/控制回执中的运行编号、采集版本等协议位置；输出正文、退出码、状态、游标、缺失范围仍参与比较，正文中碰巧出现的编号字符串保留。此比较仅用于进度提醒/预算，不改变正式回执、执行、恢复或完成证据。
6. 在原 `runtime.json` 中登记新增生产模块。没有兼容旧界面字段、双写或第二套运行时。

示例：先完整读取 `checkout.py`，然后使用该次真实 `result_ref`：

```json
{
  "tool": "edit",
  "args": {
    "path": "checkout.py",
    "base_result_ref": "<该次完整读取的真实 result_ref>",
    "region": "Store.place_order",
    "relative_indent": true,
    "content": "def place_order(self, request_key, sku, quantity):\n    ...\n"
  }
}
```

内部先用该读取的完整源码算出范围、拼出新文件，再交给原写入路径校验当前版本。外部文件变化后，旧读取不能授权修改。示例中的省略号只说明形状，实际应提交完整的新函数。

## `run` 有界等待的评估

**本轮没有启用默认短等后自动转后台。** `run_in_background=true` 启动后台；省略或 false 仍等待完成。

当前接纳回执 A 与真实执行结果 E 各自只结算一次。先以前台运行、超时再启动子任务会重复执行；先保存 accepted、随后覆盖为 completed 会破坏恢复一致性。若所有短命令也一律返回引用，又会增加模型查询往返。实现真正的短等/长退需要独立验证原子接纳、交付观察及崩溃窗口，本轮不把这个评估扩大成生命周期重写，也没有把提示变化冒充自动接管。

## 自动验证与失败保留

原始日志：`.e2e-artifacts/model-tool-load/checks/`。未运行全量 pytest；下面批次有重叠，不相加。

| 日志 | 结果与说明 |
|---|---|
| `pytest-first.txt` | 首次 14 failed、64 passed；新增测试与新读取提示误写 limit=2000，原上限为 600；两处改为引用正式定义，没有放宽上限 |
| `pytest-second.txt` | 修正后 78 passed |
| `pytest-feedback-first.txt` | 2 failed、55 passed：新失败回执引用匹配误用了仅成功记录保存的身份字段；另有测试运行期间修改实现造成恢复哈希不一致，持久失败原因已核对 |
| `pytest-feedback-second.txt` | 修正展示引用、固定实现后 60 passed，包括跨进程恢复、取消和主任务隔离 |
| `pytest-expanded-first.txt` | 扩大相关回归 334 passed、79 subtests passed |
| `pytest-final.txt` | 164 passed、79 subtests passed |
| `pytest-progress-before-fix.txt` | 2 failed、1 passed，复现编号变化被当作进展的生产问题 |
| `pytest-progress-after-fix.txt` | 34 passed；退出时有既有 RAM 盘清理 WinError 3 警告 |
| `pytest-final-02.txt` | 227 passed、79 subtests passed；同类 RAM 盘退出清理警告 |
| `pytest-symbol-region-first.txt` | 简化成名称选择后 57 passed、79 subtests passed |
| `pytest-symbol-final.txt` | 最终名称接口及后台/恢复/合同/完成展示回归 148 passed、79 subtests passed |
| `pytest-region-final.txt` | 最后简化名称区域缩进、覆盖旧多行字符串的有效替换，编辑/CodeMode 回归 48 passed |
| `pytest-recovery-final.txt` | 最终实现固定后再次检查四种跨进程崩溃窗口，4 passed、29 deselected |
| `pytest-packaging.txt` | 运行包登记与打包回归 13 passed |
| `ruff-final-05.txt` | 全树 Ruff 通过 |
| `mypy-final-05.txt` | 全生产树及本轮 5 个测试文件通过，688 文件，目标 Python 3.11 |
| `pyrefly-final-05.txt` | 同范围 0 errors；工具报告原有 35 suppressed、162 warnings，没有新增豁免 |
| `ty-final-05.txt` | 同范围通过，目标 Python 3.11 |
| `precision-final-02.txt` | 原类型精度门禁通过 |
| `changed-type-precision-final.txt` | 本轮 15 个 Python 文件扫描通过，无 Any、未注解函数、裸泛型、cast 或检查抑制 |

`pyrefly-second.txt` / `ty-second.txt` 保留旧测试替身参数名与正式 Protocol 不符的失败；修正测试替身的名字与显式类型，没有改生产接口迁就它们。修改文件另用 AST 扫描检查动态类型、未注解函数、裸泛型、cast 和检查抑制。

最初的局部区域试验使用行号。实机模型两次编造超大行号且遗漏结束行，校验正确拒绝；最终接口改为从函数/类名称机械解析范围，没有放宽无效参数。早期行号方案及其测试日志保留用于解释实验过程，不是最终接口或兼容路径。

## 实机记录

复用 `tools/tool_task_live_gate.py`，真实 headless、Gemma4 12B、KoboldCpp、进程与 durable 回执。相同复杂 SQLite/HTTP/并发/取消任务、同一 seed、同一模型配置；没有预编排模型动作。目录名 native 是请求模式，此模型实际沿原 safe_native_tools 使用 JSON Schema 文本协议。

原始请求、回复、事件、正式回执、独立 pytest 和不可修改文件检查均在 `.e2e-artifacts/model-tool-load/live/`。上轮对照为 `.e2e-artifacts/tool-task-lifecycle/live/integration-native-a1da99c5`。

| 本轮目录 | 结果 |
|---|---|
| `integration-native-c60c9d92` | 自然 no_progress，未完成修复。模型读到了函数范围与完整原文，仍漏抄事务语句导致精确替换失败，又提交 offset=1100 越界读取；工具参数与原始回复一致。独立检查 7 failed、3 passed |
| `integration-native-d2f52f27` | 编辑首例调整后仍提交冲突参数、错误旧文本并重复运行。为修复已暴露的编号进度问题，主动只取消此诊断和其 genkey；stop_reason=stopped，不算通过。`operator-stop.json` 保留原因 |
| `integration-native-93dff5ff` | 行号方案下，模型提交超大行号且缺少结束行，随后改用已有整文件更新，漏掉导入与事务，回归失败。诊断到根因后只停止此任务及其 genkey；不算局部替换成功或复杂修复通过 |
| `integration-native-ca71ac77` | 最终名称接口，自然 stop_reason=protocol。模型两次选对 `Store.place_order`，但同时传入 `replacements` 且没有 `content`，被正确拒绝；没有成功编辑。独立检查 7 failed、3 passed |

对照数据保存于 `.e2e-artifacts/model-tool-load/comparison.json`。四次本轮任务与上轮任务的文本 SHA-256 均为 `da1596eb2b3fcb8e7069a55dd42805856720507e966a0eb3dcdfa7a659d12d51`。首个 `task_output.result` 的 JSON 大小由上轮 4238 字符减至 78 字符，因为不再重复正文；采集输出仍在同一次观察的 output 中。这是展示减负的证据，不是复杂修复能力提升的证据。

**本轮复杂任务未通过，不能宣称比上轮修得更好或已稳定。** 上轮最后独立检查为 5 failed、5 passed；本轮最终未成功修改，仍为 7 failed、3 passed。原始请求确认完整源码、正确区域名称和具体错误指引已经送达，原始回复与收到的参数一致。当前失败属于模型选择/组合参数和编写业务代码的错误，未发现需要放宽生产编辑合同来处理的程序传参错误；也不能凭这几次实验断言只有模型参数量这一个原因。

所有失败与主动停止均保留，不用后一次结果覆盖。结束时后端 idle=1、queue=0；主动停止只针对自己的任务与 genkey，没有全局中止服务。

## 局部收益的量化与边界

进一步核对原始回执、请求及项目最终文件，结果保存在 `.e2e-artifacts/model-tool-load/quantified-comparison.json`。下表只比较上轮 `a1da99c5` 与最终名称接口 `ca71ac77`：

| 指标 | 上轮 | 本轮最终 | 能说明什么 |
|---|---:|---:|---|
| 第一次 `task_output` 整条 `model_text` 字符数 | 8857 | 4844 | 减少 4013 字符，约 45.3%；两次都返回前 4000 字节 stdout，正文没有为减重而缩短 |
| 其中 `result` 子对象 JSON 字符数 | 4238 | 78 | 删除重复输出；不能把此子对象约 98.2% 的缩减说成整条观察或整个提示的缩减 |
| 实际 provider 请求数 | 13 | 7 | 上轮耗尽回合预算，本轮因协议错误提前结束；少请求不等于更高效率 |
| 成功修改次数 | 1（精确替换） | 0 | 新局部编辑尚未获得本模型复杂实机成功证据 |
| 独立检查 | 5 failed、5 passed | 7 failed、3 passed | 本轮任务结果较差，不能宣称总体改善 |

上轮成功替换需要复制 12 行、868 字符旧函数。新区域模式在合同上省掉这段旧正文，让程序定位函数和对齐外层缩进；回归已验证外层 0、4、12 空格均可对齐。但模型这次把 `region` 与 `replacements` 混用且漏掉 `content`，没有真正用成新模式。故机械操作负担的减少可验证，真实模型是否更省回合、更容易修好尚未证实；新提示和编辑选项也增加了上下文与选择，不能声称总体 token 消耗降低，或把所有失败只归因为 12B 参数量。

两轮都是新建同样的坏项目。上轮修改后，负数与零数量两项由失败变为通过，其余五项仍失败；其替换还去掉了事务提交上下文，库存并没有正确落库。本轮最终文件逐字等于原始 fixture，没有成功修改，所以负数与零仍失败，保持七项失败。这里不是新编辑把原来通过的文件改坏，也不是同一份项目从五项失败退化为七项失败。模型内第一次只跑 `test_checkout.py` 为 7 failed、2 passed；独立检查另含 `test_health.py`，所以未修复的独立结果为 7 failed、3 passed，两种统计不混用。

已扫描上轮保存的复杂任务：成功编辑回执均为 `replacements`，没有成功的 `content` 整文件更新。本轮 `93dff5ff` 确实成功调用了既有整文件更新，但独立结果为 9 failed、1 passed，既不是新局部功能成功，也不是修复成功。整文件更新本来就存在，不能计为本轮新增能力。

## 剩余限制

- 局部区域仅支持可解析的 Python 类、函数、方法及嵌套类名称，必须绑定完整、未截断的本任务读取；重名定义不猜目标。其他文本仍使用原精确替换或版本绑定整文件更新。
- 自动补缩进不修复内部层级或业务逻辑，多行字符串和 Tab 需要模型提供精确内容。写入前语法检查只用于补缩进模式；普通精确编辑仍可保留修复中的语法中间态。
- 默认 `run` 自动转后台未实现，仍需显式参数。
- 这批工具改进不能保证小模型遵守指引、保留全部导入或修正事务、并发和重试业务。复杂实机任务须按独立检验结果判定，不能仅凭模型声称完成。

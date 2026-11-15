# Code Mode 交付、repair 与生命周期闭合（2026-11-16）

## 修复范围

核实了所报的四个生产问题，沿用 QuickJS/WASM、CodeModeHost、kernel、CodeModeSession 和原 durable checkpoint，没有新增执行器、工具执行路径或效果账本。

| 问题 | 修复与验证依据 |
| --- | --- |
| repair 只复用 receipt，改变 Promise.race 分支与确定性上下文 | durable repair 只读引用原 execution journal，沿用原 delivery、时间、随机种子与 state 历史；不创建 repair journal。无 durable 的失败父 receipt 保存同一 ExecutionTrace 合同，repair 从原父 receipt 取得轨迹。运行引擎合同和 limits 均参与校验，变化后拒绝。 |
| done 先于 delivery frontier 消费完毕 | driver 保留终态，继续交付已发起调用的 receipt，再结算父执行。SQLite 重启用例使用纯 Promise.race，没有添加 Promise.all 绕过窗口。若终态已出现但某条记录调用根本未发起，明确报 delivery_journal，避免一直等待到 deadline。 |
| follow-up 预留造成 child index 空洞 | 从原 receipt 的 authoritative call identity 解析真实 index，不按 receipt 列表位置重编号。kernel 与 repair 共用 identity decoder；browser ref replay 也按真实 index 查找。保留执行前预算预留。 |
| terminate 后组合动作继续发起 follow-up | session admission guard 传到 host，每个实际子调用都检查 stopping。已经接纳的 click 继续完成结算，之后的 browser_read 不再执行。没有把 session stopping 直接变成丢弃已提交效果的进程 kill。 |

durable schema 升为 3，没有旧格式兼容分支。runtime digest 明确绑定终态交付和逐效果 admission 语义。所有新执行的实际工具仍通过 execute_child_turn → execute_turn；repair 只消费已结算的原 receipt。测试断言原 journal 内容不变、仅有一个原 journal、原子效果没有重复执行。

扩展回归另外发现两类相关诊断问题并修正：终态 frontier 检查不能覆盖原语法错误或更具体的未消费 child 诊断；上传状态纠正提示及 explanation 模式的 receipt 绑定说明不能写死 done.summary 而与当前 done.paragraphs 冻结合同矛盾。后者改成指向“说明”，由既有冻结合同提供字段格式，不另建输出合同。

修改旧 repair 测试的原因是其纯单元夹具使用 one/two/three 作为 call ID，不满足原生产 child identity。改为真实格式，并按要求补齐该测试文件的类型边界；移除对无类型 ProviderActions/SimpleNamespace 的依赖，直接使用原生产 provider request/driver。state 写入负例检查稳定的 repair_rejected 状态，而不是依赖原错误包装的英文文案。

## 首次失败与复跑

全部记录独立保存在 `.e2e-artifacts/codemode-protocol-closure-20261116/`。没有修改或覆盖首次失败日志、历史原始模型回复。

| 批次 | 结果 | 归因与范围 |
| --- | --- | --- |
| reproduction-01 | 5 failed | race 错误分支与取消后新 follow-up 是生产失败。另三项分别是 Windows 路径中冒号、尚未提供 follow-up 的测试快照、手工恢复绕过正式 scheduler 导致重复 effect ID，不能当作生产 bug 证据。 |
| reproduction-02 | 5 failed | 四项生产失败：race 错误分支、原时间/随机数未继承、取消后新 follow-up、正式 SQLite 恢复出现 recorded-control-flow 错误。索引空洞用例仍停在夹具的 follow-up 配置，尚未验证该问题。 |
| reproduction-gap-03 | 1 failed | 修正夹具后，仅临时恢复旧连续索引算法，得到实际 receipts [0, 2]；同序列 repair 明确被拒绝于 step 3。运行后恢复修复代码，未保留旧实现或兼容分支。 |
| focused-01 | 1 failed / 4 passed | 生产修复后，四项通过；索引空洞夹具仍缺正确的 offered browser snapshot。 |
| focused-02 | 5 passed | 修正快照夹具后，五个原聚焦实例均通过。 |
| production-01 | 5 failed / 128 passed | 12 个明确生产回归文件。新增 limits 负例的并发上限不合法；两个 durable 用例直接比较 frozen mapping/tuple 与 JSON dict/list；旧文件动作夹具使用假 call ID；state 写入虽正确拒绝，但断言依赖旧错误文案。原始日志保留，未为这些输入或断言改变生产合同。 |
| focused-03 | 22 passed | 两个 repair/closure 文件完整运行，包含新增 durable repair、历史 state 与 runtime/limits 变化拒绝。之后另补一例未发起 delivery 的终态拒绝。 |
| production-02 | 7 failed / 366 passed | 25 文件扩展批次。两项为本轮新增 frontier 检查覆盖原先具体的 early/syntax 诊断，已修正生产代码。另五项为已有受控输入不匹配当前生产合同：Hybrid 仍用 read 对象，四个结构化上传实例仍用 summary。不是模型随机失败或 Chromium 故障。 |
| production-03 | 120 passed | 修正诊断优先级后，8 文件核心回归完整运行：closure、repair、delivery、session、replay authority、runtime、receipt feedback、kernel integration。 |
| browser-closure-01 | 120 passed | 修正 Hybrid 的 base_result_ref 与结构化上传的 paragraphs 后，browser_repair_closure、browser_structured_upload_production 两文件完整通过。错误类型编辑仍要求独立失败 edit receipt，成功复制仍要求成功 edit receipt；未降低原断言。进程退出时 RAM 临时目录清理出现已有 WinError 3 警告，退出码为 0，原日志保留。此后只补上传纠正提示及对应断言。 |
| upload-final | 3 failed / 31 passed | 上传实际完成，但新断言发现 explanation 模式的 receipt 绑定说明也写死了 done.summary。是另一处生产提示矛盾；未将这次失败归为模型错误。 |
| upload-final-02 | 34 passed | 三个真实 Chromium 上传纠正实例，加上传状态正负例。确认最后请求不再出现 done.summary，且没有重新上传或重新读取。 |
| completion-guidance-final | 1 failed / 53 passed | 额外验证两个输出合同文件时，发现旧 omission 测试还要求保留被明确声明应省略的精确 token，并期待后续拒绝；与既有 exact omission 生产合同不一致。 |
| completion-guidance-final-02 | 54 passed | 修正该受控断言：组装后只移除声明 token，保留业务文字及包含 token 的较长 ID；另把未经组装的原答案交给 gate，继续验证 answer_text_forbidden 负例和原回执复用。未修改 omission 生产行为。 |

初次 Ruff 检查发现未使用导入和新增分支超过现有复杂度门槛；通过收拢 replay 准备逻辑、统一 worker EOF 错误消息与去除重复判断解决，没有增加检查豁免。修改文件类型检查的首次失败与复跑也各自保留，问题包括旧测试的 object/JSON 边界、无类型 callback、search 参数名与现有 Protocol 不匹配，以及需明确标注的 DeliveryReference tuple。没有使用 Any、cast、type ignore 或关闭检查。

`production-02-evidence.json` 从失败任务导出 5 组原 provider JSONL 和 49 条原始 receipt，避免依赖 RAM 临时目录。上传请求明确禁止 summary，且全部 observe/select/read 子效果已成功；失败源于 scripted provider 提交旧字段，其 StopIteration/max-turns 是后果。Hybrid 的失败父 receipt 明确报原完整 read reference 缺失；把旧 read 对象改为当前 base_result_ref 后，才真正到达原测试要验证的 file_field_mismatch。保存的原始回复没有修改。

## 生产回归范围

扩展生产批次明确覆盖以下 25 个文件，不是全量 pytest：

- Code Mode：test_codemode_protocol_closure、test_codemode_delivery_recovery、test_codemode_discovery_session、test_codemode_repair_replay、test_codemode_replay_authority、test_codemode_host、test_codemode_runtime、test_codemode_kernel_integration、test_codemode_json_edit、test_codemode_writes_and_state、test_codemode_durable_scope、test_codemode_production_boundaries、test_codemode_isolation、test_codemode_receipt_feedback、test_codemode_contract。
- 浏览器：test_browser_followups_production、test_browser_full_surface_contract、test_browser_snapshot_recovery、test_browser_readable_references、test_browser_repair_closure、test_browser_file_upload、test_browser_file_download、test_browser_structured_upload_production、test_browser_stale_ref_injection、test_browser_research_direct_steps。

`production-03` 和 `browser-closure-01` 是针对修复影响的独立复跑，不把它们合并为一次“373 项全绿”。补充输出验证另含 test_upload_explanation、test_completion_output、test_explanation_delivery_contract。

## 静态检查

检查串行执行，目标 Python 为 3.11。生产代码最终版本通过下列全树门槛；之后只有测试断言调整，没有再改生产代码。

| 范围 | 最终结果 | 日志 |
| --- | --- | --- |
| Ruff 全树 `python -m ruff check .` | passed | ruff-final-03.log |
| mypy 生产树 `python -m mypy codey --python-version 3.11` | 692 个源文件无问题 | mypy-final-02.log |
| Pyrefly 生产树 `pyrefly check codey --python-version 3.11` | 0 errors；35 suppressed、157 warnings 是保留的既有检查状态，本轮未增加豁免 | pyrefly-final-02.log |
| ty 生产树 `ty check codey --python-version 3.11` | passed | ty-final-02.log |
| 全部 12 个修改 Python 文件：mypy 加 `--disallow-any-expr --disallow-any-explicit --disallow-untyped-defs --follow-imports=silent` | 无问题 | types-focused-final.log |
| 同一 12 个文件：Pyrefly | 0 errors，8 条既有多余 str/bool 转换 warnings | pyrefly-focused-final.log |
| 同一 12 个文件：ty | passed | ty-focused-final.log |

Pyrefly 的 warnings 另用 `--min-severity warn` 展开核对；早期审查列出 10 条，去掉本轮两处多余 str 转换后为上述 8 条，没有为取得绿色关闭规则。静态通过不代表整个仓库没有历史 warning 或动态类型。

## Full-surface 实机矩阵

执行原有生产实机入口，没有修改模型提示、工具脚本或原始回复：

```powershell
python -X utf8 -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/codemode-protocol-closure-20261116/full-surface-first --scenario all --timeout 180
```

模型为本机 KoboldCpp 的 Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M；浏览器为真实 Chromium，页面与附件是受控夹具。首次结果为 **12 passed / 5 failed，共 17 场景，退出码 1**。生产源码前后 SHA256 均为 `5200f303866af7ba6f2f8c8a0056532fd1c2fab8a318d36b0a57a5165a303e35`。

| 场景 | 首次结果与实际覆盖 |
| --- | --- |
| basic、codemode | Chat 与 Code Mode 浏览器流程通过 |
| structured、legacy_structured | 当前 paragraphs 说明输出，以及 browser_eval/tab_list 的结构化回执通过 |
| reread | 通过；恢复 6 个序列化快照，读取 2 次来源 |
| upload | 通过；原始回复依次使用 upload.observe/select/read，选择与读取有独立 receipt |
| download | 通过；首次错误读取 Result.elements 被拒绝，模型修正后完成下载并报告实际路径、字节数和 SHA256 |
| research、research_codemode | strict Research 及其 Code Mode 路径通过；用实际证据引用提交答案 |
| source_failure、fixed_failure | 通过；来源失败按原合同 blocked，没有把失败状态冒充成功 |
| stale_ref_injected | 通过；实际注入 1 次 stale_ref 拒绝后恢复 |
| stale_ref | 未通过；完成业务事实读取，但模型直接使用填写后新 ref，跳过了任务要求的旧 ref 负例；failed_stale_refs=0 |
| hybrid | 未通过，no_progress；模型交替请求同一 receipt 的 value/output，收到完整正文后仍重复读取，没有 read_file 或 edit |
| hybrid_codemode | 未通过，no_progress；首次 browser_wait 缺必填 timeout_ms，后续复用填写前失效 ref、忽略失败结果并多次提交含错误转义的源码，没有进入文件编辑 |
| stale_edit | 未通过，no_progress；模型反复读取导航 receipt，没有进入文件读取、编辑或版本冲突注入 |
| research_sequence | 未通过，max_turns；模型三次重复导航、snapshot、填写、click purpose=action，没有继续 browser_read 或提交答案 |

逐项检查了 5 个失败场景的 `request.json`、`task.txt`、`provider.jsonl` 与 `receipts.json`。Hybrid 首次请求包含 tools.edit 与 base_result_ref 模板；错误 value 读取的 receipt 明确列出可用 output 视图及完整调用例子，成功 output 回执有完整正文和 next_offset=null。stale_edit 重读的是导航结果，不能作为文件读取或版本恢复的证据。research_sequence 的点击后原始请求明确要求继续等待、browser_read 和带引用提交，模型却重新导航。上述首次失败的证据指向模型执行错误，没有据此放宽生产合同，也没有发现 Chromium 或本地 API 故障。

对这 5 个场景的独立复跑使用 `full-surface-retry-<scenario>/` 和对应 `.log`，保留原始请求、回复、事件与回执；首次矩阵结果不会被复跑覆盖。

| 独立复跑 | 结果 | 原始请求与回执核查 |
| --- | --- | --- |
| stale_ref | failed，done，退出码 1 | 再次直接使用最新 ref 完成业务读取，仍没有执行预期的旧 ref 负例 |
| hybrid | failed，no_progress，退出码 1 | 再次交替读取同一 receipt 的 value/output，没有进入文件编辑 |
| hybrid_codemode | failed，no_progress，退出码 1 | 首次未 unwrap snapshot；修正后完成浏览器流程并到达 read_file。随后连续三次 unwrap(read) 后从文件数据取 result_ref，向 edit 传入 undefined，被参数合同明确拒绝，没有发生写入 |
| stale_edit | failed，no_progress，退出码 1 | 再次重复读取导航 receipt，没有执行 read_file/edit，没有注入文件版本冲突 |
| research_sequence | failed，max_turns，退出码 1 | 重复导航或观察、填写、点击；第 12 轮才发起 browser_wait，没有读取正文或提交答案 |

独立复跑合计 **0 passed / 5 failed**，不与首次 17 场景合并为一个成功率。各复跑生产源码 hash 与首次相同。`full-surface-audit.json` 记录两批失败场景的路径、结果、源码 hash，以及原始请求、任务文字、provider JSONL、receipts、events 和 summary 的 SHA256，未复制或改写原始模型回复。

## 剩余限制与提交范围

- 没有运行全量 pytest；扩展批次、聚焦复跑和实机矩阵各有明确范围，不把不同范围的成功合成一次全绿。
- 本机 Gemma 的 full-surface 矩阵未全绿。Hybrid 成功编辑与 stale-edit 文件版本冲突恢复，在首次及独立复跑的模型实机路径均未完成；相关受控 Chromium 生产回归通过，不能替代上述模型覆盖。浏览器 stale-ref 拒绝与恢复由注入场景实际完成。
- 4 个所报 bug 均有生产路径回归；沿用原 QuickJS/WASM、kernel、worker driver 和 durable receipts。repair 不新增 journal 或执行副作用，终态只排空已发起调用的结果，取消按每个实际子调用停止接纳并等待已接纳调用结算。
- 按冷启动要求使用 schema 3，没有兼容旧 journal，也没有持久化 JavaScript heap。
- 提交只包含本轮 12 个 Python 文件和本报告。工作区已有的 `dist-sdist-check/codey-0.5.11.tar.gz` 删除不纳入提交。

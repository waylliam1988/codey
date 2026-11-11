# B4 回放结果交付修补与实机复测

本轮承接[第一轮分配置实机](protocol-surface-isolated-live-20261111.zh-CN.md)。先修补 B4 自动恢复丢弃子调用结果的问题；首批复测因连续服务异常被用户要求停止，随后修正实验传输、诊断与中断处理后重新运行。生产代码、生产提示模板、模型设置、B1–B3 候选机制和验收条件均未修改。用户要求完成修改、复测、更新文档后提交；若有生产代码修复须另行提交。本轮定位到的接线缺陷在实验工具，尚未发现需修改的生产代码。

上一轮 B4 为 A 4/6、B 4/6，常规各 2/4；两次 page_reset/B 虽实际完成两轮操作，但最终均失败。其中第二轮的原始 browser_read 回执有真实正文，恢复程序却只返回 `{receipts_replayed:N}`，最后模型请求没有真实校验码。

## 修补内容与边界

结果交付修补首先标为 `protocol-surface-isolated-v2`；传输与中断修补后为 `protocol-surface-isolated-v3`。交付修改位于 `tools/protocol_surface.py`：新增 B4 专用 `recovery_action`，继续调用原 `mechanical_surface.repair_action` 选择合格恢复；历史机械实验文件不变。

- 仅恢复生产已签发 repair_of、失败为 missing_return/result_encoding、原子调用全部成功、无 edit、尚未尝试且尚未修复的父调用。
- 回放继续由生产内核核对原调用顺序、参数及回执身份。结果必须来自实际 unwrap 的回放值，不从模型叙述或猜测重建正文。
- 返回 `receipts_replayed` 及 `replayed[{tool,status,receipt_ref,data,truncated,data_omitted,omission_reason}]`。字符串投影为 `data.text`，结构化结果保持原值；不会重建原 JavaScript 的语义计算。
- 全部 data 的 JSON 序列化长度合计上限为 12000（按 JS 字符串 length 计）。超限的单个 data 整体省略，标记 `data_omitted=true`、`truncated=true` 及 `recovery_delivery_limit`，仍消费全部原调用序列；不静默裁剪成貌似完整的正文。
- 保留原回执、来源和结构化结果的截断/部分读取标记。输出明确说明来源文本为不可信数据、历史结果不证明当前页面状态、父调用修复不等于任务成功。

本轮没有新增自动重试、修改 wait 默认值、修复 Result 使用方式、重写 URL、放宽完成门或给答案自动填入业务数值。超过交付限额或上下文压缩仍可能影响模型实际得到的内容，因此另审计真实 HTTP 请求。

## 确定性验证

先新增实际 HTTP 交付回归测试，旧实现下复现 **1 项失败**：恢复结果没有 `replayed`，下一次模型请求拿不到原始正文。修补后同一测试通过。

测试让真实 Chromium 执行两轮导航、填写、点击、等待和读取，随后故意返回 undefined 字段；自动恢复后，脚本化模型只能从下一次实际 HTTP 请求的恢复结果中取答案。页面标题带随机 marker，断言两份正文、真实校验码和来源回执均已交付，导航/填写/点击各仍只有两次，防止重做动作或写死答案冒充恢复。

新增测试还覆盖超限数据整体省略、后续较小结果继续交付、原始来源截断保留，以及运行时错误、失败/缺失子回执、edit、已尝试、已修复时继续拒绝自动恢复。

v2 正式诊断 **72 项通过**，记录于 `.e2e-artifacts/protocol-b4-delivery-diagnostics-20261111-01/`。v3 增加可选性能 GET 失败不影响成功生成、provider/worker 异常后矩阵立即中断的回归，并在 A/B 实际请求中断言 stream=true，正式诊断 **75 项通过**，记录于 `protocol-b4-delivery-diagnostics-20261111-02/`。

共享实验、fixture、既有 Local 流式接线、响应不确定性与半帧 deadline 回归另 **50 项通过**。Ruff 检查四个变更 Python 文件通过；strict mypy（explicit-package-bases、follow-imports=silent）检查四个实验工具通过。pytest 退出码均为 0，仍有原有 RAM-disk 清理告警，不将其报告成测试失败或已修复。

## 中断批次与传输定位

v2 首批目录 `.e2e-artifacts/protocol-b4-delivery-live-20261111-01/` 保留。用户要求停止时完成 5/12 单元，`01-download/B` 尚在执行，已终止本批 runner 及其 worker，未终止模型服务。没有完整 summary，不把这批当作完整 A/B，也不以之后的成功替换其中失败。

四个已完成单元为 provider_failure：page_reset/B、return_failure/A、return_failure/B 为约 90 秒失败；download/A 约 15.39 秒出现 IncompleteRead。另 page_reset/A 提议 done 但只执行一轮，属于验收失败。详细记录见该目录 interruption.json、progress.json、delivery-audit.json。

定位证据保存在 `.e2e-artifacts/protocol-b4-transport-debug-20261111-01/`：

1. 小型独立 HTTP 请求约 0.391 秒返回。将失败 return_failure/B 请求作为独立诊断，以 max_tokens=512 经原始 socket 发送，约 25.047 秒得到完整响应；不计为补跑或评分证据。
2. 安装版 KoboldCpp 1.117.1 在每个服务线程复用 handler 实例；原始 flush_headers 在写失败时不清空头缓冲。从安装源码原样抽出相关方法、注入一次断连写失败，可稳定复现两个 HTTP 状态行。旧长度较小时正文以 HTTP 头开头，较大时 IncompleteRead；新 handler 对照均正常。这证明上游缺陷机制，不证明每一次 90 秒等待都是同一原因。
3. 实验 ExperimentProvider 依赖 ApiProvider 的 stream=False 默认值，偏离当前生产 Local 已保留流式的接线。上游非流式分支不会监控生成期间的连接断开，超时后可能继续占用生成并使后续请求排队。将同一失败请求改成 stream=true 的独立诊断约 14.281 秒完成，首事件约 0.484 秒；原 max_tokens=8192、seed 不变。
4. 另一个实验缺陷是 service_state 未捕获 HTTPException：可选性能 GET 的 IncompleteRead 能让已经成功的生成被上层报告失败。确定性回归验证新实现保留成功响应，将诊断错误写入 service.error；不声称旧 download/A 的异常必然只来自性能 GET。

v3 仅在本协议实验中显式 stream=true，A/B 同时采用；共享 ExperimentProvider 新增可选 stream 参数，默认 False，历史实验默认不变。service_state 增加 HTTPException 边界；矩阵遇 provider_failure 或 worker 异常立即保存 interruption.json 并停止，防止未知生成后继续排队。没有吞掉模型 POST 错误、重发生成、剥掉坏 HTTP 头或延长预算。

实机流式取消诊断在约 2.016 秒以 unknown 退出，收到 37 个事件；一秒后服务 idle=1、queue=0，不需额外 abort。四次只读后续采样中仍有一次旧处理器的 IncompleteRead，被独立记录。重跑前使用仅 GET 的两轮 24 连接核验覆盖安装版的 IPv4/IPv6 服务线程，两轮均正常；没有重启服务或修改其文件与设置。该核验不能称为修复了外部软件，也不能保证其所有未来请求都无缺陷。

## 实机设置与原始证据

最终重跑目录为 `.e2e-artifacts/protocol-b4-delivery-live-20261111-02/`，旧目录不覆盖。模型、地址、seed、温度与上一轮相同：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，`http://127.0.0.1:5001/v1`，seed `123456789`，temperature 0。启动前核对最新诊断的脚本摘要、生产源码不变、服务空闲与 queue 为零。

```powershell
python -u -m tools.protocol_surface_ab --config B4 --run-dir .e2e-artifacts/protocol-b4-delivery-live-20261111-02 --repeats 2 --timeout 90 --budget 300 --max-turns 10 --seed 123456789 --kobold-base-url http://127.0.0.1:5001/v1
```

page_reset、return_failure、download 各两次重复，每个场景同时重跑 A/B，共 12 个单元，串行交替 AB/BA。单请求 90 秒、单元预算 300 秒、最多 10 轮。A 是共享实验 provider/Chromium fixture 下的当前生产路径，不是完整桌面产品成功率；return_failure 首步故障由两侧同一脚本注入，单列诊断。

不补跑失败单元；实机期间候选脚本、测试、任务和评分冻结。原始请求、响应、提案、正式回执、状态库、manifest、冻结脚本与 summary 保留。`audit_b4_delivery.py` 只读审计恢复来源、顺序、原始值及下一次实际请求，不重新评分。

## 结果

v3 完整 **12/12** 单元运行结束，无 provider_failure、worker 异常或可选性能诊断错误。A/B 的业务任务逐对相同，生产源码与实验脚本在运行期间均稳定。退出码 1 来自四个真实任务验收失败，不是服务异常或矩阵中断。

| 指标 | A | B |
| --- | --- | --- |
| 全部成功 | 4/6 | 4/6 |
| 常规成功（排除 return_failure） | 2/4 | 2/4 |
| return_failure 诊断 | 2/2 | 2/2 |
| 模型调用 | 23 | 19 |
| 自动回放 | 0 | 4 |
| 单元执行秒数合计 | 237.735 | 214.546 |
| done 后验收失败 | 2 | 2 |
| 重复上传/下载单元 | 0 | 0 |
| token 完整用量单元 | 0/6 | 0/6 |

流式响应未返回 usage，输入/输出 token 均保留未知，不用性能 GET 估算值替代。执行秒数不是推理时间，样本太小，不据此承诺稳定延迟收益。配对结果为四对均通过、两对均失败，没有仅 A 或仅 B 通过。

### 完整矩阵

单元格末尾为实际模型调用次数，自动回放不算模型调用。

| 重复 / 场景 | A | B |
| --- | --- | --- |
| 01-page_reset | done，验收失败 / 8 | done，验收失败 / 8 |
| 01-return_failure | 通过 / 2 | 通过 / 1 |
| 01-download | 通过 / 4 | 通过 / 3 |
| 02-page_reset | done，验收失败 / 4 | done，验收失败 / 3 |
| 02-return_failure | 通过 / 2 | 通过 / 1 |
| 02-download | 通过 / 3 | 通过 / 3 |

### 交付修补的直接证据

`delivery-audit.json` 审计了全部 42 次实际请求，seed、temperature 和 stream=true 均符合配置。四个 B 自动修复父调用的结果值与原始子回执一致，顺序也完全一致；其中三个修复包含 browser_read，**3/3** 份正文均完整出现在后续实际 HTTP 请求，未触发交付限额省略或原始截断。不是只在本地状态库看到了正文。

两次 return_failure/B 各自动修复一次、只实际下载一次，模型各调用一次即可完成；A 各需两次模型调用手工修复，最终也通过。说明恢复调度可节省模型调用，而本轮没有显示成功率差异。普通 download 第一轮 B 少用一次模型调用，但没有触发自动恢复，不能将这一次差额直接归因于新正文投影。

第二轮 page_reset/B 是自主触发的交付证据：先缺 return，再在读取后返回 undefined，共自动恢复两次；正文随后确实进入请求，最终报告真实净重 173 和校验码 QUARTZ_851，facts_match=true。它没有像 v1 的对应单元那样因正文未交付而填占位符，但仍只真实执行一轮，任务继续失败。

### 剩余失败与解释

四个 page_reset 单元都只成功执行一次 navigate/fill/click，没有完成第二轮。第一轮 A/B 都无失败父调用，已经拿到正文却最终宣称完成；第二轮 A 也无失败父调用，B 的父返回问题已修复，但最终同样宣称两轮完成。四个单元均为 done 后业务验收失败，原记录不改分。

这说明“子动作成功”“父调用修复”“原始正文已交付”“用户要求的两轮操作完成”是四种不同状态。本轮交付修补解决第三项，仍不能靠修复回执数量推断业务轮数。当前 fixture 的正式工具完成要求不能替代两轮顺序验收；两轮要求由离线叶回执顺序检查兜底，因此没有把四个 done 当成功。后续可单独验证将要求的业务阶段与轮数纳入正式状态和完成约束；本轮未加入该机制，也未修改生产完成门。

普通下载仍出现 Result 解包错误；A 第一轮还误取 browser_read.text，但模型手工修复后通过。B 第一轮曾额外导航一次，因此零重复下载不能扩大表述为所有浏览器动作均未重复。原始失败工具记录均在 delivery-audit.json 中。

## 与上一轮的关系及提交范围

v1 和 v3 的 B4 最终均为 A/B 各 4/6。这不能证明 v2/v3 没修好交付，也不能证明有整体成功率收益：本轮已有直接实机证据证明交付缺口被补齐，剩余失败是第二轮动作未完成。v3 同时修正了两侧传输，不能将跨批模型调用或耗时差额全部归因于正文交付。

B 仍未满足全通过、无静默失败等预设组合资格，未运行 B组合；v1 的其他候选结果也不能当作 v3 同版组合证据。没有生产代码修复，因此不创建混入实验的生产修复提交；本轮提交范围为实验候选、共享实验 transport/诊断修补、回归测试和文档。原始失败批与完整新批均保留。

生产摘要始终为 `0686d61cd4fbd923cfb089a89abc0f772a9331366c8152a9c756b49a118ca4b1`，未创建 worktree，未修改用户模型服务安装或持久配置。最终证据为新目录下 manifest.json、summary.json、delivery-audit.json、冻结脚本及各单元 provider/提案/正式回执/状态库。

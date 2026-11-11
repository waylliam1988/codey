# 机械协议分配置实机 A/B：2026-11-11

本轮实机沿用[实验设计](protocol-surface-isolated-ab.zh-CN.md)，分别测量 B1 参数与入场校验、B2 正文绑定、B3 结构化契约与编辑观察边界、B4 窄范围恢复。结果不用于直接比较历史三轮总分；本轮任务删除了旧任务中的解包与 return 教程，接口提示也随配置不同。

原始证据目录：`.e2e-artifacts/protocol-isolated-live-20261111-01/`。48 个实机单元全部完成，27 个失败全部保留；运行中未修改冻结脚本，未重跑失败单元。

本轮有局部收益：B1、B2 分数提高，B4 的指定故障恢复少用模型调用；尚未证明整套方案能稳定提高生产成功率。B3 在属性读取、命名上传上改善，在项目编辑上退步；B4 还暴露了修复后不交付正文的程序缺口。两次重复、固定 seed 和小样本只支持这些描述性结论。

用户随后授权修补 B4 的交付缺口并独立复测，见[B4 v2 修补与复测](protocol-surface-b4-delivery-live-20261111.zh-CN.md)。本文保留 v1 的原始矩阵、故障定位与结论，不用后续成功覆盖旧失败。

## 主结果

| 配置 | 全部单元 A → B | 常规单元 A → B | 仅 B 通过 / 仅 A 通过 | 解释 |
| --- | --- | --- | --- | --- |
| B1 | 1/6 → 4/6 | 1/6 → 4/6 | 3 / 0 | 正向信号；成功记录未直接触发 timeout 默认补齐 |
| B2 | 0/4 → 2/4 | 0/2 → 1/2 | 2 / 0 | 正文绑定有直接采用与成功证据 |
| B3 | 3/8 → 3/8 | 3/8 → 3/8 | 3 / 3 | 浏览器任务改善、编辑任务未通过；含服务异常 |
| B4 | 4/6 → 4/6 | 2/4 → 2/4 | 0 / 0 | 下载及故障恢复均通过，页面重置任务均失败 |

常规统计排除 upload_bound、return_failure 两类脚本注入诊断；每类各有两次重复。四组采用不同候选和场景，不将它们拼成一个可部署 B 版本的成功率。B组合**未运行**：四个独立配置均未满足预先规定的全部通过、无静默失败等组合资格；不是组合配置已经失败。

静默失败：B3/A 1 个，B4/B 1 个。重复副作用：B3/A 1 个单元重复上传；其他组均为零。外部字段丢失均为零，但 B3/B 从未到达文件写入，不据此宣称编辑安全性提升。

## 调用与用量

| 配置 | 模型调用 A / B | 输入 token A / B | 输出 token A / B | 单元执行秒数合计 A / B |
| --- | --- | --- | --- | --- |
| B1 | 54 / 49 | 410807 / 356017 | 2928 / 2754 | 302.515 / 284.764 |
| B2 | 19 / 14 | 172182 / 117380 | 4995 / 4262 | 269.155 / 229.860 |
| B3 | 35 / 27 | 未知 / 未知 | 未知 / 未知 | 552.127 / 371.031 |
| B4 | 24 / 17 | 184478 / 149546 | 4104 / 5035 | 255.798 / 311.984 |

B3 两侧均只有 6/8 单元用量完整，缺失保留未知。执行秒数不是模型推理时间，也不是统一批次总耗时；B3 受超时污染。B4 虽减少模型调用与输入 token，却输出更多 token、执行更慢，不能概括为整体成本或延迟全面改善。

B4 两次 return_failure 中，A 各用 2 次模型调用手工修复，B 各用 1 次模型调用加 1 次机械回放；均只实际下载一次。普通 download 也均通过，B 每次少用 1 次模型调用。B4 总计 7 次机械回放，另 5 次发生于最终仍失败的页面重置任务，不能全算任务收益。

## 完整配对矩阵

单元格末尾为实际模型调用次数；机械回放不计作模型调用。provider_failure 包含已发起但未成功返回的请求，不假设这些请求没有消耗生成资源。

| 配置 | 重复 / 场景 | A | B |
| --- | --- | --- | --- |
| B1 | 01-basic | 通过 / 8 次 | 通过 / 8 次 |
| B1 | 01-snapshot_restore | max_turns / 10 次 | 通过 / 8 次 |
| B1 | 01-stale_ref | max_turns / 10 次 | 通过 / 9 次 |
| B1 | 02-basic | no_progress / 6 次 | 通过 / 8 次 |
| B1 | 02-snapshot_restore | max_turns / 10 次 | no_progress / 6 次 |
| B1 | 02-stale_ref | max_turns / 10 次 | max_turns / 10 次 |
| B2 | 01-upload_bound | no_progress / 5 次 | no_progress / 4 次 |
| B2 | 01-structured | no_progress / 4 次 | 通过 / 3 次 |
| B2 | 02-upload_bound | no_progress / 6 次 | 通过 / 3 次 |
| B2 | 02-structured | no_progress / 4 次 | no_progress / 4 次 |
| B3 | 01-legacy_structured | done，事实验收失败 / 4 次 | 通过 / 2 次 |
| B3 | 01-upload | no_progress，重复上传 / 4 次 | 通过 / 2 次 |
| B3 | 01-hybrid | 通过 / 3 次 | no_progress / 7 次 |
| B3 | 01-stale_edit | 通过 / 6 次 | no_progress / 4 次 |
| B3 | 02-legacy_structured | provider_failure / 6 次 | 通过 / 2 次 |
| B3 | 02-upload | provider_failure / 1 次 | provider_failure / 1 次 |
| B3 | 02-hybrid | 通过 / 5 次 | provider_failure / 4 次 |
| B3 | 02-stale_edit | no_progress / 6 次 | no_progress / 5 次 |
| B4 | 01-page_reset | no_progress / 7 次 | no_progress / 6 次 |
| B4 | 01-return_failure | 通过 / 2 次 | 通过 / 1 次 |
| B4 | 01-download | 通过 / 3 次 | 通过 / 2 次 |
| B4 | 02-page_reset | no_progress / 6 次 | done，事实验收失败 / 4 次 |
| B4 | 02-return_failure | 通过 / 2 次 | 通过 / 1 次 |
| B4 | 02-download | 通过 / 4 次 | 通过 / 3 次 |

## 实验约束

- 使用实际本地模型 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，服务地址 `http://127.0.0.1:5001/v1`；seed 为 `123456789`，temperature 为 0。
- 四组顺序运行，每组 A/B 串行交替 AB/BA，每个场景两次重复。单请求 90 秒、单元预算 300 秒、最多 10 轮。
- A 经过共享实验 provider 和真实 Chromium fixture，执行当前生产工具、内核与完成门；不是完整桌面产品的无干预成功率。
- 两侧业务任务相同，接口契约不同。可选第二模型审查均不可用，没有伪造审查通过。
- 生产代码、生产提示模板、用户模型设置均未修改；未创建 worktree。共享实验 fixture 的可选 hook 与候选脚本在本轮实机期间保持冻结。
- 不把缺失 token 用量当零；不因服务故障替换原结果。组合仅在独立候选满足预先规定的资格时运行。

## 已核实的机制证据

**B1 的分数改善不能直接归因于自动补 timeout。** 成功的 B1 单元中，模型原始程序均显式给出 `timeout_ms=5000`。自动补参和非法参数入场拒绝已有确定性诊断，但这批自主成功记录没有直接触发默认值补齐。生产侧确实出现 `browser_wait({})`、`condition:"fish ball appears"` 等错误，随后被“恢复未消费控制流”掩盖；候选仍可能因错误 ref、真实等待超时而失败。

**B2 有一次直接的正文绑定与结果组装成功。** `B2/02-upload_bound/B` 在一次真实上传后修复 Result 访问，成功提交父调用，程序从原始正文与上传回执绑定最终 JSON；模型自由叙述遗漏 SHA256，最终 JSON 仍有实际 hash。A 两次 upload_bound 均曾实际上传、读取，但正文绑定缺失后首先得到答案格式修复提示，模型继续修答案也无法建立事实来源。

这不代表 B2 可以修复所有上游错误。`B2/01-upload_bound/B` 仍把 browser_read 字符串当对象、从错误对象取 path，父调用一直失败，未走到新绑定器；`B2/02-structured/B` 因 Result 解包与页面状态错误失败。`effect_verified=false` 的上传记录只证明控件选择及观察，不能证明远端服务器接受了上传。

**B3 的属性和控件接口确实被采用，但编辑接口尚未得到自主验证。** 两次 legacy_structured/B 都实际使用 `browser_property(name="__contractToken")` 并报告真实 token；第一轮 upload/B 使用 `target_name:"Attachment"`，只上传一次且事实完整。对照 upload/A 发生重复上传；legacy_structured/A 第一轮虽然 done，但答案仍是占位符，属于静默事实失败。

B3 的 hybrid/B、stale_edit/B 浏览器计划成功后，转入反复 `read_tool_result`，甚至对不提供 canonical value 的叶回执请求 `view="value"`。第一轮 hybrid 的第二次 HTTP 请求已经包含完整 `completed[].data.text`、真实净重与校验码、`read_files:[]` 和未修改文件状态，模型仍继续读取已交付的回执，没有调用 read_file。这里不能简单归因为“程序没有把正文交给模型”。

两次 stale_edit/B 均没有真实读取项目文件、没有注入外部修改、没有触发冲突。零字段丢失仅因未写入，不能证明观察边界改善了字段保留；对照第一轮 stale_edit/A 则实际经历一次旧凭证冲突、重新读取并保留外部字段后通过。

**B4 暴露了“修复成功”与“结果交付成功”之间的程序缺口。** 自动恢复调用复用的 `mechanical_surface.repair_action` 回放了已成功的子调用，但只返回 `{receipts_replayed:N}`，丢弃 unwrap 得到的实际数据；`recovery_state` 主要提供上传/下载事实与最近页面转换，也不补交 browser_read 正文。

在 `B4/02-page_reset/B` 中，三个原始成功 browser_read 回执均含真实校验码 `QUARTZ_851`；三个修复父调用却分别只返回 1、6、14 的计数，最终模型请求中完全没有这个校验码。模型随后 done，实际净重和校验码仍为占位符，事实验收失败。证据见 `recovery-body-delivery-audit.json`。这不是要求模型“努力理解”即可弥补的完整交付路径：已有数据可通过程序直接投影给模型，而当前实现没有做。

两次 page_reset/B 都实际完成了两轮页面操作，但任务仍失败。第一轮还有错误 URL `production-browser_record` 导致的 404，以及字符串 `.text` 误用。状态恢复不能只给动作历史，还需要区分已完成动作、已交付观察和未满足要求；目前候选没有通用任务状态机。

## 服务异常的处理

B3 第二轮出现四个 provider_failure：legacy_structured/A 第六次请求约 90 秒取消；upload/A、upload/B 首请求均约 90 秒失败；hybrid/B 第四次请求约 3 秒报告“HTTP headers inside the JSON response body”。最后一种是传输响应损坏，不能归因于业务协议算法。超时可能受生成长度、服务队列等共同影响，也不能全部断言为与接口无关的外因。

没有重启、修改或中止全局模型服务，没有补跑上述单元。主结果保留失败；另提供排除受影响**整对** A/B 的描述性敏感性结果，避免只删除某一侧失败。脚本中的 comparable 标志表示任务、源码等受控条件一致，不代表服务在每个请求上均健康。

排除 B3 的 `02-legacy_structured`、`02-upload`、`02-hybrid` 三整对后，剩余五对仍为 **A 2/5、B 2/5**，仅 A、仅 B 通过各两对，一对均失败。模型调用为 A 23、B 20。B1、B2、B4 没有 provider_failure，无需排除。这个敏感性结果同样没有显示 B3 净成功率收益，也不能用于估计服务健康时的反事实结果。

## 失败定位索引

以下索引覆盖全部 27 个失败单元；详细程序、参数、回执和完成反馈在 `original-artifact-audit.json` 及各单元目录，保留第一次拒绝和后续失败。

| 配置 | 单元（重复 / 场景 / 侧） | 主要阻断 |
| --- | --- | --- |
| B1 | 01、02 / snapshot_restore / A | 无有效 wait；缺参数或非法 condition，参数问题被恢复控制流错误遮蔽 |
| B1 | 01 / stale_ref / A | 真实旧 ref 拒绝后曾恢复点击，但 wait 仍缺有效参数 |
| B1 | 02 / stale_ref / A | document 不可用、旧 ref 与无有效 wait，最后 max_turns |
| B1 | 02 / basic / A；02 / snapshot_restore / B | 把 Locality、Load record 名称当 raw ref，反复失败 |
| B1 | 02 / stale_ref / B | ref 错误后还有实际 wait 超时，未完成有效流程 |
| B2 | 01 / structured / A | 对不存在的 browser_read.text 调用 match，重复运行时错误 |
| B2 | 02 / structured / A、B | 未解包 Result；B 另在没有 retained page 时 snapshot，未恢复 |
| B2 | 01、02 / upload_bound / A | 实际上传、读取后缺原始正文绑定；反馈先要求修答案格式；02 手工修复父调用仍未补齐绑定 |
| B2 | 01 / upload_bound / B | 错误访问 filename/path、字符串 .text；父调用未成功，未到达绑定器 |
| B3 | 01 / legacy_structured / A | .text 与 token 等返回访问错误，修复序列也失败；done 用占位符 |
| B3 | 01 / upload / A | 未定义函数、缺 return、控制流恢复失败，并重复上传 |
| B3 | 01 / hybrid / B；01、02 / stale_edit / B | 浏览器计划后反复读取回执，没有 read_file，更没有 edit |
| B3 | 02 / stale_edit / A | 未定义函数、Result 解包错误，未读到项目文件 |
| B3 | 02 / legacy_structured / A；02 / upload / A、B | 约 90 秒 provider 失败；前者在失败前已有 JS/解包错误，不能只归为服务因素 |
| B3 | 02 / hybrid / B | 回执读取循环后，第四请求出现服务响应损坏 |
| B4 | 01 / page_reset / A | 只完成首轮操作；缺 return 后 done 使用虚构 knowledge_refs，反复完成拒绝 |
| B4 | 02 / page_reset / A | 没有有效 browser_wait，也未完成第二轮操作，反复提议 done |
| B4 | 01 / page_reset / B | 两轮操作及部分父恢复完成；错误 URL、字符串 .text、重复无进展 |
| B4 | 02 / page_reset / B | 两轮操作和三次父恢复完成；修复只返计数，未交付正文，done 用占位符 |

## 证据完整性与开发验证

批次监督进程正常结束，四组矩阵均完整。各组及整批的 `source_stable`、`harnesses_stable` 均为 true；生产摘要始终为 `0686d61cd4fbd923cfb089a89abc0f772a9331366c8152a9c756b49a118ca4b1`。原始 HTTP 请求共 239 次，逐条核对 seed、temperature，没有偏离。四组退出码均为 1，表示矩阵内存在失败；监督器没有把它误当成矩阵缺失或自动重试理由。

主要汇总文件：`batch-summary.json`、各组 `summary.json`、`original-artifact-audit.json`、`descriptive-analysis.json`、`matrix.md`、`recovery-body-delivery-audit.json`。原始 provider、proposals、协议事件和 durable 状态库均保留。后处理只读取原结果，不改变评分。

实机前已有新诊断 64 项、共享旧 fixture 回归 8 项通过，Ruff 和限定范围 strict mypy 通过，详见实验设计文档。本轮运行期间未改实现，结束后仅更新报告；没有为重复证明同一实现而重跑这些测试，也未提交或部署生产修改。

## 对下一步修改的约束

1. B1 应补充“实际缺 timeout 的自主恢复”专项，再判断默认值的实机收益；不能用当前分数替代触发证据。
2. B2 可以继续作为独立候选：它解决的是程序自身无法从正确来源取正文、以及错误反馈优先级问题。仍需保留成功父回执、完整正文和身份检查，不能用模型叙述补齐证据。
3. B3 先缩小到有直接采用证据的属性、命名控件与结果形状。编辑任务需要检查为何模型在已经获得正文后仍陷入回执读取；增加结构化步骤不等于建立了任务进度。
4. 编辑观察边界的确定性通过仅证明“旧观察不能直接授权写入”等程序约束。自主模型尚未到达该阶段，下一轮应单列读取、冲突、重读、修改四个阶段的到达率与结果，不能只看最终是否丢字段。
5. B4 优先补“恢复后数据交付”：在同一授权、顺序与参数的回放中，收集原始子结果并返回有界的 `receipt_ref/tool/status/data`，正文仍标记为不可信来源数据；失败或截断必须显式保留。不能只返回回放计数，也不能让父调用修复成功冒充事实已交付。先确定性复现上述“原始正文存在、最后请求缺正文”的失败，再用独立候选重测，不在本批中途修脚本。

以上是本轮结果所支持的修改方向，尚未修改生产实现，也不把局部机制有效解释为整体稳定性提升。

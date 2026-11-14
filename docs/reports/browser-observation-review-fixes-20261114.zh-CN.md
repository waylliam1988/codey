# 浏览器观察、查询恢复与引用审计复核

基线 `457c9452`。直接修改当前工作区，没有创建或复制 worktree，没有运行全量 pytest。

## 已确认的问题与修改

| 问题 | 修改与验证边界 |
|---|---|
| pushState 改 URL 被当成换文档，pending 消失，也无法按原基线补读 | 文档观察增加 Chromium 实际 target_id。现有执行、pending、中断、查询读回与等待交接共用 target/loader 比较；URL 继续保留在来源和任务范围校验中。旧回执没有 target_id 时保留原 loader 绑定，缺少身份不能证明已换文档。 |
| 两个开放 Shadow DOM 的区域都编号 c-1；插入无关区域改变编号 | 控件元数据及最近有意义上下文节点在一次 DOM 观察中取得。观察内分组编号按实际节点分配，结构字段仍保存一次；原 _ElementRef 保存实际容器句柄和原结构信息。执行核验原节点关系和标签，不比较展示编号。 |
| 目标隐藏时后台文字更新，可能凭另一处相同正文结算查询 | 原 query_baseline 的目标观察增加 visible；后续要求实际目标可见、非 busy，并继续核验正式采集正文包含该目标内容。隐藏文字仍可作为原观察事实，不能被当成可见查询结果到达。 |
| 面板已有正文，点击仅揭露它，文字相同无法确认 | 从隐藏到可见也作为可核验披露变化；仍要求正式正文真正取得。原生 details 与 aria-controls 的揭露路径均有真实浏览器回归。没有新增业务成功标记。 |
| 普通 Chat 完成门可能只消费旧回执身份，与执行判断不一致 | 存在未确认查询时，现有完成入口同样获取实时文档身份；解释事实仍来自原回执，完成和执行各自保留原职责。 |
| 引用任意邻居 passage 也会被计成目标证据正确 | 实验审计复用生产 restore_passages，恢复原来源版本、引用身份与实际切片；只以答案实际引用的切片检查目标事实。来源级引用与精确 passage 引用分别计分；保存回执可离线重审，原评分不覆盖。 |

一并确认并修复：仅把原按钮移动到另一个同名容器，旧结构 JSON 仍相同，原实现会接受该控件；现在按原容器节点关系拒绝。新增 target_id 后，页面轮次检查保留原完整文档观察，避免在内部重新拼装时丢失身份字段。

所有修改沿原 BrowserSession、kernel、冻结合同、回执和 Research 来源链。没有第二套运行时、可变查询状态库或并行引用解析器。分组文本仍从正式结构数据生成，审计重建使用现有 passage 恢复实现。

## 确定性回归与静态检查

记录目录：`.e2e-artifacts/browser-review-20261114/`。

修复前真实 Chromium 反例保存在 `pytest-before-corrected-fixtures.log`：10 失败、1 通过，包括 pushState、Shadow DOM、插入区块、同名容器替换、隐藏更新、揭露旧正文、引用邻居 passage 和完成入口实时身份。

更早的 `pytest-before.log` 也保留：初版用 browser_eval 插入区块，会触发该工具原本就有的引用失效行为，不能单独证明展示编号的问题；修正为网站直接 DOM 变更后重新复现。隐藏目标的负例还在可见区域放置相同文字，避免仅因整页正文不含目标而拒绝，从而实际覆盖缺少可见性检查的问题。

修复后的第一轮 14 项通过。补充声明等待、原生 details、真实执行回执和文档身份负例后，曾有 1 项测试断言失败：该冻结请求通过 CodeMode 开放恢复能力，测试却要求直接 browser_read。原提示实际已给出合法 browser_read 调用；修正测试断言，没有修改生产合同，原日志 `pytest-regressions-final.log` 保留。复验 `pytest-regressions-final2.log` 17 项通过；之后补充审计目标/邻居/来源三种引用的独立断言。

扩大生产回归首轮为 161 通过、1 失败，466.03 秒，见 pytest-production1.log。唯一失败是已有实验测试的过时统计断言：直接填写和点击已通过现有 CodeMode 转换为子回执，测试仍把它们计入外层 direct_executions。原数据的 passed=true、真实填写/点击各一次、事实和证据正确；没有生产执行失败。修正断言并检查实际子回执成功数及模型直接调用次数，pytest-leaf-final2.log 的 18 项全部通过。为补齐该修改文件的类型，复用现有明确类型的浏览器 fixture，补全输入与返回注解，没有修改或豁免其他未迁移测试模块。

最终新增浏览器/身份反例 14 项通过；pytest-regressions-final2.log 的 17 项包括当时的 3 项审计测试。补充审计三种引用负例后，pytest-scope-final.log 的 6 项通过；pytest-audit-harness-final.log 的 24 项包含该 6 项及 18 项脚本回归。上述集合重叠，不相加为总数，也没有运行全量 pytest。

所有修改完成、全部生产文件跟踪后，使用同一扩大相关集合完整复验，pytest-production-final.log 为 **168 项全部通过**，478.59 秒。覆盖生产查询/交接/完成、原生详情、schema、快照和进程恢复、上传下载、冷恢复、实验入口与引用审计。进程退出时有一条 RAM-disk 清理 RuntimeWarning：某临时 meta.json 已不存在，退出码为 0，测试全部通过；这属于环境清理告警，原日志保留，没有作为生产失败或隐藏它。

全树 Ruff 通过，生产树及修改脚本/测试 mypy 692 文件通过；kernel_type_precision_gate 也通过。15 个修改 Python 模块均额外启用 disallow_any_expr / disallow_any_explicit；显式包名配置保存在本次记录目录。首次隐式 Any 失败和修正日志均保留。不使用 cast、type ignore 或检查豁免。

## 原保存实验重新审计

执行 `python -m tools.browser_query_scope_probe --audit-run .e2e-artifacts/bc3-before` 和对 bc3-final 的相同命令，没有请求模型或重新访问网页。

两轮重审仍分别为 4/4，但八个答案全部只引用 s1。它们的 exact_passage_cited 原本就是 false，修正后仍为 false；只能作为答案事实与来源级引用的结果，不能作为精确 passage 引用归属的验收。固定首步组的实际交付 passage 仍包含邻居记录，最终答复未串字段。

原 summary.json、provider.jsonl 和回执未改写；每个原运行目录新增 scope-reaudit.json，分别携带 original_evidence / original_passed 和重算值。本轮测试明确验证：答案写对目标值却只引用邻居 passage 时，即使旧缓存 passed=true，新评分也必须失败；篡改 start/end 且沿用旧 ref 同样不能通过。

## 完整实机矩阵

命令：`python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/browser-review-20261114/full-surface-first --scenario all --timeout 180`。

本地 Gemma4 12B Q4_K_M，seed 123456789，temperature 0。覆盖 Chat、strict Research、Hybrid、快照恢复、重复读取、stale-ref、上传、下载和来源失败场景。运行时固定生产源码；首次失败与后续重跑使用不同目录。

首轮完整矩阵为 **10/12**。生产摘要前后一致：`92371e9f61c9ec65174dae674e8287f7c553dfbf051b3019fd5b111d07d380ce`。

- basic 失败的原请求与回执已检查：模型四次输出含 `\\return` 的 JavaScript，解码后产生回车和 `eturn`，编译失败，child_effect_ids 为空，未执行子工具。这是模型输出错误，没有修改生产解析或工具合同来接受它。
- stale_ref 的原任务明确要求填写后尝试旧按钮引用，但模型直接使用填写后新快照中的 g4 引用完成点击，failed_stale_refs=0。答案值和读取正确，仍不计通过。这是模型遗漏验收步骤；实际旧引用拒绝由生产回归单独验证。
- structured、legacy_structured、reread/快照恢复与重复读取、Hybrid stale_edit、上传、下载、strict Research、Research sequence 和两个预期来源失败场景通过。没有发现预期外环境失败。

只重跑失败单元，分别使用 basic-recheck、stale-ref-recheck 目录：basic 为 **1/1**，stale_ref 仍为 **0/1**，原始答复仍直接使用新引用，没有旧引用拒绝。两个重跑的前后生产摘要均与首轮相同。它们是分开的重跑记录，不能将拼接结果称为新的完整 11/12 矩阵，也不覆盖首轮失败。

收尾核对发现 production_source_digest 使用 git ls-files，首轮运行时新建 browser_document.py 尚未加入索引，因此上述 92371e9f 摘要未覆盖该新模块。加入跟踪后，生产源码内容未改，完整摘要变为 `6cc040ea5800f421dd87d91deb511d0c13c68b4267c0c3324eff30663dace45b`。本次记录目录的 tracked-source-proof.json 同时保存新模块 SHA-256 `0b5278613e917d88d5c86706df7aebbbc179d05809b5f5017dbaee4aa86f1e4b`，并确认 codey 下没有未跟踪生产文件。为消除摘要覆盖缺口，在全部生产文件已跟踪的固定版本另跑完整 full-surface-tracked 矩阵；结果单独记录，不覆盖首轮。

最终已跟踪版本完整矩阵也是 **10/12**，前后摘要均为上述 6cc040ea。全部 12 项完成；basic 仍是四次 JavaScript 编译失败且没有子工具执行，stale_ref 仍直接采用新引用、failed_stale_refs=0，其余 10 项通过。原请求和回执另生成 tracked-failure-audit.json 便于核对。没有因模型错误改动生产合同，也没有预期外环境失败。

另外在固定当前源码执行 scope-current 的四个跨记录单元，为 **3/4**。两种布局的自主组都使用普通点击/读取；两种固定正确首步组都读到正确目标、实际点击一次、交付 passage 含邻居，最终都没有串用邻居字段。外部面板固定首步组只给出裸 URL，未提供审计可解析的证据标签，因此按现有引用指标失败；原模型请求、答复、正式读取和 scope-audit.json 均保留，没有为此修改生产合同或计分。四项 source_stable 均为 true，生产摘要与完整矩阵相同。该结果也不能当作精确 passage 引用归属或自主定向操作的通过证明。

## 剩余限制

同一文档仍采用原有保守 pending 限制，不会自动判断所有其他点击是否安全。可见性与节点连续性不等于业务结果成功，也不能识别没有可观察标识的全部业务对象复用。普通来源与 passage 范围未新增隔离，fitting_full_read 的原扩展语义不变。没有证据声称开放网页已经不会串字段，或 12B 自主成功率已经提高。

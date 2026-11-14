# 浏览器缺项驱动下一轮工具交接验收

基线为 `efb8c65c25894eb91e1b622fa48469d871344bcd`，直接修改当前工作区，没有创建或复制 worktree。继续使用原 kernel、RPC、CodeMode、权限、原子回执及托管产物链。原始 full-surface 夹具和通过条件没有修改，没有运行全量 pytest。

## 修改内容

1. **元数据缺项决定下一轮工具。** 普通文字报告的 `observe_metadata()` 返回成功元数据，或携带原因、证据引用和下一调用的 `MetadataPending`。JSON 说明继续使用冻结的 `ToolSuccessRequirement.observe()` 和原字段绑定。当前文档已有完整正式正文、没有未完成查询读回、其他声明步骤已完成时，交接提供原 `browser_tab_list` 或 `browser_eval`，暂时移除外层重复观察、填写、点击及读取入口。原 CodeMode 叶工具仍可用于额外工作和恢复。每轮从原始回执重新评估，没有持久化第二份任务进度。
2. **完成呈现和完成门共用观测结果。** `CompletionOutput` 携带本次已经计算的元数据观测，完成门复用它；独立调用完成门时仍可从原回执计算。指定表达式尚未读到时，不再同时提示“全部已完成，可以交付”。单纯成功执行过其他 `browser_eval`，不能替代要求的表达式。
3. **共用单步 CodeMode 转发。** 等待、标签页、脚本和下载入口转为 `return unwrap(await tools.NAME(inputs));`。工具名只能来自当前冻结工具集合，参数继续按原叶工具合同校验。实际执行仍产生真实 CodeMode 父调用和原浏览器子回执，保留 `via=codemode` 要求、授权拒绝和模型工具暴露限制。`Use codemode only` 的任务只获得原 CodeMode 模板。
4. **等待条件保持原语义。** 从上传中提取已有等待谓词匹配；上传专有的输入选择仍留在上传路径。裸 `browser_wait` 的 `{}` 可匹配一次合法等待，但不能直接执行，不生成 `purpose=result`，不填默认睡眠。明确条件且等待、读取都获授权时，直接和嵌套点击从同一计划获得 `result`，沿用原次序点击、等待、正式读取；默认超时沿用既有 5000 毫秒。点击送达后提示只补等待或读取。
5. **下载交接使用真实快照引用。** 从完整、未截断、当前文档且 generation 匹配的快照回执呈现候选名称和 `ref`，由模型选择目标；旧页面、旧引用或不完整快照先重新观察。成功、送达未知、已送达但保存失败都交付原回执状态，不再提示重下。成功文件的路径、字节数和 SHA256 继续取原托管产物。没有增加任意 Result 自动解包或错误程序改写。
6. **明确导航绑定。** 原 URL 意图解析支持 `Navigate to` 及明确的英文句间边界；继续拒绝否定、多目标、代码、引用指令示例及已发生浏览器操作后的自动导航。URL 本身加引号仍可绑定。原下载任务要求 CodeMode 时，初始导航生成真实 CodeMode 父调用。

## 一并修复的相关确定性问题

- 收窄或提升工具后，冻结 specs/executors 顺序可能与权威 inventory 不一致，CodeMode 声明也可能仍是旧菜单。统一在原 `refresh_snapshot_contract()` 中按 inventory 排序并刷新声明。
- 快照恢复原先要求全部普通 Chat 外层浏览器别名始终存在，会拒绝合法的元数据菜单。恢复现在校验授权别名子集；Hybrid 只允许本轮单步别名，原策略、grant、嵌套工具和冻结合同校验继续保留。
- Chat / Hybrid 浏览器回执统一记录原文档身份；标量 token 与当前标签页 URL 绑定现在也要求当前文档。旧文档回执、截断脚本结果和不匹配表达式不能作为当前证据；上述 token / URL 的同文档矛盾观测仍报冲突，结构化业务字段延续既有的最新回执规则。

## 生产回归与类型验证

- 最终选择的 **308 项回归全部通过**，用时 653.45 秒，未运行全量 pytest。覆盖新交接、点击计划、上传、下载、正文报告、JSON 字段绑定、导航、冻结恢复、fail-closed 协议及 Research 提交交接。
- 固定响应配真实 Chromium 验证：正文之后下一请求实际提供并执行元数据工具；期间没有重复点击、重复读取。错误表达式成功执行之后，仍要求正确表达式，不能提示已完成。
- 异步页面覆盖直接、嵌套及先读旧正文三种情况：正式子回执顺序为点击、等待、读取，交付新正文。独立进程中断恢复覆盖普通点击、查询点击、已提交下载及送达未落回执，实际点击次数仍为一；未知送达不重放。
- 普通下载和原 `CodeMode only` 下载均验证真实托管文件、字节和哈希；真实下载后注入存储失败，保留 `delivery=delivered` / `storage=failed`，不再要求另一次下载。
- `python -m ruff check codey tests tools` 通过。
- mypy 检查生产树及三个修改的测试文件，**687 个文件通过**。所有修改模块额外启用 `disallow_any_expr` 和 `disallow_any_explicit`，清理其中既有 Any；未使用 `cast`、`type: ignore` 或关闭检查。
- kernel 类型精度门禁及 `git diff --check` 通过。

日志和严格 mypy 配置：`.e2e-artifacts/browser-next-step-checks-20261114/`。

开发期间的失败未删除或算成最终通过：早期恢复校验、冻结顺序问题、Windows 长路径测试根目录、异步夹具缺少 UTF-8 声明、恢复夹具正文过短，以及旧导航计数断言，分别按原始证据修复。原始失败保存在 `pytest-first/`、`.e2e-artifacts/bsn2/`、`bsn3/`、`bsq1/` 及早期保留的 RAM 盘失败目录；其中 `bsn3` 为 6 失败、216 通过。随后定向复验 92 通过，最终扩大相关范围为上述 308 通过，分组数量不相加。

## 原始完整实机矩阵：11/12

命令：`python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/bs-live1 --scenario all --timeout 180`。

使用本地 KoboldCpp `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`、seed `123456789`、temperature 0、真实 Chromium。生产源码摘要前后一致：`8ddeb5e248c1d7a7e2421145bfa5abd279d3b8b6cc0766ef6a321a7a5e29f9d9`。

| 场景 | 首次完整轮 | 实际证据 |
|---|---|---|
| Chat basic | 通过 | 点击、真实等待、一次正式读取 |
| Chat structured | 通过 | 一次正文、脚本、标签页回执，精确 JSON 字段交付 |
| Chat legacy_structured | 通过 | 正文后菜单依次提供标签页和脚本，两次真实调用，无重复正文读取 |
| reread / 快照恢复 | 通过 | 正式读取两次，冻结快照恢复八次 |
| stale_ref | **失败** | 事实正确，但漏掉要求的旧引用拒绝验证，拒绝回执数为 0 |
| Hybrid stale_edit | 通过 | 实际版本冲突一次，重新完整读取后恢复 |
| upload | 通过 | 上传、等待和正文正式回执 |
| download | 通过 | 真实下载一次，18 字节，托管产物哈希正确 |
| strict Research | 通过 | 正式读取后实际提交研究答案 |
| strict Research sequence | 通过 | 原声明顺序与等待后正文引用通过 |
| source_failure | 通过 | 正式 HTTP 403 回执，按预期 blocked |
| fixed_failure | 通过 | 固定采集实际 HTTP 403 回执，按预期 blocked |

下载和普通 strict Research 各有一次模型把 snapshot 的 Result 外壳直接当 `.elements` 的错误程序，均被原合同拒绝，随后在同一任务内修正。下载第一次错误之后，下一请求已经提供当前真实候选 ref 和短模板；模型仍自行重写程序，不能把最终成功解释成它必然采用模板。下载实际文件 SHA256 为 `bf6e36ad931d47c37734b551ed4026ab33db8610fb71051e4b0b9573da8b3036`，与正式回执一致。

## stale-ref 独立重跑：仍失败

目录：`.e2e-artifacts/bs-rerun1/`，未覆盖首次完整轮，源码摘要与完整轮相同。

首次完整轮的原请求明确要求旧引用拒绝验证，模型却直接使用填入后提供的新引用，属于漏步骤。独立重跑时，模型在找到 Locality 输入框后就提前 `break`，未获取 Load record 引用，连续四次提交空 `ref`，原参数合同拒绝，最终 `no_progress`。原始程序、请求和持久回执均保留，未改写参数、放宽校验或把任意无效参数拒绝计作 stale-ref。

两次失败均有明确模型输出归因，没有发现相应浏览器执行器的确定性故障或本次实机的非预期环境故障。HTTP 403 和版本冲突为夹具主动要求的故障，不属于环境故障。

## 明确等待条件的独立对照

对照保持原任务和裸等待要求，只额外加入现有 `require_tool_success("browser_wait", arguments={"condition":"text","value":"鱼丸"})`。覆盖 basic、reread、legacy_structured、stale_ref，结果保存在 `.e2e-artifacts/bs-bound1/`，不混入原始 12 项矩阵。

首次对照 **3/4**：basic、reread、legacy_structured 通过；stale_ref 仍漏做旧引用拒绝验证，失败。四项首请求的原始 JSON Schema 均包含 `purpose` 的 `result / controls / action` 三个分支；但实际模型四项都选了 `action`，然后手动等待、正式读取，没有使用自动串联。

因此这组实机只证明明确条件确实开放了可执行的 `result`，没有证明模型会选它，不能据此声称减少了模型往返。自动串联本身由固定响应配真实 Chromium 的直接、嵌套和异步旧正文回归验证。原始裸等待场景和通过统计均保留，没有用条件更明确的任务替换基线。

stale_ref 另在 `.e2e-artifacts/bs-bound-rerun1/` 独立重跑通过：真实取得一张 `reason=stale_ref`、`delivery=not_delivered` 的旧引用拒绝回执，随后重新观察、点击新引用、显式等待并读取。该次仍是模型手工编写 CodeMode，没有使用 `purpose=result`。首次对照保持 3/4，不改写为 4/4；也不能用这个条件更明确的重跑替代原始裸等待 stale-ref 的失败。

完整轮、两组独立重跑及等待对照的源码摘要均与上述最终源码一致，运行前后相同。

## 结论边界

这次原始完整轮为 11/12，前次基线最终完整轮为 8/12；这是不同源码下的少量实机观测，不能据此估计稳定的普遍通过率。可以直接确认的是：legacy 元数据轨迹使用了缺项驱动的下一轮菜单，实际执行了两项原来遗漏的元数据调用；下载获得真实产物；固定响应与真实浏览器测试验证了有明确条件的自动等待链及不中断动作恢复。

裸等待仍需模型选择真实页面条件；模型仍可能选择手工分步、忽略模板、漏掉额外语义步骤或编写错误 JavaScript。没有为这些错误修改生产合同，也没有把首次矩阵失败或独立重跑失败合并成通过。

原始证据包括每项的 `request.json`、`provider.jsonl`、`events.jsonl` 和 `state/`；`audit.json` 及 checks 目录的审计日志只是派生索引，不替代原始记录。

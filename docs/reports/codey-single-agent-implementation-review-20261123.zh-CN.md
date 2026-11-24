# Codey 单代理源码对照与函数级改造方案

2026-11-23。适用范围为编码、网页操作、深度研究，以及支撑三者的本地知识与用户记忆。源码检查从 Codey `a8c6dee6` 的工作树开始，期间仓库有其他工作提交。七个参考版本列于下表。下文的“新增”“建议”均指待做设计。

**建议保留统一 kernel，把开发重心放在模型每一次选择所依据的事实。** Codey 已经能在同一任务中读资料、点网页、改文件和验证。现阶段最值得争取的优势，是小模型选错一步后仍能从真实结果恢复，换连接后仍能说明哪些要求已经满足，以及为什么相信某条旧知识适用于当前代码。目标可以用任务工件、页面业务状态和冻结请求分别检验。

需要立即动手可跳到[逐文件实施手册](#逐文件实施手册)。

### 建议拆分的交付批次

| 批次 | 改动 | 合并条件 |
|---|---|---|
| 第一批 | 01 恢复 A/B 验收入口，02 修复单组独立反证覆盖，03 对实际请求去重 | 三项独立提交。01 留两臂原始请求与终态回执，02 留同组排列红测，03 留文本与原生生产请求的分区计数。 |
| 第二批 | 04 紧凑知识上下文，05 网页等待接续，08.1–08.6 显式保存记忆 | 分别验收多条件不丢失、超时后一次观察、源消息被编辑后确认失败。显式保存先独立上线，偏好排序等到来源生命周期验证完再动。 |
| 第三批 | 06 编码停滞诊断，07 网页等待条件统一，09–11 目录指令、局部写入和研究证据 | 停滞先上线诊断，再评估自动停止。目录指令先有被阻拦目标的恢复链，再开启写前守卫。每项记录失败机制、真实执行影响和回滚点。 |
| 产品验收 | 12 跨模型接力，13 自然组合任务 | 第一天冻结留出题与独立判据，最后用真实模型跑。跨模型分别记录普通新会话、同线程新运行接力、原运行恢复。 |

## 哪些判断会改变排期

| 原判断 | 核对后的决定 |
|---|---|
| 先修研究到编码的验收入口 | 保留，而且要保住 A/B 两臂原来的研究上下文注入位置。单纯让超时消失会丢掉实验目的。 |
| 减少任务正文重复 | 保留。编码与混合入口分别装配；纯研究已有跳过通用工作视图的分支。按实际发送分区计数。 |
| 加编码进展指纹 | 保留，还要改失败信息的计分，以及先停止、后提醒的顺序。后台读取成功和测试成功有不同语义。 |
| 增加可恢复的多文件修改 | 已有 2–8 个文件的 CodeMode 重载、全组预检、逐文件结算和冷恢复。先补部分结果的结构化展示、写后结算前断点及目录指令。 |
| 浏览器增加遮挡诊断、研究增加补搜 | 内层和祖先 frame 已做命中检测，失败已有 blocker；冲突已能触发一次有配额的宿主补搜。精力应放在等待口径、证据丢失和同名候选上。 |
| 把记忆简化成一段短文字 | 推迟。现有审计会从实际渲染文本反解完整 item。先交付从原始用户消息保存，再单独迁移投影和校验合同。 |
| 用来源独立性增强研究 | 先修已复现的发现顺序依赖，再扩宽近重复识别。论文身份、文本版本和两份证据的独立性要允许分别判断。 |

## 证据有多强

| 证据 | 可以据此决定什么 |
|---|---|
| 11 月 20 日 Codey 与 DeepSeek Harness，同一 Gemma、各 36 次生成。独立测试 10/12 对 6/12，Codey 11 次编辑仅 2 次改动，9 次空编辑。最后 25 次生成无新文件修改。两边均未完成综合任务。 | 花时间定位空编辑和命令读取循环。12 是一个任务的测试项数。不同工具协议和权限、单次固定采样，无法支持总体成功率。 [实验设置](codey-deepseek-harness-comparison-20261120.zh-CN.md) · [逐次分析](../../.e2e-artifacts/codey-deepseek-harness-20261120/pair-first/analysis.json) |
| 10 月 OpenCode 和 Pi 的各 20 个配对回放。Codey 对 OpenCode 为 20/20 对 14/20，对 Pi 为 19/20 对 10/20；Pi 也有一个独胜配对。 | 保留旧回放做防退化，新增未知仓库、真实模型、独立业务验收。历史版本、二进制和当前 checkout 分开记录。 [配对汇总与版本](codey-opencode-pi-comparison-2026-10-10.json) |
| 11 月 18 日网页行动实验，可读正文的 5 个自然任务里，原生协议 2 格完成、3 格重复搜索；文本协议 4 格完成、1 格请求超时。写明步骤的两题两协议共 4/4。 | 实机题保留“只说目标”和“给出步骤”两种。登录受限后还要评分最终答复是否错误归因。该批测搜索、读取与采用，和 DOM 点击分开。 [逐格报告](model-web-action-capability-result-20261118.zh-CN.md) |
| 11 月 22 日动态按钮普通与严格模式的最终自然样本都点击 4 次。审计的 `browser_read_count=4`、`formal_read_count=5`，最后正式读取含完成提示。恢复旧前缀的成功样本另计。 | 当前风险移到同名、只改变 enabled、frame/document 替换和断线等场景。统计时明确“动作后的读取”和“整次任务的正式来源读取”。 [原始审计](../../.e2e-artifacts/browser-recovery-20261122/recovery-audit.json) · [运行口径](browser-recovery-and-source-structure-20261122.zh-CN.md) |
| 11 月 22 日多文件冲突实机保住外部注释，两个文件各写一次；同批最终研究样本 4 次模型调用、2 项带来源的比较、1 次宿主补搜。前几次研究样本曾出现虚构 passage ID 和漏比较项。 | 扩充断点与未知字段测试；保留从真实回执向模型给出可选 ID 的设计。最终样本使用受控材料、已声明比较要求。 [冲突工件](../../.e2e-artifacts/followup-audit-20261122/project-conflict-first/summary.json) · [研究工件](../../.e2e-artifacts/followup-audit-20261122/research-live-final/followup-summary.json) · [失败过程](followup-mechanisms-validation-20261122.md) |
| Ghost 第二批正常检索的 8 对简单任务，已确认 Rust 偏好命中 5/8，对照 0/8；4 个显式持久要求均未形成合法候选。两批用了相同题。 | 第一优先级是使用户明确保存的操作可靠。三格未完成的回答不能因出现 Rust 得分。已有本地存储，与多个真实供应商轮换后持续执行任务，是两种验证。 [记忆报告](../ghost_progress/memory-completion-20261121.zh-CN.md) |
| 研究语义复核 D 的冻结输出按修正后规则评分：12 例中原答命中 58/68 项，自查与带反馈各 62/68，部分改进来自答案末尾附加“候选 Claim”。 | 发布评分须核对最终正文是否正确采用，另记新增无支持断言和复核成本。多一次写作可能抬高词项得分。 [评分与原文 diff](research-semantic-review-d-analysis-20261106.json) |

## 七个参考实现如何迁移

| 本地 checkout | 源码中的行为 | 在 Codey 中的落点 |
|---|---|---|
| Codex `970111f6cb` | `load_project_instructions` 依据 environment/cwd、已有权限与共享字节预算读取，记录来源；失败写入时 `apply_hunks_to_files` 将 delta 标为不再精确。 | 目录指令记录实际路径与摘要；区分“写失败”“已确定未写”和“结果未知”。[指令](../../reference-projects/codex/codex-rs/core/src/agents_md.rs) · [写入边界](../../reference-projects/codex/codex-rs/apply-patch/src/lib.rs) |
| OpenCode `3ba597f776` | `SessionInstructions.load` 用短期 in-flight claim 避免并发重复注入，再从合成消息历史查持续去重；`PatchTool.Plugin` 先解析和准备，失败列出已应用路径。 | 第一次进入子目录按需装配，从模型可见的冻结上下文判定是否已交付；让批量编辑的部分结果可机读。[指令服务](../../reference-projects/opencode/packages/core/src/session/instructions.ts) · [补丁工具](../../reference-projects/opencode/packages/core/src/tool/plugin/patch.ts) |
| DeepSeek Harness `8bd073392e` | 请求边界 freeze header、派生消息和已选工具。`observe` 用工具名和规范化参数更新连续计数，从 post-execute 附上带来源的提醒。 | 度量最终 wire；运行事实保留在原回执，修正建议放在下一次观察。其连续相同调用检测在交替循环里未触发。[请求组装](../../reference-projects/deepseek-harness/packages/core/agent-loop/src/agent.ts) · [提醒逻辑](../../reference-projects/deepseek-harness/packages/guard/repeat-tool-reminder/src/index.ts) |
| Pi `ed4f48840` | `buildSessionContext` 从所选 leaf 的会话投影重建；`transformMessages` 按目标模型处理 image、thinking、调用 ID 和工具结果配对。 | 从 Codey 自己的事实重建，再做目标协议转换；Pi 的缺结果占位不能作为 Codey 的执行证明。[会话](../../reference-projects/pi/packages/coding-agent/src/core/session-manager.ts) · [压缩边界](../../reference-projects/pi/packages/coding-agent/src/core/compaction/compaction.ts) · [消息转换](../../reference-projects/pi/packages/ai/src/api/transform-messages.ts) |
| agent-browser `4b0e9f0` | `take_snapshot` 以 CDP session、frame 和 backend node 限定观察。`snapshot_delta_response` 除角色与名称变化，还给出可重建原树的文本差异。 | 为“同名、名字不变但状态变化、frame 替换”设测试；每次增量须能回到原基线或明确要求全量。[快照](../../reference-projects/agent-browser/cli/src/native/snapshot.rs) · [增量](../../reference-projects/agent-browser/cli/src/native/actions.rs) |
| agent-desktop `b43ac4f` | `corroborate_interception` 在证据不足或超时时产生未知判断；`finish_post_action_wait` 即使等待失败也保留前一动作及其 delivery。 | 复用操作已派发的事实。诊断携带命中点、目标和可观察的遮挡方，等待超时只转观察。[命中归因](../../reference-projects/agent-desktop/crates/windows/src/tree/hit_test/hit_test_corroborate.rs) · [动作后等待](../../reference-projects/agent-desktop/crates/core/src/commands/helpers.rs) |
| HyperResearch `aece65f` | `merge_papers` 优先 DOI，两个已知不同 DOI 拒绝按标题合并；`compute_independence` 实现中按 URL、转载开头和两两 shingle Jaccard 聚类；`triage_pairs` 的非数字路径允许连续六词重合通过。 | 借候选发现；统计按独立组，保留所有原段落。仓库探针显示一条附带相反断言的长句也可通过六词匹配，五份重复材料的权重之和可达 1.8。[文献去重](../../reference-projects/hyperresearch/src/hyperresearch/scholar/dedup.py) · [独立性](../../reference-projects/hyperresearch/src/hyperresearch/core/independence.py) · [核对](../../reference-projects/hyperresearch/src/hyperresearch/core/citecheck.py) · [反例](research-hyperresearch-comparison-probes-20261106.json) |

## 第一批可以直接开的修改

### 1　把研究到编码的 A/B 放回有执行者的环境

1. 从 `tests/manual/research_to_code_ab.py::_run_arm` 开始。它在临时项目里准备相同文件，唯一处理变量是 `AgentRequest.research_context=brief_text`。其 `_agent_kernel_request` 实际导入 `tests.support.kernel_runtime.run_agent`。本次以 20 秒限时抓栈，主线程在 `evaluate_completion → wait_tool_task`，从 `_self_test → _run_arm → run_agent` 抵达。该 helper 创建 durable、mutation line 和 managed outputs，却没有消费者启动路径。
2. 最小改动是给**这一个实验**用的上下文装配生产工具任务调度器。参考 `HeadlessAppContext.__init__` 启动 `durable_scheduler.start(tool_tasks_only=True)`；让请求、scheduler、输出仓和 workspace revision 使用同一组依赖，测试收尾在上下文管理器中停止 scheduler、关闭存储。另一条可行路径是整个实验走 `run_headless`，前提是能从正式研究交接路径等价送入两个 brief。`HeadlessRequest` 目前无 `research_context` 字段；不能直接换调用、把两份 brief 都丢掉。
3. 为每臂抓第一份实际请求，验证公式和诱饵都存在，而只有 baseline 有 raw excerpt；比较项目树哈希和两臂的去除 brief 后输入。保留 `score_arm` 的落盘检查、独立子进程 pytest 和无诱饵污染检查。再给一个错误公式，核对独立评分能判负。
4. 合并条件为两臂均在现有超时内结束，命令有真实 terminal 回执，失败退出可上报。保留外层 `SELF_TEST_TIMEOUT=240` 和 process-tree cleanup。给出首次执行与复跑记录；不能用 fixture 手填退出码通过。

入口与对照可从这四处起步。[实验](../../tests/manual/research_to_code_ab.py) · [隔离夹具](../../tests/support/kernel_runtime.py) · [实际 scheduler 装配](../../codey/app/headless_runner.py) · [有两次真实验证的生产入口测试](../../tests/test_p0_mixed_workflow.py)

### 2　让任务正文有唯一的装配位置

1. 在 `build_agent_context` 加仅限关键字参数 `include_user_task: bool = True`。关闭时同时省掉尾部的正文块和对应的 `request:user_task` 来源标签，保留 workspace intro、sources 和 memory spans。先保留默认值，覆盖现有直调测试。让 `_project_context` 的共享 kernel 调用关闭该项，同时覆盖 `project_adapter.run` 和生产的 `task_entry._entry_context`。
2. 从 `working_context` 返回的 JSON 删除 `user_task` 键。保留 `session.task_text` 本身，策略、研究范围和请求准备仍会读它。`kernel_prompt_for_session` 继续拥有首次装配的原文。保留 `task_working_context` 对纯研究的特殊分支。续轮可以依靠完整历史或恢复后的任务块；特别测试“压缩后只剩观察”的情况。
3. 在 `KernelContext.freeze` 对应的测试出口和实际 provider 请求分别统计任务块、工具定义、工作观察、历史、记忆与结果摘要的大小。用唯一任务标记和一条不同的后续更正。按解码后的消息字段统计，区分原始用户消息、当轮装配、历史引用。断言原生工具结果配对、文本工具 JSON schema、权限和选定记忆的发送位置都等价。
4. 首轮文本/原生 × 项目/混合各测一次，另测说明型回复、纯研究、冷恢复和续轮。接受条件是实际首轮只装配一次，后续更正可见，原回执可按引用读回。历史的 11,745 对 4,844 是统一序列化分词，多个因素共同造成差额。实测报告分列统一分词、provider usage、正文减少量，无 usage 时留空。

[项目渲染与 spans](../../codey/agents/context.py) · [项目适配](../../codey/operations/project_adapter.py) · [另一生产调用方](../../codey/operations/task_entry.py) · [任务与工作观察](../../codey/operations/kernel_prompt.py) · [冻结](../../codey/operations/kernel_context.py)

### 3　让编码循环同时认得重复失败与新失败

**有两个相反的错误方向。** 一个失败被反复读出时，显示文本中的新 task ID 能让它显得新鲜。另一个方向，本次用纯函数给同一命令送四条不同的 `ok=False` 结果，`idle` 从 1 涨到 4 后停止。这个探针说明当前分支如何计分；真实链中 `task_output` 读取成功还可能返回外层 `ok=True`，必须同时覆盖两种形状。

1. 从 `automatic_command_result`、`tool_task_snapshot` 和 `tool_execution_result` 梳理三种形状：已接单、命令终态直接返回、通过 `task_output` 读子任务终态。最后一种读取是否成功、子任务状态、进程退出码分列。复用 `owned_tool_task` 的所有权检查。建议在已有权访问 durable 的 `KernelContext.observe_progress` 所调用的适配层解析一次；给纯比较函数传递小的只读事实。不要在 `progress_comparison` 内重新开库，或让它以当前磁盘状态代替执行时版本。
2. 为可信的项目检查生成比较键。包含工具种类、已解析的命令身份与 cwd、执行时工作区 fingerprint、终态、退出码，以及可可靠恢复的失败标识集合。先支持一个可验证的 pytest 输出格式；若无完整标识，保留证据不完整状态和原输出定位。不同的业务输出、新读取的日志窗口、未知退出和任意有副作用的命令须走原观察路径。精确身份和时间照常留在原回执。
3. 分开“该次调用是否首次进入终态”和“任务是否取得新进展”。同一个 execution 从 pending 到 terminal 要可见一次；在相同项目版本上每次重新启动相同失败测试，不能靠新 execution ID 永远刷新预算。只有可信且在当前修复阶段新出现的失败事实才能把 `ok=False` 计为信息进展。对 A、B 两套失败交替出现，按阶段内已见集合去重，避免波动测试无限重置。
4. 改 `KernelProgress.observe`、`repeat_feedback` 和 `KernelContext.deliver_results` 一起验证。现在 `deliver_results` 检查 `stopped` 在先，之后才调用 `repeat_feedback`，后者自己遇到 stopped 也返回空。新增编码诊断只写在反馈函数中会错过第一次有效修正。让第一轮确认的编码循环获得一次反馈；下一次等价循环才按已有预算结算为未完成。若需要跨重启记住“已提示哪一轮”，在 `ProgressSnapshot` 作可回读的版本升级，保留历史 v1/v2、batch 重投幂等。不要把通用 effect_cycle 的浏览器文案送给编码任务。
5. 反馈只说有据可查的事，如“这次 edit 未改动文件；上次同版本 pytest 仍在两个用例失败；读取对应失败窗口和函数”。显示真实回执 id、文件和测试名，缺哪个就省哪个。旧失败前缀重放测“哪一轮首次提醒、能否选到正确下一步”；放开模型重新跑，另测最终业务通过项、无效生成数，以及独立诊断停止/健康检查是否完成。

正样本为相同文件、同失败、不同 task ID；反样本为版本变化、失败 A→B、新日志页、首次终态、外部修改、交互式命令。触发更早但业务通过项下降，保留诊断并撤回新的自动停止策略。[原始回执](../../codey/operations/tool_tasks.py) · [比较](../../codey/operations/progress_comparison.py) · [计分](../../codey/operations/kernel_progress.py) · [checkpoint](../../codey/runtime/core/progress.py) · [交付顺序](../../codey/operations/kernel_context.py) · [历史业务错误](codey-deepseek-harness-comparison-20261120.zh-CN.md)

### 4　使浏览器等待与下一次可选操作一致

1. **限定改动入口。** `browser_followups` 的 `purpose=controls` 已冻结等待、快照及正式读取。`browser_control_wait_handoff` 负责另一条通用路径。先按第 05 步把同一点击之后的等待与观察连起来，再按第 07 步选择等待种类。原方案只按最新 snapshot ref 查旧等待，新快照会让同一点击再次浮出等待。显式等待谓词优先走 `next_browser_step` 的实际要求匹配，通用交接仅提供剩余的观察建议。
2. **改基线。** 控件等待从 `snapshot_wait_baseline(session, ref, "controls_changed")` 取完整控件集；对于控件完整、正文截断的页面，当前 handoff 会在 `snapshot_text_baseline` 失败后提前退出。应先确认控件覆盖，文字比较作为另一个可选信号。消费者拒绝局部快照冒充整页基线，保留 owner、target、frame、document 身份和 timeout。
3. **改采样。** `BrowserRuntime._wait` 目前比较 AX 的启用 `(role, name)` 多重集合，仅对数量增加为真；`collect_snapshot` 的 DOM/AX 合并、可见性、disabled、区域语义更丰富。抽出两边共用的只读“可选控件行”投影。快照另做 registry/ref 登记；每 100 ms 的轮询避免注册新 ref、生成截图或改变 revision。用同区域的计数和跨区域的标签分别处理同名。等待语义写清“新增或变得可用”，删除、无关文字变化、旧 ref 重编不能误唤醒。不要为每一行做点击级动画等待。最终能否执行始终由 `_input` 对原目标复核。
4. **改失败交付。** `_HIT_TEST` 已返回 blocker 的 tag/role/name；`_input` 已检查目标与祖先 frame。补的是受限的命中点及坐标所属 frame、受阻层次、原动作引用，并在回交时与 `delivery` 同时展示。未派发可重新定位；已派发或未知优先回原 target 观察。存在同名控件时优先给可唯一解析的区域，仍重复则提供当前快照中可复核的 ref/局部截图。没有证据辨别业务目标时保持歧义。
5. 在页面 fixture 记录**实际处理事件次数**。至少覆盖 fieldset 延迟启用且正文不变、A 区消失同时 B 区出现同名控件、iframe DOM 被替换、正文截断但控件齐全、遮挡解除、用户切 tab、派发后 worker 断开。断言等待返回原因、下一快照身份、页面最终状态和无重复输入。另测网络/前端错误导致 click 已送达但业务失败，确保报告能表达这两件事。

[交接](../../codey/operations/browser_task_handoff.py) · [等待基线](../../codey/operations/browser_waits.py) · [快照](../../codey/automation/browser_snapshot.py) · [运行时](../../codey/automation/browser_runtime.py) · [实际命中检测](../../codey/automation/browser_input.py) · [选择器歧义](../../codey/operations/browser_references.py)

### 5　让用户明确要求保存的记忆有确定结果

1. 第一版补“从自己的消息选中原句 → 保存”。复用 UI 已有选区及记忆抽屉，界面显示原句、将保存的文本和使用范围。追加一个建议的 action `propose_from_message`，接收当前会话内的 entry id、quote、可选起止、scope 选择和持久的 request id。服务端从当前有效历史取 `user_message` 原文，验证消息版本与范围；复用 `locate_candidate_quote`。对重复句必须带能唯一定位的区间。前端的 UTF-16 偏移换成合同使用的 Unicode 码点，用 emoji、组合字符和重复中文覆盖。选中 assistant 或网页转述不能借这个入口成为用户原话。
2. `_propose` 当前只采纳白名单字段，source refs 指向 proposal 记录及会话。仅从前端多传 `message_id` 会被丢掉。扩展服务端规范化的命令载荷，绑定经核对的原消息摘要与区间，再复用现有 `record_command`、`memory.proposed` 和版本化 `_decide`。相同 request id/不同内容应明确报冲突；Core 已提交而 Ghost 投影失败时，用相同 id 重试，不产生第二条有效记忆。UI 区分待审、已确认和失败。
3. 本项目保存沿项目确认；跨项目偏好沿 `_confirm_user_preference` 的专门语义。它生成独立的 `user-confirmed:` 事件，注意“从哪段对话提议”和“用户选择独立确认的内容”有不同生命周期。确定性测试覆盖删除源消息、撤销确认、恢复旧快照后各自是否仍可消费，沿已有产品合同执行。首版从选区保存可独立上线。对自然语言“请记住”可在缺合法候选时提供同一确认入口，让用户一击保存；一般叙述、引文、假设及“这次用 Go”继续作为当前任务语境。
4. 再调整 `GhostMemory.retrieve` 排序。它已经过滤 scope、状态、过期、context、family，并预留一个长期 user preference 的首位。目前这个位置可以压过更贴切的项目偏好。先对**已注册 slot 且适用**的冲突组分别排序，优先本轮明确覆盖、同项目确认值、全局默认；当前要求只临时遮蔽全局值，下一任务仍可恢复。无注册 slot 的自由文本保留为候选和来源，不做自动互相覆盖。把优先级冲突和挤出原因写进既有 `MemoryTrace`。测试加入“记 10 条个人偏好后完成与其中 9 条无关的任务”。
5. `retrieve_context` 当前把完整 `item.to_payload()` 渲染到请求，`candidate_item` 会从 `source.text` 反解并对照 scope 与 source refs；`terminal_memory_facts` 依赖它。只有实际预算数据显示问题才做短投影。先定义新版冻结载荷中可独立校验的 item 版本、用户可见短文本摘要和其在发送中的 span，兼容旧全文解析，并保持发送前撤销校验。仅把 JSON 改短会损坏审计。
6. 合并条件：模型完全不产出候选也能按用户的保存操作落库；双击、断网重试各产生一个有效条目；“以后用 Rust”→新会话采用→“本次 Go”临时覆盖→撤销后新请求不再收到旧值。分别断言持久记录、实际发送、最终示例语言/能否运行。把“系统已保存”和“某模型遵从”分别评分。

[选区](../../frontend/src/conversation_ui.ts) · [记忆抽屉](../../frontend/src/local_context_drawer.ts) · [命令幂等](../../codey/app/memory_commands.py) · [路由](../../codey/app/memory_routes.py) · [原文定位](../../codey/memory/candidates.py) · [来源检查](../../codey/app/memory_setup.py) · [排序](../../codey/ghost/backend.py) · [发送投影](../../codey/memory/context.py) · [观察审计](../../codey/app/memory_observation.py)

## 第二批应复用哪些已有能力

### 6　把子目录指令接到读写的真实边界

`load_project_instructions(root)` 当前只尝试根目录的 `AGENTS.md`、`CLAUDE.md`，按文件截断。保留根入口兼容；增加可选 `target`，从规范化目标所在目录向根收集，按根到叶顺序输出相对路径、适用目录、全文摘要、实际投递片段和截断标记。同目录两种文件沿现有顺序，总预算按实际投递量共享。正文读取先做现有 profile/capability 和物理路径边界检查；Windows 大小写、junction、symlink、越界和不存在的待创建文件都要测。

- `project_result_observation` 同时服务于 live、恢复和历史投影。**不要在这个函数读今天磁盘上的指令并回填昨日回执。** 建议仅在这里提取已访问目标路径。在下一次 freeze 前由 workspace 上下文构造器读当前适用指令、记录摘要和投递范围；历史请求照原冻结工件恢复。目录规则改版，只使后续请求重新呈现。
- 同一 CodeMode 程序可能 `read_file` 后立刻 `edit`，中间没有模型轮次。首次写入有尚未向本次决策展示的新指令时，在第一个受影响的 child 写前返回明确的“重新观察指令”结果，把路径、有效版本送到下一请求。已展示时沿原写入路径。对多文件 patch 应尽量在整组预检发现所有目录差异，避免人为制造部分成功；没有新指令时不额外消耗一轮。另测直接 edit、create、语义编辑和跨 sibling patch；shell 能执行任意程序，只能在其真实可解析的范围内声称覆盖。
- 在冻结请求中以“适用目标、来源路径、内容版本”判重，避免单一 session 全局已读标记串目录。已展示的 A 包规则在后续 B 包工作中须保有清楚的作用域。压缩移除后再次访问 A，应重新投递。

[根目录加载](../../codey/agents/context.py) · [实时与恢复共用的事实投影](../../codey/operations/kernel_facts.py) · [批量预检](../../codey/codemode/patch.py) · [OpenCode 的历史去重](../../reference-projects/opencode/packages/core/src/session/instructions.ts)

### 7　补齐多文件修改的部分结果

`preflight_patch` 已验证重复路径、权限、原 read 的 raw 版本、替换锚点及 Python 语法回退；存在回执绑定字段的目标会被拒绝。`CodeModeHost._patch` 通过 `_invoke_one` 顺序执行，失败时把已写路径列在字符串中。`ChangeTracker.restore` 已比较当时写后哈希与磁盘现状，能识别冲突。实施顺序如下。[预检](../../codey/codemode/patch.py) · [执行](../../codey/codemode/host.py) · [恢复](../../codey/workspace/changes.py)

1. 从原 child effect 投影每个目标为“已改、有回执但无改动、失败、尚未尝试、结果未知”，给出原 child ref 和可用的版本。沿已有 CodeMode 返回兼容路径保留 `ok/error/value`，优先把结构放在宿主的结果摘要；测已有 guest `unwrap`、历史回放和缺字段的旧结果。
2. 重试只重读失败及剩余文件并重新预检。对“首文件已结算、第二个未登记”和“第二个已登记无回执”，沿已有两种恢复行为。额外在实际写入后、settlement 之前注入中断：磁盘达到预测内容可证明此刻文件的内容，不能证明由哪次执行写入；单独显示可观察结果和未知执行归属。
3. 基于首写基线与写后哈希生成恢复预览；允许按文件选择，对后来有人更改的路径返回冲突。执行恢复时再验证版本。测试混合 CRLF/BOM、连续两次改同一路径、外改后撤销、已恢复后重复操作。优先沿独立回执恢复；跨文件事务、统一锁和完整 patch 语言分别要有业务需求才值得增加。

当前冷恢复测试已含“第二个 child 是否登记”的两个参数格，最先新增的是写后结算前的故障注入。[已有冷恢复用例](../../tests/test_followup_recovery.py) · [全组预检用例](../../tests/test_project_patch.py)

### 8　先修研究证据覆盖，再扩充全文发现

**最小、可独立合并的研究改动在 `_comparison_gaps`。** 当前容器是 `directional[cohort][direction] = passage`，同方向后来的 finding 会覆盖先前 passage。以完全相同的问题、对象、比较指标和条件输入以下三条，调用公开的 `conflict_search(remaining_searches=1, findings=...)`。材料仅为本次内存探针，未访问网络。

| finding | 来源身份 | 方向 |
|---|---|---|
| a | 来源甲 | lower |
| b | 来源乙 | higher |
| c | 来源甲的另一段 | higher |

顺序 `a,b,c` 返回 `None`；顺序 `a,c,b` 返回 `['a','b']`。两组掌握的独立反证完全相同。这一现象发生在比较字段分支，另一个按 stance 的缺口渠道在具备相应输入时仍可发现冲突。[比较与补搜](../../codey/research/evidence_gaps.py)

1. 先写红测。给 `tests/test_followup_mechanisms.py` 增加三条原始 passage、同条件的三个 finding，参数化六种排列；断言包含独立相反证据，搜索候选可复现。改 `directional` 为每方向有序去重的 passage 集合；在同一 cohort 内按**确定同源**分组，找至少一对异组且方向相反的 passage。稳定选取代表，并保留同源内部矛盾作为待核对事项。不要把新语义直接塞进 `if same_source_groups(...): continue`，有一对同源无法排除其余独立证据。保留已尝试查询去重、零配额和条件不同的反例。
2. 保留 `same_source_groups` 对既有报告的确定分组；另加带原因的疑似同源关系，由 `answer_context`、报告诊断和下一轮搜索分别消费。候选只比较有足够可见内容的完整源，每份不同材料版本算一次；先按内容特征缩小候选，再做有上界的 shingle 比较。为中文无空格、页眉页脚模板、同稿改标题、同数据集不同论文各做反例。相似性阈值在标注样本上选；同 DOI 可用于工作身份，内容不同仍保留各自 revision、read window 与引用。独立证据按组计数，疑似组可给保守区间。文本低相似也不能证明使用了独立数据。
3. 在 `check_report_evidence` 扩展逐断言诊断。输入复用 submission 的 markdown 引用范围和已读 passage。对目标句记录报告字符范围、引用范围、来源窗口、数值/单位、适用版本或条件、仍待判的陈述。并列数值、转折、否定和例外先做局部定位；定位不出来就留作未判断。保留现有 `verified_quotes` 和 `verified_calculations` 的窄含义。新增结构与 `task_execution` 的提交及 `answer_report` 的恢复复算同批升级。旧回执用原算法或原字段版本比较，不能让新近重复规则改变旧报告的可重放性。
4. `answer_coverage` 是关键词覆盖信号，`build_research_plan` 是候选查询生成器。先把可定位的缺口交给 `evidence_gaps → _search_candidate → conflict_search`，用现有配额、调查回执和 submission 前的宿主补搜路径；无来源、同源待查、同条件方向冲突应产生不同查询。检查同一未解决问题在重启后仍最多按预算尝试。有正确引语却颠倒适用条件、两个分句只支持一个、使用不同分母、用摘要声称全文方法，均进入盲评。
5. 全文发现后做。`_full_text_candidate` 已支持特定 arXiv 标识与 PubMed/PMCID 升级，`_open_full_text` 会核对类型、落地地址并回退。扩展为有界候选序列，按已有连接器给出的出版方/开放仓储链接逐一核验工作身份与版次，保留每次 acquisition。当前 `PublicationMetadata.decode` 仅接受 `doi/pmcid/status`，status 只允许空、撤稿或撤稿通知；文献版次宜另建可选的版本化字段。区分摘要/正文材料类型、预印本/正式版和撤稿状态；解析不到保持未知。旧 source artifact 必须仍能解码，直接 PDF、摘要可读但全文失败、落地到别篇文章均要测。

[研究实机输入甚至明示了方向字段和算式](../../tools/followup_research_live.py) · [引文核对](../../codey/research/report_evidence.py) · [旧回执复核](../../codey/research/answer_report.py) · [词项覆盖](../../codey/research/proof_coverage.py) · [来源获取](../../codey/research/source_gateway.py) · [版次合同](../../codey/research/source_document.py) · [参考的公开全文恢复](../../reference-projects/hyperresearch/src/hyperresearch/core/oa.py)

#### 一条可以直接放进现有测试文件的红测

下面的用例使用 `tests/test_followup_mechanisms.py` 已导入的生产类型。独立异组的 lower 和 higher 是验收条件；可以额外保留同源内部矛盾的其他引用。六种排列都应满足条件。

```python
from itertools import permutations


def test_conflict_search_keeps_independent_opposition_in_every_order() -> None:
    ledger = ResearchLedger()
    calls: list[ToolCall] = []
    for ref, origin, direction in (
        ("a", "first", "lower"),
        ("b", "second", "higher"),
        ("c", "first", "higher"),
    ):
        url = f"https://{origin}.example/paper"
        source = OpenedSource(url, url, "", "", ref)
        ledger.read_passages[ref] = ReadPassage(
            ref, source, direction, (), 0, len(direction)
        )
        calls.append(ToolCall("save_finding", {
            "question_ref": "q1",
            "passage_ref": ref,
            "conditions": [],
            "comparison": {
                "subject": "Atlas", "factor": "A", "comparator": "B",
                "endpoint": "fuel", "direction": direction,
            },
        }))

    for ordered in permutations(calls):
        found = conflict_search(ledger, remaining_searches=1, findings=ordered)
        assert found is not None, [call.args["passage_ref"] for call in ordered]
        refs = found["passage_refs"]
        assert isinstance(refs, list) and "a" in refs and "b" in refs
```

这个测试没有预生成搜索结果，检查的是第一次补搜前的取舍。实现中按稳定身份选择代表，再加一条测试，确认已尝试过该 query 后同题不再补搜。

### 9　保留知识适用条件，让不同 API 模型能接续

**先让紧凑召回完整交付适用范围。** `KnowledgeRecall.search` 已按访问范围检索、留出邻接证据空间，`render_recall(compact=False)` 已带 conditions、provenance、引用和 `dependencies_changed`。严格研究的 `knowledge_background` 使用 4,000 字符紧凑模式，其旧实现会删掉 conditions 和 `dependencies_changed`。前一版内嵌补丁只发第一个条件，仍可把「API v1.x 且 WAL」送成仅在 v1.x 下成立的结论。临时副本也复现了 400 字符截断前保留肯定结论、截断后丢掉例外。新版补丁将完整正文和全部条件作为同一预算单位。[检索与渲染](../../codey/knowledge/recall.py) · [严格模式调用方](../../codey/operations/knowledge_context.py)

1. 紧凑模式只在依赖未变化、短正文和全部条件能整行交付时给正文。已报告条件和 `not_extracted` 都保留。超长、依赖变化或预算不足时给 `id@revision`、状态、依赖变化、条件数和详情标记，引导精确 `knowledge_read`。有兴趣做按任务选条件时先用留出集证明可以保住否定、例外及并列限制，再考虑加入该路径。
2. 存储沿 `KnowledgeRepository.commit` 验 revision 和引用。旧结论仍由原证据解释，新增相反材料创建新版本/反证关系。一个知识对象若出现在两份旧报告里，复用不会增加独立证据数。用户偏好仍归 Ghost；“文档说 v2 废弃 X”归 Knowledge；“这次迁移的文件已验证”归当前运行回执。三者都可以同时呈现在一轮请求，生命周期各自维护。
3. 换模型实施时保留原运行的连接绑定。普通新会话或新轮直接用目标连接重建授权范围内的上下文。需要恢复原运行时沿 `ResumeRun`。需要让另一连接接手同一业务时走第 12 步的显式新运行接力，旧运行先结算在途动作或留下已知的不确定状态。测试记录旧/新请求的实际连接、模型、原执行引用和重复动作数。
4. 跨会话 A→B 与同任务 A→B 分开验收。前者读确认记忆和适用项目知识，后者还要恢复未完成事项、最新回执及副作用状态。小窗裁剪建议顺序为当前更正、未结算要求/未知投递、最近决定性回执及其可读引用、适用偏好与结论限制、其余历史；用目标 tokenizer 实测边界，遇预算不足保留可读 locator。只在安全边界对新请求重新校验已撤销记忆；老模型私有 thinking 或签名不参与业务证明。
5. 建一个三会话例：A 在项目甲确认 Rust 并研究 v1 的兼容条件；B 在甲读 v2 后修代码；C 在甲新会话可说清 v1/v2 差别，示例服从已更正的 Go 偏好。另用项目乙、一次性语言覆盖、无图目标模型、缩小输入窗、删除已有确认做反例。固定协议 fixture 测每个分支，再用至少两种实际可调用的不同模型复测行为，冗余运行时统计独立任务数。命中语言、编译通过、引用版本正确、重复副作用数分别记录。

[版本化知识](../../codey/knowledge/model.py) · [知识提交](../../codey/knowledge/repository.py) · [切换与冷启动](../../codey/operations/conversation_plan.py) · [协议重放](../../codey/providers/replay.py) · [历史跨入口覆盖范围](../ghost_progress/portable-context-production-20261118.md)

#### 如何让 Codey 随使用时间学到更多

显式保存解决了第一天的问题。第十天更重要的是能区分“用户确实纠正了我”“我这次恰好这样回答”“这个项目的做法确实验证过”。建议沿现有事件链扩展三种可验证的行为。

- **同一偏好在不同场景有例外。** 在 `observe_terminal` 保留本轮曝光 item/version、答复、用户纠正的原消息。用 `_feedback` 和 `_review_proposal` 生成有来源、限定 context 的候选，例如“研究报告用中文，面向海外项目的 PR 用英文”。若用户只说“这次写英文”，作用域截至当前任务。自由文本条件不能唯一归类时，显示原话，等待自然纠正或用户确认。先支持一个已有 slot，把两次不同场景的真实请求做到可解释，再增加槽位。
- **采用、有效、满意各自取证。** `answer_behaviors` 现在识别代码围栏语言。某次出现 Rust 可以证明可观察的采用，不能证明编译成功，也不能推断用户满意。项目结果从原验证回执取；满意或“不适用”从明确反馈取。对无反馈的使用保留未评估，避免模型因反复收到自己的旧建议而把偏好越学越强。训练排序前在 `review_due` 的时间窗口核对是否真的发生曝光以及纠正针对哪个版本。
- **让可复用的经验附带适用范围。** `project_repair` 已有修复关系处理。先从“哪个版本在哪项验证失败、什么动作改变了什么文件、哪个新版本通过何种验证”形成候选。下次只在相同项目/技术条件下提示可读出处；若后来验证失败，将经验重新送审。用户说“偏好用某工具”和“本次使用它成功”必须能独立撤销。Codex phase 2 的锁定、领取任务再整理可供参考，Codey 用现有 `MemoryMaintenance` 与 SQLite 事件投影即可做到有界维护。

用同一用户的时间线评分：第一次确认、项目内例外、一次错误推断、后续纠正、跨项目新会话、重启后撤销。分别记录有用记忆采用、已纠正记忆再次出现的比例、需要用户重复说明的次数，以及每次发送中不适用记忆占用的字符。开启/关闭配对之外，再与“总是只放最新一条确认偏好”的简单基线比较，检验逐渐积累的净收益。[终态摄取](../../codey/app/memory_setup.py) · [实际行为特征](../../codey/app/memory_behaviors.py) · [反馈和复盘](../../codey/ghost/learning.py) · [修复经历](../../codey/ghost/repairs.py) · [维护](../../codey/app/memory_maintenance.py) · [Codex 的整理边界](../../reference-projects/codex/codex-rs/memories/write/src/phase2.rs)

### 10　用未声明字段的自然任务连接研究、网页与代码

可以先用一题开发、一题保密验收。开发题只告诉用户目标，例如“我们升级了报价组件，按新版文档修好本项目，然后在预览页确认无折扣和有折扣两种订单都显示正确，告诉我依据”。资料提供旧版和新版；仓库中有两个调用点；预览页含同名按钮和延迟状态。将选择器、期望金额、测试命令、文件路径、版本陷阱与 oracle 只给评分器。若用户明确规定顺序，评分器也检查顺序。

1. **先改评估侧。** `mixed_task` 当前直接说出了工具、控件、文件、字段和两个检查；`run_case` 默认会注入 required tools。自然组关闭夹具代填的 `declare_required_tools`，核查 `additional_required_tools`、bindings、预制模型回复和 source fixture 等其他入口；允许读正常文档、代码与测试。仅给用户自然提出的要求和运行所需权限。比较指令明确组、自然目标组、自然目标加故障组三种，分别保留首次结果。
2. **再改任务视图。** `explicit_web_intent` 和 `build_task_policy_for_entry` 管理授权和用户明示的约束。对“使用适用版规范、完成实现、测试、页面验收”这几个交付项，先按用户原句记录出处，再给模型在任务中选取真实候选。模型推导的子目标作工作提示，不能自行增加持久授权、把自己选的测试提升为唯一通过标准，或删除用户要求。对已有 `required_checks` 可表达的条件复用原合同，开放语义由结论与依据交付。
3. **按数据类型选择绑定。** `copy_target` 有意限制一个 URL、一个目标、明确类型等，并排除若干复合流程；`select_source` 处理抓取的 JSON 指针或表格。结构化数据沿原逻辑走。描述性 API 文档要绑定“passage + 版本条件 + 已读目标文件及版本”，模型提出代码替换，现有版本化 edit 执行。别把任意自然语言强塞进 JSON 字段复制器。搜索摘要仅供发现，可引用知识需要原文；截图或 UI 快照可证明看到了控件，业务结果要匹配任务定义的正式观察。
4. **只从结算事实延伸证据。** `project_result_observation` 投影 source/read/edit/check；`working_context` 在有限预算中展示还缺哪一项、该看哪条回执；`completion_gate.evaluate` 依各自原合同判断。测试结论须关联同项目版本的检查。页面若运行本地服务，要区分旧构建与刚改的版本，可用本题 fixture 的构建标识、重启后的服务结果或具有区分力的业务输入来验证。仅有“同轮点击成功、CI 也绿色”不能把旧页面当作现版本证明。
5. **失效按依赖走。** 外改实现后失效的是该实现及依赖它的测试、预览结果；资料仍可沿原版本使用。页面重载只失效旧 document 的操作引用；已成功写文件的回执仍有效。新版文档改变适用条件时，结论、派生修改与验证都应重查。用户更正某一要求时保留未受影响的事实。第一版用现有版本、document、effect、passage 引用按需计算，只有出现无法表达的边才新增可恢复的绑定字段。
6. 三个断点分别在“读到规范后尚未编辑”“测试失败后换模型”“点击派发后还没有结果”触发。每次独立运行核对文件和受保护文件哈希、两项业务值、原段落、运行版本、动作次数和顺序。最后在新的会话问同一个项目的适用条件，让回答可追溯到知识版本。此题完整成功需要三个领域都过，分别报告首个断裂点，便于归因。

[已声明字段的混合题](../../tools/p0_mixed_live_gate.py) · [夹具注入位置](../../tools/browser_production_live_gate.py) · [受限字段选择](../../codey/operations/browser_copy.py) · [字段与文件的绑定](../../codey/operations/file_field_binding.py) · [进入任务的合同](../../codey/operations/task_entry.py) · [项目验证](../../codey/operations/project_completion_checks.py) · [最终结算](../../codey/operations/completion_gate.py)

## 达到什么结果才算超过参考项目

| 分组 | 固定哪些变量 | 主要指标与失败划分 |
|---|---|---|
| 编码，对 Codex、OpenCode、DeepSeek Harness、Pi | 同仓库树、同用户约束、同模型与额度。标注工具协议、上下文窗口、可用 shell、审批模式及预装依赖。可另报各自默认配置。 | 主指标为整题通过独立测试并满足修改范围、后置动作。附空编辑、无效生成、误报完成、重启恢复、耗时与用量。单个任务通过 10/12 项不能算完成 10 题。 |
| 网页，对 agent-browser 与 agent-desktop 的网页子集 | 同 URL/页面镜像、状态、视口及初始 tab。一轨固定操作意图测底层工具，另一轨同一单模型控制器自由选动作。 | 页面后台业务值、误点、重复派发、动作后等待和人工接续。桌面端可只提供像素/UIA，单列可见信息与平台限制。 |
| 研究，对 HyperResearch | 固定问题、可获取材料和截止版本，另做开放检索组；公开工具/模型调用成本。对方单代理不可配置的流程只能做能力参照，并记录差异。 | 盲评原子断言直接支持率、反例与边界条件覆盖、独立原始研究数、错误肯定率和引用重现。两位评分者可独立按原段落打分并记录分歧。材料不足而如实限定可作为正确结果。 |
| Codey 三域融合与记忆 | 使用开发时未见的任务族，拆开首次自然任务、已知失败前缀恢复、脚本驱动回归。记忆开启/关闭用同一任务和起点，故障断点配对。 | 整题成功、跨域引用能否重建、模型切换后语义正确和副作用次数；另报错误记忆采用、跨项目泄漏、撤销后复活、成本与延迟。专用工具无需独立完成其任务面以外的全链。 |

**执行建议。** 先用少量配对用例验证评分器能识别失败，然后冻结留出任务族、评价规则、样本量与停止条件。每个项目每题保存构建身份、模型与采样、种子、预算、实际请求/回执、文件/页面/报告以及独立评分。先看配对独胜/独败和逐题差异，再给出以任务为单位的不确定区间；同一题的多个种子或多个断言不能当作多个独立任务。声明领先时列出具体维度、比较对象与适用模型。源码未能构建、服务超时、用量缺失分别记未运行、环境失败和未知。仓库已有的七项目启动核对明确标记了同题评测未完成。[七项目准备记录](../../.e2e-artifacts/followup-audit-20261122/seven-project-readiness.json) · [已有跨领域评分入口](../../tests/manual/cross_domain_evaluation.py)

## 上手顺序与验证记录

1. **第一个改动选第 1 节；若想当日合并一个产品缺陷，选第 8 节第 1 步。** 两者边界清楚，均有已定位的反例。与此同时固定自然任务集及评分器，只保存 baseline。
2. 第二个独立改动做请求去重。第三个做编码进展，先上线有来源的诊断，再以实机决定是否启用新的停止条件。二者各用相同基线对照，避免修复收益混在一起。
3. 浏览器等待、显式记忆保存分别验收；随后做目录指令和部分写入诊断。研究条件、紧凑知识回忆与跨模型接续串成一组验收。每个改动的提交都保留一条失败输入、一条反例和需要保持的原始回执。
4. 最后开放自然组合任务。逐阶段记录首次失效位置；至少再换一套材料、目标路径和页面结构。若明确步骤题升分、自然题不变，优先检查用户目标进入工作视图的地方及候选动作展示；若机械验收通过但答案错，转查来源条件与报告的逐断言支持。

本次运行的两个互不重复的定向测试组如下。第一组 44 passed，第二组 22 passed。第一组混合生产入口使用脚本化模型、真实浏览器和实际检查；两组均不提供新模型胜率。

```powershell
python -m pytest -q tests/test_project_patch.py tests/test_followup_recovery.py tests/test_followup_mechanisms.py tests/test_browser_control_continuation.py tests/test_p0_mixed_workflow.py tests/test_research_claim_support_projection.py
python -m pytest -q tests/test_runaway_result_progress.py tests/test_memory_attempt_gate_lifecycle.py tests/test_memory_adaptation_flow.py tests/test_edit_conversation_plan.py tests/durable/test_conversation_plan_no_provider_export.py tests/test_native_coding_context_uses_native_instructions.py
```

另以 20 秒 faulthandler 探针抓到旧 A/B 自测的等待栈，该进程由探针终止；未等满外层 240 秒。独立运行了第 3 节的失败计分探针和第 8 节的发现顺序探针。核对历史首份实际消息时，Codey 的正文两份直接出现，一份内嵌为转义的工作观察 JSON；Harness 的正文在一个 user 消息中出现一次。实机对照数字来自留存工件。本次没有修改产品或测试代码、调用外部 API 模型或运行全量测试。报告用仓库相对链接，一些历史原始工件仅存在这台工作机的忽略目录。





## 逐文件实施手册

以下源码锚点以 `ba3bff82` 为起点。01–05 的补丁和回归测试嵌在对应小节，04–05 已按本轮反例修订。06–13 是实施规格。命令从仓库根目录运行。合并时逐步应用各 diff，分别保留失败样本和测试结果。

### 01 修复研究到编码自测的后台命令等待

**改一个文件。** 在 `tests/manual/research_to_code_ab.py:352` 的 `_run_arm` 找到 `result = _agent_kernel_request(AgentRequest(...))`。保留这个入口，去掉在请求内单独构造的 `WorkspaceRevisionStore(home / "state")`。在临时项目的同级目录创建 `HeadlessAppContext(home / "state", port=0, emit_jsonl=...)`，用 `contextlib.closing` 包住整次调用。传给 `AgentRequest` 的对象应当是下面这一组。

```python
workspace_revision_store=state.workspace_revisions,
durable=state.durable,
managed_outputs=state.managed_outputs,
runtime_mutations=state.runtime_mutations,
provider_id=getattr(provider, "id", "") or "test",
research_context=brief_text,
```

`tests/support/kernel_runtime.py:102 run_agent` 接受外部 durable，会照常 admit、claim、start 父任务。`codey/app/headless_runner.py:106` 已启动只消费工具任务的 scheduler。两者指向同一套存储，子命令才能结算；A/B 的 provider 仍由实验注入。在 `_self_test` 把内联构造的 provider 存为 `scripted`，断言 `briefs[arm] in scripted.prompts[0]`，然后执行原有公式、诱饵、独立 pytest 和 stop_reason 断言。这样能抓住两臂误收同一份上下文的错误。

<details>
<summary>逐行补丁 01（展开查看替换行）</summary>

```diff
diff --git a/tests/manual/research_to_code_ab.py b/tests/manual/research_to_code_ab.py
index 3c8c1180..55fec24e 100644
--- a/tests/manual/research_to_code_ab.py
+++ b/tests/manual/research_to_code_ab.py
@@ -37,6 +37,7 @@ if __package__ in (None, ""):
 import contextlib

 from codey.agents.request import AgentRequest
+from codey.app.headless_runner import HeadlessAppContext
 from codey.knowledge.brief import KnowledgeBriefBuilder
 from codey.knowledge.note import KnowledgeNote
 from codey.knowledge.store import KnowledgeStore
@@ -44,7 +45,6 @@ from codey.providers import controls as provider_controls
 from codey.providers.registry import DEFAULT_PROVIDER_ID, connect_provider, provider_ids
 from codey.runtime.observe.events import RunEvent, render_run_event
 from codey.utils.text_budget import clip_middle
-from codey.workspace.revision import WorkspaceRevisionStore
 from tests.manual import ab_harness_common as common
 from tests.manual.ab_harness_common import (
     ArmRunLayout,
@@ -371,17 +371,23 @@ def _run_arm(
         root.mkdir()
         _write_case(root)
         prompts_before = len(provider.prompts)
-        result = _agent_kernel_request(AgentRequest(
-            provider=provider,
-            project=root,
-            task=TASK,
-            workspace_revision_store=WorkspaceRevisionStore(home / "state"),
-            max_turns=max_turns,
-            on_event=events.append,
-            fresh_chat=True,
-            provider_id=getattr(provider, "id", ""),
-            research_context=brief_text,
-        ))
+        with contextlib.closing(HeadlessAppContext(
+            home / "state", port=0, emit_jsonl=lambda _event: None,
+        )) as state:
+            result = _agent_kernel_request(AgentRequest(
+                provider=provider,
+                project=root,
+                task=TASK,
+                workspace_revision_store=state.workspace_revisions,
+                durable=state.durable,
+                managed_outputs=state.managed_outputs,
+                runtime_mutations=state.runtime_mutations,
+                max_turns=max_turns,
+                on_event=events.append,
+                fresh_chat=True,
+                provider_id=getattr(provider, "id", "") or "test",
+                research_context=brief_text,
+            ))
         elapsed = round(time.time() - started, 3)
         changed = _changed_files(root)
         independent_check = _run_process(root, (sys.executable, "-B", "-m", "pytest", "-q"))
@@ -719,13 +725,15 @@ def _self_test() -> None:
     )
     case = CASES[0]
     for arm in ARMS:
+        scripted = _ScriptedProvider(*replies)
         row = _run_arm(
-            _ScriptedProvider(*replies),
+            scripted,
             case,
             arm,
             max_turns=6,
             briefs=briefs,
         )
+        assert scripted.prompts and briefs[arm] in scripted.prompts[0], arm
         assert row["stop_reason"] == "done", row
         assert row["key_conclusion_applied"], row
         assert not row["trap_misused"], row
```

</details>

```powershell
python -B tests/manual/research_to_code_ab.py --self-test
python -m pytest -q tests/test_research_to_code_ab.py
```

### 02 修复独立研究反证被较晚的同源段落覆盖

**改两个文件。** 在 `codey/research/evidence_gaps.py:72 _comparison_gaps` 改三个相连位置。

| 找到的现有代码 | 替换方式 |
|---|---|
| `dict[int, str]` | 改为 `dict[int, set[str]]`；外层 cohort 继续使用 question、四个比较字段和条件。 |
| `directional.setdefault(cohort, {})[direction_value] = passage` | 改为 `directional.setdefault(cohort, {}).setdefault(direction_value, set()).add(passage)`。 |
| `refs = tuple(reports.values())` 到 `yield ResearchGap(...)` | 取两方向 passage 的并集，按 ref 排序，**一次性**调用 `same_source_groups` 得到完整的确定同源组；单例以自身 ref 作键。按稳定顺序找一对方向相反且组键不同的 ref，传给 `ResearchGap.passage_refs`。 |

在 `tests/test_followup_mechanisms.py` 新增 `test_conflict_search_keeps_independent_opposition_in_every_order`。输入甲 lower、乙 higher、甲另一段 higher，六种排列都应返回含甲 lower 和乙 higher 的补搜候选；只留下甲的两段，应返回 `None`。从公开的 `conflict_search` 测，覆盖下游取第一条 gap 的行为。

这份补丁的验收范围是**同一组内**的反证选择。将两个成立的比较组换序，临时副本仍会选先插入的组。另一个反例是同一对象和指标下的 10 L 与 100 L，两组生成相同 query，第一次查完后第二组被当成已尝试。把跨组选择留到 11A，用有效问题顺序和稳定组键排序，并把已报告的区分条件带入补搜。总搜索额度沿现有宿主预算。额度用完时，剩余组仍可在报告中标为未查。

<details>
<summary>逐行补丁 02（展开查看替换行）</summary>

```diff
diff --git a/codey/research/evidence_gaps.py b/codey/research/evidence_gaps.py
index 7b06e948..32f2434c 100644
--- a/codey/research/evidence_gaps.py
+++ b/codey/research/evidence_gaps.py
@@ -73,7 +73,7 @@ def _comparison_gaps(ledger: ResearchLedger, findings: Iterable[ToolCall]) -> It
     """Compare explicit directions only for the same reported study conditions."""
     from codey.knowledge.model import canonical_json

-    directional: dict[tuple[str, tuple[str, ...], tuple[str, ...]], dict[int, str]] = {}
+    directional: dict[tuple[str, tuple[str, ...], tuple[str, ...]], dict[int, set[str]]] = {}
     for call in findings:
         comparison = canonical_json_object(call.args.get("comparison", {}))
         fields = tuple(" ".join(str(comparison.get(key, "")).casefold().split())
@@ -90,16 +90,21 @@ def _comparison_gaps(ledger: ResearchLedger, findings: Iterable[ToolCall]) -> It
                 or not isinstance(question, str) or not isinstance(conditions, list)):
             continue
         cohort = (question, fields, tuple(sorted(canonical_json(condition) for condition in conditions)))
-        directional.setdefault(cohort, {})[direction_value] = passage
+        directional.setdefault(cohort, {}).setdefault(direction_value, set()).add(passage)
     for (question, fields, _conditions), reports in directional.items():
         if set(reports) != {-1, 1}:
             continue
-        refs = tuple(reports.values())
-        aliases = tuple((f"comparison-{index}", ref) for index, ref in enumerate(refs, 1))
-        if same_source_groups(ledger.read_passages, aliases):
-            continue
-        yield ResearchGap(question, " ".join(fields), "conflicting_findings", refs,
-                          "independent evidence under the same reported conditions")
+        refs = sorted(reports[-1] | reports[1])
+        groups = same_source_groups(ledger.read_passages, tuple((ref, ref) for ref in refs))
+        source = {ref: group[0] for group in groups for ref in group}
+        # A singleton's passage ref is its source key. Compare only independent pairs.
+        higher = sorted(reports[1])
+        for lower in sorted(reports[-1]):
+            other = next((ref for ref in higher if source.get(ref, ref) != source.get(lower, lower)), None)
+            if other is not None:
+                yield ResearchGap(question, " ".join(fields), "conflicting_findings", (lower, other),
+                                  "independent evidence under the same reported conditions")
+                break


 def conflict_search(ledger: ResearchLedger, *, remaining_searches: int,
diff --git a/tests/test_followup_mechanisms.py b/tests/test_followup_mechanisms.py
index d32a84e8..bc55e8d7 100644
--- a/tests/test_followup_mechanisms.py
+++ b/tests/test_followup_mechanisms.py
@@ -2,6 +2,7 @@ from __future__ import annotations

 from collections.abc import Iterator
 from dataclasses import replace
+from itertools import permutations
 from pathlib import Path

 import pytest
@@ -120,6 +121,38 @@ def test_legacy_submission_diagnostics_survive_signed_calculation_support() -> N
     assert isinstance(reviews, list) and decode_json_object(reviews[0])["quantity"] == "-8 kg"


+def test_conflict_search_keeps_independent_opposition_in_every_order() -> None:
+    ledger = ResearchLedger()
+    calls: list[ToolCall] = []
+    for ref, origin, direction in (
+        ("a", "first", "lower"),
+        ("b", "second", "higher"),
+        ("c", "first", "higher"),
+    ):
+        url = f"https://{origin}.example/paper"
+        source = OpenedSource(url, url, "", "", ref)
+        ledger.read_passages[ref] = ReadPassage(
+            ref, source, direction, (), 0, len(direction)
+        )
+        calls.append(ToolCall("save_finding", {
+            "question_ref": "q1",
+            "passage_ref": ref,
+            "conditions": [],
+            "comparison": {
+                "subject": "Atlas", "factor": "A", "comparator": "B",
+                "endpoint": "fuel", "direction": direction,
+            },
+        }))
+
+    for ordered in permutations(calls):
+        found = conflict_search(ledger, remaining_searches=1, findings=ordered)
+        assert found is not None, [call.args["passage_ref"] for call in ordered]
+        refs = found["passage_refs"]
+        assert isinstance(refs, list) and "a" in refs and "b" in refs
+
+    assert conflict_search(ledger, remaining_searches=1, findings=(calls[0], calls[2])) is None
+
+
 def test_conflict_search_requires_a_passage_and_a_remaining_slot() -> None:
     ledger = ResearchLedger()
     ledger.questions = ("Atlas fuel consumption at the documented 10 L workload",)
```

</details>

```powershell
python -m pytest -q tests/test_followup_mechanisms.py
```

### 03 消除当前请求中人为添加的重复任务正文

**改三个生产文件和一个测试文件。**

1. `codey/agents/context.py:92 build_agent_context` 加关键字参数 `include_user_task: bool = True`。把 `request_context` 改为先拼 `workspace_intro + context_block`，仅当开关为真才追加 `f"User task:\n{request_text}"`。同一个条件控制 `PromptEnvelopeSection.source_refs` 中的 `"request:user_task"`。记忆 span 按实际前缀计算，删除的是尾部任务块。
2. `codey/operations/project_adapter.py:41 _project_context` 调用上述函数时显式传 `include_user_task=False`。`project_adapter.run` 和 `task_entry.py:290 _entry_context` 都经这个包装器；调用处无需分别加开关。
3. `codey/operations/kernel_prompt.py:628 working_context`，删除返回 JSON 的 `user_task=session.task_text` 项。保留 `kernel_prompt_for_session` 的 `User task (verbatim)`，以及策略判断所需的 `session.task_text`。
4. `tests/test_kernel_prompt_task_guidance_ownership.py` 添加带唯一文本的项目请求，分别传 `native=True/False`。检查 workspace、工作观察和最终拼好的 prompt 三处。此测试覆盖装配层；要测实际 token 收益，再从冻结的真实请求解码观察 JSON，分开统计新装配与历史用户消息。



<details>
<summary>逐行补丁 03（展开查看替换行）</summary>

```diff
diff --git a/codey/agents/context.py b/codey/agents/context.py
index b163c5d1..650f0d5e 100644
--- a/codey/agents/context.py
+++ b/codey/agents/context.py
@@ -104,6 +104,7 @@ def build_agent_context(
     work_checkpoint: str,
     completion_repair_context: str,
     memory_sources: tuple[ContextCandidate, ...] = (),
+    include_user_task: bool = True,
 ) -> AgentContext:
     def intro_source(
         key: str,
@@ -202,11 +203,9 @@ def build_agent_context(
     context = rendered_context.text
     context_block = f"{context}\n\n" if context else ""
     workspace_intro = "Project workspace: use paths relative to the project root.\n"
-    request_context = (
-        workspace_intro +
-        f"{context_block}"
-        f"User task:\n{request_text}"
-    )
+    request_context = workspace_intro + context_block
+    if include_user_task:
+        request_context += f"User task:\n{request_text}"
     rendered = PromptEnvelope((
         PromptEnvelopeSection(
             name="coding_system_prompt",
@@ -222,7 +221,7 @@ def build_agent_context(
             freshness="run_start",
             source_refs=(
                 "project:workspace",
-                "request:user_task",
+                *(("request:user_task",) if include_user_task else ()),
                 *(context_source_ref(source.key) for source in rendered_context.sources),
             ),
             budget=sum(source.budget for source in rendered_context.sources),
diff --git a/codey/operations/kernel_prompt.py b/codey/operations/kernel_prompt.py
index a177ebca..31164276 100644
--- a/codey/operations/kernel_prompt.py
+++ b/codey/operations/kernel_prompt.py
@@ -688,7 +688,7 @@ def working_context(session: TaskSession) -> str:
                            "This obligation list does not replace the task's prerequisites or ordering. "
                            "Obtain source evidence before copying its fields; verify requested changes after editing. ")
     operations = operation_receipts(session)
-    return ("Current work observations (runtime records):\n" + json.dumps(dict[str, object](user_task=session.task_text,
+    return ("Current work observations (runtime records):\n" + json.dumps(dict[str, object](
         project=session.project, workspace_revision=session.workspace_revision,
         workspace_fingerprint=session.workspace_fingerprint, changed_files=list(session.edited_files),
         read_files=sorted(session.read_files),
diff --git a/codey/operations/project_adapter.py b/codey/operations/project_adapter.py
index b9106339..084b774f 100644
--- a/codey/operations/project_adapter.py
+++ b/codey/operations/project_adapter.py
@@ -50,6 +50,7 @@ def _project_context(request: AgentRequest, *, memory_sources: tuple[ContextCand
     rendered = build_agent_context(
         project=request.project,
         request_text=request.task,
+        include_user_task=False,
         system_prompt_text="",
         profile=profile_for_name(request.permission_profile),
         list_directory=tool_fns.list_directory,
diff --git a/tests/test_kernel_prompt_task_guidance_ownership.py b/tests/test_kernel_prompt_task_guidance_ownership.py
index cb2e79de..50ca4174 100644
--- a/tests/test_kernel_prompt_task_guidance_ownership.py
+++ b/tests/test_kernel_prompt_task_guidance_ownership.py
@@ -245,3 +245,18 @@ def test_request_scoped_contract_prompt_does_not_offer_ordinary_prose_or_duplica
     assert "FROZEN DECLARATIONS" not in text
     assert "Deliver the final answer with done" in text
     assert "as normal text" not in text and "Answer directly" not in text
+
+@pytest.mark.parametrize("native", [False, True])
+def test_project_task_occurs_once_in_assembled_prompt(tmp_path: Path, native: bool) -> None:
+    from codey.agents.request import AgentRequest
+    from codey.operations.project_adapter import _project_context
+
+    task = "TASK_UNIQUE_09c4 read the project"
+    workspace = _project_context(AgentRequest(project=tmp_path, task=task, provider=object()))
+    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), task_text=task)
+    prompt = kernel_prompt.kernel_prompt_for_session(
+        session, tool_names=(), context_text=workspace, native=native,
+    )
+    assert task not in workspace
+    assert task not in kernel_prompt.working_context(session)
+    assert prompt.count(task) == 1
```

</details>

```powershell
python -m pytest -q tests/test_kernel_prompt_task_guidance_ownership.py tests/test_kernel_prompt_rendering_has_no_side_effects.py
```

### 04 在严格研究的紧凑知识召回里保住适用条件

**改两个文件。** 在 `codey/knowledge/recall.py::render_recall` 的紧凑分支，始终带 `dependencies_changed` 和 `condition_count`。依赖未变化时，只在完整正文不超过 400 字且所有条件能与正文一同放进预算时交付正文。依赖变化、正文过长或预算不足时，降为版本定位行，保留可读详情标记。定位行也放不下则跳过。模型可通过精确的 `knowledge_read` 读取原对象及其全部条件。

`tests/test_knowledge_multilingual_recall.py` 同时使用版本、日志模式、未提取工作负载三个条件。测试整行能放下、差一个字符、依赖变更、正文末尾有例外及预算小于固定说明。合并前再从严格研究生产入口抓实际请求，确认短预算仍给正确的 `id/revision`，读详情沿旧路径可达。

<details>
<summary>逐行补丁 04（展开查看替换行）</summary>

```diff
diff --git a/codey/knowledge/recall.py b/codey/knowledge/recall.py
index 54736421..fde5039c 100644
--- a/codey/knowledge/recall.py
+++ b/codey/knowledge/recall.py
@@ -164,6 +164,8 @@ def render_recall(hits: list[KnowledgeHit], *, max_chars: int = 6000, include_an
         lines.append("This summary is a locator. Use knowledge_read with the exact id and revision. "
                      "Source/evidence reads enter the original passage citation channel. "
                      "An old claim or answer never substitutes for reading its original evidence.")
+    if compact and len("\n".join(lines)) > max_chars:
+        return ""
     for hit in hits:
         version, value = hit.version, hit.version.value
         # Clip only at presentation, leaving full content and provenance in SQLite.
@@ -176,10 +178,20 @@ def render_recall(hits: list[KnowledgeHit], *, max_chars: int = 6000, include_an
                "provenance": canonical_json_object(provenance), "references": [
                    {"target": r.target, "revision": r.revision, "relation": r.relation} for r in value.references]}
         if compact:
-            row = {key: row[key] for key in ("id", "revision", "kind", "status", "reason", "interpretation_review")}
-            row.update(content=value.content[:400], display_truncated=True, detail_available=True)
+            row = {key: row[key] for key in (
+                "id", "revision", "kind", "status", "reason", "interpretation_review", "dependencies_changed",
+            )}
+            row.update(detail_available=True, condition_count=len(value.conditions))
+            if not version.dependencies_changed and len(value.content) <= 400:
+                row.update(content=value.content, conditions=canonical_json_object(conditions))
+        remaining = max_chars - len("\n".join(lines)) - 1
         text = canonical_json(row)
-        if len("\n".join(lines)) + len(text) + 1 > max_chars:
+        if compact and len(text) > remaining:
+            # Keep a locator when the conclusion and its condition cannot travel together.
+            text = canonical_json({key: row[key] for key in (
+                "id", "revision", "status", "dependencies_changed", "condition_count", "detail_available",
+            )})
+        if len(text) > remaining:
             continue
         lines.append(text)
     return "\n".join(lines)
diff --git a/tests/test_knowledge_multilingual_recall.py b/tests/test_knowledge_multilingual_recall.py
index eabe6c3a..721fc264 100644
--- a/tests/test_knowledge_multilingual_recall.py
+++ b/tests/test_knowledge_multilingual_recall.py
@@ -1,3 +1,4 @@
+import json
 from dataclasses import replace

 from codey.knowledge.model import KnowledgeMutation as M
@@ -123,3 +124,33 @@ def test_oversized_recall_row_does_not_hide_later_small_candidate(tmp_path):
         assert '"id":"large"' not in text
     finally:
         store.close()
+
+def test_compact_recall_keeps_complete_conditions_or_only_a_locator(tmp_path) -> None:
+    store = KnowledgeStore(tmp_path)
+    try:
+        value = populate(store)
+        version = store.repository.read(value.id)
+        assert version is not None
+        limited = replace(value, conditions={
+            "api_version": {"state": "reported", "value": "1.x"},
+            "journal_mode": {"state": "reported", "value": "WAL"},
+            "workload": {"state": "not_extracted"},
+        })
+        hit = KnowledgeHit(replace(version, value=limited), "test")
+        text = render_recall([hit], compact=True)
+        row = json.loads(text.splitlines()[-1])
+        assert row["conditions"] == limited.conditions and row["condition_count"] == 3
+        assert row["content"] == limited.content
+        for candidate in (
+            hit,
+            replace(hit, version=replace(hit.version, dependencies_changed=True)),
+            replace(hit, version=replace(hit.version, value=replace(limited, content="x" * 400 + " except after failover"))),
+        ):
+            layout = render_recall([candidate], compact=True, max_chars=len(text) - 1)
+            locator = json.loads(layout.splitlines()[-1])
+            assert locator["id"] == value.id and locator["revision"] == version.revision
+            assert locator["detail_available"] is True and "content" not in locator
+            assert locator["dependencies_changed"] is candidate.version.dependencies_changed
+        assert render_recall([hit], compact=True, max_chars=1) == ""
+    finally:
+        store.close()
```

</details>

```powershell
python -m pytest -q tests/test_knowledge_multilingual_recall.py
```

### 05 浏览器已等待过同一点击时从新观察接续

**改两个文件。** 在 `browser_task_handoff.py` 中用 `_settled_post_click_wait()` 从回执定位点击、等待基线、等待终态与之后的快照。要求同一 document，且顺序为已派发点击 → 完整观察的基线引用 → 该引用的等待。只把有结构化匹配或超时事实的结果计为已等待。等待后若还没观察，建议一次 snapshot。已有新观察就交付新控件或当前结果，即使 ref 更新，也不为同一点击再建议一次通用等待。用户明示的下一条件继续用既有要求路径。

在 `tests/test_browser_text_change_wait.py` 加入快照 A → 控件等待超时 → 内容相同的快照 B，及等价的文字等待案例。验收要求 A 后给 snapshot，B 后给当前观察且无新自动等待建议。已有匹配、超时、新控件测试继续覆盖。合并前用实际 Chromium 补测新点击会重新开启通用等待，旧文档、取消、绑定错误和未执行拒绝不会被误计成已等待。第 07 步再改选择谓词和轮询口径。

<details>
<summary>逐行补丁 05（展开查看替换行）</summary>

```diff
diff --git a/codey/operations/browser_task_handoff.py b/codey/operations/browser_task_handoff.py
index 0ddce59e..c313adde 100644
--- a/codey/operations/browser_task_handoff.py
+++ b/codey/operations/browser_task_handoff.py
@@ -392,6 +392,29 @@ def browser_observation_handoff(session: TaskSession, snapshot: TurnSnapshot, co
     return "\n".join(lines)


+def _settled_post_click_wait(
+    records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
+    *, click_ref: str, current_ref: str, document: JsonObject | None,
+) -> tuple[str, str, bool] | None:
+    order = {identity: index for index, identity in enumerate(records)}
+    baselines = {ref for ref, result in committed_results(records, results, frozenset({"browser_snapshot"}))
+                 if order[click_ref] < order[ref] <= order[current_ref]
+                 and same_document(result.audit.get("browser_document"), document)}
+    for ref, wait in reversed(committed_results(records, results, frozenset({"browser_wait"}),
+                                                include_failed=frozenset({"browser_wait"}))):
+        baseline = wait.call.args.get("value")
+        outcome = wait.canonical.get("value")
+        if (wait.call.args.get("condition") not in {"text_changed", "controls_changed"}
+                or not isinstance(baseline, str) or baseline not in baselines or order[ref] <= order[baseline]
+                or not same_document(wait.audit.get("browser_document"), document) or not isinstance(outcome, dict)):
+            continue
+        matched = wait.ok and outcome.get("matched") is True
+        timed_out = not wait.ok and outcome.get("matched") is False and outcome.get("reason") == "timeout"
+        if matched or timed_out:
+            return ref, baseline, order[ref] < order[current_ref]
+    return None
+
+
 def browser_control_wait_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                                  generation: int | None) -> tuple[TurnSnapshot, str]:
     """Offer a read-only continuation when the next control may appear later."""
@@ -410,13 +433,11 @@ def browser_control_wait_handoff(session: TaskSession, snapshot: TurnSnapshot, c
             or not same_document(action.audit.get("browser_document"), context.browser_document)
             or tuple(records).index(ref) >= tuple(records).index(current[0])):
         return snapshot, ""
-    waits = [(key, row) for key, row in committed_results(records, results, frozenset({"browser_wait"}),
-                                                        include_failed=frozenset({"browser_wait"}))
-             if row.call.args.get("condition") == "text_changed" and row.call.args.get("value") == current[0]
-             and tuple(records).index(key) > tuple(records).index(current[0])]
-    if waits:
+    wait = _settled_post_click_wait(records, results, click_ref=ref, current_ref=current[0],
+                                    document=context.browser_document)
+    if wait is not None and not wait[2]:
         snapshot, observe = _call_handoff(session, snapshot, ToolCall("browser_snapshot", {}))
-        return snapshot, f"Wait {waits[-1][0]} from snapshot {current[0]} already settled. Observe its current page.\n" + observe
+        return snapshot, f"Wait {wait[0]} from snapshot {wait[1]} already settled. Observe its current page.\n" + observe
     before = [(key, row) for key, row in committed_results(records, results, frozenset({"browser_snapshot"}))
               if tuple(records).index(key) < tuple(records).index(ref)]
     references = snapshot.browser_references
@@ -442,7 +463,11 @@ def browser_control_wait_handoff(session: TaskSession, snapshot: TurnSnapshot, c
                 "Use the current observation to choose remaining work or report the observed outcome. "
                 "A visible change alone does not verify the business result.")
     except ValueError:
-        return snapshot, ""
+        if wait is None:
+            return snapshot, ""
+    if wait is not None:
+        return snapshot, (f"Wait {wait[0]} already settled after click {ref}. Snapshot {current[0]} was taken afterwards. "
+            "Use this observation to decide the next required step; a new snapshot reference alone does not require repeating the wait.")
     snapshot, offered, _error = handoff_call(session, snapshot, ToolCall("browser_wait", {
         "condition": "text_changed", "value": current[0], "timeout_ms": 5000}))
     if offered is None:
diff --git a/tests/test_browser_text_change_wait.py b/tests/test_browser_text_change_wait.py
index 80fe221e..8c6f6e49 100644
--- a/tests/test_browser_text_change_wait.py
+++ b/tests/test_browser_text_change_wait.py
@@ -207,7 +207,8 @@ def test_navigation_comparison_preserves_visible_changes_and_arbitrary_program_v


 @pytest.mark.parametrize("transition", ["new_control", "enabled_control", "ref_churn", "still_loading", "text_progress",
-                                       "wait_timeout", "wait_matched"])
+                                       "wait_timeout", "wait_matched", "controls_timeout", "controls_matched",
+                                       "controls_timeout_recaptured", "wait_timeout_recaptured"])
 def test_control_wait_is_not_offered_after_the_next_control_is_already_observed(transition: str) -> None:
     from codey.operations.browser_task_handoff import browser_control_wait_handoff

@@ -234,19 +235,31 @@ def test_control_wait_is_not_offered_after_the_next_control_is_already_observed(
     observation = replace(initial, canonical={"value": current})
     session._memory_results["current"] = observation
     project_execution_record(session, "current", observation.call, observation, ok=True)
-    if transition in {"wait_timeout", "wait_matched"}:
-        waited = ToolResult(ToolCall("browser_wait", {"condition": "text_changed", "value": "current", "timeout_ms": 5000}),
-            "Wait settled", ok=transition == "wait_matched", audit={"browser_document": document})
+    generation = 2
+    if transition.startswith(("wait_", "controls_")):
+        condition = "controls_changed" if transition.startswith("controls_") else "text_changed"
+        matched = transition.endswith("_matched")
+        outcome: JsonObject = {"matched": matched} if matched else {"matched": False, "reason": "timeout"}
+        waited = ToolResult(ToolCall("browser_wait", {"condition": condition, "value": "current", "timeout_ms": 5000}),
+            "Wait settled", ok=matched, canonical={"value": outcome}, audit={"browser_document": document})
         session._memory_results["wait"] = waited
         project_execution_record(session, "wait", waited.call, waited, ok=waited.ok)
+        if transition.endswith("_recaptured"):
+            generation = 3
+            recaptured = replace(initial, canonical={"value": {**current, "generation": generation}})
+            session._memory_results["fresh"] = recaptured
+            project_execution_record(session, "fresh", recaptured.call, recaptured, ok=True)
     context = CompletionContext(browser_document=document)
-    snapshot = bind_browser_references(session, offer_browser_tools(build_turn_snapshot(session), session), context, 2)
-    _snapshot, hint = browser_control_wait_handoff(session, snapshot, context, 2)
+    snapshot = bind_browser_references(session, offer_browser_tools(build_turn_snapshot(session), session), context, generation)
+    _snapshot, hint = browser_control_wait_handoff(session, snapshot, context, generation)
     assert ('"condition": "text_changed"' in hint) is (transition == "ref_churn")
     if transition in {"new_control", "enabled_control"}:
         assert "Continue" in hint and "already" in hint
-    elif transition in {"wait_timeout", "wait_matched"}:
+    elif transition in {"wait_timeout", "wait_matched", "controls_timeout", "controls_matched"}:
         assert '"tool": "browser_snapshot"' in hint and "already settled" in hint
+    elif transition.endswith("_recaptured"):
+        assert "already settled" in hint and "Snapshot fresh" in hint
+        assert '"tool": "browser_wait"' not in hint and '"tool": "browser_snapshot"' not in hint


 @pytest.mark.parametrize("native", [False, True])
```

</details>

```powershell
python -m pytest -q tests/test_browser_text_change_wait.py -k control_wait_is_not_offered
```

### 06 根据编码失败的真实变化判断进展

建议拆两次提交。第一份只发布诊断，比较提前提示是否减少空编辑；第二份才改变停止判定。第 03 步的去重收益可以独立测量。

| 编辑位置 | 应写的逻辑和接线 |
|---|---|
| `codey/operations/tool_tasks.py:54 owned_tool_task`、`:109 tool_execution_result` 附近 | 新增只读 `settled_check_for_progress(durable, caller_result, *, owner_task_id, store)`。入口形状有三种：终态直返的 `canonical["tool_task"]`、`task_output` 的 `canonical["value"]`、只有 `kind="accepted"` 的接单。前两种核对 task_ref、原 owner 和 execution_ref，再恢复真实 child result；第三种只记录 pending。错 owner 或缺终态回执返回“不可比较”。 |
| `codey/runtime/core/execution_receipt.py:126 execution_receipt`、`codey/operations/kernel_facts.py:169 _record_run_fact` | 复用命令退出状态和结果里的 workspace_identity。新函数只接受来自已选项目检查的 `run`，不对任意 shell、TTY、输入或取消操作作等价归并。命令从原 child call 提取，cwd 用其 `path`。旧记录缺执行时 fingerprint 时返回不可比较。 |
| `codey/operations/progress_comparison.py::ProgressComparison`、`progress_comparison` | 建议给结果增加可选 `check_key`、`failure_ids`、`execution_ref`，历史浏览器路径默认空。比较内容按 `(command,cwd,workspace_fp,terminal_state,exit_code,sorted_failure_ids)` 序列化；execution_ref 用于找原日志。第一版只支持可校验的完整 pytest 终态摘要，处理 FAILED 与 ERROR，集合数与终态总数对不上、输出截断或 runner 未识别都回退。读新输出页仍按原页指纹比较。 |
| `codey/operations/kernel_context.py:829 observe_progress` | 在持有 durable/sink 的位置为本次 delivery batch 解析上述事实，传给 `KernelProgress.observe(..., check_facts=...)`。按本批 result/原引用匹配，不按工具名全局寻找“最近一次 run”。不得重复执行命令来补日志。 |
| `codey/operations/kernel_progress.py:95 observe` | 将 `changed or (record.ok and ...)` 拆为原分支与“可信终态检查”。后者按修复阶段内的新比较键计进展；首次取得某个实际执行的终态可观察，但新启动同版本同失败检查不会持续重置 idle。空编辑不推进 edit epoch。识别 `edit(no change) → run/accepted → task_output(same failure)`，保留批次幂等和原始回执。 |
| `codey/operations/kernel_progress.py:49 stopped`、`:71 repeat_feedback`；`kernel_context.py:841 deliver_results` | 用已确认的编码循环签名及原 result_ref 生成提示，列出未改文件、失败标识、读取位置。第一批确认循环允许正常投递一次提示，检查 cancellation 和 max_turns 仍先行；同一签名在后续批次继续重复才适用停止。给编码单独文案，通用 `effect_cycle` 现会走浏览器措辞。 |
| `codey/runtime/core/progress.py:20 ProgressSnapshot` | 如需持久化提示，引入 v3 并同时改 `to_payload/from_payload`、`KernelProgress.__init__/snapshot`。建议存 `warned_check_cycle`、`warning_batch_id`，从结算批次派生提示。重启在同一批重建同一投递，不能把“已产生建议”误当成模型已收到建议；投递成功按已有 delivery 边界确认。读 v1/v2 默认空，保留之前的 repair rejection 计数。 |

**要写的测试。** 在 `tests/test_runaway_result_progress.py` 邻近增加 kernel 层用例。准备 command、cwd、fingerprint 相同的两个完整失败回执，execution_ref 不同，断言比较键相同；分别只变失败集合、cwd、文件指纹应不同。另给两个不同 stdout cursor、缺退出码、失败读取、重复 delivery_batch_id 和 A→B→A 波动失败。再从原 SQLite 重放一次空编辑循环，看下一冻结请求有没有诊断且原日志可读。退出 1 的新失败可以带来新信息，只有退出 0 的当前版本验证可以满足完成检查。运行 `python -m pytest -q tests/test_runaway_result_progress.py tests/test_p0_mixed_workflow.py`。

### 07 把浏览器的等待条件和快照口径接起来

1. **调度。** 改 `task_handoff.py::task_handoff` 与 `browser_task_handoff.py::browser_control_wait_handoff`。当前两条交接依次追加提示，同一轮可能同时出现通用和显式等待。先由 `next_browser_step()` 确定本阶段已声明要求和匹配回执，并将已算结果传给通用交接。显式等待存在时，只由原执行路径提供对应谓词。下一控件已出现就展示候选，否则自然目标仍待控件动作时选择 `controls_changed`。仅需通用文本观察时保留文字路径。集成测试断言一轮只给一个当前谓词，同时确认文字等待与控件等待的回执各自匹配，且不启动额外文件 I/O。
2. **基线。** `browser_control_wait_handoff` 现把控件比较及两次 `snapshot_text_baseline` 放在同一个 `try`。拆开它们。控件分支调用 `snapshot_wait_baseline(session,current_ref,"controls_changed")`，允许正文截断而控件完整；文字分支继续要求全文完整。上一快照属于不同 document、局部 selector 或缺少 owner 时，不生成跨文档等待。第 05 步负责等待结束后的去重。
3. **观察与轮询。** 改 `codey/automation/browser_snapshot.py:167 collect_snapshot`。把 DOM 与 AX 合并过程里生成 role、name、区域、disabled 的部分抽成私有异步读函数；入参是 FrameContext、root、max_elements，输出行与“控件是否完整”。快照消费端另行 `observe_node` 并拥有保留的 handle；等待消费端只取比较键并在 finally 释放本轮新 handle。保留 AX-only 节点和 DOM-only 节点。已有 `browser_observation.py:ELEMENT_METADATA` 会处理 fieldset 的 `:disabled`，直接复用。
4. **比较。** 改 `codey/automation/browser_runtime.py:744 _control_baseline` 和 `:755 _wait` 的 `controls_changed` 分支。两边调用同一个纯函数 `available_control_counts(rows)`，建议键为规范化的 `(role,name,区域标识)`；区域标识采用同文档可稳定对照的 owner identity，缺失时用可用区域标签再计数。丢弃 disabled 行；不把 ref、generation 计入键。保持“某一可用组数量增加”的定义。采样截断时未知，不能据此声称条件满足。轮询前后检查 `same_document`、timeout 与取消，避免为每次轮询生成正式快照。
5. **输入与诊断。** `codey/automation/browser_input.py:_HIT_TEST` 的 `blocked(hit)` 补命中点和 frame 内坐标；`browser_runtime.py:654 _input` 把目标 ref、当前 document 和等待失败/投递状态一起返回。祖先 iframe 遮挡记录实际受阻层。沿 `browser_references.py:203 reference_failure` 将诊断投递给下一轮。已派发/未知时恢复观察；焦点跳到新 tab 时从原 target 重新确认。业务完成继续由正式读取或用户要求的可观测值判断。
6. **测试。** 扩展 `tests/test_browser_text_change_wait.py`，区分明确文字等待、只启用控件、文字截断、控件截断。给 `tests/test_browser_control_continuation.py` 增加 A/B 两区同名按钮交换可用性的网页，服务端记录点击次数。在 `tests/test_browser_async_frames.py` 覆盖 frame 替换、祖先遮挡、worker 恢复。按回执检查实际顺序、选中的区域、点击次数、超时后一份新观察，以及业务完成的独立证据。运行 `python -m pytest -q tests/test_browser_text_change_wait.py tests/test_browser_control_continuation.py tests/test_browser_async_frames.py`。

### 08 把显式记忆保存接到用户消息、Ghost 和前端

按“先有能直接验证的 API，再接 UI，最后改检索”提交。这样模型即使没有返回 `memory_candidates`，用户仍能完成保存。

| 顺序与文件 | 找到的现有符号 | 具体修改 |
|---|---|---|
| 8.1　`codey/app/memory_routes.py:208` | `memory_action` 的现有会话检查及 `action=="propose"` 路由 | 在会话校验之后新增 `propose_from_message`，从当前会话有效历史读取 entry。校验 `entry.kind=="user_message"`、原文摘要、quote、区间、所选 scope。项目来源取有权限的会话/项目上下文，核对请求中的 project。草稿、另一会话、旧分支、助手消息均返回 400。 |
| 8.2　`codey/memory/candidates.py:37`、`codey/app/memory_setup.py:19` | `locate_candidate_quote`、`_message_source` | 复用现有校验。唯一 quote 可由后端定位；一段原话出现两次时，要求用户选区对应的精确码点区间。把生成消息来源引用的纯函数提升成可复用的同包函数，让保存与 `source_available` 用相同摘要格式。 |
| 8.3　`codey/app/memory_routes.py:81` | `_propose` 的字段白名单、`record_command` 调用、`MemoryEvent(...provenance...)` | 添加只接受后端已验证来源的关键字参数。普通手工提议保持原状；选区提议的持久命令另带规范 message ref、quote、start/end。Ghost item 的字段仍由原白名单产生，消息引用连同管理命令引用进入事件 provenance。若回执重试遇到同 request id 而选区或范围不同，应冲突。 |
| 8.4　`codey/app/memory_commands.py:32`、`memory_routes.py:103/129` | `record_command`、`_decide`、`_confirm_user_preference` | 提议和确认各有幂等身份。返回真实 proposal/version。首次确认在同一个 `durable.control_line()` 内先检查有效消息分支、摘要、区间和权限，再提交确认。待审提议保留原 scope。跨项目确认只由明确的 `confirm_user_preference` 生成独立 `user-confirmed:` 事件。回显实际生效范围。 |
| 8.5　`frontend/index.html:209`、`frontend/src/conversation_ui.ts:1001/1069/1094` | selection menu、`selectionFor`、`showSelectionMenu`、`initSelectionMenu` | 菜单新增“记住选中内容”。现有 `quoteBody` 对应助手回复，另取起止都落在同一条已持久化用户消息的选区，沿节点的 `data-entry-id` 取 entry。已发送用户消息若尚未获得 entry id，等历史同步再启用。DOM 渲染后的 Markdown 与原文不一定等长，先按原文查唯一匹配，重复时只接受可验证的原文位置。UI 若从 UTF-16 切片求码点数，使用 `Array.from(raw.slice(0, u16Offset)).length`。 |
| 8.6　`frontend/src/local_context_drawer.ts:608` | `postAction` 和现有提议确认表单 | 抽出同模块共用的保存选区入口，显示原句、范围和提议文本。在提交开始时生成 request id，超时重试复用；用户修改选区则生成新 id。先显示“待确认”，在确认命令成功后才显示“已保存”。把已有 drawer 中 Accept/跨项目确认的 UI 复用。 |
| 8.7　`codey/ghost/backend.py:72` | `GhostMemory.retrieve` 中 `candidates.sort` 与 `reserved = next(...user preference...)` | 保留 active、过期、family、context 和来源控制；在排序与 user 首位预留之间处理已注册且适用的同 slot 冲突。先按当前显式任务覆盖，再按匹配项目、用户默认选择。若本轮要求已明确，则遮蔽该偏好并留下 trace；下一轮仍能取回原偏好。把有效槽位和任务覆盖从 `codey/memory/context.py:47 retrieve_context` 的调用边界传入。无可靠结构化覆盖时使用现有提示层级，避免从自由文本猜“永久更正”。 |
| 8.8　`codey/app/memory_observation.py:15`、`codey/ghost/learning.py:14/137/173` | `terminal_memory_facts`、`_feedback`、`_review_proposal`、`review_due` | 将纠正连到实际曝光的 request、item/version 与反馈原话。对已注册槽位的矛盾生成带 context 的待审例外；无法绑定精确版本的反馈只参与原有粗粒度统计。没有反馈保留 unjudged。保留当前 `candidate_item(source)` 的全文反解，缩短请求文本需要以后单独升级冻结版本与审计。 |

建议请求主体如下。客户端选择范围；权威来源和持久事件 ID 都在后端生成。服务端可把 quote 默认作为待审文本，再让用户确认适用范围。

```json
{
  "action": "propose_from_message",
  "session_id": "conversation-A",
  "entry_id": "entry-17",
  "quote": "以后示例用 Rust",
  "scope_kind": "project",
  "request_id": "selection-unique-id"
}
```

上例为唯一 quote 的最小请求。若前端提交原文摘要，后端必须复算校验；重复 quote 要同时传 start/end；允许修改提议文本时应另保留原 quote。在 `tests/test_memory_adaptation_flow.py` 补四条链：模型无候选时保存；同 id 重试一次仅得一个活动 item；项目 A/B 隔离；先确认 Rust、临时 Go、再明确更正与撤销。在 `tests/test_memory_attempt_gate_lifecycle.py` 补“冻结后撤销、物理发送前被拦截”。前端扩展 `frontend/src/local_context_drawer.test.ts`，覆盖重复中文、emoji、选区跨两条消息及失败重试。分别断言 request、持久化、发送、最终行为。

**重点验收提议之后发生对话编辑。** `record_command()` 对已有 request id 直接返回，`MemoryService.admissible()` 则在检索时看来源。两处之间仍可能接受一个失效的提议，或把失效提议提升为独立用户偏好。建议抽出 `_validate_message_origin()`，由消息提议和首次确认共用。确认前源消息被编辑或撤回时，拒绝项目确认和全局提升，并保留同槽的原有效偏好。幂等重试按已提交命令分类：已确认只重读实际状态；Core 已确认而 Ghost 未投影时，用同一事件身份恢复，并执行现有撤销和删除规则。用户已明确确认的独立全局偏好沿既有独立生命周期，单靠消息选区无法获得该范围。测试在「提议后、确认前」及「Core 提交后、Ghost 投影前」各插入一次编辑或删除。

### 09 在实际目标目录加载并执行仓库指令

1. **发现文件。** 在 `codey/agents/context.py:47 ProjectInstruction` 的现有字段之后添加带默认值的 `scope_dir` 和 `content_digest`，保留旧位置参数语义。在 `:61 load_project_instructions(root, *, max_chars=...)` 增加可选目标参数和总预算。调用方明确告诉它目标是目录还是文件；不存在的待创建文件使用父目录。用现有路径检查取得物理目标，从允许的 workspace 根到目标目录逐层按现有文件顺序读取。返回的 `name` 填根相对路径。全文摘要用于检出变化，投递正文可截断；预算不足以呈现有效规则时明示截断并允许进一步读取。
2. **绑定实际投递。** `_record_read_fact()` 已收集成功读取的规范路径。还要从下一请求有权读取的原回执提取 `project_instructions_changed` 中的规范目标路径，合并到 `KernelContext.request_snapshot()` 的待补规则列表。否则首次创建文件或跨目录编辑会被挡住，却无法靠成功读取的集合发现缺失的规则。历史事实重放只恢复目标和旧引用。当轮 freeze 再读当前规则，记录 `(relative_path, scope_dir, full_digest, delivered_digest)`。截断的规则展示须带标志和继续读入口，完整规则被模型实际看到后才满足写前条件。
3. **让冷恢复能还原。** 若把绑定放在 `TurnSnapshot`，修改 `kernel_protocol.py:50 InputContractVersion`、`:138 TurnSnapshot`、`:465 build_turn_snapshot`。当前新请求版本为 17，可引入 18。同步改 `provider_receipts.py:121 snapshot_description`、`:271 _snapshot_payload` 的精确字段集合、`:325 restore_snapshot`。旧 11–17 无新字段，按各自形状解码。对于未派发的旧写意图，如必须满足新规则，重新观察并生成新请求；已结算旧回执按原版本恢复。
4. **写前比较。** 在 `codey/operations/kernel_execution.py:288 execute_tool_operation` 及 CodeMode 的真正 child edit 路径接入同一检查，传入本轮冻结的目录指令绑定。最终写前复核有效目录指令版本；缺失或变化返回可恢复的 `project_instructions_changed`、差异路径及重新观察入口。把批量版放在 `codey/codemode/kernel.py::build_execute_turn_executor` 的 `plan_patch` 调用 `preflight_patch` 之前，先检查全部目标。对直接 write/create 与语义 rename 同样测试，确保只检查一次、且不会重放已写目标。对无目录规则的项目维持现有轮数。
5. **失败用例。** 在 `tests/test_project_patch.py` 加“读 A 包 → 写 B 包”和“首次创建 B 中尚不存在的文件”。第一次拒绝写入。下一请求必须自动呈现 B 的规则，且 A 范围保持明确。对批量编辑断言整组预检未写任何文件。再测只有规则名称比较一致但内容已改、规则被删、截断后完整读取、junction 越界。冷恢复序列放在 `tests/test_codemode_replay_authority.py`，同时断言实际请求的规则与文件字节。

### 10 用子回执展示多文件局部成功并安全恢复

| 文件与函数 | 修改内容 |
|---|---|
| `codey/codemode/host.py:174 CodeModeHost._patch` | 在 `for child_index, row in zip(...)` 里保留原 `_invoke_one`。每项成功后按回执加一行 `{path,state,result_ref}`，其中 `state` 按 `audit.changed` 区分 changed/unchanged；ref 来自 `receipt.audit.get("result_ref")`，缺失不得伪造。遇第一项失败，把已经有证据的项、失败项、尚未派发的路径放入结构化摘要，保留现有 `ok/error/value` 的类型。未确认写入单列 unknown。 |
| `codey/codemode/kernel.py:44 _settled_child_description`、`build_execute_turn_executor` 的 `plan_patch` | 让宿主错误提示从持久 child effects 重建同一摘要。guest 可以自己捕获错误或丢弃 `tools.edit` 的返回值，所以父程序的返回值不足以当恢复依据。按原 execution id、child index、工具、参数摘要查回执；原 patch 已成功的 edit 复用，不再发给执行器。 |
| `codey/operations/kernel_execution.py:896 _settle_edit_with_workspace_bump`、`:952 _settle_unconfirmed_edit` | 判定是否可确认依旧由原始结算拥有。遇“已登记、无结算”时可只读检查当前文件摘要并展示是否匹配预期；恢复按钮不能因字节恰好相同就生成成功回执。业务上可验证“文件当前符合预期”，执行归属仍未知。 |
| `codey/workspace/changes.py:771 ChangeTracker.restore` | 抽出纯的逐文件恢复判定，输入 before/current/recorded_after 摘要，输出 already_restored、restorable、conflict 或 unavailable。新增只读预览及按路径选择，真正执行时在写入处重新比较；预览之后用户改文件要拒绝覆盖。 |

**精确故障注入。** `tests/test_followup_recovery.py:33` 现有布尔参数覆盖第二个 child 登记前和登记后写前。在同一测试增加第三相位 `after_write_before_receipt`：对 b 调原 `_write_text_after_final_path_check` 确认实际成功后抛 `Crash`，再执行原恢复步骤；断言 a、b 字节已变、两者都未被重复写入、b 的缺回执状态没有被伪装成成功。给 `tests/test_project_patch.py` 加读失败、锚点失败、版本冲突、已结算 no-op 四种部分摘要。运行 `python -m pytest -q tests/test_project_patch.py tests/test_followup_recovery.py tests/test_codemode_replay_authority.py`。

### 11 将“来源身份”“近重复”“断言支持”“全文取得”分开修改

建议按 A、B、C 三个独立提交走。A 依赖第 02 步，B 不必等待 C 的新连接器数据。

**A　近重复只产生可解释的候选。** 在 `codey/research/evidence_gaps.py:115 same_source_groups` 之后新增 `suspected_source_pairs(passages, aliases, *, max_pairs)`。先去重同一源快照、排除已确定同源组和 truncated 源；对足够长的正文用段落/字词 shingles 产生候选，再计相似度。每条返回两个 passage ref、算法版本、依据和分数。设对比上限，达到上限输出未比较状态。用字符 shingles 支持无空格中文，在重复模板下测试误报率。复用同 DOI 的工作身份，但保留不同原文版本各自 hash。`answer_context.py:190 _render_context` 展示“疑似转载、需要独立材料”；`evidence_gaps.py:59 _search_candidate` 增加该 reason 的检索语句。只有存在实证才提升到确定同源。老报告的 `shared_source_groups` 保持原算法。

同一提交前先完成跨比较组的排序与去重测试。让 `_comparison_gaps()` 保留有效 question ref、规范化比较字段和符合条件的值，供 `_search_candidate()` 利用。采用用户问题顺序和稳定组键选择候选，查询包含能区分组的已报告条件。把“这一字面查询已经发送”和“这一比较组已有可用调查证据”各自记录。去重后第二组仍可显示待查。测试两个组的顺序反转、10 L 先查后 100 L 仍能提议不同查询、两个组被同一次读取覆盖，以及总额度耗尽。

**B　把证明范围限定到报告的具体字符。** `codey/research/report_evidence.py:89 check_report_evidence` 已有 citation 起止、相邻引用组与 `clause`。在 `_calculations` 和 `_quantities` 后生成新的 `claim_checks`。每项至少有 `report_start/end, passage_refs, kind, review_state`；直接引语/等式使用已验证的 source offsets。复合句内其余实体、适用版本、比较条件只在有对应证据时标记已核对，保留原文与未核对分句。另扫无引用的重大结论，避免只在含 citation 的循环里计算覆盖率。`codey/operations/task_execution.py:953 _submit_research_answer` 写入带算法版本的 `research_evidence_check`；`codey/research/answer_report.py:192 compile_answer_report` 根据原回执版本分派并对照原字段。当前恢复会重新算 diagnostics，简单修改旧字段语义会让老任务恢复失败。`proof_coverage.py:43 answer_coverage` 保留词项诊断；需要补搜的具体缺口经 `evidence_gaps → _search_candidate → build_research_plan` 进入既有预算。

**C　再扩展全文候选。** `codey/research/source_gateway.py:68 _full_text_candidate` 改为返回有界有序候选；先保留现有 arXiv、PMCID 映射，再加入有文献身份依据的出版方/开放仓储链接。`:163 open` 按顺序尝试。`:192 _open_full_text` 现在会返回带原摘要的 fallback；多候选时应把“尝试一个候选”和“所有候选失败后的摘要回退”拆开，否则首个失败就会结束迭代。把每次 acquisition 串起来并传到最终接收，取消立即上抛。扩展 `source_document.py:26 PublicationMetadata.decode` 的可选 edition/version，原有 status 继续表达撤稿。同步改 `source_connectors.py` 中构造 metadata 的位置、`ledger_receipts.py:227 source_snapshot` 和恢复时的 decode。旧三字段对象照常载入；新版材料类型、版次、状态分别有来源。候选全文必须检查目标内容、落地 URL 和论文身份，最后引用只能来自实际读过的窗口。

**回归在哪写。** A 放 `tests/test_followup_mechanisms.py`，用转载加脚注、同模板独立研究、确实冲突的不同版本。B 放 `tests/test_research_submission_handoff_production.py`，用“前半句真、后半句反”“旧版成立新版失效”“未引证的总结”“旧回执重放”。C 放 `tests/test_source_gateway.py`，覆盖首候选失败次候选成功、全失败只返回摘要、身份错误、取消与旧元数据。三个测试文件分开运行，避免把新检索命中误计为报告质量改善。

### 12 在更换 API 连接的安全边界重建上下文

1. **锁定三种入口语义。** 普通新会话只消费有权限的项目知识和用户记忆。同会话的新一轮可选择新连接并重建可见历史。`ResumeRun` 继续原任务的 API 选择与原窗口，测试明确拒绝悄悄换连接。`task_entry.py::_admit_api_selection()` 会从 `previous_run_id` 和当前 run 的 snapshot 还原选择，`task_submit.py::_dispatch_durable_task()` 也从已存身份重新派发。只改 `build_conversation_plan()` 无法让旧任务换模型。
2. **把跨连接接力做成新运行。** 用户显式选择目标连接，并给新运行不同的身份。在旧运行到达安全边界后才接入。由原 owner 对已做动作结算，能确认的以原引用交接，无法确认的保留未知并从只读观察开始。若需要新字段，单独持久化旧 run 的来源引用与允许继承的证据，避免把接力引用塞入会恢复旧 API 选择的 `previous_run_id`。新运行按正常入场路径冻结目标连接、能力及预算。跨任务读取沿现有所有权验证加显式接力授权，只读继承可验证事实。审批和已经派发的动作继续归原运行。
3. `codey/providers/replay.py:17 transform_replay_messages` 继续负责目标协议的 tool id 归一化、有图/无图及私有字段剥离。目标协议需要补全上下文时使用有出处的“未取得结果”陈述；禁止构造能够满足 completion gate 的假工具结果。旧调用 ID 在另一组批次重复出现时，配对测试需核对对应的原始调用。
4. `codey/operations/kernel_request_builder.py:57 KernelRunRequestBuilder.build` 的 `validate_selected`、`codey/memory/context.py:47 retrieve_context` 负责重新校验当前有效的记忆；`codey/operations/knowledge_context.py:15 knowledge_background` 消费第 04 步的紧凑知识。旧请求工件保留原版本；当请求尚未发送且记忆已撤回，新建请求前重新进行选择。小窗优先保留最新更正、未知副作用和版本化引用，再纳入研究背景。
5. **验收入场、回执及实际发送。** 在 `tests/test_context_work_state_portability_across_models_and_protocols.py` 加同一 provider 改 model_id、协议切换、小窗口无图与已撤销偏好。在 `tests/test_task_submit_durable.py` 加原连接不可用时不能猜恢复模型、显式新运行接力的第一次请求确实走目标连接，以及旧 click 或 shell 在途时新运行未重复执行。记录 old/new run、实际连接和模型、最后的已知回执与未知副作用。最后用 A 确认 Rust，B 临时用 Go 并按 v2 改代码，C 新会话核对全局 Rust 与 v2 项目知识。真实供应商对照另跑。

### 13 让自然语言组合任务进入正常的生产入口

1. **先改用于评分的实验。** `tools/p0_mixed_live_gate.py:28 mixed_task` 保留原题，新增 `natural_mixed_task`，只说用户目标和成功条件。将 `:41 mixed_run` 扩成可选择 guided/natural。给 `tools/browser_production_live_gate.py:150 run_case` 传 `declare_required_tools=False, additional_required_tools=()`；检查 explanation bindings、预填 URL 或预制动作是否还把答案送进请求。保存第一份真实 payload，断言它不含留出题的选择器、期望值或隐藏检查命令。正常工作区中用户能读到的文档和测试仍可用。
2. **另外改独立评分器。** `run_case` 的 `passed` 当前仍会查 `required <= successful`，`mixed_run` 也检查两个固定命令和文件子串。只关掉发送端的 required_tools 会使合法的另一条解法仍被判错。抽出建议的新函数 `score_natural_mixed(project, receipts, page_observations, oracle)`：在隔离项目跑隐藏业务测试、校验受保护文件、观察真实页面结果、检查实际来源窗口和 task 定义的先后关系。不同合法测试命令等价计分。若用户明确指定测试命令，才要求对应回执。固定命令组保留原 scorer。
3. **再改生产任务入口。** 在 `codey/operations/task_entry.py:108 build_task_policy_for_entry`、`:124 start_task_session` 分开“用户要求的交付”和“模型计划采取的动作”。现有 `required_checks` 能表达的明确要求沿原合同；需开放判断的事项先挂工作视图，记录用户原话定位。新提交/更正时由当次用户输入建立要求版本。模型决定先读哪个文件不会新增授权，也不能自己宣布用户要求已满足。
4. **把证据接回代码。** `codey/operations/browser_copy.py:130 select_source` 继续校验结构化 JSON/表格，`file_field_binding.py:52 bound_content` 从已读值写入明确目标。对于 API 迁移，在 `kernel_facts.py:32 project_result_observation` 的实际 source/read/edit 已结算之后，由 `task_handoff.py:26 task_handoff` 提供可选择的 passage/ref、文件 path/version；把所选关联写入该次编辑的输入或结算元数据。宿主逐项验证原引用属于本任务、文件已读且版本仍匹配。保持“模型选依据”与“程序证明代码语义等价”的区分。新增持久关联字段时给旧回执默认空，并在冷恢复用相同路径重建。
5. **把证据接回完成条件。** `kernel_prompt.py:628 working_context` 投影“交付项、现有依据、还缺什么”。`project_completion_checks.py:266 _verification_checks` 核当前工作区版本和 cwd。`completion_gate.py:192 evaluate` 与 `task_handoff.py:18 report_completes_task` 核明示的“还要改项目、还要检验页面”是否完成，研究报告成功后仍有工作则继续。页面来自旧服务实例时，把构建身份或能区分新旧版本的行为作为验收依据；文件外改只使依赖该版本的测试和页面失效。对资料不充分的结论说明未知，并保留已完成的其他项。
6. **提交前的端到端用例。** 在 `tests/test_p0_mixed_workflow.py:194` 的生产入口旁新增自然题：开发演示先用脚本化模型确认宿主边界，再将同一入口交给真实模型。独立验收器分别在“读文档后重启”“失败验证后换模型”“点击已派发未回读”注入一次故障；断言没有重复写入/点击、真正改了正确文件、页面业务值来自新版本。再开新会话核对第 12 步的知识与偏好。最终记录整题是否成功、最先断在哪一项、耗时和调用预算。运行 `python -m pytest -q tests/test_p0_mixed_workflow.py` 验宿主；真实模型的自然题成绩另列。



### 本轮代码示例的验证

前一版五个内嵌补丁在独立的 `ba3bff82` 副本中一起生效后，下列命令得到 **118 passed in 263.58s**，手册 01 的独立 self-test 输出 `self-test ok`。此前 65 项组合运行有 1 项 `source_stable=false`，当时副本仍在改代码。前文的 66 项是源码审查基线。这些运行使用前一版 04 和 05。

修订后，五个内嵌 diff 均在当前工作树通过 `git apply --check`。将 02–05 一起应用于临时副本，运行独立反证六种排列、任务提示装配、知识召回、等待接续参数和 Chromium 控件接续，结果为 **50 passed in 32.35s**。其中包含单独运行的 16 项知识召回和等待接续测试，两次数字无须相加。01 的运行证据来自上面的历史批次。06–13 仍为实施与验收规格。合并时用下列完整批次重新验收修订版。

```powershell
python -m pytest -q tests/test_research_to_code_ab.py tests/test_kernel_prompt_task_guidance_ownership.py tests/test_kernel_prompt_rendering_has_no_side_effects.py tests/test_followup_mechanisms.py tests/test_knowledge_multilingual_recall.py tests/test_browser_text_change_wait.py
```

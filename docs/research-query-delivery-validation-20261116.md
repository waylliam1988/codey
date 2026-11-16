# Research 查询、证据交付与浏览器恢复验证

基线 `4d82abdb`。在原工作区修改，没有创建或复制 worktree。原始数据保留于 `.e2e-artifacts/research-query-delivery-20261116/`；下文运行目录均相对于该目录。

## 最终实现

1. 保留原 CodeMode `code + inputs`，撤掉实验性的 `browser_steps` 输入替换。查询候选提供原工具或 CodeMode 调用，不关闭其他已授权来源、导航和页面操作。明确字段复制继续使用原目标路径入口。
2. 普通 strict Research 的首次正式来源读取后，在原固定 CodeMode 程序中执行有正式回执的控件观察。模型直接看到真实候选；角色、名称、所属记录和引用来自同一文档的观察。候选优先支持 `aria-controls`，原生 `<details>` 读取仍独立工作。
3. 选择 `purpose=result` 后，原 host 完成一次点击、已声明等待及正式读取。结果捕获校验原文档、原目标节点、可见性、忙碌状态和变化。点击已送达而读取未确认时，使用原回执续等、续读，不重放点击；成功后交付新正文的记录和 `eN`，提示只修答案。
4. 明确要求 `browser_navigate` 的 Research，在控件操作前完成正式导航、初始读取及观察。恢复建议只用当前文档的填写、点击等回执判断前置进度；旧页历史仍保留，但不能推动新页直接等待。
5. 原始任务中单列 URL 进入已有冻结来源合同及预读取。支持裸地址、尖括号、来源标签、列表及 Markdown 链接；引用、代码、否定和逐地址撤销继续排除。只产生来源读取义务，不推断点击或增加权限；旧任务保留原冻结来源列表。
6. 扩展原 `ToolSuccessRequirement` 支持调用方明确声明的 Markdown 字段。来源、字面边界、字段路径、标签、记录归属和是否必须来自查询之后都来自原合同。记录归属与查询后结果独立，初始正文和原生详情无需强制点击。
7. 多对象、跨网页先筛来源、记录及字段范围，再选该范围的最新证据。初始整页读取的原回执记录结果区 DOM 身份和正文偏移。同区更新即使不再包含字段，也使旧值失效；独立区域保留原字段。默认空 `context` 同样依据 `query_region`、正式 `read_passages` 和目标身份，不能把未读的捕获正文当作新证据。
8. 原 Research scope 固定精确值及真实 `eN`。先展开全部 `{{field:/pointer}}`，再整篇使用正式 Markdown 引用解析器；字段检查消费同一份引用位置。代码围栏内的引用不能满足字段。正式完成门再次校验当前值和报告已有引用绑定，不能只展开字段。
9. 允许缺失须由调用方声明，且需要合格正式读取；`partial/unresolved` 不能绕过硬性执行要求、错记录、歧义、截断或未确认点击。字段齐全和单来源加工具清单推断出的流程完成都只提示进度；原明确完整流程仍保留原提交收窄。
10. Hybrid 最终交接包含真实绑定值及原来源回执，避免最后请求仅含写入状态。字段复制沿原取证、编辑阶段交回证据，防止同段自由 JS 手写取值和转换后绕开固定复制。
11. 等待超时在原执行边界、参数校验和回放匹配前补齐。合法显式值保持原样；省略时用当前阶段匹配声明，否则 `5000ms`。同段 JS 点击推进阶段后，每次等待都按已提交回执重算阶段。全部声明来自冻结合同；恢复沿用原调用完整超时参数，不能按当前进度生成另一份参数。错误类型和越界继续拒绝，不猜条件和目标。

没有新增运行时、发布事务、持久进度账本或题目特征。无字段 scope 使用原 v4，有明确字段才使用同一 scope 的 v5；旧读取合同按原版本恢复。

## 最终验证

冻结生产源码摘要：`df77456aebf20686fb3e4494586ae25fc4d9779f463dc1c7d8ac2871f7157c87`。

| 验证 | 最终结果 | 原始记录 |
| --- | --- | --- |
| 32 个相关生产回归测试文件 | 552 通过，766.65 秒 | `regressions-final-09.log` |
| host/kernel、交付恢复、重放权限及返回值修复回归 | 51 通过，37.49 秒 | `kernel-replay-final-09.log` |
| 正式 `repair_of` 恢复保留原阶段超时 | 1 通过，2.35 秒；整批收集后新增，单独验证 | `wait-formal-repair-09.log` |
| 全树 Ruff | 通过 | `ruff-final-09.log` |
| mypy，以及修改文件额外严格 Any 检查 | 各 707 个源文件，无问题 | `mypy-final-09.log`、`mypy-precision-final-09.log` |
| Pyrefly | 0 errors；现有 35 suppressed、158 warnings | `pyrefly-final-09.log` |
| ty | 通过 | `ty-final-09.log` |
| kernel 类型精度门 | Any 计数为零，类型合同逃逸检查通过 | `precision-gate-final-09.log` |
| 原 17 项 full-surface 实机矩阵 | 16/17，保留旧引用负例遗漏失败 | `full-surface-07/` |
| 5 项普通 Research 自主实机 | 5/5 | `ordinary-live-auto-observation-02/` |
| 自由 JS 部分成功后取值失败及真实模型续跑 | 1/1，填写、点击、等待、读取各一次 | `partial-continuation-02/` |

三组最终实机的 `source_before`、`source_after` 均等于上述冻结摘要。完整矩阵覆盖 Chat、strict Research、Hybrid、CodeMode、结构化输出、重复读取及 6 份当次请求快照恢复、stale-ref、stale-edit、上传、下载和来源失败。HTTP 403 两个场景按正式合同停止为 `blocked`，属于预期通过，不代表取得了业务答案。

没有运行全量 pytest。Ruff 覆盖全树；类型检查覆盖整个 `codey` 和本轮修改测试、工具。额外对修改文件启用 `disallow_any_expr`、`disallow_any_explicit`；未新增 cast、ignore 或检查抑制。

pytest 完成后出现 RAM 盘目录清理的 `WinError 3` 警告；552 项用例及进程退出均成功。这是清理阶段的环境问题，未改动生产行为或测试预期。

`remaining-gaps-11.log`：7 项通过，覆盖初始整页证据关联目标、同区缺字段失效、独立区保留、空上下文、局部读取及完成门重验。另有单段 JS 两次点击后分别采用 `8200/13300ms`、恢复沿用原 `8200ms`，以及直接入口、固定降级调用、自由 JS 的完整参数回归。

最新空上下文回归位于 `tests/test_research_answer_fields.py`：`test_contextless_partial_read_of_another_region_does_not_replace_the_field` 同时覆盖有查询目标及仅有局部 `read_passages` 两种回执；`test_initial_open_field_is_invalidated_only_by_its_own_observed_dom_target` 通过正式浏览器/host/kernel 链，覆盖初始整页读取后同区刷新失效与独立区域刷新保留，以及空/非空 `context`。这些用例均已包含在最终 552 项中。

单列的正式恢复用例先点击并以 `8200ms` 等待，再让父程序取不存在属性失败；新增点击使当前阶段超时变为 `13300ms` 后，用原 `repair_of` 只修返回值，仍复用原 `8200ms` 回执，等待实际交付总计一次。

### 最终实机的原始失败与恢复

- `full-surface-07/stale_ref`：原回复只有观察、当前引用填写、当前引用点击、等待和读取；正式回执无旧引用失败，`failed_stale_refs=0`。业务事实正确，但漏了任务明确要求的负例，因此保留失败。`failure-analysis-09.json` 保存分类。未重跑追绿，未放宽生产合同或验收；独立的真实注入 `stale_ref_injected` 已正确拒绝并恢复。
- `full-surface-07/hybrid_codemode`：自由 JS 等待省略超时已被程序补齐；其后模型读取正文的子调用成功，父程序因没有返回值而失败。下一轮使用原目标路径复制入口，文件和最终说明均为 `173 / QUARTZ_851`。正式填写、点击、编辑各一次。保留失败父回执及成功子回执，不能表述为首尝试没有模型错误。
- `ordinary-live-auto-observation-02/ordinary`：首次模型请求已有两个带所属记录的真实候选，模型选择目标后取得并引用新 `[e3]`，点击一次。
- `ordinary-live-auto-observation-02/explicit`：已取得 `Friday 18:37`；首次回答改写为中文时间，被明确精确值合同拒绝。模型用字段占位符补答后通过，没有重新点击。这也显示精确值合同不承担通用等义判断。
- `ordinary-live-auto-observation-02/slow`：3.2 秒更新超过首次约 2.5 秒捕获窗口，点击成功、首次正式读取未确认的失败均保留；模型随后只补读取，引用新 `[e3]`，点击总计一次。
- `ordinary-live-auto-observation-02/visible`、`unrelated_controls`：初始正文足够，直接正式提交，点击零次；Print/Delete 无关控件未触发强制调查。
- `partial-continuation-02/partial`：脚本明确注入“填写成功、取得新观察、错误访问外层 Result 的 `.elements`”失败；交接保留成功填写和当前控件，之后四次真实模型生成从点击继续，填写、点击、等待、读取各一次。这验证续跑位置，不代表自主首尝试成功率。

完整原回复与正式回执索引见 full-surface 各场景的 `final-audit.json` 和普通 Research 各场景的 `raw-audit.json`；首轮失败及后续结果并列保留。

`historical-snapshot-restoration-09.json`：来自 `full-surface-02` 的 32 份真实请求快照全部能解析并往返编码，覆盖 Research、顺序 Research、Research CodeMode、Hybrid、Hybrid CodeMode 和 stale-edit。这是历史合同恢复验证，不取消跨实现版本执行的原兼容性检查。

## 保留的失败和对照

| 运行 | 结果 | 原始请求与正式回执确认的情况 |
| --- | --- | --- |
| `full-surface-02` | 16/17 | 模型漏做旧引用负例，直接用新引用；真实注入旧引用通过。事实正确不能代替负例拒绝回执。 |
| `full-surface-06` | 14/17 | 除漏旧引用负例外，实验步骤入口下 Hybrid CodeMode 连续快照 12 次；顺序 Research 最后补导航后，恢复沿用旧页进度并等待超时。后两项按本轮退化及生产接线问题处理，撤掉步骤输入替换并修导航、文档进度。 |
| `full-surface-07` | 16/17 | 冻结源码最终矩阵；Hybrid CodeMode、顺序 Research 已通过，仍保留模型省略旧引用负例的失败。单轮结果不能证明普遍稳定性。 |
| `ordinary-live-auto-observation-01` | 5/5 | 普通动态查询、明确字段、正文足够、3.2 秒更新、无关 Print/Delete 控件通过。慢更新点击仅一次。属于较早源码，不能替代最终冻结矩阵。 |
| `partial-continuation-01` | 1/1 | 预设部分成功后取值失败，再由真实模型续跑；填写仅一次。失败父程序和成功子回执保留。 |
| `ordinary-live-auto-observation-02`、`partial-continuation-02` | 5/5、1/1 | 冻结源码最终自主查询与恢复验证；首次失败分类及续跑次数如上。 |
| `regressions-final-07` | 528 通过、3 失败 | 两项实验步骤接口断言和一项旧强制提交断言；实验接口已撤，推断流程改软提示。 |
| `regressions-final-08` | 533 通过、10 失败 | 六项旧测试仍假定无导航后初始读取，一项把漏超时当作错误；另三项恢复测试期间生产源码变更，被原实现兼容检查拒绝。更新过时断言，冻结后整批重跑，不放宽恢复检查。 |

更早自主普通 Research 全部保留：`ordinary-live-01` 和 `ordinary-live-required-source-01` 各 0/5，断在单列来源接入；明确字段例编造 `10:30 AM` 被正式门拒绝。`ordinary-live-source-wording-01/02` 各 2/5，出现成功后被交接引向重观察、重复点击，以及跳过观察提前 partial。`ordinary-live-source-address-01` 还出现被拒后重读旧正文，缺的是查询证据却提示整页重读。后续分别修复来源接入、自动正式观察和按缺失原因的交接。

`full-surface-01/03/04/05` 不完整，不能作完整矩阵。`remaining-gaps-09/10` 的新测试漏传 `native` 是测试安装器错误，已修并保留日志；一次原生详情测试在源码变更期间取得正确答案，但源码稳定性检查失败，冻结后重跑。

历史 `150 / ABC123XYZ` 是模型自由说明的事实错误。程序修复的是最终请求的真实正文、绑定事实交付，以及有明确字段合同时的程序展开和引用校验，不声称能证明任意自然语言解释正确。

## 剩余边界

- 普通问题仍由模型理解缺项和入口相关性。没有“一有按钮就必须点击”的规则，也没有把自由问法伪装成完整字段合同。
- 候选依赖真实页面关系；其他页面保留原浏览、观察和来源能力。`purpose=result` 不扩大点击权限。
- 更新捕获约 2.5 秒，证明原目标更新且可读取，不证明站点业务完成；慢更新复用等待、读回恢复。
- 字段检查针对精确值、漏项和来源版本，不是语义证明。等义改写、否定、条件及推理仍有边界。占位符避免抄值错误，不能替代解释。
- 自由 JS 仍可使用旧变量。程序提供新观察、拒绝旧引用并保留成功动作，不会偷偷替换成同名新控件，也不扩大整段程序重放权限。
- 当前是受控页面和固定模型/seed 的验证，缺少充分跨站点、多轮成对数据，不能宣称稳定性普遍提升。撤掉的实验步骤输入不是新的受支持合同；原 CodeMode 与既有历史合同继续使用原恢复边界。

# 12B research 下一轮：先验证两项假设，再决定生产改动

基于 `c346b9e2` 的代码及已有实机记录。本文件是实施教学，**不是下面改动已实现、测试已通过的报告**。本轮仅新增本文档，没有修改生产代码、测试脚本或调用模型。

目标不是继续完善一套宏大的 research 流程，而是回答两个窄问题：

1. 移除当前固定材料任务不需要的 `knowledge_read`，能否减少错误动作，并改善正常结束？
2. 同一固定部署，在只有问题和材料时，能否区分“材料支持答案”和“材料不足”？进入原 kernel 合同后又损失多少？

两项实验分开进行。读工具实验不能同时修改提示、拆题、采样和评分；能力探针也不能一边看答案一边调整提示。生产继续使用同一 kernel、原知识存储、原效果结算和唯一冻结报告。

## 一、先理解当前错误发生在哪里

现有链路可以简化为：原计划 → 冻结局部任务及工具 → 模型形成工具动作 → 原知识保存与明确选择 → 原验收及唯一报告。

| 观察 | 属于什么问题 | 下一步验证什么 |
| --- | --- | --- |
| 将 passage ID 交给 `knowledge_read` | 动作选择与身份负担 | 移除无用途工具后是否减少错误调用 |
| 已说“没有原因证据”，却保存缺口占位 finding | 判断正确，协议表达错误 | 首次正确判断、首次合法动作分开评分 |
| 把同主机或共享内存解释成单写者原因 | 证据与因果关系判断错误 | 裸模型在同材料上是否也误答 |
| 正确回答恢复机制，又附加错误停止条件 | 生成答案时扩大了关系范围 | 对照直接回答中是否也出现错误附加 |

第一项修复不能被当成后三项的语义修复。现有引用和版本校验能证明来源身份、阅读与选择关系，不能证明中文解释受原文蕴含。

## 二、第一批只改实验脚本，先验证移除工具的价值

### 2.1 为什么先不改生产

`tests/manual/research_evidence_local.py` 的 `seeded_kernel` 在 fixture 建好计划和已读来源后，固定暴露：

```python
session.model_tool_names = (("save_finding",) if args.submission_mode == "select" else ()) + (
    "submit_research_answer", "knowledge_read", "read_tool_result")
```

因此，已有实验即使没有任何历史知识对象，仍会邀请模型选择 `knowledge_read`。这是一处很小且可直接对照的入口。

先在**全新隔离目录、given 材料、没有历史知识 setup**中比较“暴露”和“移除”。这一步只回答固定材料任务的工具暴露问题，**不是已经实现了通用的动态可见性**。移除组会在整次运行中隐藏该工具，保存 finding 后也不会重新开放它；这个限制必须写进实验说明，不能把它当成最终生产设计。

### 2.2 具体改哪里

以下参数和代码均为**待实现草案**，当前脚本没有这些参数。

在 `research_evidence_local.py` 增加实验参数：

```python
parser.add_argument("--read-exposure", choices=("legacy", "omit"), default="legacy")
```

`omit` 只准用于 `--layer given`，拒绝与 `--candidate-refresh-probe` 组合；fixture 开始必须是新的隔离存储，setup 只建计划及来源阅读，不预存 finding、历史对象或背景召回知识。有这类知识时，实验前置条件不成立，应中止 setup，不能悄悄删掉模型需要的工具。

将原工具列表构造改为：

```python
names = (("save_finding",) if args.submission_mode == "select" else ()) + (
    "submit_research_answer",)
if args.read_exposure == "legacy":
    names += ("knowledge_read",)
names += ("read_tool_result",)
session.model_tool_names = names
```

legacy 分支保留原工具顺序；顺序本身也会影响实际请求与模型选择，不应在对照时无意改变。

在 `fixture-control.json` 中记录 `read_exposure` 和隔离存储/无历史知识的 setup 声明。原 `source_sha256`、每个阅读窗口、固定时钟、seed、模式等继续保留。

在 `tests/manual/research_local_matrix.py` 增加同名参数，传给子进程并记入 `matrix.json`。默认 `legacy`，使旧调用保持原实验含义。不要添加生产环境变量或持久化 research 模式开关。

这仍然运行 `ResearchContinuation.kernel`、原 `stage`、原 KnowledgeStore 和报告链；只改变原本已经存在的实验工具暴露范围。

### 2.3 必须保留什么

- `read_tool_result` 和原文回执索引。模型仍能恢复完整原文。
- 原问题、完整原文范围、OR 条件、否定、版本和例外。
- 原 inline 保存与明确选择逻辑，以及合法的 `unresolved + findings=[]`。
- 原预算和 no_progress。不能因为工具少了就由 host 代填 unresolved。
- 原评分。原来要求首次干净缺口结束，不能改成“最后没发报告就算通过”。

不要同时收紧答案字数、加入 WAL 专用提示或更改 `submission_mode`。建议这一轮两组都固定 inline；select 不作为第三个因素混入。

### 2.4 如何做离线测试

优先给这个实验参数写少量有意义的脚本/请求集成测试，而不是测试 `tuple` 拼接的实现细节：

1. 默认调用的实际请求仍有 `knowledge_read`。
2. omit 的实际文本合同、JSON schema 和 native tools 均没有 `knowledge_read`，且仍有提交与原文恢复工具。
3. omit 与候选刷新、非 given 或有历史知识 setup 的非法组合，在模型请求前报错。
4. 人工构造隐藏工具调用时，原 normalize 和 `execute_turn(snapshot=...)` 拒绝；没有发起读取效果。
5. 从原 provider receipt 恢复后，可见工具集合与发送时相同。

真实请求测试可参考 `test_research_scoped_request_projection.py` 和 `test_research_inline_submission.py` 的假传输捕获方法。使用 fake provider 即可，不访问本地模型。绕过 kernel 直接调用 `_execute_research` 不足以测试隐藏工具，因为那个内部方法不是完整准入入口。

第一批只改实验脚本，不必为它跑整套进程写入恢复矩阵。若接着改了生产快照、canonical 回执或恢复投影，则需执行第五节的恢复检查。

### 2.5 如何跑真实模型

先跑两题：`writer_limit` 和 `coexistence_only`，每题 seed 74/75/76，两种暴露各一遍，共 **12 次运行**。它们分别检查“有证据仍回答”和“没有原因证据时结束”。模型请求数可能大于 12。

固定材料可使用已有归档，下面命令中的路径当前存在：

```powershell
$researchSource = 'D:\codey\.e2e-artifacts\research-contract-convergence-20261101\batch4b-schema-inline\local-writer_limit-74\sources.json'
```

当前代码即可执行的单例基线命令如下；会调用本地模型，应在你开始实测时运行，本轮没有执行：

```powershell
python -X utf8 -m tests.manual.research_evidence_local --directory .e2e-artifacts/research-next-baseline-gap74 --source-file "$researchSource" --clock-file tests/fixtures/research_local_clock.txt --case coexistence_only --layer given --seed 74 --order-seed 0 --submission-mode inline --max-turns 12 --provider-timeout 120
```

**实现新增参数后**，才能在同一脚本分别传 `--read-exposure legacy`、`--read-exposure omit`。给每次运行一个新目录，串行运行；每个 case/seed 的两组尽量相邻，交替先后顺序，并记录实际执行顺序。超时、异常和失败也保留，不用同 seed 重跑的成功结果覆盖。

先看实际 `research-wire.jsonl`，确认工具真的被移除了。若仍在发出的 schema 中，先修实验，不解释模型表现。

如果两题试验证明错误读取减少，且没有明显新增有证据拒答，再决定是否扩展原五题：两组 × 五题 × 三 seed，共 **30 次运行**。前面的两题可以作为其中的预先计划子集，不必重复跑；前提是期间没有改代码、输入和配置。新旧实现版本不能混成一个矩阵。

检查四类独立结果：

| 指标 | 怎么判 |
| --- | --- |
| 无意义读取 | passage/source 定位符被送入知识读取的次数；另记其他错误动作是否增加 |
| 正向能力 | 有证据题完整回答，条件正确，无错误附加，且成功交付 |
| 缺口能力 | 首次认识到当前证据不足、首次合法空 unresolved、最终结束分别记录 |
| 成本 | 请求数、输入 token、耗时分列，不以预算耗尽或早停冒充高效率 |

omit 组没有 `knowledge_read` 调用是干预本身的机械结果。真正的收益是总错误动作减少、动作不转移到其他无效工具、正常结束改善；**不能仅凭被隐藏工具调用数归零就宣称 research 准确率提升**。

## 三、第二批做能力探针：直接回答与原 kernel 对照

### 3.1 复用什么，新增什么

已有 `tests/manual/research_layered_local.py` 可直接发固定材料诊断，但原 level 1/2/3 要求约 150 字、三项发现或 300 字综合，与本次任务不相同。不要直接把那些旧结果当成这次最小拒答基线。

在这个诊断 runner 增加一个独立 `sufficiency` 模式，**不是新增生产阶段**。参数名称由实现确定；当前并不存在可运行的 `--mode sufficiency` 命令。

kernel 侧扩展 `research_evidence_local.py` 的 fixture 读取入口，接受同一批显式问题和完整材料窗口；仍通过原 delegate 建计划/阅读回执，并调用原 kernel。不得实现另一套保存、循环、完成或发布逻辑。

输入包与人工 rubric 分开。两个入口读取同一个问题和材料文件，只有评分者读取 expected/rubric 文件；不能把“本题为 negative”“应该 unknown”或正确答案写进模型输入。ID 使用中性编号，不用 suggestive 的 `no_answer` 等名字提示答案。

### 3.2 试题怎么选

选择 **3 组未参与提示调优的新问题，每组两份材料**，共 6 个案例：

- 正材料：含直接支持所问关系的完整段落。
- 负材料：相同主题的邻近段落，但不支持所问关系。

三组可分别覆盖原因、完整条件/OR、恢复机制。题目不必继续来自 SQLite；最好选没有反复调优过的官方资料。先由人确认正材料确实足够、负材料没有直接或可明确推出的答案。材料长度不必完全一致，但不要让“很短就是缺口”成为统一暗示。

每组问题文字相同，只换材料。保留段落的条件与例外；负材料应换成完整邻近段落，不能截掉半句或 OR 尾部来制造歧义。材料定位到冻结版本及明确窗口，判断只针对提供的材料，不能评价整页或现实世界里是否有答案。

下面只是**说明判法的校准例子**，不是未见验收题：

> 问：系统 F 为什么限定同时只有一个提交者？
>
> 正材料：F 的所有提交都会更新同一份提交序号表。为了避免该表出现相互冲突的更新，F 限定同时只有一个提交者。
>
> 负材料：F 的进程可以共享读取缓存，并要求位于同一台主机。读取者可以在提交者追加日志期间继续读取旧快照。

正材料应回答序号表冲突；负材料只能说当前材料不足以说明限制原因。不能用共享缓存或同主机要求自行补一个原因。若模型说“当前材料没有原因，因此系统没有单提交者限制”，同样错误。

### 3.3 直接回答入口怎么写

最小指导即可，例如：

> 仅根据下面材料回答这个问题。材料足以支持时，只表达回答所需的完整关系，保留条件、否定和例外；不足时说明当前材料不足以回答。不使用外部知识，不补充旁支机制。

然后是原问题与原材料。不要要求约 150 字或至少几项发现，也先不要新增复杂 JSON、身份编号、excerpt 校验或自评分。直接回答使用普通中文；无工具 JSON 是该入口刻意移除的负担。

kernel 入口使用当前原合同，固定 inline、不拆分。先固定一种读工具策略，所有 kernel 案例一致；不要同时把“读工具前后”加入第三个因素。若第一批还没得出结论，使用 legacy，并明确标注这是当前合同基线。

这是两种任务形态的能力对照，不是严格只差一个 token 的消融：kernel 天然有工具声明、身份、状态、回执和后续请求。若直接回答明显更好，说明外围负担值得继续定位，不能立即证明唯一原因就是某一个 schema 字段。

### 3.4 控制哪些变量

- 同一实际部署，记录模型完整 ID、量化文件/部署配置身份、上下文预算、chat template、thinking、采样与输出限制。不要只写“12B”。
- 两入口固定 temperature=0.3、相同 74/75/76 seed、thinking 等既有采样项，并核对实际发出请求；不要把旧 runner 默认 73 起算当成相同 seed。
- 固定同一 `research_local_clock.txt`，实际请求中只出现一次时钟；保留真实采集和耗时时间。
- kernel 当前局部请求上限为 4096。直接入口也使用足以完成最小答案的相同上限；检查未因 length/超时截断，不能沿用旧 runner 的 1200 后声称预算完全一致。
- 只有一个模型服务，串行执行；交替入口顺序，记录顺序。新的隔离 state 和知识目录，避免先前答案被背景召回。
- 不改原五题 rubric。新六题有单独预冻结评分；评分变更需新实验版本，旧失败保留。

总量：3 组 × 正反 2 份 × 3 seed × 2 入口 = **36 次运行**。直接入口为 18 个请求；kernel 的 18 次运行可能产生更多请求。三个 seed 只是有限样本，不能证明通用稳定性。只有当相同配置结果摇摆、影响诊断时，再预先登记少量重复，不通过挑选重复结果抹掉失败。

### 3.5 如何人工评分

正材料每次都检查：完整回答所问关系、条件准确、没有错误附加、有证据却拒答。负材料每次检查：没有捏造支持关系，承认的是“当前材料不足”，不是“来源/世界没有答案”。直接入口没有工具状态，不强行把它映射成生产的 submission。

kernel 还要分别标注：

| 字段 | 含义 |
| --- | --- |
| first_judgment | 首次可观察到的内容判断正确/错误/未表达；不推断隐藏思维 |
| first_action | 首次内容动作是否合法、与其判断一致；单纯读取也可为尚未作答 |
| unsupported_addition | 是否在任意已生成候选中加入不受支持的事实；最终采用的另记 |
| first_clean_gap | 是否未先制造/选择伪答案，就完成合法空 unresolved |
| final_stop | 正常 unresolved、done、no_progress、max_turns、服务异常分别保留 |
| false_publication | 是否真正发布了包含错误关系的正式报告 |
| positive_abstention | 有充分证据时是否仍 unresolved、漏答或未形成受接纳答案 |

原四维评分继续保留，这些是额外诊断维度，不替换原通过标准。假如先保存“没证据”的 partial 占位，再改空 unresolved：语义缺口识别可正确，动作和首次干净结束仍失败。假如保存过错误候选但最终未采用且没发布：候选错误要记，不能记作错误正式发布。

建议结果写入独立人工 review 文件，按 run ID 对应原请求、工具回执和最终报告；原 wire 与原始响应不改写。未人工检查的项保持 pending，不用关键词命中或引文连续匹配自动判语义通过。

## 四、根据结果选择下一步

| 结果 | 解释及下一步 |
| --- | --- |
| 移除读取后错误动作少，但缺口仍误答 | 保留减负观察；不要继续堆读取反馈，把重点放到能力对照 |
| 直接回答正反都好，kernel 明显差 | 查实际请求里剩下的重复指导、无效工具、状态组合负担；一次只改一项，再对照 |
| 直接回答也把邻近事实当原因 | 当前部署有证据关系判断瓶颈；暂缓改控制流，收窄可交付范围 |
| 两入口都容易在正确答案后加错机制 | 不是引用身份能解决的问题；不加同模型评审循环，不承诺仅改 schema 可保证语义正确 |
| 负材料全拒答，但正材料也拒答 | 过度保守，不能算改善 |
| 已知题变好、新题失败 | 对已知题适配，不能推广完整 research |

如果直接回答正确、kernel 错误，只对照**实际发送的完整载荷**找额外负担，不凭函数内某段提示的局部文本下结论。此前已经收窄过回答范围；不要把“再加一句不要扩写”当成新的确定修复。

建议的有限验收门槛：新六题的每个正例都完整且无错误附加，每个负例都无错误肯定；kernel 的负例还要正常、干净空缺口结束，不能靠预算停止。即便 18 次全过，也只准进入下一层候选/检索验收，不足以宣布通用 research 稳定。

## 五、证实工具减负有价值后，才落地生产动态隐藏

### 5.1 先明确事实和边界

生产应区分三个集合：

1. **已看到可读定位**：知道真实 object/document ID，必要时有精确 revision，可以读取。
2. **已经完整读取**：读取成功，获得原证据与绑定。
3. **本 unit 可采用的 finding**：当前 scope 准入，模型可明确选择。

`scope.finding_refs` 属于第三类，不能拿它代替第一类。否则搜索命中、历史 fN、背景召回对象尚未读就无法再读。

`tools.py:knowledge_search` 目前只有展示文本；`task_execution.py` 的对应分支也没有 canonical 搜索命中。因此，可靠动态隐藏不能靠正则从 `model_text` 抠 ID，也不能重新搜索全库猜“模型应该知道哪些 ID”。

### 5.2 推荐的生产改动顺序

**A. 原搜索结果同时产生展示和结构化定位观察。**

修改 `codey/research/tools.py`，从同一份 `hits`、legacy note 搜索行产生渲染结果与类型化 locator。通过原 `ResearchToolOutput` 和 `_execute_research` 的 `ToolResult.canonical` 回传；字段名由实现统一，例如 `knowledge_locators`。这是原工具结果的观察元数据，不含答案正文，也不表示已读/已采用。

注意 `render_recall` 会按展示预算跳过部分 hits：canonical 中标记“模型已看到”的集合必须对应真正展示的行，不能把所有检索命中都算已观察。应让一次渲染选择同时返回选中行及文本，而非事后解析文本。legacy document ID 与结构化 object ID 都有合法读取用途，不能混成一种身份。

**B. 从原已结算回执推导目标，处理背景召回。**

已有 `research_reference_bindings` 提供保存/读取过的 fN 与固定版本；新增搜索观察提供尚未读的目标。使用同一原回执投影，允许按需重建的内存缓存，不新增权威表或可写游标。

`knowledge_context.py:knowledge_background` 的紧凑输出也携带真实 ID，不能漏掉。若需要精确跟踪，应由同一次召回/渲染返回实际展示的结构化定位，并沿原 provider 请求准备及冻结入口记录；从原请求恢复，不能恢复时再查当前库重建旧请求。明确由用户提供且已通过原知识准入的 ID 同样保留通路，不要靠字符串形状猜身份。

对于旧回执只有展示文本、背景身份无法可靠还原等情况，**保守保留 `knowledge_read`**。第一版宁可少裁剪，也不要引入一次全面历史迁移。没有真实知识目标时必须保留原文阅读/恢复能力；需要历史知识则保留搜索→取得身份→下一快照读取的路径。

**C. 仅在能证明没有目标时收窄冻结工具清单。**

入口为 `kernel_protocol.py:build_turn_snapshot`。保留原 policy/controller 的授权检查，再校验 `model_tool_names` 是合法收窄，最后按已观察事实进一步过滤本轮 `names`，随后由同一 `names` 产生 contract、specs、executor inventory。

示意伪码，非现有接口：

```python
authorized = authorized_from_existing_policy_and_controller(...)
check_explicit_exposure_is_subset(exposure, authorized)
names = apply_existing_exposure(authorized, exposure)
if strict_local_scope and locators_are_known_empty:
    names = tuple(name for name in names if name != "knowledge_read")
# 继续走原冻结定义、contract、inventory、执行绑定构造。
```

过滤放在显式 exposure 校验之后，可避免现有 harness 硬编码含 `knowledge_read` 时触发 `model exposure must narrow the authorized tool inventory`。不要只从提示文本删掉名字，却让 schema 或执行层继续接受。

搜索或保存成功后重建下一快照：真实目标出现便可重新暴露读取，仍受原权限与显式 exposure 上限约束，不能反向扩大调用方刻意限定的工具集合。最初 omit 实验故意隐藏整个运行，与这一动态生产行为不同，落地后需单独验证。

`TurnSnapshot` 已冻结工具集合、schema 和执行绑定，**仅为工具可见性本身，不需要升级 ResearchScope v1/v2/v3**。若为背景观察确实要增加原请求/快照载荷字段，必须显式处理那个载荷的版本与旧格式，不能悄悄往严格字段集合里加字段；继续保留现有 implementation_id 恢复检查。

新快照入口至少包括：`kernel_preparation.py` 首轮、`kernel_context.py` 工具结果后的下一轮、`kernel_control.py` 修复轮。它们必须使用一致的观察推导。收到响应后仍使用原响应绑定的快照，不能拿新查出的对象扩大旧请求权限。

### 5.3 生产测试清单

| 场景 | 预期 |
| --- | --- |
| 只有 source/passages，没有知识定位 | 读取知识隐藏；原文完整可用或可恢复 |
| 搜索命中真实 object/document | 搜索本身不算已读；下一快照允许读取 |
| 搜索无命中 | 已知没有其他目标时不凭空产生读取机会 |
| 保存过 finding | 下一快照允许读取真实 fN/固定 object revision |
| 历史 fN 当前不可采用 | 仍可显式重读；未读前不能自动进入当前可采用集合 |
| 背景召回/合法显式知识 ID | 不被错误隐藏；读取前不升级成 finding |
| 真实知识 ID 与 source/passage 同名 | 有真实目标时正常读取，不按 p/s 前缀封禁 |
| 旧回执无结构化搜索观察 | 保守兼容，不从展示文字猜事实 |
| 模型调用本轮隐藏工具 | normalize 和执行器均拒绝，效果未准入 |
| 冻结后改 live registry/知识库 | 旧响应仍按原冻结工具/定义执行 |
| 中断恢复 | 复原相同观察与可见性，不重复搜索/写入/发布 |
| native 与 text 实际载荷 | 工具集合和约束同源，计数与实际发送一致 |

至少覆盖两个与新观察直接相关的中断点：搜索结果已经结算但下一请求尚未发出；下一请求已冻结而响应尚未处理。恢复后核对原 search 不重复执行、原请求按原 snapshot 解释；合法后续 knowledge_read 是另一个新动作，不算重放搜索。

可复用的现有测试命令如下。当前文件均存在，**本轮未运行**；先补新用例，再按实际改动范围选择执行：

```powershell
python -X utf8 -m pytest tests/test_research_local_action_feedback.py tests/test_research_local_scope.py tests/test_research_scoped_request_projection.py tests/test_turn_snapshot_binding.py tests/test_turn_snapshot_freezes_tool_definitions.py tests/test_kernel_execution_snapshot_guards.py -q
```

若改了知识绑定、回执或恢复，再补：

```powershell
python -X utf8 -m pytest tests/test_research_read_projection.py tests/test_research_current_reference_repair.py tests/test_research_context_priority.py tests/test_research_inline_submission.py tests/test_safe_tool_replay.py tests/test_runtime_operation_reducer.py -q
```

第二条包含现有真实子进程恢复测试。执行期间冻结源码，不边跑边改，否则 implementation_id 变化会破坏恢复可比性。生产变更再跑仓库原有 lint/typecheck；不必因只写实验说明就重跑 785 项。

## 六、做到什么程度就停止继续改

建议提交粒度：

1. 实验脚本暴露选项、实际请求/前置条件测试；不改生产语义。
2. 新正反 fixture、独立人工 rubric、已有直接/kernel runner 的入口适配；用 fake provider 检查材料和预期答案没有串线。
3. 完成真实对照及人工评分后，**若支持收益**，才提交生产的观察回执与动态可见性。

不要先把第三批架构工作全部做完，才发现裸模型也不会判断缺证据。也不要为了少一次错误读取，顺手改报告、语义验收和研究调度。

host 可以按现有预算停止重复动作，准确显示“未形成受接纳答案”。host 不能根据没有新材料，宣告原文没有答案；不能伪造模型的 unresolved；不能把已保存或有引用的 claim 自动批准为正确。

如果这套固定部署的直接最小回答，在完整证据和邻近负材料之间也反复混淆，当前应交付的是受限资料提取/原文核对能力，并将未稳定的因果解释或综合关系留给人工确认。若产品要求完整中文研究结论，不能用仅返回原文或预算停止替代后宣布 research 跑通。

这一轮成功的定义首先是：**知道下一处值得修改的瓶颈在哪里，并保留可复核的证据。** 它不要求一次实验就把所有 research 失败变成成功。

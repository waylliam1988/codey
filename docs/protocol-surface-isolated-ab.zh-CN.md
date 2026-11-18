# 分配置机械协议 A/B 脚本

本轮新增分配置脚本，不修改 `codey/`、生产提示模板、用户模型设置，不创建 worktree。历史三轮脚本和结果保留。共享浏览器 fixture 只新增可选 `task_transform` 参数，默认行为不变。

用户随后授权运行实机，批次为 `.e2e-artifacts/protocol-isolated-live-20261111-01/`；结果与限制记录于[实机报告](protocol-surface-isolated-live-20261111.zh-CN.md)。下述实现、确定性验证与实机证据分别记录，不以诊断通过代替自主成功率。

后续 v2 修补 B4 回放后的结果交付；中断复测并定位传输后，v3 同时把本协议实验 A/B 显式改为流式、隔离可选性能诊断错误，并在 provider/worker 异常后立即中断矩阵，见[B4 修补与复测](protocol-surface-b4-delivery-live-20261111.zh-CN.md)。v1、v2 证据保留，不与 v3 混作同一版组合证据；共享实验 provider 默认保持非流式，历史实验默认不变。

入口：`python -m tools.protocol_surface_ab`。候选机制位于 `tools/protocol_surface.py`，结构化执行器位于 `tools/protocol_surface_program.js`；确定性测试为 `tests/test_protocol_surface.py`。

### 2026-11-18 当前基线维护

当前生产 `browser_wait` 已允许省略 `timeout_ms`：优先采用当前阶段匹配的任务声明，否则采用 5000ms；显式非法值继续拒绝。B3 的 schema 来自当前生产快照，因此不安装 B1，并不代表仍使用历史上的“超时必填”合同。B1 开关目前控制的是实验编译/调用入口的补值机制，不能再用“省略参数是否成功”单独证明 B1 相对当前生产有收益。历史实机报告保留原日期、源码与结果，不能据此推断当前差异。

本次维护回归测试、两份实验运行器与本文档，不修改生产合同。修复前定向复现为 **5 失败、4 通过**：两项依赖旧的必填假设，一项把合法省略当成非法调用，另两项依赖旧错误字段路径。新测试检查 B3 继承生产 schema/说明、B1 编译补值与 B3 保留省略值的区别、合法边界及显式非法值、原参数不变；真实 Chromium 覆盖生产 A、B1、B3、combined 的省略参数执行与正式回执。原有非法调用不登记 durable 子意图、真实超时、恢复和 stale ref 断言继续保留。

扩大到四份相关测试文件后得到 **20 失败、288 通过**，原始日志为同目录 `regression-final.log`（这是中间失败轮，不是最终通过记录），请求与回执位于 `R:/codey-pytest/run-bk8nexuj/`。归属核对及修改如下：

- 普通来源字段复制现在提供 `codemode(path)`，拒绝同程序读来源并自由改写。旧实验却一直假定自由编辑。两个运行器现在共同使用 `content_edit_task()`，仅为 hybrid/stale_edit 的**完整文件接口诊断**明确要求自由 JavaScript 与 content 版本编辑；两侧任务相同，所有字段来源检查、版本冲突和外部字段 scorer 保留。它验证现有显式自由编辑入口，不能代表普通任务默认路径。普通字段复制的同程序拒绝与后续 destination-only 输入，由 `test_browser_repair_closure.py` 的原生产回归继续验证。
- 结果正文后可附正式运行时说明，测试不能按旧的 metadata 标题切割并整段解析 JSON。测试辅助函数改用现有平衡括号扫描器定位开头的 JSON，再严格解码；回执身份与内容断言保留，不把附加说明作为结果数据。
- 当前完整声明让部分脚本化请求超过原来的 32768 窗口。仅确定性测试 provider 使用 65536 窗口，使接口测试不混入预算耗尽；真实实验 provider 的配置不变，不声称在原 32768 窗口解决了该问题。生产预算与拒绝逻辑不变。
- 结构化旧测试的“缺少必填参数”改为真实缺少 `condition`，仍要求无子意图、无子回执；实验 CLI 补充具体参数类型、严格 JSON 解码和日志容器类型，清除修改文件中的隐式 `Any`。

首次复现证据为 `regression-before.log`，对应请求与回执位于保留的 `R:/codey-pytest/run-7qz_dtz5/`。开发中一次 schema 测试误用了不存在的工具函数，产生 **1 失败、34 通过**，也保留在 `regression-focused.log`；已改为使用现有 `canonical_json_object`。扩大修复的首次重跑为 **1 失败、35 通过**，记录在 `expanded-fix-1.log`，剩余失败是另一个旧 JSON 切割点；修正后单项 **1 通过**（`misfiled-fix.log`，该轮退出时另有 Windows RAM 临时目录清理警告）。最终验证结果另列，所有首次失败均保留。没有运行自主模型实验，本次不新增模型收益结论；任务及 harness 指纹变化后，历史报告不能当作当前实验资格证据。

进一步纳入共享上传测试后，五文件整轮为 **314 通过、8 失败**（`regression-verified.log`，该文件名同样不代表整轮通过）。其中原四个核心文件的 **310 项全部通过**；新增的 8 项失败均位于 `test_receipt_bound_edit_surface_probe.py`：旧脚本自由 JS 在当前结构化上传阶段被拒绝，组装回答仍发送旧 `summary`，没有实际上传回执。现已复用现有上传 action helper，按 observe → select → read → done 调用，并采用 `paragraphs`。预取来源不能满足上传后读取、提前完成拒绝、导航顺序、上传只执行一次、错误服务器接收声明需纠正、哈希来自回执等断言均保留。该文件中的 JSON 输入也改为严格解码与字段收窄。

修复后执行 `python -m pytest -vv tests/test_receipt_bound_edit_surface_probe.py tests/test_browser_structured_upload_production.py`，**22 项全部通过**（`upload-contract-fix-1.log`）。结合上一轮四个核心文件，分批覆盖 **332 个不同测试场景**；没有运行全量 pytest，也没有将中间两轮失败抹去或写成整轮通过。

补全 JSON 字段的显式类型收窄后，对导航顺序、两种提前完成及四种回答组装再做定向回归，**7 项通过**（`upload-final-type-regression.log`）。该轮与最后上传整轮均正常清理 RAM 临时目录。

最终静态检查：全仓 `python -m ruff check .`、完整 `codey` 树的 `mypy`（709 文件）、`pyrefly`、`ty`（三者均显式指定 `--python-version 3.11`）以及 `python -m tools.kernel_type_precision_gate` 通过。生产 Pyrefly 按现有配置仍显示 35 suppressed / 160 warnings，本轮没有新增豁免。五个修改 Python 文件的 Pyrefly、ty 均无错误；mypy 的文件精度审计使用 `--follow-imports=silent --disallow-any-expr --disallow-any-explicit --disallow-any-generics`，五文件无诊断。此审计只证明这些文件，不代替展开导入依赖的检查。

完整展开测试导入依赖的 mypy **仍未通过**：`final-mypy-imports.log` 保留了 5 个未修改辅助文件中的 **68 条既有诊断**，分别是 `tools/local_model_gate_attempts.py`（2）、`tests/support/provider_actions.py`（6）、`tests/support/kernel_runtime.py`（11）、`tests/test_session_log_receipt_recovery_preserves_facts.py`（48）、`tests/test_mechanical_surface.py`（1）。五个本轮修改文件自身无诊断。未更改检查配置或新增豁免；这些跨测试基础设施的类型债单列待审，不声称测试依赖树类型门禁已通过。所有检查日志仍在上述本机证据目录中。

## 配置与隔离

每个候选配置单独与 A 配对；A 使用当前生产工具、完成门和绑定器，经已有真实 Headless/Chromium fixture 与 ExperimentProvider 文本 JSON schema 接入。它不是完整桌面产品的无干预基线。两侧都将可选第二模型审查标记为不可用，不伪造审查通过。

| 配置 | 独立变化 | 默认实机场景 | 默认两次重复的单元数 |
| --- | --- | --- | --- |
| A | 每组中的生产路径对照 | 与候选相同 | 已包含在下列数量内 |
| B1 | 缺省等待 5000ms；进入 durable 子调用前校验参数 | basic、snapshot_restore、stale_ref | 12 |
| B2 | 原始正文选择器；绑定依赖错误优先反馈；对应来源字段说明 | upload_bound、structured | 8 |
| B3 | 完整步骤契约、结果投影、命名控件、属性读取、全文编辑观察边界 | legacy_structured、upload、hybrid、stale_edit | 16 |
| B4 | 现有授权回放自动调度、原始动作历史与最近页面转换提示 | page_reset、return_failure、download | 12 |
| combined | B1–B4 组合，要求四份合格独立结果 | 原 14 场景，加 page_reset、return_failure | 64 |

B1/B2/B4 不安装旧编辑 helper、正文类型包装、结构化步骤或额外事实报告。B3 自身不补等待默认值、不安装正文绑定器或自动回放；其静态计划校验属于步骤接口本身。组合才启用全部候选。

## 实现边界

**B1。** 仅当 `browser_wait.timeout_ms` 未提供时补 5000。显式 null、布尔值、字符串及超范围值继续拒绝。规范化参数进入原执行器、正式回执和回放身份；不重写模型 JS，不放宽必需能力。声明同步将该参数标成可省略。入场校验保留具体参数错误，避免先进入恢复集合才发现没有合法意图。

**B2。** 重用已验证的 `research_observation.text` 选择器，另外拒绝整个回执被截断的情况。成功父调用、子调用身份、参数、正文完整性和冲突检查仍存在。只调整完成门反馈选择顺序，所有完成检查照常执行。字段缺失或截断时拒绝，不猜值、不伪造 canonical.value、不把模型叙述当来源。

**B3。** 保留逐操作说明和返回数据 schema。浏览器动作使用 `target_name`，缺失和歧义分别报告，并返回最多 20 个真实快照候选及页面身份。`browser_property` 经原 browser_eval 执行。结果统一为 `completed[{id,op,status,data,receipt_ref}]`，字符串投影为 `data.text`；原叶回执不变。静态拒绝、完成门待执行说明均使用 steps 契约。

完整文件观察显式附在本次模型请求中，最多 4 份、合计 12000 字符；超限、部分读取、失败或截断父调用不发编辑句柄。句柄就是原始读取回执身份，无可覆盖的 read_id 状态。全文 edit 只接受本次请求已经交付的句柄，不能使用同计划将来才产生的读取；同路径先重读再引用旧句柄写入也静态拒绝。冲突后必须分计划重读、看到正文、再提交修改。此边界给模型重新计算的机会，不保证任意全文语义正确；字段保留另行验收。本轮没有增加 JSON patch 或任意源码语义验证器。

编辑句柄还会在实际 HTTP 请求发出时重新核对：若上下文压缩删除或截断完整观察，该句柄在本次回复中不可用于写入。程序只能证明完整正文进入了模型请求，不能证明模型确实理解了正文。

B3 执行器不缓存导航、填表、点击或编辑。历史成功不能代表当前页面状态。stale_ref 专用 `snapshot_id` 只能选择同计划更早的真实 snapshot，必须由模型选择，不能把默认新快照当旧引用诊断通过。

**B4。** 只在生产已签发 `repair_of`、错误为 missing_return/result_encoding、原子调用全部成功、无 edit、无已修复父调用时，最多调度一次原顺序/参数回放。原内核验证授权和回执；不重新执行副作用。随后提供正式上传/下载事实以及最近页面转换的历史信息，明确历史不证明当前表单状态。不会自动重复动作，也不承诺阻止模型使用新程序再次执行。没有签发修复凭证的运行时错误、未知副作用和崩溃窗口仍不自动修复。这是窄范围恢复候选，不声称实现了通用状态机或全局 exactly-once。

v1 回放只返回计数，实机发现它不能保证正文交付。v2 在相同授权边界内返回实际回放的原始子结果和回执引用，字符串放在 `data.text`，保留截断标记；数据序列化长度合计超过 12000 时整项省略并显式标记，继续消费全部原序列。来源数据仍不可信，恢复父调用不代表原 JS 语义计算正确。历史机械实验文件不变，具体诊断与实机证据见 B4 复测报告。

本轮结果组装复用生产 json_explanation 路径，B2 改善其正文绑定。没有额外启用上一轮给普通文本答案追加整份事实报告的机制，避免混入未经独立测量的收益。

## 任务与验收

两侧共享相同业务任务。删除旧任务中的 unwrap、browser_read 返回字符串和显式 JS return 教程，属性任务改为“读取 window.__contractToken”。不修改实际数值、权限、来源和完成要求。新请求文字与历史批次不同，不能直接跨批比较总分；A/B 的接口契约也不同，不能说完整 prompt 相同。

原 14 场景继承历史验收，包括真实旧凭证冲突、新读取恢复、外部字段保留、真实旧 DOM 引用拒绝、严格 Research、必要来源 403 后正确 blocked。

- `page_reset`：自主模型须完成两轮导航、填表、点击、等待、读取。第二次导航真实重置页面；按原始叶回执顺序检查两个完整循环及实际正文。只报告旧结果不通过。
- `return_failure`：两侧首步由同一脚本实际下载、读取后返回 undefined，触发真实父结果编码错误。后续模型恢复；验收要求成功修复父回执、只下载一次及最终事实正确。它是故障恢复诊断，不是自主首次工具选择。
- `upload_bound`、`source_failure_fixed`、`return_failure` 单列诊断；常规成功率不包含它们。
- 缺参、非法参数、缺父回执、缺正文、截断、冲突等由离线确定性测试强制覆盖。自主实机里的 basic/upload_bound 并不保证模型恰好触发这些故障，不能拿未发生故障的通过冒充恢复收益。

## 使用方式

先看计划，无模型连接、无文件写入：

```powershell
python -m tools.protocol_surface_ab --plan --config B1
python -m tools.protocol_surface_ab --plan --config combined
```

运行确定性诊断：真实 Chromium、生产内核、脚本化 HTTP 回答，不请求实际模型。目录必须不存在。

```powershell
python -m tools.protocol_surface_ab --diagnostics --run-dir .e2e-artifacts/protocol-diagnostics-01
```

诊断保存 junit.xml 和 diagnostics.json。脚本化回答验证机制行为，不代替模型自主能力。

分别运行独立实机，每条均自动包含 A/B、串行交替 AB/BA、默认两次重复。以下为使用示例；实际批次的命令和目录以 batch-manifest.json、各组 manifest 为准：

```powershell
python -m tools.protocol_surface_ab --config B1 --run-dir .e2e-artifacts/protocol-b1-01
python -m tools.protocol_surface_ab --config B2 --run-dir .e2e-artifacts/protocol-b2-01
python -m tools.protocol_surface_ab --config B3 --run-dir .e2e-artifacts/protocol-b3-01
python -m tools.protocol_surface_ab --config B4 --run-dir .e2e-artifacts/protocol-b4-01
```

可重复传 `--scenario` 缩小或扩展诊断范围；`--repeats`、`--seed`、`--timeout`、`--budget`、`--max-turns` 可预先设定。默认单次请求 90 秒、单元 300 秒、最多 10 轮。服务地址由 `--kobold-base-url` 指定。失败不自动补跑，已有目录拒绝覆盖，超时只终止脚本拥有的进程树。

组合配置要求独立结果满足：同一版脚本和生产源码、同一模型/服务地址/seed/请求超时；每项默认场景至少两次完整配对；候选全部通过且无重复副作用、done 后验收失败、外部字段丢失。脚本重新检查具体记录，不只相信 combination_eligible 布尔值。该条件表示可以组合验证，不表示已有统计显著收益。只运行少数场景、旧代码结果或不完整矩阵不能作为组合前提。

```powershell
python -m tools.protocol_surface_ab --config combined --run-dir .e2e-artifacts/protocol-combined-01 --evidence .e2e-artifacts/protocol-b1-01 --evidence .e2e-artifacts/protocol-b2-01 --evidence .e2e-artifacts/protocol-b3-01 --evidence .e2e-artifacts/protocol-b4-01
```

独立候选失败时，先分析该机制，不跳过前提直接组合。退出码 1 表示存在评分失败、来源变化或配对不可比，不能仅凭进程退出码判断 B 没收益；例如 A 失败、B 成功也会返回 1，应读取 summary.json。

v3 遇 provider_failure 或 worker 异常时改为立即停止后续单元，保留 progress.json、interruption.json，不生成貌似完整的 summary。普通评分失败继续完成矩阵。中断批次不能作为完整对照，也不自动重试失败生成。

## 证据与统计

每组保存 manifest、冻结脚本副本、原始 HTTP 请求响应、提案、编译前后程序、交付观察、真实回执、状态库、首次工具/完成拒绝、产物、超时记录。每侧在独立子进程运行，核对模型与生产摘要；矩阵前后核对脚本摘要。超时不会覆盖 worker 已写的原始摘要。

summary.json 单列完整与常规成功率、静默失败、外部字段丢失、重复上传/下载；每单元保留模型调用、token 完整性、耗时、失败父调用、内部 failed_step 和机械回放次数。缺失用量保留未知，不当零。输入 token、父调用数减少均不能代替任务完成率。快照测试仍仅覆盖 frozen turn contract 序列化恢复，不等于整个实验进程重启。

脚本交付时只验证实现与诊断能力。后续自主成功率、成本收益和恢复证据见[实机报告](protocol-surface-isolated-live-20261111.zh-CN.md)，原始失败单元均保留。

## 本轮开发验证

新诊断 **64 项通过**，旧共享 fixture 回归 **8 项通过**。Ruff 检查新模块、运行器、测试和修改的共享 fixture 通过；strict mypy（explicit-package-bases、follow-imports=silent）检查三个变更 Python 工具文件通过，不代表全部导入依赖均重新检查。

最终通过记录：`.e2e-artifacts/protocol-diagnostics-implementation-02/`，由正式 `--diagnostics` 入口生成，包含 junit.xml 和源码/脚本稳定性记录。此前 `implementation-01` 的 **58 通过、1 失败**保留：发现无 via 声明的完成要求仍提示旧 browser_eval，修复为实际 steps 操作提示后新增测试并重新验证，未覆盖原失败目录。测试中的外部字段 marker 随机生成，脚本化模型从实际交付正文保留它，不提前写死答案。

最终 pytest 退出码 0，仍出现原有 RAM-disk 长路径清理告警；未修改测试基础设施。以上为脚本交付阶段的验证，后续另行授权的自主模型 A/B 记录于实机报告；这些诊断未覆盖或修复此前生产报告中的旧 versioned_content_edit 提示文案失败。

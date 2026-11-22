# Codey 统一能力重构实施方案

日期：2026-11-21（Asia/Taipei）。依据本轮源码检查、相同授权下的工具快照对照、小红书真实观察及两个无模型浏览器机械复现。本文是实施方案，未修改生产实现。

实施主文档已细化为[统一任务与浏览器能力：函数级重构方案](codey-unified-agent-browser-function-plan-20261121.zh-CN.md)，包含参考项目函数映射、签名与职责变化、调用链、测试、迁移和删除顺序。本文保留为较短的阶段概览。

补充约束：本方案本身尚不足以证明浏览器执行层超过 agent-browser。后续实施还须满足[对照静态复审](codey-vs-agent-browser-static-review-20261121.zh-CN.md)补充的依赖方向、引用生命周期、增量观察、动作阶段、调度取消与效果恢复合同。

## 1. 验收目标与设计边界

同一个用户任务内，模型可以执行：读项目、搜索资料、操作浏览器、读取来源、修改代码、运行验证、重新检查页面、继续研究并最终交付。保留同一个任务身份、可接续的浏览器资源和已取得的来源/工具记录。用户不需要选择 Hybrid，也不需要主动提示模型调用某个工具。

任务类型可以继续作为界面、历史记录和默认交付偏好；不再作为互斥能力模式。用户授权决定能力边界，真实资源和已观察事实决定动作前提，交付要求决定何时完成。

延用现有 kernel、TaskPolicy、TaskSession、ExecutionDelegate、ToolSpec、CodeMode、来源账本与 durable 机制。内部继续按职责分模块。首轮不以重命名 ResearchTools 或创建新的 agent/runtime 作为目标。

明确保留：用户显式禁止、只读限制、命令审批、文件原始读取凭据、浏览器目标身份、不可确认效果的防重复机制、来源真实性与历史回执完整性。

## 2. 第一批：统一能力规划

### 改动位置

- `codey/policies/task_policy.py`：`build_task_policy`。
- `codey/operations/kernel_protocol.py`：`controller_allowed_for_session`、`offer_browser_tools`、`offer_browser_call`、`build_turn_snapshot`。
- `codey/operations/browser_followups.py`：`allows_surface`。

### 具体修改

1. 从整体用户目标构建既有授权字段。例如用户要求“先研究再实现”，初始任务应包含实现所需项目权限；研究质量选项不再额外撤掉这些已授权能力。仅要求研究不自动获得修改权限；恢复仍使用原授权。
2. 移除严格研究的全局工具白名单对已授权项目读写、验证工具的排除。研究结果引用绑定仅作用于相关工具参数，不收窄其他能力。
3. 直接浏览器工具的展示按授权、资源和模型显式工具范围判断，移除 project/chat/hybrid/research 的区别待遇。CodeMode 仍调用同一批执行器。
4. 保留 open_result/open_hit 等引用工具对真实已观察目标的要求，但不因为“还没读完来源”就隐藏文件或浏览器检查能力。
5. 结构化复制、上传等专用参数绑定应约束对应动作，不因某一个输入表单就默认隐藏整个任务的其他已授权工具。用户明确声明的固定流程仍可约束其对应工作。
6. 授权集合与模型工具展示继续区别对待。可以压缩说明和按需展示参数，但能力应可发现、可调用，不能靠切任务类型找回。

### 验收

- 相同授权和资源条件下，project/hybrid/research 均保留相同的项目与浏览器可执行能力；报告工具因交付要求不同而变化可以接受。
- 移除 project.write、browser.interact 或明确禁止工具时，任何任务类型都不能执行对应动作。
- CodeMode 与直接工具通过同一快照、权限和执行器，不能产生第二套授权路径。
- 更新 `tests/test_browser_direct_hybrid.py` 中把 project/research 类型本身当作拒绝理由的断言；保留 denied、hidden、只读等真实约束测试。

## 3. 第二批：统一交接与组合交付

### 改动位置

- `codey/operations/kernel_context.py`：`request_snapshot` 和研究提交后的结果处理。
- `codey/operations/browser_task_handoff.py`：`browser_task_handoff`、`_project_edit_handoff`。
- `codey/operations/research_submission_handoff.py`：`research_browser_handoff`、`research_submission_handoff`、`_offer_submission`。
- `codey/operations/kernel_protocol.py`：`_task_tool_specs`。
- `codey/operations/kernel_control.py`、`completion_output.py`、`research_completion_checks.py`。
- `codey/research/answer_contract.py`：报告提交及其 scope 与任务最终结束的关系。

### 具体修改

1. 每轮先形成共同的授权工具、当前资源和任务事实视图，统一处理浏览器观察与恢复。研究引用、原生详情和报告提交的指导在该视图上补充，替换 `research_scope is None` 两条互斥交接路径。
2. `_project_edit_handoff` 等逻辑判断“是否有用户要求的项目修改尚未完成”，不再判断“是否恰好为 hybrid”。交接只提供下一步建议；真实来源、文件及动作前提继续在相应工具上执行。
3. `submit_research_answer` 的主要语义改为提交并验证研究交付物，不能无条件代表整个任务已结束。报告、代码修改和测试结果可以在同一个任务记录内组合。
4. 修改成功提交报告后直接进入 `LEAF_COMPLETION_PENDING` 的分支：只有满足整体任务交付要求时才能结束；仍需编辑或验证时正常继续模型循环。统一最终完成检查，避免另造一个研究任务续接代码任务。
5. `_offer_submission` 不再因研究证据就绪就无条件只留下报告提交工具。纯报告任务可以明确建议提交；混合目标必须保留其余已授权动作。
6. 首轮沿用 `required_checks`、既有来源/文件/验证事实表达组合要求，避免再建一份独立进度数据库。只有现有结构不足以引用已验证报告时才增加必要的报告引用结构。
7. 普通实现任务不强制保存研究笔记或写长报告；正式研究要求仍检查来源、引用与未解决范围。检查可组合，但不把研究过程永久固定成先搜索、后阅读、再编辑的一条顺序。
8. 若提交或最终输出协议改变，连同既有 schema 版本、冻结定义、回执恢复与 provider ack 一起更新。不改写已提交的历史回执，不长期并行维护两套执行运行时。

### 验收

- 用户要求研究后实现时，报告提交后仍可编辑、测试并重新浏览。
- 纯研究报告仍能正确交付；报告引用校验不因统一工具集合而放宽。
- 项目编辑成功但测试失败时，不能因报告已经成功就宣布整体完成。
- 验证两种交替顺序：研究→实现→页面验证，以及实现→遇到问题→研究→修正→验证。
- 修改既有 Hybrid 完成投影测试，验证任意满足相同任务要求的入口都能投影研究与项目产物。

## 4. 第三批：修复浏览器的确定性恢复缺口

### 改动位置

- `codey/automation/browser_session.py`：点击、结果区域观察、`_capture_query_result`。
- `codey/automation/browser_query.py`：查询输出区域绑定。
- `codey/operations/browser_followups.py`：pending 查询投影。
- `codey/operations/task_execution.py`：点击准入。
- `codey/operations/browser_next_step.py`：恢复建议。

### 具体修改

1. 不再把所有点击异常一概分类为 uncertain。利用动作前检查和派发事实识别确定未交付的遮挡、失效目标等情况；无法证明未交付时仍保留 uncertain。
2. 将未确认动作与其目标关联，而不是把 pending 查询当作所有后续点击的全局锁。禁止盲目重放原动作，但允许观察、检查及有依据的恢复动作；恢复不删除原不确定回执。
3. 增加对普通 DOM 输出区的观察路径：允许基于当前观察选择实际结果区域并记录身份，不要求网站必有 aria-controls 或 article 等标记。
4. 正文读取与业务查询确认分别记录。已经读取的真实页面可以继续检查；不能因缺少查询关联就失去所有观察与恢复能力，也不能把任何 DOM 变化自动当作正确查询结果。
5. 正式业务结论仍核对用户查询、结果内容和来源归属。登录弹窗出现、广告变化、时间变化不算业务成功。

### 验收

将已保存的两个机械复现转为正式生产回归：普通 div 已出现结果但确认失败；遮罩拦截查询后关闭按钮被禁止。补充一个动作确实可能已派发的负例，保证修复不允许盲目重复提交。

复现基线：[probe.py](../../.e2e-artifacts/browser-open-world-audit-20261121/probe.py)、[results.json](../../.e2e-artifacts/browser-open-world-audit-20261121/results.json)。

## 5. 第四批：由整个任务持有浏览器资源

### 改动位置

- `codey/runtime/core/tool_resources.py`。
- `codey/operations/research_resources.py`、`task_entry.py`。
- `codey/operations/task_execution.py`、`browser_detail.py`。
- `codey/automation/browser_binding.py`、`browser_session.py`。
- `codey/operations/kernel_control.py`、`codey/durable/questions.py` 的既有问题 continuation。

### 具体修改

1. 在现有 ToolResources 基础上支持父任务持有浏览器 provider 租约，按需要创建。当前 bind_browser 面向绑定不可变的工具任务，不能直接当作父任务“所有导航后 binding 永远相同”的注册器使用。
2. 区分浏览器资源所有权、当前活动页面以及一次人工协助所绑定的页面。任务可以显式切换标签页；等待协助时仍准确定位用户正在处理的那张页。
3. ResearchTools 可继续提供来源和知识接口，但不再因研究/编码阶段返回就销毁整个任务仍需要的浏览器。避免为了命名好看而同时更名所有调用点。
4. 失败抓取也记录可观察和可恢复页面的绑定。保留可人工处理的受限页；网络准入拒绝及真正销毁仍按既有规则处理。
5. ask_user 继续使用既有持久化问题和回答机制。等待时保留页面并展示到前台，不反复刷新登录二维码；任务完成、取消及资源关闭走明确释放路径。
6. 用户回答后接回原浏览器身份和 target，重新观察 UI 并刷新 refs。修复同一 Page 上旧导航响应缓存；SPA 变化不依赖 URL 或 loader 必须改变。
7. 原浏览器仍存活时按记录精确恢复；已消失则明确报告，不能选择另一个同域标签页冒充恢复。

### 验收

- 浏览器→编辑→测试→浏览器保持可接续的任务资源与来源记录。
- 等待用户登录时 finally 不关闭页面；回答后继续原查询意图。
- 用户未真正登录、关闭原页、取消任务及进程恢复均有明确结果。
- 完成与取消后租约释放，无重复关闭，不影响用户借用页面。

## 6. 第五批：补齐共享浏览器观察和动作

### 改动位置

- `codey/automation/browser_observation.py`、`browser_session.py`。
- `codey/operations/browser_references.py`。
- `codey/toolchain/tool_spec.py` 及浏览器工具定义。
- `codey/operations/task_execution.py`、共享任务指导和工具目录。

### 首批必做

1. snapshot 支持按已观察区域或容器缩小范围；前景弹层和任务区域优先。保留上限、范围与截断信息，不只把 80 改成一个更大的数。
2. 补充无名但真实可交互元素，提供所属区域、位置、可访问名称和原始 placeholder 等事实。不因 class 名像 close 就凭空生成一个已确认用途。
3. 补充遮挡与可操作性观察。离屏、内部滚动和被遮罩挡住需要区分；不简单删除所有命中测试失败的元素。
4. 描述式控件绑定基于一个完整的已观察局部范围工作，避免要求整张无限信息流必须完整枚举。原始 ref 的身份与新鲜度校验继续保留。
5. 增加原生按键，并让滚动可以指定容器；必要时补 hover 和 select。只支持 Enter 的搜索不必迫使模型伪造 KeyboardEvent。
6. 将登录识别改为局部 UI 证据加实际动作结果；区分可关闭提示与必需登录，不依赖全文长度和密码框。未知网站失败仍通过通用观察与协助处理。

### 后续按场景推进

frame 选择、复杂 Shadow DOM、弹出窗口等沿同一身份和观察框架补充，不要求首批一次实现所有浏览器 API。现有 CodeMode 保留可组合编程入口，底层仍使用同一个任务浏览器与执行器。

### 验收

长列表不会挤掉前景控件；无名图标可进一步观察；内层评论面板可滚动；Enter 查询可执行；同名按钮按区域正确选择。所有能力同时覆盖编码与研究任务，不再各做一套。

## 7. 第六批：图像回传与完整自然任务

### 图像链路

截图必须真正到达支持视觉的模型，不能仅返回文件路径。检查并修改 BrowserSession、ToolSpec、ProviderToolResult、各 provider codec、工具结果持久化与上下文恢复；沿既有 artifact 存储保存图像身份。

文本输出继续可用。模型不支持视觉时，使用 DOM/控件观察并说明缺失，不把图片路径当作已经看图。首版以当前视口及目标区域截图为主，避免每次点击都返回整页长图。

### 集成验收

固定原本地模型、采样参数和任务，分别衡量能力统一、恢复修复、观察增强的作用；不要同时换模型、提示、工具和网站后归因。

至少覆盖：

- 阅读项目 → 查官方文档 → 操作示例页面 → 修改代码 → 运行测试 → 在原浏览器检查。
- 已修改代码 → 测试失败 → 查询资料 → 修复 → 再验证。
- 研究途中操作网站并提交报告，同时完成用户要求的项目修改。
- 遇登录/验证 → ask_user 等待 → 人工处理 → 重新观察 → 接续原目标。
- 多种弹窗、普通 div、控件重排、延时结果、分页及必要 frame；任务中不预教工具调用序列。
- 只读及显式禁网仍被正确执行。

成功依据真实页面/文件/测试与来源范围，而非只看 stop_reason=done。保留失败样本、实际工具请求、观察、动作回执与最终输出。

## 8. 提交与验证顺序

| 批次 | 主要可审阅成果 | 进入下一批的条件 |
| --- | --- | --- |
| 1 能力规划 | 同授权下的跨类型工具一致性及权限负例 | 不再按活动模式剔除已授权能力 |
| 2 交接与完成 | 报告、代码、测试共同完成一个任务 | 中间研究交付不提前终止实现任务 |
| 3 浏览器恢复 | 两个机械失败的生产修复与回归 | 普通结果可继续确认，遮挡可恢复，无盲目重放 |
| 4 资源与协助 | 父任务所有权、等待与恢复 | 页面可跨活动接续，释放与失败准确 |
| 5 观察与动作 | 区域观察、图标、遮挡、按键与容器滚动 | 真实复杂页面可操作性有确定性测试支持 |
| 6 图像与集成 | 真实图像链路、同模型自然任务结果 | 用户不切模式即可完成跨能力目标 |

现有回归优先复用任务策略、工具快照/冻结恢复、browser handoff、来源阅读、Hybrid 完成、问答恢复与 CDP 生命周期测试。扩展检查只在改动涉及对应行为时执行；最后再跑仓库要求的 lint、类型检查及相关集成集合。不能因为旧断言体现了能力隔离就保留错误目标，也不能为通过新目标而删除权限与真实性负例。

源码目前有另一批进行中的修改，涉及 kernel_context.py、kernel_protocol.py、task_entry.py 等共同文件。实际实施时应基于现有改动定点合并并检查差异，避免整文件覆盖。重构完成后删除被替代的模式专用路由和重复拼接，不长期积累新旧两套选择器。

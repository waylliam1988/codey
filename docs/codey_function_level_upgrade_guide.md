# Codey 函数级改造指南

本指南保留审计基线时的设计建议。本轮实现、实机结果、首次失败及剩余边界见 [后续机制验证](reports/followup-mechanisms-validation-20261122.md)。

CodeMode 来源计算、JS/TS/Python 语义查询、语义修改与 finding 阶段控制的追加实现和实测见 [计算与语义验证](reports/codemode-analysis-validation-20261122.md)。

源码基线为 `1fb04490`。带「建议新增」的函数、字段和测试名是实施方案，其他函数均可从相对链接找到。七个参考仓库的版本和行为出处见 [源码审计](codey_deep_audit_and_benchmark.md)。全部改造沿现有单 Agent 工具循环完成，使用原有项目权限、工具回执、durable 会话、Ghost 和知识库。

| 合并顺序 | 使用者看到的变化 | 过关条件 |
| --- | --- | --- |
| P0-1 至 P0-4 | 一条混合任务可以查文档、操作页面、改代码并验证 | 正常入口和恢复入口得到同一套可核验结果 |
| P1-1 至 P1-3 | 新会话检索旧项目结论，换 API 模型后偏好和未完成事项可用 | 能追溯来源，支持修改与撤销，跨项目隔离经过测试 |
| P2-1 至 P2-3 | 更少编辑失败和浏览器重复观察，研究能围绕冲突与缺口补证 | 对照相同任务测成功率、误动作、引用准确率与成本 |
| P3 | 根据重复任务和生产数据决定高级能力 | 有可复跑的对照与失败样本 |

## P0 真实入口的混合任务

### P0-1 将混合意图接到现有任务入口

**先写一个端到端用例。** 给临时仓库放一个 Python 服务和一个 TypeScript 页面。让脚本化 provider 按真实 ToolSpec 查固定文档、打开夹具页面、点击并读回状态、编辑代码、运行两个检查、提交完成。把正常合约跑到红灯位置，记录是路由、权限、工具回执还是完成证据出问题。随后加入用户明确指定 `research`、用户要求只读、恢复同一任务、无项目等对照。使用现有混合任务和浏览器入口测试夹具。 [混合入口测试](../tests/test_browser_entry_surface.py#L99) · [任务入口](../codey/operations/task_entry.py#L635)

1. 在 `prepare_submission()` 所在的提交链查清请求何时进入 durable admission。若自动任务确实被路由为纯项目，建议在首次持久化前加纯函数 `infer_auto_hybrid(request: TaskSubmission) -> bool`。只考虑自动请求、有明确项目、同时表达网页调查与项目修改的情况。优先复用已解析的 `TaskInput`、显式网页意图和授权字段。网页正文、代码样例、URL 本身都不能产生写权限。用 `replace(request, intent="hybrid")` 或等价的已解析字段，让首次冻结的路由同时供 `_ensure_durable_task()` 和 `_setup_run_state()` 使用。检查 UI、CLI 已预先冻结的提交是否走同一入口。恢复时读任务 checkpoint 中的路由和策略。 [提交准备](../codey/operations/task_run.py#L186) · [durable 准入](../codey/operations/task_phases/lifecycle.py#L80) · [任务类型](../codey/task/kind.py#L117) · [网页意图](../codey/task/web_intent.py#L160)
2. 沿 `build_task_policy_for_entry() → _entry_policy_with_recovery() → _entry_executors()` 检查项目、网页和知识授权。`run_task_mode()` 的 `hybrid` 分支已经使用同一 `TaskSession`。需要网页写操作时依现有权限与回执执行，代码验证按 P0-2 收口。显式 strict Research 的写入条件依旧由 `build_task_policy()` 处理。 [入口权限](../codey/operations/task_entry.py#L108) · [执行器](../codey/operations/task_entry.py#L191) · [混合路由](../codey/operations/task_entry.py#L570) · [授权](../codey/policies/task_policy.py#L231)
3. 用已有 `open_url`、`browser_read`、`edit` 和 `run` 回执串接各步骤。研究结论通过现有来源和 evidence 引用进入报告或项目上下文。浏览器直调用于单步，依赖顺序的网页动作可以经 CodeMode。完成时由 `_entry_project_receipt()` 提供代码验证证据，`_enrich_hybrid_outcome()` 汇总研究和工程显示。写入 durable 的证据需要引用完整回执，UI 的摘要字段只负责显示。 [项目回执](../codey/operations/task_entry.py#L265) · [混合结果](../codey/operations/task_entry.py#L601) · [最终结算](../codey/operations/task_phases/settlement.py#L343)

**验收**　同一个 run ID 下能查到来源、浏览器观察、文件写入、两项验证及最终结论。省掉任一步后，完成结果指出缺项。恢复时不会重复已经投递的浏览器动作或项目副作用。

### P0-2 将单验证候选扩展为检查组合

`select_verification_candidate()` 先要求同一候选覆盖所有路径。原测试用例可保留单命令路径。下面的建议实现复用单候选选择器。把它加入生产文件时可用该文件已有 imports。命令执行、回执验证和完成判定由后续调用链负责。 [候选选择](../codey/completion/verification_policy.py#L788)

```python
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from codey.completion.edit_scope import is_document_path
from codey.completion.verification_policy import (
    VerificationCandidate,
    select_verification_candidate,
)

@dataclass(frozen=True)
class VerificationObligation:
    candidate: VerificationCandidate
    paths: tuple[str, ...]

@dataclass(frozen=True)
class VerificationPlan:
    obligations: tuple[VerificationObligation, ...]
    uncovered_paths: tuple[str, ...]

def select_verification_plan(
    candidates: Sequence[VerificationCandidate],
    changed_paths: Sequence[str],
    *,
    root: Path | None = None,
) -> VerificationPlan:
    paths = tuple(sorted({
        path.replace("\\", "/")
        for path in changed_paths
        if path and not is_document_path(path)
    }))
    selected = select_verification_candidate(candidates, paths, root=root)
    if selected is not None:
        return VerificationPlan((VerificationObligation(selected, paths),), ())

    grouped: dict[tuple[str, str], tuple[VerificationCandidate, list[str]]] = {}
    uncovered: list[str] = []
    for path in paths:
        candidate = select_verification_candidate(candidates, (path,), root=root)
        if candidate is None:
            uncovered.append(path)
            continue
        _, covered = grouped.setdefault((candidate.cwd, candidate.command), (candidate, []))
        covered.append(path)
    obligations = tuple(
        VerificationObligation(candidate, tuple(covered))
        for candidate, covered in grouped.values()
    )
    return VerificationPlan(obligations, tuple(uncovered))
```

1. 优先使用现有函数找到唯一且覆盖全量的检查。找不到时，对每个尚未覆盖的代码路径调用 `select_verification_candidate(candidates, (path,), root=root)`，按 `cwd` 和命令归并。同优先级冲突、候选缺失与文档改动各按原有规则处理，无法覆盖的代码路径放入 `uncovered_paths`。针对 `pytest tests/x.py`，以明确验证约定、已知覆盖映射或可信配置限定它能覆盖的路径。 [路径和运行时逻辑](../codey/completion/verification_policy.py#L554) · [匹配](../codey/completion/verification_policy.py#L845)
2. 在 `refresh_verification_candidates()` 计算最新计划，替换单候选的调用链时同步处理 `_resolve_selected_check()`、`_verification_checks()`，以及 Project 单独的 `validate_stopped_candidate()`、`_prepare_completion_enforcement()`、`_apply_repair_result()`。每个义务都只用其路径子集检查对应回执。同 workspace revision 和 fingerprint 的成功才算 fresh。相关失败优先，老的恢复记录按既有继承规则处理。旧单检查数据可解释为单义务。 [刷新](../codey/operations/project_verification.py#L25) · [核验](../codey/operations/project_completion_checks.py#L233) · [停止后验证](../codey/operations/project_candidate_validation.py#L89) · [工程收口](../codey/operations/project_completion_enforcement.py#L178) · [工作树约束](../codey/completion/verification.py#L368)
3. 把未验证义务展示到 completion feedback。让真实 `run` 检查产生回执。新增检查后在完成证据中保留命令、工作目录、路径集合和版本。加载候选失败时沿用已有保守行为，完成反馈说明需要重试刷新。

**验收**　`service/core.py` 和 `web/component.ts` 各有一项检查，两项 fresh pass 才满足验证。仅 Python 通过、任一失败、执行后又编辑、候选歧义各自得到可解释结果。单语言、用户禁止验证、只改文档的历史用例继续覆盖。 [验证策略用例](../tests/test_verification_policy.py)

### P0-3 大页面与 SPA 点击确认

#### 优先让截断页面可以下钻

沿 `collect_snapshot() → BrowserRuntime._observe() → BrowserReferences.observe()` 查当前版本。`BrowserSession.snapshot()` 已经接受 `scope_ref`、`selector`，先把现成能力在截断反馈里表达清楚。随后借鉴 Agent-Desktop 的锚点计划，从当前文档已观察的语义容器产生少量可下钻入口。 [采集](../codey/automation/browser_snapshot.py#L114) · [观察入口](../codey/automation/browser_runtime.py#L553) · [Session](../codey/automation/browser_session.py#L112) · [参考实现](https://github.com/vercel-labs/agent-desktop/blob/b43ac4f6/crates/core/src/refs/ref_skeleton_anchors.rs#L12)

- 在 `collect_snapshot()` 建议增加可选 `scope_suggestions`，每项含真实容器引用和可读标签，标为仅限观察。若需新增引用种类，动作解析明确校验引用种类，作用域解析继续核对文档、frame 与活节点。
- 在冻结工具面和展示层提示下一次调用 `browser_snapshot(scope_ref=...)`。`BrowserReferences` 只在完整的当前范围内生成可读绑定。范围仍截断时继续下钻，原始 ref 走既有节点校验。
- 端到端覆盖 150 控件页面中第 120 个按钮、禁用的同名邻居、跨 iframe 同名、换页后旧 scope ref、同范围增量快照。运行并扩展现有截断约束测试。 [引用](../codey/operations/browser_references.py#L46) · [回归](../tests/test_browser_readable_references.py#L92)

#### 把查询未确认处理成明确的状态

`_query_baseline()` 捕获点击前基线，`_query()` 读取同文档中的目标区域。`read_browser_source()` 落可回放读回执，`pending_click()` 从回执算等待，`_browser_completion_block()` 决定能否完成。借鉴 Agent-Desktop 的投递语义，把「点击已经投递」与「业务结果已经证实」分别记录。 [基线](../codey/automation/browser_runtime.py#L806) · [读取](../codey/operations/browser_detail.py#L199) · [待定查询](../codey/operations/browser_followups.py#L206) · [完成检查](../codey/operations/completion_gate.py#L485) · [参考语义](https://github.com/vercel-labs/agent-desktop/blob/b43ac4f6/crates/core/src/actions/delivery_semantics.rs#L6)

1. 在 `_query()` 区分可确认的结果、仍在等待、无绑定目标、文档改变。已有绑定区域按原逻辑核对稳定变更。无区域时，用用户指定且能唯一绑定的结果容器、与操作相关的可验证状态或同文档的业务结果读回来形成证据。整页文字变化只能作为重新观察线索。
2. 在 `read_browser_source()` 给非成功结果输出带 `query_click` 的诊断回执。让 `unconfirmed_clicks()` 只消费同一点击、同一文档的确定状态。允许重新观察或给出清楚的阻塞结果。状态投影从 durable 回执推导，恢复时用相同规则。
3. `_browser_completion_block()` 对用户要求的业务结果继续检查证据。确认不了时说明缺少哪种读回。可确定未投递时才考虑安全重试。已投递或不确定时先观察，避免重复提交表单。

**验收**　同文档无 `aria-live` 的真实状态更新能经范围化读回验收。无更新、只有无关内容变化、换文档、错误目标与恢复中的不确定投递分别产生正确结果。单独记录研究报告修订时的读取次数来定位现有回归。 [浏览器回执回归](../tests/test_browser_reference_choices.py#L99) · [报告修订回归](../tests/test_research_report_tail.py#L89)

### P0-4 统一复核网页数据边界

- 在 `render_opened_source()` 明确来源文本的显示与证据表示。模型可见呈现使用结构化的非可信数据边界，引用和偏移继续针对原始来源。借鉴 HyperResearch `wrap_body()` 对来源呈现的处理，验证来源正文无法修改工具权限、写入范围或允许的下一步。 [来源渲染](../codey/research/source_rendering.py#L18) · [参考渲染](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/untrusted.py#L42)
- 沿 `ResearchSourceGateway.open() → _admit_source_url() → 实际 fetch` 分别核对直连、重定向、DNS 解析和代理模式。生产决策在 `NetworkPolicy`，需要约束连接目标时核对传输层是否执行了决定。用模拟解析与连接夹具覆盖允许来源、拒绝来源和允许来源发生地址变化，显式授权访问项目本地页面由浏览器原有任务权限路径处理。 [来源入口](../codey/research/source_gateway.py#L140) · [网络策略](../codey/policies/network.py#L66)

## P1 知识、记忆与跨模型续跑

### P1-1 把验证过的工程和网页事实写进版本化知识

**先追一次读写。** 复现实测缺口，用 `record_project_memory()` 写实施笔记，在新会话从 `search_notes()` 和 `search()` 读取同一独特词。`knowledge_background()` 的召回走后者。`hybrid` 已产出项目回执，`AppContext.finish_run()` 已经有终态观察入口。建议共用终态事实投影，覆盖 Project、Hybrid 与有可复用观察的 Browser 任务。 [工程笔记](../codey/operations/project_completion_flow.py#L346) · [搜索入口](../codey/knowledge/store.py#L66) · [回执](../codey/operations/task_entry.py#L265) · [终态](../codey/app/context.py#L509) · [召回](../codey/operations/knowledge_context.py#L15)

1. 在终态投影处建议新增 `project_completed_observations(task, settled_receipts) -> list[KnowledgeMutation]`。按已完成的任务及对应最终版本选取事实。例如「修改 `web/search.ts` 后两项检查通过，已观察到一次同文档更新」。正文记录现象、被验证的改动和检查，出处记录 run ID、持久化回执、文件版本或来源版本。未验证的模型根因用候选推断，网页只投影能复用且被证实的观察。移除页面私密内容、敏感字段和短时会话数据。
2. 复用 `KnowledgeObject(kind="observation", origin="observed_result", scope="project", scope_id=...)`。代码项目的 `scope_id` 使用与 `KnowledgeAccess` 一致的规范身份。无项目的观察默认保留在会话。关联研究证据时使用已读过且拥有权限的 `KnowledgeReference(target, revision, relation)`。只保存能够解析的引用。`commit_knowledge()` 接收确定性的提交 ID 和 `KnowledgeMutation.expected_revision`，同一个运行重试不会新增重复知识。 [知识类型](../codey/knowledge/model.py#L35) · [访问与召回](../codey/knowledge/recall.py#L20) · [提交](../codey/knowledge/store.py#L184) · [幂等提交](../codey/knowledge/repository.py#L91)
3. 在终态 durable commit 后执行一处投影。复用结算或重建入口实现同 ID 重试，避免进程退出后永久漏写。投影失败在 run 诊断中可见，任务完成状态维持其真实结果。旧实施笔记继续通过兼容读取可见。若实测需要迁移，按已验证回执做可重复的回填，再决定是否减少旧写入。研究继续使用现有 `publication_command()`。注意自定义 `state_home` 当前默认停用知识库，集成测试显式注入库，产品配置另行明确启用与数据位置。 [结算顺序](../codey/operations/task_phases/settlement.py#L343) · [研究发布](../codey/research/answer_archive.py#L148) · [知识库配置](../codey/app/context.py#L148)

**验收**　同项目新会话查到工程观察，可追溯到通过的版本和命令。其他项目或用户权限无法查到。失败或取消的任务不会产生已验证知识。重复结算只得到一版，来源更新后旧结论显示待复核。

### P1-2 Ghost 从提取、确认到召回

借鉴 Codex 的两步处理。任务结束时提取带证据的候选，之后用已有 Ghost 维护批次做归并和冲突提示。用户确认或纠正的内容在本地生效，并且可追溯与撤回。 [Codex 第一阶段](https://github.com/openai/codex/blob/970111f6cb/codex-rs/memories/write/src/phase1.rs#L55) · [第二阶段](https://github.com/openai/codex/blob/970111f6cb/codex-rs/memories/write/src/phase2.rs#L50) · [Ghost 批次维护](../codey/ghost/maintenance.py#L22)

1. 改 `candidate_schema()` 和 `candidate_from_payload()`。新候选只需 `kind`、`text`、`quote`，保留可选的成对偏移用于历史兼容和消除重复原话的歧义。宿主从 `user_text` 计算准确位置。先校验原话非空并在输入中原样出现。唯一匹配直接接受，零匹配拒绝，多处匹配仅在既有合法偏移明确指中一处时接受。仍输出 `MemoryCandidateDraft` 和确切偏移，`delivered_candidates()` 继续校验接受回执并记录拒绝原因。 [schema 与解析](../codey/memory/candidates.py#L18) · [已送达候选](../codey/app/memory_candidates.py#L92)
2. `observe_terminal()` 默认产生项目或会话候选。在 `memory_routes._decide()` 的用户确认面建议增加明确的「保存为跨项目偏好」选项，并让用户看到将保存的文字。首次交付可复用显式 `user` 提议和确认。若要求删聊之后仍可保留，定义必须独立存在的用户确认记录，再让 `source_available()` 与 `_delete()` 认可这一独立来源。仅改 item.scope 会留下旧会话 provenance 和 control scope，仍触发清理或作用域过滤。确认版本、确认后的文字、作用域与请求 ID 一起提交。删除会话可撤下其来源和未确认候选，独立确认过的文字按用户选择保留，显式忘记应清理其召回和派生项。历史记录按原有依赖处理。 [终态候选](../codey/app/memory_setup.py#L72) · [来源存活](../codey/app/memory_setup.py#L23) · [确认](../codey/app/memory_routes.py#L103) · [删除](../codey/ghost/projection.py#L182)
3. 在 `GhostMemory.retrieve()` 先执行现有作用域、到期、撤销和 family 控制过滤。给用户确认且适用于当前上下文的稳定偏好预留一小段预算，剩余名额沿用相关度与反馈排序。注入仍经过 `admit_context()`，沿用来源、作用域和预算检查，并让用户能查看哪条记忆进入了本轮。归并只处理确有重复依据的同槽位条目，冲突应可说明与纠正。 [召回](../codey/ghost/backend.py#L67) · [注入准入](../codey/workspace/context_admission.py#L37)

建议在 `candidate_from_payload()` 内调用如下纯函数，再沿用现有的字段长度、作用域和元数据校验。长度限制在调用前校验。对历史响应提供的偏移，保留原有精确验证逻辑。

```python
def locate_candidate_quote(
    user_text: str,
    quote: str,
    *,
    legacy_span: tuple[int, int] | None = None,
) -> tuple[int, int]:
    if not quote.strip():
        raise ValueError("memory candidate quote is empty")
    if legacy_span is not None:
        start, end = legacy_span
        if 0 <= start < end <= len(user_text) and user_text[start:end] == quote:
            return start, end
        raise ValueError("memory candidate does not quote the bound user input")
    start = user_text.find(quote)
    if start < 0:
        raise ValueError("memory candidate does not quote the bound user input")
    if user_text.find(quote, start + 1) >= 0:
        raise ValueError("memory candidate quote is ambiguous")
    return start, start + len(quote)
```

**验收**　中文、emoji、重复原话、旧偏移字段、捏造引语分别覆盖。跨项目只召回已确认的全局偏好。删聊与忘记按各自语义执行，重启、投影重建、重试和用户改口得到同一结果。 [记忆生命周期](../tests/test_memory_lifecycle.py)

### P1-3 在 durable 历史上做跨模型回放

1. 在 `build_conversation_plan()` 分离三个输入。用上一次可靠的 `ProviderBinding` 与当前绑定判断连接、模型和协议变化。用传输历史是否为空判断冷恢复。用目标预算导致的窗口重建判断 rollover。`WindowOpenReason` 已有 first open、model change 和 context rollover。若现有诊断无法明确表达冷恢复，建议新增 `TRANSPORT_RECOVERY` 并覆盖序列化与旧窗口兼容。触发了 `OpenWindow` 仍按现有恢复规则执行。 [计划](../codey/operations/conversation_plan.py#L83) · [窗口原因](../codey/runtime/core/window_action.py#L12) · [绑定](../codey/runtime/core/protocol_window.py#L1)
2. 在 `activate_window() → _restore_context() → ApiProvider.load_durable_context()` 保持 durable 历史和来源 ID。借鉴 Pi，建议加只在重建 API 视图时调用的 `transform_replay_messages(messages, target_binding)`。成对转换工具调用和结果，映射须确定且处理冲突。丢弃无法在目标复用的签名或加密状态。图片沿现有能力检查变为文本提示或有权读取的附件引用。扩展一条消息时继续对应同一来源 entry ID。框架仍用现有 codec validate 与最终计量。协议转换完成后才替换内存视图，避免失败留下半激活状态。 [激活](../codey/operations/conversation_window.py#L28) · [API 重建](../codey/providers/api_provider.py#L381) · [Pi 转换](https://github.com/earendil-works/pi/blob/ed4f48840/packages/ai/src/api/transform-messages.ts#L91)
3. 目标窗口更小时，从 `build_context()` 的 omitted IDs 找真实缺口。在切换前或冷恢复前，保留用户约束、已验证证据、文件及回执、未完成步骤和显式删改。已有 `commit_context()` 可用来源 entry ID、revision 和 head 写 durable `context_edit`。仅在来源仍有效且计量后能降低压力时接受摘要，提交条件不满足则重建候选。摘要不可用时给出确定性的关键事实交接和省略诊断。有未结算工具动作时复用当前恢复语义，等动作结算或明确记录不确定结果后再跨协议。 [投影](../codey/durable/context.py#L25) · [压缩提交](../codey/providers/api_context.py#L198) · [OpenCode 预压缩](https://github.com/anomalyco/opencode/blob/3ba597f776/packages/core/src/session/compaction.ts#L332)

**验收**　同模型连续回合、同模型冷重启、真实切小窗、用户编辑历史、含工具调用和单工具多结果的回放、非视觉模型降级都单独覆盖。确认目标协议通过校验，源 entry ID 与工具结果仍配对，已执行动作未重放。先用脚本化 provider 验证请求结构，再用相同测试矩阵跑支持的实际 API；缓存收益按连接记录。 [上下文权威回归](../tests/durable/test_provider_context_authority.py) · [压缩绑定回归](../tests/test_provider_durable_compaction_binding.py)

## P2 编码、网页和深研的单项改造

### P2-1 编码编辑与多文件 Patch

1. 在 `edit_file()` 保持精确替换为首选。只有原文未匹配时，在 `_edit_anchor()` 附近增加逐级匹配。先做换行视图，再尝试行尾空白等去歧义规则。匹配必须唯一，否则展示少量行号与上下文，由模型重读后提交。借鉴 Pi，把规范化视图的命中范围映射回原文，替换区域以外的字节从原文件保留。分别验证 CRLF、LF、混合换行、无结尾换行及重复片段。 [Codey 编辑](../codey/toolchain/runtime.py#L708) · [现有锚点](../codey/toolchain/runtime.py#L491) · [Codex 逐级匹配](https://github.com/openai/codex/blob/970111f6cb/codex-rs/apply-patch/src/seek_sequence.rs#L12) · [Pi 写回](https://github.com/earendil-works/pi/blob/ed4f48840/packages/coding-agent/src/core/tools/edit-diff.ts#L132)
2. 整文件更新若确有需求，在 `edit_base_version()` 增加可信文件版本的读取方式。分页读取保留页边界、同一文件版本和原始读取回执。若支持组装整文件基线，检查同任务、同路径、同版本、从首行到末行连续覆盖。模型回传的哈希只用于选择已有回执，写前宿主复核当前原始内容或与版本一致的证据。精确替换仍适合长文件上的局部编辑。 [读取](../codey/toolchain/runtime.py#L841) · [整文件基线](../codey/operations/edit_base.py#L15)
3. 当多文件编辑失败样本足够，借鉴 DeepSeek Harness 拆成建议函数 `preflight_patch(root, patch) -> PatchPlan` 和 `apply_patch_plan(plan) -> ToolOutcome`。预检在内存模拟顺序变更，归一化路径、检查当前文件版本、所有锚点、权限和重命名冲突。全组预检成功后通过现有执行器、变更跟踪和副作用记录写入。写入前复核版本。部分写入后失败要保留已写清单及当前版本。仅在安全归属可证明时回滚，否则汇报部分成功和冲突。 [参考预检](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392e/packages/fs/tool-fs/src/patch/preflight.ts#L146) · [Codey 执行](../codey/operations/task_execution.py) · [工作区变更](../codey/workspace/changes.py)
4. 对真实失败任务增加只读语义查询。借鉴 DeepSeek Harness 的 stdio 生命周期，建议先接一个语言服务器和 `definition`、`references` 或 `diagnostics` 中最需要的一项。查询以工作区和文档版本为界，启用超时、取消与关闭。写后差量诊断建立在同版本查询结果上，并与 P0-2 的正式验证证据并存。 [参考文档生命周期](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392e/packages/lsp/lsp-stdio/src/instance.ts#L150)

**验收**　匹配歧义无写入。并发编辑保护有效。第二个文件预检失败时第一文件无变化。模拟写入中断后能明确指出已写文件并恢复。语义查询超时后可继续运行，旧版本诊断不会计入新版本完成证据。

### P2-2 批量页面快照与动作可操作性

1. 先给 `collect_snapshot()` 统计 DOM/AX 采集耗时和 RPC 数，固定普通页、150 控件页、隐藏表单和跨 frame 夹具。借鉴 Agent-Browser，将逐控件的可见性、语义、边界与容器采集成每 frame 的少量批量读，和 `collect_ax_tree()` 结果按可靠节点身份合并。保留仅 AX 可见控件、可操作的隐藏表单关联和局部边界。无法建立可信身份的条目可显示为观察，动作继续走现有引用校验。 [Codey AX](../codey/automation/browser_snapshot.py#L74) · [参考快照](https://github.com/vercel-labs/agent-browser/blob/4b0e9f0/cli/src/native/snapshot.rs#L430)
2. 已有 `diff_snapshot()` 可复用。范围或文档变化发全量，稳定同范围按已有添加、变化、删除输出差量。大比例改变的回退策略通过真实 payload 尺寸实测再定。`_resolve()` 保留版本、语义和当前文档检查。React 重渲染要重绑时，仅在原目标与活节点都能证明唯一且操作风险允许的情况下考虑。 [快照差量](../codey/automation/browser_snapshot.py#L201) · [引用解析](../codey/automation/browser_runtime.py#L1117)
3. 从现有元素动作和 `_HIT_TEST` 接入多点遮挡验证与短时稳定采样，最终在验证过的点执行。借鉴 Agent-Desktop 将几何稳定和投递作为可测试判断。动作后用对应业务状态判断是否完成，例如 checkbox 的 checked 状态、表单字段读取、点击后的绑定区域。处理对话框或用户介入时留下明确的投递状态。 [动作输入](../codey/automation/browser_input.py) · [参考落点](https://github.com/vercel-labs/agent-desktop/blob/b43ac4f6/crates/core/src/actionability/evaluate.rs#L160)

**验收**　用同一页面测 P50/P95、RPC 数、正确定位率、错误点击和重复提交。额外测同名控件、遮挡、重渲染、frame 和标签切换。

### P2-3 用证据缺口和反证推动研究

1. 从现有 `ResearchLedger` 构造按子问题分组的证据。建议新增纯函数 `evidence_gaps(ledger, questions) -> tuple[ResearchGap, ...]`。每个缺口引用 `question_ref`、现有 passage、候选原因和还需要的来源类型。原文支持、反证、结论成立条件、证据不足分别表示。按同实体、单位、时间、场景等条件归组，避免把不同实验条件当矛盾。先在会话 ledger 投影这份信息，确认需要跨运行索引再入版本化知识。 [问题关联](../codey/research/questions.py#L41) · [证据项](../codey/research/ledger.py#L311) · [参考立场矩阵](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/claims.py#L342)
2. 把缺口接入 `build_turn_snapshot()` 的取证到提交决策。只在合同允许取证、预算和剩余轮次充足、目标缺口尚未试过时，调用 `build_research_plan()` 形成有限的查询建议。本次仍使用同一个 `TaskSession`、ledger 和搜索工具。用问题、缺口原因与证据标识判定是否已尝试，沿现有采集配额扣减，并确保恢复后继续读已尝试过的回执。预算耗尽或无法补证就交明确的 partial 或 unresolved。冻结提交后，格式修订复用原证据。若确需补新证据，走显式研究阶段转换再冻结。 [工具快照](../codey/operations/kernel_protocol.py#L431) · [计划](../codey/research/query_planner.py#L169) · [提交回执](../codey/operations/task_execution.py#L920) · [冻结后修订回归](../tests/test_research_report_tail.py#L89)
3. 在 `compile_answer_report()` 或其现有证据校验层验证引语和数字。先解析引用标签，定位到对应来源的版本化 passage。逐字引语按可解释的空白规则匹配，并保留原文偏移。数字检查由实体、数值、单位、分母、时间及条件组成。确定伪造或绑定错误才阻止提交，未核实的改写和派生数值进入待说明。需要运行分析时，使用已授权的项目执行器，保留输入文件版本、脚本、命令和输出回执，报告注明派生结果的计算过程。 [报告编译](../codey/research/answer_report.py#L192) · [证据归档](../codey/research/answer_archive.py#L55) · [参考引文](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/citecheck.py#L257)
4. 在 `ResearchLedger.record_open_document()` 的来源入账链路扩展同源信息。优先对已验证的 DOI、规范 URL、转载指纹做聚类。近重复聚类也须保留各自来源与版本。同一主源只计一次独立支持，引用关系按具体论点确定。对论文类研究再于 `ResearchSourceGateway.open()` 的数据获取路径增加 OA 候选，全程使用同一出站边界，保留全文版本和替换来源。撤稿状态保留已确认事实，接口缺失或超时单独记录。 [来源入账](../codey/research/ledger.py#L205) · [获取](../codey/research/source_gateway.py#L140) · [参考独立性](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/independence.py#L61) · [参考全文](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/oa.py#L835)

**验收**　冲突结论触发一次针对性搜索，重复转载只有一组独立证据。预算耗尽时带着未解决缺口结束。用「引语出现在别的文章」「相同数字关联不同实体」「单位不同」「计算结果可复现」四种文档验证引文。格式修订只执行提交，保留原来源回执。

## P3 同题评测与后续投入

| 场景 | 对照设计 | 记录结果 |
| --- | --- | --- |
| 编码 | 同仓库基础提交和任务，覆盖单文件、多语言、并发改动 | 测试通过率、修改范围、回滚恢复、耗时与 token |
| 网页 | 同站点夹具和动作目标，区分观察、点击和业务确认 | 完成率、误点、重复提交、RPC 及耗时 P50/P95 |
| 研究 | 固定可访问的资料集，加入来源冲突、转载、词法改写和派生数字 | 结论证据覆盖、引文对齐、有效独立来源、补证质量和成本 |
| 混合与连续性 | 固定一条主任务与相关后续任务，更换连接或进程并变动证据版本 | 端到端完成率、回执复用、关键事实保留、偏好正确召回、撤销传播 |

脚本化 provider 先验证工具合同与可恢复性。实际模型对照固定任务、工具权限、可访问页面、完成条件、最大轮数和预算，记录模型与适配层差异。重复场景的指标稳定后，再决定是否接 OpenCode 的影子快照、Agent-Browser 的只读 Flow、学术来源全文扩展和知识混合检索。知识混合检索先看范围内的 recall@k、旧知识误召与延迟，再决定是否引入 embedding。网页交互可靠之后，可用控制台、页面错误和请求回执验证 DevTools 诊断价值。Windows 写隔离和 CodeMode 调度优化分别用作用域隔离与卡住案例衡量。 [对比审计](codey_deep_audit_and_benchmark.md#七个参考版本及可迁移机制)

## 第一批实现的拆分

1. **提交 A**　增加混合入口夹具，修复它实际遇到的路由或调用问题。先用显式 `hybrid` 证明链路，再验证普通自然语言入口。
2. **提交 B**　把 Project 和 Hybrid 的单候选验证改为按路径覆盖的检查组合，补旧回执和恢复测试。
3. **提交 C**　让截断快照提供范围入口，让点击未确认得到可诊断结果。单独解决现有报告修订重复读取的来源。
4. **提交 D**　接通已验证工程事实的版本化写入与召回，再交付 Ghost 跨项目偏好与 API 续跑。

第一批的最终检查是同一个项目中的后续任务能说清楚「上次依据哪篇资料、观察了哪种页面状态、做了什么改动、哪些检查通过」。更换 API 模型后，它仍能使用这些事实和已经确认的用户偏好。

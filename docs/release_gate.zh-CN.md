# Codey 发布门槛（Release Gate）

> 借鉴 `reference-projects/opencode`：bug 理论上永远改不完，发布不靠“零 bug”，靠**固定门槛全绿**。
> 本文档是 codey 的发布门槛标准。暂不发版时也要按此门槛跑实机，记录结果。

## 0. 结论（给语音输入的你）

- opencode 的门槛 = `unit（linux+windows）` + `e2e（playwright app）` + `typecheck` + `lint` + `generated client 检查` + `HttpApi exerciser（coverage/auth/effect，fail-on-missing/skip）`。
- codey 对应门槛 = `静态` + `单元全量` + `机器契约` + `本地模型实机` + `记录`。缺一不可。
- agent 实机走 CLI 共用的 `run_headless` 入口（headless JSONL）；脚本仅注入固定目标的生产 provider 并存档，**不需要人盯屏**。
- 开发收尾先完成定向回归、静态检查与机器契约，冻结代码后再跑全量 pytest；
  全量结束后更新实际数字与文档。离线通过不替代真实模型矩阵，429/403 不算通过。

## 1. Gate 0：静态（必须全过）

```powershell
python -m ruff check codey tests tools
python -m mypy codey
git diff --check
python -m compileall -q codey tests
# JS（有 node 时本地也要跑；CI 必跑）
Get-ChildItem codey/web/assets -Filter *.js | ForEach-Object { node --check $_.FullName }
```

## 2. Gate 1：单元全量（必须全过）

```powershell
python -m pytest -q -o faulthandler_timeout=120
```

- 通过标准：`0 failed`；逐项记录平台、权限和 opt-in 导致的 skip，不用固定跳过数量作为判定。
- 不允许用“重跑一遍就绿了”掩盖 flake：flake 必须记入 `TEST_REPORT.md`，说明路径、重跑结果、是否触及本次改动。
- 对应 opencode 的 `bun turbo test`（linux+windows 双跑）。

## 3. Gate 2：机器契约（必须全过，无模型也跑）

对应 opencode 的 `test:httpapi --mode coverage/auth/effect --fail-on-missing --fail-on-skip`。

```powershell
python -m tools.machine_contract_gate
```

- 必跑清单由 `tools/machine_contract_gate.py` 维护；文件缺失、失败或任何 skip 均关闭此门，CI 与本地使用同一命令。需要 Node.js 执行实际 SSE JavaScript 回归，以及已安装的 Playwright Chromium（`python -m playwright install chromium`）执行就绪/清理契约。
- 用脚本 provider 与真实存储检查认证、授权、取消/连接/交付失败、原生 ID 配对、来源状态、SSE 游标与输出身份、实际请求诊断及恢复。实际模型行为属于 Gate 3。
- 桌面/CLI 正式入口的自动审查和单次修复、只读/联网授权、无项目、冷启动复用、顾问服务消费及配置错误也在必跑清单中。脚本 provider 场景隔离外部模型发现，不以用户当时打开的模型决定结果。
- 两种 API 的原子交换、native Auto 首轮一次准备/所属会话，以及 Zen 包无法导入时 Local、目录和历史的正式消费者也必须通过；不能只检查连接注册是否存在。
- JSONL 每行必须合法；任务事件有一致的 `schema_version/type/run_id/session_id`，全局连接状态按自己的事件契约检查；终态事件必须与实际结果一致。

## 4. Gate 3：本地模型实机（发布前必须全过）

工具：`tools/local_model_release_gate.py`（自动抓 JSONL + 独立校验，不依赖模型自评）。

```powershell
python tools/local_model_release_gate.py --case all --repeat 1 --timeout 600 --protocol native --json
python tools/local_model_release_gate.py --case all --repeat 1 --timeout 600 --protocol json --json
```

同一工具也支持 Zen，必须明确选择当前目录中的模型和原生工具路径，API wire 协议
由正式冻结 selection 决定；不是把 Zen 请求伪装成 Local：

```powershell
python tools/local_model_release_gate.py --provider zen --model muse-spark-1.3-contributor-free --case all --protocol native --turn-budget 24 --timeout 600 --json
python tools/local_model_release_gate.py --provider zen --model space-bunny-free --case all --protocol native --turn-budget 24 --timeout 600 --json
```

示例 ID 只代表本次实际测试目标；免费目录变化后应显式改为当前可用 ID，不维护
代码内永久名单。`--turn-budget` 为显式任务预算，不传则保留原场景预算。
`--run-dir` 可以固定新产物目录，已存在的目录拒绝覆盖。gate 排除用户个人
Local/网页辅助模型；Zen 的第二模型选择仍使用生产连接内的正式审查策略。

`--case all` 使用当前默认门槛集合，包含带隔离 research vault 的网页研究 case。可单独运行：

```powershell
python tools/local_model_release_gate.py --cases research --repeat 1 --timeout 600 --json
```

覆盖范围（不等同于所有任务正确性或所有模型兼容性）：

| case | 意图 | 独立判定 |
|---|---|---|
| `chat` | 直连所选连接的生产 `ApiProvider.send`（Zen 带独立连接包装） | 去首尾空白后精确等于 `KOBOLD_OK`；无执行权限的聊天也需通过上游资格，Zen 临时网络声明不授予权限 |
| `read` | `planning_readonly`：读现有源码 | 成功的 `read_file` 收据、文件哈希不变、`done` |
| `create` | `project`：新建 `math_utils.py` + 单测 | 独立输入矩阵、可发现的 unittest；生成的测试必须能拒绝错误加法 |
| `edit` | `project`：修 `pricing.py`（fixture 带可发现的 `tests/` 目录） | 独立公式矩阵、原测试哈希与 unittest 通过；内核正常 `done` |
| `references` | `project`：改 `calculate_total` 并更新调用方 | 原调用方断言和独立输入矩阵通过；原测试不能删除或改写 |
| `hybrid` | `hybrid`：网页和项目工具连续使用 | 独立公式矩阵通过，成功的 `web_search→open→read_file→edit→run→done` 收据同 run/session |
| `discussion` | `project`：只讨论不建文件 | 文件不变且 `done`；回答质量待评审 |
| `planning` | `planning_readonly`：对实际源码给方案 | 原文件哈希不变且 `done`；方案质量待评审 |
| `auto` | `auto`：小任务自动选路 | 文件内容精确等于 `hello auto`（仅容忍末尾换行）+ `done` + exit 0 |
| `ghost` | 隔离 state 下写→按 run_id 读→检索→删→不可检索 | roundtrip 完整；独立控制面检查，不计入模型任务成功率，不读取默认用户状态 |
| `tests` | 为已有实现补充测试 | 测试通过、原实现未改动、变异实现必须被测试拒绝 |
| `research` | 显式 Research：查官方 pathlib 文档 | 实际搜索和打开来源、隔离账本/报告要求通过、正常 `done` |
| `recovery` | 两个独立进程：真实编辑结算后强制退出，再从正式日志恢复 | 原结果已交付、编辑只执行一次、原策略保持、文件精确符合请求、独立测试通过、最终 `done` |
| `review` | 正式 headless 只读审查，使用选定模型 | 实际请求非零且模型匹配、完整结构化结果、产物/ledger/终态一致、文件哈希不变；不可用不通过，不断言固定问题措辞 |
| `project_review` | 同一次正式 project：编码→验证→自动审查→可选一次 repair；Local 固定目标自审，Zen 正式独立第二模型 | 原测试哈希及独立公式矩阵通过；Writer/Reviewer 真实请求和期望模型、审查期间文件不变、完整持久结果与正确 self_review、同 run/session、最新编辑后验证和最终收据一致 |

### 3.1 Gate 3b：真实桌面 UI 摩擦（可选但建议发布前运行）

`local_model_release_gate.py` 覆盖的是 headless 真实模型协议；`tools/ui_e2e.py`
覆盖真实 Edge 的按钮、SSE 和 drawer，但故意使用 `ScriptedWriter`。两者都不能证明
桌面 UI 与真实 API provider 已接通。`tools/local_model_ui_gate.py` 补这个边界：它启动
真实 Codey HTTP server 和 Edge，选择明确的 Local 或 Zen 模型，发送真实任务，然后检查 Local Settings、项目
绑定、Local context、Changes drawer 的打开/关闭按钮，并检查终态事件、DOM 可见性和 drawer
互斥。模型回答文本只作为存在性信号，不作为 UI 正确性的断言。

该 gate 不默认进入 pytest；Local 必须显式提供 endpoint，Zen 必须显式选择模型，
避免把用户保存的目标误当成发布输入：

```powershell
$env:LOCAL_OPENAI_BASE_URL = 'http://127.0.0.1:1234/v1'
$env:LOCAL_OPENAI_MODEL = '实际 /models 返回的 id'
python tools/local_model_ui_gate.py --json
python tools/local_model_ui_gate.py --provider zen --model muse-spark-1.3-contributor-free --cases coding,review,ghost --artifacts .e2e-artifacts/muse-ui --json
python tools/local_model_ui_gate.py --provider zen --model space-bunny-free --cases chat,research,coding,review,ghost --artifacts .e2e-artifacts/space-ui --json
```

需要真实 Edge、Playwright 浏览器，以及正在运行的本地服务或可访问且有调用资格的 Zen；缺少这些环境
只能记录为未运行，不能替代 headless gate 或静态 UI 契约测试。该 gate 使用临时项目和
临时 server state，不写入默认项目或本地 provider 配置。
UI 的 `review` case 检查 Review/details 面板，不能作为真实第二模型审查的证据；
后者由 headless `project_review` 的真实请求与正式持久结果判定。

Research UI gate 只发送用户任务；研究完成指导由生产 `research/completion_guidance.py` 定义，
正式入口按授权通过 `KernelRunRequest.task_guidance` 交给 `kernel_prompt.py` 组合，包含先搜索并打开来源、
用 `knowledge_write` 保存精确摘录、六个独立 Markdown 章节、`[n]` 引用、固定反证模板、原样传递
note id，以及拒绝后的 `knowledge_read` 恢复步骤。`local-model-provider-history.jsonl` 保存本次运行的
完整 request/response/tool error；gate 的 history 分析只统计每轮最后一个 tool message，避免把重放的旧错误
重复计数；同时识别 Responses 的 input/function_call_output/output。两种协议在生产
transport 录制实际 request/response/wire error，不存请求头、不额外发起生成。
2026-10-05 在 KoboldCpp Gemma4-12B 上观察到，缺少这些生产提示时会在
`web_search -> open_url -> done` 后收到 `research_evidence_missing`，随后重复 `done`；这不是 JSON 解码或
UI 重绘问题。补充生产 prompt 后，模型曾在严格完成门下完成 `web_search -> open_url -> knowledge_write -> done`，
但后续复测仍暴露报告质量、搜索超时和服务端 HTTP 200 非 JSON 风险。完成门没有放宽；非 JSON 在 provider
边界拒绝，未知或已发送的生成不自动重发。只有确定尚未提交 HTTP 生成的 TLS EOF
可在共享 deadline 内重连一次；证书失败不重连。模型输出截断续写是另一条明确预算路径。

machine gate 必跑任务指导归属、提示渲染无副作用、native 编码指令、正式入口消费和
不可变编码上下文测试。实机 gate 的 production hashes 同时记录领域指导、报告契约、
工具 ID 规则及上下文准备/渲染模块；指纹只用于实验诊断，不充当完成证明。

实机要求：

- `project_review` 使用无 `.git` 的普通隔离文件夹，进入
  与桌面/CLI 默认共用的 project review 阶段；不把单独发起的 review 任务冒充自动
  自审。Local 固定连接器约束 Writer/Reviewer 同目标；Zen 保留生产独立第二模型选择，
  观察实际选择和 recorder 身份。可运行 `--cases review,project_review --repeat 1`。
  `coding_review` 单独分类，不混入只读审查统计；整体编码成功率仍包含该编码任务。
  真实模型可能给 approved；发现问题后的一次 repair 用确定性正式入口测试锁定，
  不要求模型碰巧返回某个固定 finding。五个自动自审/输入/状态/门槛回归文件已进入
  Gate 2 必跑清单。详见[本轮验收](automatic-local-review-2026-10-04.zh-CN.md)。
- `review` 可用 `--cases review --repeat 1` 单独冒烟。Reviewer 当前发送文本审查 JSON；
  gate 的 `--protocol native` 不表示 Reviewer 调用了原生工具。单例通过不替代完整矩阵。
- Windows 临时 Git 对象可能是只读文件。清理只对已确认的 owned 临时目录内删除权限
  错误处理文件写入位并重试一次；不忽略清理/存档失败，不修改用户项目权限。

- 默认地址通过本地模型发现（LM Studio、Ollama、KoboldCpp 和通用兼容端点）；
  设置 `LOCAL_OPENAI_BASE_URL` 时只探测该地址。先探 `/models`，`reason=ok` 才开跑。
- 每次运行创建唯一目录，每次尝试分别保存 JSONL、provider 请求/解析后的响应、最终项目和隔离状态、独立验证结果。已有 `--run-dir` 拒绝覆盖。原始产物留在忽略目录，只提交脱敏的文字报告。
- 首次探测后固定 endpoint/model，聊天与 agent 均用同一生产连接选择；Local gate 的 temperature 固定 0，Zen 按冻结连接参数发送，实际请求另存。未发送 seed，不能宣称模型生成完全确定性。Local 的 `--protocol native/json` 分别测原生/文本 JSON 路径。
- `--timeout` 是单个案例的总进程截止时间，包含工具和独立验证；到期终止 owned process tree。请求 timeout 不超过该预算。生成长度、结束状态与终止预算从实际 wire 请求/响应记录；Local 默认和 Zen 的显式输出预算分开。终止交付不是完成证明；Chat 可用 max_tokens=1/无工具关闭，auto-only 服务可保留此前原授权声明，以有限预算关闭调用链。
- 活动 tool_choice/parallel 策略由冻结能力决定，支持 required 或 auto；仍校验实际返回的全部调用，不假定上游遵守单调用字段。native auto 首轮使用原授权快照，收到的 typed turn 计入同一内核预算。完成/取消/无进展/协议/预算终止不执行新调用。Zen 连接可补充显式不可用的 read/shell 网络声明；原快照仍是执行权限依据，文本消费者拒绝全部工具调用。[临时适配边界](zen_request_profile_2026-10-08.md)。[早期 Local 实机根因与 12/12 复测](local_native_protocol_2026-10-02.zh-CN.md)。
- recovery 确定性预检走同一正式入口与存储，仅脚本化 HTTP 响应；实机使用真实模型。新 provider 窗口接收原任务与恢复事实文本，原 call ID 保留在收据中，不向新窗口发送孤立的旧 tool result。同窗口 ID 关闭、失败保持失败、未结算危险动作不重放由机器契约验证。
- 重启后 exchange 可能从 1 开始，请求/响应按 recorder 实例 ID 与 exchange 共同关联；诊断 hash 不能替代完成证明。
- 元数据保存客户端上下文预算、服务端可查询信息、Python、Git commit 和脚本哈希；未报告的聊天模板/量化明确标为未知。usage 和 finish_reason 从实际响应记录，不凭模型总结估算。
- summary 每完成一个尝试就更新，异常和超时也计入分母。`objective_tasks.passed` 与 `artifacts_correct/artifacts_observed` 分开：代码正确、但协议未结束时不能计为任务完成。
- `python3 -m unittest` 在 Windows 策略里会被拒，门槛任务一律写 `python -m unittest [discover]`。
- agent case 必须同时满足：`stop_reason=done`、`exit_code=0`、`task_done` 事件存在、独立文件验证通过；`unittest discover` 输出 `Ran 0 tests` 也算 FAIL。
- agent 实机一律使用位于 Codey Git 仓库之外的隔离项目/`state_home`，不得污染默认 Ghost 状态；保存诊断材料后清理临时运行目录，存档失败即 FAIL。
- research 使用仓库外隔离的 `research_store_root`，不会读取或污染用户默认 vault；完整联网实机现在属于默认门槛，结果必须有成功的搜索、来源打开和终态证据。
- 600s 非流式超时仍是有限预算：实机的 1 POST、零客户端异常只证明**被观测的请求**正常送达，不能代替 Kobold 服务端日志，也不能证明今后绝无 `10053`。

## 5. Gate 4：记录（必须写）

- `TEST_REPORT.md` 追加一条：范围、验证命令、实际 `/models` ID、JSONL 路径、全量 suite 数字，不沿用上一次模型名。
- 开发验证更新 `Unreleased`；版本发布更新源码版本、README、中英文 changelog、必要的
  当前架构/能力文档与 TEST_REPORT。全量数字必须等实际测试结束后记录。
- tag 与 GitHub Release 按用户明确选择执行，不从版本提交自动推导。本次 0.5.11
  按本轮明确授权创建 `v0.5.11` tag 与 GitHub Release，不制作安装包。早先仅版本提交的验收记录保留为历史。

## 6. Bug 闭环（本次已在用）

1. 实机 FAIL → 先存 JSONL + ledger（`run_ledgers/<session>/<run>.jsonl`）。
2. 用最小复现脚本锁定（`tests/` 或 `tools/` 下可重复跑，不依赖模型）。
3. 修完先跑定向单测，再跑 `local_model_release_gate` 对应 case，再跑全量 `pytest`（或至少受影响面）。
4. 回写 `TEST_REPORT.md`。

## 7. 已知严格性（不是 bug，但 gate 必须知道）

- 无 `tests/`、无 `pytest.ini`/`pyproject` 的极简工程**发现不到验证候选**，即使模型跑了 `python -m unittest test_x.py` exit 0 也会判 `blocked(unobserved)`。这是 fail-closed 设计。门槛 fixture 一律用可发现形状（`tests/` + `python -m unittest discover`）。
- `python -m unittest test_pricing.py`（带具体文件名）不算 full-family 命令，不会替代候选；必须跑候选原命令或同 family 的 full 命令。
- 模型必须选自 `/models` 的实际 ID，固定到每个案例，不依赖服务端忽略错误模型名，也不修改用户保存的配置。
- 12B 的参数量不能独自解释失败。先区别程序接线/判定错误、模型生成的错误代码、协议或输出预算问题、联网环境问题；证据不足时保持未归因。测试授权与完成门不放宽。
- 每个结果同时记录任务轴、失败轴和根因类别。`production_defect`、`provider_boundary`、`model_boundary`、`gate_defect`、`environment` 等人工裁决类别必须带 `root_cause_evidence`；没有证据时保持 `undetermined`。
- Zen 免费目录、合作标识和一次成功不承诺所有任务资格。403/429 保留上游响应和失败分母，
  不扩大执行权限、不偷换模型，不把 UI 面板检查当真实审查。临时请求适配后的 Muse
  chat/read/review 三项实机通过；完整模型/任务矩阵仍未重新验收，见 TEST_REPORT 最新节。

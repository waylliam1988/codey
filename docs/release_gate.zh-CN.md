# Codey 发布门槛（Release Gate）

> 借鉴 `reference-projects/opencode`：bug 理论上永远改不完，发布不靠“零 bug”，靠**固定门槛全绿**。
> 本文档是 codey 的发布门槛标准。暂不发版时也要按此门槛跑实机，记录结果。

## 0. 结论（给语音输入的你）

- opencode 的门槛 = `unit（linux+windows）` + `e2e（playwright app）` + `typecheck` + `lint` + `generated client 检查` + `HttpApi exerciser（coverage/auth/effect，fail-on-missing/skip）`。
- codey 对应门槛 = `静态` + `单元全量` + `机器契约` + `本地模型实机` + `记录`。缺一不可。
- agent 实机走 CLI 共用的 `run_headless` 入口（headless JSONL）；脚本仅注入固定目标的生产 provider 并存档，**不需要人盯屏**。

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

- 必跑清单由 `tools/machine_contract_gate.py` 维护；文件缺失、失败或任何 skip 均关闭此门，CI 与本地使用同一命令。需要 Node.js 执行实际 SSE JavaScript 回归。
- 用脚本 provider 与真实存储检查认证、授权、取消/连接/交付失败、原生 ID 配对、来源状态、SSE 游标与输出身份、实际请求诊断及恢复。实际模型行为属于 Gate 3。
- 桌面/CLI 正式入口的自动审查和单次修复、只读/联网授权、无项目、冷启动复用、顾问服务消费及配置错误也在必跑清单中。脚本 provider 场景隔离外部模型发现，不以用户当时打开的模型决定结果。
- JSONL 每行必须合法；任务事件有一致的 `schema_version/type/run_id/session_id`，全局连接状态按自己的事件契约检查；终态事件必须与实际结果一致。

## 4. Gate 3：本地模型实机（发布前必须全过）

工具：`tools/local_model_release_gate.py`（自动抓 JSONL + 独立校验，不依赖模型自评）。

```powershell
python tools/local_model_release_gate.py --case all --repeat 1 --timeout 600 --protocol native --json
python tools/local_model_release_gate.py --case all --repeat 1 --timeout 600 --protocol json --json
```

`--case all` 使用当前默认门槛集合，包含带隔离 research vault 的网页研究 case。可单独运行：

```powershell
python tools/local_model_release_gate.py --cases research --repeat 1 --timeout 600 --json
```

覆盖范围（不等同于所有任务正确性或所有模型兼容性）：

| case | 意图 | 独立判定 |
|---|---|---|
| `chat` | 直连生产 `LocalOpenAIProvider.send` | 去首尾空白后精确等于 `KOBOLD_OK` |
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
| `review` | 正式 headless 只读审查，固定目标传入 Reviewer 连接器 | 实际请求非零且模型匹配、完整结构化结果、产物/ledger/终态一致、文件哈希不变；不可用不通过，不断言固定问题措辞 |
| `project_review` | 同一次正式 project：编码→验证→同一本地目标自动自审→可选一次 repair | 原测试哈希及独立公式矩阵通过；Writer/Reviewer 真实请求、审查期间文件不变、完整持久结果、同 run/session、最新编辑后验证和最终收据一致 |

实机要求：

- `project_review` 使用无 `.git` 的普通隔离文件夹，经显式 Reviewer 连接器进入
  与桌面/CLI 默认共用的 project review 阶段；不把单独发起的 review 任务冒充自动
  自审。固定连接器约束实机 Writer/Reviewer 目标。可运行 `--cases review,project_review --repeat 1`。
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
- 首次探测后固定 endpoint/model，聊天与 agent 均用同一生产 provider 配置；采样 temperature 固定 0，但未发送 seed，不能宣称完全确定性。`--protocol native/json` 分别测原生/文本 JSON 路径。
- `--timeout` 是单个案例的总进程截止时间，包含工具和独立验证；到期终止 owned process tree。请求 timeout 不超过该预算。活动轮次生成长度由服务端默认决定，不把服务端报告的默认值当作请求硬上限。原生终止收据撤回工具，显式 `max_tokens=1`；该确认不替代已验证的最终回答。
- 原生活动轮次使用 `tool_choice=required`、`parallel_tool_calls=false`，提示每轮一个调用并等待结果；仍校验服务端实际返回的所有调用，不假定它执行了单调用字段。完成/取消/预算终止时撤回工具，关闭 call id 且不执行后续调用。[实机根因与 12/12 复测](local_native_protocol_2026-10-02.zh-CN.md)。
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

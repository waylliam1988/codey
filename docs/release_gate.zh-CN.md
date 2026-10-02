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
python -m pytest tests/test_cli.py tests/test_headless_runner.py tests/test_release_gate_tool_order.py tests/test_headless_real_kernel_lifecycle.py -q
```

- 这里用脚本 provider 检查入口与事件，不需要运行模型。实际聊天与 agent 执行属于 Gate 3。
- JSONL 每行必须合法；任务事件有一致的 `schema_version/type/run_id/session_id`，全局连接状态按自己的事件契约检查；终态事件必须与实际结果一致。

## 4. Gate 3：本地模型实机（发布前必须全过）

工具：`tools/local_model_release_gate.py`（自动抓 JSONL + 独立校验，不依赖模型自评）。

```powershell
python tools/local_model_release_gate.py --cases chat,read,create,edit,references,hybrid --repeat 3 --timeout 600 --json
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

实机要求：

- 默认地址通过本地模型发现（LM Studio、Ollama、KoboldCpp 和通用兼容端点）；
  设置 `LOCAL_OPENAI_BASE_URL` 时只探测该地址。先探 `/models`，`reason=ok` 才开跑。
- 每次运行创建唯一目录，每次尝试分别保存 JSONL、provider 请求/解析后的响应、最终项目和隔离状态、独立验证结果。已有 `--run-dir` 拒绝覆盖。原始产物留在忽略目录，只提交脱敏的文字报告。
- 首次探测后固定 endpoint/model，聊天与 agent 均用同一生产 provider 配置；采样 temperature 固定 0，但未发送 seed，不能宣称完全确定性。`--protocol native/json` 分别测原生/文本 JSON 路径。
- `--timeout` 是单个案例的总进程截止时间，包含工具和独立验证；到期终止 owned process tree。请求 timeout 不超过该预算。生成长度仍由服务端默认决定，未发送 `max_tokens`，不把服务端报告的默认值当作请求硬上限。
- 元数据保存客户端上下文预算、服务端可查询信息、Python、Git commit 和脚本哈希；未报告的聊天模板/量化明确标为未知。usage 和 finish_reason 从实际响应记录，不凭模型总结估算。
- summary 每完成一个尝试就更新，异常和超时也计入分母。`objective_tasks.passed` 与 `artifacts_correct/artifacts_observed` 分开：代码正确、但协议未结束时不能计为任务完成。
- `python3 -m unittest` 在 Windows 策略里会被拒，门槛任务一律写 `python -m unittest [discover]`。
- agent case 必须同时满足：`stop_reason=done`、`exit_code=0`、`task_done` 事件存在、独立文件验证通过；`unittest discover` 输出 `Ran 0 tests` 也算 FAIL。
- agent 实机一律使用位于 Codey Git 仓库之外的隔离项目/`state_home`，不得污染默认 Ghost 状态；保存诊断材料后清理临时运行目录，存档失败即 FAIL。
- research 的完整联网实机不在阻塞门内（需要搜索连接器+长生成，等连接器准备好后单独加入）。
- 600s 非流式超时仍是有限预算：实机的 1 POST、零客户端异常只证明**被观测的请求**正常送达，不能代替 Kobold 服务端日志，也不能证明今后绝无 `10053`。

## 5. Gate 4：记录（必须写）

- `TEST_REPORT.md` 追加一条：范围、验证命令、实际 `/models` ID、JSONL 路径、全量 suite 数字，不沿用上一次模型名。
- 按用户要求更新 `Unreleased` 与 TEST_REPORT；不 bump 版本、不打 tag、不 release。TEST_REPORT 的全量数字必须等测试实际结束后记录。

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

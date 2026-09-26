# Codey 发布门槛（Release Gate）

> 借鉴 `reference-projects/opencode`：bug 理论上永远改不完，发布不靠“零 bug”，靠**固定门槛全绿**。
> 本文档是 codey 的发布门槛标准。暂不发版时也要按此门槛跑实机，记录结果。

## 0. 结论（给语音输入的你）

- opencode 的门槛 = `unit（linux+windows）` + `e2e（playwright app）` + `typecheck` + `lint` + `generated client 检查` + `HttpApi exerciser（coverage/auth/effect，fail-on-missing/skip）`。
- codey 对应门槛 = `静态` + `单元全量` + `机器契约` + `koboldcpp 实机` + `记录`。缺一不可。
- 实机一律走 `python -m codey agent --provider local --json`（headless JSONL），自动落盘到 `.e2e-artifacts/`，**不需要人盯屏**。

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

- 通过标准：`0 failed`，允许的 skip 只有 Windows POSIX/opt-in 家族（当前约 7 个）。
- 不允许用“重跑一遍就绿了”掩盖 flake：flake 必须记入 `TEST_REPORT.md`，说明路径、重跑结果、是否触及本次改动。
- 对应 opencode 的 `bun turbo test`（linux+windows 双跑）。

## 3. Gate 2：机器契约（必须全过，无模型也跑）

对应 opencode 的 `test:httpapi --mode coverage/auth/effect --fail-on-missing --fail-on-skip`。

```powershell
python -m codey chat --provider local "Reply with exactly: CLI_OK"
python -m codey agent --provider local --project <tmp> --json "Create hello.txt ..."
python -m codey ghost list
python -m codey ghost export
```

- 要求：`chat` 回复可用；`agent --json` 每行都是合法 JSONL 且含 `schema_version/type/run_id/session_id`；`task_done` 必达；`ghost list/export` 的 `ok=true`。
- JSONL 必须存档（见 Gate 3），人只看结论，不盯屏。

## 4. Gate 3：koboldcpp 实机（发布前必须全过）

工具：`tools/kobold_live_gate.py`（自动抓 JSONL + 独立校验，不依赖模型自评）。

```powershell
python tools/kobold_live_gate.py --json
```

覆盖（全覆盖版）：

| case | 意图 | 独立判定 |
|---|---|---|
| `chat` | 直连 `LocalOpenAIProvider.send` | 回复非空 |
| `create` | `project`：新建 `math_utils.py` + 单测 | `python -m unittest` exit 0 |
| `edit` | `project`：修 `pricing.py`（**必须带 `tests/` 目录**，否则无验证候选会按 fail-closed 判 `blocked`，这是设计不是 bug） | `python -m unittest discover` exit 0 且 `stop_reason=done` + `checks_passed=true` |
| `references` | `project`：改 `calculate_total` 并更新调用方 | 三个断言全过 |
| `discussion` | `project`：只讨论不建文件 | 无文件变更且 `done` |
| `planning` | `planning_readonly`：只给方案不写文件 | 无文件变更 |
| `auto` | `auto`：小任务自动选路 | `task_done` 必达 |
| `ghost` | `ghost list/export/directive/continuity/work-list` + 本次 `observations` 落盘 | `ok=true` 且观测到本次 `run_id` |

实机要求：

- koboldcpp 地址固定走发现（`127.0.0.1:5001/v1` 优先），先探 `/models`，`reason=ok` 才开跑。
- 每个 case 的 JSONL 存 `.e2e-artifacts/kobold-live-<case>.jsonl`，`summary.json` 存结论。
- `chat` 允许 `temperature` 默认；`agent` 统一 `max_turns=8~12`，`timeout=600`（`stream=False` 下 timeout 即整代预算，参考 242s 长生成实测）。
- `python3 -m unittest` 在 Windows 策略里会被拒，门槛任务一律写 `python -m unittest [discover]`。
- research 的完整联网实机默认不进阻塞门（需要搜索连接器+长生成），要跑用 `--include-research` 显式开。

## 5. Gate 4：记录（必须写）

- `TEST_REPORT.md` 追加一条：范围、验证命令、实机模型（`koboldcpp/Gemma-4-Queen-31B-...`）、JSONL 路径、全量 suite 数字。
- `CHANGELOG.md` 的 `Unreleased` 只有在要发版时才动；**暂不发版就只写 TEST_REPORT，不碰版本文件**（本次即如此）。

## 6. Bug 闭环（本次已在用）

1. 实机 FAIL → 先存 JSONL + ledger（`run_ledgers/<session>/<run>.jsonl`）。
2. 用最小复现脚本锁定（`tests/` 或 `tools/` 下可重复跑，不依赖模型）。
3. 修完先跑定向单测，再跑 `kobold_live_gate` 对应 case，再跑全量 `pytest`（或至少受影响面）。
4. 回写 `TEST_REPORT.md`。

## 7. 已知严格性（不是 bug，但 gate 必须知道）

- 无 `tests/`、无 `pytest.ini`/`pyproject` 的极简工程**发现不到验证候选**，即使模型跑了 `python -m unittest test_x.py` exit 0 也会判 `blocked(unobserved)`。这是 fail-closed 设计。门槛 fixture 一律用可发现形状（`tests/` + `python -m unittest discover`）。
- `python -m unittest test_pricing.py`（带具体文件名）不算 full-family 命令，不会替代候选；必须跑候选原命令或同 family 的 full 命令。
- 配置里的旧模型名（如 `Gemma4-12B`）只要 koboldcpp 忽略 `model` 字段就能通；门槛以 `/models` 实际返回为准记录，不静默改用户配置。

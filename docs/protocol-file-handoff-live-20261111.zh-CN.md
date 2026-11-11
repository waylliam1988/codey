# 网页读取到项目文件读取的交接

## 对照

前一轮静态全文写入实验的八个单元都已把网页正文送进实际模型请求。模型随后累计调用 `read_tool_result` 44 次，项目文件读取和编辑次数均为零。本轮只测网页操作完成后，程序接手第一次文件读取的效果。 [前一轮实验](protocol-edit-boundary-live-20261111.zh-CN.md)

A、B 使用相同任务、浏览器动作契约、原生产编辑执行器和静态全文写入保护。A 由模型发起第一次项目文件读取。B 在目标网页操作链完成、完整 `browser_read` 已有成功父回执、且当前页面未再切换时，为任务明确指定的 `result.json` 发起一次真实 `read_file`。调度由实验 fixture 显式提供目标。已尝试过目标文件读取时跳过调度。失败结果经原回执交付，一次 worker 内最多调度一次。

两侧由模型决定写入内容。静态全文写入仍只接收已与完整文件正文一起进入当前实际请求的凭证。发生版本冲突后的重读和重新写入继续由模型提交。A、B 的内核轮次上限相同，B 的运行时动作占一个内核轮次，单独记账。

## 验证方法

- 脚本化 HTTP 配合真实 Chromium 验证两个场景的 A/B，逐步检查网页正文、文件正文和凭证是否进入实际请求。用真实版本冲突及外部随机字段验收重读后的写入。
- 验证缺授权路径、缺成功父回执、网页读取失败或已尝试读取时没有额外动作。注入文件读取失败，核对下一次请求看到原错误，运行时只尝试一次。
- 保留上一轮静态旧全文保护和 CodeMode 执行时读后计算的回归。

```powershell
python -m pytest -q tests/test_protocol_surface.py -k 'file_handoff or codemode_can_compute or edit_boundary_rejects'
python -u -m tools.protocol_surface_ab --config file-handoff --run-dir .e2e-artifacts/protocol-file-handoff-live-20261111-01 --repeats 2 --timeout 90 --budget 300 --max-turns 10 --seed 123456789 --kobold-base-url http://127.0.0.1:5001/v1
```

实机使用本地 KoboldCpp Gemma4 12B，temperature 0，两个场景各两对，交替 AB/BA。按实际文件读取、成功写入、冲突后重读、字段保留和正式任务完成分别记账。

## 实机结果

| 场景 | 第 1 轮 A | 第 1 轮 B | 第 2 轮 A | 第 2 轮 B |
| --- | --- | --- | --- | --- |
| `hybrid` | 未读文件（8） | 写入正确，完成被拒（5） | 未读文件（5） | 写入正确，完成被拒（5） |
| `stale_edit` | 未读文件（8） | 真实冲突，未重读（5） | 未读文件（5） | 真实冲突，后续请求超上下文（5） |

括号内为实际模型请求次数。8 个单元都已启动并保留原结果。计入末个上下文失败单元，正式任务通过率 **A、B 均 0/4**。仅看未被上下文错误中断的三个整对，仍均为 **0/3**。A 发出 26 次实际请求，B 发出 20 次，B 另有 4 次运行时文件读取。四对任务文字一致，生产源码和冻结实验脚本摘要保持一致。

- **第一次文件读取从 0/4 到 4/4。** B 每个单元恰好调度一次。四份完整文件正文及对应凭证都出现在调度后的下一次实际请求，同一请求也都含对应的网页正文。A 调用 `read_tool_result` 22 次，B 为 0 次。A 没有发起项目编辑，B 四个单元都发起了编辑。
- **两轮 `hybrid/B` 都实际写入正确值。** 文件均变成 `{"amount": 173, "code": "QUARTZ_851"}`。模型随后把 `task_turn_effect:` 工具回执编号填进 `done.knowledge_refs`，两次均被正式完成检查拒绝，最终 `no_progress`。
- **两轮 `stale_edit/B` 都发生了一次真实版本冲突。** 第一轮把重读和重写放进同一计划三次，均在动作前被拦截。第二轮被拦一次，随后提前提交完成，缺真实项目修改。后续准备下一次请求时，估算输入 24,691 超过 24,576 上限，发送前被拒绝。两轮都未独立重读和成功重写。外部字段仍在，目标业务值未写入。

本轮确认运行时接手第一次文件读取能打通网页到编辑的交接。自主模型的剩余阻断发生在冲突后重读和正式完成。只读审计核对了 46 次实际请求和四份交接回执，末次超限请求未发出。 [原始进度](../.e2e-artifacts/protocol-file-handoff-live-20261111-01/progress.json) · [中断记录](../.e2e-artifacts/protocol-file-handoff-live-20261111-01/interruption.json) · [逐单元审计](../.e2e-artifacts/protocol-file-handoff-live-20261111-01/file-handoff-audit.json)

## 回归检查

10 项定向测试通过。脚本化 A/B 中两个场景均完成，B 各少一次模型调用。133 项相关回归通过，退出时有 RAM disk 清理告警。Ruff、strict mypy 和 `git diff --check` 通过。提交范围为实验脚本、测试和报告。

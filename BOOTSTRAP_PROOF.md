# Codey 自举能力证明

日期：2026-06-28  
环境：Windows / Edge CDP `127.0.0.1:9222` / 本地仓库 `E:\codey`

## 目标

验证 Codey 是否已经具备初级自举能力：

> Codey 自己出现可测试、可定位、可编辑的问题时，能否依靠接入的网页 AI、自己的工具协议、测试输出、diff/restore 链条，把 Codey 的代码修回来。

这里的目标不是证明 Codey 永远不坏，而是证明它坏了以后，可以进入一个受控修复闭环。

## 测试方法

为避免污染主仓库，每个 provider 都使用 Codey 的临时副本：

1. 复制当前 `E:\codey` 到临时目录。
2. 在临时副本中故意破坏 `codey/changes.py`：
   - 将 `_diff_for()` 的返回逻辑改坏。
   - 造成 `tests.test_changes` 失败，最初表现为 `IndentationError`。
3. 用对应网页 AI provider 运行 Codey agent：

```powershell
python -m codey agent --provider <provider> --project <临时副本> --max-turns 14 `
  "This is a Codey self-repair test. Run python -m unittest tests.test_changes, inspect the failure, fix the bug in codey/changes.py, then run python -m unittest tests.test_changes again. Use search/read/edit/run tools only. When tests are green, finish with done and summarize the fix."
```

4. 观察 provider 是否能完成闭环：
   - 运行失败测试。
   - 读取失败信息。
   - 读取 Codey 自身源码。
   - 修改 `codey/changes.py`。
   - 重新运行测试。
   - 测试通过后 `done`。
5. 对修复后的临时副本运行全量测试：

```powershell
python -B -m unittest
```

主仓库最终也再次运行全量测试，并确认 `git status --short` 干净。

## Provider 结果

| Provider | 结果 | 修复轮数 | 修复后副本全量测试 | 观察 |
|---|---:|---:|---:|---|
| DeepSeek | 成功 | 12 | 118 tests OK | 能修复，但过程更绕，生成的实现偏啰嗦 |
| MiMo | 成功 | 8 | 118 tests OK | 修复较顺，改法接近主仓库当前实现 |
| Qwen | 成功 | 7 | 118 tests OK | 最快，改法也较干净 |

三家 provider 都完成了同一个自举修复闭环。

## UI / Diff / Restore 链条验证

除了真实网页 AI 修复临时副本，还单独验证了 UI 后端链条：

1. 使用 fake provider 触发 `server._run_task()`。
2. 确认 task 创建 snapshot tracker。
3. 修改文件后，通过 `collect_changes()` 获得 snapshot diff。
4. 确认 diff 包含红绿变更内容。
5. 调用 `restore_snapshot_changes()` 能回滚到原始内容。

结果：通过。

这证明 UI 模式下的 snapshot diff 和 restore 能作为自举过程的本地安全护栏。

## 主仓库最终状态

最终在主仓库运行：

```powershell
python -B -m unittest
```

结果：

```text
Ran 118 tests
OK
```

并确认：

```powershell
git status --short
```

输出为空，主仓库未被临时自举测试污染。

## 结论

Codey 已经具备初级自举能力：

- 网页 AI 可以通过 Codey 的工具协议读写 Codey 自己的源码。
- 网页 AI 可以读取测试失败信息并进行多轮修复。
- Codey 能执行测试并把结果反馈给网页 AI。
- 修复过程可以在临时副本中完成，避免污染主仓库。
- snapshot diff / restore 能作为无 Git 或小白场景下的安全护栏。
- DeepSeek、MiMo、Qwen 三个 provider 均完成同一自修复任务。

这说明 Codey 的核心闭环已经成立：不是永远不坏，而是坏了以后有机会靠自己接入的网页 AI 修回来。

## 剩余风险

自举能力仍然需要工程护栏约束：

- 网页 AI 能修，但不保证修得最简洁；DeepSeek 本次修复就比主仓库实现更啰嗦。
- provider DOM 仍可能因网页改版失效，需要 live smoke 暴露问题。
- UI 改动需要浏览器截图验证，否则容易出现视觉回归。
- 自举测试不应每次都全量跑三家 provider，成本和耗时都偏高。

## 建议护栏

保持克制，不做无限叠加：

1. 平时默认跑 `python -B -m unittest`。
2. 修改 provider 时，跑对应 provider 的 live smoke。
3. 修改 UI 时，跑浏览器截图验证并检查 `DESIGN.md` 约束。
4. 发版或大改前，再跑 DeepSeek / MiMo / Qwen 的小型 benchmark。

目标是让护栏像门禁，而不是把项目包成厚重的测试装甲。

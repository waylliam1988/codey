# 任务合同失败的固定回复回归

以下基线记录对应 `bffcf553`；末节另记后续 basic 生产修复及验收，保留原始失败证据。

本次在 `0fb5c03c` 上建立失败基线，没有修改生产代码、绑定控件或导航 URL，也没有调用真实模型或重跑完整实机矩阵。浏览器动作由真实 Chromium 执行，页面沿用既有本地路由夹具；提供方回复固定，入口、策略、CodeMode、内核执行及持久回执均走现有生产链。

原始四项失败均已重现。`basic` 的回放错误属于程序覆盖诊断，不是模型申请了不完整的回执修复。`structured` 的业务值缺项仍可被生产 `done` 接受。原始 `stale_ref` 没有发出真实旧引用操作，原始 Hybrid 没有到达文件版本校验。正确调用的两个恢复对照均成功。

## 固定输入与复跑

新增 `tests/fixtures/task_contract_failure_replies_20261113.json`，直接保存最终矩阵中 basic、structured、stale_ref、stale_edit 的原始 assistant content 字符串，并记录源 `provider.jsonl` 路径和 SHA256。保留原始 JSON 转义，不先修正代码、换 URL 或重新序列化模型输出。共 17 条原始回复。夹具可独立运行，不依赖未提交的 `.e2e-artifacts`。

新增 `tests/test_task_contract_fixed_replies.py`，包含 11 项基线及对照：

| 固定输入 | 验证结果 |
| --- | --- |
| basic 原始四条回复 | 首条 `syntax_error`；后三条新调用均导航到下划线 URL，真实路由返回 404，成功快照的 elements 为空；父调用均返回控制流回放错误，参数均无 `repair_of` |
| structured 原始四条回复 | 正文成功读到 173 和 QUARTZ_851；最终 JSON 的 page_url/token 正确，但 explanation 没有两个业务值；生产接受 done，场景验收失败 |
| stale_ref 原始四条回复 | 四次 syntax_error，真实 browser_click 为零，旧引用拒绝为零；没有覆盖恢复机制 |
| Hybrid 原始五条回复 | 浏览器等待、读回成功；解包后的数据被当作编辑凭证，另有编译错误；文件仍为初始值，版本校验和真实冲突均未触发 |
| 错 URL 后仅返回快照 | 新调用自身成功，空控件快照正常交付；404/空快照本身不会引发控制流回放错误 |
| 错 URL 后用空 ref 填写，不 unwrap / unwrap 两种 | 两种均稳定重现诊断覆盖，无需先发生编译失败；保留导航和快照回执 |
| structured 故意省略 / 完整说明两种 | 两种生产均接受 done；省略时业务事实缺失，完整时场景通过，确认缺口位于最终交付 |
| Hybrid 先错误凭证，再外层凭证两次 | 错误凭证先拒绝；正确 `{read, changes}` 到达版本校验，真实冲突恰好一次，重新读取后成功写入，并保留 external_revision |
| 可执行的旧引用调用，再重新观察继续 | 真实 stale_ref 拒绝恰好一次，重新 snapshot 后完成加载、等待、读回和交付 |

本组是当前行为的特征回归：pytest 通过表示上述缺陷及恢复边界得到了稳定复现，不表示四项原始失败已被修复。后续修复应将相应错误反馈、交付断言改为期望的新行为。

复跑命令（先确保产物父目录存在，使用新的 basetemp 目录以保留历史）：

```powershell
python -m pytest tests/test_task_contract_fixed_replies.py tests/test_browser_full_surface_contract.py -q --basetemp=.e2e-artifacts/task-contract-fixed-replies-20261113/final
```

## basic 的诊断覆盖链

最小对照使用一条全新的 CodeMode 调用，依次导航到 `https://example.com/production_browser_record`、snapshot、用空 ref 调用 browser_fill。测试仅在原方法外记录结果，仍调用真实的 `NodeWorkerRuntime.execute` 和 `DurableCodeModeReplay.assert_valid`，不修改返回值或执行行为。

观察到的顺序是：

1. `codey/codemode/kernel.py` 的 execute_child 在进入内核前调用 replay.child，将准备执行的子调用记入 `_children_seen`。
2. `codey/operations/kernel_execution.py` 的 execute_turn 在参数校验时拒绝空 ref，返回 `ERROR: browser_fill missing required arg 'ref'`，尚未创建这个子调用的 intent 或回执。
3. execute_child 检查到结果没有提交，运行时返回 `tool_error`，原文为 `nested kernel result was not committed: ERROR: browser_fill missing required arg 'ref'`。
4. `CodeModeExecutor.execute` 在处理运行时失败前调用 replay.assert_valid。记录显示 seen 为三个子调用，实际 intent 只有导航和快照两个，多出的那个是被参数校验拒绝的填写调用。
5. assert_valid 因 seen 与实际 intent 集合不一致抛出 `CodeMode recovery did not consume its recorded control flow`；父调用返回这条错误，盖住此前的缺少 ref 诊断。

两种空 ref 对照都保存 `feedback-trace.json`，含原始运行时错误、seen/committed 差异和最终父反馈。只导航、快照并返回的对照没有集合差异，父调用成功。因此定位到了确定的程序边界问题，不能将它描述为模型使用了 `repair_of`，也不能仅靠修正 URL 就认为诊断问题已解决。

## 验证记录和限制

产物根目录：`.e2e-artifacts/task-contract-fixed-replies-20261113/`。每个浏览器场景保存 provider.jsonl、events.jsonl、receipts.json、summary.json 和持久 state。

- 初次启动因 basetemp 父目录不存在而未执行场景；创建目录后继续。
- `run2.txt`：7 通过、3 失败。两个失败是新测试对字段名及错误码的预期写错；一个是新 Hybrid 对照混用了 `{read, content}` 与 `{read, changes}` 两种参数形态。均只修改测试，原始回复夹具未变。
- `run3.txt`：修正测试后，10 项全部通过。随后加入“错 URL 后只返回快照”的隔离对照。
- `final.txt`：16 项全部通过，耗时 103.43 秒，包含 11 项新增回归及 5 项既有完整表面合同用例。
- `fixture-audit.json`：四份原始 provider.jsonl 的 SHA256 均匹配，17 条 assistant content 与夹具逐字相同。
- 新测试文件通过 Ruff，改动通过 git diff --check。

这次验证的是固定决策下的生产执行行为。没有证明模型能自主选用新入口，也没有证明绑定后能够补齐业务值或自动持有编辑凭证；这些改动本次尚未实施。上传控件的单控件、同名控件、同 URL 重载，以及不同页面字段和歧义来源的交付验收，应随对应绑定改动单独执行。

## 后续 basic 生产修复

`codey/codemode/kernel.py` 将原来仅针对 browser_wait 的提前校验，改为复用内核 `_validate_tool_args`，按当前轮的冻结工具合同校验所有子调用。参数拒绝发生在 replay.child、子调用开始事件和执行之前，因此不会将没有 intent 的调用计入必须核对回执的集合。实际执行仍进入原 execute_turn；没有跳过或放宽回放完整性校验。

固定原回复的断言已更新：模型后续请求能收到 `browser_fill missing required arg 'ref'`，父调用不再返回“回放未走完”；错 URL 后仅返回快照仍成功。空 ref 的两种独立对照均确认 runtime 和父反馈保留直接错误，seen 与 committed 都恰好包含先前的导航、快照两项，父调用 child_effect_ids 仍引用这两条成功回执。原始 basic 任务仍因错误 URL 和缺 ref 而失败，本修复只纠正反馈和记账边界。

测试中的 JSON 解析改用项目的严格 JSON 解码器；请求观察回调、运行时调用和控制流记录使用明确类型。Pyrefly 同时发现两个原有类型边界问题：limits 的 asdict 结果标注改为实际的 dict[str, int]，kernel_prompt 的搜索 URL 列表明确标注 list[str]。两处只调整类型注解，未改变运行行为，也未增加 Any、类型忽略或基线豁免。

本次产物独立存于 `.e2e-artifacts/codemode-child-admission-20261113/`：

- `before.txt`：先更新验收断言，未修生产时两种空 ref 对照均失败，仍收到旧回放错误。
- `after.txt`：修复后 43 项通过，包括固定原回复、独立浏览器对照、缺失回执阻断、正式恢复篡改、并发失效和 repair 参数变更拒绝。
- `final.txt`：最终源码 95 项通过，涵盖固定回复及通用 CodeMode 内核集成、生产边界、来源预检、JSON 编辑和失败反馈。与 after 组有重叠，不相加。
- `mypy-final.txt`：生产全树 680 个文件通过，目标 Python 3.11。
- `no-any-final.txt`：修改的三个 Python 文件通过 mypy --disallow-any-expr（导入使用 follow-imports=silent；生产全树另行检查）。
- `pyrefly-final.txt`、`ty-final.txt`：生产全树及修改的回归文件通过，目标 Python 3.11。Pyrefly 留存现有 suppressed / warnings，未添加忽略规则。
- `ruff.txt`、`precision.txt`：全仓 Ruff 和内核类型精度检查通过；git diff --check 通过。

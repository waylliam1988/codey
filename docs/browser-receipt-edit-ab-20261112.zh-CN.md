# 从已核验的网页回执填入文件编辑值

生产基线为 `0ccaa635`，实际生产代码与 `e3d876b0` 相同。对照从上轮延迟加载场景出发。两臂均使用真实 Chromium、读取内就绪检查、生产 CodeMode 和文件版本保护。任务均要求先用独立调用返回已加载正文，再读取并编辑 `result.json`。文件的第一次完整读取后会注入外部修改。

- A 由模型从浏览器结果提取 `amount` 和 `code`，作为局部编辑参数。文件读取提供版本。
- B 由模型用原始文件读取对象提交 `tools.edit({read, changes: {}})`。实验宿主只接受该形状，从当前文档中真实点击 Load record 之后、成功 CodeMode 父调用提交的完整网页回执提取两项值，再交给生产局部编辑器。文件版本仍由原读取授权。发生冲突后，模型须重新读取文件再编辑，网页字段仍绑定到验证过的回执。

实机各跑一对 `173 / QUARTZ_851` 和 `247 / BASALT_426`，按 AB、BA 串行运行。夹具在点击后 1.5 秒发布记录。期望值由夹具和外部验收使用，B 的写入值来自回执正文。两臂的任务仅在提供字段的指令和调用示例上有差异。统一验收真实导航、点击后读取、一次真实版本冲突、冲突后重读、最终字段和外部修改。B 还将每次绑定尝试与持久文件编辑回执中的读取凭证逐项比对。

定向用例注入错误的模型字段，并检查两个不同的有效网页记录。负例覆盖同 URL 重载、读取的父调用失败以及模型试图传入非空字段。被拒绝的编辑不进入文件工具。

## 结果

| 延迟页面记录 | A 模型提供字段 | B 程序绑定字段 |
| --- | --- | --- |
| `173 / QUARTZ_851` | 通过，7 次请求 | 通过，4 次请求 |
| `247 / BASALT_426` | 通过，7 次请求 | 通过，4 次请求 |

**两臂均通过 2/2。** 四个单元均发生一次真实版本冲突、冲突后完整重读、正确落盘，且保留 `external_revision`。两臂均没有显式 `browser_wait`。两个 B 各执行两次绑定编辑，首次被版本检查拒绝，第二次成功。每个单元的两次绑定都引用同一份已核验网页回执和不同的文件读取凭证。不同页面值均以正确的数字和字符串类型写入磁盘。

实机中 A、B 分别使用 14 和 8 次模型请求。每个 A 单元都在浏览器采集阶段多了三次 `browser_snapshot` 的 Result 解包错误。编辑阶段两臂各有一次冲突写入和一次成功写入，因此这批调用差不能归因于字段绑定。四个单元均完整结束，22 次实际请求的模型、seed 123456789、temperature 0 和非流式配置一致。生产摘要和三份实验脚本摘要保持一致。

**确定性收益来自错误注入。** 当脚本化 A 把 `unknown` 写入两个字段时，文件版本保护仍正常运行，最终值验收失败。同一延迟夹具下，脚本化 B 只提交空的 `changes`，真实写入值来自网页正文，并通过完整冲突验收。重载、失败父调用和非空字段三种负例均在写入前拒绝，实际编辑回执为零。本实验只验证该夹具中有明确文字边界的两个字段，尚未测量任意网页字段抽取的可靠性。

21 项 pytest 通过，退出时有 RAM disk 清理告警。Ruff、限定范围 strict mypy 和 `git diff --check` 通过。 [完整实机结果](../.e2e-artifacts/browser-receipt-edit-20261112/summary.json) · [原始记录绑定回执](../.e2e-artifacts/browser-receipt-edit-20261112/original-B/bindings.jsonl) · [替换记录绑定回执](../.e2e-artifacts/browser-receipt-edit-20261112/alternate-B/bindings.jsonl)

## 复现

```powershell
python -m pytest -q tests/test_browser_receipt_edit_ab.py tests/test_browser_outcome_ab.py tests/test_codemode_json_edit.py
python -u -m tools.browser_receipt_edit_ab --run-dir .e2e-artifacts/browser-receipt-edit-20261112
```

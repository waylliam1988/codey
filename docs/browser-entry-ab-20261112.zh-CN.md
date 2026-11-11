# 浏览器常用入口独立 A/B

## 对照

A 使用当前生产工具执行和完成链。B 只开启当前候选，沿用相同的用户任务、Chromium、权限、执行器、原始回执和完成门。实验适配器只在各自 worker 中安装。

| 候选 | 开启的行为 | 场景 |
| --- | --- | --- |
| `browser-results` | 浏览器入口返回状态、直接数据、回执引用和可读视图。已结算文件动作后发生程序错误时反馈原回执。 | `basic`、`upload_bound`、`download` |
| `browser-targets` | 当前快照内按控件名和可选角色匹配；零匹配或多匹配返回实际候选及快照身份。 | `basic`、`stale_ref`、`upload` |
| `browser-wait` | 在登记子调用前按原工具契约校验等待参数，保留原始错误。 | `basic`、`snapshot_restore`、`stale_ref` |
| `browser-properties` | 通过原工具读取当前绑定 URL 和单个合法属性，交付值和类型。 | `structured`、`legacy_structured` |

原有 `tools.*` 与 `Result` / `unwrap` 契约照常可用。显式旧引用照常被生产浏览器拒绝。适配器沿用现有回放授权，下载成功后的返回访问错误只补充真实动作凭证。上传保留 `effect_verified=false` 的原语义。

## 方法

确定性检查使用脚本化 HTTP 与真实 Chromium。核对原始回执、模型实际请求、真实超时和旧引用拒绝、零匹配及同名控件拒绝，以及文件下载后返回访问出错时的动作次数。另用四项同时开启的 Chromium 测试验证接口可组合。

```powershell
python -m pytest -q tests/test_browser_entry_candidates.py tests/test_browser_entry_surface.py tests/test_browser_production_execution.py tests/test_protocol_surface.py
python -u -m tools.protocol_surface_ab --config browser-results --run-dir .e2e-artifacts/browser-entry-20261112-browser-results --repeats 1 --timeout 75 --budget 240 --max-turns 7 --seed 123456789
```

其余三项更换 `--config` 和独立目录。使用本地 KoboldCpp Gemma4 12B，temperature 0。每场景一对，交替 AB/BA，共 22 个独立配置单元。单对结果用于发现机制采用和失败位置。发生明确的发送前上下文超限时保留该失败，并继续排队后续独立单元。

## 独立实机结果

| 候选 | A | B | 实际模型请求 A → B | 可归因的证据 |
| --- | ---: | ---: | ---: | --- |
| 结果形状与可读视图 | 2/3 | 2/3 | 11 → 8 | 无新结果入口调用。B 普通下载在结果访问失败后收到已结算回执，之后完成。 |
| 控件定位 | 3/3 | 2/3 | 9 → 9 | 无新控件入口调用。两侧 `stale_ref` 都真实拒绝一次旧引用并通过。 |
| 等待与错误反馈 | 1/3 | 2/3 | 12 → 6 | B 的两个通过单元均使用合法等待参数，实机未触发新增校验。 |
| URL 和属性读取 | 0/2 | 0/2 | 8 → 5 | 无新 URL 或属性入口调用。一个 B 单元的第 2 次请求超时。 |

- **下载动作反馈实际送达。** 结果组 `download/B` 只成功下载一次。之后在访问 `Result` 字段时失败，失败父回执和下一次实际请求都包含同一下载回执编号，模型随后以真实哈希完成。A 同对成功下载两次。B 在 `basic` 漏交付真实事实。B 的 `upload_bound` 通过沿用了已有生产绑定能力。
- **可选入口仍有采用障碍。** 结果、控件和属性组的 B 请求均包含新入口声明。审计原始 CodeMode 程序和对应真实叶调用，三个组均未发现新入口被实际采用。确定性测试覆盖了这些入口的有效行为。实机通过差异保留原分，归因以真实触发证据为准。
- **等待预校验解决了具体诊断缺口。** 定向测试用真实 durable 执行验证缺超时和非法条件的明确错误，以及有效参数进入真实等待后返回独立的超时错误。实机 `snapshot_restore/B` 通过，A 耗尽轮次。两侧 `stale_ref` 均触发真实拒绝，最终都未通过事实验收。
- **属性组的超时单元保留。** `structured/B` 的第 2 次请求在 75 秒取消，原矩阵停止。确认服务空闲后，只运行了尚未开始的 `legacy_structured` 配对。续跑使用同一模型、脚本和任务设置，该单独目录内按 A、B 顺序运行。

[结果组](../.e2e-artifacts/browser-entry-20261112-browser-results/entry-audit.json) · [控件组](../.e2e-artifacts/browser-entry-20261112-browser-targets/entry-audit.json) · [等待组](../.e2e-artifacts/browser-entry-20261112-browser-wait/entry-audit.json) · [属性组](../.e2e-artifacts/browser-entry-20261112-browser-properties/entry-audit.json) · [属性组续跑](../.e2e-artifacts/browser-entry-20261112-browser-properties-legacy/entry-audit.json)

## 组合验收

将实机已触发的文件动作反馈与确定性验证过的等待预校验组合，配对运行 `download` 和 `stale_ref`。

```powershell
python -u -m tools.protocol_surface_ab --config browser-entry-combined --entry-feature browser-results --entry-feature browser-wait --scenario download --scenario stale_ref --run-dir .e2e-artifacts/browser-entry-20261112-combined --repeats 1 --timeout 75 --budget 240 --max-turns 7 --seed 123456789
```

组合 A、B 均为 **1/2**，实际请求各 **7 次**。下载均通过，各只执行一次。旧引用场景两侧均产生一次真实拒绝并读到页面，最终事实仍未通过。B 没有使用新结果入口，也没有触发等待预校验。本次组合显示这两项可以一起启用，实机完成率与调用次数持平。四项同时开启的确定性 Chromium 用例通过。 [组合审计](../.e2e-artifacts/browser-entry-20261112-combined/entry-audit.json)

26 个实机单元的各自目录已保留，全部目录的生产和实验脚本摘要均与冻结清单一致。151 项回归通过，其中包括本次新增的 16 个用例。退出时有 RAM disk 清理告警。Ruff、strict mypy 和 `git diff --check` 通过。生产代码未改。

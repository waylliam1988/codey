# 浏览器入口复验

A 为生产提交 `8a207bb7`。B 的变化仅在实验进程生效。两组使用同一真实 Chromium 页面和本地 KoboldCpp Gemma4 12B，temperature 0、seed 123456789，每场景一对。等待组的首个程序在两臂相同，由脚本执行到故意传错参数，后续交给真实模型。属性交付组由脚本经各臂接口做真实采集，再由真实模型作答。另跑自主属性组、控件组和结果组。结果组只比较数据形状和回执引用，A、B 都使用已合入的生产下载错误反馈。

等待预校验后来由 `3c7a533c` 合入生产。当前回归要求两臂都在登记子调用前拒绝非法等待并指出字段。历史实机表格保留当时的 A/B 结果，原始目录的 `frozen-harness` 保存当时的实验脚本。

## 等待预校验

| 场景 | A | B | 首次成功等待与读取 |
| --- | --- | --- | --- |
| `basic` | 失败，3 次模型请求 | 通过，2 次 | 两侧均在第 2 轮 |
| `snapshot_restore` | 通过，2 次 | 通过，2 次 | 两侧均在第 2 轮 |
| `stale_ref` | 失败，3 次 | 失败，3 次 | 两侧均未执行成功等待或读取 |

B 的实际请求在 3/3 次注入后收到具体字段错误，A 的 3/3 次都收到控制流错误。`basic` 的 A 其实已经成功等待并读取；之后的父调用返回 `undefined`，后续回放也失败。`stale_ref` 的注入首轮两侧都先真拒绝旧引用，再重新快照并点击成功。接手的模型重新执行了旧引用诊断，两侧均在获取新快照后又传入过期引用。A 还两次把导航 URL 的连字符写成下划线。预校验修复了字段诊断，单对样本尚未看到它让成功等待或读取提前。[原始审计](../.e2e-artifacts/browser-entry-followup-wait/followup-audit.json)

## URL、token 和业务值

固定采集组 A 1/2、B 2/2，均共 2 次实际模型请求。四个单元的真实叶回执都包含当前 URL、页面 token 和正文事实，三类值均进入实际请求。B 由脚本明确调用 `browser.url` 和 `browser.property`。`legacy_structured/A` 收到正确 URL，最后把 `production-browser-record` 写成 `production_browser_record`。`structured` 两侧都通过。这个配对验证了交付路径，并观察到一次作答转录错误。[固定采集审计](../.e2e-artifacts/browser-entry-followup-properties-delivery/followup-audit.json)

脚本化回归在两个场景分别验证三种情况。缺少真实 token 读取时，浏览器结果中只有 URL 和正文，完成门拒绝。三类事实已经交付但最终答案漏报业务值时，任务可返回 `done`，事后事实验收仍失败。三类事实全部正确交付并写入答案时通过。

自主采集组 A、B 均 0/2，实际请求 7 → 9。B 两次都使用原有 `browser_eval` 和 `browser_tab_list` 取得正确 token 和 URL。`structured/B` 对字符串正文再取 `.text`，调用 `.match()` 时抛错。`legacy_structured/B` 同样产生 `undefined`，在编码父结果时失败，后续回放也失败。两次读到的 token 均未进入后续实际请求。`structured/A` 也因 `.match()` 调用失败。`legacy_structured/A` 的正确 URL 和 token 进入实际请求，正文业务值仍缺失，最终答案使用了输出标记和 `verified`。自主组未观察到新增属性入口调用。[自主采集审计](../.e2e-artifacts/browser-entry-followup-properties-autonomous/followup-audit.json)

## 控件定位和返回形状

真实 Chromium 回归在页面放置两个同名按钮，同时测试零匹配。两类失败都返回当前快照真实 `page_id`、`generation` 和实际候选，且未执行点击。按名称定位的上传和下载各真实执行一次，回执数据、SHA256 和回执引用与下一次请求中的结果一致。上传仍明确记录 `effect_verified=false`，即已选择附件。旧引用回归验证真实拒绝后能用新快照继续。

控件自主组 A 2/3、B 0/3，实际请求 14 → 16。A 通过 `basic` 和 `upload`。`stale_ref/A` 有两次真实旧引用拒绝，最终报告的业务值错误。`stale_ref/B` 未执行到真实旧引用拒绝。B 三个单元均未调用新增命名入口。[控件审计](../.e2e-artifacts/browser-entry-followup-targets/followup-audit.json)

结果自主组 A、B 各 3/3、各 9 次请求。普通下载两侧各下载一次。`basic` 为 3 → 4 次，`upload_bound` 为 3 → 2 次，`download` 各 3 次。B 三个单元均未调用新增结果入口，完成使用原生产路径。[结果审计](../.e2e-artifacts/browser-entry-followup-results/followup-audit.json)

## 结论

26 个实机单元全部完成，13 对均可比，生产源码和冻结脚本摘要一致。每个场景只测一对，结果适合判断具体机制是否触发。下一项生产候选是等待预校验，它在三对中都修复了字段诊断，且脚本回归确认非法调用在子调用登记前拦截、合法等待仍返回真实超时。属性、控件和结果入口保留为候选。属性组下一步应优先验证成功的原始读取能否在父程序出错后送达，以及如何让最终答复使用已交付的事实。

54 项回归通过。Ruff 和 strict mypy 检查了本次实验与测试代码。此次只改实验、测试和报告。

## 复现

```powershell
python -m pytest -q tests/test_browser_entry_followup.py tests/test_browser_entry_candidates.py tests/test_browser_entry_surface.py tests/test_browser_production_execution.py
python -u -m tools.protocol_surface_ab --config browser-wait-recovery --run-dir .e2e-artifacts/browser-entry-followup-wait --repeats 1 --timeout 110 --budget 420 --max-turns 6
```

其余四组依次使用 `browser-properties-delivery`、`browser-properties`、`browser-targets`、`browser-result-data` 和独立的 `--run-dir`，其余选项相同，单元预算为 480 秒。用 `python -m tools.browser_entry_followup_audit <run-dir> ...` 从保留的浏览器回执和实际请求生成 `followup-audit.json`。

# basic 导航 URL 绑定：固定回复与实机 A/B（2026-11-13）

结论：固定回复证明，原失败修复代码只换导航参数，就能从真实 404 / 缺 ref 走到正确页面并完成浏览器链。实机 **A 1/2、B 2/2**，B 两次都采用任务页面选择器；但 A 唯一失败是 JavaScript 编译错误，没有执行导航，本轮没有实机地址抄错的配对，不能把完成率差异归因于 URL 绑定。保持实验状态，不改生产。

脚本为 [basic_navigation_url_ab.py](../../tools/basic_navigation_url_ab.py)，测试为 [test_basic_navigation_url_ab.py](../../tests/test_basic_navigation_url_ab.py)。[机器结果](basic-navigation-url-20261113.json)和[原始记录包](basic-navigation-url-20261113.zip)随脚本在 `master` 提交；未改 `codey/`，未合并或推送。Hybrid、structured、stale_ref 的接口均保持原样。

A 使用原生产 `tools.browser_navigate({url})`。B 在 CodeMode 声明中只把这一项改成 `tools.browser_navigate({target:'task'})`，模型选择动作和时机，隔离的 guest 适配器将它降低为原 `browser_navigate({url:任务原地址})`。其他浏览器工具、真实授权、冻结的底层工具 schema、执行器、回执和生产完成约束均保留。模型回复在实机中不做改写；传旧 url 参数的 B 调用会明确拒绝，不偷偷纠正模型给出的另一个地址。

目标来自实际任务文字。复用现有显式网页意图判断，再核对唯一原始 URL，保留其大小写、路径、查询和 fragment 字符，不把规范化比较键当导航值。零目标、多目标、同一规范化地址但不同原始地址、未获授权或不合格地址均拒绝绑定。单元测试确认 selector 未被调用时不会导航，传 url / 错误 target / 额外参数不能发出导航，其他工具声明和导航输出类型不变。普通生产入口已有来源预取，两组均保留；统计正式导航时只数 `browser_navigate` 回执，不把预取算成模型选用了导航入口。

固定对照复用现有原始 basic 回复夹具的前两条：第一条保留真实编译错误；第二条是错误 URL 的完整修复代码。A 原样重放，B 只把导航调用的一个参数对象换成 `{target:'task'}`，其余代码不动；最后两组都追加同一条受控 done，用来验收完整交付。

| 固定对照 | A | B |
| --- | --- | --- |
| 第一条编译错误 | 保持失败，无子调用 | 保持失败，无子调用 |
| 第二条正式导航 | 原下划线地址，HTTP 404 | 任务原地址，HTTP 200 |
| 后续步骤 | 空控件快照，缺 ref；保留导航和快照回执 | 真实填写、重新快照、点击、指定等待、完整读回 |
| 最终受控交付 | 不通过 | 通过，真实正文含 173 / QUARTZ_851 |

测试起初还完整重放了四条原回复。B 的后三条都完成浏览器链，但连续返回相同正文触发生产 no_progress，来不及处理追加 done。因此最终因果对照限定“首次失败 → 第一次修复 → 同一 done”，不把重复成功轮次的中断说成 URL 绑定失败。完整重复对照的原始记录也保留在包内 `development/full-repeated-replies/`，既有全量原回复回归仍通过。另一次早期失败是实验日志未解码 mappingproxy，已只修实验记录边界，记录保留在 checks。

实机无固定回复、无预设浏览器动作。模型使用本地 `http://127.0.0.1:5001/v1` 的 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，temperature 0、thinking 关闭。两对 seed 为 `123456789`、`123456790`，顺序分别 A/B、B/A，每项最多 6 轮。任务只因 B 的新导航合同多一条参数说明，原始任务来源、页面和完成要求相同。

| seed | A | B |
| --- | --- | --- |
| 123456789 | 通过；正式导航一次，地址正确；3 次模型请求 | 通过；采用 task 选择器，正式导航一次；2 次请求 |
| 123456790 | 失败；4 次编译拒绝，无正式导航；4 次请求 | 通过；采用 task 选择器，正式导航一次；2 次请求 |

A 第二项四次都把 return 写成实际回车加 eturn，生产回执均为 syntax_error，没有一次真实浏览器子调用。总导航列表仍有来源预取的地址，这不构成正式导航覆盖。B 两项均完整走过“导航 → 快照 → 填写 → 新快照 → 点击 → 指定等待 → 完整读取”，实际正文和最终答案都包含正确业务值，未另加事实交付绑定。

| 本轮整体成本 | A | B |
| --- | --- | --- |
| 真实模型请求 | 7 | 4 |
| prompt token | 51339 | 26638 |
| completion token | 1504 | 689 |
| 累计模型回复耗时（秒） | 68.843 | 35.203 |

请求数包含生产中的规划和重规划。A 的失败编译反复生成，A 第一项也有未形成工具回执的额外模型回复。因此 7 对 4 是观测结果，不能断言 URL 参数传递本身省了三次调用。样本只有两对，时钟上下文也随真实请求变化；本轮证据支持“绑定机制消除了固定抄写错误，模型会使用入口”，尚不足以估计实机可靠率或因果收益。

新增定向测试 16 项通过；与既有 task-contract 固定回复合跑 27 项通过。采样后修正了一个诊断导出字段：browser_navigate 的实际观察地址字段是 url，不是 page_url；保留原始报告，并从同批回执重建 observed_url，所有通过/失败判定不变。最终 16 项再验收通过，未修改提示、执行逻辑或重跑 A/B。

全仓 Ruff、生产全树 mypy（680 文件）、包含新增脚本与测试的 pyrefly 和 ty、新文件 mypy `--disallow-any-expr`、内核类型精度门及 git diff --check 均通过；没有新增 Any、类型忽略或放宽配置。pyrefly 仍报告原有 35 suppressed / 157 warnings，不代表全仓零警告。生产源码实机前后哈希相同：`d326c7e78cdf46062992381eca5ca8ed21ba4a295b24bbe77f65a5c879846038`。

```powershell
python -m pytest tests/test_basic_navigation_url_ab.py tests/test_task_contract_fixed_replies.py -q
python -u -m tools.basic_navigation_url_ab --run-dir .e2e-artifacts/basic-navigation-new --pairs 2 --max-turns 6
```

实机目录须全新；退出码 0 表示采样完成且生产源码未变，不表示两组全部成功。记录包附逐文件 SHA-256 manifest，包含完整实机请求/回复、原任务、导航合同、原始与执行程序、真实工具回执、逐项判定、固定参数替换对照及检查日志。这是受控页面的导航接口实验，未验证多来源选择、中途换站、重定向或一般网站可靠性。

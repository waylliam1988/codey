# 生产参数绑定与否定地址回归（2026-11-13）

本轮落地两个独立改动：CodeMode 的 `open_result({id: "r2"})` 兼容，以及普通 Chat 的 `browser_navigate({target: "task"})`。它们吸收字段名误写和任务 URL 抄写的定向实验收益，不能代表已经解决 Result 解包、代码生成、来源选择、业务值交付或 Hybrid 编辑凭证问题。没有新增详情导航入口，详情仍按实际元素引用点击。

## 生产行为

- Host 在新执行与 `repair_of` 分流前，将单独的合法 `id: rN` 转为 `result_id`。工具声明继续只展示 `result_id`；双字段、额外字段、空值和未观察引用不会打开来源。两种写法交换后，修复回放仍复用同一条原始回执。
- 原始 URL 和比较键由同一份意图解析产生。只有普通 Chat、唯一明确且与入场来源要求一致的地址、已授权的 CodeMode 导航才提供 task 选择器。模型决定调用时机，显式 `{url: ...}` 仍可导航其他获准页面。
- 绑定随 `TurnSnapshot` 持久化；新版快照为 7，版本 6 按无绑定恢复，继续遵守原有实现兼容检查。目录声明与执行目录使用同一个构建函数，父执行指纹包含绑定值。Host 将选择器转为原来的 `{url: 原始地址}`，子调用仍走生产授权、执行与回执链。
- 上传和浏览器复制专用合同保持各自的输入绑定；strict Research、Hybrid 和 project 不新增 task 选择器。
- 边界回归发现浏览器工具会被可见性计算重新加入 `controller_denied` 已排除的集合，现已在构建冻结库存时修正浏览器工具的交集。没有改变项目工具的控制器规则。

## 否定地址：先复现，再修复，再启动实机

用户指出的 `Open the report and do not visit https://example.com/records/Case-A` 确实存在错误绑定。原解析按整句是否有肯定动作收集全部 URL，前面的 `Open` 把后面的否定地址一并加入必需来源；草稿随后把它冻结成导航目标，恢复后仍存在。

已修复共享的网页意图解析：逐个 URL 关联相邻的动作及其否定状态，不再用一个肯定动作授权整句所有地址。同一比较键同时被肯定和否定时，移除该来源要求。地址边界不明时不生成原始 URL 绑定。

新增九项中英文、前后顺序、混合地址和冲突指令回归，修复前全部失败，原始失败日志保留；修复后检查来源要求、原始地址、快照往返，以及通过生产内核实际调用选择器时是否拒绝执行。用户原句的 `urls` 和 `original_urls` 均为空，绑定和恢复绑定均为 `null`。

修复后先完成相关回归、Ruff 和 mypy，才开始下述实机矩阵。没有在错误绑定版本上运行实机。

## 验证记录

本地原始记录保存在 [.e2e-artifacts/production-bound-arguments-20261113](../../.e2e-artifacts/production-bound-arguments-20261113/)；否定句修复前后的日志分别为 `negated-url-red.log`、`negated-url-green.log`，另有 `negated-url-fixed.json`。

- 第一笔生产 Host / 内核与回放回归：25 项通过。
- 较大生产回归组：285 项通过，包含原失败回复、搜索与真实点击、复制、完成检查、快照、模型输入绑定和类型精度门。该组在否定地址修复前启动；否定地址修复后的专项组另跑 82 项通过，两组有重叠。退出清理阶段出现 Chromium 缓存文件占用的 RAM 盘清理警告，测试断言及退出码通过。
- 全部生产修改完成、实机结束后，最终生产回归组 259 项通过，包含参数绑定、否定地址、旧来源要求、回放、上传、生产边界、来源覆盖及持久请求/回执恢复。日志为 `final-production-regressions.log`；各组有重叠，不相加为独立用例数。
- 全仓 Ruff 通过。生产全树及三个本轮测试文件的 mypy 通过，共 683 个文件；对所有本轮修改的 Python 模块另启用 `disallow_any_expr`，没有新增或保留显式/隐式 `Any`、`cast` 或类型忽略，也未放宽仓库检查配置。
- 未运行全量 pytest，未复制 worktree。

最终检查命令（mypy 临时配置沿用仓库配置，只为本轮修改模块增加 `disallow_any_expr = true`，未关闭任何检查）：

```powershell
python -m ruff check .
python -m mypy --config-file .e2e-artifacts/production-bound-arguments-20261113/mypy-scoped.toml codey tests/test_task_navigation_binding.py tests/test_codemode_open_result_arguments.py tests/test_chat_source_gaps_contract.py
python -m tools.kernel_type_precision_gate
git diff --check
```

## 完整实机矩阵：首轮 10/12，失败项单次重跑 0/2

真实模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，端点 `http://127.0.0.1:5001/v1`，seed `123456789`，真实 Chromium，受控网页。沿用完整生产注册表、冻结合同、CodeMode、执行器、完成检查及持久回执，不修改工具或模型回复。

| 场景 | 首次结果 | 实际覆盖 |
| --- | --- | --- |
| basic | 失败 | 四次编译拒绝，没有正式浏览器子调用；采用了 task 选择器，但未测到执行 |
| structured | 通过 | 实际使用 task 选择器，导航回执仍为原始 URL；真实表单、等待、读回及最终结构化交付通过 |
| legacy_structured | 通过 | 一次返回值处理错误后恢复；完整浏览器链和交付通过 |
| reread | 通过 | 快照恢复后完成两次真实读取、表单操作与交付 |
| stale_ref | 通过 | 真实旧引用操作被拒绝一次，随后恢复并完成 |
| stale_edit / Hybrid | 失败 | 正确导航及快照成功，后续三次编译拒绝，未进入文件版本检查 |
| upload | 通过 | 两次导航；实际附件选择一次，之后真实等待和读回 |
| download | 通过 | 两次 Result 误解包错误后恢复；实际下载一次，文件与交付 SHA256 校验通过 |
| strict Research | 通过 | 完整浏览器操作、等待、读取和带证据的研究提交 |
| strict Research sequence | 通过 | 完整顺序链；后续请求确实切换为仅研究提交合同 |
| source_failure | 通过 | 真实来源失败回执，任务阻断 |
| fixed_failure | 通过 | 导航及真实读取保留，来源失败后阻断 |

对两个失败场景各按原参数另跑一次，不改代码、提示、合同、回复或判定。`basic` 再次四次编译拒绝，仍没有正式导航。Hybrid 重跑完成真实导航、表单、等待和正确读回，但编辑程序生成 `changes\tib`，两次编译拒绝；另外两次使用未知 `repair_of` token，被正确拒绝。没有发出一次 `read_file` 或 `edit` 子调用，文件仍为初始值，版本冲突恢复仍未被本轮实机覆盖。

两次 `basic` 的原始模型回复都把 `return` 写成实际回车加 `eturn`。Hybrid 首次也出现该转义错误；重跑则是编辑代码中的实际 tab 加 `ib`。这些与生产回执中的编译错误一致，未知修复 token 也来自模型原始回复。本轮未发现实机环境失败或新增生产确定性错误，未为这些模型错误修改生产合同。

三批采样的生产源码哈希前后均为 `219a8c844ff991f5ed601e058bc30b535bc6658170defd3c475f96f68addc704`。首轮和重跑结果分别保留，不合并成另一项完成率。该矩阵不是实机 A/B，不能由 10/12 推断可靠率或因果收益。

原始请求、回复、任务、summary、真实回执与持久状态分别保存在：

- [完整首轮](../../.e2e-artifacts/production-bound-arguments-full-surface-20261113-first/)
- [basic 单次重跑](../../.e2e-artifacts/production-bound-arguments-full-surface-20261113-rerun-basic/)
- [Hybrid 单次重跑](../../.e2e-artifacts/production-bound-arguments-full-surface-20261113-rerun-hybrid/)

[机器摘要](production-bound-arguments-20261113.json)只汇总独立批次；完整原始记录保留在上述本地目录。剩余限制是模型代码生成、Result 使用、来源选择和交付等原有问题；本轮也不将 URL 绑定视为 Hybrid 文件冲突恢复的验收。

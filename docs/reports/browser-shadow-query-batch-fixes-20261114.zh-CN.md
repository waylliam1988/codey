# Shadow DOM 查询、快照分组与引用审计修复

基线提交 `b67e8f3b`。直接修改当前工作区，没有创建或复制 worktree，没有运行全量 pytest。原始复现、性能测量、生产回归和实机记录保存在 `.e2e-artifacts/browser-shadow-batch-20261114/`。

## 确认的问题与修改

三类问题均已在修复前复现。`pytest-before.log` 为 16 失败、7 通过，另有 `pytest-neighbor-before.log` 单独确认外部节点更新会错误结算内部查询；这些原记录保留。

- **Shadow DOM 查询目标错绑。** `aria-controls` 现在只从控件所在 DOM 根解析，必须在该根唯一匹配；原生 `details` 直接保留实际父节点。通过实际 ElementHandle 取得 Chromium backend node ID，后续读取继续使用原有 CDP 节点身份，不保存或重放选择器、文档序号。绑定前再次检查控件声明仍指向原节点。Playwright 与 CDP 的公开接口没有共用的远程句柄编号，因此以一次性、不可枚举的节点引用跨接，随即删除，不维护页面注册表或第二份查询状态。
- **快照分组的平方次跨进程比较。** 在一次浏览器观察中复用原 `ELEMENT_METADATA` 采集逻辑，使用局部 `WeakMap` 按容器节点对象分组；同次观察分别交回结构 JSON 和实际容器句柄。Python 不再逐对比较节点。原控件选择与可见性规则、快照上限和正式输出合同保留；分组文本仍由正式结构字段渲染。执行前继续核验实际控件、实际容器和观察到的标签关系，展示编号不承担执行身份。
- **代码内标签被误计为引用。** 实验审计扫描标签前调用生产已有的 `mask_markdown_code`，屏蔽行内代码、反引号或波浪线围栏、缩进代码。实际 passage 切片和版本恢复继续使用原 `restore_passages`，没有新增引用合同或解析器。

复验还发现修复草稿在关系不存在或不唯一时，对空 JSHandle 读取属性会抛异常。已在同一关系观察边界统一返回无节点的观察结果，维持原有“没有可核验绑定”的行为。没有把它当成成功绑定，也不回退到外层同名 ID。

## 性能测量

命令：`python -u -m tools.browser_snapshot_group_probe --run-dir <新记录目录> --repeats 3`。同一探针、同一页面分别生成 10、40、80 个独立命名容器；每个规模重复三次。表内耗时为中位数，调用数三次一致。

| 控件／容器数 | 修复前 same_node 调用 | 修复后调用 | 修复前耗时 | 修复后耗时 |
|---|---:|---:|---:|---:|
| 10 | 45 | 0 | 0.216 秒 | 0.081 秒 |
| 40 | 780 | 0 | 1.607 秒 | 0.153 秒 |
| 80 | 3,160 | 0 | 5.398 秒 | 0.316 秒 |

80 个容器三次原始耗时分别为 5.645401、5.398499、5.009303 秒，修复后为 0.316436、0.321079、0.290789 秒。确定性验收是快照构建阶段逐对 `same_node` 调用为零；耗时属于本机测量，不代表所有网页的绝对性能。剩余控件句柄、可见性检查和正文采集仍有成本。

前测生产摘要为 `6cc040ea5800f421dd87d91deb511d0c13c68b4267c0c3324eff30663dace45b`，后测为 `743df391257f6ba877dea3f4d57c12e144a0fa63cb83b7d2c936bbd1cacb13d2`；各自前后摘要一致。原始结果分别在 `perf-before/summary.json`、`perf-after/summary.json`。

## 保存实验重新审计

对 `bc3-before`、`bc3-final`、`browser-review-20261114/scope-current` 三组保存请求和回执离线重审，共 12 个单元，分别仍为 4/4、4/4、3/4，证据评分均未变化。没有请求模型或重新访问网页。新结果写入本轮 `scope-reaudit.json`，未覆盖原 summary 或上轮重审文件。

这次合成反例证明代码内标签曾被误计，不能据此声称历史报告已被污染。历史两个 4/4 仍是来源级引用结果，不能当作精确 passage 归属的通过证明。

## 验证结果

全树 Ruff 通过。生产树及四个修改测试／脚本的 mypy 检查为 691 文件通过；对全部七个修改 Python 模块额外启用 `disallow_any_expr`、`disallow_any_explicit`，配置保存在本轮 `mypy.toml`。`kernel_type_precision_gate` 通过。没有加入 `Any`、`cast`、`type: ignore` 或检查豁免。

首次 mypy 调用因为工具模块被识别成两种包名而终止，改用仓库已有显式包名模式 `--explicit-package-bases`；随后修正测试／探针从非公开模块导入函数、JSON 容器类型的问题。首次 Ruff 发现探针闭包未绑定循环变量，已明确绑定。初次失败日志全部保留，最终检查使用原严格配置并增加上述约束，没有关闭检查。

明确选择 24 个相关文件运行扩大回归，覆盖正式观察、查询、回执交接、详情、schema、执行、快照／进程恢复、冷恢复、上传下载、引用扫描和相关实验入口；没有运行全量 pytest。`pytest-production.log` 为 **288 通过、7 失败**，655.10 秒。

七项失败全部位于已有 `tests/test_browser_chat_followup_gate.py`，对应历史实验框架。已查看原始 provider 请求、动作和回执：直接工具没有按旧实验预期开放，脚本却直接发送旧工具调用；另有 CodeMode 分支实际填写、点击和正式读取均成功，但预置 done 后任务仍需模型请求，固定回复耗尽。为了区分本轮回归与已有问题，把三个修改生产文件和一个实验脚本暂时原位切换到 `b67e8f3b` 内容，运行该文件，结果同样 **7 失败、5 通过**，失败名称逐项一致，见 `pytest-legacy-baseline.log`。这四个文件的当前字节先保存在内存，最后由 `finally` 原样恢复；没有复制工作树或创建替代执行器。`legacy-baseline-proof.json` 记录基线、恢复后的摘要和每个文件字节校验，恢复后摘要与完整实机一致。没有扩改该历史实验框架，也不把这七项写成通过。

基线包装脚本在保存 pytest 原始日志后，打印包含替换字符的中文时遇到 GBK 编码异常；`finally` 的文件恢复与校验已完成。基线测试结果取自原始 pytest 日志，未靠包装脚本退出码推断。该打印问题未影响生产执行或实机矩阵。

单独定向回归在草稿空句柄问题修正后为 23/23；随后补充“Shadow 根没有目标但外部有同名 ID”的负例，最终 `pytest-targeted-final.log` 为 **24/24**，17.11 秒。它覆盖 Shadow 原生详情与 ID 关联、实际节点替换、邻居延迟更新、无／多匹配拒绝外部兜底、10／40／80 分组调用数，以及代码内来源／passage 标签负例和正常正文引用正例。原有插入无关区域、同名容器替换、双 Shadow 分组回归包含在扩大集合的通过项中。测试集合重叠，不把多轮通过数累加。补充负例后，最终 Ruff 与 691 文件的严格 mypy 再次通过。

## 完整 full-surface 实机矩阵

命令：`python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/browser-shadow-batch-20261114/full-surface-first --scenario all --timeout 180`。

本地 Gemma4 12B Q4_K_M，seed 123456789、temperature 0，使用现有生产 registry、kernel、CodeMode、Research 和持久化回执。首轮全部 12 项跑完，结果 **10/12**，前后源码摘要均为 `743df391257f6ba877dea3f4d57c12e144a0fa63cb83b7d2c936bbd1cacb13d2`。

| 单元 | 结果 |
|---|---|
| Chat basic | 通过 |
| structured 解释与精确字段 | 失败 |
| legacy_structured 值读取及标签页 | 通过 |
| reread：恢复冻结快照、重复读取 | 通过 |
| stale_ref | 失败 |
| Hybrid stale_edit | 通过 |
| 上传 | 通过 |
| 下载 | 通过 |
| strict Research | 通过 |
| strict Research sequence | 通过 |
| source_failure | 按预期阻塞，通过 |
| fixed_failure | 按预期阻塞，通过 |

失败均检查原 `request.json`、`provider.jsonl`、`receipts.json`：

- structured：模型四次提交缺少闭合结构的 JavaScript，均在编译阶段失败，`error_code=syntax_error`、`child_effect_ids=[]`，没有执行浏览器子工具。模型已拿到原调用合同与修复提示，仍重复无效代码。这是模型输出错误。
- stale_ref：原任务明确要求填写后先尝试旧按钮引用；模型从 g2 的首次观察转而直接使用填写后交付的 g4 新引用，`failed_stale_refs=0`，却在最终答复声称经历了旧引用拒绝。字段答对也仍判该单元失败。这是模型遗漏验收步骤并错误描述执行过程；真实旧引用拒绝另有确定性生产回归。

没有为这两项修改生产合同，也未发现实机矩阵有预期外环境失败。失败单元各在独立目录重跑一次，均为 **0/1**：

- `structured-recheck`：这次子工具已执行，模型遗漏显式返回，得到 `missing_return`；随后三次带原 repair token 却更换子调用顺序，收到 `repair_rejected`。原已提交子回执保留，拒绝的重放没有重新执行子动作。仍是模型输出／修复错误，没有放宽合同。
- `stale-ref-recheck`：仍直接使用新引用，旧引用拒绝计数为 0。

两轮重跑的源码摘要前后一致，且与首轮完整矩阵相同。`failure-audit.json` 汇总原请求位置、模型动作、错误回执和子效果引用。重跑不覆盖首次结果，也没有拼接成新的完整通过矩阵。

## 剩余限制

这次修正查询关系解析和节点身份，没有扩展普通正式来源的 Shadow DOM 正文采集。即使实际目标节点已更新，正式来源未取得对应正文时，原查询仍须保持未确认。没有新增局部来源或 passage 范围隔离；全文扩展语义不变，因此不能声称已经消除全部跨记录证据混用。

同一文档的原 pending 防重复约束、授权、文档身份和来源范围检查继续由现有执行链负责。没有新运行时、新工具合同、并行查询状态表或双写事实；模型实机错误不会通过放宽生产合同处理。
